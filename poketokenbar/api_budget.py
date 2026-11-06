"""Monthly Anthropic API-key budget — spend Hermes billed to api.anthropic.com.

Hermes tags every session with the backend it was billed through
(`sessions.billing_provider` / `billing_base_url`) and its own dollar figure
(`actual_cost_usd` when the provider reported one, else `estimated_cost_usd`
from Hermes's pricing snapshot). Sessions routed through the user's own
Anthropic key carry `billing_provider = 'anthropic'`; everything else
(OpenRouter, Nous free tier, NVIDIA, a local proxy) is someone else's bill and
is left out.

Unlike HermesProvider, which reads only the most recently written profile
database (today's token counts), this sums *every* profile's `state.db`: the
API key — and so the budget — is shared by all of them.

The budget period is a calendar month starting on `reset_day` (1..28), local
time. A session counts toward the period of its last activity, the same
attribution HermesProvider uses. Like Hermes's own `usage_totals()`, only
top-level sessions count (`parent_session_id IS NULL`); compression/branch
children would double the total.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time as dtime
from pathlib import Path

from . import format as fmt
from . import limits, pricing
from .providers.hermes import _count, _open_readonly, default_home

PROVIDER = "anthropic"


@dataclass
class BudgetUsage:
    spent: float
    sessions: int
    period_start: date
    period_end: date  # exclusive: the next reset


def all_databases(home: Path | None = None) -> list[Path]:
    """Every Hermes `state.db`: the top-level one and one per profile."""
    root = default_home(home=home)
    found: list[Path] = []
    if (root / "state.db").is_file():
        found.append(root / "state.db")
    try:
        for child in sorted((root / "profiles").iterdir()):
            db = child / "state.db"
            if child.is_dir() and db.is_file():
                found.append(db)
    except OSError:
        pass
    return found


def _clamp_day(reset_day) -> int:
    try:
        day = int(reset_day)
    except (TypeError, ValueError):
        return 1
    return min(max(day, 1), 28)


def period(today: date, reset_day: int = 1) -> tuple[date, date]:
    """`[start, end)` of the budget month containing `today`."""
    day = _clamp_day(reset_day)
    start = today.replace(day=day)
    if today < start:
        start = (start.replace(year=start.year - 1, month=12) if start.month == 1
                 else start.replace(month=start.month - 1))
    end = (start.replace(year=start.year + 1, month=1) if start.month == 12
           else start.replace(month=start.month + 1))
    return start, end


def _session_cost(row: tuple) -> float:
    (model, actual, estimated, inp, out, reasoning, cache_read, cache_write) = row
    for value in (actual, estimated):
        if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value < 1e6:
            return float(value)
    # Hermes didn't price it (an unknown model, an old row): fall back to ours.
    return pricing.cost(
        model or "unknown", _count(inp), _count(out) + _count(reasoning),
        _count(cache_write), _count(cache_read),
    )


def _read(db: Path, start_epoch: float, end_epoch: float) -> list[tuple] | None:
    conn = _open_readonly(db)
    if conn is None:
        return None
    try:
        return conn.execute(
            """SELECT model, actual_cost_usd, estimated_cost_usd,
                      input_tokens, output_tokens, reasoning_tokens,
                      cache_read_tokens, cache_write_tokens
                 FROM sessions
                WHERE parent_session_id IS NULL
                  AND lower(billing_provider) = ?
                  AND coalesce(last_activity_at, started_at) >= ?
                  AND coalesce(last_activity_at, started_at) < ?""",
            (PROVIDER, start_epoch, end_epoch),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        # An older Hermes without billing columns has no API-key spend to report.
        if "no such" in str(exc):
            return []
        return None
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def usage(today: date, reset_day: int = 1, home: Path | None = None,
          errors: list[str] | None = None) -> BudgetUsage:
    start, end = period(today, reset_day)
    start_epoch = datetime.combine(start, dtime.min).astimezone().timestamp()
    end_epoch = datetime.combine(end, dtime.min).astimezone().timestamp()
    spent = 0.0
    sessions = 0
    for db in all_databases(home=home):
        rows = _read(db, start_epoch, end_epoch)
        if rows is None:
            if errors is not None:
                errors.append(f"api_budget: could not read {db}")
            continue
        for row in rows:
            cost = _session_cost(row)
            if cost > 0:
                spent += cost
                sessions += 1
    return BudgetUsage(spent=spent, sessions=sessions, period_start=start, period_end=end)


def payload(used: BudgetUsage, budget: float, warn: float = 80, crit: float = 95) -> dict:
    """The state.json `api_budget` block; empty when no budget is set."""
    if not budget or budget <= 0:
        return {}
    percent = round(used.spent / budget * 100, 1)
    remaining = budget - used.spent
    return {
        "provider": PROVIDER,
        "spent": round(used.spent, 4),
        "budget": budget,
        "remaining": round(remaining, 4),
        "percent": percent,
        "level": limits.level(percent, warn, crit),
        "sessions": used.sessions,
        "period_start": used.period_start.isoformat(),
        # Local midnight of the next reset, for the popup's "resets in" label.
        "resets_at": datetime.combine(used.period_end, dtime.min).astimezone().isoformat(),
        "spent_text": fmt.cost(used.spent),
        "budget_text": fmt.cost(budget),
        "remaining_text": fmt.cost(max(remaining, 0.0)),
        "over_text": fmt.cost(-remaining) if remaining < 0 else "",
    }
