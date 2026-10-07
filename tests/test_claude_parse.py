import json

from poketokenbar.providers import claude


def _line(**over):
    obj = {
        "type": "assistant",
        "timestamp": "2026-08-18T20:50:59.023Z",
        "requestId": "req_1",
        "message": {
            "id": "msg_1",
            "model": "claude-opus-5",
            "usage": {
                "input_tokens": 2,
                "output_tokens": 570,
                "cache_creation_input_tokens": 24_155,
                "cache_read_input_tokens": 16_203,
            },
        },
    }
    obj.update(over)
    return json.dumps(obj)


def test_parse_line_extracts_four_token_kinds():
    e = claude.parse_line(_line())
    assert e is not None
    assert (e.input, e.output, e.cache_write, e.cache_read) == (2, 570, 24_155, 16_203)
    assert e.total == 40_930
    assert e.model == "claude-opus-5"


def test_parse_line_id_is_message_id_joined_with_request_id():
    assert claude.parse_line(_line()).id == "msg_1|req_1"


def test_parse_line_ignores_non_assistant_rows():
    assert claude.parse_line(_line(type="user")) is None


def test_parse_line_ignores_rows_without_usage():
    obj = json.loads(_line())
    del obj["message"]["usage"]
    assert claude.parse_line(json.dumps(obj)) is None


def test_parse_line_survives_malformed_json():
    assert claude.parse_line("{not json") is None


def test_parse_line_ignores_nested_iterations_totals():
    # Real logs repeat the same counts inside usage.iterations[]. Summing them
    # would double-count every turn. Only top-level fields may be read.
    obj = json.loads(_line())
    obj["message"]["usage"]["iterations"] = [
        {
            "input_tokens": 2,
            "output_tokens": 570,
            "cache_read_input_tokens": 16_203,
            "cache_creation_input_tokens": 24_155,
        }
    ]
    assert claude.parse_line(json.dumps(obj)).total == 40_930


def test_dedup_keeps_the_largest_total_per_id():
    # Streaming and session resume re-log the same (message.id, requestId) with
    # a growing output while input/cacheRead stay fixed. Keeping the first
    # occurrence under-counts cost badly.
    partial = claude.parse_line(_line())
    complete = claude.parse_line(_line())
    partial.output = 10
    complete.output = 570
    kept = claude.dedup_keep_max([partial, complete])
    assert len(kept) == 1
    assert kept[0].output == 570


def test_dedup_keeps_distinct_ids():
    a = claude.parse_line(_line(requestId="req_1"))
    b = claude.parse_line(_line(requestId="req_2"))
    assert len(claude.dedup_keep_max([a, b])) == 2


def test_parse_file_dedups_within_the_file(tmp_path):
    f = tmp_path / "s.jsonl"
    small = json.loads(_line())
    small["message"]["usage"]["output_tokens"] = 1
    f.write_text(json.dumps(small) + "\n" + _line() + "\n", encoding="utf-8")
    entries = claude.parse_file(f)
    assert len(entries) == 1
    assert entries[0].output == 570


def test_parse_file_returns_empty_for_unreadable_path(tmp_path):
    assert claude.parse_file(tmp_path / "missing.jsonl") == []


def _cost_state(model_usage, **over):
    obj = {"type": "cost-state", "modelUsage": model_usage}
    obj.update(over)
    return json.dumps(obj)


def _assistant(msg_id, model, output):
    return _line(
        requestId=f"req_{msg_id}",
        message={
            "id": msg_id,
            "model": model,
            "usage": {"input_tokens": 0, "output_tokens": output},
        },
    )


def test_cost_state_strips_context_window_suffix_and_pools_variants():
    state = claude.parse_cost_state_line(
        _cost_state(
            {
                "claude-opus-5[1m]": {"costUSD": 1.5},
                "claude-opus-5": {"costUSD": 0.5},
                "claude-haiku-4-5": {"costUSD": -1},  # nonsense, dropped
                "claude-sonnet-5": {"costUSD": "1"},  # not a number, dropped
            }
        )
    )
    assert state == {"claude-opus-5": 2.0}


def test_cost_state_ignores_other_rows():
    assert claude.parse_cost_state_line(_line()) is None
    assert claude.parse_cost_state_line(_cost_state({})) is None


def test_parse_file_spreads_the_last_cost_state_by_tokens(tmp_path):
    f = tmp_path / "s.jsonl"
    f.write_text(
        "\n".join(
            [
                _assistant("a", "claude-opus-5", 100),
                _cost_state({"claude-opus-5[1m]": {"costUSD": 99.0}}),  # superseded
                _assistant("b", "claude-opus-5", 300),
                _assistant("c", "claude-sonnet-5", 50),
                # Cumulative ledger: only this final record counts.
                _cost_state(
                    {
                        "claude-opus-5[1m]": {"costUSD": 4.0},
                        "claude-haiku-4-5": {"costUSD": 7.0},  # no entry: dropped
                    }
                ),
            ]
        )
        + "\n"
    )
    by_id = {e.id: e for e in claude.parse_file(f)}
    assert by_id["a|req_a"].explicit_cost == 1.0
    assert by_id["b|req_b"].explicit_cost == 3.0
    # A model the ledger doesn't cover keeps the table estimate.
    assert by_id["c|req_c"].explicit_cost is None


def test_reported_cost_wins_over_the_table(tmp_path):
    from poketokenbar import pricing

    f = tmp_path / "s.jsonl"
    f.write_text(
        _assistant("a", "some-new-claude", 1000)
        + "\n"
        + _cost_state({"some-new-claude": {"costUSD": 0.42}})
        + "\n"
    )
    [entry] = claude.parse_file(f)
    assert pricing.cost(entry.model, 0, 1000, 0, 0) == 0.0
    assert pricing.entry_cost(entry) == 0.42


def test_absurd_or_negative_token_counts_count_as_zero():
    # upstream #307: a corrupt line must not wreck the day's totals.
    e = claude.parse_line(_line(message={"id": "m", "model": "x", "usage": {
        "input_tokens": 10**15, "output_tokens": -5,
        "cache_creation_input_tokens": True, "cache_read_input_tokens": 7}}))
    assert (e.input, e.output, e.cache_write, e.cache_read) == (0, 0, 0, 7)
