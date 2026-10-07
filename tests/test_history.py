from datetime import UTC, date, datetime

from poketokenbar import history
from poketokenbar.models import Entry


def _e(day, total, model="claude-sonnet-4-6", cost=None):
    return Entry(id=f"{day}-{total}", date=datetime(2026, 10, 1, tzinfo=UTC),
                 local_day=day, model=model, output=total, explicit_cost=cost)


class P:
    def __init__(self, pid, entries=None, boom=False):
        self.id, self._entries, self._boom = pid, entries or [], boom

    def scan_entries(self):
        if self._boom:
            raise RuntimeError("boom")
        return self._entries


def test_collect_groups_by_day_and_provider_with_reported_cost_preferred():
    errors = []
    h = history.collect([
        P("claude_code", [_e("2026-10-06", 100, cost=0.5), _e("2026-10-06", 50, cost=0.25)]),
        P("codex", [_e("2026-10-05", 10, model="unpriced")]),
        P("broken", boom=True),
    ], errors)
    assert h["2026-10-06"]["claude_code"] == {"tokens": 150, "cost": 0.75}
    assert h["2026-10-05"]["codex"]["tokens"] == 10
    assert errors and "broken" in errors[0]


def test_periods_count_every_provider_monday_week_and_calendar_month():
    h = {
        "2026-09-30": {"codex": {"tokens": 7, "cost": 0.0}},     # last month
        "2026-10-04": {"codex": {"tokens": 5, "cost": 0.1}},     # Sunday, last week
        "2026-10-05": {"codex": {"tokens": 3, "cost": 0.2},      # Monday
                       "claude_code": {"tokens": 2, "cost": 0.3}},
        "2026-10-09": {"codex": {"tokens": 100, "cost": 1.0}},   # future: ignored
    }
    p = history.periods(h, date(2026, 10, 7))
    assert p["week"]["tokens"] == 5
    assert p["month"]["tokens"] == 10
    assert round(p["month"]["cost"], 2) == 0.6


def test_payload_is_compact_and_sorted():
    out = history.payload({"2026-10-02": {"b": {"tokens": 1, "cost": 0.123456}},
                           "2026-10-01": {"a": {"tokens": 2, "cost": 0.0}}})
    assert list(out) == ["2026-10-01", "2026-10-02"]
    assert out["2026-10-02"] == {"b": [1, 0.1235]}


def test_payload_keeps_three_calendar_years():
    h = {d: {"codex": {"tokens": 1, "cost": 0.0}} for d in ("2023-12-31", "2024-01-01", "2026-10-07")}
    assert list(history.payload(h, date(2026, 10, 7))) == ["2024-01-01", "2026-10-07"]
