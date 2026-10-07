"""Codex parsing on synthetic rollouts — runs without the Swift fixtures."""

import json

from poketokenbar.providers.codex import CodexProvider, parse_rollout, session_roots


def _token_count(last, cumulative=None, ts="2026-09-10T12:00:00Z"):
    info = {"last_token_usage": last}
    if cumulative is not None:
        info["total_token_usage"] = cumulative
    return json.dumps(
        {"timestamp": ts, "type": "event_msg", "payload": {"type": "token_count", "info": info}}
    )


def _usage(input_=0, cached=0, output=0, total=None):
    if total is None:
        total = input_ + output
    return {
        "input_tokens": input_,
        "cached_input_tokens": cached,
        "output_tokens": output,
        "total_tokens": total,
    }


def _write(path, *lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    return path


def test_total_only_turn_without_cumulative_counts_its_total(tmp_path):
    f = _write(tmp_path / "r.jsonl", _token_count(_usage(total=1200)))
    [e] = parse_rollout(f).entries
    assert (e.input, e.output, e.cache_read, e.total) == (1200, 0, 0, 1200)


def test_total_only_session_counts_every_turn(tmp_path):
    f = _write(
        tmp_path / "r.jsonl",
        _token_count(_usage(total=100), _usage(total=100)),
        _token_count(_usage(total=50), _usage(total=150)),
    )
    assert sum(e.total for e in parse_rollout(f).entries) == 150


def test_mid_session_total_only_turn_counts_when_cumulative_grew(tmp_path):
    f = _write(
        tmp_path / "r.jsonl",
        _token_count(_usage(100, 20, 10), _usage(100, 20, 10)),
        _token_count(_usage(total=40), _usage(130, 20, 20)),
    )
    first, second = parse_rollout(f).entries
    assert first.total == 110
    assert (second.input, second.total) == (40, 40)


def test_fork_zero_context_turn_stays_zero(tmp_path):
    # Replayed parent turn, then a zero-component turn whose total is not
    # part of any cumulative growth.
    f = _write(
        tmp_path / "r.jsonl",
        _token_count(_usage(100, 20, 10), _usage(100, 20, 10)),
        _token_count(_usage(total=999), _usage(100, 20, 10)),
    )
    assert [e.total for e in parse_rollout(f).entries] == [110, 0]


def test_component_turns_are_unchanged(tmp_path):
    f = _write(tmp_path / "r.jsonl", _token_count(_usage(100, 30, 5, total=105)))
    [e] = parse_rollout(f).entries
    assert (e.input, e.cache_read, e.output) == (70, 30, 5)


def test_archived_sessions_are_scanned(tmp_path):
    _write(
        tmp_path / ".codex" / "sessions" / "2026" / "09" / "10" / "rollout-a.jsonl",
        _token_count(_usage(10, 0, 1), _usage(10, 0, 1)),
    )
    _write(
        tmp_path / ".codex" / "archived_sessions" / "rollout-b.jsonl",
        _token_count(_usage(20, 0, 2), _usage(20, 0, 2)),
    )
    assert [r.name for r in session_roots(tmp_path)] == ["sessions", "archived_sessions"]
    entries = CodexProvider(home=tmp_path).scan_entries()
    assert sorted(e.total for e in entries) == [11, 22]


def test_a_rollout_caught_mid_archive_counts_once(tmp_path):
    line = _token_count(_usage(10, 0, 1), _usage(10, 0, 1))
    _write(tmp_path / ".codex" / "sessions" / "rollout-a.jsonl", line)
    _write(tmp_path / ".codex" / "archived_sessions" / "rollout-a.jsonl", line)
    assert [e.total for e in CodexProvider(home=tmp_path).scan_entries()] == [11]
