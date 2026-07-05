"""tapout buddy — Windows system tray icon over the pipes Slices 1-2.6 built.

Everything in this module that touches a real GUI/toast/tray library imports
that library LAZILY, inside the function that needs it. The pure logic (the
edge-trigger state machine, icon color selection, tooltip text, which agents
are eligible for a menu/toast) has zero GUI dependencies and is what the unit
tests exercise — `pip install tapout` alone (no `[tray]` extra) can still run
`tap buddy --dry-run` and the whole test suite for this module.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .capture import CaptureError, TaskState, find_project_transcript, run_capture
from .detect import DetectionResult, detect_all
from .monitor import _fmt_reset, read_state
from .resume import ResumeError, launch, prepare_resume

TAPOUT_DIR = Path.home() / ".tapout"
TRAY_STATE_PATH = TAPOUT_DIR / "tray-state.json"
TRAY_LOG_PATH = TAPOUT_DIR / "tray.log"
TRAY_LOCK_PATH = TAPOUT_DIR / "tray.lock"
TRAY_ICON_DIR = TAPOUT_DIR / "tray-icons"

WARNING_THRESHOLD = 85.0
LIMIT_THRESHOLD = 100.0
WEEKLY_THRESHOLD = 90.0
RESET_DROP_THRESHOLD = 40.0  # a same-poll drop bigger than this means the window rolled over
STALE_AFTER_SEC = 60.0
POLL_INTERVAL_SEC = 5.0

DEFAULT_TRAY_STATE = {
    "previous_pct_5h": 0.0,
    "warning_85_fired": False,
    "limit_100_fired": False,
    "previous_pct_7d": 0.0,
    "warning_week_90_fired": False,
}


def log_tray(message: str) -> None:
    try:
        TAPOUT_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        with TRAY_LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(f"{ts} {message}\n")
    except OSError:
        pass


# --------------------------------------------------------------------------
# edge-trigger state machine (pure logic — no GUI deps)
# --------------------------------------------------------------------------

def load_tray_state() -> dict:
    if not TRAY_STATE_PATH.exists():
        return dict(DEFAULT_TRAY_STATE)
    try:
        data = json.loads(TRAY_STATE_PATH.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return dict(DEFAULT_TRAY_STATE)
    merged = dict(DEFAULT_TRAY_STATE)
    merged.update(data)
    return merged


def save_tray_state(state: dict) -> None:
    try:
        TAPOUT_DIR.mkdir(parents=True, exist_ok=True)
        TRAY_STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    except OSError:
        pass


def compute_toast_events(state: dict, pct_5h: Optional[float], pct_7d: Optional[float]) -> tuple[list[str], dict]:
    """Given the persisted state and the latest readings, return (events, new_state).

    Events fire on TRANSITIONS (previous < threshold <= current), gated by a
    per-window flag so they never repeat until a reset is detected (a
    same-poll drop bigger than RESET_DROP_THRESHOLD, i.e. the window rolled
    over). The flag — not just the raw comparison — is what makes this
    idempotent across daemon restarts: a restart reloads the flag from disk,
    so a transient dip-then-rise within the same window can't re-fire it.
    """
    state = dict(state)
    events: list[str] = []

    if pct_5h is not None:
        prev5 = float(state.get("previous_pct_5h", 0.0))
        if prev5 - pct_5h > RESET_DROP_THRESHOLD:
            state["warning_85_fired"] = False
            state["limit_100_fired"] = False
            prev5 = float(state.get("previous_pct_5h", 0.0))  # only flags reset; comparison below still uses real prev

        if prev5 < WARNING_THRESHOLD <= pct_5h and not state.get("warning_85_fired"):
            events.append("warning_85")
            state["warning_85_fired"] = True

        if prev5 < LIMIT_THRESHOLD <= pct_5h and not state.get("limit_100_fired"):
            events.append("limit_100")
            state["limit_100_fired"] = True

        state["previous_pct_5h"] = pct_5h

    if pct_7d is not None:
        prev7 = float(state.get("previous_pct_7d", 0.0))
        if prev7 - pct_7d > RESET_DROP_THRESHOLD:
            state["warning_week_90_fired"] = False
            prev7 = float(state.get("previous_pct_7d", 0.0))

        if prev7 < WEEKLY_THRESHOLD <= pct_7d and not state.get("warning_week_90_fired"):
            events.append("warning_week_90")
            state["warning_week_90_fired"] = True

        state["previous_pct_7d"] = pct_7d

    return events, state


# --------------------------------------------------------------------------
# icon color + tooltip (pure logic)
# --------------------------------------------------------------------------

_COLOR_RGB = {
    "green": (46, 160, 67, 255),
    "amber": (230, 154, 23, 255),
    "red": (218, 54, 51, 255),
    "gray": (140, 140, 140, 255),
}


def icon_color_for_pct(pct: Optional[float]) -> str:
    if pct is None:
        return "gray"
    if pct >= LIMIT_THRESHOLD or pct >= 90:
        return "red"
    if pct >= 70:
        return "amber"
    return "green"


def icon_color_for_status(status: Optional[dict], now: Optional[float] = None) -> str:
    if not status:
        return "gray"
    now = time.time() if now is None else now
    age = now - status.get("ts", 0)
    if age > STALE_AFTER_SEC:
        return "gray"
    return icon_color_for_pct(status.get("pct_5h"))


def build_tooltip(status: Optional[dict], results: Optional[list[DetectionResult]] = None, now: Optional[float] = None) -> str:
    now = time.time() if now is None else now
    if not status or (now - status.get("ts", 0)) > STALE_AFTER_SEC:
        return "no status data yet — is `tap statusline` configured in Claude Code's settings.json?"

    parts: list[str] = []
    pct5 = status.get("pct_5h")
    if pct5 is not None:
        line = f"Claude: {round(pct5)}% of 5h window"
        resets5 = status.get("resets_5h")
        if resets5:
            line += f" · resets {_fmt_reset(resets5)}"
        parts.append(line)
    else:
        parts.append("Claude: --")

    if results is None:
        results = detect_all()
    for r in results:
        if r.entry.key not in ("codex", "gemini"):
            continue
        parts.append(f"{r.entry.display_name}: {_agent_readiness_label(r)}")

    return " · ".join(parts)


def _agent_readiness_label(r: DetectionResult) -> str:
    if r.entry.notes and "deprecat" in r.entry.notes.lower():
        return "deprecated"
    return "ready" if r.installed else "not installed"


# --------------------------------------------------------------------------
# menu / toast eligibility (pure logic)
# --------------------------------------------------------------------------

def _is_deprecated(entry) -> bool:
    return bool(entry.notes) and "deprecat" in entry.notes.lower()


def menu_resume_agents(results: Optional[list[DetectionResult]] = None) -> list[tuple[str, str]]:
    """(key, display_name) pairs for the right-click 'Resume in ->' submenu.

    Installed, not deprecated. Clipboard-only agents are still menu-clickable
    (a human driving a menu can paste manually) even though they're excluded
    from the automated limit-hit toast buttons below.
    """
    results = cached_detect_all() if results is None else results
    return [
        (r.entry.key, r.entry.display_name)
        for r in results
        if r.installed and not _is_deprecated(r.entry)
    ]


def limit_toast_agents(results: Optional[list[DetectionResult]] = None) -> list[tuple[str, str]]:
    """(key, display_name) pairs eligible for the limit-hit toast's buttons.

    Installed, resumable without a human pasting (resume_style != clipboard_only),
    and not flagged deprecated in the registry.
    """
    results = cached_detect_all() if results is None else results
    return [
        (r.entry.key, r.entry.display_name)
        for r in results
        if r.installed and r.entry.resume_style != "clipboard_only" and not _is_deprecated(r.entry)
    ]


# --------------------------------------------------------------------------
# actions — same code paths as `tap out` / `tap <agent>`, called in-process
# --------------------------------------------------------------------------

def find_last_active_repo() -> Optional[Path]:
    """Best-effort 'current session' proxy: the repo whose Claude Code project
    dir has the most recently modified top-level transcript. Reads the real
    repo path out of the transcript's own `cwd` field — project directory
    names are lossily mangled and can't be reversed.
    """
    from .capture import CLAUDE_PROJECTS

    if not CLAUDE_PROJECTS.exists():
        return None
    newest: Optional[Path] = None
    newest_mtime = -1.0
    for d in CLAUDE_PROJECTS.iterdir():
        if not d.is_dir():
            continue
        for f in d.glob("*.jsonl"):
            mtime = f.stat().st_mtime
            if mtime > newest_mtime:
                newest_mtime = mtime
                newest = f
    if newest is None:
        return None
    return _extract_cwd_from_transcript(newest)


def _extract_cwd_from_transcript(transcript: Path) -> Optional[Path]:
    try:
        with transcript.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                cwd = obj.get("cwd")
                if cwd:
                    return Path(cwd)
    except OSError:
        return None
    return None


def prep_handoff_now(repo: Optional[Path] = None) -> Optional[TaskState]:
    """'Prep handoff now' — capture immediately for the given (or last-active) repo."""
    if repo is None:
        repo = find_last_active_repo()
    if repo is None:
        log_tray("prep handoff: no Claude Code project found")
        return None
    try:
        transcript = find_project_transcript(repo)
    except CaptureError:
        transcript = None
    try:
        return run_capture(repo, "claude", transcript, force=True, use_llm=False)
    except CaptureError as exc:
        log_tray(f"prep handoff failed for {repo}: {exc!r}")
        return None


def resume_in(repo: Path, agent_key: str) -> bool:
    """'Resume in -> <agent>' — same as `tap <agent>` from the CLI, called directly."""
    try:
        plan = prepare_resume(repo, agent_key)
        launch(plan, repo)
        return True
    except ResumeError as exc:
        log_tray(f"resume in {agent_key} failed: {exc!r}")
        return False


# --------------------------------------------------------------------------
# icon image (Pillow, lazily imported, disk-cached)
# --------------------------------------------------------------------------

def get_tray_icon_image(color: str):
    from PIL import Image, ImageDraw

    TRAY_ICON_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = TRAY_ICON_DIR / f"{color}.png"
    if cache_path.exists():
        try:
            return Image.open(cache_path).convert("RGBA")
        except Exception:
            pass

    size = 32
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    fill = _COLOR_RGB.get(color, _COLOR_RGB["gray"])
    draw.ellipse((2, 2, size - 2, size - 2), fill=fill)
    try:
        img.save(cache_path, format="PNG")
    except OSError:
        pass
    return img


# --------------------------------------------------------------------------
# single-instance lock
# --------------------------------------------------------------------------

def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def acquire_lock() -> bool:
    """True if this process now holds the lock; False if another instance runs."""
    TAPOUT_DIR.mkdir(parents=True, exist_ok=True)
    if TRAY_LOCK_PATH.exists():
        try:
            existing = int(TRAY_LOCK_PATH.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            existing = None
        if existing and existing != os.getpid() and _pid_alive(existing):
            return False
    try:
        TRAY_LOCK_PATH.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass
    return True


def release_lock() -> None:
    try:
        if TRAY_LOCK_PATH.exists():
            if TRAY_LOCK_PATH.read_text(encoding="utf-8").strip() == str(os.getpid()):
                TRAY_LOCK_PATH.unlink()
    except OSError:
        pass


# --------------------------------------------------------------------------
# toasts (windows_toasts, lazily imported; silent no-op elsewhere)
# --------------------------------------------------------------------------

def _send_toast(title: str, body: str, buttons: list[tuple[str, str]], on_click) -> None:
    """buttons: (label, argument) pairs. on_click(argument) fires on activation."""
    if sys.platform != "win32":
        log_tray(f"toast skipped (non-Windows): {title}")
        return
    try:
        from windows_toasts import InteractableWindowsToaster, Toast, ToastButton
    except Exception as exc:
        log_tray(f"toast skipped (windows_toasts unavailable): {exc!r}")
        return
    try:
        toaster = InteractableWindowsToaster("tapout")
        toast = Toast()
        toast.text_fields = [title, body] if body else [title]
        for label, arg in buttons:
            toast.AddAction(ToastButton(label, arg))

        def _on_activated(args) -> None:
            try:
                if on_click and getattr(args, "arguments", None):
                    on_click(args.arguments)
            except Exception as exc2:
                log_tray(f"toast callback failed: {exc2!r}")

        toast.on_activated = _on_activated
        toaster.show_toast(toast)
    except Exception as exc:
        log_tray(f"toast failed to send ({title}): {exc!r}")


def fire_warning_toast(repo: Optional[Path] = None) -> None:
    def on_click(arg: str) -> None:
        if arg == "prep_handoff":
            prep_handoff_now(repo)

    _send_toast(
        "Claude window almost done",
        "85% of your 5-hour window used. Prep a handoff now?",
        [("Prep handoff", "prep_handoff")],
        on_click,
    )


def fire_limit_toast(repo: Optional[Path] = None, agents: Optional[list[tuple[str, str]]] = None) -> None:
    agents = limit_toast_agents() if agents is None else agents

    def on_click(arg: str) -> None:
        target_repo = repo or find_last_active_repo()
        if target_repo:
            resume_in(target_repo, arg)

    buttons = [(f"→ {label}", key) for key, label in agents]
    _send_toast(
        "Claude tapped out",
        "5-hour window exhausted. Tag in the next agent?",
        buttons,
        on_click,
    )


def fire_weekly_toast() -> None:
    _send_toast(
        "Claude weekly window almost done",
        "90% of your 7-day window used.",
        [],
        None,
    )


def fire_already_running_toast() -> None:
    _send_toast("tapout buddy is already running", "", [], None)


def dispatch_toast_event(event: str) -> None:
    if event == "warning_85":
        fire_warning_toast()
    elif event == "limit_100":
        fire_limit_toast()
    elif event == "warning_week_90":
        fire_weekly_toast()


# --------------------------------------------------------------------------
# poll cycle (shared by --dry-run and the real loop)
# --------------------------------------------------------------------------

# detect_all() shells out to probe each agent's --version (measured: ~3.5s for
# 7 agents) — spawning that on every 5s poll tick would eat most of the poll
# interval for data that barely changes. Cache it; the menu/tooltip only need
# to be this fresh, not sub-second.
_DETECT_CACHE_TTL = 60.0
_detect_cache: dict = {"ts": 0.0, "results": None}


def cached_detect_all(now: Optional[float] = None) -> list[DetectionResult]:
    now = time.time() if now is None else now
    if _detect_cache["results"] is None or now - _detect_cache["ts"] > _DETECT_CACHE_TTL:
        _detect_cache["results"] = detect_all()
        _detect_cache["ts"] = now
    return _detect_cache["results"]


def poll_once() -> dict:
    status = read_state()
    results = cached_detect_all()
    color = icon_color_for_status(status)
    tooltip = build_tooltip(status, results)
    events: list[str] = []
    if status is not None and (time.time() - status.get("ts", 0)) <= STALE_AFTER_SEC:
        state = load_tray_state()
        events, new_state = compute_toast_events(state, status.get("pct_5h"), status.get("pct_7d"))
        save_tray_state(new_state)
    return {"color": color, "tooltip": tooltip, "events": events, "status": status}


def run_dry_run() -> None:
    result = poll_once()
    print(f"icon: {result['color']}")
    print(f"tooltip: {result['tooltip']}")
    if result["events"]:
        print(f"would fire: {', '.join(result['events'])}")
    else:
        print("would fire: (none)")


# --------------------------------------------------------------------------
# tray loop (pystray, lazily imported)
# --------------------------------------------------------------------------

def tray_deps_available() -> tuple[bool, str]:
    try:
        import pystray  # noqa: F401
        import PIL  # noqa: F401
    except ImportError as exc:
        return False, str(exc)
    return True, ""


_FIRST_RUN_HINT_PATH = TAPOUT_DIR / ".tray-hint-shown"


def _print_first_run_hint_once() -> None:
    if _FIRST_RUN_HINT_PATH.exists():
        return
    print(
        "Tray icon is running. If you can't see it, click the ^ arrow in the "
        "notification area and drag tapout to the visible tray."
    )
    try:
        TAPOUT_DIR.mkdir(parents=True, exist_ok=True)
        _FIRST_RUN_HINT_PATH.write_text("1", encoding="utf-8")
    except OSError:
        pass


def _open_tapout_folder() -> None:
    try:
        TAPOUT_DIR.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(str(TAPOUT_DIR))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.run(["open", str(TAPOUT_DIR)])
        else:
            subprocess.run(["xdg-open", str(TAPOUT_DIR)])
    except Exception as exc:
        log_tray(f"open folder failed: {exc!r}")


def _show_status_window() -> None:
    try:
        import tkinter as tk

        status = read_state() or {}
        root = tk.Tk()
        root.title("tapout status")
        text = tk.Text(root, width=64, height=24)
        text.insert("1.0", json.dumps(status, indent=2, sort_keys=True))
        text.configure(state="disabled")
        text.pack(fill="both", expand=True)
        root.mainloop()
    except Exception as exc:
        log_tray(f"status window failed: {exc!r}")


def _show_about_window() -> None:
    try:
        import tkinter as tk

        from . import __version__

        root = tk.Tk()
        root.title("About tapout")
        tk.Label(root, text=f"tapout {__version__}", font=("Segoe UI", 12, "bold")).pack(padx=24, pady=(20, 6))
        tk.Label(root, text="https://pypi.org/project/tapout/").pack(padx=24)
        tk.Label(root, text="https://github.com/MIthunvasanth/tapout").pack(padx=24, pady=(0, 20))
        root.mainloop()
    except Exception as exc:
        log_tray(f"about window failed: {exc!r}")


def _build_menu():
    import pystray
    import webbrowser

    def on_prep_handoff(icon_obj, item):
        prep_handoff_now()

    def make_resume_action(key: str):
        def _action(icon_obj, item):
            repo = find_last_active_repo()
            if repo:
                resume_in(repo, key)
        return _action

    resume_pairs = menu_resume_agents()
    if resume_pairs:
        resume_submenu = pystray.Menu(*[
            pystray.MenuItem(label, make_resume_action(key)) for key, label in resume_pairs
        ])
        resume_item = pystray.MenuItem("Resume in", resume_submenu)
    else:
        resume_item = pystray.MenuItem("Resume in (no agents installed)", None, enabled=False)

    def on_show_status(icon_obj, item):
        _show_status_window()

    def on_open_folder(icon_obj, item):
        _open_tapout_folder()

    def on_docs(icon_obj, item):
        webbrowser.open("https://github.com/MIthunvasanth/tapout")

    def on_about(icon_obj, item):
        _show_about_window()

    def on_quit(icon_obj, item):
        icon_obj._tapout_running = False
        icon_obj.stop()

    return pystray.Menu(
        pystray.MenuItem("Prep handoff now", on_prep_handoff),
        resume_item,
        pystray.MenuItem("Show status window", on_show_status),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Open ~/.tapout", on_open_folder),
        pystray.MenuItem("Docs", on_docs),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("About tapout", on_about),
        pystray.MenuItem("Quit", on_quit),
    )


def _run_pystray_loop() -> None:
    import pystray

    _print_first_run_hint_once()

    icon = pystray.Icon("tapout", icon=get_tray_icon_image("gray"), title="tapout: starting…")
    icon.menu = _build_menu()
    icon._tapout_running = True

    def setup(icon_obj):
        icon_obj.visible = True
        while getattr(icon_obj, "_tapout_running", True):
            try:
                result = poll_once()
                icon_obj.icon = get_tray_icon_image(result["color"])
                icon_obj.title = result["tooltip"][:127]  # Windows tooltip length cap
                icon_obj.menu = _build_menu()  # cheap: backed by cached_detect_all()
                for event in result["events"]:
                    dispatch_toast_event(event)
            except Exception as exc:
                log_tray(f"poll cycle failed: {exc!r}")
            time.sleep(POLL_INTERVAL_SEC)

    icon.run(setup=setup)


def run_buddy() -> None:
    ok, err = tray_deps_available()
    if not ok:
        print("tapout buddy needs the tray extras — install with: pip install tapout[tray]", file=sys.stderr)
        log_tray(f"buddy exited: missing tray deps: {err}")
        return

    if not acquire_lock():
        log_tray("another tapout buddy instance is already running; exiting")
        fire_already_running_toast()
        return

    try:
        _run_pystray_loop()
    finally:
        release_lock()


def relaunch_detached_if_needed() -> bool:
    """Re-exec without a console window if invoked with --detached from one.

    Returns True if a relaunch/fork was performed and the caller should exit
    immediately without doing any further work.
    """
    if sys.platform == "win32":
        if Path(sys.executable).name.lower() == "pythonw.exe":
            return False  # already consoleless
        pythonw = _pythonw_path()
        if Path(pythonw).name.lower() != "pythonw.exe":
            return False  # no pythonw available; run in the current console
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        subprocess.Popen(
            [pythonw, "-m", "tapout", "buddy", "--detached"],
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return True
    else:
        pid = os.fork()
        if pid > 0:
            return True  # parent exits; child continues detached
        os.setsid()
        return False


# --------------------------------------------------------------------------
# autostart (Windows Startup shortcut via WScript.Shell COM, no pywin32 needed)
# --------------------------------------------------------------------------

STARTUP_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
AUTOSTART_SHORTCUT_NAME = "tapout-buddy.lnk"


def _pythonw_path() -> str:
    exe = Path(sys.executable)
    pythonw = exe.parent / "pythonw.exe"
    return str(pythonw) if pythonw.exists() else str(exe)


def autostart_shortcut_path() -> Path:
    return STARTUP_DIR / AUTOSTART_SHORTCUT_NAME


def install_autostart() -> Path:
    STARTUP_DIR.mkdir(parents=True, exist_ok=True)
    shortcut_path = autostart_shortcut_path()
    pythonw = _pythonw_path()
    ps_script = (
        f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut('{shortcut_path}'); "
        f"$s.TargetPath = '{pythonw}'; "
        f"$s.Arguments = '-m tapout buddy --detached'; "
        f"$s.WorkingDirectory = '{Path.home()}'; "
        f"$s.Save()"
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
        check=True, capture_output=True, text=True,
    )
    return shortcut_path


def uninstall_autostart() -> bool:
    shortcut_path = autostart_shortcut_path()
    if shortcut_path.exists():
        shortcut_path.unlink()
        return True
    return False
