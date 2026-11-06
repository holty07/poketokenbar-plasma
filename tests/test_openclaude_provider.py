import json

from poketokenbar.providers.openclaude import OpenClaudeProvider, project_roots


def _write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def _assistant(mid, rid, ts, out):
    return {
        "type": "assistant",
        "requestId": rid,
        "timestamp": ts,
        "message": {
            "id": mid,
            "model": "claude-opus-5-5",
            "usage": {"input_tokens": 10, "output_tokens": out,
                      "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
        },
    }


def test_roots_only_openclaude(tmp_path):
    (tmp_path / ".claude" / "projects").mkdir(parents=True)
    assert project_roots(home=tmp_path, env={}) == []
    (tmp_path / ".openclaude" / "projects").mkdir(parents=True)
    assert project_roots(home=tmp_path, env={}) == [tmp_path / ".openclaude" / "projects"]


def test_matches_provider_interface(tmp_path):
    ts = "2026-11-06T03:00:00.000Z"
    _write(tmp_path / ".openclaude" / "projects" / "p" / "s.jsonl", [
        _assistant("m1", "r1", ts, 5),
        _assistant("m1", "r1", ts, 20),  # streamed update: keep the larger
        _assistant("m2", "r2", ts, 1),
    ])
    # Claude Code's own logs must not be picked up.
    _write(tmp_path / ".claude" / "projects" / "p" / "s.jsonl", [_assistant("x", "y", ts, 999)])
    provider = OpenClaudeProvider(home=tmp_path)
    entries = provider.scan_entries()  # no-arg, as history.py calls it
    assert sorted(e.output for e in entries) == [1, 20]
    day = entries[0].local_day
    daily = provider.fetch_daily(day)
    assert daily.output_tokens == 21
    assert provider.fetch_daily() is None or daily is not None  # default arg works
