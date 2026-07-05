"""Machine-invoked capture: turn a session transcript into handoff artifacts.

This is the non-interactive sibling of `tap out`. Claude Code's PreCompact /
SessionEnd hooks invoke it; it reads the hook JSON on stdin, summarizes the
session transcript into the task-state schema, and writes the artifacts
atomically so a crash mid-capture never leaves a half-written state file.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

from .detect import resolve_executable
from .handoff import (
    SUMMARIZATION_PROMPT,
    TaskState,
    now_iso,
    parse_task_state,
    render_markdown,
    tapout_paths,
)

# Offline / test override: if set, read the agent's JSON reply from this file
# instead of calling the summarizer. Also handy for deterministic demos.
ENV_CAPTURE_FROM = "TAPOUT_CAPTURE_FROM"

# How much transcript text to feed the summarizer (chars). Keeps the prompt
# bounded on very long sessions; the tail carries the most recent state.
TRANSCRIPT_BUDGET = 60_000


class CaptureError(Exception):
    """Capture could not complete."""


# --------------------------------------------------------------------------
# hook plumbing
# --------------------------------------------------------------------------

def read_hook_stdin() -> dict:
    """Parse the PreCompact / SessionEnd JSON Claude Code writes to stdin."""
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def condense_transcript(path: Path) -> str:
    """Flatten a Claude Code transcript JSONL into role-tagged plain text."""
    if not path.exists():
        raise CaptureError(f"transcript not found: {path}")
    chunks: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        role, text = _extract_message(obj)
        if text:
            chunks.append(f"{role}: {text}")
    joined = "\n".join(chunks)
    if len(joined) > TRANSCRIPT_BUDGET:
        joined = "...(earlier turns elided)...\n" + joined[-TRANSCRIPT_BUDGET:]
    return joined


def _extract_message(obj: dict) -> tuple[str, str]:
    """Best-effort (role, text) from one transcript record across CC formats."""
    msg = obj.get("message", obj)
    role = msg.get("role") or obj.get("type") or "unknown"
    content = msg.get("content", "")
    if isinstance(content, str):
        return role, content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif block.get("type") == "tool_result":
                    parts.append(str(block.get("content", ""))[:500])
            elif isinstance(block, str):
                parts.append(block)
        return role, " ".join(p for p in parts if p)
    return role, ""


# --------------------------------------------------------------------------
# summarization
# --------------------------------------------------------------------------

def summarize_via_claude(prompt_text: str, timeout: float = 180) -> str:
    """Ask a fresh headless `claude -p` to emit the task-state JSON block."""
    exe = resolve_executable("claude")
    if not exe:
        raise CaptureError("claude not found on PATH; cannot summarize the session.")
    try:
        proc = subprocess.run(
            [exe, "-p"],
            input=prompt_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise CaptureError(f"summarizer timed out after {timeout}s") from exc
    except OSError as exc:
        raise CaptureError(f"could not run claude: {exc}") from exc
    if proc.returncode != 0:
        raise CaptureError(f"claude exited {proc.returncode}: {(proc.stderr or '').strip()[:300]}")
    return proc.stdout


def build_summarization_input(transcript_text: str) -> str:
    return (
        f"{SUMMARIZATION_PROMPT}\n\n"
        "The session transcript to summarize follows (most recent turns last):\n"
        "--- BEGIN TRANSCRIPT ---\n"
        f"{transcript_text}\n"
        "--- END TRANSCRIPT ---\n"
    )


# --------------------------------------------------------------------------
# atomic artifact write
# --------------------------------------------------------------------------

def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)  # atomic on POSIX and Windows
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def write_artifacts_atomic(state: TaskState, repo: Path) -> tuple[Path, Path]:
    paths = tapout_paths(repo)
    _atomic_write(paths["state"], state.to_json())
    _atomic_write(paths["handoff"], render_markdown(state))
    return paths["state"], paths["handoff"]


def log(repo: Path, message: str) -> None:
    path = tapout_paths(repo)["dir"] / "capture.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(f"{now_iso()} {message}\n")


# --------------------------------------------------------------------------
# top-level capture
# --------------------------------------------------------------------------

def run_capture(
    repo: Path,
    agent: str,
    transcript_path: Optional[Path],
    force: bool,
) -> Optional[TaskState]:
    """Capture the session into artifacts. Returns the state, or None if skipped.

    Silent: never prompts. All progress goes to .tapout/capture.log. Raises
    CaptureError only on genuine failure (which the caller logs too).
    """
    state_path = tapout_paths(repo)["state"]
    if state_path.exists() and not force:
        log(repo, "SKIP existing task-state.json present (re-run with --force to overwrite)")
        return None

    # Obtain the agent's task-state JSON reply.
    override = os.environ.get(ENV_CAPTURE_FROM)
    if override:
        reply = Path(override).read_text(encoding="utf-8")
        log(repo, f"summary source: {ENV_CAPTURE_FROM}={override}")
    else:
        if transcript_path is None:
            raise CaptureError("no transcript path and no summary override available")
        transcript_text = condense_transcript(transcript_path)
        reply = summarize_via_claude(build_summarization_input(transcript_text))
        log(repo, f"summarized transcript {transcript_path} ({len(transcript_text)} chars)")

    state = parse_task_state(reply)
    # The capture stamps who/when — the transcript reflects the source agent now.
    state.created_at = now_iso()
    state.source_agent = agent

    state_path, handoff_path = write_artifacts_atomic(state, repo)
    log(repo, f"OK wrote {state_path.name} + {handoff_path.name} ('{state.task_title}')")
    return state
