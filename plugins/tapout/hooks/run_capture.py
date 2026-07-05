#!/usr/bin/env python
"""tapout PreCompact / SessionEnd hook launcher.

Claude Code pipes the hook JSON (session_id, transcript_path, cwd, ...) to this
script on stdin. We forward it to `tap capture --hook-stdin`, preferring the
installed tapout package and falling back to uvx (zero-install) if needed.

Kept dependency-free and defensive: a hook must never crash the session, so any
failure is swallowed with exit 0 (SessionEnd ignores exit codes anyway, and a
non-zero PreCompact code is only a non-blocking notice).
"""

import shutil
import subprocess
import sys

CAPTURE_ARGS = ["capture", "--agent", "claude", "--hook-stdin", "--force"]


def main() -> int:
    hook_json = sys.stdin.read()

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
    # Best-effort: never disrupt the Claude session.
    return 0


if __name__ == "__main__":
    sys.exit(main())
