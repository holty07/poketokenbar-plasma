"""Per-day usage history across providers — the data behind the daily trend
(upstream #270/#395/#348) and the week/month/year recap (#332/#367/#368).

One pass over every provider's parsed entries produces
`{local day: {provider id: {"tokens", "cost"}}}`. Week and month totals are
derived from it too, so every provider counts toward them — previously only
providers that implemented their own fetch_periods() did, which left Codex
out of the week and month figures.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from . import pricing
from .models import Entry


def collect(providers, errors: list[str] | None = None) -> dict[str, dict[str, dict]]:
    history: dict[str, dict[str, dict]] = {}
    for provider in providers:
        scan = getattr(provider, "scan_entries", None)
        if scan is None:
            continue
        try:
            entries = scan()
        except Exception as exc:  # noqa: BLE001 - per-provider isolation
            if errors is not None:
                errors.append(f"{provider.id} history: {exc}")
            continue
        for e in entries:
            if not isinstance(e, Entry) or e.total <= 0:
                continue
            slot = history.setdefault(e.local_day, {}).setdefault(
                provider.id, {"tokens": 0, "cost": 0.0}
            )
            slot["tokens"] += e.total
            slot["cost"] += pricing.entry_cost(e)
    return history


def _day(text: str) -> date | None:
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()  # noqa: DTZ007 - a calendar date
    except ValueError:
        return None


def periods(history: dict[str, dict[str, dict]], today: date) -> dict:
    """Week-to-date (Monday start, as upstream) and month-to-date totals."""
    week_start = today - timedelta(days=today.weekday())
    week = {"tokens": 0, "cost": 0.0}
    month = {"tokens": 0, "cost": 0.0}
    for text, by_provider in history.items():
        day = _day(text)
        if day is None or day > today:
            continue
        tokens = sum(v["tokens"] for v in by_provider.values())
        cost = sum(v["cost"] for v in by_provider.values())
        if (day.year, day.month) == (today.year, today.month):
            month["tokens"] += tokens
            month["cost"] += cost
        if week_start <= day:
            week["tokens"] += tokens
            week["cost"] += cost
    return {"week": week, "month": month}


def payload(history: dict[str, dict[str, dict]]) -> dict:
    """Compact form for state.json: day -> provider -> [tokens, cost]. The
    popup aggregates it into the month trend and any recap period."""
    return {
        day: {pid: [v["tokens"], round(v["cost"], 4)] for pid, v in sorted(by.items())}
        for day, by in sorted(history.items())
    }
