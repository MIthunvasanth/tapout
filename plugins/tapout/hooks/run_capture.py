#!/usr/bin/env python
"""tapout PreCompact / SessionEnd hook launcher.

Claude Code pipes the hook JSON (session_id, transcript_path, cwd, ...) to this
script on stdin. We call tapout's capture logic IN-PROCESS (no subprocess) so a
Windows session-teardown process-group kill at `/exit` can't cut a spawned
child off mid-flight — that was the root cause of #1's SessionEnd silently
producing nothing. If the in-process import fails (e.g. this interpreter
doesn't have tapout installed), fall back to spawning `python -m tapout` /
`uvx tapout`, preferring the installed package.

Kept dependency-free and defensive: a hook must never crash the session, so
any failure is swallowed with exit 0 (SessionEnd ignores exit codes anyway,
and a non-zero PreCompact code is only a non-blocking notice). If EVERY path
fails, a one-line reason is appended to .tapout/capture.log so a user hitting
this in the wild has something to debug from.
"""

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone

CAPTURE_ARGS = ["capture", "--agent", "claude", "--hook-stdin", "--force"]


def _fallback_log(hook_json: str, message: str) -> None:
    """Minimal, dependency-free log used only when tapout itself is unreachable."""
    try:
        cwd = json.loads(hook_json).get("cwd") if hook_json.strip() else None
        if not cwd:
            return
        log_path = os.path.join(cwd, ".tapout", "capture.log")
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as fh:
            ts = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
            fh.write(f"{ts} {message}\n")
    except Exception:
        pass


def main() -> int:
    hook_json = sys.stdin.read()

    try:
        from tapout.capture import run_hook_capture

        run_hook_capture(hook_json, agent="claude", force=True)
        return 0
    except Exception as exc:
        _fallback_log(hook_json, f"SessionEnd: in-process capture failed: {exc!r}")
        # Fall through to the subprocess fallback below.

    candidates = [[sys.executable, "-m", "tapout", *CAPTURE_ARGS]]
    if shutil.which("uvx"):
        candidates.append(["uvx", "tapout", *CAPTURE_ARGS])

    for cmd in candidates:
        try:
            proc = subprocess.run(cmd, input=hook_json, text=True)
        except OSError:
            continue
        if proc.returncode == 0:
            return 0

    _fallback_log(hook_json, "SessionEnd: capture failed via in-process and subprocess fallback")
    # Best-effort: never disrupt the Claude session.
    return 0


if __name__ == "__main__":
    sys.exit(main())
