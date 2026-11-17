import json
import sqlite3
from datetime import date, datetime

from poketokenbar import api_budget, config
from poketokenbar.daemon import Daemon
from poketokenbar.notify import Notifier


def _make_db(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, parent_session_id TEXT,"
        " started_at REAL, last_activity_at REAL, model TEXT,"
        " input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,"
        " cache_write_tokens INTEGER, reasoning_tokens INTEGER,"
        " billing_provider TEXT, billing_base_url TEXT,"
        " estimated_cost_usd REAL, actual_cost_usd REAL)"
    )
    for i, row in enumerate(rows):
        conn.execute(
            "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row.get("id", f"s{i}"), row.get("parent"),
                row["at"], row["at"], row.get("model", "claude-opus-5-5"),
                row.get("input", 0), row.get("output", 0), row.get("cache_read", 0),
                row.get("cache_write", 0), 0,
                row.get("provider", "anthropic"), "https://api.anthropic.com",
                row.get("estimated"), row.get("actual"),
            ),
        )
    conn.commit()
    conn.close()


def _at(y, m, d, hour=12):
    return datetime(y, m, d, hour).astimezone().timestamp()


def test_period_handles_reset_day_and_year_wrap():
    assert api_budget.period(date(2026, 11, 6)) == (date(2026, 11, 1), date(2026, 12, 1))
    assert api_budget.period(date(2026, 12, 31)) == (date(2026, 12, 1), date(2027, 1, 1))
    assert api_budget.period(date(2026, 1, 10), 15) == (date(2025, 12, 15), date(2026, 1, 15))
    assert api_budget.period(date(2026, 3, 20), 15) == (date(2026, 3, 15), date(2026, 4, 15))
    # Out-of-range days clamp instead of crashing on short months.
    assert api_budget.period(date(2026, 2, 28), 31)[0] == date(2026, 2, 28)


def test_sums_only_this_months_top_level_anthropic_sessions_across_profiles(tmp_path):
    home = tmp_path / ".hermes"
    _make_db(home / "state.db", [
        {"at": _at(2026, 11, 3), "estimated": 1.25},
        {"at": _at(2026, 11, 4), "estimated": 9.0, "actual": 2.0},  # actual wins
        {"at": _at(2026, 11, 4), "estimated": 5.0, "parent": "s0"},  # child: skipped
        {"at": _at(2026, 10, 30), "estimated": 7.0},  # last month
        {"at": _at(2026, 11, 5), "estimated": 3.0, "provider": "openrouter"},
    ])
    _make_db(home / "profiles" / "overseer" / "state.db", [
        {"at": _at(2026, 11, 5), "estimated": 0.75},
    ])
    used = api_budget.usage(date(2026, 11, 6), 1, home=tmp_path)
    assert used.spent == 4.0
    assert used.sessions == 3


def test_falls_back_to_our_pricing_when_hermes_did_not_price(tmp_path):
    home = tmp_path / ".hermes"
    _make_db(home / "state.db", [
        {"at": _at(2026, 11, 6), "model": "claude-opus-5-5", "output": 1_000_000},
    ])
    used = api_budget.usage(date(2026, 11, 6), home=tmp_path)
    assert round(used.spent, 2) == 20.0


def test_old_schema_without_billing_columns_reports_nothing(tmp_path):
    db = tmp_path / ".hermes" / "state.db"
    db.parent.mkdir(parents=True)
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE sessions (id TEXT, parent_session_id TEXT, model TEXT)")
    conn.commit()
    conn.close()
    errors = []
    assert api_budget.usage(date(2026, 11, 6), home=tmp_path, errors=errors).spent == 0
    assert errors == []


def test_payload_levels_and_overspend():
    used = api_budget.BudgetUsage(34.0, 10, date(2026, 11, 1), date(2026, 12, 1))
    block = api_budget.payload(used, 40.0)
    assert block["percent"] == 85.0
    assert block["level"] == "warn"
    assert block["spent_text"] == "$34.00"
    assert block["remaining_text"] == "$6.00"
    assert block["over_text"] == ""
    over = api_budget.payload(api_budget.BudgetUsage(42.5, 1, used.period_start, used.period_end), 40.0)
    assert over["level"] == "crit"
    assert over["remaining_text"] == "$0.00"
    assert over["over_text"] == "$2.50"
    assert api_budget.payload(used, 0) == {}


def test_daemon_publishes_budget_and_notifies_once(tmp_path):
    today = date.today()
    _make_db(tmp_path / ".hermes" / "state.db", [
        {"at": datetime(today.year, today.month, today.day).astimezone().timestamp(),
         "estimated": 36.0},
    ])
    config.save(tmp_path / "config.json", {"api_budget_usd": 40.0})
    sent = []
    d = Daemon(
        state_path=tmp_path / "state.json",
        config_path=tmp_path / "config.json",
        cache=None,
        providers=[],
        notifier=Notifier(send_fn=lambda *a, **k: sent.append(a[0])),
        hermes_home=tmp_path,
    )
    d.poll_once()
    d.poll_once()
    written = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert written["api_budget"]["percent"] == 90.0
    assert sent == ["Anthropic API budget at 90%"]


