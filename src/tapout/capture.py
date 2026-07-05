"""Machine-invoked capture: turn a session transcript into handoff artifacts.

This is the non-interactive sibling of `tap out`. Claude Code's PreCompact /
SessionEnd hooks invoke it; it reads the hook JSON on stdin, summarizes the
session transcript into the task-state schema, and writes the artifacts
atomically so a crash mid-capture never leaves a half-written state file.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

from .detect import resolve_executable
from .handoff import (
    SCHEMA_VERSION,
    SUMMARIZATION_PROMPT,
    PlanStep,
    StepStatus,
    TaskState,
    now_iso,
    parse_task_state,
    render_markdown,
    tapout_paths,
)

# Offline / test override: if set, read the agent's JSON reply from this file
# instead of calling the summarizer. Also handy for deterministic demos.
ENV_CAPTURE_FROM = "TAPOUT_CAPTURE_FROM"

# How much transcript text to feed the summarizer (chars). Keeps the prompt
# bounded on very long sessions; the tail carries the most recent state.
TRANSCRIPT_BUDGET = 60_000


class CaptureError(Exception):
    """Capture could not complete."""


# --------------------------------------------------------------------------
# hook plumbing
# --------------------------------------------------------------------------

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"


def _mangle_cwd(path: Path) -> str:
    """Claude Code names a project dir by replacing every non-alnum char with '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def find_project_transcript(repo: Path) -> Path:
    """Newest top-level session transcript for `repo`'s Claude Code project dir.

    Only considers top-level `*.jsonl` — subagent transcripts live in a
    `subagents/` subdir and must not be picked. Raises CaptureError (listing
    candidates) if the project dir or a transcript can't be found.
    """
    if not CLAUDE_PROJECTS.exists():
        raise CaptureError(f"no Claude Code projects directory at {CLAUDE_PROJECTS}")

    target = _mangle_cwd(repo.resolve()).lower()  # drive-letter case varies
    match = next(
        (d for d in CLAUDE_PROJECTS.iterdir() if d.is_dir() and d.name.lower() == target),
        None,
    )
    if match is None:
        available = "\n".join(f"  - {d.name}" for d in sorted(CLAUDE_PROJECTS.iterdir()) if d.is_dir())
        raise CaptureError(
            f"no Claude Code session directory for {repo}.\n"
            f"Expected a dir named '{target}' under {CLAUDE_PROJECTS}.\n"
            f"Available project dirs:\n{available or '  (none)'}\n"
            "Pass --session-transcript <path> explicitly, or run this from the repo "
            "where the Claude session happened."
        )

    sessions = sorted(match.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not sessions:
        raise CaptureError(f"no session transcripts (*.jsonl) in {match}")
    return sessions[0]


def condense_transcript(path: Path) -> str:
    """Flatten a Claude Code transcript JSONL into role-tagged plain text."""
    if not path.exists():
        raise CaptureError(f"transcript not found: {path}")
    chunks: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        role, text = _extract_message(obj)
        if text:
            chunks.append(f"{role}: {text}")
    joined = "\n".join(chunks)
    if len(joined) > TRANSCRIPT_BUDGET:
        joined = "...(earlier turns elided)...\n" + joined[-TRANSCRIPT_BUDGET:]
    return joined


def _extract_message(obj: dict) -> tuple[str, str]:
    """Best-effort (role, text) from one transcript record across CC formats."""
    msg = obj.get("message", obj)
    role = msg.get("role") or obj.get("type") or "unknown"
    content = msg.get("content", "")
    if isinstance(content, str):
        return role, content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif block.get("type") == "tool_result":
                    parts.append(str(block.get("content", ""))[:500])
            elif isinstance(block, str):
                parts.append(block)
        return role, " ".join(p for p in parts if p)
    return role, ""


# --------------------------------------------------------------------------
# summarization
# --------------------------------------------------------------------------

def summarize_via_claude(prompt_text: str, timeout: float = 180) -> str:
    """Ask a fresh headless `claude -p` to emit the task-state JSON block."""
    exe = resolve_executable("claude")
    if not exe:
        raise CaptureError("claude not found on PATH; cannot summarize the session.")
    try:
        proc = subprocess.run(
            [exe, "-p"],
            input=prompt_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise CaptureError(f"summarizer timed out after {timeout}s") from exc
    except OSError as exc:
        raise CaptureError(f"could not run claude: {exc}") from exc
    if proc.returncode != 0:
        raise CaptureError(f"claude exited {proc.returncode}: {(proc.stderr or '').strip()[:300]}")
    return proc.stdout


_FILE_TOKEN_RE = re.compile(r"[\w./\\-]+\.[A-Za-z0-9]{1,6}\b")

# Pure version strings (2.41.5, 0.4.2, 26.1.2, 9.0.2, 3.12.0, 4.2, ...).
_VERSION_RE = re.compile(r"^\d+(\.\d+)+([-.]\w+)?$")

# git-bash / MSYS absolute path: /c/Users/... — not a real Windows path.
_GITBASH_ABS_RE = re.compile(r"^/([A-Za-z])/(.+)$")

# Extensions that mean "this is source/config the session plausibly touched".
_SOURCE_EXTENSIONS = {
    "py", "js", "ts", "tsx", "jsx", "md", "json", "yaml", "yml", "toml",
    "rs", "go", "java", "html", "css", "sql", "sh", "txt", "c", "cpp", "h",
    "hpp", "rb", "php", "kt", "swift", "ini", "cfg", "env",
}

# Extensions that are never project source, even when path-shaped (interpreter
# binaries, compiled artifacts) — e.g. .../Python312/python.exe.
_DENY_EXTENSIONS = {"exe", "dll", "so", "dylib", "whl", "pyc", "pyo"}


def _normalize_path_style(tok: str) -> str:
    """Rewrite to native path style; fixes git-bash `/c/Users/...` absolutes."""
    m = _GITBASH_ABS_RE.match(tok)
    if m and os.name == "nt":
        drive, rest = m.group(1).upper(), m.group(2)
        return f"{drive}:\\" + rest.replace("/", "\\")
    if os.name == "nt":
        return tok.replace("/", "\\")
    return tok.replace("\\", "/")


def _looks_like_project_file(tok: str) -> bool:
    """Reject version strings, platform/tool identifiers, and bare binaries."""
    if _VERSION_RE.match(tok):
        return False
    ext = tok.rsplit(".", 1)[-1].lower() if "." in tok else ""
    if ext in _DENY_EXTENSIONS:
        return False
    has_separator = "/" in tok or "\\" in tok
    has_source_extension = ext in _SOURCE_EXTENSIONS
    return has_separator or has_source_extension


def _extract_files_touched(transcript_text: str) -> list[str]:
    candidates: set[str] = set()
    for m in _FILE_TOKEN_RE.finditer(transcript_text):
        normalized = _normalize_path_style(m.group(0))
        if _looks_like_project_file(normalized):
            candidates.add(normalized)
    return sorted(candidates)[:20]


def heuristic_state_from_transcript(
    transcript_text: str, transcript_path: Path, repo: Path, agent: str
) -> TaskState:
    """Build a valid, best-effort TaskState directly from transcript text.

    No LLM call, no subprocess — pure string processing so it finishes in well
    under a second. Used for hook-invoked capture (SessionEnd/PreCompact),
    where a nested `claude -p` subprocess would otherwise get killed by session
    teardown before it can respond. Coarser than the LLM summary, but reliable.
    """
    user_lines = [
        line[len("user:"):].strip()
        for line in transcript_text.splitlines()
        if line.lower().startswith("user:")
    ]
    first_user = next((l for l in user_lines if l), "")
    task_title = (first_user[:80].strip() or f"{repo.name} — session capture")
    goal = (
        first_user[:400].strip()
        or "Auto-captured at session end (no LLM summarization available). "
        "See the linked transcript for full context."
    )

    files = _extract_files_touched(transcript_text)

    return TaskState(
        schema_version=SCHEMA_VERSION,
        created_at=now_iso(),
        source_agent=agent,
        task_title=task_title,
        goal=goal,
        plan=[
            PlanStep(
                step="Auto-captured at session end without LLM summarization — "
                "verify actual progress against the transcript.",
                status=StepStatus.in_progress,
            )
        ],
        decisions=[],
        files_touched=files,
        next_steps=[
            f"Read the full session transcript at {transcript_path} for complete context.",
            "Continue the task described in Goal above.",
        ],
        blockers=[],
        commands_to_verify=["git status", "git diff"],
    )


def build_summarization_input(transcript_text: str) -> str:
    return (
        f"{SUMMARIZATION_PROMPT}\n\n"
        "The session transcript to summarize follows (most recent turns last):\n"
        "--- BEGIN TRANSCRIPT ---\n"
        f"{transcript_text}\n"
        "--- END TRANSCRIPT ---\n"
    )


# --------------------------------------------------------------------------
# atomic artifact write
# --------------------------------------------------------------------------

def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)  # atomic on POSIX and Windows
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def write_artifacts_atomic(state: TaskState, repo: Path) -> tuple[Path, Path]:
    paths = tapout_paths(repo)
    _atomic_write(paths["state"], state.to_json())
    _atomic_write(paths["handoff"], render_markdown(state))
    return paths["state"], paths["handoff"]


def log(repo: Path, message: str) -> None:
    path = tapout_paths(repo)["dir"] / "capture.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(f"{now_iso()} {message}\n")


# --------------------------------------------------------------------------
# top-level capture
# --------------------------------------------------------------------------

def run_capture(
    repo: Path,
    agent: str,
    transcript_path: Optional[Path],
    force: bool,
    use_llm: bool = True,
) -> Optional[TaskState]:
    """Capture the session into artifacts. Returns the state, or None if skipped.

    Silent: never prompts. All progress goes to .tapout/capture.log.

    use_llm controls how a bare transcript (no TAPOUT_CAPTURE_FROM override) is
    turned into a task state:
      - True (manual `tap capture`, `/tapout:pause`): spawn a fresh `claude -p`
        for a proper LLM summary. The caller is alive and can tolerate the delay.
      - False (SessionEnd/PreCompact hooks): summarize with pure string
        processing, no subprocess. A nested `claude -p` here would get killed
        by session teardown before it could respond, silently producing nothing.
        Missing/unreadable transcript is not an error in this mode — it's
        logged and skipped so a hook never stains the session with exit != 0.
    """
    state_path = tapout_paths(repo)["state"]
    if state_path.exists() and not force:
        log(repo, "SKIP existing task-state.json present (re-run with --force to overwrite)")
        return None

    # Obtain the agent's task-state JSON reply.
    override = os.environ.get(ENV_CAPTURE_FROM)
    if override:
        reply = Path(override).read_text(encoding="utf-8")
        log(repo, f"summary source: {ENV_CAPTURE_FROM}={override}")
        state = parse_task_state(reply)
    elif use_llm:
        if transcript_path is None:
            # Auto-pick the newest session transcript for this project's cwd.
            transcript_path = find_project_transcript(repo)
            log(repo, f"auto-picked transcript {transcript_path}")
        transcript_text = condense_transcript(transcript_path)
        reply = summarize_via_claude(build_summarization_input(transcript_text))
        log(repo, f"summarized transcript {transcript_path} via claude -p ({len(transcript_text)} chars)")
        state = parse_task_state(reply)
    else:
        if transcript_path is None or not transcript_path.exists():
            log(repo, "SessionEnd: no transcript payload, skipping")
            return None
        transcript_text = condense_transcript(transcript_path)
        state = heuristic_state_from_transcript(transcript_text, transcript_path, repo, agent)
        log(repo, f"heuristic-summarized transcript {transcript_path}, no LLM ({len(transcript_text)} chars)")

    # The capture stamps who/when — the transcript reflects the source agent now.
    state.created_at = now_iso()
    state.source_agent = agent

    state_path, handoff_path = write_artifacts_atomic(state, repo)
    log(repo, f"OK wrote {state_path.name} + {handoff_path.name} ('{state.task_title}')")
    return state


def run_hook_capture(
    hook_json: str, agent: str = "claude", force: bool = True
) -> Optional[TaskState]:
    """In-process entrypoint for the plugin's SessionEnd/PreCompact hook.

    Parses Claude Code's hook JSON and captures immediately, IN THIS PROCESS —
    no subprocess, so a Windows session-teardown kill can't cut a spawned
    child off mid-flight. Never raises — a hook must not stain the session
    with a non-zero exit; any failure is logged to .tapout/capture.log so a
    user hitting this in the wild can debug it.
    """
    if os.environ.get("CLAUDE_CODE_CHILD_SESSION"):
        # A subagent's own session end — not the user's top-level session.
        # Capturing here would overwrite the real handoff with a subagent's
        # transcript, and (with refinement) spawn a real claude -p per
        # subagent. Skip entirely; the top-level session's own SessionEnd
        # still fires normally.
        return None

    try:
        data = json.loads(hook_json) if hook_json.strip() else {}
    except json.JSONDecodeError:
        data = {}

    repo = Path(data.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or Path.cwd())
    transcript = data.get("transcript_path")
    tpath = Path(transcript) if transcript else None

    try:
        state = run_capture(repo, agent, tpath, force, use_llm=False)
    except Exception as exc:
        log(repo, f"SessionEnd: capture failed: {exc!r}")
        return None

    if state is not None:
        from .resume import record_history
        record_history(repo, from_agent=agent, to_agent="(capture)",
                       task_title=state.task_title, event="capture")
        if tpath is not None and refine_on_capture_enabled():
            if resolve_executable("claude"):
                spawn_detached_refine(repo, tpath)
            else:
                log(repo, "refine: claude not on PATH, skipping auto-refine")
    return state


# --------------------------------------------------------------------------
# background LLM refinement
# --------------------------------------------------------------------------

CONFIG_PATH = Path.home() / ".tapout" / "config.toml"

REFINE_TIMEOUT_SEC = 90


def refine_on_capture_enabled() -> bool:
    """~/.tapout/config.toml: `refine_on_capture = false` opts out. Default: on."""
    if not CONFIG_PATH.exists():
        return True
    try:
        from .registry import _toml
        # utf-8-sig: Windows tools (PowerShell Out-File, Notepad) default to
        # writing a UTF-8 BOM, which tomllib/tomli reject outright — a BOM'd
        # config would otherwise silently fail-open (opt-out ignored, real
        # background claude -p calls keep firing). utf-8-sig strips it if
        # present and is identical to utf-8 when it's not.
        cfg = _toml.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    except Exception:
        return True
    return bool(cfg.get("refine_on_capture", True))


def spawn_detached_refine(repo: Path, transcript_path: Path) -> None:
    """Fire-and-forget `tap refine` in a fully detached child process.

    Must survive the parent (Claude Code / this hook process) exiting —
    that's the whole point: the heuristic handoff is already on disk, and this
    upgrades it in the background without blocking or risking session
    teardown killing it. Best-effort: any spawn failure is silently ignored,
    the heuristic handoff stands.
    """
    argv = [
        sys.executable, "-m", "tapout", "refine",
        "--transcript", str(transcript_path), "--repo", str(repo),
    ]
    try:
        if sys.platform == "win32":
            DETACHED_PROCESS = 0x00000008
            CREATE_NEW_PROCESS_GROUP = 0x00000200
            subprocess.Popen(
                argv,
                creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
                close_fds=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            subprocess.Popen(
                argv,
                start_new_session=True,
                close_fds=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
    except Exception:
        pass


def run_refine(repo: Path, transcript_path: Path, timeout: float = REFINE_TIMEOUT_SEC) -> Optional[TaskState]:
    """Upgrade a heuristic handoff to an LLM summary. Never raises.

    Runs (usually) as the detached child `spawn_detached_refine` launches, but
    is also directly invokable via `tap refine` to manually re-refine a stale
    handoff. On any failure — claude missing, timeout, invalid JSON — logs one
    line to .tapout/capture.log and leaves the existing artifacts untouched.
    """
    try:
        if not resolve_executable("claude"):
            log(repo, "refine: claude not on PATH, skipping")
            return None
        transcript_text = condense_transcript(transcript_path)
        reply = summarize_via_claude(build_summarization_input(transcript_text), timeout=timeout)
        state = parse_task_state(reply)
    except Exception as exc:
        log(repo, f"refine: failed, heuristic handoff kept: {exc!r}")
        return None

    state.created_at = now_iso()
    state.source_agent = "claude"
    write_artifacts_atomic(state, repo)
    log(repo, f"refined by background LLM at {now_iso()}")
    return state
