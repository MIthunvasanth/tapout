# Changelog

All notable changes to tapout are documented here. This project adheres to [Semantic Versioning](https://semver.org/).

## 0.1.1

Auto-capture on `/exit` now works reliably on Windows, and the resulting handoff is high-quality enough that the receiving agent can genuinely continue the work. Closes [#1](https://github.com/MIthunvasanth/tapout/issues/1).

### Fixed
- **SessionEnd hook is now in-process** — the previous subprocess parent/child dance was killed by Claude Code's session teardown on Windows before the child could write artifacts (silent failure, no `.tapout/`, no error). Hook now imports `tapout.capture` directly; artifacts land in <1s. Subprocess fallback retained for cases where in-process import fails; logs a one-line reason to `.tapout/capture.log`.
- **Plugin hooks never loaded** — manifest declared `"hooks": "./hooks/hooks.json"`, which the runtime auto-loads, producing `Hook load failed: Duplicate hooks file detected` and failing the whole hook set. Removed `hooks` and `commands` from the manifest.
- **`/tapout:pause` was "Unknown skill"** — slash commands namespace by plugin name; the plugin was `tapout-claude`. Renamed to `tapout`; the command is now `/tapout:pause` and install is `tapout@tapout`. Dropped `displayName` (rejected by validator) and trimmed `marketplace.json`.
- **Heuristic HANDOFF.md was unusable** — the file-touch extractor was capturing version strings (`2.41.5`, `0.4.2`), platform strings (`Windows-11-10.0.26200`, `pytest-9.0.2`), and bare identifiers (`python.exe`) as filenames, producing garbage `Files touched` lists that confused receiving agents. Filter now rejects version-shaped tokens, requires path separator or source-code extension, and normalizes to native path style (no more mixed `/c/Users/`).
- **`tap capture` CLI failed silently** — a bare `except` swallowed exceptions before they hit stderr, producing `rc=1` with zero stdout/stderr. CLI now writes a one-line `tapout: fatal: ...` diagnostic on any error path.
- **Config files silently defaulted `refine_on_capture` to on** — `~/.tapout/config.toml` and user `agents.toml` were read as strict `utf-8`, and PowerShell/Notepad's default UTF-8 BOM made `tomllib` raise; the broad `except` returned the default. Both readers now use `utf-8-sig` and log parse failures.

### Added
- **`tap capture --agent claude` with no `--session-transcript`** — auto-picks the newest top-level Claude Code session for the current repo's project dir (`~/.claude/projects/<mangled-cwd>/*.jsonl`, skipping `subagents/`). Clear error listing candidates when none matches. Closes the Slice-2 mtime caveat.
- **Background LLM refinement of HANDOFF.md** — on SessionEnd, the heuristic version writes immediately, then a detached background process runs `claude -p` and overwrites the handoff with a proper LLM summary (~20s later, opt-out via `refine_on_capture = false` in `~/.tapout/config.toml`). Detached with `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP` on Windows / `start_new_session=True` on POSIX — survives session teardown. Silent skip if `claude` binary is missing or errors; capture.log always records the reason.
- **`tap refine --transcript <path> --repo <path>`** — manually re-refine a stale handoff.
- **Subagent recursion guard** — the plugin is enabled machine-wide, so subagents (themselves Claude Code sessions) would fire SessionEnd and overwrite the outer session's handoff. `CLAUDE_CODE_CHILD_SESSION` env var skips capture/refine for subagent sessions.

### Tests
- 84 (up from 54).

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
