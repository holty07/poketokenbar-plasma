"""Mirrors upstream OmpUsageTests.swift (PokeTokenBar #214, #276)."""

from datetime import UTC, datetime

import pytest

from poketokenbar.cache import ScanCache
from poketokenbar.providers.omp import OmpProvider, parse_file, session_roots

_SESSION_LINES = (
    '{"type":"session","version":3,"id":"019f9552","timestamp":"2026-07-03T01:00:00.000Z","cwd":"/Users/x/Proj"}',
    '{"type":"model_change","id":"aa01","parentId":null,"timestamp":"2026-07-03T01:00:01.000Z","model":"modal-k3/moonshotai/Kimi-K3"}',
    '{"type":"message","id":"bb01","parentId":"aa01","timestamp":"2026-07-03T01:00:05.000Z","message":{"role":"user","content":[{"type":"text","text":"hi"}]}}',
    '{"type":"message","id":"cc01","parentId":"bb01","timestamp":"2026-07-03T01:00:10.000Z","message":{"role":"assistant","content":[{"type":"text","text":"hello"}],"model":"moonshotai/Kimi-K3","usage":{"input":100,"output":10,"cacheRead":600,"cacheWrite":40,"totalTokens":750,"cost":{"input":0.001,"output":0.002,"cacheRead":0.002,"cacheWrite":0.0,"total":0.005}}}}',
    '{"type":"message","id":"dd01","parentId":"cc01","timestamp":"2026-07-03T01:00:12.000Z","message":{"role":"toolResult","toolCallId":"t1","content":[{"type":"text","text":"ok"}]}}',
    '{"type":"message","id":"ee01","parentId":"dd01","timestamp":"2026-07-03T01:01:00.000Z","message":{"role":"assistant","content":[],"model":"modal/nvidia/GLM-5.2","usage":{"input":5,"output":7,"cacheRead":0,"cacheWrite":3,"totalTokens":15,"cost":{"input":0,"output":0,"cacheRead":0,"cacheWrite":0,"total":0}}}}',
    '{"type":"custom","customType":"tool_execution_start","timestamp":"2026-07-03T01:01:05.000Z"}',
)
SESSION_JSONL = "\n".join(_SESSION_LINES)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("OMP_CODING_AGENT_DIR", raising=False)


def _project(home):
    directory = home / ".omp" / "agent" / "sessions" / "-Users-x-Proj"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _write(home, name, content):
    path = _project(home) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    return path


def test_parse_assistant_usage_mapping(tmp_path):
    path = _write(tmp_path, "2026-07-03T01-00-00-000Z_019f9552.jsonl", SESSION_JSONL)
    entries = parse_file(path)
    assert len(entries) == 2  # user / toolResult / custom excluded
    by_msg = {e.id.split("|")[-1]: e for e in entries}
    cc = by_msg["cc01"]
    assert cc.model == "moonshotai/Kimi-K3"
    assert (cc.input, cc.cache_read, cc.cache_write, cc.output) == (100, 600, 40, 10)
    assert cc.total == 750
    assert cc.explicit_cost == 0.005
    ee = by_msg["ee01"]
    assert ee.model == "modal/nvidia/GLM-5.2"
    assert ee.total == 15
    assert ee.explicit_cost == 0  # an explicit zero is preserved


def test_skips_non_assistant_and_malformed_lines(tmp_path):
    tricky_lines = (
            '{"type":"message","id":"u1","timestamp":"2026-07-03T01:00:05.000Z","message":{"role":"user","content":[{"type":"text","text":"what is my usage?"}]}}',
            '{"type":"message","id":"a1","timestamp":"2026-07-03T01:00:10.000Z","message":{"role":"assistant","content":[],"model":"m"}}',
            '{"type":"message","id":"a2","message":{"role":"assistant","usage":{"input":1,"output":1,"cacheRead":0,"cacheWrite":0}}}',
            '{"type":"message","id":"a3","timestamp":"2026-07-03T01:00:20.000Z","message":{"role":"assistant","stopReason":"aborted","usage":{"input":9,"output":1,"cacheRead":0,"cacheWrite":0}}}',
            '{"type":"compaction","id":"cp1","timestamp":"2026-07-03T01:00:30.000Z","usage":null}',
            '{"type":"message","id":"a4","timestamp":"2026-07-03T01:00:40.000Z","message":{"role":"assistant","usage":{"input":1,',
    )
    tricky = "\n".join(tricky_lines)
    assert parse_file(_write(tmp_path, "broken.jsonl", tricky)) == []


