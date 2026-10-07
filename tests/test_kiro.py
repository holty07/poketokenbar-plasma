"""Mirrors upstream KiroUsageTests.swift and KiroContentBlockTests.swift
(PokeTokenBar #178, #236, #383, #307)."""

import json
import sqlite3
import time
from datetime import UTC, datetime

import pytest

from poketokenbar.cache import ScanCache
from poketokenbar.providers.kiro import (
    KiroProvider,
    parse_cli_jsonl,
    parse_database,
    roots,
    source_files,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in ("KIRO_CLI_HOME", "KIRO_HOME", "XDG_DATA_HOME"):
        monkeypatch.delenv(key, raising=False)


# --- fixtures --------------------------------------------------------------


def _turn(timestamp_ms, model, user_text, assistant_text="", response_bytes=0):
    meta = {"request_start_timestamp_ms": timestamp_ms, "response_size": response_bytes,
            "time_between_chunks": [], "tool_use_ids_and_names": []}
    if model is not None:
        meta["model_id"] = model
    return {"user": {"content": user_text}, "assistant": {"content": assistant_text},
            "request_metadata": meta}


def _conversation(conv_id, turns):
    return json.dumps({"conversation_id": conv_id, "history": turns, "latest_summary": None})


def _db(root):
    return root / "data.sqlite3"


def _seed_v2(root, rows):
    conn = sqlite3.connect(_db(root))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS conversations_v2 (conversation_id TEXT PRIMARY KEY,"
        " key TEXT, created_at INTEGER, updated_at INTEGER, value TEXT)"
    )
    conn.executemany(
        "INSERT INTO conversations_v2 VALUES (?, '/Users/dev/project', 0, 0, ?)", rows
    )
    conn.commit()
    conn.close()


def _seed_v1(root, rows):
    conn = sqlite3.connect(_db(root))
    conn.execute("CREATE TABLE IF NOT EXISTS conversations (key TEXT PRIMARY KEY, value TEXT)")
    conn.executemany("INSERT INTO conversations VALUES (?, ?)", rows)
    conn.commit()
    conn.close()


def _seed_cli(root, session_id, turns, model="claude-sonnet-4-5"):
    lines = []
    for index, (prompt, assistant, ts) in enumerate(turns, start=1):
        lines.append(json.dumps({"version": "v1", "kind": "Prompt", "data": {
            "message_id": f"prompt-{index}",
            "content": [{"kind": "text", "data": prompt}], "meta": {"timestamp": ts}}}))
        lines.append(json.dumps({"version": "v1", "kind": "AssistantMessage", "data": {
            "message_id": f"assistant-{index}",
            "content": [{"kind": "text", "data": assistant}]}}))
    companion = {"session_id": session_id, "cwd": "/tmp/project",
                 "session_state": {"rts_model_state": {"model_info": {"model_id": model}}}}
    _seed_cli_raw(root, session_id, "\n".join(lines) + "\n", json.dumps(companion))


def _seed_cli_raw(root, session_id, jsonl, companion=None):
    cli = root / "sessions" / "cli"
    cli.mkdir(parents=True, exist_ok=True)
    (cli / f"{session_id}.jsonl").write_text(jsonl, encoding="utf-8")
    (cli / f"{session_id}.json").write_text(
        companion or json.dumps({"session_id": session_id, "cwd": "/tmp"}), encoding="utf-8"
    )
    return cli / f"{session_id}.jsonl"


def _session_dir(root, workspace, session_id):
    directory = root / "sessions" / workspace / session_id
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _entries(root):
    """Scan one root the way the provider does, without the merge memory."""
    from poketokenbar.providers import _local
    from poketokenbar.providers.kiro import parse_source

    found = []
    for path in source_files([root]):
        if path.exists():
            found.extend(parse_source(path) or [])
    return _local.dedup_keep_max(found)


# --- SQLite token accounting ------------------------------------------------


def test_first_turn_input_is_the_user_message_byte_estimate(tmp_path):
    _seed_v2(tmp_path, [("conv-1", _conversation("conv-1", [
        _turn(1_780_000_000_000, "claude-sonnet-4.5", "u" * 400, response_bytes=200)]))])
    entries = _entries(tmp_path)
    assert len(entries) == 1
    e = entries[0]
    assert (e.input, e.output, e.cache_read, e.cache_write) == (100, 50, 0, 0)
    assert e.model == "claude-sonnet-4.5"
    assert e.id == "kiro|conv-1|1780000000000"
    assert e.explicit_cost == 0.0  # byte estimate, never priced


def test_later_turn_input_includes_accumulated_history(tmp_path):
    _seed_v2(tmp_path, [("conv-1", _conversation("conv-1", [
        _turn(1_780_000_000_000, "m", "u" * 400, "a" * 800, 800),
        _turn(1_780_000_100_000, "m", "u" * 40, response_bytes=40)]))])
    second = next(e for e in _entries(tmp_path) if e.id == "kiro|conv-1|1780000100000")
    assert second.input == (400 + 800 + 40) // 4


def test_skipped_turns_still_contribute_to_later_history(tmp_path):
    missing_ts = {"user": {"content": "u" * 400}, "assistant": {"content": "a" * 400},
                  "request_metadata": {"response_size": 0, "model_id": "m"}}
    _seed_v2(tmp_path, [("conv-1", _conversation("conv-1", [
        missing_ts, _turn(1_780_000_000_000, "m", "u" * 40, response_bytes=40)]))])
    entries = _entries(tmp_path)
    assert len(entries) == 1
    assert entries[0].input == (400 + 400 + 40) // 4


def test_latest_summary_seeds_the_accumulated_history(tmp_path):
    turn = json.dumps(_turn(1_780_000_000_000, "m", "u" * 40, response_bytes=40))
    value = f'{{"conversation_id":"conv-1","latest_summary":["{"s" * 120}"],"history":[{turn}]}}'
    _seed_v2(tmp_path, [("conv-1", value)])
    assert _entries(tmp_path)[0].input == (120 + 40) // 4


def test_rescanning_produces_stable_ids(tmp_path):
    _seed_v2(tmp_path, [("conv-1", _conversation("conv-1", [
        _turn(1_780_000_000_000, "m", "u" * 400, "a" * 200, 200),
        _turn(1_780_000_100_000, "m", "u" * 40, response_bytes=40)]))])
    first = parse_database(_db(tmp_path))
    second = parse_database(_db(tmp_path))
    assert {e.id for e in first} == {e.id for e in second}


def test_missing_model_falls_back_to_unknown(tmp_path):
    _seed_v2(tmp_path, [("conv-1", _conversation("conv-1", [
        _turn(1_780_000_000_000, None, "u" * 40, response_bytes=40)]))])
    assert _entries(tmp_path)[0].model == "unknown"


def test_zero_byte_turns_are_skipped(tmp_path):
    _seed_v2(tmp_path, [("conv-1", _conversation("conv-1", [
        _turn(1_780_000_000_000, "m", "", response_bytes=0),
        _turn(1_780_000_001_000, "m", "u" * 4, response_bytes=0)]))])
    assert [e.id for e in _entries(tmp_path)] == ["kiro|conv-1|1780000001000"]


def test_images_field_is_excluded_from_the_byte_estimate(tmp_path):
    turn = _turn(1_780_000_000_000, "m", "u" * 40, response_bytes=0)
    turn["user"]["images"] = ["x" * 1_000_000]
    _seed_v2(tmp_path, [("conv-1", _conversation("conv-1", [turn]))])
    assert _entries(tmp_path)[0].input == 10


def test_reads_the_v1_conversations_table(tmp_path):
    _seed_v1(tmp_path, [("/Users/dev/project", _conversation("conv-2", [
        _turn(1_780_000_000_000, "m", "u" * 800, response_bytes=400)]))])
    e = _entries(tmp_path)[0]
    assert e.id == "kiro|conv-2|1780000000000"
    assert (e.input, e.output) == (200, 100)


def test_v1_row_without_conversation_id_is_skipped(tmp_path):
    value = ('{"history":[{"request_metadata":{"request_start_timestamp_ms":1780000000000,'
             '"model_id":"m","response_size":200}}]}')
    _seed_v1(tmp_path, [("/Users/dev/project", value)])
    assert _entries(tmp_path) == []


def test_malformed_json_rows_are_skipped_in_both_schemas(tmp_path):
    _seed_v2(tmp_path, [("conv-1", "{not valid json")])
    _seed_v1(tmp_path, [("/Users/dev/project", "{not valid json")])
    assert parse_database(_db(tmp_path)) == []


def test_huge_values_do_not_crash_and_are_clamped(tmp_path):
    value = ('{"conversation_id":"conv-huge","history":['
             '{"user":{"content":"hi"},"request_metadata":{"request_start_timestamp_ms":1e30,'
             '"model_id":"m","response_size":1e30}},'
             '{"user":{"content":"' + "u" * 38 + '"},"request_metadata":'
             '{"request_start_timestamp_ms":1780000000000,"model_id":"m","response_size":1e30}}]}')
    _seed_v2(tmp_path, [("conv-huge", value)])
    entries = _entries(tmp_path)
    # The unrepresentable timestamp's turn is skipped but still joins history;
    # the absurd response_size clamps to zero output.
    assert [(e.input, e.output) for e in entries] == [((2 + 38) // 4, 0)]


def test_conversation_without_history_array_is_skipped(tmp_path):
    _seed_v2(tmp_path, [("conv-1", '{"conversation_id":"conv-1"}')])
    assert _entries(tmp_path) == []


def test_same_conversation_in_both_tables_is_not_double_counted(tmp_path):
    turns = [_turn(1_780_000_000_000, "m", "u" * 400, response_bytes=200)]
    _seed_v2(tmp_path, [("conv-1", _conversation("conv-1", turns))])
    _seed_v1(tmp_path, [("/Users/dev/project", _conversation("conv-1", turns))])
    assert len(_entries(tmp_path)) == 1


def test_accepts_a_direct_database_path_as_root(tmp_path):
    _seed_v2(tmp_path, [("conv-1", _conversation("conv-1", [
        _turn(1_780_000_000_000, "m", "u" * 400, response_bytes=200)]))])
    assert len(_entries(_db(tmp_path))) == 1


def test_nonexistent_root_returns_nothing(tmp_path):
    assert _entries(tmp_path / "nope") == []


# --- roots -----------------------------------------------------------------


def test_default_roots_are_linux_data_dir_and_kiro_sessions(tmp_path):
    assert roots(home=tmp_path) == [
        tmp_path / ".local/share/kiro-cli", tmp_path / ".kiro/sessions",
    ]


def test_root_env_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    assert roots(home=tmp_path)[0] == tmp_path / "data/kiro-cli"
    monkeypatch.setenv("KIRO_CLI_HOME", f"{tmp_path}/a, {tmp_path}/b")
    monkeypatch.setenv("KIRO_HOME", str(tmp_path / "k"))
    assert roots(home=tmp_path) == [tmp_path / "a", tmp_path / "b", tmp_path / "k/sessions"]


# --- JSONL sessions ----------------------------------------------------------


def test_cli_jsonl_session_is_read_from_writer_shaped_events(tmp_path):
    _seed_cli(tmp_path, "session-1", [("hello world", "response text", 1_770_983_426.420942)])
    entries = _entries(tmp_path)
    assert len(entries) == 1
    e = entries[0]
    assert e.model == "claude-sonnet-4-5"
    assert (e.input, e.output) == (11 // 4, 13 // 4)
    assert e.id == "kiro|cli|session-1|1770983426420"


def test_cli_jsonl_later_turn_accumulates_history(tmp_path):
    _seed_cli(tmp_path, "session-2", [
        ("u" * 400, "a" * 800, 1_770_983_426.0),
        ("u" * 40, "a" * 40, 1_770_983_526.0),
    ])
    second = next(e for e in _entries(tmp_path) if e.id.endswith("|1770983526000"))
    assert second.input == (400 + 800 + 40) // 4
    assert second.output == 40 // 4


def test_extra_kiro_home_with_only_jsonl_yields_usage(tmp_path):
    kiro_home = tmp_path / ".kiro"
    _seed_cli(kiro_home, "session-1", [("u" * 400, "a" * 200, 1_770_983_426.420942)])
    entries = _entries(kiro_home)
    assert [(e.input, e.output) for e in entries] == [(100, 50)]


def test_v3_messages_jsonl_session_is_read(tmp_path):
    directory = _session_dir(tmp_path, "my-project", "sess_abc")
    ts = "2026-06-20T10:00:00Z"
    (directory / "session.json").write_text(json.dumps({
        "schemaVersion": "1.0.0", "id": "sess_abc", "modelId": "claude-sonnet-4-5",
        "createdAt": ts, "lastModifiedAt": ts}), encoding="utf-8")
    (directory / "messages.jsonl").write_text("\n".join([
        json.dumps({"timestamp": ts, "payload": {"type": "user", "content": "u" * 400}}),
        json.dumps({"timestamp": ts, "payload": {"type": "assistant", "content": "a" * 200}}),
        json.dumps({"payload": {"type": "usage_summary", "promptTurnSummaries": [{"usage": 2.5}]}}),
        json.dumps({"payload": {"type": "turn_end"}, "timestamp": ts}),
    ]), encoding="utf-8")
    entries = _entries(tmp_path)
    assert len(entries) == 1
    e = entries[0]
    assert (e.model, e.input, e.output, e.id) == ("claude-sonnet-4-5", 100, 50, "kiro|v3|sess_abc|0")
    assert e.explicit_cost == 0.0  # credits are not dollars


def _flat_session(root, session_id, turns, created_at="2026-06-30T12:57:10.991Z"):
    directory = _session_dir(root, "ws", session_id)
    (directory / "session.json").write_text(json.dumps({
        "schemaVersion": "1.0.0", "id": session_id,
        "createdAt": created_at, "lastModifiedAt": created_at}), encoding="utf-8")
    lines = []
    for prompt, assistant in turns:
        lines += [json.dumps({"role": "user", "content": prompt}),
                  json.dumps({"role": "assistant", "content": assistant})]
    (directory / "messages.jsonl").write_text("\n".join(lines), encoding="utf-8")


def test_v3_flat_role_messages_jsonl_is_read(tmp_path):
    _flat_session(tmp_path, "sess_flat", [("u" * 400, "a" * 200)])
    assert [(e.input, e.output) for e in _entries(tmp_path)] == [(100, 50)]


def test_v3_flat_multi_turn_without_timestamps_keeps_every_turn(tmp_path):
    _flat_session(tmp_path, "sess_multi", [("u" * 400, "a" * 200), ("u" * 40, "a" * 40)])
    assert {e.id for e in _entries(tmp_path)} == {"kiro|v3|sess_multi|0", "kiro|v3|sess_multi|1"}


def test_v3_tool_call_args_count_toward_output(tmp_path):
    directory = _session_dir(tmp_path, "ws", "sess_tools")
    (directory / "session.json").write_text(json.dumps({
        "id": "sess_tools", "modelId": "m", "createdAt": "2026-06-20T10:00:00Z"}), encoding="utf-8")
    (directory / "messages.jsonl").write_text("\n".join([
        json.dumps({"timestamp": "2026-06-20T10:00:00Z",
                    "payload": {"type": "user", "content": "u" * 400}}),
        json.dumps({"payload": {"type": "tool_call", "args": "t" * 200}}),
        json.dumps({"payload": {"type": "turn_end"}, "timestamp": "2026-06-20T10:00:05Z"}),
    ]), encoding="utf-8")
    e = _entries(tmp_path)[0]
    assert (e.input, e.output) == (100, 50)


def test_sqlite_and_jsonl_sessions_are_both_counted(tmp_path):
    _seed_v2(tmp_path, [("conv-sqlite", _conversation("conv-sqlite", [
        _turn(1_780_000_000_000, "m", "u" * 400, response_bytes=200)]))])
    _seed_cli(tmp_path, "session-jsonl", [("u" * 400, "a" * 200, 1_770_983_426.0)])
    ids = {e.id for e in _entries(tmp_path)}
    assert len(ids) == 2
    assert any(i.startswith("kiro|conv-sqlite|") for i in ids)
    assert any(i.startswith("kiro|cli|session-jsonl|") for i in ids)


def test_malformed_jsonl_lines_are_skipped(tmp_path):
    lines = (
        (
            '{"version":"v1","kind":"Prompt","data":{"message_id":"prompt-3","content":'
            '[{"kind":"text","data":"hello world"}],"meta":{"timestamp":1770983426.420942}}}'
        ),
        "not valid json at all",
        (
            '{"version":"v1","kind":"AssistantMessage","data":{"message_id":"assistant-3",'
            '"content":[{"kind":"text","data":"response text"}]}}'
        ),
    )
    _seed_cli_raw(tmp_path, "session-3", "\n".join(lines))
    entries = _entries(tmp_path)
    assert len(entries) == 1
    assert entries[0].input + entries[0].output > 0


def test_unrelated_jsonl_files_are_ignored(tmp_path):
    (tmp_path / "notes.jsonl").write_text('{"role":"user","content":"ignore me please"}\n')
    assert _entries(tmp_path) == []


# --- CLI 2.25 content blocks (#383) ------------------------------------------

TS = 1_770_983_426.0


def _block(kind, data):
    return {"kind": kind, "data": data}


def _message(kind, content):
    return {"version": "v1", "kind": kind, "data": {"content": content}}


def _prompt(text="abcd", at=None, extra=()):
    return {"version": "v1", "kind": "Prompt", "data": {
        "content": [_block("text", text), *extra], "meta": {"timestamp": at or TS}}}


def _blocks_file(tmp_path, events):
    path = tmp_path / "cli" / "synthetic.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e, sort_keys=True) + "\n" for e in events))
    return path


def test_nested_tool_results_count_text_and_json_once(tmp_path):
    event = _message("ToolResults", [_block("toolResult", {
        "toolUseId": "tool-1", "status": "success", "content": [
            _block("text", "r" * 800),
            _block("json", {"stdout": "가나ab", "nested": ["more", 12, None]}),
        ]})])
    event["data"]["results"] = {"tool-1": {"result": {"Success": {"items": "duplicate" * 100}}}}
    path = _blocks_file(tmp_path, [_prompt(), _message("AssistantMessage", [_block("text", "done")]),
                                   event])
    e = parse_cli_jsonl(path)[0]
    assert e.input == (4 + 800 + 14) // 4
    assert e.output == 1


def test_tool_arguments_and_thinking_count_but_metadata_and_images_do_not(tmp_path):
    opaque = "x" * 1024
    image = _block("image", {"format": "png", "source": {"bytes": opaque}})
    path = _blocks_file(tmp_path, [
        _prompt(extra=[image]),
        _message("AssistantMessage", [
            _block("thinking", {"text": "think think!", "signature": opaque,
                                "redactedContent": [opaque], "modelId": "model",
                                "toolsDigest": opaque}),
            _block("toolUse", {"name": opaque, "toolUseId": opaque,
                               "input": {"path": "file", "content": "abcd"}}),
            _block("text", "done"),
        ]),
        _message("ToolResults", [_block("toolResult", {"content": [image]})]),
    ])
    e = parse_cli_jsonl(path)[0]
    assert e.input == 1
    assert e.output == (12 + 8 + 4) // 4


def test_tool_only_response_does_not_disappear(tmp_path):
    path = _blocks_file(tmp_path, [_prompt(text=""), _message("AssistantMessage", [
        _block("toolUse", {"name": "test_tool", "toolUseId": "tool-1",
                           "input": {"command": "abcd"}})])])
    e = parse_cli_jsonl(path)[0]
    assert (e.input, e.output) == (0, 1)


def test_tool_content_accumulates_across_days_and_clear_resets_history(tmp_path):
    path = _blocks_file(tmp_path, [
        _prompt(),
        _message("AssistantMessage", [_block("thinking", {"text": "plan"}),
                                      _block("toolUse", {"input": {"path": "file", "content": "abcd"}}),
                                      _block("text", "done")]),
        _message("ToolResults", [_block("toolResult", {"content": [
            _block("text", "abcdefgh"), _block("json", {"stdout": "ijklmnop"})]})]),
        _prompt(at=TS + 86_400), _message("AssistantMessage", [_block("text", "done")]),
        _message("Clear", []),
        _prompt(at=TS + 86_500), _message("AssistantMessage", [_block("text", "done")]),
    ])
    entries = sorted(parse_cli_jsonl(path), key=lambda e: e.date)
    assert [e.input for e in entries] == [5, 10, 1]
    assert [e.output for e in entries] == [4, 1, 1]


def test_missing_and_unknown_block_payloads_do_not_invent_content(tmp_path):
    path = _blocks_file(tmp_path, [_prompt(), _message("AssistantMessage", [
        _block("toolUse", {}), _block("thinking", {"signature": "opaque"}),
        _block("thinking", None), _block("unsupported", {"text": "do not count"}),
        _block("text", "done"),
    ]), _message("ToolResults", [_block("toolResult", {}), _block("toolResult", {"content": [
        {"kind": "json"}, _block("json", None)]})])])
    e = parse_cli_jsonl(path)[0]
    assert (e.input, e.output) == (1, 1)


# --- provider ----------------------------------------------------------------


def test_late_tool_result_updates_same_entry_and_cache_skips_unchanged(tmp_path):
    home = tmp_path
    now = time.time()
    path = _seed_cli_raw(home / ".kiro", "s1", "".join(
        json.dumps(e) + "\n"
        for e in [_prompt(at=now), _message("AssistantMessage", [_block("text", "done")])]
    ))
    cache = ScanCache(tmp_path / "scan.db")
    provider = KiroProvider(cache=cache, home=home)
    first = provider.scan_entries()
    assert [(e.input, e.output) for e in first] == [(1, 1)]
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_message("ToolResults", [_block("toolResult", {
            "content": [_block("text", "abcdefghijklmnop")]})])) + "\n")
    updated = provider.scan_entries()
    assert [e.id for e in updated] == [e.id for e in first]
    assert [(e.input, e.output) for e in updated] == [(5, 1)]
    daily = provider.fetch_daily()
    assert daily.total_tokens == 6
    assert daily.total_cost == 0.0
    cache.close()


def test_cleared_turns_stay_counted_for_the_process(tmp_path):
    now = time.time() * 1000
    db_root = tmp_path / ".local/share/kiro-cli"
    db_root.mkdir(parents=True)
    _seed_v2(db_root, [("conv-1", _conversation("conv-1", [
        _turn(now, "m", "u" * 400, response_bytes=200)]))])
    provider = KiroProvider(home=tmp_path)
    assert provider.fetch_daily().total_tokens == 150
    conn = sqlite3.connect(_db(db_root))
    conn.execute("UPDATE conversations_v2 SET value = ?",
                 (_conversation("conv-1", []),))
    conn.commit()
    conn.close()
    assert provider.fetch_daily().total_tokens == 150
    assert KiroProvider(home=tmp_path).fetch_daily() is None


def test_provider_identity():
    provider = KiroProvider()
    assert provider.id == "kiro"
    assert provider.display_name == "Kiro"
    assert provider.reports_cost is False


def test_daily_aggregates_every_turn_of_the_day(tmp_path):
    _seed_v2(tmp_path, [("conv-1", _conversation("conv-1", [
        _turn(1_780_000_000_000, "m", "u" * 400, response_bytes=200),
        _turn(1_780_000_100_000, "m", "", response_bytes=40)]))])
    entries = _entries(tmp_path)
    day = entries[0].local_day
    assert datetime.fromtimestamp(1_780_000_000, tz=UTC).astimezone().strftime("%Y-%m-%d") == day
    from poketokenbar.providers import _local

    assert _local.daily(entries, day).total_tokens == sum(e.total for e in entries)
