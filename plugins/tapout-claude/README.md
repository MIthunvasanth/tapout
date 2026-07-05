# tapout-claude

Claude Code plugin for [tapout](https://pypi.org/project/tapout/). Makes capture zero-effort: your task state is always fresh, so when Claude hits a usage limit you can `tap codex` (or gemini/cursor) and keep going.

## What it does

- **PreCompact + SessionEnd hooks** — before Claude compacts context or the session ends, tapout summarizes the session into `HANDOFF.md` + `.tapout/task-state.json`. No manual `tap out` needed.
- **`/tapout:pause`** — capture on demand from inside a live session.
- **Statusline** — a `tap: 84%` indicator showing how much of your 5-hour window is used (see setup below).

## Requirements

tapout must be runnable on your machine. Either:
- `pip install tapout` (or `pipx install tapout`), or
- have `uvx` available (the hook falls back to `uvx tapout`).

The hook launcher prefers your installed package (`python -m tapout`) and falls back to `uvx tapout`.

## Install

From this repo (marketplace):

```
/plugin marketplace add <path-or-git-url-to-this-repo>
/plugin install tapout-claude@tapout
```

Or load locally for one session (no install):

```
claude --plugin-dir ./plugins/tapout-claude
```

## Statusline setup (optional)

Claude Code only exposes usage/rate-limit data to a statusline command, so add this to `~/.claude/settings.json`:

```json
{
  "statusLine": { "type": "command", "command": "tap statusline", "refreshInterval": 5 }
}
```

`tap statusline` prints `tap: N%` and also records the reading to `~/.tapout/claude-status.json`, which `tap watch` reads to show a live monitor and to hint `run: tap codex` when the window is spent.

## Notes

- Usage limits surface as API errors, not a special exit code — the monitor is driven by the statusline's `rate_limits.five_hour.used_percentage` (Pro/Max; appears after the first API response).
- Auto-capture uses a fresh headless `claude -p` to summarize the transcript. On a hard limit that call may itself fail; `/tapout:pause` (which summarizes from the live session directly) is the reliable manual fallback.