def test_unreadable_file_yields_none(tmp_path):
    assert parse_file(_write(tmp_path, "garbage.jsonl", b"\xff\xfe\x41\x42")) is None


def test_assistant_line_without_id_still_counts_with_a_stable_id(tmp_path):
    line = (
        '{"type":"message","timestamp":"2026-07-03T01:00:10.000Z","message":{"role":"assistant",'
        '"usage":{"input":10,"output":5,"cacheRead":0,"cacheWrite":0}}}'
    )
    path = _write(tmp_path, "noid.jsonl", line)
    entries = parse_file(path)
    assert len(entries) == 1
    assert entries[0].total == 15
    assert entries[0].id.startswith("omp|noid.jsonl|")
    assert parse_file(path)[0].id == entries[0].id


def test_duplicate_lines_within_file_dedup(tmp_path):
    line = (
        '{"type":"message","id":"cc01","timestamp":"2026-07-03T01:00:10.000Z","message":'
        '{"role":"assistant","usage":{"input":10,"output":5,"cacheRead":0,"cacheWrite":0}}}'
    )
    entries = parse_file(_write(tmp_path, "dup.jsonl", f"{line}\n{line}"))
    assert len(entries) == 1
    assert entries[0].total == 15


def test_branch_summary_envelope_usage_counts(tmp_path):
    line = (
        '{"type":"branch_summary","id":"bs01","timestamp":"2026-07-03T01:04:00.000Z","usage":'
        '{"input":40,"output":6,"cacheRead":0,"cacheWrite":0,"totalTokens":46,'
        '"cost":{"input":0,"output":0,"cacheRead":0,"cacheWrite":0,"total":0}}}'
    )
    entries = parse_file(_write(tmp_path, "branch.jsonl", line))
    assert len(entries) == 1
    assert entries[0].total == 46
    assert entries[0].model == "omp"


def test_partial_granular_usage_defaults_missing_buckets(tmp_path):
    line = (
        '{"type":"message","id":"pg01","timestamp":"2026-07-03T01:05:00.000Z",'
        '"message":{"role":"assistant","usage":{"input":7}}}'
    )
    e = parse_file(_write(tmp_path, "partial.jsonl", line))[0]
    assert e.input == 7
    assert e.output + e.cache_write + e.cache_read == 0


def test_total_token_only_usage_is_preserved_as_input(tmp_path):
    line = (
        '{"type":"message","id":"tt01","timestamp":"2026-07-03T01:06:00.000Z",'
        '"message":{"role":"assistant","usage":{"totalTokens":42}}}'
    )
    e = parse_file(_write(tmp_path, "totalonly.jsonl", line))[0]
    assert [e.input, e.output, e.cache_write, e.cache_read] == [42, 0, 0, 0]


def test_absurd_token_counts_are_clamped(tmp_path):
    line = (
        '{"type":"message","id":"hg01","timestamp":"2026-07-03T01:06:00.000Z",'
        '"message":{"role":"assistant","usage":{"input":1e30,"output":-3,"cacheRead":9}}}'
    )
    e = parse_file(_write(tmp_path, "huge.jsonl", line))[0]
    assert [e.input, e.output, e.cache_read] == [0, 0, 9]


