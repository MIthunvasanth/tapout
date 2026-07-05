"""Tests for tapout Slice 1."""

from __future__ import annotations

import json
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
from tapout.detect import resolve_executable
from tapout.registry import load_registry
from tapout.resume import (
    ResumeError,
    batbadbut_risk,
    build_opening_prompt,
    is_batch_wrapper,
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


def test_resolve_executable_prefers_cmd_over_shim(tmp_path: Path, monkeypatch):
    # Regression (WinError 193): npm global installs ship an extensionless bash
    # shim next to a real .cmd; the resolver must pick the .cmd, not the shim.
    shim = tmp_path / "gemini"          # extensionless bash shim
    shim.write_text("#!/bin/sh\n", encoding="utf-8")
    cmd = tmp_path / "gemini.cmd"       # real Windows launcher
    cmd.write_text("@echo off\n", encoding="utf-8")
    for p in (shim, cmd):
        p.chmod(0o755)

    monkeypatch.setattr("tapout.detect.sys.platform", "win32")
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    monkeypatch.setenv("PATH", str(tmp_path))

    resolved = resolve_executable("gemini")
    assert resolved is not None
    assert resolved.lower().endswith(".cmd")


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


def test_batbadbut_guard_downgrades_on_cmd(tmp_path: Path, monkeypatch):
    hostile = json.loads(json.dumps(VALID))
    hostile["goal"] = 'danger & echo | set %X% say "hi"'
    write_artifacts(TaskState.model_validate(hostile), tmp_path)
    monkeypatch.setattr("tapout.resume.resolve_binary", lambda _: "C:/x/codex.cmd")
    plan = prepare_resume(tmp_path, "codex")
    assert plan.effective_style == "clipboard_gui"
    assert plan.guard_reason is not None
    assert plan.argv == ["C:/x/codex.cmd"]  # bare launch, prompt NOT in argv


def test_batbadbut_clean_prompt_launches_on_cmd(tmp_path: Path, monkeypatch):
    write_artifacts(TaskState.model_validate(VALID), tmp_path)  # clean fields
    monkeypatch.setattr("tapout.resume.resolve_binary", lambda _: "C:/x/codex.cmd")
    plan = prepare_resume(tmp_path, "codex")
    assert plan.effective_style == "cli_prompt"
    assert plan.guard_reason is None
    assert plan.argv == ["C:/x/codex.cmd", plan.opening]


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
