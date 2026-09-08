import json
import sqlite3
from datetime import datetime, timezone

from poketokenbar.providers.opencode import OpencodeProvider, _parse_database


def _make_db(path, rows):
    """`rows` is a list of (id, session_id, time_created, data_dict)."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT,"
        " time_created INTEGER, time_updated INTEGER, data TEXT)"
    )
    for msg_id, session_id, time_created, data in rows:
        conn.execute(
            "INSERT INTO message VALUES (?, ?, ?, ?, ?)",
            (msg_id, session_id, time_created, time_created, json.dumps(data)),
        )
    conn.commit()
    conn.close()


def _assistant(
    input_=100,
    output=50,
    reasoning=0,
    cache_write=0,
    cache_read=0,
    model="claude-sonnet-4-6",
    provider="anthropic",
    cost=0.01,
):
    return {
        "role": "assistant",
        "cost": cost,
        "modelID": model,
        "providerID": provider,
        "tokens": {
            "input": input_,
            "output": output,
            "reasoning": reasoning,
            "cache": {"write": cache_write, "read": cache_read},
        },
    }


def test_parses_assistant_rows_and_ignores_user_rows(tmp_path):
    db = tmp_path / "opencode.db"
    _make_db(
        db,
        [
            ("msg_1", "ses_1", 1_726_000_000_000, _assistant(input_=10, output=20)),
            ("msg_2", "ses_1", 1_726_000_001_000, {"role": "user"}),
        ],
    )
    entries = _parse_database(db)
    assert entries is not None
    assert len(entries) == 1
    assert entries[0].input == 10
    assert entries[0].output == 20
    assert entries[0].model == "claude-sonnet-4-6"


def test_reasoning_tokens_are_folded_into_output(tmp_path):
    db = tmp_path / "opencode.db"
    _make_db(
        db,
        [("msg_1", "ses_1", 1_726_000_000_000, _assistant(output=50, reasoning=25))],
    )
    entries = _parse_database(db)
    assert entries[0].output == 75


def test_zero_usage_row_is_skipped(tmp_path):
    db = tmp_path / "opencode.db"
    _make_db(
        db,
        [
            (
                "msg_1",
                "ses_1",
                1_726_000_000_000,
                _assistant(input_=0, output=0, cache_write=0, cache_read=0),
            )
        ],
    )
    assert _parse_database(db) == []


def test_local_day_derives_from_time_created(tmp_path):
    db = tmp_path / "opencode.db"
    millis = int(datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)
    _make_db(db, [("msg_1", "ses_1", millis, _assistant())])
    entries = _parse_database(db)
    assert entries[0].local_day == datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc).astimezone().strftime(
        "%Y-%m-%d"
    )


def test_provider_reports_free_bundled_models_at_zero_cost(tmp_path):
    # opencode's own bundled models don't appear in pricing.TABLE at all —
    # unlike Antigravity, no prefix trick is needed to zero them out.
    home = tmp_path
    db_dir = home / ".local" / "share" / "opencode"
    db_dir.mkdir(parents=True)
    millis = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    _make_db(
        db_dir / "opencode.db",
        [
            (
                "msg_1",
                "ses_1",
                millis,
                _assistant(input_=1000, output=500, model="muse-spark-1.2-contributor-free", provider="opencode"),
            )
        ],
    )
    provider = OpencodeProvider(home=home)
    today = datetime.now(tz=timezone.utc).astimezone().strftime("%Y-%m-%d")
    daily = provider.fetch_daily(today=today)
    assert daily is not None
    assert daily.total_tokens == 1500
    assert daily.total_cost == 0.0


def test_a_real_upstream_model_prices_normally(tmp_path):
    home = tmp_path
    db_dir = home / ".local" / "share" / "opencode"
    db_dir.mkdir(parents=True)
    millis = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    _make_db(
        db_dir / "opencode.db",
        [
            (
                "msg_1",
                "ses_1",
                millis,
                _assistant(input_=1_000_000, output=0, model="claude-sonnet-4-6"),
            )
        ],
    )
    provider = OpencodeProvider(home=home)
    today = datetime.now(tz=timezone.utc).astimezone().strftime("%Y-%m-%d")
    daily = provider.fetch_daily(today=today)
    assert daily is not None
    assert daily.total_cost == 3.0  # per_million(3, 15, ...) input rate


def test_no_database_means_no_entries(tmp_path):
    provider = OpencodeProvider(home=tmp_path)
    assert provider.scan_entries() == []
    assert provider.fetch_daily() is None


def test_cache_roundtrips_scan_entries(tmp_path):
    from poketokenbar.cache import ScanCache

    home = tmp_path
    db_dir = home / ".local" / "share" / "opencode"
    db_dir.mkdir(parents=True)
    millis = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    _make_db(db_dir / "opencode.db", [("msg_1", "ses_1", millis, _assistant())])

    cache = ScanCache(tmp_path / "scan.db")
    try:
        provider = OpencodeProvider(cache=cache, home=home)
        first = provider.scan_entries()
        second = provider.scan_entries()
        assert len(first) == 1
        assert [e.id for e in first] == [e.id for e in second]
    finally:
        cache.close()
