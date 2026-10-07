"""Mirrors upstream AsideUsageTests.swift (PokeTokenBar #283). Synthetic
SQLite fixtures only."""

import sqlite3
import time
from datetime import datetime, timedelta

from poketokenbar.cache import ScanCache
from poketokenbar.providers import _local
from poketokenbar.providers.aside import AsideProvider, databases, parse_database, roots

FULL_USAGE = (
    '{"input":22744,"output":5515,"cacheRead":758400,"cacheWrite":0,'
    '"totalTokens":786659,"cost":{"total":0.65837}}'
)
FULL_TOTAL = 786659


def _sql(path, script):
    conn = sqlite3.connect(path)
    conn.executescript(script)
    conn.commit()
    conn.close()


def _fixture(home, user="0", started=None, last_message=None, finished=None,
             aborted=None, usage=FULL_USAGE):
    directory = home / ".aside" / "u" / user
    directory.mkdir(parents=True, exist_ok=True)
    db = directory / "state.db"
    now = time.time()
    started = int(started if started is not None else now)
    last_message = int(last_message if last_message is not None else started)
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, model TEXT);"
        "CREATE TABLE session_turns (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " session_id TEXT REFERENCES sessions(id) ON DELETE CASCADE, token_usage TEXT,"
        " started_at INTEGER, last_message_timestamp INTEGER NOT NULL,"
        " finished_at INTEGER, aborted_at INTEGER);"
    )
    conn.execute(
        "INSERT INTO sessions VALUES ('synthetic-session', '{\"modelId\":\"synthetic-model\"}')"
    )
    conn.execute(
        "INSERT INTO session_turns VALUES (1, 'synthetic-session', ?, ?, ?, ?, ?)",
        (usage, started, last_message,
         None if finished is None else int(finished),
         None if aborted is None else int(aborted)),
    )
    conn.commit()
    conn.close()
    return db


def _scan(home):
    return [e for db in databases(roots(home=home)) for e in (parse_database(db) or [])]


def _total(entries):
    return sum(e.total for e in entries)


def test_reads_turn_buckets_without_counting_total_again(tmp_path):
    _fixture(tmp_path)
    entries = _scan(tmp_path)
    assert len(entries) == 1
    e = entries[0]
    assert (e.input, e.output, e.cache_read) == (22744, 5515, 758400)
    assert e.total == FULL_TOTAL
    assert e.explicit_cost == 0.65837
    assert e.model == "synthetic-model"


def test_total_only_usage_falls_back_without_inventing_cache(tmp_path):
    _fixture(tmp_path, usage='{"input":null,"totalTokens":123}')
    e = _scan(tmp_path)[0]
    assert e.total == 123
    assert e.cache_read == 0
    assert e.explicit_cost == 0.0  # no recorded cost: never priced from the session model


def test_absurd_token_counts_are_clamped(tmp_path):
    _fixture(tmp_path, usage='{"input":1e30,"output":-4,"cacheRead":5}')
    e = _scan(tmp_path)[0]
    assert (e.input, e.output, e.cache_read) == (0, 0, 5)


def test_provider_reports_daily_and_periods(tmp_path):
    _fixture(tmp_path)
    _fixture(tmp_path, user="1")
    provider = AsideProvider(home=tmp_path)
    assert provider.id == "aside"
    daily = provider.fetch_daily()
    assert daily.total_tokens == 2 * FULL_TOTAL
    assert abs(daily.total_cost - 2 * 0.65837) < 1e-9
    periods = provider.fetch_periods()
    assert periods["week"]["tokens"] == 2 * FULL_TOTAL
    assert periods["month"]["tokens"] == 2 * FULL_TOTAL


def test_deleted_session_stays_counted_for_the_process(tmp_path):
    db = _fixture(tmp_path)
    _fixture(tmp_path, user="1")
    provider = AsideProvider(home=tmp_path)
    assert provider.fetch_daily().total_tokens == 2 * FULL_TOTAL
    _sql(db, "PRAGMA foreign_keys = ON; DELETE FROM sessions WHERE id = 'synthetic-session';")
    assert len(_scan(tmp_path)) == 1  # the cascade really removed the turn
    assert provider.fetch_daily().total_tokens == 2 * FULL_TOTAL
    # A fresh process (no in-memory history) sees the rescan as the truth.
    assert AsideProvider(home=tmp_path).fetch_daily().total_tokens == FULL_TOTAL


def test_growing_turn_replaces_the_cached_value(tmp_path):
    db = _fixture(tmp_path)
    cache = ScanCache(tmp_path / "scan.db")
    provider = AsideProvider(cache=cache, home=tmp_path)
    assert provider.fetch_daily().total_tokens == FULL_TOTAL
    _sql(db, "UPDATE session_turns SET token_usage = "
             "'{\"input\":22744,\"output\":9999,\"cacheRead\":758400}' WHERE id = 1;")
    assert provider.fetch_daily().total_tokens == FULL_TOTAL + 9999 - 5515
    cache.close()


