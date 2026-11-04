"""Usage recap — a Pokédex-style summary of any week, month or year.

Computes aggregates over a chosen calendar scope from per-day history.
Ports upstream's UsageRecap / UsageLedger (PokeTokenBar #332/#367/#368).

The ledger (per-day totals) is already collected by history.collect() and
published in state.json. This module turns it into a recap with
previous-period comparison, best day, streak, and graduated Pokémon.

A day is "covered" when it appears in the history — meaning the daemon
scanned every provider that day. A missing day means "no usage recorded",
not "unknown": the daemon re-scans every poll, so an absent day genuinely
had no entries.
"""

from __future__ import annotations

from datetime import date, timedelta

from . import history


def _week_start(d: date) -> date:
    """Monday of the week containing d (week starts Monday, upstream)."""
    return d - timedelta(days=d.weekday())


def _period_bounds(scope: str, today: date, offset: int) -> tuple[date, date]:
    """Start (inclusive) and end (exclusive) dates for a calendar period.

    `offset` steps whole periods back from the period containing today.
    """
    if scope == "year":
        start = date(today.year + offset, 1, 1)
        end = date(today.year + offset + 1, 1, 1)
    elif scope == "month":
        # Shift whole months: total months from year 0, then normalize.
        total_months = today.year * 12 + (today.month - 1) + offset
        start_y, start_m = divmod(total_months, 12)
        start = date(start_y, start_m + 1, 1)
        end_total = total_months + 1
        end_y, end_m = divmod(end_total, 12)
        end = date(end_y, end_m + 1, 1)
    else:  # week
        start = _week_start(today) + timedelta(weeks=offset)
        end = start + timedelta(days=7)
    return start, end


def _day_key(d: date) -> str:
    return d.strftime("%Y-%m-%d")


def _day_tokens(usage_history: dict[str, dict[str, dict]], d: date) -> int:
    by_provider = usage_history.get(_day_key(d), {})
    return sum(v.get("tokens", 0) for v in by_provider.values())


def _day_cost(usage_history: dict[str, dict[str, dict]], d: date) -> float:
    by_provider = usage_history.get(_day_key(d), {})
    return sum(v.get("cost", 0.0) for v in by_provider.values())


def compute_recap(
    usage_history: dict[str, dict[str, dict]],
    scope: str,
    offset: int,
    today: date | None = None,
    graduated_species: list[dict] | None = None,
) -> dict:
    """Build a recap dict for the given scope/period.

    Returns a dict with keys: scope, offset, start, end, buckets, total,
    cost, is_in_progress, previous_total, best_day, active_days,
    counted_days, best_streak, graduated, can_go_back, delta.
    """
    today = today or date.today()
    start, end = _period_bounds(scope, today, offset)

    # Every day in the period, then only the ones up to and including today
    # ("elapsed" — future days are unknown, not zero).
    period_days: list[date] = []
    cursor = start
    while cursor < end:
        period_days.append(cursor)
        cursor += timedelta(days=1)
    elapsed_days = [d for d in period_days if d <= today]

    # Buckets: one per day (week/month) — the caller already builds these
    # from usageHistory, but we compute them here too so the daemon owns the
    # source of truth and the QML can use either.
    buckets = []
    for d in period_days:
        key = _day_key(d)
        tokens = _day_tokens(usage_history, d)
        cost = _day_cost(usage_history, d)
        buckets.append({
            "key": key,
            "tokens": tokens,
            "cost": round(cost, 4),
            "is_current": d == today,
            "has_data": d <= today and key in usage_history,
        })

    total = sum(_day_tokens(usage_history, d) for d in elapsed_days)
    cost_total = round(sum(_day_cost(usage_history, d) for d in elapsed_days), 4)

    # Previous period for comparison. A running period is compared over the
    # same number of elapsed days (so the 18th of a month doesn't read as a
    # 40% drop against a full month); otherwise the whole previous period.
    prev_start, prev_end = _period_bounds(scope, today, offset - 1)
    prev_days: list[date] = []
    cursor = prev_start
    while cursor < prev_end:
        prev_days.append(cursor)
        cursor += timedelta(days=1)

    is_in_progress = start <= today < end
    compared = prev_days[: len(elapsed_days)] if is_in_progress else prev_days

    # Previous period counts only if its last day is covered — otherwise days
    # the app never saw would count as zero and invent a fake drop.
    prev_last_covered = _day_key(prev_days[-1]) in usage_history
    previous_total = (
        sum(_day_tokens(usage_history, d) for d in compared) if prev_last_covered else None
    )

    # Best day: the single elapsed day with the most tokens (> 0 only).
    best_day_entry = None
    best_tokens = 0
    for d in elapsed_days:
        t = _day_tokens(usage_history, d)
        if t > best_tokens:
            best_tokens = t
            best_day_entry = {
                "key": _day_key(d),
                "tokens": t,
                "cost": round(_day_cost(usage_history, d), 4),
            }

    # Active days (tokens > 0) and longest consecutive active streak.
    active_flags = [_day_tokens(usage_history, d) > 0 for d in elapsed_days]
    active_days = sum(active_flags)
    counted_days = sum(1 for d in elapsed_days if _day_key(d) in usage_history)

    best_streak = 0
    run = 0
    for active in active_flags:
        run = run + 1 if active else 0
        if run > best_streak:
            best_streak = run

    # Graduated Pokémon whose catch date falls inside the period.
    graduated = graduated_species or []

    delta = None
    if previous_total is not None and previous_total > 0:
        delta = (total - previous_total) / previous_total

    can_go_back = prev_last_covered

    return {
        "scope": scope,
        "offset": offset,
        "start": _day_key(start),
        "end": _day_key(end - timedelta(days=1)),
        "buckets": buckets,
        "total": total,
        "cost": cost_total,
        "is_in_progress": is_in_progress,
        "previous_total": previous_total,
        "best_day": best_day_entry,
        "active_days": active_days,
        "counted_days": counted_days,
        "best_streak": best_streak,
        "graduated": graduated,
        "can_go_back": can_go_back,
        "delta": delta,
    }


def recap_for_period(
    usage_history: dict[str, dict[str, dict]],
    scope: str,
    offset: int,
    today: date | None = None,
    catch_log: list[dict] | None = None,
) -> dict:
    """Convenience: compute_recap with graduated Pokémon drawn from the
    catch log (entries whose `caught_at` falls inside the period)."""
    graduated = []
    if catch_log:
        today = today or date.today()
        start, _ = _period_bounds(scope, today, offset)
        for entry in catch_log:
            caught = entry.get("caught_at")
            if not caught:
                continue
            # caught_at is an epoch seconds number (float/int).
            try:
                caught_day = date.fromtimestamp(float(caught))
            except (ValueError, OSError, OverflowError):
                continue
            if start <= caught_day < start + (
                timedelta(days=365) if scope == "year"
                else timedelta(days=30) if scope == "month"
                else timedelta(days=7)
            ):
                # Refine: use exact period end.
                _, end = _period_bounds(scope, today, offset)
                if start <= caught_day < end:
                    graduated.append({
                        "species_id": entry.get("chain", [{}])[-1].get("species_id")
                        if entry.get("chain")
                        else None,
                        "name": entry.get("chain", [{}])[-1].get("name")
                        if entry.get("chain")
                        else None,
                        "is_shiny": entry.get("is_shiny", False),
                        "rarity": entry.get("rarity", "common"),
                        "caught_at": caught,
                    })
    return compute_recap(usage_history, scope, offset, today, graduated)
