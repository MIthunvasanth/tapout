# Changelog

All notable changes to tapout are documented here. This project adheres to [Semantic Versioning](https://semver.org/).

## 0.1.1

Closes [#1](https://github.com/MIthunvasanth/tapout/issues/1): the Claude Code plugin's hooks and slash command now fire.

### Fixed
- **Plugin hooks never loaded** — the manifest declared `"hooks": "./hooks/hooks.json"`, which the runtime already auto-loads, producing `Hook load failed: Duplicate hooks file detected` and failing the whole hook set. Removed `hooks` and `commands` from the manifest; both are auto-discovered.
- **`/tapout:pause` was "Unknown skill"** — slash commands namespace by plugin name, and the plugin was named `tapout-claude` (so the command was `/tapout-claude:pause`). Renamed the plugin to **`tapout`** → the command is now `/tapout:pause`. Install is now `tapout@tapout`.
- Dropped `displayName` (rejected by the plugin validator) and trimmed `marketplace.json` to the accepted keys (`metadata.description`, not root `description`/`version`).

### Added
- **`tap capture --agent claude` with no `--session-transcript`** auto-picks the newest top-level session transcript for the current repo's Claude Code project dir (`~/.claude/projects/<mangled-cwd>/*.jsonl`), skipping `subagents/` transcripts. Clear error listing candidates when none matches. This is what the hooks call, and it closes the Slice-2 mtime caveat (which could grab a subagent transcript).

## 0.1.0

First real release. (PyPI 0.0.1 was a name-reservation placeholder.)

### Added
- **Cross-agent handoff loop** — `tap out` captures the current task into `HANDOFF.md` + `.tapout/task-state.json`, and `tap <agent>` (or `tap resume <agent>`) resumes it in another agent. Pydantic task-state schema (v1); git-friendly artifacts.
- **Registry-driven agents** — bundled `agents.toml` plus a user overlay at `~/.tapout/agents.toml`. Adding or overriding an agent needs zero package changes; no hardcoded per-agent branches. Ships claude, codex, gemini, cursor, windsurf, aider, copilot. `tap scan` lists what's installed; `tap scan --discover` prints TOML skeletons for unknown tools on PATH.
- **`tap capture --agent claude --session-transcript <path>`** — summarize a real Claude Code JSONL transcript into the artifacts (atomic writes, `.tapout/capture.log`, `--force`/warn-on-stale).
- **`tap statusline` + `tap watch`** — live usage awareness. `tap statusline` reads Claude Code's statusline JSON, prints `tap: N%`, and records `~/.tapout/claude-status.json`; `tap watch` reads that for a live percentage and prints `run: tap codex` at 100%.
- **Claude Code plugin** scaffold under `plugins/tapout/` — PreCompact + SessionEnd hooks and a `/tapout:pause` slash command.

### Windows
- PATHEXT-aware executable resolution — resolves npm `.cmd` shims instead of the extensionless bash shim (fixes WinError 193).
- UTF-8 stdio so box-drawing/dash glyphs never crash on cp1252 consoles.
- **BatBadBut guard** — refuses to pass a prompt containing shell metacharacters (`& | % " ^ < >`) as an argv to a `.cmd`/`.bat` launcher.
- **stdin prompt delivery** (`prompt_delivery = "stdin"`) — codex/gemini/claude receive the prompt via stdin, bypassing cmd.exe argument parsing entirely; hostile characters arrive byte-intact (regression tested).

### Tests
- 54 tests (schema, rendering, registry/overlay, resume delivery incl. hostile-char stdin round-trip, capture, monitor, CLI).
