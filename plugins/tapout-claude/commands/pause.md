---
description: Capture the current task state now so you can hand off to another agent (tapout)
disable-model-invocation: true
---

The user wants to capture the current session's task state so they can resume it in another AI coding agent (via tapout). Do this silently and without asking questions:

1. Build a task-state JSON object describing THIS session's work, following tapout's schema exactly (schema_version 1). Fields:
   - `schema_version`: 1
   - `created_at`: current time, ISO-8601 UTC
   - `source_agent`: "claude"
   - `task_title`: short title of the overall task
   - `goal`: one paragraph on the end state we're driving toward
   - `plan`: array of `{ "step": "...", "status": "done" | "in_progress" | "todo" }`
   - `decisions`: array of key decisions made and why
   - `files_touched`: array of "path — what changed"
   - `next_steps`: ordered, concrete actions the next agent should take first
   - `blockers`: array (or empty)
   - `commands_to_verify`: shell commands to confirm the repo state
   Base every field on the REAL work done in this session — do not invent.

2. Write that JSON (only the JSON, no prose) to `.tapout/pause-input.json`.

3. Run this exact command to persist the artifacts and log the capture:
   ```
   TAPOUT_CAPTURE_FROM=.tapout/pause-input.json python -m tapout capture --agent claude --force
   ```
   (If `python -m tapout` is unavailable, use `uvx tapout` in its place.)

4. Confirm that `HANDOFF.md` and `.tapout/task-state.json` now exist and tell the user they can run `tap codex` (or `tap gemini` / `tap cursor`) to continue elsewhere.
