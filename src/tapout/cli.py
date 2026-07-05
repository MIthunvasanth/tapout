"""tapout CLI — `tap` / `tapout`."""

from __future__ import annotations

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
from .detect import detect_all
from .handoff import SUMMARIZATION_PROMPT, parse_task_state, tapout_paths, write_artifacts
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
def scan() -> None:
    """Find installed AI coding agents on this machine."""
    results = detect_all()
    table = Table(title="tapout — detected agents")
    table.add_column("agent", style="bold")
    table.add_column("installed")
    table.add_column("version")
    table.add_column("resumable")

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
        resumable = r.spec.resumable if r.installed else "[dim]-[/dim]"
        table.add_row(r.spec.label, installed, version, resumable)

    console.print(table)


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
    console.print("[green]Handoff captured.[/green]")
    console.print(f"  state:   {state_path}")
    console.print(f"  handoff: {handoff_path}")
    console.print(f"\nResume with: [bold]tap codex[/bold]  ·  [bold]tap gemini[/bold]  ·  [bold]tap cursor[/bold]  ·  [bold]tap claude[/bold]")


# --------------------------------------------------------------------------
# tap <agent>  — resume
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
    console.print(f"  launch: {plan.exe}")

    if plan.is_gui:
        console.print(
            f"[yellow]Cursor has no prompt-injection flag.[/yellow] Opening the folder now; "
            "the opening prompt is on your clipboard — paste it into a new Cursor chat (Ctrl+L)."
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
def claude(dry_run: bool = typer.Option(False, "--dry-run", help="Show what would launch, don't launch.")) -> None:
    """Resume the task in Claude Code."""
    _resume("claude", dry_run)


@app.command()
def codex(dry_run: bool = typer.Option(False, "--dry-run", help="Show what would launch, don't launch.")) -> None:
    """Resume the task in Codex CLI."""
    _resume("codex", dry_run)


@app.command()
def gemini(dry_run: bool = typer.Option(False, "--dry-run", help="Show what would launch, don't launch.")) -> None:
    """Resume the task in Gemini CLI."""
    _resume("gemini", dry_run)


@app.command()
def cursor(dry_run: bool = typer.Option(False, "--dry-run", help="Show what would launch, don't launch.")) -> None:
    """Resume the task in Cursor (via clipboard)."""
    _resume("cursor", dry_run)


if __name__ == "__main__":
    app()
