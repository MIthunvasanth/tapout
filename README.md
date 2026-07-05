# tapout

Your coding agent tapped out? Tag in the next one.

Hit your Claude Code limit mid-task — `tap codex` and it picks up exactly where you left off. Same task, same plan, same progress. No re-explaining.

## How it works

1. `tap out` — prints a summarization prompt (also copied to your clipboard). Paste it into your current agent; it replies with a task-state JSON block.
2. `tap out --from <file>` — ingests that reply and writes two artifacts: `HANDOFF.md` (human/agent readable) and `.tapout/task-state.json` (schema, git-friendly).
3. `tap <agent>` — opens the target agent in the repo, seeded with the handoff. Works for `claude`, `codex`, `gemini`, `cursor`, and any agent you add yourself.

Run `tap scan` to see which agents are installed on this machine.

## Zero-effort capture (Claude Code plugin)

Install the bundled Claude Code plugin so capture happens automatically — no manual `tap out`:

```
/plugin marketplace add <this-repo>
/plugin install tapout-claude@tapout
```

- **PreCompact + SessionEnd hooks** refresh `HANDOFF.md` + `.tapout/task-state.json` before Claude compacts context or the session ends.
- **`/tapout:pause`** captures on demand mid-session.
- **`tap watch`** shows a live usage line and prints `run: tap codex` when your 5-hour window is spent. Add the statusline to `~/.claude/settings.json`:
  ```json
  { "statusLine": { "type": "command", "command": "tap statusline", "refreshInterval": 5 } }
  ```
  `tap statusline` prints `tap: N%` and records state that `tap watch` reads.

`tap capture --agent claude` is the machine-invoked capture (used by the hooks): it summarizes the session transcript into the artifacts and writes them atomically.

## Auto-capture from a Claude Code session (works today)

After a Claude Code session, find the transcript and capture:

```powershell
# Find the latest session file for your project
dir $env:USERPROFILE\.claude\projects -Recurse -Filter *.jsonl `
  | Sort-Object LastWriteTime -Descending | Select-Object -First 1

python -m tapout capture --agent claude --session-transcript "<path from above>"
tap codex     # or tap cursor, tap gemini
```

> **Note:** the packaged Claude Code plugin includes a SessionEnd hook and `/tapout:pause`
> slash command intended to run capture automatically. On some Claude Code versions the
> plugin runtime does not wire these up (tracked in issue #TBD, Slice 2.5). Until that's
> fixed, use the command above — it's the exact same capture logic the hook would invoke.

## Zero-install via uvx

Once published, the hooks (and you) can run tapout without installing it: `uvx tapout <command>`. The plugin's hook launcher prefers your installed package and falls back to `uvx tapout`.

## Adding your own agent

Agents are data, not code. Drop a `~/.tapout/agents.toml` to add or override any agent — no package changes:

```toml
[mytool]
display_name = "My Tool"
binaries = ["mytool"]
config_dirs = ["~/.mytool"]
resume_style = "cli_prompt"                 # or clipboard_gui / clipboard_only
launch_template = ["{binary}", "{prompt}"]  # {binary} {prompt} {cwd} substituted
version_probe = { args = ["--version"], timeout_sec = 5 }
```

Then `tap resume mytool` (or `tap scan` to confirm it's detected). User keys win over the bundled registry.

Each entry can also set `prompt_delivery`: `"argv"` (default), `"stdin"` (pipe the prompt to the child — bypasses cmd.exe, the safe default for codex/gemini/claude), `"file:<flag>"` (write the prompt to a file, pass the path via `<flag>`), or `"clipboard"`.

> **Note:** Gemini CLI's free tier was deprecated in December 2025 — the registry flags this, and `tap scan` surfaces the note.

## First run in a new folder

The first `tap <agent>` in a repo may hit the *receiving* agent's own setup: trust/workspace-approval dialogs, IDE-connect prompts, or a sign-in. That's the target agent, not tapout. Get through those once — subsequent handoffs into that folder go straight to work.

## Windows notes

- npm-installed agents ship both an extensionless shim and a real `.cmd`; tapout resolves the `.cmd` (PATHEXT-aware) so launches don't fail with WinError 193.
- When an **argv-delivery** launcher is a `.cmd`/`.bat` and the handoff contains shell metacharacters (`& | % " ^ < >`), tapout will not pass the prompt as a command-line argument (cmd.exe would re-parse it — the "BatBadBut" class of bug). It falls back to clipboard. Agents using `stdin` or `file` delivery (codex, gemini, claude) avoid this entirely.
- PowerShell's `type` may mangle UTF-8; run `chcp 65001` first, or open `HANDOFF.md` in an editor.

---

_Early alpha (0.1.0.dev0). Manual + auto (Claude Code plugin) handoff; usage monitor via `tap watch`._
