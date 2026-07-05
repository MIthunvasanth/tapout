"""tapout CLI — `tap` / `tapout`."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

# Windows consoles often default to cp1252, which crashes on box-drawing / dash
# glyphs. Force utf-8 so output never raises UnicodeEncodeError.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

from . import __version__
from .capture import CaptureError, run_capture, run_hook_capture, run_refine
from .detect import detect_all, resolve_executable
from .handoff import (
    SUMMARIZATION_PROMPT,
    parse_task_state,
    tapout_paths,
    write_artifacts,
)
from .monitor import run_statusline, run_watch
from .registry import RegistryError, load_registry
from .resume import (
    ResumeError,
    copy_to_clipboard,
    launch,
    prepare_resume,
    record_history,
)

app = typer.Typer(
    name="tap",
    help="Limit-aware handoff between AI coding agents. Tap out, tag in the next one.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()
err = Console(stderr=True)


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"tapout {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        callback=_version_callback,
        is_eager=True,
        help="Show version and exit.",
    ),
) -> None:
    """tapout — hand off a task from one AI coding agent to another."""


# --------------------------------------------------------------------------
# tap scan
# --------------------------------------------------------------------------

@app.command()
def scan(
    discover: bool = typer.Option(
        False, "--discover", help="Scan PATH for unknown agent-like tools and print TOML skeletons."
    ),
) -> None:
    """Find installed AI coding agents on this machine."""
    try:
        results = detect_all()
    except RegistryError as exc:
        err.print(f"[red]Registry error:[/red] {exc}")
        raise typer.Exit(code=1)

    table = Table(title="tapout — detected agents")
    table.add_column("agent", style="bold")
    table.add_column("installed")
    table.add_column("version")
    table.add_column("resumable")

    notes: list[str] = []
    for r in results:
        installed = "[green]yes[/green]" if r.installed else "[dim]no[/dim]"
        details = []
        if r.which:
            details.append("cli")
        if r.config_found:
            details.append("config")
        if details and r.installed:
            installed += f" [dim]({', '.join(details)})[/dim]"
        version = r.version or "[dim]-[/dim]"
        resumable = r.entry.resumable_label() if r.installed else "[dim]-[/dim]"
        table.add_row(r.entry.display_name, installed, version, resumable)
        if r.entry.notes:
            notes.append(f"[yellow]note[/yellow] {r.entry.display_name}: {r.entry.notes}")

    console.print(table)
    for line in notes:
        console.print(line)

    if discover:
        _discover(results)


def _discover(results) -> None:
    """Print TOML skeletons for unrecognized agent-like binaries on PATH."""
    known_bins = set()
    for r in results:
        known_bins.update(r.entry.binaries)

    keywords = ("ai", "code", "agent", "gpt", "llm", "copilot", "cody", "cline", "cursor", "aider")
    seen: set[str] = set()
    candidates: list[str] = []
    for d in os.environ.get("PATH", "").split(os.pathsep):
        p = Path(d)
        if not p.is_dir():
            continue
        try:
            entries = list(p.iterdir())
        except OSError:
            continue
        for f in entries:
            stem = f.stem.lower()
            if stem in seen or stem in known_bins:
                continue
            if any(k in stem for k in keywords):
                seen.add(stem)
                candidates.append(stem)

    console.rule("discover")
    if not candidates:
        console.print("No unknown agent-like tools found on PATH.")
        return
    console.print(
        f"Found {len(candidates)} candidate(s). Paste any into "
        "[bold]~/.tapout/agents.toml[/bold] and edit:\n"
    )
    for name in sorted(candidates):
        skeleton = (
            f"[{name}]\n"
            f'display_name = "{name}"\n'
            f'binaries = ["{name}"]\n'
            f'config_dirs = ["~/.{name}"]\n'
            'resume_style = "cli_prompt"    # or clipboard_gui / clipboard_only\n'
            'launch_template = ["{binary}", "{prompt}"]\n'
            "version_probe = { args = [\"--version\"], timeout_sec = 5 }\n"
        )
        console.print(skeleton, markup=False)


# --------------------------------------------------------------------------
# tap out
# --------------------------------------------------------------------------

@app.command()
def out(
    from_: Optional[str] = typer.Option(
        None,
        "--from",
        help="Path to the agent's response (or '-' for stdin) to ingest into artifacts.",
    ),
    force: bool = typer.Option(
        False, "--force", help="Overwrite an existing .tapout/task-state.json."
    ),
) -> None:
    """Capture task state. No --from: print the summarization prompt. --from: write artifacts."""
    repo = Path.cwd()

    if from_ is None:
        copied = copy_to_clipboard(SUMMARIZATION_PROMPT)
        console.print(SUMMARIZATION_PROMPT, markup=False)
        console.rule()
        if copied:
            console.print("[green]Prompt copied to clipboard.[/green]")
        else:
            console.print("[yellow]Clipboard unavailable[/yellow] — copy the prompt above manually.")
        console.print(
            "Paste it into your CURRENT agent session. Save its JSON reply to a file, then run:\n"
            "  [bold]tap out --from <file>[/bold]   (or pipe it: agent | tap out --from -)"
        )
        return

    # Ingest mode.
    state_path = tapout_paths(repo)["state"]
    if state_path.exists() and not force:
        err.print(
            f"[yellow]A handoff already exists:[/yellow] {state_path}\n"
            "It looks like a task state from an earlier session is still here. Re-run with "
            "[bold]--force[/bold] to overwrite it (this is irreversible)."
        )
        raise typer.Exit(code=1)

    if from_ == "-":
        raw = sys.stdin.read()
    else:
        src = Path(from_)
        if not src.exists():
            err.print(f"[red]File not found:[/red] {from_}")
            raise typer.Exit(code=1)
        raw = src.read_text(encoding="utf-8")

    try:
        state = parse_task_state(raw)
    except ValueError as exc:
        err.print(f"[red]Could not read task state.[/red]\n{exc}")
        raise typer.Exit(code=1)

    state_path, handoff_path = write_artifacts(state, repo)
    record_history(repo, from_agent=state.source_agent, to_agent="(capture)",
                   task_title=state.task_title, event="capture")
    console.print("[green]Handoff captured.[/green]")
    console.print(f"  state:   {state_path}")
    console.print(f"  handoff: {handoff_path}")
    console.print(
        "\nResume with: [bold]tap codex[/bold]  ·  [bold]tap gemini[/bold]  ·  "
        "[bold]tap cursor[/bold]  ·  [bold]tap resume <agent>[/bold]"
    )


# --------------------------------------------------------------------------
# tap resume <agent>  — generic; aliases below are thin wrappers
# --------------------------------------------------------------------------

def _resume(agent: str, dry_run: bool) -> None:
    repo = Path.cwd()
    try:
        plan = prepare_resume(repo, agent)
    except ResumeError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)

    record_history(repo, from_agent=plan.state.source_agent, to_agent=agent,
                   task_title=plan.state.task_title)

    clip_note = "copied to clipboard" if plan.clipboard_ok else "clipboard unavailable"
    console.print(f"[bold]Resuming[/bold] '{plan.state.task_title}' → [cyan]{agent}[/cyan]")
    console.print(f"  opening prompt {clip_note} ({len(plan.opening)} chars)")
    if plan.exe:
        console.print(f"  launch: {plan.exe}")
    if plan.delivery == "stdin":
        console.print("  prompt delivery: [green]stdin[/green] (BatBadBut-safe, headless)")
    elif plan.delivery == "file":
        console.print(f"  prompt delivery: [green]file[/green] → {plan.prompt_file}")

    if plan.guard_reason:
        console.print(f"[yellow]BatBadBut guard:[/yellow] {plan.guard_reason}")

    if plan.effective_style == "clipboard_only":
        console.print(
            f"[yellow]{plan.entry.display_name} has no scriptable resume.[/yellow] "
            "The opening prompt is on your clipboard — start it and paste to begin."
        )
        return

    if plan.effective_style == "clipboard_gui":
        console.print(
            "Opening now; the opening prompt is on your clipboard — paste it into a new chat to begin."
        )

    if dry_run:
        console.print("[dim]--dry-run: not launching. argv would be:[/dim]")
        console.print(f"  {plan.argv}", markup=False)
        return

    try:
        launch(plan, repo)
    except OSError as exc:
        err.print(f"[red]Failed to launch {agent}:[/red] {exc}")
        if plan.clipboard_ok:
            console.print("The opening prompt is on your clipboard — start the agent manually and paste.")
        raise typer.Exit(code=1)


@app.command()
def resume(
    agent: str = typer.Argument(..., help="Any registry key (built-in or from ~/.tapout/agents.toml)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would launch, don't launch."),
) -> None:
    """Resume the captured task in ANY registered agent."""
    _resume(agent, dry_run)


@app.command()
def claude(dry_run: bool = typer.Option(False, "--dry-run", help="Show what would launch, don't launch.")) -> None:
    """Resume the task in Claude Code (alias for `tap resume claude`)."""
    _resume("claude", dry_run)


@app.command()
def codex(dry_run: bool = typer.Option(False, "--dry-run", help="Show what would launch, don't launch.")) -> None:
    """Resume the task in Codex CLI (alias for `tap resume codex`)."""
    _resume("codex", dry_run)


@app.command()
def gemini(dry_run: bool = typer.Option(False, "--dry-run", help="Show what would launch, don't launch.")) -> None:
    """Resume the task in Gemini CLI (alias for `tap resume gemini`)."""
    _resume("gemini", dry_run)


@app.command()
def cursor(dry_run: bool = typer.Option(False, "--dry-run", help="Show what would launch, don't launch.")) -> None:
    """Resume the task in Cursor (alias for `tap resume cursor`)."""
    _resume("cursor", dry_run)


# --------------------------------------------------------------------------
# tap capture  — machine-invoked (hooks, /tapout:pause)
# --------------------------------------------------------------------------

@app.command()
def capture(
    agent: str = typer.Option("claude", "--agent", help="Source agent producing the state."),
    session_transcript: Optional[str] = typer.Option(
        None, "--session-transcript", help="Path to the session transcript (JSONL)."
    ),
    hook_stdin: bool = typer.Option(
        False, "--hook-stdin", help="Read the CC hook JSON (transcript_path, cwd) from stdin."
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite existing task-state.json."),
) -> None:
    """Non-interactive capture: summarize the session into handoff artifacts."""
    if hook_stdin:
        # Delegate entirely to the in-process hook entrypoint — this is the
        # subprocess FALLBACK path (the plugin launcher normally calls
        # run_hook_capture directly, in-process). Same function either way, so
        # there's exactly one place that parses the hook JSON and captures.
        run_hook_capture(sys.stdin.read(), agent=agent, force=force)
        return

    repo = Path(os.environ.get("CLAUDE_PROJECT_DIR") or Path.cwd())
    tpath = Path(session_transcript) if session_transcript else None
    try:
        state = run_capture(repo, agent, tpath, force, use_llm=True)
    except CaptureError as exc:
        err.print(f"[yellow]tap capture:[/yellow] {exc}")
        raise typer.Exit(code=1)

    if state is None:
        console.print("[dim]tap capture: existing handoff kept (use --force to overwrite).[/dim]")
        return

    record_history(repo, from_agent=agent, to_agent="(capture)",
                   task_title=state.task_title, event="capture")
    console.print(f"[green]tap capture:[/green] handoff refreshed — '{state.task_title}'")


@app.command()
def refine(
    transcript: str = typer.Option(..., "--transcript", help="Path to the session transcript (JSONL)."),
    repo: str = typer.Option(..., "--repo", help="Repo whose HANDOFF.md/task-state.json to refine."),
) -> None:
    """Upgrade a heuristic handoff into an LLM summary.

    This is what the background auto-refine spawns after a SessionEnd
    heuristic capture; also useful standalone to manually re-refine a stale
    handoff. Silent no-op if claude isn't available — never destroys the
    existing heuristic artifacts on failure.
    """
    state = run_refine(Path(repo), Path(transcript))
    if state is None:
        console.print("[dim]tap refine: no change (see .tapout/capture.log for the reason).[/dim]")
        return
    console.print(f"[green]tap refine:[/green] handoff refined — '{state.task_title}'")


# --------------------------------------------------------------------------
# tap statusline / tap watch  — usage monitor
# --------------------------------------------------------------------------

@app.command()
def statusline() -> None:
    """Claude Code statusline sink: prints 'tap: N%' and records state for `tap watch`."""
    run_statusline()


@app.command()
def watch(
    once: bool = typer.Option(False, "--once", help="Print one reading and exit."),
    interval: float = typer.Option(2.0, "--interval", help="Seconds between readings."),
    duration: Optional[float] = typer.Option(
        None, "--duration", help="Stop after N seconds (default: run until Ctrl-C)."
    ),
) -> None:
    """Live-monitor Claude Code usage; hints 'run: tap codex' when the window is spent."""
    try:
        run_watch(once=once, interval=interval, duration=duration)
    except KeyboardInterrupt:
        pass


# --------------------------------------------------------------------------
# tap buddy  — Windows tray icon
# --------------------------------------------------------------------------

buddy_app = typer.Typer(
    invoke_without_command=True,
    no_args_is_help=False,
    help="System tray buddy: usage % icon, toast warnings, one-click resume.",
)
app.add_typer(buddy_app, name="buddy")


@buddy_app.callback(invoke_without_command=True)
def buddy_default(
    ctx: typer.Context,
    dry_run: bool = typer.Option(False, "--dry-run", help="Run one poll cycle, print the result, exit."),
    detached: bool = typer.Option(False, "--detached", help="Run with no console window (used by autostart)."),
) -> None:
    """Launch the tray icon (default). See `tap buddy --help` for subcommands."""
    if ctx.invoked_subcommand is not None:
        return

    from . import tray

    if dry_run:
        tray.run_dry_run()
        return

    if detached and tray.relaunch_detached_if_needed():
        return  # relaunched consoleless; this process's job is done

    tray.run_buddy()


@buddy_app.command("install-autostart")
def buddy_install_autostart() -> None:
    """Launch tapout buddy silently on login (Windows Startup shortcut)."""
    from . import tray

    if sys.platform != "win32":
        err.print("[yellow]Autostart is Windows-only for now.[/yellow]")
        raise typer.Exit(code=1)
    try:
        path = tray.install_autostart()
    except Exception as exc:
        err.print(f"[red]Could not create autostart shortcut:[/red] {exc}")
        raise typer.Exit(code=1)
    console.print(f"[green]Autostart installed:[/green] {path}")


@buddy_app.command("uninstall-autostart")
def buddy_uninstall_autostart() -> None:
    """Remove the autostart shortcut, if present."""
    from . import tray

    removed = tray.uninstall_autostart()
    if removed:
        console.print("[green]Autostart removed.[/green]")
    else:
        console.print("[dim]No autostart shortcut was installed.[/dim]")


def main() -> None:
    """Entry point for the `tap`/`tapout` console scripts and `python -m tapout`.

    Never fail silently: any exception that escapes typer's own dispatch
    (SystemExit from typer.Exit/click is passed through untouched) gets one
    diagnostic line on stderr before exiting non-zero, so a crash in a
    subprocess-invoked context (e.g. the plugin hook's fallback path) is never
    a bare non-zero exit code with empty stdout/stderr.
    """
    try:
        app()
    except SystemExit:
        raise
    except Exception as exc:
        sys.stderr.write(f"tapout: fatal: {exc!r}\n")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
