# Changelog

All notable changes to tapout are documented here. This project adheres to [Semantic Versioning](https://semver.org/).

## 0.1.1

Auto-capture on `/exit` now works reliably on Windows, and the resulting handoff is high-quality enough that the receiving agent can genuinely continue the work. Closes [#1](https://github.com/MIthunvasanth/tapout/issues/1).

### Fixed
- **SessionEnd hook is now in-process** — the previous subprocess parent/child dance was killed by Claude Code's session teardown on Windows before the child could write artifacts (silent failure, no `.tapout/`, no error). Hook now imports `tapout.capture` directly; artifacts land in <1s. Subprocess fallback retained for cases where in-process import fails; logs a one-line reason to `.tapout/capture.log`.
- **Plugin hooks never loaded** — the manifest declared `"hooks": "./hooks/hooks.json"`, which the runtime already auto-loads, producing `Hook load failed: Duplicate hooks file detected` and failing the whole hook set. Removed `hooks` and `commands` from the manifest; both are auto-discovered.
- **`/tapout:pause` was "Unknown skill"** — slash commands namespace by plugin name, and the plugin was named `tapout-claude` (so the command was `/tapout-claude:pause`). Renamed the plugin to **`tapout`** → the command is now `/tapout:pause`. Install is now `tapout@tapout`.
- Dropped `displayName` (rejected by the plugin validator) and trimmed `marketplace.json` to the accepted keys (`metadata.description`, not root `description`/`version`).
- **Heuristic `files_touched` was unusable garbage** — version strings (`2.41.5`, `0.4.2`), platform identifiers (`Windows-11-10.0.26200`, `python.exe`, `pluggy-1.6.0`) and git-bash-mangled paths (`/c/Users/...`) leaked in instead of the files actually touched. Filter now rejects version strings and bare non-source identifiers, requires a path separator or a real source extension, and normalizes to native path style (`C:\...` on Windows).
- **Config files with a UTF-8 BOM silently disabled the opt-out** — `refine_on_capture_enabled()` and the `agents.toml` user overlay read config as strict `utf-8`; a BOM (the default for PowerShell's `Out-File -Encoding utf8` and Notepad) made `tomllib` raise, and the broad exception handler silently fell back to "enabled" — the opt-out was invisibly never honored. Both now read `utf-8-sig`.
- **A crashing `tap` invocation could exit non-zero with empty stdout/stderr**, making failures undebuggable (this is what made an earlier diagnostic pass harder than it needed to be). The CLI entrypoint now always writes one diagnostic line to stderr before exiting non-zero.

### Added
- **`tap capture --agent claude` with no `--session-transcript`** auto-picks the newest top-level session transcript for the current repo's Claude Code project dir (`~/.claude/projects/<mangled-cwd>/*.jsonl`), skipping `subagents/` transcripts. Clear error listing candidates when none matches. This is what the hooks call, and it closes the Slice-2 mtime caveat (which could grab a subagent transcript).
- **Background LLM refinement** — the heuristic handoff is upgraded seconds later by a fully detached `tap refine` subprocess that re-summarizes the transcript with `claude -p` and overwrites `HANDOFF.md` + `task-state.json`. Detachment means it survives the parent session exiting (verified: killing the launcher immediately after spawn still lets refinement complete). Silent no-op if `claude` isn't on PATH or refinement fails — the heuristic handoff always stands as the floor. Opt out per-machine with `~/.tapout/config.toml`: `refine_on_capture = false`. New `tap refine --transcript <path> --repo <path>` command also works standalone to manually re-refine a stale handoff.
- **Subagent sessions are skipped** — capture and refinement no longer fire for a session's own subagents (detected via `CLAUDE_CODE_CHILD_SESSION`), which previously could overwrite the top-level handoff and spawn a background `claude -p` call per subagent.

### Tests
- 84 tests (schema, rendering, registry/overlay incl. BOM handling, resume delivery, capture incl. the real garbage-input regression, background refinement incl. detachment and failure paths, monitor, CLI).

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
