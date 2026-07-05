"""Handoff schema, rendering, capture, and ingest."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field, ValidationError

SCHEMA_VERSION = 1

TAPOUT_DIR = ".tapout"
STATE_FILENAME = "task-state.json"
HANDOFF_FILENAME = "HANDOFF.md"
HISTORY_FILENAME = "history.jsonl"


class StepStatus(str, Enum):
    done = "done"
    in_progress = "in_progress"
    todo = "todo"


class PlanStep(BaseModel):
    step: str = Field(..., description="What this step accomplishes.")
    status: StepStatus = Field(..., description="One of: done, in_progress, todo.")


class TaskState(BaseModel):
    schema_version: int = Field(SCHEMA_VERSION)
    created_at: str = Field(..., description="ISO-8601 UTC timestamp.")
    source_agent: str = Field(..., description="Agent that produced this state.")
    task_title: str
    goal: str
    plan: list[PlanStep] = Field(default_factory=list)
    decisions: list[str] = Field(default_factory=list)
    files_touched: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    commands_to_verify: list[str] = Field(default_factory=list)

    def to_json(self) -> str:
        """Git-friendly: pretty, stable key order, utf-8 safe."""
        data = self.model_dump(mode="json")
        return json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


# --------------------------------------------------------------------------
# JSON extraction + validation
# --------------------------------------------------------------------------

_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)


def extract_json_block(text: str) -> str:
    """Pull the JSON out of a fenced ```json block, else assume whole text is JSON."""
    matches = _FENCE_RE.findall(text)
    for candidate in matches:
        candidate = candidate.strip()
        if candidate.startswith("{"):
            return candidate
    # No fence — maybe the whole payload is raw JSON.
    stripped = text.strip()
    if stripped.startswith("{"):
        return stripped
    raise ValueError(
        "No JSON found. Expected a single ```json fenced block containing the task state."
    )


def parse_task_state(text: str) -> TaskState:
    """Extract + validate. Raises ValueError with a clear message on any problem."""
    block = extract_json_block(text)
    try:
        data = json.loads(block)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Task state is not valid JSON: {exc}") from exc
    try:
        return TaskState.model_validate(data)
    except ValidationError as exc:
        raise ValueError(_format_validation_error(exc)) from exc


def _format_validation_error(exc: ValidationError) -> str:
    lines = ["Task state JSON does not match the schema:"]
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "(root)"
        lines.append(f"  - {loc}: {err['msg']}")
    lines.append(
        "Fix the flagged fields. Statuses must be one of: done, in_progress, todo."
    )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# HANDOFF.md rendering
# --------------------------------------------------------------------------

_STATUS_MARK = {
    StepStatus.done: "[x]",
    StepStatus.in_progress: "[~]",
    StepStatus.todo: "[ ]",
}


def render_markdown(state: TaskState) -> str:
    def bullets(items: list[str]) -> str:
        if not items:
            return "_(none)_\n"
        return "".join(f"- {i}\n" for i in items)

    lines: list[str] = []
    lines.append(f"# Handoff: {state.task_title}\n")
    lines.append(
        f"_Captured from **{state.source_agent}** at {state.created_at} "
        f"(schema v{state.schema_version})._\n"
    )
    lines.append("\n## Goal\n\n" + state.goal + "\n")

    lines.append("\n## Plan\n\n")
    if state.plan:
        for step in state.plan:
            mark = _STATUS_MARK[step.status]
            lines.append(f"- {mark} {step.step}  _({step.status.value})_\n")
    else:
        lines.append("_(none)_\n")

    lines.append("\n## Decisions\n\n" + bullets(state.decisions))
    lines.append("\n## Files touched\n\n" + bullets(state.files_touched))
    lines.append("\n## Next steps\n\n" + bullets(state.next_steps))
    lines.append("\n## Blockers\n\n" + bullets(state.blockers))

    lines.append("\n## Commands to verify state\n\n")
    if state.commands_to_verify:
        lines.append("```bash\n")
        for cmd in state.commands_to_verify:
            lines.append(cmd + "\n")
        lines.append("```\n")
    else:
        lines.append("_(none)_\n")

    lines.append("\n## Instructions for the next agent\n\n")
    lines.append(
        "You are taking over this task from another AI coding agent. Do this in order:\n\n"
        "1. Read the **Goal** and **Plan** above so you understand the whole task and what is already done.\n"
        "2. Run the **Commands to verify state** to confirm the repo is really in the state described "
        "(build/tests/git). If reality disagrees with the plan, trust reality and reconcile.\n"
        "3. Review **Decisions** and **Files touched** so you keep prior choices and don't redo finished work.\n"
        "4. Continue from **Next steps**, clearing **Blockers** first if any block progress.\n"
        "5. Keep going until the Goal is met. Do not restart from scratch.\n"
    )
    return "".join(lines)


