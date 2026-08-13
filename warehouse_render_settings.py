"""Persistence boundary for presentation-only warehouse map settings."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import Any

from warehouse_persistence import atomic_write_json, read_json

RENDER_SETTINGS_PATH = Path("data/last_import/render_settings.json")


def load_render_settings(defaults: dict[str, Any], path: Path = RENDER_SETTINGS_PATH) -> dict[str, Any]:
    settings = deepcopy(defaults)
    try:
        payload = read_json(path, default={})
    except (OSError, UnicodeError, ValueError):
        return settings
    if isinstance(payload, dict):
        for key in settings:
            if key != "colors" and key in payload:
                settings[key] = payload[key]
        if isinstance(payload.get("colors"), dict) and isinstance(settings.get("colors"), dict):
            settings["colors"].update(payload["colors"])
    return settings


def save_render_settings(settings: dict[str, Any], path: Path = RENDER_SETTINGS_PATH) -> None:
    """Merge and atomically save settings, retaining forward-compatible keys."""
    try:
        existing = read_json(path, default={})
    except json.JSONDecodeError:
        # Historical policy permits resetting a malformed presentation-only
        # settings file.  I/O and decoding failures must propagate instead:
        # overwriting then could discard forward-compatible keys.
        existing = {}
    payload = dict(existing) if isinstance(existing, dict) else {}
    colors = dict(payload.get("colors", {})) if isinstance(payload.get("colors"), dict) else {}
    payload.update(settings)
    if isinstance(settings.get("colors"), dict):
        colors.update(settings["colors"])
        payload["colors"] = colors
    atomic_write_json(path, payload)
