import json
import sqlite3
from datetime import datetime, timezone

from poketokenbar import pricing
from poketokenbar.providers.crush import CrushProvider, _parse_database, default_dbs


def _make_db(path, sessions, messages=()):
    """`sessions`: (id, parent, prompt, completion, cost, updated_at).
    `messages`: (id, session_id, role, model, created_at[, parts_size[, is_summary]])."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, parent_session_id TEXT,"
        " title TEXT NOT NULL DEFAULT '', message_count INTEGER NOT NULL DEFAULT 0,"
        " prompt_tokens INTEGER NOT NULL DEFAULT 0, completion_tokens INTEGER NOT NULL DEFAULT 0,"
        " cost REAL NOT NULL DEFAULT 0.0, updated_at INTEGER NOT NULL, created_at INTEGER NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE messages (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, role TEXT NOT NULL,"
        " parts TEXT NOT NULL DEFAULT '[]', model TEXT, created_at INTEGER NOT NULL,"
        " updated_at INTEGER NOT NULL, provider TEXT, is_summary_message INTEGER NOT NULL DEFAULT 0)"
    )
    for sid, parent, prompt, completion, cost, updated in sessions:
        conn.execute(
            "INSERT INTO sessions (id, parent_session_id, prompt_tokens, completion_tokens, cost,"
            " updated_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (sid, parent, prompt, completion, cost, updated, updated),
        )
    for mid, sid, role, model, created, *rest in messages:
        size = rest[0] if rest else 2
        summary = rest[1] if len(rest) > 1 else 0
        conn.execute(
            "INSERT INTO messages (id, session_id, role, parts, model, created_at, updated_at,"
            " is_summary_message) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (mid, sid, role, "x" * size, model, created, created, summary),
        )
    conn.commit()
    conn.close()


def _write_index(home, data_dirs):
    index = home / ".local" / "share" / "crush" / "projects.json"
    index.parent.mkdir(parents=True, exist_ok=True)
    index.write_text(json.dumps({"projects": [{"path": str(d.parent), "data_dir": str(d)} for d in data_dirs]}))


def _ts(day):
    return int(datetime.strptime(day + " 12:00", "%Y-%m-%d %H:%M").timestamp())


def test_estimates_each_request_from_context_growth(tmp_path):
    db = tmp_path / ".crush" / "crush.db"
    _make_db(
        db,
        [("s1", None, 10000, 120, 0.30, 1_792_652_800)],
        [
            ("u1", "s1", "user", None, 1_792_652_000, 1000),
            ("a1", "s1", "assistant", "old-model", 1_792_652_100, 400),
            ("t1", "s1", "tool", None, 1_792_652_200, 2600),
            ("a2", "s1", "assistant", "hyper", 1_792_652_300, 800),
            ("t2", "s1", "tool", None, 1_792_652_400, 1200),
            ("a3", "s1", "assistant", "hyper", 1_792_652_500, 100),
        ],
    )
    entries = {e.id: e for e in _parse_database(db)}
    assert set(entries) == {"crush|s1|a1", "crush|s1|a2", "crush|s1|a3"}
    # Context before each request: 1000, 4000, 6000 bytes; the last matches
    # the measured counter exactly.
    a1, a2, a3 = entries["crush|s1|a1"], entries["crush|s1|a2"], entries["crush|s1|a3"]
    assert (a1.input, a2.input, a3.input) == (1666, 6666, 10000)
    assert (a1.output, a2.output, a3.output) == (100, 200, 120)
    assert a1.model == "old-model" and a3.model == "hyper"
    assert a1.date == datetime.fromtimestamp(1_792_652_100, tz=timezone.utc)
    # Accumulated cost is shared out by estimated tokens.
    assert abs(sum(e.explicit_cost for e in entries.values()) - 0.30) < 1e-9
    assert a3.explicit_cost > a1.explicit_cost


def test_summary_restarts_context(tmp_path):
    db = tmp_path / ".crush" / "crush.db"
    _make_db(
        db,
        [("s1", None, 3000, 50, 0.0, 1_792_652_800)],
        [
            ("u1", "s1", "user", None, 1_792_652_000, 9000),
            ("a1", "s1", "assistant", "m", 1_792_652_100, 1000),
            ("sum", "s1", "assistant", "m", 1_792_652_200, 500, 1),
            ("u2", "s1", "user", None, 1_792_652_300, 1000),
            ("a2", "s1", "assistant", "m", 1_792_652_400, 100),
        ],
    )
    entries = {e.id: e for e in _parse_database(db)}
    # The summary itself is not a request; a1 saw 9000 bytes against a2's
    # 1500 after the restart, and so estimates larger than the final counter.
    assert set(entries) == {"crush|s1|a1", "crush|s1|a2"}
    assert entries["crush|s1|a2"].input == 3000
    assert entries["crush|s1|a1"].input == 18000
    assert all(e.explicit_cost is None for e in entries.values())


def test_requests_split_across_days(tmp_path):
    a = tmp_path / "a" / ".crush"
    _make_db(
        a / "crush.db",
        [("s1", None, 2000, 10, 0.0, _ts("2026-10-22"))],
        [
            ("u1", "s1", "user", None, _ts("2026-10-21") - 60, 1000),
            ("a1", "s1", "assistant", "m", _ts("2026-10-21"), 1000),
            ("a2", "s1", "assistant", "m", _ts("2026-10-22"), 1000),
        ],
    )
    _write_index(tmp_path, [a])
    provider = CrushProvider(home=tmp_path)
    assert provider.fetch_daily("2026-10-21").input_tokens == 1000
    assert provider.fetch_daily("2026-10-22").input_tokens == 2000


def test_session_without_messages_uses_counters(tmp_path):
    db = tmp_path / ".crush" / "crush.db"
    _make_db(db, [("s1", None, 30000, 120, 0.33, 1_792_652_732)])
    (e,) = _parse_database(db)
    assert e.id == "crush|s1"
    assert (e.input, e.output, e.model, e.explicit_cost) == (30000, 120, "unknown", 0.33)


def test_zero_cost_falls_back_to_price_table_and_empty_sessions_skipped(tmp_path):
    db = tmp_path / ".crush" / "crush.db"
    _make_db(
        db,
        [
            ("s1", None, 1000, 10, 0.0, 1_792_652_732),
            ("child$$call-1", "s1", 500, 5, 0.0, 1_792_652_733),
            ("empty", None, 0, 0, 0.0, 1_792_652_734),
            ("negative", None, -5, 0, 0.0, 1_792_652_735),
        ],
    )
    entries = {e.id: e for e in _parse_database(db)}
    assert set(entries) == {"crush|s1", "crush|child$$call-1"}
    assert entries["crush|s1"].explicit_cost is None
    assert entries["crush|s1"].model == "unknown"


def test_not_a_crush_db_is_empty(tmp_path):
    db = tmp_path / "crush.db"
    sqlite3.connect(db).close()
    assert _parse_database(db) == []


def test_index_discovery_dedups_and_skips_missing(tmp_path):
    a = tmp_path / "a" / ".crush"
    b = tmp_path / "b" / ".crush"
    _make_db(a / "crush.db", [])
    _write_index(tmp_path, [a, a, b])  # b has no database
    assert default_dbs(home=tmp_path) == [a / "crush.db"]


def test_missing_index_means_no_usage(tmp_path):
    provider = CrushProvider(home=tmp_path)
    assert provider.scan_entries() == []
    assert provider.fetch_daily("2026-10-22") is None


def test_fetch_daily_sums_across_projects(tmp_path):
    a = tmp_path / "a" / ".crush"
    b = tmp_path / "b" / ".crush"
    _make_db(a / "crush.db", [("s1", None, 1000, 100, 0.5, _ts("2026-10-22"))])
    _make_db(
        b / "crush.db",
        [
            ("s2", None, 2000, 200, 0.25, _ts("2026-10-22")),
            ("s3", None, 9999, 999, 9.0, _ts("2026-10-21")),
        ],
    )
    _write_index(tmp_path, [a, b])
    daily = CrushProvider(home=tmp_path).fetch_daily("2026-10-22")
    assert daily.input_tokens == 3000
    assert daily.output_tokens == 300
    assert daily.total_tokens == 3300
    assert abs(daily.total_cost - 0.75) < 1e-9


def test_fetch_periods(tmp_path):
    a = tmp_path / "a" / ".crush"
    _make_db(
        a / "crush.db",
        [
            ("s1", None, 1000, 0, 1.0, _ts("2026-10-22")),  # Thursday
            ("s2", None, 500, 0, 1.0, _ts("2026-10-19")),  # Monday
            ("s3", None, 200, 0, 1.0, _ts("2026-10-02")),
        ],
    )
    _write_index(tmp_path, [a])
    periods = CrushProvider(home=tmp_path).fetch_periods("2026-10-22")
    assert periods["week"] == {"tokens": 1500, "cost": 2.0}
    assert periods["month"] == {"tokens": 1700, "cost": 3.0}


def test_cache_round_trip_keeps_explicit_cost(tmp_path):
    from poketokenbar.cache import ScanCache

    a = tmp_path / "a" / ".crush"
    _make_db(a / "crush.db", [("s1", None, 1000, 10, 0.5, _ts("2026-10-22"))])
    _write_index(tmp_path, [a])
    cache = ScanCache(tmp_path / "scan.db")
    try:
        first = CrushProvider(cache=cache, home=tmp_path).scan_entries()
        second = CrushProvider(cache=cache, home=tmp_path).scan_entries()
    finally:
        cache.close()
    assert first == second
    assert pricing.entry_cost(second[0]) == 0.5
