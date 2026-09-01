"""Antigravity provider tests.

Antigravity ships no reusable fixture files upstream (unlike Codex's real
rollout logs) — `AntigravityUsageTests.swift` builds its SQLite stores from
raw protobuf bytes instead, using field numbers read out of the CLI's own
`FileDescriptorProto` pool. These tests build the same bytes the same way
(`ModelUsageStats` 1/2/3/5/6/11, `ChatStartMetadata.created_at` 4,
`ChatModelMetadata.response_model` 19, `StepMetadata` 1/8/9/12) so the Python
port is checked against the same wire-level scenarios, not just re-derived
from the Python code itself.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from poketokenbar.providers.antigravity import AntigravityProvider, _parse_conversation

# --- protobuf encoding helpers, mirroring the Swift test suite's AntigravityProto extension ---


def _encode_raw_varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value == 0:
            out.append(byte)
            return bytes(out)
        out.append(byte | 0x80)


def _encode_varint(field: int, value: int) -> bytes:
    return _encode_raw_varint(field << 3) + _encode_raw_varint(value)


def _encode_message(field: int, payload: bytes) -> bytes:
    return _encode_raw_varint((field << 3) | 2) + _encode_raw_varint(len(payload)) + payload


def _encode_string(field: int, text: str) -> bytes:
    return _encode_message(field, text.encode("utf-8"))


def make_record(
    response_id: str | None,
    model: str,
    input_: int,
    output: int,
    cache_read: int,
    created_at_seconds: int | None = None,
    thinking: int | None = None,
    response: int | None = None,
    execution: str | None = None,
) -> bytes:
    """One `gen_metadata.data` blob, matching `AntigravityUsageTests.makeRecord`."""
    usage = _encode_varint(1, 1071)  # model enum, irrelevant to the reader
    usage += _encode_varint(2, input_)
    usage += _encode_varint(3, output)
    usage += _encode_varint(5, cache_read)
    usage += _encode_varint(6, 24)  # api_provider, irrelevant to the reader
    if thinking is not None:
        usage += _encode_varint(9, thinking)
    if response is not None:
        usage += _encode_varint(10, response)
    if response_id is not None:
        usage += _encode_string(11, response_id)

    chat_model = _encode_varint(3, 1071)
    chat_model += _encode_message(4, usage)
    if created_at_seconds is not None:
        chat_start = _encode_message(4, _encode_varint(1, created_at_seconds))
    else:
        # Antigravity 2.0 still writes chat_start_metadata; only created_at is gone.
        chat_start = _encode_varint(2, (1 << 64) - 1)
    chat_model += _encode_message(9, chat_start)
    chat_model += _encode_string(19, model)

    record = _encode_message(1, chat_model)
    if execution is not None:
        record += _encode_string(4, execution)
    return record


def make_step(
    queued: int, finished: int | None = None, response_id: str | None = None, execution: str | None = None
) -> bytes:
    """One `steps.metadata` blob, matching `AntigravityUsageTests.makeStep`."""

    def stamp(field: int, seconds: int) -> bytes:
        return _encode_message(field, _encode_varint(1, seconds))

    metadata = stamp(1, queued)
    if finished is not None:
        metadata += stamp(8, finished)
    if response_id is not None:
        metadata += _encode_message(9, _encode_string(11, response_id))
    if execution is not None:
        metadata += _encode_string(12, execution)
    return metadata


def write_conversation(path: Path, records: list[bytes], steps: list[bytes] | None = None) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE gen_metadata (idx integer, data blob, size integer NOT NULL DEFAULT 0)")
        conn.executemany(
            "INSERT INTO gen_metadata VALUES (?, ?, ?)",
            [(i, blob, len(blob)) for i, blob in enumerate(records)],
        )
        if steps:
            conn.execute("CREATE TABLE steps (idx integer, metadata blob)")
            conn.executemany(
                "INSERT INTO steps VALUES (?, ?)", [(i, blob) for i, blob in enumerate(steps)]
            )
        conn.commit()
    finally:
        conn.close()


def _epoch(text: str) -> int:
    return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())


# --- token mapping ---


def test_token_mapping_keeps_the_writer_semantics(tmp_path):
    db = tmp_path / "c1.db"
    write_conversation(
        db,
        [
            make_record(
                "r1", "gemini-3.6-flash", input_=4667, output=462, cache_read=52968,
                created_at_seconds=_epoch("2026-03-04T10:00:00Z"), thinking=398, response=64,
            )
        ],
    )
    entries = _parse_conversation(db)
    assert entries is not None
    entry = entries[0]
    assert entry.input == 4667, "input_tokens is already net of the cache read"
    assert entry.cache_read == 52968
    assert entry.output == 462, "output_tokens already sums thinking and response"
    assert entry.cache_write == 0
    assert entry.model == "antigravity/gemini-3.6-flash"


def test_total_is_the_sum_of_the_counters(tmp_path):
    db = tmp_path / "c1.db"
    write_conversation(
        db,
        [make_record("r1", "gemini-3.6-flash", input_=100, output=20, cache_read=300,
                      created_at_seconds=_epoch("2026-03-04T10:00:00Z"))],
    )
    entries = _parse_conversation(db)
    assert entries[0].total == 420


def test_thinking_and_response_are_not_added_on_top_of_output(tmp_path):
    db = tmp_path / "c1.db"
    write_conversation(
        db,
        [make_record("r1", "gemini-3.6-flash", input_=10, output=900, cache_read=0,
                      created_at_seconds=_epoch("2026-03-04T10:00:00Z"), thinking=800, response=100)],
    )
    entries = _parse_conversation(db)
    assert entries[0].output == 900


def test_row_with_no_tokens_produces_no_entry(tmp_path):
    db = tmp_path / "c1.db"
    write_conversation(
        db,
        [make_record("r1", "gemini-3.6-flash", input_=0, output=0, cache_read=0,
                      created_at_seconds=_epoch("2026-03-04T10:00:00Z"))],
    )
    assert _parse_conversation(db) == []


# --- identity ---


def test_response_id_deduplicates_across_conversations(tmp_path):
    shared = make_record("same-call", "gemini-3.6-flash", input_=100, output=20, cache_read=300,
                          created_at_seconds=_epoch("2026-03-04T10:00:00Z"))
    write_conversation(tmp_path / "c1.db", [shared])
    write_conversation(tmp_path / "c2.db", [shared])

    from poketokenbar.providers import antigravity as mod

    entries = mod.dedup_keep_max(
        (_parse_conversation(tmp_path / "c1.db") or []) + (_parse_conversation(tmp_path / "c2.db") or [])
    )
    assert len(entries) == 1
    assert entries[0].id == "antigravity|same-call"


def test_record_without_response_id_falls_back_to_conversation_and_index(tmp_path):
    db = tmp_path / "c1.db"
    write_conversation(
        db,
        [make_record(None, "gemini-3.6-flash", input_=100, output=20, cache_read=300,
                      created_at_seconds=_epoch("2026-03-04T10:00:00Z"))],
    )
    entries = _parse_conversation(db)
    assert entries[0].id == "antigravity|c1|0"


# --- time ---


def test_created_at_drives_the_local_day(tmp_path):
    db = tmp_path / "c1.db"
    write_conversation(
        db,
        [make_record("r1", "gemini-3.6-flash", input_=100, output=20, cache_read=300,
                      created_at_seconds=_epoch("2026-03-04T10:00:00Z"))],
    )
    entries = _parse_conversation(db)
    expected = datetime.fromtimestamp(_epoch("2026-03-04T10:00:00Z"), tz=timezone.utc)
    assert entries[0].local_day == expected.astimezone().strftime("%Y-%m-%d")


def test_conversation_without_protobuf_created_at_uses_file_mtime_fallback(tmp_path):
    db = tmp_path / "c_modern.db"
    write_conversation(
        db,
        [make_record("r_modern_1", "gemini-3.7-flash", input_=500, output=100, cache_read=2000)],
    )
    entries = _parse_conversation(db)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.id == "antigravity|r_modern_1"
    assert entry.input == 500
    assert entry.output == 100
    assert entry.cache_read == 2000
    assert entry.total == 2600
    assert entry.model == "antigravity/gemini-3.7-flash"


def test_record_is_dated_from_its_step_when_the_protobuf_omits_created_at(tmp_path):
    db = tmp_path / "c_modern.db"
    queued = _epoch("2026-03-04T09:00:00Z")
    finished = _epoch("2026-03-04T09:05:00Z")
    write_conversation(
        db,
        records=[make_record("r1", "gemini-3.7-flash", input_=10, output=5, cache_read=0)],
        steps=[make_step(queued=queued, finished=finished, response_id="r1")],
    )
    entries = _parse_conversation(db)
    expected = datetime.fromtimestamp(finished, tz=timezone.utc)
    assert entries[0].local_day == expected.astimezone().strftime("%Y-%m-%d")


def test_execution_id_falls_back_when_no_response_match(tmp_path):
    db = tmp_path / "c_modern.db"
    queued = _epoch("2026-03-04T09:00:00Z")
    write_conversation(
        db,
        records=[make_record("r1", "gemini-3.7-flash", input_=10, output=5, cache_read=0, execution="exec-1")],
        steps=[make_step(queued=queued, execution="exec-1")],
    )
    entries = _parse_conversation(db)
    expected = datetime.fromtimestamp(queued, tz=timezone.utc)
    assert entries[0].local_day == expected.astimezone().strftime("%Y-%m-%d")


# --- discard / durability ---


def test_sentinel_token_count_is_discarded_rather_than_counted(tmp_path):
    db = tmp_path / "c1.db"
    huge = 2_000_000_000  # above TOKEN_CEILING
    write_conversation(
        db,
        [make_record("r1", "gemini-3.6-flash", input_=huge, output=20, cache_read=300,
                      created_at_seconds=_epoch("2026-03-04T10:00:00Z"))],
    )
    entries = _parse_conversation(db)
    # The oversized counter is dropped from the sum rather than dominating it.
    assert entries[0].input == 0
    assert entries[0].total == 320


def test_database_without_the_expected_table_is_ignored(tmp_path):
    db = tmp_path / "not_a_conversation.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE something_else (idx integer)")
    conn.commit()
    conn.close()
    assert _parse_conversation(db) == []


def test_missing_directory_yields_no_entries(tmp_path):
    provider = AntigravityProvider(home=tmp_path)
    assert provider.scan_entries() == []
    assert provider.fetch_daily() is None


# --- provider integration ---


def test_provider_reports_tokens_only():
    assert AntigravityProvider.reports_cost is False


def test_fetch_daily_aggregates_across_stores_and_roots(tmp_path):
    root = tmp_path / ".gemini" / "antigravity-cli" / "conversations"
    root.mkdir(parents=True)
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    today_epoch = int(datetime.now().astimezone().timestamp())

    write_conversation(
        root / "c1.db",
        [make_record("r1", "gemini-3.6-flash", input_=100, output=20, cache_read=0,
                      created_at_seconds=today_epoch)],
    )
    write_conversation(
        root / "c2.db",
        [make_record("r2", "gemini-3.6-flash", input_=50, output=10, cache_read=0,
                      created_at_seconds=today_epoch)],
    )

    provider = AntigravityProvider(home=tmp_path)
    daily = provider.fetch_daily(today=today)
    assert daily is not None
    assert daily.total_tokens == 180
    assert daily.total_cost == 0.0, "subscription-billed — antigravity/ prefix zeroes the rate card"
