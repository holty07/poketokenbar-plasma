"""Copilot CLI usage — mirrors upstream CopilotUsageTests.swift."""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from poketokenbar.providers import _local
from poketokenbar.providers.copilot import (
    CopilotProvider,
    _parse_row,
    _signature,
    database_for_root,
    default_roots,
)


def _make_db(tmp_path, name="session-store.db"):
    copilot_dir = tmp_path / ".copilot"
    copilot_dir.mkdir(parents=True, exist_ok=True)
    db = copilot_dir / name
    conn = sqlite3.connect(str(db))
    conn.execute(
        """
        CREATE TABLE assistant_usage_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            model TEXT NOT NULL,
            input_tokens INTEGER,
            output_tokens INTEGER,
            cache_read_tokens INTEGER,
            cache_write_tokens INTEGER,
            reasoning_tokens INTEGER,
            created_at TEXT DEFAULT (datetime('now'))
        )
        """
    )
    conn.commit()
    conn.close()
    return db


def _insert(db, rows):
    conn = sqlite3.connect(str(db))
    for row in rows:
        conn.execute(
            "INSERT INTO assistant_usage_events "
            "(id, session_id, model, input_tokens, output_tokens, "
            "cache_read_tokens, cache_write_tokens, reasoning_tokens, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            row,
        )
    conn.commit()
    conn.close()


def _row(row_id, model, input_, output, cache_read, cache_write, reasoning, created_at):
    return (row_id, "session-1", model, input_, output, cache_read, cache_write, reasoning, created_at)


def test_cached_prompt_tokens_not_counted_twice(tmp_path):
    db = _make_db(tmp_path)
    _insert(db, [
        _row(1, "claude-opus-5", 43_792, 443, 42_698, 904, 59, "2026-01-04T10:00:00.000Z"),
    ])
    provider = CopilotProvider(home=tmp_path)
    entries = provider.scan_entries()
    assert len(entries) == 1
    e = entries[0]
    # input = 43792 - 42698 - 904 = 190
    assert e.input == 190
    assert e.cache_read == 42_698
    assert e.cache_write == 904
    assert e.output == 443  # reasoning is part of output, not extra
    assert e.total == 44_235
    assert e.id == f"copilot|{db}|1"
    assert e.model == "claude-opus-5"


def test_fully_cached_prompt_keeps_zero_input(tmp_path):
    db = _make_db(tmp_path)
    _insert(db, [
        _row(1, "gpt-5.4-mini", 57_375, 35, 57_375, 0, 0, "2026-01-04T10:00:00.000Z"),
    ])
    provider = CopilotProvider(home=tmp_path)
    entries = provider.scan_entries()
    assert len(entries) == 1
    assert entries[0].input == 0
    assert entries[0].total == 57_410


def test_reads_both_timestamp_shapes(tmp_path):
    db = _make_db(tmp_path)
    _insert(db, [
        _row(1, "claude-opus-5", 100, 10, 0, 0, 0, "2026-01-04T10:00:00.000Z"),
        _row(2, "claude-opus-5", 200, 20, 0, 0, 0, "2026-01-04 11:00:00"),
    ])
    provider = CopilotProvider(home=tmp_path)
    entries = provider.scan_entries()
    assert len(entries) == 2
    dates = sorted(e.date for e in entries)
    assert dates[0] == datetime(2026, 1, 4, 10, 0, 0, tzinfo=UTC)
    assert dates[1] == datetime(2026, 1, 4, 11, 0, 0, tzinfo=UTC)


def test_rows_without_tokens_are_skipped(tmp_path):
    db = _make_db(tmp_path)
    _insert(db, [
        _row(1, "claude-opus-5", 0, 0, 0, 0, 0, "2026-01-04T10:00:00.000Z"),
        _row(2, "claude-opus-5", 5, 1, 0, 0, 0, "2026-01-04T10:01:00.000Z"),
    ])
    provider = CopilotProvider(home=tmp_path)
    entries = provider.scan_entries()
    assert [e.id for e in entries] == [f"copilot|{db}|2"]


def test_daily_aggregates_every_event(tmp_path):
    db = _make_db(tmp_path)
    _insert(db, [
        _row(1, "claude-opus-5", 100, 10, 0, 0, 0, "2026-01-04T10:00:00.000Z"),
        _row(2, "gpt-5.4-mini", 250, 40, 50, 100, 5, "2026-01-04T10:05:00.000Z"),
    ])
    provider = CopilotProvider(home=tmp_path)
    entries = provider.scan_entries()
    daily = _local.daily(entries, today="2026-01-04")
    assert daily is not None
    # Row 1: input=100, output=10, total=110
    # Row 2: input=250-50-100=100, output=40, cache_read=50, cache_write=100, total=290
    assert daily.total_tokens == 110 + 290


def test_provider_identity():
    provider = CopilotProvider()
    assert provider.id == "copilot"
    assert provider.display_name == "Copilot CLI"
    assert provider.reports_cost is False


def test_nonexistent_root_returns_nothing(tmp_path):
    from poketokenbar.providers.copilot import _parse_database
    assert _parse_database(tmp_path / "nope.db") is None


def test_database_for_root(tmp_path):
    # directory containing session-store.db
    assert database_for_root(tmp_path) == tmp_path / "session-store.db"
    # direct db path
    db = tmp_path / "session-store.db"
    assert database_for_root(db) == db
    # nonexistent root
    assert database_for_root(tmp_path / "nope") is None


def test_default_roots(monkeypatch, tmp_path):
    monkeypatch.delenv("COPILOT_HOME", raising=False)
    roots = default_roots(home=tmp_path)
    assert roots == [tmp_path / ".copilot"]


def test_default_roots_env(monkeypatch, tmp_path):
    monkeypatch.setenv("COPILOT_HOME", "/opt/copilot1,/opt/copilot2")
    roots = default_roots(home=tmp_path)
    assert roots == [Path("/opt/copilot1"), Path("/opt/copilot2")]


def test_signature_includes_wal(tmp_path):
    db = _make_db(tmp_path)
    sig = _signature(db)
    assert sig is not None
    mtime, size = sig
    assert size > 0


def test_parse_row():
    from pathlib import Path
    db = Path("/tmp/x/session-store.db")
    row = (1, "claude-opus-5", 43_792, 443, 42_698, 904, 59, "2026-01-04T10:00:00.000Z")
    e = _parse_row(db, row)
    assert e is not None
    assert e.input == 190
    assert e.cache_read == 42_698


def test_provider_fetch_daily_and_periods(tmp_path):
    db = _make_db(tmp_path)
    now = datetime(2026, 1, 4, 10, 0, 0, tzinfo=UTC)
    _insert(db, [
        _row(1, "claude-opus-5", 300, 100, 50, 0, 0, now.isoformat()),
    ])
    provider = CopilotProvider(home=tmp_path)
    assert provider.id == "copilot"
    daily = provider.fetch_daily(today="2026-01-04")
    assert daily is not None
    # input = 300 - 50 = 250, output = 100, total = 400
    assert daily.total_tokens == 400
    assert provider.fetch_periods(today="2026-01-04")["month"]["tokens"] == 400


def test_provider_without_data_returns_none(tmp_path):
    assert CopilotProvider(home=tmp_path).fetch_daily() is None