def test_unreadable_file_inside_scanned_root_is_skipped(tmp_path):
    _write(tmp_path, "ok.jsonl", SESSION_JSONL)
    _write(tmp_path, "garbage.jsonl", b"\xff\xfe\x41\x42")
    assert len(OmpProvider(home=tmp_path).scan_entries()) == 2


def test_recurses_into_subagent_sessions_and_cache_round_trips(tmp_path):
    _write(tmp_path, "2026-07-03T01-00-00-000Z_019f9552.jsonl", SESSION_JSONL)
    advisor = (
        '{"type":"message","id":"ad01","timestamp":"2026-07-03T01:02:00.000Z","message":'
        '{"role":"assistant","usage":{"input":7,"output":3,"cacheRead":0,"cacheWrite":0}}}'
    )
    _write(tmp_path, "2026-07-03T01-00-00-000Z_019f9552/__advisor.jsonl", advisor)

    cache = ScanCache(tmp_path / "scan.db")
    entries = OmpProvider(cache=cache, home=tmp_path).scan_entries()
    assert len(entries) == 3
    assert sum(e.total for e in entries) == 750 + 15 + 10
    again = OmpProvider(cache=cache, home=tmp_path).scan_entries()
    assert len(again) == 3
    cache.close()


def test_bridge_directory_is_excluded(tmp_path):
    _write(tmp_path, "s3.jsonl", SESSION_JSONL)
    bridge = tmp_path / ".omp" / "agent" / "sessions" / "bridge"
    bridge.mkdir(parents=True)
    (bridge / "converted.jsonl").write_text(
        '{"type":"message","id":"br01","timestamp":"2026-07-03T01:03:00.000Z","message":'
        '{"role":"assistant","usage":{"input":9,"output":1,"cacheRead":0,"cacheWrite":0}}}',
        encoding="utf-8",
    )
    assert len(OmpProvider(home=tmp_path).scan_entries()) == 2


def test_bridge_above_the_scan_root_does_not_hide_sessions(tmp_path):
    home = tmp_path / "bridge" / "home"
    _write(home, "s.jsonl", SESSION_JSONL)
    assert len(OmpProvider(home=home).scan_entries()) == 2


def test_session_roots(tmp_path, monkeypatch):
    assert session_roots(home=tmp_path) == [tmp_path / ".omp/agent/sessions"]
    monkeypatch.setenv("OMP_CODING_AGENT_DIR", str(tmp_path / "agent"))
    assert session_roots(home=tmp_path) == [
        tmp_path / ".omp/agent/sessions", tmp_path / "agent/sessions",
    ]


def test_daily_prefers_source_cost_for_unknown_model(tmp_path):
    entries = parse_file(_write(tmp_path, "s2.jsonl", SESSION_JSONL))
    provider = OmpProvider(home=tmp_path)
    daily = provider.fetch_daily(today=entries[0].local_day)
    assert daily.total_tokens == 765
    assert daily.total_cost == pytest.approx(0.005)


def test_provider_fetch_daily_keeps_source_cost(tmp_path):
    iso = datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    _write(
        tmp_path,
        "today.jsonl",
        "\n".join(
            [
                (
                    f'{{"type":"message","id":"m1","timestamp":"{iso}","message":{{"role":"assistant",'
                    '"content":[],"model":"moonshotai/Kimi-K3","usage":{"input":100,"output":200,'
                    '"cacheRead":0,"cacheWrite":0,"totalTokens":300,"cost":{"total":0.01}}}}'
                ),
                (
                    f'{{"type":"message","id":"m2","timestamp":"{iso}","message":{{"role":"assistant",'
                    '"content":[],"model":"modal/nvidia/GLM-5.2","usage":{"input":10,"output":20,'
                    '"cacheRead":0,"cacheWrite":0,"totalTokens":30,"cost":{"total":0.002}}}}'
                ),
            ]
        ),
    )
    provider = OmpProvider(home=tmp_path)
    assert provider.id == "omp"
    daily = provider.fetch_daily()
    assert daily.total_tokens == 330
    assert daily.total_cost == pytest.approx(0.012)
