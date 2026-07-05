"""Detect installed AI coding agents — registry-driven."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .registry import AgentEntry, expand_dirs, load_registry


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


def resolve_binary(entry: AgentEntry) -> str | None:
    """First of the entry's candidate binaries that resolves on PATH."""
    for name in entry.binaries:
        found = resolve_executable(name)
        if found:
            return found
    return None


@dataclass
class DetectionResult:
    entry: AgentEntry
    installed: bool
    which: str | None
    config_found: Path | None
    version: str | None


def _version(exe: str, entry: AgentEntry) -> str | None:
    probe = entry.version_probe
    if probe is None:
        return None
    try:
        proc = subprocess.run(
            [exe, *probe.args],
            capture_output=True,
            text=True,
            timeout=probe.timeout_sec,
            shell=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    out = (proc.stdout or proc.stderr or "").strip()
    if not out:
        return None
    return out.splitlines()[0].strip()


def detect_one(entry: AgentEntry) -> DetectionResult:
    which = resolve_binary(entry)
    config_found = next((d for d in expand_dirs(entry.config_dirs) if d.exists()), None)
    installed = bool(which) or config_found is not None
    version = _version(which, entry) if which else None
    return DetectionResult(
        entry=entry,
        installed=installed,
        which=which,
        config_found=config_found,
        version=version,
    )


def detect_all() -> list[DetectionResult]:
    registry = load_registry()
    return [detect_one(entry) for entry in registry.values()]
