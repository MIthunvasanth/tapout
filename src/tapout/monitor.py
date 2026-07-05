"""Statusline sink + `tap watch`.

Claude Code only exposes usage/rate-limit data to a *statusline* command (via
stdin JSON). A standalone process can't read it directly. So `tap statusline`
does double duty: it prints the "tap: N%" indicator for the status bar AND
writes the parsed state to a file that `tap watch` (and future Slice-3 daemons)
can read. There is no documented exit-code-11 limit signal; the limit is driven
off rate_limits.five_hour.used_percentage reaching ~100%.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from .detect import resolve_executable

STATE_DIR = Path.home() / ".tapout"
STATE_FILE = STATE_DIR / "claude-status.json"

WARN_PCT = 90.0
LIMIT_PCT = 100.0


def parse_status(data: dict) -> dict:
    """Extract the fields we care about from Claude Code's statusline JSON."""
    rl = data.get("rate_limits") or {}
    five = rl.get("five_hour") or {}
    seven = rl.get("seven_day") or {}
    ctx = data.get("context_window") or {}
    workspace = data.get("workspace") or {}
    return {
        "pct_5h": five.get("used_percentage"),
        "resets_5h": five.get("resets_at"),
        "pct_7d": seven.get("used_percentage"),
        "resets_7d": seven.get("resets_at"),
        "context_pct": ctx.get("used_percentage"),
        "transcript_path": data.get("transcript_path"),
        "session_id": data.get("session_id"),
        "cwd": data.get("cwd") or workspace.get("current_dir"),
        "ts": time.time(),
    }


def indicator(state: dict) -> str:
    """One-glance status-bar string, e.g. 'tap: 84%' or 'tap: 100% LIMIT'."""
    pct = state.get("pct_5h")
    if pct is None:
        ctx = state.get("context_pct")
        if ctx is None:
            return "tap: --"
        return f"tap: ctx {round(ctx)}%"
    text = f"tap: {round(pct)}%"
    if pct >= LIMIT_PCT:
        text += " LIMIT"
    elif pct >= WARN_PCT:
        text += " !"
    return text


def write_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state), encoding="utf-8")


def read_state() -> Optional[dict]:
    if not STATE_FILE.exists():
        return None
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def run_statusline(stdin_text: Optional[str] = None) -> str:
    """Read CC statusline JSON, persist state, return (and print) the indicator."""
    raw = stdin_text if stdin_text is not None else sys.stdin.read()
    try:
        data = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        data = {}
    state = parse_status(data)
    try:
        write_state(state)
    except OSError:
        pass
    line = indicator(state)
    print(line)
    return line


def _fmt_reset(epoch) -> str:
    if not epoch:
        return "?"
    try:
        return datetime.fromtimestamp(float(epoch)).strftime("%H:%M")
    except (ValueError, OSError, OverflowError):
        return "?"


def watch_line(state: dict) -> str:
    """The live line `tap watch` prints each tick."""
    pct = state.get("pct_5h")
    ctx = state.get("context_pct")
    if pct is None and ctx is None:
        return "waiting for Claude Code statusline data… (open a Claude session with tapout's statusline configured)"
    parts = []
    if pct is not None:
        parts.append(f"{round(pct)}% of 5h window · resets {_fmt_reset(state.get('resets_5h'))}")
    if state.get("pct_7d") is not None:
        parts.append(f"{round(state['pct_7d'])}% of 7d · resets {_fmt_reset(state.get('resets_7d'))}")
    if ctx is not None:
        parts.append(f"ctx {round(ctx)}%")
    return " · ".join(parts)


def is_limit_hit(state: dict) -> bool:
    pct = state.get("pct_5h")
    return pct is not None and pct >= LIMIT_PCT


def run_watch(
    *,
    once: bool = False,
    interval: float = 2.0,
    duration: Optional[float] = None,
    emit: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Foreground monitor. Degrades gracefully if Claude Code isn't installed."""
    if not resolve_executable("claude"):
        emit("no monitorable agents on this machine (Claude Code not found on PATH)")
        return

    emit("tap watch — monitoring Claude Code usage (Ctrl-C to stop)")
    start = clock()
    limit_announced = False
    while True:
        state = read_state()
        if state is None:
            emit("waiting for statusline data… (configure tapout's statusline in Claude Code settings)")
        else:
            emit(watch_line(state))
            if is_limit_hit(state) and not limit_announced:
                emit("")
                emit("==================================================")
                emit("  LIMIT HIT — run:  tap codex")
                emit("==================================================")
                limit_announced = True
            elif not is_limit_hit(state):
                limit_announced = False
        if once:
            return
        if duration is not None and (clock() - start) >= duration:
            return
        sleep(interval)
