"""Grok Build CLI usage — mirrors upstream GrokUsageTests.swift."""

import time
from datetime import UTC, datetime

import pytest

from poketokenbar.providers.grok import (
    GrokProvider,
    _parse_file,
    _parse_updates_line,
    session_roots,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("GROK_HOME", raising=False)


def _root(home):
    return home / ".grok" / "sessions"


def _write_session(home, lines, session_id="s1", session_kind=None):
    directory = _root(home) / "cwd-group" / session_id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "updates.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if session_kind:
        (directory / "summary.json").write_text(
            f'{{"session_kind":"{session_kind}"}}', encoding="utf-8"
        )
    return path


CHUNK_LINE = (
    '{"timestamp":1785000000,"method":"_x.ai/session/update",'
    '"params":{"sessionId":"s1","update":{"sessionUpdate":"agent_message_chunk",'
    '"content":{"type":"text","text":"hi"}},"_meta":{"totalTokens":100,"eventId":"e0",'
    '"agentTimestampMs":1785000000000,"chunkId":0}}}'
)


def turn_line(prompt_id="p-1", input_=41_203, output=812, cached_read=38_400,
              total=42_015, cost_ticks=12_000_000_000, model="grok-build-1",
              envelope_seconds=1_785_000_010, agent_timestamp_ms=1_785_000_010_000,
              is_replay=False, snake_case=False, cost_is_partial=False,
              usage_is_incomplete=False):
    if snake_case:
        usage = [
            '"input_tokens":' + str(input_),
            '"output_tokens":' + str(output),
            '"total_tokens":' + str(total),
            '"cached_read_tokens":' + str(cached_read),
        ]
    else:
        usage = [
            '"inputTokens":' + str(input_),
            '"outputTokens":' + str(output),
            '"totalTokens":' + str(total),
            '"cachedReadTokens":' + str(cached_read),
            '"reasoningTokens":260',
            '"modelCalls":3',
            '"numTurns":1',
        ]
        if cost_ticks is not None:
            usage.append('"costUsdTicks":' + str(cost_ticks))
        if cost_is_partial:
            usage.append('"costIsPartial":true')
        if usage_is_incomplete:
            usage.append('"usageIsIncomplete":true')
        if model:
            usage.append(
                '"modelUsage":{"' + model + '":{'
                '"inputTokens":' + str(input_) + ','
                '"outputTokens":' + str(output) + ','
                '"totalTokens":' + str(total) + ','
                '"cachedReadTokens":' + str(cached_read) + '}'
                '}'
            )
    meta = [
        '"totalTokens":' + str(total),
        '"eventId":"ev-' + prompt_id + '"',
        '"promptId":"' + prompt_id + '"',
    ]
    if agent_timestamp_ms:
        meta.append('"agentTimestampMs":' + str(agent_timestamp_ms))
    if is_replay:
        meta.append('"isReplay":true')
    ts = ('"timestamp":' + str(envelope_seconds) + ',') if envelope_seconds is not None else ""
    return (
        '{' + ts +
        '"method":"_x.ai/session.update",'
        '"params":{"sessionId":"s1","update":{"sessionUpdate":"turn_completed",'
        '"prompt_id":"' + prompt_id + '","stop_reason":"end_turn","usage":{'
        + ','.join(usage) + '}},'
        '"_meta":{' + ','.join(meta) + '}}}'
    )


def test_parses_turn_completed_token_mapping(tmp_path):
    path = _write_session(tmp_path, [CHUNK_LINE, turn_line()])
    entries = _parse_file(path)
    assert len(entries) == 1
    e = entries[0]
    # input = inputTokens(41203) - cachedReadTokens(38400) = 2803
    assert e.input == 2_803
    assert e.cache_read == 38_400
    # output includes reasoning
    assert e.output == 812
    assert e.cache_write == 0
    assert e.total == 42_015
    assert e.model == "grok-build-1"
    # 12e10 ticks = $1.2
    assert abs(e.explicit_cost - 1.2) < 1e-9


def test_skips_chunk_lines(tmp_path):
    path = _write_session(tmp_path, [CHUNK_LINE, CHUNK_LINE])
    assert _parse_file(path) == []


def test_headless_snake_case_input_not_cache_adjusted(tmp_path):
    line = turn_line(prompt_id="p-snake", input_=60, output=10, cached_read=40,
                     total=110, cost_ticks=None, snake_case=True)
    path = _write_session(tmp_path, [line], session_id="s2")
    entries = _parse_file(path)
    assert len(entries) == 1
    e = entries[0]
    assert e.input == 60  # snake_case already excludes cache
    assert e.cache_read == 40
    assert e.output == 10
    assert e.total == 110


def test_multiple_turns_aggregate(tmp_path):
    path = _write_session(tmp_path, [
        CHUNK_LINE, turn_line(prompt_id="p-1"), CHUNK_LINE,
        turn_line(prompt_id="p-2", input_=100, output=20, cached_read=0,
                  total=120, cost_ticks=None),
    ])
    entries = _parse_file(path)
    assert len(entries) == 2
    assert sum(e.total for e in entries) == 42_015 + 120


def test_replay_lines_not_counted(tmp_path):
    path = _write_session(tmp_path, [
        turn_line(prompt_id="p-1"),
        turn_line(prompt_id="p-1", is_replay=True),
    ])
    entries = _parse_file(path)
    assert len(entries) == 1
    assert entries[0].total == 42_015

    # isReplay with a different id is dropped too
    path2 = _write_session(tmp_path, [
        turn_line(prompt_id="p-live"),
        turn_line(prompt_id="p-replayed", is_replay=True),
    ], session_id="s4-replay")
    entries2 = _parse_file(path2)
    assert [e.id for e in entries2] == ["grok|p-live"]


def test_forked_session_dedup_by_prompt_id(tmp_path):
    _write_session(tmp_path, [turn_line(prompt_id="p-1")], session_id="parent")
    _write_session(tmp_path, [turn_line(prompt_id="p-1")], session_id="child-fork", session_kind="fork")
    provider = GrokProvider(home=tmp_path)
    entries = provider.scan_entries()
    assert len(entries) == 1
    assert entries[0].total == 42_015


def test_subagent_skipped_but_user_and_worktree_kept(tmp_path):
    _write_session(tmp_path, [turn_line(prompt_id="p-main")], session_id="main")
    _write_session(tmp_path, [turn_line(prompt_id="p-sub")], session_id="sub", session_kind="subagent")
    _write_session(tmp_path, [turn_line(prompt_id="p-sub2")], session_id="sub2", session_kind="subagent_fork")
    _write_session(tmp_path, [turn_line(prompt_id="p-wt")], session_id="wt", session_kind="worktree")
    provider = GrokProvider(home=tmp_path)
    ids = {e.id for e in provider.scan_entries()}
    assert ids == {"grok|p-main", "grok|p-wt"}


def test_untrustworthy_costs_dropped(tmp_path):
    path = _write_session(tmp_path, [turn_line(prompt_id="p-partial", cost_is_partial=True)])
    entries = _parse_file(path)
    assert entries[0].explicit_cost is None

    path2 = _write_session(tmp_path, [turn_line(prompt_id="p-incomplete", usage_is_incomplete=True)])
    entries2 = _parse_file(path2)
    assert entries2[0].explicit_cost is None


def test_grok_names_never_inherit_other_family_pricing():
    from poketokenbar import pricing
    for name in ["grok-build-1", "grok-4-fast", "grok-code-fast-1", "grok-codex-next", "grok-4o-mini"]:
        assert pricing.rate(name) == pricing.ZERO, f"{name} should price at 0"


def test_zero_usage_turn_produces_no_entry(tmp_path):
    path = _write_session(tmp_path, [turn_line(prompt_id="p-zero", input_=0, output=0,
                                               cached_read=0, total=0, cost_ticks=None)])
    assert _parse_file(path) == []


def test_agent_timestamp_wins_over_envelope_write_time(tmp_path):
    path = _write_session(tmp_path, [turn_line(
        prompt_id="p-old", envelope_seconds=1_785_600_000,
        agent_timestamp_ms=1_785_000_010_000,
    )])
    entries = _parse_file(path)
    assert entries[0].date == datetime.fromtimestamp(1_785_000_010, tz=UTC)


def test_envelope_seconds_used_when_agent_timestamp_missing(tmp_path):
    path = _write_session(tmp_path, [turn_line(
        prompt_id="p-ts", envelope_seconds=1_785_000_010, agent_timestamp_ms=None,
    )])
    entries = _parse_file(path)
    assert entries[0].date == datetime.fromtimestamp(1_785_000_010, tz=UTC)


def test_null_token_fields_use_snake_case(tmp_path):
    line = (
        '{"timestamp":1785000030,"method":"_x.ai/session/update",'
        '"params":{"sessionId":"s5","update":{"sessionUpdate":"turn_completed",'
        '"prompt_id":"p-null","stop_reason":"end_turn","usage":{'
        '"inputTokens":null,"input_tokens":60,"outputTokens":null,"output_tokens":10,'
        '"cachedReadTokens":40,"totalTokens":110}},"_meta":{'
        '"eventId":"ev-null","agentTimestampMs":1785000030000}}}'
    )
    path = _write_session(tmp_path, [line], session_id="nulls")
    entries = _parse_file(path)
    assert entries[0].input == 60
    assert entries[0].output == 10
    assert entries[0].cache_read == 40
    assert entries[0].total == 110


def test_cache_read_clamped_to_prompt_total(tmp_path):
    path = _write_session(tmp_path, [turn_line(
        prompt_id="p-clamp", input_=1_000, output=50, cached_read=1_200,
        total=1_050, cost_ticks=None,
    )])
    entries = _parse_file(path)
    assert entries[0].input == 0
    assert entries[0].cache_read == 1_000
    assert entries[0].total == 1_050


def test_residual_goes_to_output(tmp_path):
    path = _write_session(tmp_path, [turn_line(
        prompt_id="p-res", input_=500, output=10, cached_read=100,
        total=700, cost_ticks=None,
    )])
    entries = _parse_file(path)
    assert entries[0].total == 700
    assert entries[0].input == 400
    assert entries[0].cache_read == 100
    assert entries[0].output == 200


def test_missing_model_usage_falls_back_to_grok(tmp_path):
    path = _write_session(tmp_path, [turn_line(prompt_id="p-nm", model=None)])
    entries = _parse_file(path)
    assert entries[0].model == "grok"
    assert entries[0].total == 42_015


def test_file_without_turn_completed_yields_nothing(tmp_path):
    path = _write_session(tmp_path, [CHUNK_LINE, CHUNK_LINE, CHUNK_LINE], session_id="chunks")
    assert _parse_file(path) == []


def test_provider_fetch_daily_and_periods(tmp_path):
    now = int(time.time() * 1000)
    _write_session(tmp_path, [turn_line(
        prompt_id="p-1", input_=1000, output=100, cached_read=400,
        total=1100, cost_ticks=None, agent_timestamp_ms=now,
    )])
    provider = GrokProvider(home=tmp_path)
    assert provider.id == "grok"
    daily = provider.fetch_daily()
    assert daily is not None
    assert daily.total_tokens == 1100
    assert provider.fetch_periods()["month"]["tokens"] == 1100


def test_provider_without_data_returns_none(tmp_path):
    assert GrokProvider(home=tmp_path).fetch_daily() is None


def test_session_roots_default(tmp_path):
    assert session_roots(home=tmp_path) == [tmp_path / ".grok" / "sessions"]


def test_session_roots_env(monkeypatch, tmp_path):
    monkeypatch.setenv("GROK_HOME", "/opt/grok")
    roots = session_roots(home=tmp_path)
    assert any(str(p) == "/opt/grok/sessions" for p in roots)
