"""Usage recap — mirrors upstream UsageRecap / UsageLedger tests."""

from datetime import date

import pytest

from poketokenbar.recap import compute_recap, recap_for_period, _period_bounds, _week_start


def _day(tokens: int, cost: float = 0.0) -> dict:
    return {"tokens": tokens, "cost": cost}


def _history(days: dict[str, dict]) -> dict[str, dict[str, dict]]:
    """{yyyy-mm-dd: {provider: {tokens, cost}}}"""
    out: dict[str, dict[str, dict]] = {}
    for day, providers in days.items():
        out[day] = {pid: _day(v) for pid, v in providers.items()}
    return out


# --- period bounds ---------------------------------------------------------


def test_week_bounds():
    today = date(2026, 11, 4)  # Wednesday
    start, end = _period_bounds("week", today, 0)
    assert start == date(2026, 11, 2)  # Monday
    assert end == date(2026, 11, 9)    # next Monday


def test_week_bounds_offset():
    today = date(2026, 11, 4)
    start, end = _period_bounds("week", today, -1)
    assert start == date(2026, 10, 26)
    assert end == date(2026, 11, 2)


def test_month_bounds():
    today = date(2026, 11, 15)
    start, end = _period_bounds("month", today, 0)
    assert start == date(2026, 11, 1)
    assert end == date(2026, 12, 1)


def test_month_bounds_offset():
    today = date(2026, 11, 15)
    start, end = _period_bounds("month", today, -1)
    assert start == date(2026, 10, 1)
    assert end == date(2026, 11, 1)


def test_year_bounds():
    today = date(2026, 7, 4)
    start, end = _period_bounds("year", today, 0)
    assert start == date(2026, 1, 1)
    assert end == date(2027, 1, 1)


# --- compute_recap ---------------------------------------------------------


def test_compute_recap_week_totals():
    today = date(2026, 11, 4)
    history = _history({
        "2026-11-02": {"claude_code": 1000, "codex": 500},
        "2026-11-03": {"claude_code": 2000},
        "2026-11-04": {"claude_code": 1500},
    })
    recap = compute_recap(history, "week", 0, today)
    assert recap["scope"] == "week"
    assert recap["offset"] == 0
    assert recap["total"] == 1000 + 500 + 2000 + 1500  # 5000
    assert recap["is_in_progress"] is True
    assert recap["active_days"] == 3
    assert recap["counted_days"] == 3


def test_compute_recap_previous_total():
    today = date(2026, 11, 4)
    # Current week: Nov 2-4. Previous week: Oct 26-Nov 1.
    history = _history({
        "2026-11-02": {"claude_code": 1000},
        "2026-11-03": {"claude_code": 2000},
        "2026-11-04": {"claude_code": 1500},
        "2026-10-26": {"claude_code": 800},
        "2026-10-27": {"claude_code": 900},
        "2026-10-28": {"claude_code": 700},
        "2026-10-29": {"claude_code": 600},
        "2026-10-30": {"claude_code": 500},
        "2026-10-31": {"claude_code": 400},
        "2026-11-01": {"claude_code": 300},
    })
    recap = compute_recap(history, "week", 0, today)
    # Previous week compared over same elapsed days (3): Oct 26-28 = 2400
    assert recap["previous_total"] == 2400
    # Delta = (4500 - 2400) / 2400 = 0.875
    assert recap["delta"] is not None
    assert abs(recap["delta"] - 2100 / 2400) < 1e-6


def test_compute_recap_previous_total_none_when_not_covered():
    today = date(2026, 11, 4)
    # Current week has data, but previous week's last day (Nov 1) is missing.
    history = _history({
        "2026-11-02": {"claude_code": 1000},
        "2026-11-03": {"claude_code": 2000},
        "2026-11-04": {"claude_code": 1500},
        # Oct 26-30 present, Nov 1 missing → not covered
        "2026-10-26": {"claude_code": 800},
        "2026-10-27": {"claude_code": 900},
        "2026-10-28": {"claude_code": 700},
        "2026-10-29": {"claude_code": 600},
        "2026-10-30": {"claude_code": 500},
    })
    recap = compute_recap(history, "week", 0, today)
    assert recap["previous_total"] is None
    assert recap["delta"] is None
    assert recap["can_go_back"] is False


def test_compute_recap_best_day():
    today = date(2026, 11, 4)
    history = _history({
        "2026-11-02": {"claude_code": 1000},
        "2026-11-03": {"claude_code": 5000},
        "2026-11-04": {"claude_code": 1500},
    })
    recap = compute_recap(history, "week", 0, today)
    assert recap["best_day"] is not None
    assert recap["best_day"]["key"] == "2026-11-03"
    assert recap["best_day"]["tokens"] == 5000


def test_compute_recap_best_streak():
    today = date(2026, 11, 7)  # Saturday
    history = _history({
        "2026-11-02": {"claude_code": 1000},  # Mon - active
        "2026-11-03": {"claude_code": 2000},  # Tue - active
        "2026-11-04": {},                      # Wed - no data (zero)
        "2026-11-05": {"claude_code": 500},   # Thu - active
        "2026-11-06": {"claude_code": 300},   # Fri - active
        "2026-11-07": {"claude_code": 400},   # Sat - active (today)
    })
    recap = compute_recap(history, "week", 0, today)
    # Streak: Mon-Tue = 2, break Wed, Thu-Fri-Sat = 3 → best = 3
    assert recap["best_streak"] == 3
    assert recap["active_days"] == 5


def test_compute_recap_year_scope():
    today = date(2026, 7, 4)
    history = _history({
        "2026-01-15": {"claude_code": 1000},
        "2026-06-01": {"claude_code": 2000},
        "2026-07-04": {"claude_code": 500},
    })
    recap = compute_recap(history, "year", 0, today)
    assert recap["scope"] == "year"
    assert recap["start"] == "2026-01-01"
    assert recap["end"] == "2026-12-31"
    assert recap["total"] == 3500


def test_compute_recap_empty_history():
    today = date(2026, 11, 4)
    recap = compute_recap({}, "week", 0, today)
    assert recap["total"] == 0
    assert recap["active_days"] == 0
    assert recap["best_day"] is None
    assert recap["best_streak"] == 0
    assert recap["previous_total"] is None
    assert recap["delta"] is None


# --- recap_for_period with graduated ---------------------------------------


def test_recap_for_period_graduated():
    today = date(2026, 11, 4)
    history = _history({
        "2026-11-02": {"claude_code": 1000},
        "2026-11-03": {"claude_code": 2000},
    })
    catch_log = [
        {
            "caught_at": date(2026, 11, 3).strftime("%s"),  # epoch seconds as string
            "chain": [{"species_id": 25, "name": "Pikachu"}],
            "is_shiny": False,
            "rarity": "common",
        },
        {
            "caught_at": date(2026, 10, 30).strftime("%s"),  # outside period
            "chain": [{"species_id": 1, "name": "Bulbasaur"}],
            "is_shiny": False,
            "rarity": "common",
        },
    ]
    recap = recap_for_period(history, "week", 0, today, catch_log)
    assert len(recap["graduated"]) == 1
    assert recap["graduated"][0]["species_id"] == 25
    assert recap["graduated"][0]["name"] == "Pikachu"


def test_recap_for_period_no_graduated():
    today = date(2026, 11, 4)
    history = _history({"2026-11-02": {"claude_code": 1000}})
    recap = recap_for_period(history, "week", 0, today, [])
    assert recap["graduated"] == []
