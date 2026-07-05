"""Resume a captured task in a target agent."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .detect import resolve_executable
from .handoff import (
    HANDOFF_FILENAME,
    TaskState,
    load_state,
    now_iso,
    render_markdown,
    tapout_paths,
)

# Agents we know how to launch with an initial prompt.
CLI_AGENTS = {"claude", "codex", "gemini"}
GUI_AGENTS = {"cursor"}
KNOWN_AGENTS = CLI_AGENTS | GUI_AGENTS


class ResumeError(Exception):
    """Friendly, user-facing resume failure."""


def copy_to_clipboard(text: str) -> bool:
    """Copy text to clipboard. Returns False (never raises) if unavailable."""
    try:
        import pyperclip  # type: ignore
    except Exception:
        return False
    try:
        pyperclip.copy(text)
        return True
    except Exception:
        return False


def build_opening_prompt(handoff_md: str) -> str:
    return (
        "You are continuing a task handed off from another AI coding agent that hit a "
        "usage limit. The complete handoff document follows. Follow its "
        '"Instructions for the next agent" section: read the goal and plan, run the '
        "verify commands to confirm the real repo state, then continue from the next "
        "steps. Do not restart the task from scratch.\n\n"
        "--- BEGIN HANDOFF ---\n"
        f"{handoff_md}\n"
        "--- END HANDOFF ---\n"
    )


def build_launch_argv(agent: str, exe: str, opening: str, repo: Path) -> list[str]:
    """Argv to launch `agent` in `repo` with the opening prompt as initial input."""
    if agent == "claude":
        # Claude Code takes the initial prompt as a positional argument.
        return [exe, opening]
    if agent == "codex":
        # Codex CLI takes the initial prompt as a positional argument.
        return [exe, opening]
    if agent == "gemini":
        # Gemini CLI: -i keeps the session interactive after seeding the prompt.
        return [exe, "-i", opening]
    if agent == "cursor":
        # Cursor has no reliable prompt-injection flag: just open the folder.
        return [exe, str(repo)]
    raise ResumeError(f"Unknown agent '{agent}'.")


def record_history(repo: Path, from_agent: str, to_agent: str, task_title: str) -> None:
    path = tapout_paths(repo)["history"]
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "from_agent": from_agent,
        "to_agent": to_agent,
        "timestamp": now_iso(),
        "task_title": task_title,
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


@dataclass
class ResumePlan:
    agent: str
    exe: str
    argv: list[str]
    opening: str
    clipboard_ok: bool
    is_gui: bool
    state: TaskState


def prepare_resume(repo: Path, agent: str) -> ResumePlan:
    """Validate everything and build the launch plan. Raises ResumeError on any issue."""
    if agent not in KNOWN_AGENTS:
        raise ResumeError(
            f"Unknown agent '{agent}'. Known: {', '.join(sorted(KNOWN_AGENTS))}."
        )

    state = load_state(repo)
    if state is None:
        raise ResumeError(
            "No handoff found. Run `tap out` first to capture the task state, then "
            "`tap out --from <file>` to write the artifacts."
        )

    handoff_path = repo / HANDOFF_FILENAME
    if handoff_path.exists():
        handoff_md = handoff_path.read_text(encoding="utf-8")
    else:
        handoff_md = render_markdown(state)

    exe = resolve_executable(agent)
    if not exe:
        raise ResumeError(
            f"{agent} is not installed (not found on PATH). Run `tap scan` to see what "
            "is available on this machine."
        )

    opening = build_opening_prompt(handoff_md)
    argv = build_launch_argv(agent, exe, opening, repo)
    clipboard_ok = copy_to_clipboard(opening)

    return ResumePlan(
        agent=agent,
        exe=exe,
        argv=argv,
        opening=opening,
        clipboard_ok=clipboard_ok,
        is_gui=agent in GUI_AGENTS,
        state=state,
    )


def launch(plan: ResumePlan, repo: Path) -> subprocess.Popen:
    """Actually spawn the target agent in `repo`."""
    return subprocess.Popen(plan.argv, cwd=str(repo), shell=False)
