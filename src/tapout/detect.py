"""Detect installed AI coding agents on this machine."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path


def resolve_executable(name: str) -> str | None:
    """Resolve a command name to a launchable path.

    On Windows, npm-style global installs ship BOTH an extensionless bash shim
    (e.g. ...\\npm\\gemini) and a real launcher (gemini.cmd). Plain shutil.which
    can return the shim, which CreateProcess cannot execute -> WinError 193.
    Prefer a real executable extension from PATHEXT before falling back.
    """
    if sys.platform == "win32" and not os.path.splitext(name)[1]:
        pathext = os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD")
        for ext in pathext.split(os.pathsep):
            ext = ext.strip()
            if not ext:
                continue
            found = shutil.which(name + ext)
            if found:
                return found
    return shutil.which(name)


@dataclass
class AgentSpec:
    key: str
    label: str
    cmd: str | None  # CLI binary name, or None if GUI-only
    config_dirs: list[Path] = field(default_factory=list)
    resumable: str = "yes"  # "yes" or an explanatory note


def _home() -> Path:
    return Path.home()


def _cursor_dirs() -> list[Path]:
    """Windows install / appdata locations for the Cursor editor."""
    dirs: list[Path] = []
    local = os.environ.get("LOCALAPPDATA")
    roaming = os.environ.get("APPDATA")
    if local:
        dirs.append(Path(local) / "Programs" / "cursor")
    if roaming:
        dirs.append(Path(roaming) / "Cursor")
    # POSIX config location (harmless on Windows).
    dirs.append(_home() / ".cursor")
    return dirs


def agent_specs() -> list[AgentSpec]:
    return [
        AgentSpec("claude", "Claude Code", "claude", [_home() / ".claude"]),
        AgentSpec("codex", "Codex CLI", "codex", [_home() / ".codex"]),
        AgentSpec("gemini", "Gemini CLI", "gemini", [_home() / ".gemini"]),
        AgentSpec(
            "cursor",
            "Cursor",
            "cursor",
            _cursor_dirs(),
            resumable="prompt injection via clipboard",
        ),
    ]


@dataclass
class DetectionResult:
    spec: AgentSpec
    installed: bool
    which: str | None
    config_found: Path | None
    version: str | None


def _version(exe: str) -> str | None:
    try:
        proc = subprocess.run(
            [exe, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            shell=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    out = (proc.stdout or proc.stderr or "").strip()
    if not out:
        return None
    return out.splitlines()[0].strip()


def detect_one(spec: AgentSpec) -> DetectionResult:
    which = resolve_executable(spec.cmd) if spec.cmd else None
    config_found = next((d for d in spec.config_dirs if d.exists()), None)
    installed = bool(which) or config_found is not None
    version = _version(which) if which else None
    return DetectionResult(
        spec=spec,
        installed=installed,
        which=which,
        config_found=config_found,
        version=version,
    )


def detect_all() -> list[DetectionResult]:
    return [detect_one(s) for s in agent_specs()]