def test_daemon_omits_budget_when_unset(tmp_path):
    d = Daemon(
        state_path=tmp_path / "state.json",
        config_path=tmp_path / "config.json",
        cache=None,
        providers=[],
        hermes_home=tmp_path,
    )
    assert d.poll_once()["api_budget"] == {}


def _add_usage(path, rows):
    """Hermes's per-call ledger: one row per (session, model, provider, task)."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS session_model_usage (session_id TEXT, model TEXT,"
        " billing_provider TEXT, billing_base_url TEXT, billing_mode TEXT, task TEXT,"
        " api_call_count INTEGER, input_tokens INTEGER, output_tokens INTEGER,"
        " cache_read_tokens INTEGER, cache_write_tokens INTEGER, reasoning_tokens INTEGER,"
        " estimated_cost_usd REAL, actual_cost_usd REAL, cost_status TEXT, cost_source TEXT,"
        " first_seen REAL, last_seen REAL)"
    )
    for row in rows:
        conn.execute(
            "INSERT INTO session_model_usage VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row["session"], row.get("model", "claude-sonnet-5-5"),
                row.get("provider", "anthropic"), "https://api.anthropic.com", "",
                row.get("task", ""), 1, 0, row.get("output", 0), 0, 0, 0,
                row.get("estimated", 0.0), row.get("actual", 0.0), None, None,
                row["at"], row["at"],
            ),
        )
    conn.commit()
    conn.close()


def test_usage_ledger_counts_anthropic_calls_whatever_the_session_label(tmp_path):
    db = tmp_path / ".hermes" / "state.db"
    _make_db(db, [
        # Started on NVIDIA, later /model'd to Sonnet on the key: the row says nvidia.
        {"id": "switched", "at": _at(2026, 11, 8), "provider": "nvidia", "estimated": 6.0},
        {"id": "plain", "at": _at(2026, 11, 9), "estimated": 1.0},
        {"id": "child", "at": _at(2026, 11, 9), "parent": "plain"},
    ])
    _add_usage(db, [
        {"session": "switched", "at": _at(2026, 11, 8), "estimated": 6.0},
        {"session": "switched", "at": _at(2026, 11, 8), "provider": "nvidia",
         "model": "nvidia/nemotron"},
        {"session": "switched", "at": _at(2026, 11, 8), "task": "background_review",
         "estimated": 1.5},
        # 0.0 is Hermes's "no billed figure", not a free call.
        {"session": "plain", "at": _at(2026, 11, 9), "estimated": 1.0, "actual": 0.0},
        # A compression child's own calls are not rolled into its parent.
        {"session": "child", "at": _at(2026, 11, 9), "estimated": 0.25},
        {"session": "plain", "at": _at(2026, 10, 30), "estimated": 9.0},  # last month
        {"session": "plain", "at": _at(2026, 11, 9), "provider": "openrouter",
         "task": "vision", "estimated": 4.0},
    ])
    used = api_budget.usage(date(2026, 11, 16), home=tmp_path)
    assert round(used.spent, 2) == 8.75
    assert used.sessions == 3


def test_usage_ledger_prefers_billed_cost_and_prices_unpriced_rows(tmp_path):
    db = tmp_path / ".hermes" / "state.db"
    _make_db(db, [{"id": "a", "at": _at(2026, 11, 6), "provider": "nvidia"}])
    _add_usage(db, [
        {"session": "a", "at": _at(2026, 11, 6), "estimated": 9.0, "actual": 2.0},
        {"session": "a", "at": _at(2026, 11, 6), "model": "claude-opus-5-5",
         "output": 1_000_000},
    ])
    assert round(api_budget.usage(date(2026, 11, 6), home=tmp_path).spent, 2) == 22.0


def test_ledger_is_authoritative_over_session_totals(tmp_path):
    db = tmp_path / ".hermes" / "state.db"
    # The session row is unpriced (our pricing would say $20); the ledger
    # already holds the same call, so it must not be counted twice.
    _make_db(db, [{"id": "a", "at": _at(2026, 11, 10), "model": "claude-opus-5-5",
                   "output": 1_000_000}])
    _add_usage(db, [{"session": "a", "at": _at(2026, 11, 10), "model": "claude-opus-5-5",
                     "output": 1_000_000}])
    used = api_budget.usage(date(2026, 11, 16), home=tmp_path)
    assert round(used.spent, 2) == 20.0
    assert used.sessions == 1
