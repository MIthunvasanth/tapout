"""Agent registry — bundled agents.toml merged with the user overlay."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal, Optional

try:  # Python 3.11+
    import tomllib as _toml
except ModuleNotFoundError:  # Python 3.10
    import tomli as _toml  # type: ignore

from importlib.resources import files

from pydantic import BaseModel, Field, ValidationError

ResumeStyle = Literal["cli_prompt", "clipboard_gui", "clipboard_only"]

USER_OVERLAY = Path.home() / ".tapout" / "agents.toml"


class VersionProbe(BaseModel):
    args: list[str] = Field(default_factory=list)
    timeout_sec: float = 5.0


class AgentEntry(BaseModel):
    key: str
    display_name: str
    binaries: list[str] = Field(default_factory=list)
    config_dirs: list[str] = Field(default_factory=list)
    resume_style: ResumeStyle = "clipboard_only"
    launch_template: list[str] = Field(default_factory=list)
    # How the opening prompt reaches the child (cli_prompt style only):
    #   "argv"        prompt substituted into launch_template as an argument
    #   "stdin"       prompt written to the child's stdin (bypasses cmd.exe)
    #   "file:<flag>" prompt written to a file, path passed via <flag>
    #   "clipboard"   prompt only copied to clipboard; child launched bare
    prompt_delivery: str = "argv"
    version_probe: Optional[VersionProbe] = None
    notes: str = ""

    def resumable_label(self) -> str:
        return {
            "cli_prompt": "yes",
            "clipboard_gui": "prompt injection via clipboard",
            "clipboard_only": "clipboard (manual paste)",
        }[self.resume_style]

    def delivery_kind(self) -> str:
        """Normalized delivery: 'argv' | 'stdin' | 'file' | 'clipboard'."""
        if self.prompt_delivery.startswith("file:"):
            return "file"
        return self.prompt_delivery

    def file_flag(self) -> Optional[str]:
        if self.prompt_delivery.startswith("file:"):
            return self.prompt_delivery.split(":", 1)[1]
        return None


class RegistryError(Exception):
    """Malformed registry file."""


def _load_toml(text: str, source: str) -> dict:
    try:
        return _toml.loads(text)
    except Exception as exc:  # tomllib.TOMLDecodeError etc.
        raise RegistryError(f"Could not parse {source}: {exc}") from exc


def _bundled_raw() -> dict:
    text = files("tapout").joinpath("agents.toml").read_text(encoding="utf-8")
    return _load_toml(text, "bundled agents.toml")


def _overlay_raw() -> dict:
    if not USER_OVERLAY.exists():
        return {}
    # utf-8-sig: Windows tools (PowerShell Out-File, Notepad) default to a
    # UTF-8 BOM, which tomllib/tomli reject outright. Strips it if present;
    # identical to utf-8 when it's not.
    return _load_toml(USER_OVERLAY.read_text(encoding="utf-8-sig"), str(USER_OVERLAY))


def _merge(base: dict, overlay: dict) -> dict:
    """Field-level merge; user keys win. New agents just appear."""
    out = {k: dict(v) for k, v in base.items()}
    for key, entry in overlay.items():
        if not isinstance(entry, dict):
            continue
        if key in out:
            out[key].update(entry)
        else:
            out[key] = dict(entry)
    return out


def load_registry() -> dict[str, AgentEntry]:
    merged = _merge(_bundled_raw(), _overlay_raw())
    registry: dict[str, AgentEntry] = {}
    for key, fields in merged.items():
        try:
            registry[key] = AgentEntry(key=key, **fields)
        except ValidationError as exc:
            raise RegistryError(f"Invalid registry entry '{key}': {exc}") from exc
    return registry


def get_entry(key: str) -> Optional[AgentEntry]:
    return load_registry().get(key)


def expand_dirs(config_dirs: list[str]) -> list[Path]:
    """Expand ~ and {ENV} placeholders; drop entries whose env var is unset."""
    out: list[Path] = []
    env = {
        "APPDATA": os.environ.get("APPDATA"),
        "LOCALAPPDATA": os.environ.get("LOCALAPPDATA"),
        "HOME": str(Path.home()),
    }
    for raw in config_dirs:
        expanded = raw
        skip = False
        for name, value in env.items():
            token = "{" + name + "}"
            if token in expanded:
                if not value:
                    skip = True
                    break
                expanded = expanded.replace(token, value)
        if skip:
            continue
        out.append(Path(expanded).expanduser())
    return out
