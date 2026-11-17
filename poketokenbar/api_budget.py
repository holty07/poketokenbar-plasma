"""Monthly Anthropic API-key budget — spend Hermes billed to api.anthropic.com.

Hermes records every API call's usage in `session_model_usage`, one row per
(session, model, billing provider, task), keyed by the route that was live for
*that call*. That table is the source of truth here, because the `sessions`
row cannot be:

- `sessions.billing_provider` is a single label per session, fixed by its
  first billed route. A session that started on another provider and later
  switched (/model) to Claude on the user's key keeps the other label, and
  all its Anthropic spend vanishes.
- Auxiliary calls (background_review, compression, vision, title generation)
  land only in `session_model_usage`, never in the session's totals.

Rows count when `billing_provider = 'anthropic'`; everything else (OpenRouter,
Nous free tier, NVIDIA, a local proxy) is someone else's bill. Each row holds
only its own session's calls (compression children get rows of their own), so
every row counts, child sessions included. A row's dollar figure is
`actual_cost_usd` when positive (Hermes writes 0.0, not NULL, when the
provider reported none), else `estimated_cost_usd`, else our own pricing.

Every Hermes profile's `state.db` is summed: the API key, and so the budget,
is shared by all of them. An older Hermes without `session_model_usage` falls
back to top-level `sessions` rows billed to Anthropic.

The budget period is a calendar month starting on `reset_day` (1..28), local
time. A usage row counts toward the period of its `last_seen`, a session toward
that of its last activity. Rows are running totals per session, so a session
that spans a reset lands wholly in the later period.
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


def _positive(value) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 < value < 1e6:
        return float(value)
    return None


def _row_cost(model, actual, estimated, inp, out, reasoning, cache_read, cache_write) -> float:
    """A row's dollars: Hermes's billed figure, then its estimate, then ours."""
    for value in (actual, estimated):
        cost = _positive(value)
        if cost is not None:
            return cost
    # Hermes didn't price it (an unknown model, an old row): fall back to ours.
    return pricing.cost(
        model or "unknown", _count(inp), _count(out) + _count(reasoning),
        _count(cache_write), _count(cache_read),
    )


_COST_COLUMNS = """model, actual_cost_usd, estimated_cost_usd,
                   input_tokens, output_tokens, reasoning_tokens,
                   cache_read_tokens, cache_write_tokens"""


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None


def _from_usage_table(conn: sqlite3.Connection, start: float, end: float) -> dict[str, float]:
    """Per-session Anthropic spend from Hermes's per-call ledger."""
    spent: dict[str, float] = {}
    for session_id, *row in conn.execute(
        f"""SELECT session_id, {_COST_COLUMNS}
              FROM session_model_usage
             WHERE lower(billing_provider) = ?
               AND last_seen >= ? AND last_seen < ?""",
        (PROVIDER, start, end),
    ):
        spent[session_id] = spent.get(session_id, 0.0) + _row_cost(*row)
    return spent


def _from_sessions(conn: sqlite3.Connection, start: float, end: float) -> dict[str, float]:
    """Older Hermes without `session_model_usage`: top-level sessions only,
    since compression/branch children would double the total."""
    return {
        session_id: _row_cost(*row)
        for session_id, *row in conn.execute(
            f"""SELECT id, {_COST_COLUMNS}
                  FROM sessions
                 WHERE parent_session_id IS NULL
                   AND lower(billing_provider) = ?
                   AND coalesce(last_activity_at, started_at) >= ?
                   AND coalesce(last_activity_at, started_at) < ?""",
            (PROVIDER, start, end),
        )
    }


def _read(db: Path, start_epoch: float, end_epoch: float) -> dict[str, float] | None:
    """`{session id: dollars}` billed to Anthropic in the period, or None when
    the database could not be read."""
    conn = _open_readonly(db)
    if conn is None:
        return None
    try:
        if _has_table(conn, "session_model_usage"):
            return _from_usage_table(conn, start_epoch, end_epoch)
        return _from_sessions(conn, start_epoch, end_epoch)
    except sqlite3.OperationalError as exc:
        # An older Hermes without billing columns has no API-key spend to report.
        if "no such" in str(exc):
            return {}
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
        per_session = _read(db, start_epoch, end_epoch)
        if per_session is None:
            if errors is not None:
                errors.append(f"api_budget: could not read {db}")
            continue
        for cost in per_session.values():
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
