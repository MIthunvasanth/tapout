"""Tests for tapout.tray (Slice 3 — the Windows tray buddy).

Pure logic (state machine, icon color, tooltip, menu/toast eligibility) is
tested directly with no GUI. Toast/pystray calls are mocked at the module's
own low-level seams (_send_toast, subprocess.run for autostart) so these
tests never touch a real notification, tray icon, or Windows Startup folder.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tapout.cli import app
from tapout.registry import AgentEntry
from tapout.detect import DetectionResult
import tapout.tray as tray

runner = CliRunner()


def _entry(key, display_name, resume_style="cli_prompt", notes="") -> AgentEntry:
    return AgentEntry(
        key=key,
        display_name=display_name,
        binaries=[key],
        resume_style=resume_style,
        launch_template=["{binary}", "{prompt}"],
        notes=notes,
    )


def _detection(entry: AgentEntry, installed: bool) -> DetectionResult:
    return DetectionResult(entry=entry, installed=installed, which=None, config_found=None, version=None)


# --------------------------------------------------------------------------
# THE CORRECTNESS GATE — edge-trigger state machine
# --------------------------------------------------------------------------

def _feed(seq, seed=None):
    state = dict(seed) if seed is not None else dict(tray.DEFAULT_TRAY_STATE)
    fired = []
    for pct in seq:
        events, state = tray.compute_toast_events(state, pct, None)
        fired.extend(events)
    return fired, state


def test_edge_trigger_single_85_crossing():
    fired, _ = _feed([10, 20, 60, 84, 85, 87])
    assert fired == ["warning_85"]


def test_edge_trigger_idempotent_within_window():
    fired, _ = _feed([85, 86, 87, 84, 85, 87])
    assert fired == ["warning_85"]  # exactly one, no re-fire on the dip-then-rise


def test_edge_trigger_both_85_and_100():
    fired, _ = _feed([85, 90, 95, 100])
    assert fired == ["warning_85", "limit_100"]


def test_edge_trigger_reset_rearms_next_crossing():
    fired1, state = _feed([95, 98, 99, 3, 5, 10])
    assert fired1 == ["warning_85"]
    assert state["warning_85_fired"] is False  # reset cleared it
    fired2, _ = _feed([85], seed=state)
    assert fired2 == ["warning_85"]  # fires again after reset


def test_edge_trigger_cold_start_fires():
    fired, _ = _feed([85])  # fresh state file (no previous data)
    assert fired == ["warning_85"]


def test_edge_trigger_persisted_flag_survives_restart():
    # Simulates a daemon restart: file says the flag already fired, even
    # though the raw "previous" value alone would suggest a fresh transition.
    # The flag — not just the number — must be what prevents the re-fire.
    seed = {
        "previous_pct_5h": 80.0,
        "warning_85_fired": True,
        "limit_100_fired": False,
        "previous_pct_7d": 0.0,
        "warning_week_90_fired": False,
    }
    fired, _ = _feed([85], seed=seed)
    assert fired == []


def test_edge_trigger_weekly_independent_of_5h():
    state = dict(tray.DEFAULT_TRAY_STATE)
    events, state = tray.compute_toast_events(state, 10.0, 91.0)
    assert events == ["warning_week_90"]
    events, state = tray.compute_toast_events(state, 10.0, 92.0)
    assert events == []  # idempotent


def test_edge_trigger_weekly_reset():
    state = dict(tray.DEFAULT_TRAY_STATE)
    events, state = tray.compute_toast_events(state, 0.0, 95.0)
    assert events == ["warning_week_90"]
    events, state = tray.compute_toast_events(state, 0.0, 2.0)  # big drop -> reset
    assert events == []
    assert state["warning_week_90_fired"] is False
    events, state = tray.compute_toast_events(state, 0.0, 91.0)
    assert events == ["warning_week_90"]


# --------------------------------------------------------------------------
# tray-state.json persistence
# --------------------------------------------------------------------------

def test_load_tray_state_missing_file_returns_default(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(tray, "TRAY_STATE_PATH", tmp_path / "missing.json")
    assert tray.load_tray_state() == tray.DEFAULT_TRAY_STATE


def test_save_and_load_tray_state_roundtrip(tmp_path: Path, monkeypatch):
    path = tmp_path / "tray-state.json"
    monkeypatch.setattr(tray, "TRAY_STATE_PATH", path)
    monkeypatch.setattr(tray, "TAPOUT_DIR", tmp_path)
    state = {"previous_pct_5h": 42.0, "warning_85_fired": True, "limit_100_fired": False,
              "previous_pct_7d": 10.0, "warning_week_90_fired": False}
    tray.save_tray_state(state)
    assert tray.load_tray_state() == state


def test_load_tray_state_tolerates_utf8_bom(tmp_path: Path, monkeypatch):
    path = tmp_path / "tray-state.json"
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps({"warning_85_fired": True}).encode())
    monkeypatch.setattr(tray, "TRAY_STATE_PATH", path)
    state = tray.load_tray_state()
    assert state["warning_85_fired"] is True


def test_load_tray_state_malformed_defaults(tmp_path: Path, monkeypatch):
    path = tmp_path / "tray-state.json"
    path.write_text("not json", encoding="utf-8")
    monkeypatch.setattr(tray, "TRAY_STATE_PATH", path)
    assert tray.load_tray_state() == tray.DEFAULT_TRAY_STATE


# --------------------------------------------------------------------------
# icon color selection
# --------------------------------------------------------------------------

@pytest.mark.parametrize("pct,expected", [(0, "green"), (69, "green"), (70, "amber"),
                                            (89, "amber"), (90, "red"), (100, "red")])
def test_icon_color_boundaries(pct, expected):
    assert tray.icon_color_for_pct(pct) == expected


def test_icon_color_none_is_gray():
    assert tray.icon_color_for_pct(None) == "gray"


def test_icon_color_for_status_gray_when_missing():
    assert tray.icon_color_for_status(None) == "gray"


def test_icon_color_for_status_gray_when_stale():
    status = {"pct_5h": 50.0, "ts": 1000.0}
    assert tray.icon_color_for_status(status, now=1000.0 + 61) == "gray"


def test_icon_color_for_status_fresh():
    status = {"pct_5h": 95.0, "ts": 1000.0}
    assert tray.icon_color_for_status(status, now=1000.0 + 10) == "red"


# --------------------------------------------------------------------------
# tooltip
# --------------------------------------------------------------------------

def test_build_tooltip_no_data():
    msg = tray.build_tooltip(None)
    assert "no status data yet" in msg
    assert "tap statusline" in msg


def test_build_tooltip_stale():
    msg = tray.build_tooltip({"pct_5h": 50.0, "ts": 0.0}, now=1000.0)
    assert "no status data yet" in msg


def test_build_tooltip_fresh_with_agents():
    status = {"pct_5h": 42.0, "resets_5h": 1751731200, "ts": 1000.0}
    results = [
        _detection(_entry("codex", "Codex CLI"), installed=True),
        _detection(_entry("gemini", "Gemini CLI", notes="Deprecated Dec 2025"), installed=True),
    ]
    msg = tray.build_tooltip(status, results, now=1000.0 + 5)
    assert "Claude: 42% of 5h window" in msg
    assert "resets" in msg
    assert "Codex CLI: ready" in msg
    assert "Gemini CLI: deprecated" in msg


def test_build_tooltip_agent_not_installed():
    status = {"pct_5h": 10.0, "ts": 1000.0}
    results = [_detection(_entry("codex", "Codex CLI"), installed=False)]
    msg = tray.build_tooltip(status, results, now=1000.0)
    assert "Codex CLI: not installed" in msg


# --------------------------------------------------------------------------
# menu / toast eligibility (registry-driven)
# --------------------------------------------------------------------------

def test_menu_resume_agents_excludes_deprecated_but_keeps_clipboard_only():
    results = [
        _detection(_entry("codex", "Codex CLI"), installed=True),
        _detection(_entry("gemini", "Gemini CLI", notes="Deprecated Dec 2025"), installed=True),
        _detection(_entry("cursor", "Cursor", resume_style="clipboard_only"), installed=True),
        _detection(_entry("aider", "Aider"), installed=False),
    ]
    pairs = tray.menu_resume_agents(results)
    keys = [k for k, _ in pairs]
    assert "codex" in keys
    assert "cursor" in keys       # clipboard_only still menu-clickable
    assert "gemini" not in keys   # deprecated excluded
    assert "aider" not in keys    # not installed


def test_limit_toast_agents_excludes_clipboard_only_and_deprecated():
    results = [
        _detection(_entry("codex", "Codex CLI"), installed=True),
        _detection(_entry("gemini", "Gemini CLI", notes="Deprecated Dec 2025"), installed=True),
        _detection(_entry("cursor", "Cursor", resume_style="clipboard_only"), installed=True),
    ]
    pairs = tray.limit_toast_agents(results)
    keys = [k for k, _ in pairs]
    assert keys == ["codex"]  # gemini deprecated, cursor clipboard_only


# --------------------------------------------------------------------------
# toasts (mocked at _send_toast — never touches windows_toasts for real)
# --------------------------------------------------------------------------

def test_fire_warning_toast_content(monkeypatch):
    captured = {}
    monkeypatch.setattr(tray, "_send_toast", lambda title, body, buttons, on_click: captured.update(
        title=title, body=body, buttons=buttons, on_click=on_click))
    tray.fire_warning_toast()
    assert captured["title"] == "Claude window almost done"
    assert "85%" in captured["body"]
    assert captured["buttons"] == [("Prep handoff", "prep_handoff")]


def test_fire_warning_toast_button_calls_prep_handoff(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(tray, "prep_handoff_now", lambda repo=None: calls.append(repo))
    monkeypatch.setattr(tray, "_send_toast", lambda title, body, buttons, on_click: on_click("prep_handoff"))
    tray.fire_warning_toast(tmp_path)
    assert calls == [tmp_path]


def test_fire_limit_toast_dynamic_buttons_from_registry(monkeypatch):
    agents = [("codex", "Codex CLI"), ("gemini", "Gemini CLI")]
    monkeypatch.setattr(tray, "limit_toast_agents", lambda: agents)
    captured = {}
    monkeypatch.setattr(tray, "_send_toast", lambda title, body, buttons, on_click: captured.update(
        title=title, buttons=buttons))
    tray.fire_limit_toast()
    assert captured["title"] == "Claude tapped out"
    assert captured["buttons"] == [("→ Codex CLI", "codex"), ("→ Gemini CLI", "gemini")]


def test_fire_limit_toast_button_calls_resume_in(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(tray, "resume_in", lambda repo, key: calls.append((repo, key)))
    monkeypatch.setattr(tray, "limit_toast_agents", lambda: [("codex", "Codex CLI")])
    monkeypatch.setattr(tray, "_send_toast", lambda title, body, buttons, on_click: on_click("codex"))
    tray.fire_limit_toast(tmp_path)
    assert calls == [(tmp_path, "codex")]


def test_fire_weekly_toast_content(monkeypatch):
    captured = {}
    monkeypatch.setattr(tray, "_send_toast", lambda title, body, buttons, on_click: captured.update(title=title))
    tray.fire_weekly_toast()
    assert "weekly" in captured["title"].lower()


def test_send_toast_non_windows_is_silent_noop(monkeypatch):
    monkeypatch.setattr(tray.sys, "platform", "linux")
    tray._send_toast("t", "b", [], None)  # must not raise


def test_dispatch_toast_event_routes_correctly(monkeypatch):
    called = []
    monkeypatch.setattr(tray, "fire_warning_toast", lambda: called.append("warn"))
    monkeypatch.setattr(tray, "fire_limit_toast", lambda: called.append("limit"))
    monkeypatch.setattr(tray, "fire_weekly_toast", lambda: called.append("week"))
    tray.dispatch_toast_event("warning_85")
    tray.dispatch_toast_event("limit_100")
    tray.dispatch_toast_event("warning_week_90")
    assert called == ["warn", "limit", "week"]


# --------------------------------------------------------------------------
# single-instance lock
# --------------------------------------------------------------------------

def test_acquire_lock_when_no_lockfile(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(tray, "TAPOUT_DIR", tmp_path)
    monkeypatch.setattr(tray, "TRAY_LOCK_PATH", tmp_path / "tray.lock")
    assert tray.acquire_lock() is True
    assert (tmp_path / "tray.lock").read_text(encoding="utf-8") == str(os.getpid())


def test_acquire_lock_blocked_by_running_pid(tmp_path: Path, monkeypatch):
    lock = tmp_path / "tray.lock"
    lock.write_text("99999", encoding="utf-8")
    monkeypatch.setattr(tray, "TAPOUT_DIR", tmp_path)
    monkeypatch.setattr(tray, "TRAY_LOCK_PATH", lock)
    monkeypatch.setattr(tray, "_pid_alive", lambda pid: pid == 99999)
    assert tray.acquire_lock() is False


def test_acquire_lock_stale_pid_is_reclaimed(tmp_path: Path, monkeypatch):
    lock = tmp_path / "tray.lock"
    lock.write_text("99999", encoding="utf-8")
    monkeypatch.setattr(tray, "TAPOUT_DIR", tmp_path)
    monkeypatch.setattr(tray, "TRAY_LOCK_PATH", lock)
    monkeypatch.setattr(tray, "_pid_alive", lambda pid: False)  # dead pid
    assert tray.acquire_lock() is True
    assert lock.read_text(encoding="utf-8") == str(os.getpid())


def test_release_lock_only_removes_own_lock(tmp_path: Path, monkeypatch):
    lock = tmp_path / "tray.lock"
    lock.write_text(str(os.getpid()), encoding="utf-8")
    monkeypatch.setattr(tray, "TRAY_LOCK_PATH", lock)
    tray.release_lock()
    assert not lock.exists()


def test_release_lock_leaves_other_pids_lock(tmp_path: Path, monkeypatch):
    lock = tmp_path / "tray.lock"
    lock.write_text("12345", encoding="utf-8")
    monkeypatch.setattr(tray, "TRAY_LOCK_PATH", lock)
    tray.release_lock()
    assert lock.exists()


def test_run_buddy_second_instance_shows_toast_and_exits(monkeypatch):
    monkeypatch.setattr(tray, "tray_deps_available", lambda: (True, ""))
    monkeypatch.setattr(tray, "acquire_lock", lambda: False)
    fired = []
    monkeypatch.setattr(tray, "fire_already_running_toast", lambda: fired.append(True))
    monkeypatch.setattr(tray, "_run_pystray_loop", lambda: (_ for _ in ()).throw(AssertionError("must not run loop")))
    tray.run_buddy()  # must not raise, must not start the loop
    assert fired == [True]


# --------------------------------------------------------------------------
# missing deps — clean error, no traceback
# --------------------------------------------------------------------------

def test_run_buddy_missing_deps_prints_clean_error(monkeypatch, capsys):
    monkeypatch.setattr(tray, "tray_deps_available", lambda: (False, "No module named 'pystray'"))
    tray.run_buddy()
    captured = capsys.readouterr()
    assert "pip install tapout[tray]" in captured.err


def test_cli_buddy_missing_deps_no_traceback(monkeypatch):
    monkeypatch.setattr("tapout.tray.tray_deps_available", lambda: (False, "boom"))
    result = runner.invoke(app, ["buddy"])
    assert result.exit_code == 0
    assert "Traceback" not in result.output
    assert "pip install tapout[tray]" in result.output


# --------------------------------------------------------------------------
# autostart (subprocess.run mocked — never touches the real Startup folder)
# --------------------------------------------------------------------------

def test_install_autostart_calls_powershell_with_right_target(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(tray, "STARTUP_DIR", tmp_path)
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        class R:
            returncode = 0
        return R()

    monkeypatch.setattr(tray.subprocess, "run", fake_run)
    path = tray.install_autostart()
    assert path == tmp_path / "tapout-buddy.lnk"
    joined = " ".join(captured["argv"])
    assert "powershell" in captured["argv"][0].lower()
    assert "buddy --detached" in joined
    assert str(path) in joined


def test_install_autostart_idempotent_same_target(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(tray, "STARTUP_DIR", tmp_path)
    calls = []
    monkeypatch.setattr(tray.subprocess, "run", lambda argv, **k: calls.append(argv))
    tray.install_autostart()
    tray.install_autostart()
    assert len(calls) == 2  # ran twice, but...
    assert tray.autostart_shortcut_path() == tmp_path / "tapout-buddy.lnk"  # ...always the same one file


def test_uninstall_autostart_removes_existing(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(tray, "STARTUP_DIR", tmp_path)
    shortcut = tmp_path / "tapout-buddy.lnk"
    shortcut.write_text("stub", encoding="utf-8")
    assert tray.uninstall_autostart() is True
    assert not shortcut.exists()


def test_uninstall_autostart_noop_when_absent(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(tray, "STARTUP_DIR", tmp_path)
    assert tray.uninstall_autostart() is False


@pytest.mark.skipif(sys.platform != "win32", reason="autostart is Windows-only by design")
def test_cli_install_uninstall_autostart(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("tapout.tray.STARTUP_DIR", tmp_path)

    def fake_run(argv, **k):
        # Real powershell would create the .lnk; stub that side effect.
        (tmp_path / "tapout-buddy.lnk").write_text("stub", encoding="utf-8")

    monkeypatch.setattr("tapout.tray.subprocess.run", fake_run)
    result = runner.invoke(app, ["buddy", "install-autostart"])
    assert result.exit_code == 0, result.output
    assert "Autostart installed" in result.output
    assert (tmp_path / "tapout-buddy.lnk").exists()

    result = runner.invoke(app, ["buddy", "uninstall-autostart"])
    assert result.exit_code == 0, result.output
    assert "removed" in result.output.lower()
    assert not (tmp_path / "tapout-buddy.lnk").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="only exercises the non-Windows refusal path")
def test_cli_install_autostart_refuses_on_non_windows():
    result = runner.invoke(app, ["buddy", "install-autostart"])
    assert result.exit_code == 1
    assert "windows-only" in result.output.lower()


# --------------------------------------------------------------------------
# last-active-repo detection (reads the real cwd out of transcript content)
# --------------------------------------------------------------------------

def test_extract_cwd_from_transcript(tmp_path: Path):
    t = tmp_path / "t.jsonl"
    t.write_text(json.dumps({"cwd": r"C:\Users\User\Videos\myrepo"}) + "\n", encoding="utf-8")
    assert tray._extract_cwd_from_transcript(t) == Path(r"C:\Users\User\Videos\myrepo")


def test_extract_cwd_from_transcript_missing_field(tmp_path: Path):
    t = tmp_path / "t.jsonl"
    t.write_text(json.dumps({"no_cwd_here": True}) + "\n", encoding="utf-8")
    assert tray._extract_cwd_from_transcript(t) is None


def test_find_last_active_repo_picks_newest(tmp_path: Path, monkeypatch):
    import time as time_mod
    projects = tmp_path / "projects"
    monkeypatch.setattr("tapout.capture.CLAUDE_PROJECTS", projects)

    old_proj = projects / "C--old-repo"
    old_proj.mkdir(parents=True)
    old_t = old_proj / "a.jsonl"
    old_t.write_text(json.dumps({"cwd": "C:\\old-repo"}) + "\n", encoding="utf-8")

    new_proj = projects / "C--new-repo"
    new_proj.mkdir(parents=True)
    new_t = new_proj / "b.jsonl"
    new_t.write_text(json.dumps({"cwd": "C:\\new-repo"}) + "\n", encoding="utf-8")

    os.utime(old_t, (1, 1000))
    os.utime(new_t, (1, 2000))

    assert tray.find_last_active_repo() == Path("C:\\new-repo")


def test_find_last_active_repo_none_when_no_projects(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("tapout.capture.CLAUDE_PROJECTS", tmp_path / "nope")
    assert tray.find_last_active_repo() is None


# --------------------------------------------------------------------------
# prep_handoff_now / resume_in wiring
# --------------------------------------------------------------------------

def test_prep_handoff_now_uses_given_repo(tmp_path: Path, monkeypatch):
    called = {}
    monkeypatch.setattr(tray, "find_project_transcript", lambda repo: tmp_path / "t.jsonl")
    monkeypatch.setattr(tray, "run_capture", lambda repo, agent, tpath, force, use_llm: called.update(
        repo=repo, agent=agent, tpath=tpath, force=force, use_llm=use_llm) or "STATE")
    result = tray.prep_handoff_now(tmp_path)
    assert result == "STATE"
    assert called == {"repo": tmp_path, "agent": "claude", "tpath": tmp_path / "t.jsonl",
                       "force": True, "use_llm": False}


def test_prep_handoff_now_falls_back_to_last_active(monkeypatch, tmp_path):
    monkeypatch.setattr(tray, "find_last_active_repo", lambda: tmp_path)
    monkeypatch.setattr(tray, "find_project_transcript", lambda repo: None)
    monkeypatch.setattr(tray, "run_capture", lambda *a, **k: "STATE")
    assert tray.prep_handoff_now() == "STATE"


def test_prep_handoff_now_no_repo_found(monkeypatch):
    monkeypatch.setattr(tray, "find_last_active_repo", lambda: None)
    assert tray.prep_handoff_now() is None


def test_resume_in_success(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(tray, "prepare_resume", lambda repo, key: "PLAN")
    launched = []
    monkeypatch.setattr(tray, "launch", lambda plan, repo: launched.append((plan, repo)))
    assert tray.resume_in(tmp_path, "codex") is True
    assert launched == [("PLAN", tmp_path)]


def test_resume_in_failure_returns_false(tmp_path: Path, monkeypatch):
    from tapout.resume import ResumeError

    def boom(repo, key):
        raise ResumeError("not installed")

    monkeypatch.setattr(tray, "prepare_resume", boom)
    assert tray.resume_in(tmp_path, "codex") is False


# --------------------------------------------------------------------------
# poll_once + integration smoke test (`tap buddy --dry-run`, CI-runnable)
# --------------------------------------------------------------------------

def test_poll_once_computes_and_persists(tmp_path: Path, monkeypatch):
    status_file = tmp_path / "claude-status.json"
    status = {"pct_5h": 85.0, "pct_7d": None, "ts": __import__("time").time()}
    status_file.write_text(json.dumps(status), encoding="utf-8")
    monkeypatch.setattr("tapout.monitor.STATE_FILE", status_file)
    monkeypatch.setattr(tray, "TRAY_STATE_PATH", tmp_path / "tray-state.json")
    monkeypatch.setattr(tray, "TAPOUT_DIR", tmp_path)
    monkeypatch.setattr(tray, "detect_all", lambda: [])

    result = tray.poll_once()
    assert result["color"] == "amber"  # 85 is amber (red starts at 90)
    assert result["events"] == ["warning_85"]

    # second call: idempotent, no repeat fire
    result2 = tray.poll_once()
    assert result2["events"] == []


def test_dry_run_prints_icon_and_tooltip(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setattr(tray, "poll_once", lambda: {
        "color": "amber", "tooltip": "Claude: 75% of 5h window", "events": [], "status": {}
    })
    tray.run_dry_run()
    out = capsys.readouterr().out
    assert "icon: amber" in out
    assert "Claude: 75%" in out
    assert "would fire: (none)" in out


def test_cli_buddy_dry_run_smoke(tmp_path: Path, monkeypatch):
    # This is the CI-runnable integration smoke test: no pystray/PIL/windows_toasts
    # touched, no real GUI — just the poll cycle end to end through the CLI.
    monkeypatch.setattr("tapout.monitor.STATE_FILE", tmp_path / "missing.json")
    result = runner.invoke(app, ["buddy", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "icon:" in result.output
    assert "tooltip:" in result.output
    assert "would fire:" in result.output


# --------------------------------------------------------------------------
# cached_detect_all — regression: detect_all() shells out (~3.5s for 7 agents
# with version probes) and was being called on every 5s poll tick, making the
# tray feel laggy/unresponsive. Must be cached.
# --------------------------------------------------------------------------

def test_cached_detect_all_only_calls_detect_all_once_within_ttl(monkeypatch):
    monkeypatch.setitem(tray._detect_cache, "results", None)
    monkeypatch.setitem(tray._detect_cache, "ts", 0.0)
    calls = []
    monkeypatch.setattr(tray, "detect_all", lambda: calls.append(1) or ["fake"])

    r1 = tray.cached_detect_all(now=1000.0)
    r2 = tray.cached_detect_all(now=1000.0 + 30)  # within TTL (60s)
    assert len(calls) == 1
    assert r1 == r2 == ["fake"]


def test_cached_detect_all_refreshes_after_ttl(monkeypatch):
    monkeypatch.setitem(tray._detect_cache, "results", None)
    monkeypatch.setitem(tray._detect_cache, "ts", 0.0)
    calls = []
    monkeypatch.setattr(tray, "detect_all", lambda: calls.append(1) or ["fake"])

    tray.cached_detect_all(now=1000.0)
    tray.cached_detect_all(now=1000.0 + tray._DETECT_CACHE_TTL + 1)
    assert len(calls) == 2


def test_poll_once_uses_cache_not_raw_detect_all(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("tapout.monitor.STATE_FILE", tmp_path / "missing.json")
    calls = []
    monkeypatch.setattr(tray, "cached_detect_all", lambda: calls.append(1) or [])
    monkeypatch.setattr(tray, "detect_all", lambda: (_ for _ in ()).throw(
        AssertionError("poll_once must use cached_detect_all, not detect_all directly")))
    tray.poll_once()
    assert calls == [1]


def test_tray_deps_available_never_raises_and_returns_a_bool():
    # pystray/PIL ARE installed in this test env, but pystray's Linux Xorg
    # backend probes the display at import time — on a headless box (no
    # DISPLAY: CI, SSH, Docker) that raises Xlib.error, not ImportError. The
    # contract is "never a traceback, always a clean (bool, str) verdict" —
    # not "always True" (platform/environment-dependent).
    ok, err = tray.tray_deps_available()
    assert isinstance(ok, bool)
    assert isinstance(err, str)
    if sys.platform == "win32":
        assert ok is True
        assert err == ""


def test_tray_deps_available_catches_non_import_errors(monkeypatch):
    # Simulates exactly the headless-Linux Xlib.error.DisplayNameError case:
    # the module "imports" but raises a non-ImportError during init.
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "pystray":
            raise RuntimeError("Bad display name \"\"")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    ok, err = tray.tray_deps_available()
    assert ok is False
    assert "display" in err.lower()
