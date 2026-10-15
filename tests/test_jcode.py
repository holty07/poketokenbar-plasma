"""Jcode provider tests.

Mirrors the pattern used by test_pi.py: build journal lines from the
real JSON structure and exercise the parser end-to-end.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from poketokenbar.providers import _local
from poketokenbar.providers.jcode import JcodeProvider, journal_roots, parse_file


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    pass  # no env overrides for jcode


def _usage(
    prompt_tokens=0, input_tokens=0, output=0, cache_read=0, cache_write=0
):
    return {
        "prompt_tokens": prompt_tokens,
        "input_tokens": input_tokens,
        "output_tokens": output,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_write,
    }


def _meta(model="fcm", provider_key="openai-compatible"):
    return {"model": model, "provider_key": provider_key}


def _msg(msg_id, usage, role="assistant", timestamp=None):
    return {
        "id": msg_id,
        "role": role,
        "timestamp": timestamp or "2026-10-13T07:00:29.869Z",
        "token_usage": usage,
    }


def _line(messages, meta=None):
    obj = {"append_messages": messages}
    if meta is not None:
        obj["meta"] = meta
    return json.dumps(obj)


def _write(home, lines, name="session.journal.jsonl", sub="a"):
    directory = journal_roots(home)[0] / sub
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def test_parses_assistant_message_with_cache_read(tmp_path):
    path = _write(
        tmp_path,
        [
            _line(
                [_msg("m1", _usage(prompt_tokens=30530, input_tokens=30530, output=534, cache_read=12288))],
                _meta("fcm", "openai-compatible"),
            )
        ],
    )
    entries = parse_file(path)
    assert entries is not None
    assert len(entries) == 1
    e = entries[0]
    assert e.id == "jcode|m1"
    assert e.model == "openai-compatible:fcm"
    # input_tokens == prompt_tokens includes cache_read; subtract to get true input
    assert e.input == 30530 - 12288
    assert e.cache_read == 12288
    assert e.output == 534
    assert e.cache_write == 0
    assert e.total == (30530 - 12288) + 12288 + 534


def test_parses_message_without_cache_fields(tmp_path):
    path = _write(
        tmp_path,
        [
            _line(
                [_msg("m2", _usage(prompt_tokens=1000, input_tokens=1000, output=200))],
                _meta("fcm", "openai-compatible"),
            )
        ],
    )
    entries = parse_file(path)
    assert len(entries) == 1
    e = entries[0]
    assert e.input == 1000
    assert e.output == 200
    assert e.cache_read == 0
    assert e.cache_write == 0


def test_parses_message_with_cache_write(tmp_path):
    path = _write(
        tmp_path,
        [
            _line(
                [
                    _msg(
                        "m3",
                        _usage(prompt_tokens=5000, input_tokens=5000, output=100, cache_write=200),
                    )
                ],
                _meta("fcm", "openai-compatible"),
            )
        ],
    )
    entries = parse_file(path)
    assert len(entries) == 1
    e = entries[0]
    assert e.input == 5000
    assert e.output == 100
    assert e.cache_write == 200
    assert e.cache_read == 0


def test_skips_user_and_tool_messages(tmp_path):
    path = _write(
        tmp_path,
        [
            _line(
                [
                    _msg("user1", _usage(prompt_tokens=100, input_tokens=100, output=0), role="user"),
                    _msg("tool1", _usage(prompt_tokens=0, input_tokens=0, output=50), role="tool"),
                    _msg("assist1", _usage(prompt_tokens=200, input_tokens=200, output=50)),
                ],
                _meta("fcm", "openai-compatible"),
            )
        ],
    )
    entries = parse_file(path)
    assert len(entries) == 1
    assert entries[0].id == "jcode|assist1"


def test_deduplicates_by_message_id(tmp_path):
    # Same message ID appears in multiple journal lines (forked session)
    usage = _usage(prompt_tokens=1000, input_tokens=1000, output=200, cache_read=100)
    path = _write(
        tmp_path,
        [
            _line([_msg("same-id", usage)], _meta("fcm", "openai-compatible")),
            _line([_msg("same-id", usage)], _meta("fcm", "openai-compatible")),
        ],
    )
    entries = parse_file(path)
    assert len(entries) == 1
    assert entries[0].id == "jcode|same-id"


def test_global_dedup_across_files(tmp_path):
    usage = _usage(prompt_tokens=100, input_tokens=100, output=20)
    _write(tmp_path, [_line([_msg("dup-id", usage)], _meta("fcm", "openai-compatible"))], name="a.journal.jsonl")
    _write(tmp_path, [_line([_msg("dup-id", usage)], _meta("fcm", "openai-compatible"))], name="b.journal.jsonl")

    provider = JcodeProvider(home=tmp_path)
    entries = provider.scan_entries()
    assert len(entries) == 1
    assert entries[0].id == "jcode|dup-id"


def test_provider_fetch_daily_aggregates_today(tmp_path):
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    today_ms = int(datetime.now(UTC).timestamp() * 1000)

    # Today's usage
    today_line = _line(
        [_msg("today-msg", _usage(prompt_tokens=1000, input_tokens=1000, output=200, cache_read=100))],
        _meta("fcm", "openai-compatible"),
    )
    # Modify timestamp to be today
    today_line = today_line.replace("2026-10-13T07:00:29.869Z", datetime.now(UTC).isoformat().replace("+00:00", "Z"))

    _write(tmp_path, [today_line])

    provider = JcodeProvider(home=tmp_path)
    daily = provider.fetch_daily(today=today)
    assert daily is not None
    assert daily.input_tokens == 900  # 1000 - 100
    assert daily.output_tokens == 200
    assert daily.cache_read_tokens == 100
    assert daily.total_tokens == 1200


def test_provider_reports_cost():
    assert JcodeProvider.reports_cost is True


def test_missing_directory_yields_no_entries(tmp_path):
    provider = JcodeProvider(home=tmp_path / "nonexistent")
    assert provider.scan_entries() == []
    assert provider.fetch_daily() is None