def test_recreated_database_gets_fresh_entry_ids(tmp_path):
    db = _fixture(tmp_path)
    provider = AsideProvider(home=tmp_path)
    assert provider.fetch_daily().total_tokens == FULL_TOTAL
    keep_inode_busy = db.with_name("old.db")
    db.rename(keep_inode_busy)  # so the new file cannot reuse the inode
    _fixture(tmp_path, usage='{"input":10,"output":20}')
    assert provider.fetch_daily().total_tokens == FULL_TOTAL + 30
    assert AsideProvider(home=tmp_path).fetch_daily().total_tokens == 30


def test_unreadable_database_keeps_previous_values(tmp_path):
    db = _fixture(tmp_path)
    provider = AsideProvider(home=tmp_path)
    assert provider.fetch_daily().total_tokens == FULL_TOTAL
    db.write_bytes(b"not a database")
    assert provider.fetch_daily().total_tokens == FULL_TOTAL
    assert provider.fetch_periods()["week"]["tokens"] == FULL_TOTAL


def test_aborted_turn_with_tokens_is_counted(tmp_path):
    _fixture(tmp_path, aborted=time.time())
    assert _total(_scan(tmp_path)) == FULL_TOTAL


def test_skips_unreadable_database_and_keeps_healthy_ones(tmp_path):
    no_turns = tmp_path / ".aside" / "u" / "0"
    no_turns.mkdir(parents=True)
    _sql(no_turns / "state.db", "CREATE TABLE sessions (id TEXT PRIMARY KEY, model TEXT);")
    _fixture(tmp_path, user="1")
    assert _total(AsideProvider(home=tmp_path).scan_entries()) == FULL_TOTAL
    not_sqlite = tmp_path / ".aside" / "u" / "2"
    not_sqlite.mkdir()
    (not_sqlite / "state.db").write_bytes(b"not a database")
    assert _total(AsideProvider(home=tmp_path).scan_entries()) == FULL_TOTAL
    (tmp_path / ".aside" / "u" / "3" / "state.db").mkdir(parents=True)
    assert _total(AsideProvider(home=tmp_path).scan_entries()) == FULL_TOTAL


def test_partial_scan_failure_discards_that_database_only(tmp_path):
    corrupt_dir = tmp_path / ".aside" / "u" / "0"
    corrupt_dir.mkdir(parents=True)
    corrupt = corrupt_dir / "state.db"
    pad = "x" * 4000
    now = int(time.time())
    seed = [
        "PRAGMA page_size=4096; PRAGMA journal_mode=DELETE;",
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, model TEXT);",
        (
            "CREATE TABLE session_turns (id INTEGER PRIMARY KEY, session_id TEXT, token_usage TEXT,"
            " started_at INTEGER, last_message_timestamp INTEGER NOT NULL, finished_at INTEGER);"
        ),
    ]
    seed += [
        f"INSERT INTO session_turns VALUES ({i}, 's',"
        f" '{{\"input\":1,\"output\":1,\"pad\":\"{pad}\"}}', {now}, {now}, NULL);"
        for i in range(1, 201)
    ]
    _sql(corrupt, "\n".join(seed))
    with corrupt.open("r+b") as handle:  # keep page 1 so prepare succeeds
        handle.seek(4096 * 20)
        handle.write(b"\xff" * 4096 * 100)
    assert AsideProvider(home=tmp_path).scan_entries() == []
    _fixture(tmp_path, user="1")
    assert _total(AsideProvider(home=tmp_path).scan_entries()) == FULL_TOTAL


def test_drops_zero_token_turns(tmp_path):
    _fixture(tmp_path, usage='{"input":0,"output":0,"cacheRead":0,"cacheWrite":0,"totalTokens":0}')
    assert _scan(tmp_path) == []


def test_turn_spanning_midnight_counts_toward_last_activity_day(tmp_path):
    start_of_today = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    late_yesterday = start_of_today - timedelta(minutes=10)
    _fixture(tmp_path, started=late_yesterday.timestamp(),
             last_message=(start_of_today + timedelta(minutes=10)).timestamp())
    assert _scan(tmp_path)[0].local_day == _local.today_key()


def test_finished_at_outranks_last_message_timestamp(tmp_path):
    start_of_today = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday = (start_of_today - timedelta(hours=1)).timestamp()
    today = (start_of_today + timedelta(hours=1)).timestamp()
    _fixture(tmp_path, user="0", started=yesterday, last_message=yesterday, finished=today)
    _fixture(tmp_path, user="1", started=yesterday, last_message=today, finished=yesterday)
    days = sorted(e.local_day for e in _scan(tmp_path))
    assert days == sorted([_local.today_key(), _local.local_day(start_of_today - timedelta(hours=1))])
