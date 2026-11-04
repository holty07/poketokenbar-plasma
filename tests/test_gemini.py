"""Gemini CLI usage — mirrors upstream GeminiUsageTests.swift."""

from datetime import UTC, datetime

import pytest

from poketokenbar.providers import _local
from poketokenbar.providers.gemini import (
    GeminiProvider,
    chat_roots,
    is_chat_file,
    parse_file,
)


def _root(home):
    return home / ".gemini" / "tmp" / "chats"


def _write_jsonl(home, lines, session="session-2026-07-03T01-00-abcd1234"):
    directory = _root(home) / "hash1"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session}.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _write_json(home, text, name="checkpoint-old"):
    directory = _root(home) / "hash1"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.json"
    path.write_text(text, encoding="utf-8")
    return path


NEW_JSONL = [
    '{"type":"session_metadata","sessionId":"s1","startTime":"2026-07-03T01:00:00.000Z"}',
    '{"type":"user","id":"m1","timestamp":"2026-07-03T01:00:05.000Z","content":[{"text":"hi"}]}',
    '{"type":"gemini","id":"m2","timestamp":"2026-07-03T01:00:10.000Z","model":"gemini-2.5-pro",'
    '"tokens":{"input":1000,"output":50,"cached":600,"thoughts":30,"tool":20,"total":1100}}',
    '{"type":"gemini","id":"m3","timestamp":"2026-07-03T01:01:00.000Z","model":"gemini-2.5-flash",'
    '"tokens":{"input":10,"output":5,"cached":0,"thoughts":0,"tool":0,"total":15}}',
    '{"type":"message_update","id":"m3","tokens":{"input":10,"output":8,"cached":0,"thoughts":2,'
    '"tool":0,"total":20}}',
]

LEGACY_JSON = (
    '{"sessionId":"s0","startTime":"2026-07-02T00:00:00.000Z","messages":['
    '{"id":"a1","type":"gemini","timestamp":"2026-07-02T00:10:00.000Z","model":"gemini-2.5-pro",'
    '"tokens":{"input":100,"output":10,"cached":0,"thoughts":0,"tool":0,"total":110}},'
    '{"id":"a2","type":"user","content":[{"text":"x"}]}'
    "]}"
)


def test_parses_new_jsonl_mapping_and_update(tmp_path):
    path = _write_jsonl(tmp_path, NEW_JSONL)
    entries = parse_file(path)
    assert len(entries) == 2

    m2 = entries[0]
    assert m2.model == "gemini-2.5-pro"
    # input = (1000 - 600 uncached) + 20 tool = 420
    assert m2.input == 420
    assert m2.cache_read == 600
    # output = 50 + 30 thoughts = 80
    assert m2.output == 80
    assert m2.cache_write == 0
    assert m2.total == 1100
    assert m2.date == datetime(2026, 7, 3, 1, 0, 10, tzinfo=UTC)

    m3 = entries[1]
    # message_update: output = 8 + 2 thoughts = 10, total = 20
    assert m3.output == 10
    assert m3.total == 20


def test_parses_legacy_json(tmp_path):
    path = _write_json(tmp_path, LEGACY_JSON)
    entries = parse_file(path)
    assert len(entries) == 1
    assert entries[0].input == 100
    assert entries[0].output == 10
    assert entries[0].total == 110


def test_file_without_tokens_yields_nothing(tmp_path):
    directory = _root(tmp_path)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "logs.json"
    path.write_text(
        '{"entries":[{"sessionId":"x","type":"user","message":"hello"}]}',
        encoding="utf-8",
    )
    assert parse_file(path) == []


def test_only_chat_files_are_scanned(tmp_path):
    _write_jsonl(tmp_path, NEW_JSONL)
    (_root(tmp_path) / "hash1" / "notes.txt").write_text("not a chat", encoding="utf-8")
    assert len(GeminiProvider(home=tmp_path).scan_entries()) == 2


def test_dedup_keep_max_across_files(tmp_path):
    first = _write_jsonl(tmp_path, NEW_JSONL, session="ses_a")
    # Same message id m2 in a second file with a larger total wins.
    second_lines = [
        '{"type":"gemini","id":"m2","timestamp":"2026-07-03T01:00:10.000Z","model":"gemini-2.5-pro",'
        '"tokens":{"input":3000,"output":100,"cached":500,"thoughts":0,"tool":0,"total":3100}}'
    ]
    _write_jsonl(tmp_path, second_lines, session="ses_b")
    entries = GeminiProvider(home=tmp_path).scan_entries()
    by_id = {e.id: e for e in entries}
    assert by_id["gemini|m2"].total == 3100


def test_provider_fetch_daily_and_periods(tmp_path):
    now = int(__import__("time").time() * 1000)
    _write_jsonl(
        tmp_path,
        [
            '{"type":"gemini","id":"m1","timestamp":%d,"model":"gemini-2.5-pro",'
            '"tokens":{"input":300,"output":100,"cached":50,"thoughts":10,"tool":5,"total":415}}'
            % now,
        ],
    )
    provider = GeminiProvider(home=tmp_path)
    assert provider.id == "gemini"
    daily = provider.fetch_daily()
    assert daily is not None
    assert daily.total_tokens == 415
    # input = 300 - 50 + 5 = 255, output = 100 + 10 = 110, cache_read = 50
    assert daily.input_tokens == 255
    assert daily.output_tokens == 110
    assert daily.cache_read_tokens == 50
    assert provider.fetch_periods()["month"]["tokens"] == 415


def test_provider_without_data_returns_none(tmp_path):
    assert GeminiProvider(home=tmp_path).fetch_daily() is None


def test_chat_roots_default(tmp_path):
    assert chat_roots(home=tmp_path) == [tmp_path / ".gemini" / "tmp" / "chats"]


def test_is_chat_file(tmp_path):
    assert is_chat_file(tmp_path / "session.jsonl")
    assert is_chat_file(tmp_path / "checkpoint.json")
    assert not is_chat_file(tmp_path / "notes.txt")


def test_gemini_pricing():
    from poketokenbar import pricing

    assert pricing.rate("gemini-2.5-pro").input == pytest.approx(1.25e-6)
    assert pricing.rate("gemini-2.5-flash").input == pytest.approx(0.30e-6)
    assert pricing.rate("gemini-2.5-flash-lite").input == pytest.approx(0.10e-6)
    assert pricing.rate("gemini-3.1-pro-preview") == pricing.ZERO
    assert pricing.rate("gemini-nano-banana") == pricing.ZERO


def test_cost_with_cache_read():
    from poketokenbar import pricing

    c = pricing.cost("gemini-2.5-pro", input_=420, output=80, cache_write=0, cache_read=600)
    expected = 420 * 1.25e-6 + 80 * 10e-6 + 600 * 0.125e-6
    assert abs(c - expected) < 1e-12