# --------------------------------------------------------------------------
# Filesystem write
# --------------------------------------------------------------------------

def tapout_paths(repo: Path) -> dict[str, Path]:
    d = repo / TAPOUT_DIR
    return {
        "dir": d,
        "state": d / STATE_FILENAME,
        "history": d / HISTORY_FILENAME,
        "handoff": repo / HANDOFF_FILENAME,
    }


def write_artifacts(state: TaskState, repo: Path) -> tuple[Path, Path]:
    """Write .tapout/task-state.json and HANDOFF.md. Returns (state_path, handoff_path)."""
    paths = tapout_paths(repo)
    paths["dir"].mkdir(parents=True, exist_ok=True)
    paths["state"].write_text(state.to_json(), encoding="utf-8")
    paths["handoff"].write_text(render_markdown(state), encoding="utf-8")
    return paths["state"], paths["handoff"]


def load_state(repo: Path) -> Optional[TaskState]:
    path = tapout_paths(repo)["state"]
    if not path.exists():
        return None
    return parse_task_state(path.read_text(encoding="utf-8"))


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# --------------------------------------------------------------------------
# The summarization prompt — crown jewel
# --------------------------------------------------------------------------

SUMMARIZATION_PROMPT = f"""\
You are about to hand this task off to a different AI coding agent because I have \
hit a usage limit. Produce a complete task-state snapshot so the next agent can \
continue with zero context loss.

OUTPUT RULES — follow exactly:
- Reply with ONE fenced code block tagged ```json and NOTHING else. No prose before \
or after the block.
- The block must be a single valid JSON object matching the schema below.
- Include EVERY field, even when empty (use [] or "").
- Every plan step "status" MUST be exactly one of: "done", "in_progress", "todo".
- Base every field on what has ACTUALLY happened in this session — real files, real \
decisions, real commands. Do not invent.
- "created_at" must be an ISO-8601 UTC timestamp (e.g. 2026-07-05T14:03:00+00:00).
- "source_agent" is the agent you are (e.g. "claude", "codex", "gemini").
- "commands_to_verify" are shell commands the next agent can run to confirm the repo \
state (build, tests, git status) — include the ones that matter for this task.
- "next_steps" are concrete, ordered, actionable — the first thing the next agent \
should do, then the next, in plain imperative sentences.

SCHEMA (schema_version {SCHEMA_VERSION}):
```json
{{
  "schema_version": {SCHEMA_VERSION},
  "created_at": "<ISO-8601 UTC>",
  "source_agent": "<your agent name>",
  "task_title": "<short title of the overall task>",
  "goal": "<one-paragraph description of the end state we are driving toward>",
  "plan": [
    {{ "step": "<what this step does>", "status": "done|in_progress|todo" }}
  ],
  "decisions": ["<key decision made and why>"],
  "files_touched": ["<path/to/file — what changed>"],
  "next_steps": ["<first concrete action for the next agent>", "<then this>"],
  "blockers": ["<anything blocking progress, or leave []>"],
  "commands_to_verify": ["<shell command to confirm state>"]
}}
```

Now emit the JSON block for THIS task.
"""
