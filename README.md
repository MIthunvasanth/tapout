# tapout

Your coding agent tapped out? Tag in the next one.

Hit your Claude Code limit mid-task — `tap codex` and it picks up exactly where you left off. Same task, same plan, same progress. No re-explaining.

## How it works

1. `tap out` — prints a summarization prompt (also copied to your clipboard). Paste it into your current agent; it replies with a task-state JSON block.
2. `tap out --from <file>` — ingests that reply and writes two artifacts: `HANDOFF.md` (human/agent readable) and `.tapout/task-state.json` (schema, git-friendly).
3. `tap <agent>` — opens the target agent in the repo, seeded with the handoff. Works for `claude`, `codex`, `gemini`, `cursor`, and any agent you add yourself.

Run `tap scan` to see which agents are installed on this machine.

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

## First run in a new folder

The first `tap <agent>` in a repo may hit the *receiving* agent's own setup: trust/workspace-approval dialogs, IDE-connect prompts, or a sign-in. That's the target agent, not tapout. Get through those once — subsequent handoffs into that folder go straight to work.

## Windows notes

- npm-installed agents ship both an extensionless shim and a real `.cmd`; tapout resolves the `.cmd` (PATHEXT-aware) so launches don't fail with WinError 193.
- When the resolved launcher is a `.cmd`/`.bat` and the handoff contains shell metacharacters (`& | % " ^ < >`), tapout will not pass the prompt as a command-line argument (cmd.exe would re-parse it). It copies the prompt to your clipboard and opens the agent — paste to begin.

---

_Early alpha (0.1.0.dev0). Manual handoff only; hooks and auto-capture are planned._
