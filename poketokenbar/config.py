"""Settings shared by the daemon and the plasmoid.

Keys port from UsageStore's UserDefaults. Dropped deliberately:
disableKeychainAccess (no Keychain on Linux) and updateNotificationsEnabled
(no release channel for a personal build).
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path

DEFAULTS: dict[str, object] = {
    "refresh_interval": 120,
    "warn_threshold": 80,
    "crit_threshold": 95,
    "show_tokens_in_menu": False,
    "show_cost_in_menu": False,
    "show_limit_in_menu": True,
    "limit_display_mode": "both",
    "panel_percent_source": "evolution",
    "limit_notifications": True,
    "companion_notifications": True,
    "status_checks_enabled": True,
    "floating_pet_enabled": False,
    "floating_pet_size": 96,
    "floating_pet_bubble_alerts": True,
    "language": "en",
    # Difficulty multipliers (upstream #244): 0.1 (easiest) .. 2.0 (hardest).
    "growth_difficulty": 1.0,
    "shop_difficulty": 1.0,
    # Provider id -> extra folders to scan on top of the built-in ones
    # (upstream #177). Edited with `poketokenctl folder add|remove`.
    "extra_scan_folders": {},
}


def default_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(base) / "poketokenbar" / "config.json"


def load(path: Path) -> dict:
    values = dict(DEFAULTS)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return values
    if not isinstance(raw, dict):
        return values
    for key, value in raw.items():
        if key in DEFAULTS:
            values[key] = value
    folders = values.get("extra_scan_folders")
    values["extra_scan_folders"] = (
        {
            str(pid): [str(p) for p in paths if isinstance(p, str) and p]
            for pid, paths in folders.items()
            if isinstance(paths, list)
        }
        if isinstance(folders, dict)
        else {}
    )
    return values


def edit_scan_folder(path: Path, action: str, provider_id: str, folder: str) -> list[str]:
    """Add or remove one extra scan folder for a provider; returns that
    provider's list afterwards. Folders are stored absolute."""
    if action not in ("add", "remove"):
        raise ValueError(f"unknown folder action: {action}")
    values = load(path)
    folders = dict(values["extra_scan_folders"])
    current = list(folders.get(provider_id, []))
    target = os.path.abspath(os.path.expanduser(folder))
    if action == "add" and target not in current:
        current.append(target)
    elif action == "remove":
        current = [p for p in current if p != target]
    if current:
        folders[provider_id] = current
    else:
        folders.pop(provider_id, None)
    values["extra_scan_folders"] = folders
    save(path, values)
    return current


def save(path: Path, values: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(values, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _coerce(key: str, raw: str):
    default = DEFAULTS[key]
    if isinstance(default, bool):
        lowered = raw.strip().lower()
        if lowered in ("true", "1", "yes", "on"):
            return True
        if lowered in ("false", "0", "no", "off"):
            return False
        raise ValueError(f"{key} expects a boolean, got {raw!r}")
    if isinstance(default, int):
        return int(raw)
    if isinstance(default, float):
        value = float(raw)
        if not math.isfinite(value):
            raise ValueError(f"{key} expects a number, got {raw!r}")
        return value
    return raw


def set_value(path: Path, key: str, raw: str) -> None:
    if key not in DEFAULTS or isinstance(DEFAULTS[key], dict):
        raise KeyError(f"unknown setting: {key}")
    values = load(path)
    values[key] = _coerce(key, raw)
    save(path, values)
