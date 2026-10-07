"""Mirrors upstream CursorUsageTests.swift (PokeTokenBar #197, #426, #307).
No test reaches the network: every dashboard call goes through an injected
fetcher."""

import base64
import json
import sqlite3
import time
from datetime import UTC, datetime, timedelta

import pytest

from poketokenbar.providers import _local
from poketokenbar.providers.cursor import (
    CursorProvider,
    account_identifier,
    auth_value,
    fetch_filtered_events,
    has_next_page,
    parse_bubble,
    parse_usage_event,
    roots,
    workos_session_cookie,
)

SINCE = datetime(2025, 1, 1, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in ("CURSOR_DATA_DIR", "CURSOR_SESSION_TOKEN", "CURSOR_USAGE_API", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(key, raising=False)


def _no_network(*_args):
    raise AssertionError("network must not be touched")


def _storage(home):
    path = home / ".config" / "Cursor" / "User" / "globalStorage"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _execute(db, script):
    conn = sqlite3.connect(db)
    conn.executescript(script)
    conn.commit()
    conn.close()


def _bubbles(db, rows):
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE IF NOT EXISTS cursorDiskKV (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB)")
    conn.executemany("INSERT INTO cursorDiskKV VALUES (?, ?)",
                     [(k, json.dumps(v) if isinstance(v, dict) else v) for k, v in rows])
    conn.commit()
    conn.close()


def _bubble(input_, output, created, model="gpt-4o"):
    return {"tokenCount": {"inputTokens": input_, "outputTokens": output},
            "createdAt": created, "modelType": model}


def _jwt(sub="user_01TEST"):
    payload = base64.urlsafe_b64encode(json.dumps({"sub": sub}).encode()).decode().rstrip("=")
    return f"hdr.{payload}.sig"


# --- local bubbles -------------------------------------------------------------


def test_reads_bubble_tokens_from_state_db(tmp_path):
    db = _storage(tmp_path) / "state.vscdb"
    _bubbles(db, [
        ("bubbleId:tab-1:msg-1", _bubble(1500, 800, "2026-01-04T10:34:54.766Z", "claude-3.5-sonnet")),
        ("composerData:other", {"unrelated": True}),
        ("bubbleId:tab-1:msg-zero", _bubble(0, 0, "2026-01-04T11:00:00.000Z")),
        ("bubbleid:tab-1:wrong-case", _bubble(999, 1, "2026-01-04T12:00:00.000Z")),
    ])
    entries = CursorProvider(home=tmp_path, fetcher=_no_network).scan_entries()
    assert len(entries) == 1  # GLOB is case-sensitive
    e = entries[0]
    assert (e.input, e.output, e.model) == (1500, 800, "claude-3.5-sonnet")
    assert e.id == "cursor|bubbleId:tab-1:msg-1"
    assert e.explicit_cost == 0.0


def test_incremental_scan_picks_up_new_rows_and_resets_on_replacement(tmp_path):
    db = _storage(tmp_path) / "state.vscdb"
    _bubbles(db, [(f"bubbleId:t:{i}", _bubble(10 + i, 5, f"2026-01-04T10:0{i}:00.000Z"))
                  for i in range(3)])
    provider = CursorProvider(home=tmp_path, fetcher=_no_network)
    assert len(provider.scan_entries()) == 3
    _bubbles(db, [("bubbleId:t:9", _bubble(1, 1, "2026-01-04T11:00:00.000Z"))])
    assert len(provider.scan_entries()) == 4
    # A replaced database restarts rowids below the high-water: full rescan.
    db.unlink()
    _bubbles(db, [("bubbleId:new:0", _bubble(7, 3, "2026-01-04T12:00:00.000Z"))])
    assert [e.id for e in provider.scan_entries()] == ["cursor|bubbleId:new:0"]


def test_unreadable_database_keeps_last_good_bubbles(tmp_path):
    db = _storage(tmp_path) / "state.vscdb"
    _bubbles(db, [("bubbleId:t:0", _bubble(10, 5, "2026-01-04T10:00:00.000Z"))])
    provider = CursorProvider(home=tmp_path, fetcher=_no_network)
    assert len(provider.scan_entries()) == 1
    db.write_bytes(b"not a database at all, definitely not")
    assert len(provider.scan_entries()) == 1


@pytest.mark.parametrize(
    ("created", "expected"),
    [(1_767_312_000_000, 15), (1_767_312_000, 15), ("2026-01-04T10:34:54.766Z", 15)],
)
def test_parse_bubble_accepts_numeric_and_iso_created_at(created, expected):
    entry = parse_bubble(_bubble(10, 5, created), "bubbleId:t:m")
    assert entry is not None
    assert entry.total == expected


@pytest.mark.parametrize(
    "bubble",
    [
        _bubble(0, 0, "2026-01-04T10:00:00Z"),
        {"type": 1, "modelType": "gpt-4", "createdAt": "2026-01-04T10:00:00Z"},
        {"tokenCount": {"inputTokens": 100, "outputTokens": 50}},
        _bubble(100, 50, "not-a-date"),
    ],
)
def test_parse_bubble_skips_unusable_rows(bubble):
    assert parse_bubble(bubble, "bubbleId:t:m") is None


def test_parse_bubble_defaults_model_and_prefixes_id():
    entry = parse_bubble({"tokenCount": {"inputTokens": 100, "outputTokens": 50},
                          "createdAt": "2026-01-04T10:00:00Z"}, "bubbleId:abc:def")
    assert entry.model == "unknown"
    assert entry.id == "cursor|bubbleId:abc:def"


def test_parse_bubble_clamps_absurd_counts():
    entry = parse_bubble(_bubble(1e30, 5, "2026-01-04T10:00:00Z"), "bubbleId:t:m")
    assert (entry.input, entry.output) == (0, 5)


def test_nonexistent_path_returns_nothing(tmp_path):
    assert CursorProvider(home=tmp_path, fetcher=_no_network).scan_entries() == []


# --- roots / auth -------------------------------------------------------------


def test_default_roots_include_stable_and_nightly(tmp_path, monkeypatch):
    assert roots(home=tmp_path) == [
        tmp_path / ".config/Cursor/User/globalStorage",
        tmp_path / ".config/Cursor Nightly/User/globalStorage",
    ]
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    assert roots(home=tmp_path)[0] == tmp_path / "cfg/Cursor/User/globalStorage"
    monkeypatch.setenv("CURSOR_DATA_DIR", "/a, /b")
    assert [str(p) for p in roots(home=tmp_path)] == ["/a", "/b"]


def test_workos_session_cookie_builds_sub_double_colon_jwt():
    jwt = _jwt()
    assert workos_session_cookie(jwt) == f"user_01TEST::{jwt}"
    assert workos_session_cookie(f"x::{jwt}") == f"x::{jwt}"


def test_cache_identifier_uses_account_subject():
    jwt = _jwt()
    assert account_identifier(jwt) == "subject:user_01TEST"
    assert account_identifier(f"user_01TEST::{jwt}") == "subject:user_01TEST"
    assert account_identifier("opaque-token-a") != account_identifier("opaque-token-b")
    assert "opaque" not in account_identifier("opaque-token-a")


def test_auth_access_token_reads_item_table(tmp_path):
    _execute(_storage(tmp_path) / "state.vscdb",
             "CREATE TABLE ItemTable (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB);"
             "INSERT INTO ItemTable VALUES ('cursorAuth/accessToken', 'test-session-token-abc');")
    assert auth_value("cursorAuth/accessToken", roots(home=tmp_path)) == "test-session-token-abc"


# --- dashboard events -----------------------------------------------------------


def test_parse_usage_event_maps_token_usage_fields():
    entry = parse_usage_event({
        "timestamp": "1750979225854", "model": "claude-opus-5-thinking-high",
        "tokenUsage": {"inputTokens": 126, "outputTokens": 450,
                       "cacheWriteTokens": 6112, "cacheReadTokens": 11964, "totalCents": 12.5},
    }, 0, SINCE)
    assert (entry.input, entry.output, entry.cache_write, entry.cache_read) == (126, 450, 6112, 11964)
    assert entry.explicit_cost == pytest.approx(0.125)


def test_parse_usage_event_clamps_huge_token_counts():
    entry = parse_usage_event({
        "timestamp": "1750979225854", "model": "gpt-5",
        "tokenUsage": {"inputTokens": 1e30, "outputTokens": 1e30,
                       "cacheWriteTokens": 1e30, "cacheReadTokens": 3},
    }, 0, SINCE)
    assert (entry.input, entry.output, entry.cache_write, entry.cache_read) == (0, 0, 0, 3)


def test_event_ids_use_global_row_index_or_stable_id():
    event = {"timestamp": "1750979225854", "model": "gpt", "tokenUsage": {"inputTokens": 1}}
    assert parse_usage_event(event, 0, SINCE).id != parse_usage_event(event, 100, SINCE).id
    stable = dict(event, id="evt-stable-123")
    assert parse_usage_event(stable, 0, SINCE).id == "cursor|api|evt-stable-123"


def test_usage_event_date_accepts_seconds_precision_epoch():
    event = {"timestamp": "1750979225", "model": "gpt", "tokenUsage": {"inputTokens": 10}}
    assert parse_usage_event(event, 0, SINCE).input == 10


def test_has_next_page():
    assert has_next_page(None, None, 1, 100)
    assert not has_next_page(None, None, 1, 42)
    assert has_next_page(None, 239, 1, 100)
    assert has_next_page(None, 239, 2, 100)
    assert not has_next_page(None, 239, 3, 39)
    assert not has_next_page({"hasNextPage": False}, 1000, 1, 100)
    assert has_next_page({"numPages": 3}, None, 2, 1)


class _Recorder:
    def __init__(self, respond):
        self.calls = []
        self._respond = respond

    def __call__(self, url, headers, body, timeout):
        request = json.loads(body)
        self.calls.append((url, headers, request, timeout))
        result = self._respond(request, headers)
        if result is None:
            return None
        payload, status = result
        return (payload if isinstance(payload, bytes) else json.dumps(payload).encode()), status


def test_request_sends_epoch_millisecond_range_and_cookie_auth():
    fetcher = _Recorder(lambda req, h: ({"usageEventsDisplay": [], "totalUsageEventsCount": 0}, 200))
    entries, reason = fetch_filtered_events(_jwt(), SINCE, fetcher)
    assert (entries, reason) == ([], None)
    url, headers, request, _ = fetcher.calls[0]
    assert url == "https://cursor.com/api/dashboard/get-filtered-usage-events"
    assert request["startDate"] == "1735689600000"
    assert int(request["endDate"]) > 1_700_000_000_000
    assert headers["Cookie"] == f"WorkosCursorSessionToken=user_01TEST::{_jwt()}"


def test_rejected_cookie_falls_back_to_bearer():
    def respond(req, headers):
        if "Cookie" in headers:
            return b"", 401
        return {"usageEvents": [{"timestamp": "1750979225854", "model": "gpt",
                                 "tokenUsage": {"inputTokens": 5}}]}, 200
    fetcher = _Recorder(respond)
    entries, reason = fetch_filtered_events("tok", SINCE, fetcher)
    assert reason is None
    assert len(entries) == 1
    assert fetcher.calls[1][1]["Authorization"] == "Bearer tok"


def test_reads_usage_events_display_key():
    fetcher = _Recorder(lambda req, h: ({"usageEventsDisplay": [{
        "timestamp": "1750979225854", "model": "composer-2.5-fast",
        "tokenUsage": {"inputTokens": 12, "outputTokens": 34}}], "totalUsageEventsCount": 1}, 200))
    entries, reason = fetch_filtered_events("tok", SINCE, fetcher)
    assert reason is None
    assert [e.model for e in entries] == ["composer-2.5-fast"]


def test_paginates_across_pages():
    def respond(req, _headers):
        if req["page"] == 1:
            events = [{"timestamp": "1750979225854", "model": "gpt",
                       "tokenUsage": {"inputTokens": i + 1}} for i in range(100)]
            return {"usageEvents": events, "pagination": {"hasNextPage": True}}, 200
        return {"usageEvents": [{"timestamp": "1750979225854", "model": "gpt",
                                 "tokenUsage": {"inputTokens": 999}}],
                "pagination": {"hasNextPage": False}}, 200
    fetcher = _Recorder(respond)
    entries, reason = fetch_filtered_events("tok", SINCE, fetcher)
    assert reason is None
    assert [c[2]["page"] for c in fetcher.calls] == [1, 2]
    assert len(entries) == 101


@pytest.mark.parametrize("total", [None, 101])
def test_without_pagination_metadata_keeps_paginating_while_full(total):
    def respond(req, _headers):
        page = req["page"]
        count = 100 if page == 1 else 1
        obj = {"usageEvents": [{"timestamp": "1750979225854", "model": "gpt",
                                "tokenUsage": {"inputTokens": page * 1000 + i + 1}}
                               for i in range(count)]}
        if total is not None:
            obj["totalUsageEventsCount"] = total
        return obj, 200
    fetcher = _Recorder(respond)
    entries, reason = fetch_filtered_events("tok", SINCE, fetcher)
    assert reason is None
    assert [c[2]["page"] for c in fetcher.calls] == [1, 2]
    assert len(entries) == 101


def test_returns_failure_reason_for_http_error():
    fetcher = _Recorder(lambda req, h: (b"rate limited", 429))
    assert fetch_filtered_events("tok", SINCE, fetcher) == (
        None, "http 429 on page 1 (12 bytes) rate limited")


def test_transport_error_is_a_failure():
    assert fetch_filtered_events("tok", SINCE, _Recorder(lambda r, h: None)) == (
        None, "transport error on page 1")


def test_empty_page_is_success():
    fetcher = _Recorder(lambda req, h: ({"usageEvents": [], "pagination": {"hasNextPage": False}}, 200))
    assert fetch_filtered_events("tok", SINCE, fetcher) == ([], None)


# --- provider: dashboard vs bubbles ----------------------------------------------


def _signed_in_home(tmp_path):
    storage = _storage(tmp_path)
    _execute(storage / "state.vscdb",
             "CREATE TABLE ItemTable (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB);"
             f"INSERT INTO ItemTable VALUES ('cursorAuth/accessToken', '{_jwt()}');")
    now = datetime.now(UTC)
    _bubbles(storage / "state.vscdb", [
        ("bubbleId:tab-1:live", _bubble(999, 888, now.strftime("%Y-%m-%dT%H:%M:%S.000Z")))])
    return now


def test_dashboard_entries_replace_bubble_estimates(tmp_path):
    now = _signed_in_home(tmp_path)
    millis = str(int(now.timestamp() * 1000))
    fetcher = _Recorder(lambda req, h: ({"usageEvents": [{
        "timestamp": millis, "model": "gpt",
        "tokenUsage": {"inputTokens": 1000, "outputTokens": 500, "totalCents": 3}}]}, 200))
    provider = CursorProvider(home=tmp_path, fetcher=fetcher)
    daily = provider.fetch_daily()
    assert daily.total_tokens == 1500
    assert daily.total_cost == pytest.approx(0.03)
    # Throttled: the next poll reuses the response without another request.
    provider.fetch_periods()
    assert len(fetcher.calls) == 1


def test_empty_dashboard_result_suppresses_bubble_fallback(tmp_path):
    _signed_in_home(tmp_path)
    fetcher = _Recorder(lambda req, h: ({"usageEvents": []}, 200))
    assert CursorProvider(home=tmp_path, fetcher=fetcher).scan_entries() == []


def test_failed_dashboard_falls_back_to_bubbles_then_reuses_last_good(tmp_path):
    now = _signed_in_home(tmp_path)
    millis = str(int(now.timestamp() * 1000))
    state = {"fail": True}
    clock = {"t": time.time()}

    def respond(req, headers):
        if state["fail"]:
            return b"boom", 500
        return {"usageEvents": [{"timestamp": millis, "model": "gpt",
                                 "tokenUsage": {"inputTokens": 10}}]}, 200

    provider = CursorProvider(home=tmp_path, fetcher=_Recorder(respond), clock=lambda: clock["t"])
    assert [e.id for e in provider.scan_entries()] == ["cursor|bubbleId:tab-1:live"]
    assert provider.last_error.startswith("http 500")
    state["fail"] = False
    clock["t"] += 301
    assert [e.input for e in provider.scan_entries()] == [10]
    state["fail"] = True
    clock["t"] += 301
    assert [e.input for e in provider.scan_entries()] == [10]  # last good response
    clock["t"] += 7 * 3600
    assert [e.id for e in provider.scan_entries()] == ["cursor|bubbleId:tab-1:live"]


def test_usage_api_can_be_disabled(tmp_path, monkeypatch):
    _signed_in_home(tmp_path)
    monkeypatch.setenv("CURSOR_USAGE_API", "0")
    entries = CursorProvider(home=tmp_path, fetcher=_no_network).scan_entries()
    assert [e.id for e in entries] == ["cursor|bubbleId:tab-1:live"]


def test_scan_window_covers_week_and_month():
    start = _local.scan_window_start(datetime(2026, 10, 7, 15, 0, tzinfo=UTC))
    assert start <= datetime(2026, 10, 1, tzinfo=UTC) + timedelta(days=1)
    assert start <= datetime(2026, 9, 30, 15, 0, tzinfo=UTC)


def test_provider_identity():
    provider = CursorProvider(fetcher=_no_network)
    assert (provider.id, provider.display_name, provider.reports_cost) == ("cursor", "Cursor", True)
