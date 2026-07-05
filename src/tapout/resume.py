"""Resume a captured task in a target agent — registry + strategy driven."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .detect import resolve_binary
from .handoff import (
    HANDOFF_FILENAME,
    TaskState,
    load_state,
    now_iso,
    render_markdown,
    tapout_paths,
)
from .registry import AgentEntry, get_entry, load_registry

# Characters cmd.exe parses when it launches a .cmd/.bat wrapper. Passing a
# prompt containing these straight through would let cmd.exe expand/inject
# them (the "BatBadBut" class of bug). If present, we refuse the direct-arg
# path and fall back to clipboard.
BATBADBUT_CHARS = set('&|%"^<>')


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
        "'Instructions for the next agent' section: read the goal and plan, run the "
        "verify commands to confirm the real repo state, then continue from the next "
        "steps. Do not restart the task from scratch.\n\n"
        "--- BEGIN HANDOFF ---\n"
        f"{handoff_md}\n"
        "--- END HANDOFF ---\n"
    )


def render_template(template: list[str], *, binary: str, prompt: str, cwd: str) -> list[str]:
    """Substitute {binary} {prompt} {cwd} placeholders in an argv template."""
    subs = {"{binary}": binary, "{prompt}": prompt, "{cwd}": cwd}
    argv: list[str] = []
    for tok in template:
        for placeholder, value in subs.items():
            tok = tok.replace(placeholder, value)
        argv.append(tok)
    return argv


def is_batch_wrapper(exe: str) -> bool:
    return os.path.splitext(exe)[1].lower() in {".cmd", ".bat"}


def batbadbut_risk(exe: str, prompt: str) -> bool:
    """True if launching `exe` with `prompt` as an arg risks cmd.exe injection."""
    return is_batch_wrapper(exe) and any(c in BATBADBUT_CHARS for c in prompt)


def record_history(
    repo: Path,
    from_agent: str,
    to_agent: str,
    task_title: str,
    event: str = "resume",
) -> None:
    path = tapout_paths(repo)["history"]
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "event": event,
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
    entry: AgentEntry
    effective_style: str          # resume_style after any guard downgrade
    exe: Optional[str]            # resolved binary, or None for clipboard_only
    argv: Optional[list[str]]     # what launch() spawns, or None if no launch
    opening: str
    clipboard_ok: bool
    state: TaskState
    guard_reason: Optional[str]   # set when downgraded away from direct-arg launch


def prepare_resume(repo: Path, agent: str) -> ResumePlan:
    """Validate everything and build the launch plan. Raises ResumeError on any issue."""
    entry = get_entry(agent)
    if entry is None:
        known = ", ".join(sorted(load_registry().keys()))
        raise ResumeError(f"Unknown agent '{agent}'. Known: {known}.")

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
    opening = build_opening_prompt(handoff_md)
    clipboard_ok = copy_to_clipboard(opening)

    style = entry.resume_style
    guard_reason: Optional[str] = None
    exe: Optional[str] = None
    argv: Optional[list[str]] = None
    cwd = str(repo)

    if style in ("cli_prompt", "clipboard_gui"):
        exe = resolve_binary(entry)
        if not exe:
            names = " / ".join(entry.binaries) or agent
            raise ResumeError(
                f"{entry.display_name} is not installed ({names} not found on PATH). "
                "Run `tap scan` to see what is available on this machine."
            )

    if style == "cli_prompt":
        if batbadbut_risk(exe, opening):
            # Downgrade: launch the wrapper WITHOUT the prompt as an argument.
            guard_reason = (
                f"resolved launcher is a {os.path.splitext(exe)[1]} wrapper (parsed by "
                "cmd.exe) and the opening prompt contains shell metacharacters "
                "(&|%\"^<>). Passing it as an argument risks command injection, so "
                "tapout falls back to clipboard: the prompt is on your clipboard — "
                "paste it into the agent once it opens."
            )
            style = "clipboard_gui"
            argv = [exe]  # bare launch in cwd
        else:
            argv = render_template(entry.launch_template, binary=exe, prompt=opening, cwd=cwd)
    elif style == "clipboard_gui":
        argv = render_template(entry.launch_template, binary=exe, prompt=opening, cwd=cwd)
    # clipboard_only: no exe, no argv.

    return ResumePlan(
        agent=agent,
        entry=entry,
        effective_style=style,
        exe=exe,
        argv=argv,
        opening=opening,
        clipboard_ok=clipboard_ok,
        state=state,
        guard_reason=guard_reason,
    )


def launch(plan: ResumePlan, repo: Path) -> subprocess.Popen:
    """Spawn the target agent in `repo`. Only valid when plan.argv is set."""
    if not plan.argv:
        raise ResumeError(f"{plan.agent} has no launch command (clipboard-only).")
    return subprocess.Popen(plan.argv, cwd=str(repo), shell=False)
