"""XDG-compliant path management for fwts.

Respects XDG Base Directory Specification:
- XDG_CONFIG_HOME: Config files (default: ~/.config)
- XDG_STATE_HOME: State files (default: ~/.local/state)
- XDG_DATA_HOME: Data files (default: ~/.local/share)

Also supports FWTS_CONFIG_DIR and FWTS_STATE_DIR for full override.
"""

from __future__ import annotations

import os
from pathlib import Path


def get_config_dir() -> Path:
    """Get the fwts config directory.

    Priority:
    1. FWTS_CONFIG_DIR env var (full override)
    2. XDG_CONFIG_HOME/fwts
    3. ~/.config/fwts (default)
    """
    if override := os.environ.get("FWTS_CONFIG_DIR"):
        return Path(override).expanduser()

    xdg_config = os.environ.get("XDG_CONFIG_HOME")
    if xdg_config:
        return Path(xdg_config) / "fwts"

    return Path.home() / ".config" / "fwts"


def get_state_dir() -> Path:
    """Get the fwts state directory.

    Priority:
    1. FWTS_STATE_DIR env var (full override)
    2. XDG_STATE_HOME/fwts
    3. ~/.local/state/fwts (default)
    """
    if override := os.environ.get("FWTS_STATE_DIR"):
        return Path(override).expanduser()

    xdg_state = os.environ.get("XDG_STATE_HOME")
    if xdg_state:
        return Path(xdg_state) / "fwts"

    return Path.home() / ".local" / "state" / "fwts"


def get_global_config_path() -> Path:
    """Get the global config file path."""
    return get_config_dir() / "config.toml"


def get_global_hooks_dir() -> Path:
    """Get the global hooks directory."""
    return get_config_dir() / "hooks"


def ensure_config_dir() -> Path:
    """Ensure config directory exists and return it."""
    config_dir = get_config_dir()
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir


def ensure_state_dir() -> Path:
    """Ensure state directory exists and return it."""
    state_dir = get_state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir
