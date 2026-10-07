"""Mirrors upstream PiUsageTests.swift (PokeTokenBar #189, #225)."""

import json
from datetime import UTC, datetime

import pytest

from poketokenbar.providers import _local
from poketokenbar.providers.pi import PiProvider, parse_file, session_roots


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in ("PI_CODING_AGENT_DIR", "PI_CODING_AGENT_SESSION_DIR"):
        monkeypatch.delenv(key, raising=False)


def _usage(input_=0, output=0, reasoning=None, cache_write=0, cache_read=0, total=None):
    value = {"input": input_, "output": output, "cacheWrite": cache_write, "cacheRead": cache_read}
    if reasoning is not None:
        value["reasoning"] = reasoning
    if total is not None:
        value["totalTokens"] = total
    return value


def _message(msg_id, usage, role="assistant", envelope_ts="2026-08-17T10:00:00.000Z",
             message_ts=None, stop_reason=None):
    nested = {"role": role, "provider": "example", "model": "model-name",
              "content": [], "usage": usage}
    if message_ts is not None:
        nested["timestamp"] = message_ts
    if stop_reason is not None:
        nested["stopReason"] = stop_reason
    return {"type": "message", "id": msg_id, "parentId": None,
            "timestamp": envelope_ts, "message": nested}


def _sessions(home):
    return home / ".pi" / "agent" / "sessions"


def _write(home, objects, name="session.jsonl", sub="a", trailing=()):
    directory = _sessions(home) / sub
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    lines = [json.dumps(o) for o in objects] + list(trailing)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def test_output_already_includes_reasoning_and_prefers_message_timestamp(tmp_path):
    actual = datetime(2026, 8, 16, 23, 59, 59, tzinfo=UTC)
    path = _write(tmp_path, [_message(
        "turn-1",
        _usage(input_=10, output=20, reasoning=5, cache_write=4, cache_read=30, total=64),
        envelope_ts="2026-08-17T10:00:00.000Z",
        message_ts=actual.timestamp() * 1000,
    )])
    entry = parse_file(path)[0]
    assert entry.id == "turn-1"
    assert entry.model == "model-name"
    assert (entry.input, entry.output, entry.cache_write, entry.cache_read) == (10, 20, 4, 30)
    assert entry.total == 64
    assert entry.date == actual
    assert entry.local_day == _local.local_day(actual)


def test_direct_message_compaction_and_branch_summary_usage_only(tmp_path):
    retained = _message("copied", _usage(input_=1000))
    compaction = {"type": "compaction", "id": "compact", "parentId": "assistant",
                  "timestamp": "2026-08-17T10:00:02.000Z", "usage": _usage(input_=3),
                  "retainedTail": [retained["message"]]}
    branch = {"type": "branch_summary", "id": "summary", "parentId": "compact",
              "timestamp": "2026-08-17T10:00:03.000Z", "usage": _usage(output=4)}
    path = _write(tmp_path, [
        _message("assistant", _usage(input_=1)),
        _message("tool-result", _usage(output=2), role="toolResult"),
        compaction,
        branch,
    ])
    entries = parse_file(path)
    assert {e.id for e in entries} == {"assistant", "tool-result", "compact", "summary"}
    assert sum(e.total for e in entries) == 10


def test_skips_aborted_and_errored_messages(tmp_path):
    path = _write(tmp_path, [
        _message("aborted", _usage(input_=10), stop_reason="aborted"),
        _message("errored", _usage(input_=20), stop_reason="error"),
        _message("complete", _usage(input_=30), stop_reason="stop"),
    ])
    assert [e.id for e in parse_file(path)] == ["complete"]


def test_fallback_malformed_and_partial_records_are_safe(tmp_path):
    total_only = {"type": "message", "id": "total-only", "parentId": None,
                  "timestamp": "2026-08-17T10:00:00.000Z",
                  "message": {"role": "assistant", "usage": {"totalTokens": 77}}}
    null_granular = {"type": "message", "id": "null-fields", "parentId": None,
                     "timestamp": "2026-08-17T10:00:00.000Z",
                     "message": {"role": "assistant", "usage": {
                         "input": None, "output": "not-a-number", "totalTokens": 88}}}
    negative = _message("negative", _usage(input_=-5, total=99))
    path = _write(tmp_path, [total_only, null_granular, negative,
                             {"type": "message", "id": "no-usage"}],
                  trailing=['{"type":"message","id":'])
    by_id = {e.id: e for e in parse_file(path)}
    assert by_id["total-only"].total == 77
    assert by_id["null-fields"].total == 88
    # Negative granular fields clamp to zero (not a fallback to totalTokens),
    # and a zero-token entry is not kept.
    assert "negative" not in by_id
    assert len(by_id) == 2


def test_absurd_fields_are_clamped(tmp_path):
    path = _write(tmp_path, [_message(
        "huge", _usage(input_=1e30, output=1e30, reasoning=1e30, cache_write=1e30, cache_read=12))])
    entry = parse_file(path)[0]
    assert (entry.input, entry.output, entry.cache_write, entry.cache_read) == (0, 0, 0, 12)


def test_global_envelope_id_dedup_removes_fork_copies(tmp_path):
    shared = _message("shared", _usage(input_=10))
    _write(tmp_path, [shared, _message("branch-a", _usage(input_=20))], sub="a")
    _write(tmp_path, [shared, _message("branch-b", _usage(input_=30))], sub="b")
    entries = PiProvider(home=tmp_path).scan_entries()
    assert {e.id for e in entries} == {"shared", "branch-a", "branch-b"}
    assert sum(e.total for e in entries) == 60


def test_session_roots_cover_default_and_overrides_and_remove_overlap(tmp_path, monkeypatch):
    agent = tmp_path / "custom-agent"
    sessions = tmp_path / "custom-sessions"
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent))
    monkeypatch.setenv("PI_CODING_AGENT_SESSION_DIR", str(sessions))
    assert set(session_roots(home=tmp_path)) == {
        tmp_path / ".pi/agent/sessions", agent / "sessions", sessions,
    }
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / ".pi/agent"))
    monkeypatch.setenv("PI_CODING_AGENT_SESSION_DIR", str(tmp_path / ".pi/agent/sessions/project"))
    assert session_roots(home=tmp_path) == [tmp_path / ".pi/agent/sessions"]


def test_provider_identity_and_cost_policy():
    provider = PiProvider()
    assert provider.id == "pi"
    assert provider.display_name == "Pi"
    assert provider.reports_cost is True


def test_attributes_model_from_envelope_and_message(tmp_path):
    omp_style = {"type": "message", "id": "omp-1", "timestamp": "2026-08-17T10:00:00.000Z",
                 "model": "openrouter/stealth/ox-alpha",
                 "message": {"role": "assistant", "content": [],
                             "usage": _usage(input_=100, output=200)}}
    path = _write(tmp_path, [omp_style, _message("pi-1", _usage(input_=10, output=20))])
    by_id = {e.id: e for e in parse_file(path)}
    assert by_id["omp-1"].model == "openrouter/stealth/ox-alpha"
    assert by_id["pi-1"].model == "model-name"


def test_provider_fetch_daily_prefers_source_cost(tmp_path):
    now = datetime.now(tz=UTC)
    iso = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    usage = _usage(input_=100, output=200)
    usage["cost"] = {"total": 0.01}
    _write(tmp_path, [
        _message("a", usage, envelope_ts=iso),
        # No source cost and an unpriced model: estimates to $0.
        _message("b", _usage(input_=10, output=20), envelope_ts=iso,
                 message_ts=now.timestamp() * 1000),
    ])
    daily = PiProvider(home=tmp_path).fetch_daily()
    assert daily is not None
    assert daily.total_tokens == 330
    assert daily.total_cost == pytest.approx(0.01)
    periods = PiProvider(home=tmp_path).fetch_periods()
    assert periods["month"]["tokens"] == 330
    assert periods["week"]["cost"] == pytest.approx(0.01)
