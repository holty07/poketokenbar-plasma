"""state.json — the daemon's only output to the UI.

Written atomically (temp + rename) so the plasmoid, which polls, can never
read a torn file. Always parses: a failing poll produces errors[], never a
partial document.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from . import format as fmt
from . import l10n, limits
from .models import DailyUsage

SCHEMA_VERSION = 1


def default_path() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state")
    return Path(base) / "poketokenbar" / "state.json"


def _limits_payload(status) -> dict:
    """Serialise a limits.LimitStatus, or an empty dict when unavailable."""
    if status is None:
        return {}

    def window(w):
        if w is None:
            return None
        return {
            "utilization": w.utilization,
            "resets_at": w.resets_at,
            "severity": w.severity,
        }

    return {
        "session": window(status.session),
        "weekly": window(status.weekly),
        "plan": status.subscription_type,
        "account": status.account or {},
    }


def _limit_windows(limit_status, mode: str, warn: float, crit: float) -> list[dict]:
    return [
        {
            "value": w.utilization,
            "text": limits.format_percent(w.utilization),
            "level": limits.level(w.utilization, warn, crit),
        }
        for w in limits.windows(limit_status, mode)
    ]


def _evolution_windows(companion_payload: dict | None, warn: float, crit: float) -> list[dict]:
    """One progress window toward the companion's next evolution or
    graduation — hatch progress while it's still an egg. `goal` distinguishes
    this from a limits window, e.g. for the floating pet's bubble copy."""
    if not companion_payload:
        return []
    stage = companion_payload.get("stage")
    if stage == "egg":
        progress = companion_payload.get("egg_progress")
        goal = "hatch"
    elif stage == "mon":
        progress = companion_payload.get("stage_progress")
        goal = "graduation" if companion_payload.get("is_final_form") else "evolution"
    else:
        return []
    if progress is None:
        return []
    value = round(progress * 100, 1)
    text = limits.format_percent(value)
    remaining_text = companion_payload.get("remaining_text")
    if remaining_text:
        text = f"{text} ({remaining_text})"
    return [
        {
            "value": value,
            "text": text,
            "level": limits.level(value, warn, crit),
            "goal": goal,
        }
    ]


def build(
    daily_by_provider: dict[str, DailyUsage],
    config_values: dict,
    errors: list[str],
    scanning: bool = False,
    limit_status=None,
    companion_payload: dict | None = None,
    shop_payload: list | None = None,
    bag_payload: list | None = None,
    dex_payload: list | None = None,
    catch_log: list | None = None,
    rarity_counts: dict | None = None,
    catch_counts: dict | None = None,
    periods: dict | None = None,
    burn: dict | None = None,
    provider_status: dict | None = None,
    celebration: dict | None = None,
) -> dict:
    total_tokens = sum(d.total_tokens for d in daily_by_provider.values())
    total_cost = sum(d.total_cost for d in daily_by_provider.values())
    limit_mode = config_values.get("limit_display_mode", "both")

    return {
        "schema_version": SCHEMA_VERSION,
        "updated_at": time.time(),
        "scanning": scanning,
        "errors": errors,
        "today": {
            "total_tokens": total_tokens,
            "total_cost": total_cost,
            "tokens_grouped": fmt.grouped(total_tokens),
            "tokens_compact": fmt.compact(total_tokens),
            "cost_text": fmt.cost(total_cost),
        },
        "providers": {
            pid: {
                "total_tokens": d.total_tokens,
                # Preformatted: QML's Number.toLocaleString() renders large
                # values as 8.55336e+07. Formatting belongs in one place.
                "total_tokens_text": fmt.grouped(d.total_tokens),
                "total_tokens_compact": fmt.compact(d.total_tokens),
                "total_cost": d.total_cost,
                "input_tokens": d.input_tokens,
                "output_tokens": d.output_tokens,
                "cache_creation_tokens": d.cache_creation_tokens,
                "cache_read_tokens": d.cache_read_tokens,
            }
            for pid, d in daily_by_provider.items()
        },
        "limits": _limits_payload(limit_status),
        "companion": companion_payload or {},
        "shop": shop_payload or [],
        "bag": bag_payload or [],
        "dex": dex_payload or [],
        "catch_log": catch_log or [],
        "rarity_counts": rarity_counts or {},
        "catch_counts": catch_counts or {},
        "periods": periods or {},
        "strings": l10n.catalogue(config_values.get("language", "en")),
        "celebration": celebration or {},
        "burn": burn or {},
        "provider_status": provider_status or {},
        "panel": {
            "tokens_text": fmt.compact(total_tokens)
            if config_values.get("show_tokens_in_menu")
            else "",
            "cost_text": fmt.cost_compact(total_cost)
            if config_values.get("show_cost_in_menu")
            else "",
            "limit_text": limits.panel_text(limit_status, limit_mode)
            if config_values.get("show_limit_in_menu")
            else "",
            # Structured form so the panel can colour each number on its own.
            # Holds either official limit utilization or evolution/graduation
            # progress, per panel_percent_source — same {value, text, level}
            # shape either way, so the panel widgets render it generically.
            "limit_windows": (
                _evolution_windows(
                    companion_payload,
                    config_values.get("warn_threshold", 80),
                    config_values.get("crit_threshold", 95),
                )
                if config_values.get("panel_percent_source", "limits") == "evolution"
                else _limit_windows(
                    limit_status,
                    limit_mode,
                    config_values.get("warn_threshold", 80),
                    config_values.get("crit_threshold", 95),
                )
            )
            if config_values.get("show_limit_in_menu")
            else [],
            "sprite_path": (companion_payload or {}).get("sprite_path", ""),
        },
    }


def write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)
