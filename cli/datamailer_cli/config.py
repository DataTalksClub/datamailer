"""Configuration resolution for the Datamailer CLI.

Settings are resolved with this precedence (highest first):

1. Explicit command-line flags (``--url``, ``--api-key``, ...).
2. Environment variables ``DATAMAILER_URL`` and ``DATAMAILER_API_KEY``
   (the same names used across the Datamailer repo and docs).
3. The config file at ``$XDG_CONFIG_HOME/datamailer/config.toml``
   (``~/.config/datamailer/config.toml`` by default).
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

CONFIG_ENV_VAR = "DATAMAILER_CONFIG"
URL_ENV_VAR = "DATAMAILER_URL"
API_KEY_ENV_VAR = "DATAMAILER_API_KEY"

# Keys persisted in config.toml. All values are plain strings.
_FIELDS = ("url", "api_key", "default_to", "default_from")


@dataclass
class Settings:
    url: str = ""
    api_key: str = ""
    default_to: str = ""
    default_from: str = ""

    @property
    def base_url(self) -> str:
        return self.url.rstrip("/")


def config_path() -> Path:
    override = os.environ.get(CONFIG_ENV_VAR)
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(Path.home(), ".config")
    return Path(base) / "datamailer" / "config.toml"


def load_file(path: Path | None = None) -> dict:
    path = path or config_path()
    if not path.exists():
        return {}
    with path.open("rb") as handle:
        return tomllib.load(handle)


def resolve(args, path: Path | None = None) -> Settings:
    """Merge file, environment, and CLI flags into a single Settings object."""
    data = load_file(path)
    settings = Settings(
        url=str(data.get("url", "")),
        api_key=str(data.get("api_key", "")),
        default_to=str(data.get("default_to", "")),
        default_from=str(data.get("default_from", "")),
    )

    env_url = os.environ.get(URL_ENV_VAR)
    if env_url:
        settings.url = env_url
    env_key = os.environ.get(API_KEY_ENV_VAR)
    if env_key:
        settings.api_key = env_key

    if getattr(args, "url", None):
        settings.url = args.url
    if getattr(args, "api_key", None):
        settings.api_key = args.api_key

    return settings


def save_file(values: dict, path: Path | None = None) -> Path:
    """Persist the given values to config.toml, preserving unknown keys."""
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    merged = load_file(path)
    for key in _FIELDS:
        if key in values and values[key] is not None:
            merged[key] = values[key]

    path.write_text(_dump_toml(merged), encoding="utf-8")
    try:
        path.chmod(0o600)  # the file holds an API key
    except OSError:
        pass
    return path


def _dump_toml(values: dict) -> str:
    lines = []
    for key in _FIELDS:
        if values.get(key):
            lines.append(f"{key} = {_toml_string(str(values[key]))}")
    return "\n".join(lines) + "\n"


def _toml_string(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'
