"""Tests for tapout Slice 1."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tapout import __version__
from tapout.cli import app
from tapout.handoff import (
    SCHEMA_VERSION,
    SUMMARIZATION_PROMPT,
    TaskState,
    extract_json_block,
    load_state,
    parse_task_state,
    render_markdown,
    write_artifacts,
)
from tapout.capture import (
    CaptureError,
    _mangle_cwd,
    condense_transcript,
    find_project_transcript,
    run_capture,
)
from tapout.detect import resolve_executable
from tapout.monitor import (
    indicator,
    is_limit_hit,
    parse_status,
    read_state,
    run_statusline,
    run_watch,
    watch_line,
)
from tapout.registry import load_registry
from tapout.resume import (
    ResumeError,
    batbadbut_risk,
    build_opening_prompt,
    is_batch_wrapper,
    launch,
    prepare_resume,
    record_history,
    render_template,
)

runner = CliRunner()

DEMO = Path(__file__).resolve().parents[1] / "demo" / "fake-session.md"

VALID = {
    "schema_version": 1,
    "created_at": "2026-07-05T14:03:00+00:00",
    "source_agent": "claude",
    "task_title": "Test task",
    "goal": "Do the thing.",
    "plan": [{"step": "first", "status": "done"}, {"step": "second", "status": "todo"}],
    "decisions": ["chose X"],
    "files_touched": ["a.py"],
    "next_steps": ["do second"],
    "blockers": [],
    "commands_to_verify": ["pytest -q"],
}


def _block(obj: dict) -> str:
    return f"prose before\n```json\n{json.dumps(obj)}\n```\nprose after"


# --- schema ---------------------------------------------------------------

def test_parse_valid():
    state = parse_task_state(_block(VALID))
    assert state.task_title == "Test task"
    assert state.plan[0].status.value == "done"
    assert state.schema_version == SCHEMA_VERSION


def test_extract_json_block_raw():
    assert extract_json_block(json.dumps(VALID)).startswith("{")


def test_no_json_raises():
    with pytest.raises(ValueError, match="No JSON found"):
        parse_task_state("just some prose, no block")


def test_missing_field_error():
    bad = dict(VALID)
    del bad["goal"]
    with pytest.raises(ValueError) as exc:
        parse_task_state(_block(bad))
    assert "goal" in str(exc.value)


def test_bad_status_enum_error():
    bad = json.loads(json.dumps(VALID))
    bad["plan"][0]["status"] = "finished"
    with pytest.raises(ValueError) as exc:
        parse_task_state(_block(bad))
    msg = str(exc.value)
    assert "done, in_progress, todo" in msg


def test_bad_json_error():
    with pytest.raises(ValueError, match="not valid JSON"):
        parse_task_state("```json\n{not json}\n```")


def test_to_json_stable_and_sorted():
    state = TaskState.model_validate(VALID)
    text = state.to_json()
    assert text.endswith("\n")
    # sort_keys → created_at appears before goal appears before schema_version
    assert text.index("created_at") < text.index("schema_version")


# --- rendering ------------------------------------------------------------

def test_render_has_all_sections():
    state = TaskState.model_validate(VALID)
    md = render_markdown(state)
    for section in [
        "# Handoff:",
        "## Goal",
        "## Plan",
        "## Decisions",
        "## Files touched",
        "## Next steps",
        "## Blockers",
        "## Commands to verify",
        "## Instructions for the next agent",
    ]:
        assert section in md
    assert "[x]" in md and "[ ]" in md  # done + todo marks


# --- filesystem -----------------------------------------------------------

def test_write_and_load_roundtrip(tmp_path: Path):
    state = TaskState.model_validate(VALID)
    state_path, handoff_path = write_artifacts(state, tmp_path)
    assert state_path.exists() and handoff_path.exists()
    reloaded = load_state(tmp_path)
    assert reloaded is not None
    assert reloaded.task_title == state.task_title


def test_load_state_none_when_missing(tmp_path: Path):
    assert load_state(tmp_path) is None


# --- resume ---------------------------------------------------------------

def test_render_template_substitutes():
    assert render_template(["{binary}", "{prompt}"], binary="x", prompt="P", cwd="C") == ["x", "P"]
    assert render_template(["{binary}", "-i", "{prompt}"], binary="g", prompt="P", cwd="C") == ["g", "-i", "P"]
    assert render_template(["{binary}", "{cwd}"], binary="x", prompt="P", cwd="C") == ["x", "C"]


def test_resolve_executable_prefers_cmd_over_shim(monkeypatch):
    # Regression (WinError 193): npm global installs ship an extensionless bash
    # shim next to a real .cmd; the resolver must pick the .cmd, not the shim.
    # This is Windows-only behavior — simulate it by mocking shutil.which so the
    # logic runs deterministically on any OS (calling the real which under a
    # faked sys.platform="win32" hits Windows-only code / case-sensitivity).
    monkeypatch.setattr("tapout.detect.sys.platform", "win32")
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")

    def fake_which(name):
        # both an extensionless shim and a real .cmd are on PATH
        return {"gemini": "/x/npm/gemini", "gemini.CMD": "/x/npm/gemini.CMD"}.get(name)

    monkeypatch.setattr("tapout.detect.shutil.which", fake_which)

    resolved = resolve_executable("gemini")
    assert resolved is not None
    assert resolved.lower().endswith(".cmd")  # picked the .cmd, not the bare shim


def test_resolve_executable_keeps_explicit_extension(monkeypatch):
    # A name that already has an extension is passed straight through.
    monkeypatch.setattr("tapout.detect.sys.platform", "win32")
    monkeypatch.setattr("tapout.detect.shutil.which", lambda n: f"/x/{n}")
    assert resolve_executable("thing.exe") == "/x/thing.exe"


def test_opening_prompt_wraps_handoff():
    op = build_opening_prompt("HANDOFF BODY")
    assert "HANDOFF BODY" in op
    assert "Instructions for the next agent" in op


def test_prepare_resume_no_state(tmp_path: Path):
    with pytest.raises(ResumeError, match="No handoff found"):
        prepare_resume(tmp_path, "codex")


def test_prepare_resume_unknown_agent(tmp_path: Path):
    with pytest.raises(ResumeError, match="Unknown agent"):
        prepare_resume(tmp_path, "notanagent")


def test_prepare_resume_not_installed(tmp_path: Path, monkeypatch):
    write_artifacts(TaskState.model_validate(VALID), tmp_path)
    monkeypatch.setattr("tapout.resume.resolve_binary", lambda _: None)
    with pytest.raises(ResumeError, match="not installed"):
        prepare_resume(tmp_path, "codex")


def test_prepare_resume_ok(tmp_path: Path, monkeypatch):
    write_artifacts(TaskState.model_validate(VALID), tmp_path)
    monkeypatch.setattr("tapout.resume.resolve_binary", lambda _: "/fake/codex")
    monkeypatch.setattr("tapout.resume.copy_to_clipboard", lambda _: True)
    plan = prepare_resume(tmp_path, "codex")
    assert plan.argv[0] == "/fake/codex"
    assert plan.effective_style == "cli_prompt"
    assert plan.state.task_title == "Test task"


# --- registry / user overlay ---------------------------------------------

def test_bundled_registry_has_known_agents():
    reg = load_registry()
    for key in ("claude", "codex", "gemini", "cursor"):
        assert key in reg
    assert reg["gemini"].notes  # deprecation note present


def test_user_overlay_adds_agent(tmp_path: Path, monkeypatch):
    overlay = tmp_path / "agents.toml"
    overlay.write_text(
        '[mytool]\n'
        'display_name = "My Tool"\n'
        'binaries = ["mytool"]\n'
        'resume_style = "cli_prompt"\n'
        'launch_template = ["{binary}", "{prompt}"]\n',
        encoding="utf-8",
    )
    monkeypatch.setattr("tapout.registry.USER_OVERLAY", overlay)
    reg = load_registry()
    assert "mytool" in reg
    assert reg["mytool"].display_name == "My Tool"
    # bundled agents still present — overlay merges, doesn't replace
    assert "claude" in reg


def test_user_overlay_field_wins(tmp_path: Path, monkeypatch):
    overlay = tmp_path / "agents.toml"
    overlay.write_text('[gemini]\ndisplay_name = "Gemini (mine)"\n', encoding="utf-8")
    monkeypatch.setattr("tapout.registry.USER_OVERLAY", overlay)
    reg = load_registry()
    assert reg["gemini"].display_name == "Gemini (mine)"
    assert reg["gemini"].binaries == ["gemini"]  # non-overridden field survives


def test_prepare_resume_user_added_agent(tmp_path: Path, monkeypatch):
    overlay = tmp_path / "agents.toml"
    overlay.write_text(
        '[mytool]\n'
        'display_name = "My Tool"\n'
        'binaries = ["mytool"]\n'
        'resume_style = "cli_prompt"\n'
        'launch_template = ["{binary}", "{prompt}"]\n',
        encoding="utf-8",
    )
    monkeypatch.setattr("tapout.registry.USER_OVERLAY", overlay)
    write_artifacts(TaskState.model_validate(VALID), tmp_path)
    monkeypatch.setattr("tapout.resume.resolve_binary", lambda _: "/fake/mytool")
    plan = prepare_resume(tmp_path, "mytool")
    assert plan.effective_style == "cli_prompt"
    assert plan.argv == ["/fake/mytool", plan.opening]


# --- BatBadBut guard ------------------------------------------------------

def test_batbadbut_risk_detection():
    assert is_batch_wrapper("C:/x/codex.cmd")
    assert is_batch_wrapper("C:/x/codex.BAT")
    assert not is_batch_wrapper("C:/x/codex.exe")
    assert batbadbut_risk("x.cmd", "hello & del")     # metachar on a .cmd
    assert not batbadbut_risk("x.cmd", "hello world")  # clean prompt on a .cmd
    assert not batbadbut_risk("x.exe", "hello & del")  # metachar but not a wrapper


def _argv_overlay(tmp_path: Path, monkeypatch):
    """An overlay agent that uses argv delivery (so the BatBadBut guard applies)."""
    overlay = tmp_path / "agents.toml"
    overlay.write_text(
        '[argvtool]\n'
        'display_name = "Argv Tool"\n'
        'binaries = ["argvtool"]\n'
        'resume_style = "cli_prompt"\n'
        'launch_template = ["{binary}", "{prompt}"]\n'
        'prompt_delivery = "argv"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr("tapout.registry.USER_OVERLAY", overlay)


def test_batbadbut_guard_downgrades_on_cmd(tmp_path: Path, monkeypatch):
    _argv_overlay(tmp_path, monkeypatch)
    hostile = json.loads(json.dumps(VALID))
    hostile["goal"] = 'danger & echo | set %X% say "hi"'
    write_artifacts(TaskState.model_validate(hostile), tmp_path)
    monkeypatch.setattr("tapout.resume.resolve_binary", lambda _: "C:/x/argvtool.cmd")
    plan = prepare_resume(tmp_path, "argvtool")
    assert plan.effective_style == "clipboard_gui"
    assert plan.guard_reason is not None
    assert plan.argv == ["C:/x/argvtool.cmd"]  # bare launch, prompt NOT in argv


def test_batbadbut_clean_prompt_launches_on_cmd(tmp_path: Path, monkeypatch):
    _argv_overlay(tmp_path, monkeypatch)
    write_artifacts(TaskState.model_validate(VALID), tmp_path)  # clean fields
    monkeypatch.setattr("tapout.resume.resolve_binary", lambda _: "C:/x/argvtool.cmd")
    plan = prepare_resume(tmp_path, "argvtool")
    assert plan.effective_style == "cli_prompt"
    assert plan.guard_reason is None
    assert plan.argv == ["C:/x/argvtool.cmd", plan.opening]


# --- prompt delivery: stdin / file (fixes the 1.5 .cmd downgrade) ---------

def test_stdin_delivery_bypasses_guard(tmp_path: Path, monkeypatch):
    # codex now uses stdin delivery — hostile prompt, .cmd launcher, NO guard.
    hostile = json.loads(json.dumps(VALID))
    hostile["goal"] = 'danger & echo | set %X% say "hi" > out'
    write_artifacts(TaskState.model_validate(hostile), tmp_path)
    monkeypatch.setattr("tapout.resume.resolve_binary", lambda _: "C:/x/codex.cmd")
    plan = prepare_resume(tmp_path, "codex")
    assert plan.delivery == "stdin"
    assert plan.guard_reason is None
    assert plan.argv == ["C:/x/codex.cmd", "exec", "-"]
    assert plan.stdin_text == plan.opening  # full prompt goes via stdin


def _write_echo_binary(bindir: Path, name: str = "echotool") -> None:
    """Create a fake agent binary that copies stdin to <bindir>/out.txt.

    Cross-platform: a .cmd shim on Windows (resolved via PATHEXT), an
    executable POSIX shell script elsewhere (resolved as a plain, extensionless
    binary — which requires the +x bit that shutil.which checks).
    """
    bindir.mkdir(exist_ok=True)
    if sys.platform == "win32":
        (bindir / f"{name}.cmd").write_text(
            "@echo off\n"
            r'python -c "import sys,io; io.open(r'"'"'%~dp0out.txt'"'"', '"'"'w'"'"', encoding='"'"'utf-8'"'"').write(sys.stdin.read())"'
            "\n",
            encoding="utf-8",
        )
    else:
        script = bindir / name
        script.write_text('#!/bin/sh\ncat > "$(dirname "$0")/out.txt"\n', encoding="utf-8")
        script.chmod(0o755)


def test_stdin_delivery_roundtrips_hostile_chars(tmp_path: Path, monkeypatch):
    # Real launch: a fake binary reads stdin and writes it to a file. Hostile
    # metacharacters must arrive byte-intact (stdin never touches shell argv).
    bindir = tmp_path / "bin"
    _write_echo_binary(bindir)
    overlay = tmp_path / "agents.toml"
    overlay.write_text(
        '[echotool]\n'
        'display_name = "Echo Tool"\n'
        'binaries = ["echotool"]\n'
        'resume_style = "cli_prompt"\n'
        'launch_template = ["{binary}"]\n'
        'prompt_delivery = "stdin"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr("tapout.registry.USER_OVERLAY", overlay)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))

    hostile = json.loads(json.dumps(VALID))
    hostile["goal"] = 'inject & whoami | find %X% "quoted" > redir < in ^caret'
    write_artifacts(TaskState.model_validate(hostile), tmp_path)

    plan = prepare_resume(tmp_path, "echotool")
    assert plan.delivery == "stdin"
    proc = launch(plan, tmp_path)
    proc.wait(timeout=30)
    received = (bindir / "out.txt").read_text(encoding="utf-8")
    assert received == plan.opening
    # the hostile chars survived intact
    for ch in "&|%\"><^":
        assert ch in received


def test_resolve_executable_finds_extensionless_binary(tmp_path: Path, monkeypatch):
    # Regression: a registry binary name with no extension must resolve on the
    # current OS. On Linux that means a plain +x file (shutil.which checks the
    # exec bit) — a .cmd-only fixture would fail here, which is what broke CI.
    bindir = tmp_path / "bin"
    _write_echo_binary(bindir, name="toolx")
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))
    assert resolve_executable("toolx") is not None


def test_file_delivery_writes_prompt_and_flag(tmp_path: Path, monkeypatch):
    overlay = tmp_path / "agents.toml"
    overlay.write_text(
        '[filetool]\n'
        'display_name = "File Tool"\n'
        'binaries = ["filetool"]\n'
        'resume_style = "cli_prompt"\n'
        'launch_template = ["{binary}"]\n'
        'prompt_delivery = "file:--prompt-file"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr("tapout.registry.USER_OVERLAY", overlay)
    write_artifacts(TaskState.model_validate(VALID), tmp_path)
    monkeypatch.setattr("tapout.resume.resolve_binary", lambda _: "/fake/filetool")
    plan = prepare_resume(tmp_path, "filetool")
    assert plan.delivery == "file"
    assert plan.prompt_file is not None and plan.prompt_file.exists()
    assert plan.prompt_file.read_text(encoding="utf-8") == plan.opening
    assert plan.argv == ["/fake/filetool", "--prompt-file", str(plan.prompt_file)]


def test_record_history(tmp_path: Path):
    record_history(tmp_path, "claude", "codex", "Test task")
    record_history(tmp_path, "claude", "gemini", "Test task")
    lines = (tmp_path / ".tapout" / "history.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["from_agent"] == "claude" and first["to_agent"] == "codex"


# --- CLI ------------------------------------------------------------------

def test_cli_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_cli_scan_runs():
    result = runner.invoke(app, ["scan"])
    assert result.exit_code == 0
    assert "Claude Code" in result.stdout


def test_cli_out_prints_prompt():
    result = runner.invoke(app, ["out"])
    assert result.exit_code == 0
    assert "schema_version" in result.stdout
    assert "ONE fenced code block" in SUMMARIZATION_PROMPT


def test_cli_out_from_demo(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["out", "--from", str(DEMO)])
    assert result.exit_code == 0, result.stdout
    assert (tmp_path / ".tapout" / "task-state.json").exists()
    assert (tmp_path / "HANDOFF.md").exists()


def test_cli_out_from_malformed(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    bad = tmp_path / "bad.md"
    bad_state = dict(VALID)
    del bad_state["goal"]
    bad.write_text(_block(bad_state), encoding="utf-8")
    result = runner.invoke(app, ["out", "--from", str(bad)])
    assert result.exit_code == 1
    assert "goal" in result.output


def test_cli_out_from_missing_file(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["out", "--from", str(tmp_path / "nope.md")])
    assert result.exit_code == 1


def test_cli_resume_no_state(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["codex"])
    assert result.exit_code == 1
    assert "No handoff found" in result.output


def test_cli_resume_dry_run(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_artifacts(TaskState.model_validate(VALID), tmp_path)
    monkeypatch.setattr("tapout.resume.resolve_binary", lambda _: "/fake/codex")
    result = runner.invoke(app, ["codex", "--dry-run"])
    assert result.exit_code == 0
    assert "not launching" in result.output
    # history recorded even on dry run
    assert (tmp_path / ".tapout" / "history.jsonl").exists()


def test_cli_resume_generic_subcommand(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_artifacts(TaskState.model_validate(VALID), tmp_path)
    monkeypatch.setattr("tapout.resume.resolve_binary", lambda _: "/fake/codex")
    result = runner.invoke(app, ["resume", "codex", "--dry-run"])
    assert result.exit_code == 0
    assert "not launching" in result.output


def test_cli_out_requires_force_when_state_exists(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    first = runner.invoke(app, ["out", "--from", str(DEMO)])
    assert first.exit_code == 0, first.output
    # second run refuses without --force
    second = runner.invoke(app, ["out", "--from", str(DEMO)])
    assert second.exit_code == 1
    assert "already exists" in second.output
    # --force overwrites
    forced = runner.invoke(app, ["out", "--from", str(DEMO), "--force"])
    assert forced.exit_code == 0, forced.output
    hist = (tmp_path / ".tapout" / "history.jsonl").read_text(encoding="utf-8")
    assert '"event": "capture"' in hist


# --- capture (machine-invoked) --------------------------------------------

def test_capture_with_override_writes_artifacts(tmp_path: Path, monkeypatch):
    reply = tmp_path / "reply.md"
    reply.write_text(_block(VALID), encoding="utf-8")
    monkeypatch.setenv("TAPOUT_CAPTURE_FROM", str(reply))
    state = run_capture(tmp_path, "claude", None, force=False)
    assert state is not None
    assert state.source_agent == "claude"
    assert (tmp_path / ".tapout" / "task-state.json").exists()
    assert (tmp_path / "HANDOFF.md").exists()
    assert (tmp_path / ".tapout" / "capture.log").exists()


def test_capture_skips_when_exists_without_force(tmp_path: Path, monkeypatch):
    reply = tmp_path / "reply.md"
    reply.write_text(_block(VALID), encoding="utf-8")
    monkeypatch.setenv("TAPOUT_CAPTURE_FROM", str(reply))
    run_capture(tmp_path, "claude", None, force=True)
    skipped = run_capture(tmp_path, "claude", None, force=False)
    assert skipped is None
    log = (tmp_path / ".tapout" / "capture.log").read_text(encoding="utf-8")
    assert "SKIP" in log


def test_capture_force_overwrites(tmp_path: Path, monkeypatch):
    reply = tmp_path / "reply.md"
    reply.write_text(_block(VALID), encoding="utf-8")
    monkeypatch.setenv("TAPOUT_CAPTURE_FROM", str(reply))
    run_capture(tmp_path, "claude", None, force=True)
    again = run_capture(tmp_path, "claude", None, force=True)
    assert again is not None


def test_capture_no_transcript_no_override_errors(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("TAPOUT_CAPTURE_FROM", raising=False)
    with pytest.raises(CaptureError):
        run_capture(tmp_path, "claude", None, force=True)


def test_find_project_transcript_picks_newest_toplevel(tmp_path: Path, monkeypatch):
    projects = tmp_path / "projects"
    repo = tmp_path / "work" / "myrepo"
    repo.mkdir(parents=True)
    proj = projects / _mangle_cwd(repo.resolve())
    proj.mkdir(parents=True)
    old = proj / "old.jsonl"; old.write_text("{}\n", encoding="utf-8")
    new = proj / "new.jsonl"; new.write_text("{}\n", encoding="utf-8")
    subs = proj / "subagents"; subs.mkdir()
    subf = subs / "agent-x.jsonl"; subf.write_text("{}\n", encoding="utf-8")
    os.utime(old, (1, 1000))
    os.utime(new, (1, 2000))
    os.utime(subf, (1, 9999))  # newest overall, but must be ignored (subagent)
    monkeypatch.setattr("tapout.capture.CLAUDE_PROJECTS", projects)
    assert find_project_transcript(repo) == new


def test_find_project_transcript_errors_when_no_match(tmp_path: Path, monkeypatch):
    projects = tmp_path / "projects"
    projects.mkdir()
    (projects / "C--some-other-repo").mkdir()
    monkeypatch.setattr("tapout.capture.CLAUDE_PROJECTS", projects)
    with pytest.raises(CaptureError, match="no Claude Code session directory"):
        find_project_transcript(tmp_path / "nope")


def test_capture_auto_picks_transcript(tmp_path: Path, monkeypatch):
    projects = tmp_path / "projects"
    repo = tmp_path / "repo"
    repo.mkdir()
    proj = projects / _mangle_cwd(repo.resolve())
    proj.mkdir(parents=True)
    (proj / "s.jsonl").write_text(
        json.dumps({"message": {"role": "user", "content": "do X"}}) + "\n", encoding="utf-8"
    )
    monkeypatch.setattr("tapout.capture.CLAUDE_PROJECTS", projects)
    monkeypatch.setattr("tapout.capture.summarize_via_claude", lambda text, **k: _block(VALID))
    monkeypatch.delenv("TAPOUT_CAPTURE_FROM", raising=False)
    state = run_capture(repo, "claude", None, force=True)
    assert state is not None
    assert (repo / "HANDOFF.md").exists()
    assert (repo / ".tapout" / "task-state.json").exists()


def test_condense_transcript(tmp_path: Path):
    t = tmp_path / "t.jsonl"
    t.write_text(
        json.dumps({"type": "user", "message": {"role": "user", "content": "hello world"}}) + "\n"
        + json.dumps({"message": {"role": "assistant", "content": [{"type": "text", "text": "hi back"}]}}) + "\n",
        encoding="utf-8",
    )
    text = condense_transcript(t)
    assert "hello world" in text
    assert "hi back" in text


# --- monitor (statusline + watch) -----------------------------------------

def test_parse_status_extracts_rate_limits():
    data = {
        "rate_limits": {
            "five_hour": {"used_percentage": 84.0, "resets_at": 1738425600},
            "seven_day": {"used_percentage": 41.2, "resets_at": 1738857600},
        },
        "context_window": {"used_percentage": 8},
        "transcript_path": "/x/t.jsonl",
    }
    s = parse_status(data)
    assert s["pct_5h"] == 84.0
    assert s["pct_7d"] == 41.2
    assert s["context_pct"] == 8
    assert s["transcript_path"] == "/x/t.jsonl"


def test_indicator_levels():
    assert indicator({"pct_5h": 84.0}) == "tap: 84%"
    assert "LIMIT" in indicator({"pct_5h": 100.0})
    assert indicator({"pct_5h": 92.0}).endswith("!")
    assert indicator({"pct_5h": None, "context_pct": 30}) == "tap: ctx 30%"
    assert indicator({}) == "tap: --"


def test_is_limit_hit():
    assert is_limit_hit({"pct_5h": 100.0})
    assert not is_limit_hit({"pct_5h": 50.0})
    assert not is_limit_hit({"pct_5h": None})


def test_run_statusline_writes_state_and_prints(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("tapout.monitor.STATE_DIR", tmp_path)
    monkeypatch.setattr("tapout.monitor.STATE_FILE", tmp_path / "s.json")
    data = json.dumps({"rate_limits": {"five_hour": {"used_percentage": 84.0, "resets_at": 1}}})
    line = run_statusline(data)
    assert line == "tap: 84%"
    st = read_state()
    assert st["pct_5h"] == 84.0


def test_run_watch_degrades_without_claude(monkeypatch):
    monkeypatch.setattr("tapout.monitor.resolve_executable", lambda _: None)
    out: list[str] = []
    run_watch(once=True, emit=out.append)
    assert any("no monitorable agents" in line for line in out)


def test_run_watch_announces_limit(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("tapout.monitor.resolve_executable", lambda _: "/fake/claude")
    monkeypatch.setattr("tapout.monitor.STATE_FILE", tmp_path / "s.json")
    (tmp_path / "s.json").write_text(json.dumps({"pct_5h": 100.0, "resets_5h": None}), encoding="utf-8")
    out: list[str] = []
    run_watch(once=True, emit=out.append)
    assert any("LIMIT HIT" in line for line in out)
    assert any("tap codex" in line for line in out)


def test_watch_line_formats():
    line = watch_line({"pct_5h": 84.0, "resets_5h": None, "context_pct": 8})
    assert "5h window" in line and "ctx 8%" in line


# --- CLI: capture / statusline / watch ------------------------------------

def test_cli_capture_hook_stdin(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    reply = tmp_path / "r.md"
    reply.write_text(_block(VALID), encoding="utf-8")
    monkeypatch.setenv("TAPOUT_CAPTURE_FROM", str(reply))
    hook_json = json.dumps({"transcript_path": "/x/t.jsonl", "cwd": str(tmp_path)})
    result = runner.invoke(app, ["capture", "--agent", "claude", "--hook-stdin", "--force"], input=hook_json)
    assert result.exit_code == 0, result.output
    assert (tmp_path / "HANDOFF.md").exists()
    assert (tmp_path / ".tapout" / "task-state.json").exists()


def test_cli_statusline(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("tapout.monitor.STATE_DIR", tmp_path)
    monkeypatch.setattr("tapout.monitor.STATE_FILE", tmp_path / "s.json")
    data = json.dumps({"rate_limits": {"five_hour": {"used_percentage": 42.0}}})
    result = runner.invoke(app, ["statusline"], input=data)
    assert result.exit_code == 0
    assert "tap: 42%" in result.output


def test_cli_watch_once(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("tapout.monitor.resolve_executable", lambda _: "/fake/claude")
    monkeypatch.setattr("tapout.monitor.STATE_FILE", tmp_path / "s.json")
    (tmp_path / "s.json").write_text(json.dumps({"pct_5h": 10.0, "resets_5h": None}), encoding="utf-8")
    result = runner.invoke(app, ["watch", "--once"])
    assert result.exit_code == 0
    assert "5h window" in result.output
