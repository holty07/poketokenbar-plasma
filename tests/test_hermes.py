import sqlite3
from datetime import datetime, timezone

from poketokenbar.providers.hermes import HermesProvider, _parse_database, default_home


def _make_db(path, rows):
    """`rows` is a list of dicts; missing keys default to None/0 like a real row."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, parent_session_id TEXT,"
        " started_at REAL, last_activity_at REAL, model TEXT,"
        " input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,"
        " cache_write_tokens INTEGER, reasoning_tokens INTEGER)"
    )
    for row in rows:
        conn.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row.get("id", "sess_1"),
                row.get("parent_session_id"),
                row.get("started_at"),
                row.get("last_activity_at"),
                row.get("model", "claude-sonnet-4-6"),
                row.get("input_tokens", 0),
                row.get("output_tokens", 0),
                row.get("cache_read_tokens", 0),
                row.get("cache_write_tokens", 0),
                row.get("reasoning_tokens", 0),
            ),
        )
    conn.commit()
    conn.close()


def _epoch(dt: datetime) -> float:
    return dt.timestamp()


def test_parses_session_rows_and_skips_zero_usage_sessions(tmp_path):
    db = tmp_path / "state.db"
    now = _epoch(datetime.now(tz=timezone.utc))
    _make_db(
        db,
        [
            {"id": "sess_1", "started_at": now, "last_activity_at": now,
             "input_tokens": 100, "output_tokens": 50},
            # Started but never actually called a model — zero everywhere.
            {"id": "sess_2", "started_at": now, "last_activity_at": None,
             "input_tokens": 0, "output_tokens": 0},
        ],
    )
    entries = _parse_database(db)
    assert entries is not None
    assert len(entries) == 1
    assert entries[0].input == 100
    assert entries[0].output == 50
    assert entries[0].model == "claude-sonnet-4-6"
    assert entries[0].id == "hermes|sess_1"


def test_reasoning_tokens_are_folded_into_output(tmp_path):
    db = tmp_path / "state.db"
    now = _epoch(datetime.now(tz=timezone.utc))
    _make_db(
        db,
        [{"id": "sess_1", "started_at": now, "last_activity_at": now,
          "output_tokens": 50, "reasoning_tokens": 25}],
    )
    entries = _parse_database(db)
    assert entries[0].output == 75


def test_branch_sessions_with_a_parent_are_excluded(tmp_path):
    """Mirrors Hermes's own usage_totals(): parent_session_id IS NULL, so a
    compression/branch child is not counted again on top of its lineage."""
    db = tmp_path / "state.db"
    now = _epoch(datetime.now(tz=timezone.utc))
    _make_db(
        db,
        [
            {"id": "root", "started_at": now, "last_activity_at": now, "input_tokens": 100},
            {"id": "child", "parent_session_id": "root", "started_at": now,
             "last_activity_at": now, "input_tokens": 999},
        ],
    )
    entries = _parse_database(db)
    assert [e.id for e in entries] == ["hermes|root"]


def test_local_day_derives_from_last_activity_at(tmp_path):
    db = tmp_path / "state.db"
    started = datetime(2026, 9, 1, 23, 0, tzinfo=timezone.utc)
    last_activity = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    _make_db(
        db,
        [{"id": "sess_1", "started_at": _epoch(started), "last_activity_at": _epoch(last_activity),
          "input_tokens": 10}],
    )
    entries = _parse_database(db)
    assert entries[0].local_day == last_activity.astimezone().strftime("%Y-%m-%d")


def test_local_day_falls_back_to_started_at_without_last_activity(tmp_path):
    db = tmp_path / "state.db"
    started = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    _make_db(
        db,
        [{"id": "sess_1", "started_at": _epoch(started), "last_activity_at": None,
          "input_tokens": 10}],
    )
    entries = _parse_database(db)
    assert entries[0].local_day == started.astimezone().strftime("%Y-%m-%d")


def test_provider_prices_a_recognized_model_normally(tmp_path):
    home = tmp_path
    hermes_dir = home / ".hermes"
    hermes_dir.mkdir(parents=True)
    now = datetime.now(tz=timezone.utc)
    _make_db(
        hermes_dir / "state.db",
        [{"id": "sess_1", "started_at": _epoch(now), "last_activity_at": _epoch(now),
          "model": "claude-sonnet-4-6", "input_tokens": 1_000_000, "output_tokens": 0}],
    )
    provider = HermesProvider(home=home)
    today = now.astimezone().strftime("%Y-%m-%d")
    daily = provider.fetch_daily(today=today)
    assert daily is not None
    assert daily.total_cost == 3.0  # per_million(3, 15, ...) input rate


def test_unrecognized_gateway_model_prices_at_zero(tmp_path):
    # A Hermes/OpenRouter/Nous-style free route doesn't appear in pricing.TABLE
    # and matches none of the family-fallback substrings.
    home = tmp_path
    hermes_dir = home / ".hermes"
    hermes_dir.mkdir(parents=True)
    now = datetime.now(tz=timezone.utc)
    _make_db(
        hermes_dir / "state.db",
        [{"id": "sess_1", "started_at": _epoch(now), "last_activity_at": _epoch(now),
          "model": "upstage/solar-mini4:free", "input_tokens": 1000, "output_tokens": 500}],
    )
    provider = HermesProvider(home=home)
    today = now.astimezone().strftime("%Y-%m-%d")
    daily = provider.fetch_daily(today=today)
    assert daily is not None
    assert daily.total_tokens == 1500
    assert daily.total_cost == 0.0


def test_no_database_means_no_entries(tmp_path):
    provider = HermesProvider(home=tmp_path)
    assert provider.scan_entries() == []
    assert provider.fetch_daily() is None


def test_cache_roundtrips_scan_entries(tmp_path):
    from poketokenbar.cache import ScanCache

    home = tmp_path
    hermes_dir = home / ".hermes"
    hermes_dir.mkdir(parents=True)
    now = _epoch(datetime.now(tz=timezone.utc))
    _make_db(hermes_dir / "state.db", [{"id": "sess_1", "started_at": now,
                                         "last_activity_at": now, "input_tokens": 10}])

    cache = ScanCache(tmp_path / "scan.db")
    try:
        provider = HermesProvider(cache=cache, home=home)
        first = provider.scan_entries()
        second = provider.scan_entries()
        assert len(first) == 1
        assert [e.id for e in first] == [e.id for e in second]
    finally:
        cache.close()


def test_default_home_respects_hermes_home_env_var(monkeypatch, tmp_path):
    override = tmp_path / "custom-hermes"
    monkeypatch.setenv("HERMES_HOME", str(override))
    monkeypatch.delenv("HERMES_DATA_DIR_SUFFIX", raising=False)
    assert default_home() == override


def test_default_home_falls_back_to_dot_hermes(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.delenv("HERMES_DATA_DIR_SUFFIX", raising=False)
    assert default_home(home=tmp_path) == tmp_path / ".hermes"
