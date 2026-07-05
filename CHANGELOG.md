# Changelog

All notable changes to tapout are documented here. This project adheres to [Semantic Versioning](https://semver.org/).

## 0.1.0

First real release. (PyPI 0.0.1 was a name-reservation placeholder.)

### Added
- **Cross-agent handoff loop** — `tap out` captures the current task into `HANDOFF.md` + `.tapout/task-state.json`, and `tap <agent>` (or `tap resume <agent>`) resumes it in another agent. Pydantic task-state schema (v1); git-friendly artifacts.
- **Registry-driven agents** — bundled `agents.toml` plus a user overlay at `~/.tapout/agents.toml`. Adding or overriding an agent needs zero package changes; no hardcoded per-agent branches. Ships claude, codex, gemini, cursor, windsurf, aider, copilot. `tap scan` lists what's installed; `tap scan --discover` prints TOML skeletons for unknown tools on PATH.
- **`tap capture --agent claude --session-transcript <path>`** — summarize a real Claude Code JSONL transcript into the artifacts (atomic writes, `.tapout/capture.log`, `--force`/warn-on-stale).
- **`tap statusline` + `tap watch`** — live usage awareness. `tap statusline` reads Claude Code's statusline JSON, prints `tap: N%`, and records `~/.tapout/claude-status.json`; `tap watch` reads that for a live percentage and prints `run: tap codex` at 100%.
- **Claude Code plugin** scaffold under `plugins/tapout-claude/` — PreCompact + SessionEnd hooks and a `/tapout:pause` slash command.

### Windows
- PATHEXT-aware executable resolution — resolves npm `.cmd` shims instead of the extensionless bash shim (fixes WinError 193).
- UTF-8 stdio so box-drawing/dash glyphs never crash on cp1252 consoles.
- **BatBadBut guard** — refuses to pass a prompt containing shell metacharacters (`& | % " ^ < >`) as an argv to a `.cmd`/`.bat` launcher.
- **stdin prompt delivery** (`prompt_delivery = "stdin"`) — codex/gemini/claude receive the prompt via stdin, bypassing cmd.exe argument parsing entirely; hostile characters arrive byte-intact (regression tested).

### Known limitations
- The Claude Code plugin installs and the statusline works, but the SessionEnd hook auto-trigger and the `/tapout:pause` slash command do not fire on some Claude Code runtimes (likely plugin-manifest schema drift). Workaround: run `tap capture --agent claude --session-transcript <path>` — the same capture logic the hook invokes. Tracked in [#1](https://github.com/MIthunvasanth/tapout/issues/1).

### Tests
- 54 tests (schema, rendering, registry/overlay, resume delivery incl. hostile-char stdin round-trip, capture, monitor, CLI).
