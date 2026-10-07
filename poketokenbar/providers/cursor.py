"""Cursor usage — ports upstream's Cursor reader and dashboard client
(CursorUsageAPI.swift; PokeTokenBar #197, #426, plus the #307 clamp).

Two sources, in order of trust:

1. Cursor's dashboard API (unofficial personal-account endpoint,
   `POST https://cursor.com/api/dashboard/get-filtered-usage-events`), which
   reports real per-request token usage and `totalCents`. It is used whenever
   a session token is available; a successful response is authoritative even
   when empty — local estimates are then not mixed in (#197).
2. Otherwise, the chat bubbles in Cursor's own `state.vscdb`
   (`cursorDiskKV`, keys `bubbleId:*`, JSON `tokenCount.{inputTokens,
   outputTokens}`, `createdAt`, `modelType`). These counts are often zero in
   newer Cursor builds, which is why the API exists; cost is $0 for them.

Auth needs no Keychain: the IDE stores its login JWT in plain SQLite, in
`state.vscdb`'s `ItemTable` under `cursorAuth/accessToken`, on every OS.
`$CURSOR_SESSION_TOKEN` overrides it (a `WorkosCursorSessionToken` cookie
value works too). The request tries the dashboard cookie form
(`<sub>::<jwt>`) first and falls back to `Authorization: Bearer` on 401/403.
Pagination follows `pagination.hasNextPage` / `numPages`, else
`totalUsageEventsCount`, else "keep going while pages are full" (#426), with
the range sent as epoch-millisecond strings (ISO dates get HTTP 500).

Networking is stdlib `urllib` behind an injectable `fetcher`, so tests never
touch the network. The API is polled at most every five minutes; between
polls (and for up to six hours after a failed one) the last good response is
reused. `$CURSOR_USAGE_API=0` disables it. Upstream's on-disk response cache,
the "remaining included usage" limits panel (#273) and its auth-expired
notice (#412) are not ported — this port's limits pipeline is Claude-only.

The local bubble scan is incremental like upstream's: `cursorDiskKV` rows are
written with `ON CONFLICT REPLACE`, so a changed bubble gets a new rowid, and
after the first full read only rows past the highest rowid seen are walked.
A rowid high-water that goes backwards (VACUUM, a replaced database) forces a
full rescan. That state lives in memory, so it is not kept in the ScanCache.

Paths: Cursor is a VS Code fork, which keeps `User/globalStorage` under
`~/Library/Application Support/<App>` on macOS and `$XDG_CONFIG_HOME/<App>`
(default `~/.config`) on Linux — so `~/.config/Cursor/User/globalStorage`
and `~/.config/Cursor Nightly/User/globalStorage`. `$CURSOR_DATA_DIR`
(comma-separated) overrides both, as upstream.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import math
import os
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from ..cache import ScanCache
from ..models import DailyUsage, Entry, ProviderEnrichment
from . import _local
from .base import with_extra_roots

PARSER_VERSION = 1

API_URL = "https://cursor.com/api/dashboard/get-filtered-usage-events"
PAGE_SIZE = 100
MAX_PAGES = 200
REQUEST_TIMEOUT = 10.0
FETCH_DEADLINE = 60.0
REFRESH_SECONDS = 300.0
STALE_MAX_AGE = 6 * 3600.0
_AUTH_MODES = ("cookie", "bearer")

# (url, headers, body, timeout) -> (response body, HTTP status), or None on a
# transport error.
Fetcher = Callable[[str, dict[str, str], bytes, float], tuple[bytes, int] | None]


# --- paths / auth ------------------------------------------------------------


def roots(home: Path | None = None) -> list[Path]:
    override = _local.env_paths("CURSOR_DATA_DIR")
    if override is not None:
        return override
    config = _local.xdg_config_home(home)
    return [
        config / "Cursor" / "User" / "globalStorage",
        config / "Cursor Nightly" / "User" / "globalStorage",
    ]


def database_path(root: Path) -> Path:
    return root if root.suffix == ".vscdb" else root / "state.vscdb"


def auth_value(key: str, scan_roots: list[Path]) -> str | None:
    """A value from the IDE's `ItemTable` (first root that has it)."""
    for root in scan_roots:
        conn = _local.open_readonly(database_path(root))
        if conn is None:
            continue
        try:
            row = conn.execute(
                "SELECT value FROM ItemTable WHERE key = ? LIMIT 1", (key,)
            ).fetchone()
        except sqlite3.Error:
            row = None
        finally:
            conn.close()
        if row is None:
            continue
        value = row[0]
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        value = _local.text(value)
        if value is not None:
            return value
    return None


def session_token(scan_roots: list[Path]) -> str | None:
    override = _local.text(os.environ.get("CURSOR_SESSION_TOKEN"))
    return override or auth_value("cursorAuth/accessToken", scan_roots)


def jwt_subject(jwt: str) -> str | None:
    parts = jwt.split(".")
    if len(parts) < 2:
        return None
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, TypeError):
        return None
    subject = claims.get("sub") if isinstance(claims, dict) else None
    return subject if isinstance(subject, str) and subject else None


def workos_session_cookie(token: str) -> str:
    """The dashboard cookie is `sub::jwt`, not the bare access-token JWT."""
    if "::" in token or "%3A%3A" in token:
        return token
    subject = jwt_subject(token)
    return f"{subject}::{token}" if subject else token


def account_identifier(token: str) -> str:
    """Which account a cached response belongs to — never the token itself."""
    decoded = urllib.parse.unquote(token)
    head, sep, _ = decoded.partition("::")
    if sep and head:
        return f"subject:{head}"
    subject = jwt_subject(decoded)
    if subject:
        return f"subject:{subject}"
    return "token:" + hashlib.sha256(decoded.encode("utf-8")).hexdigest()


# --- dashboard API -----------------------------------------------------------


def _string(value) -> str | None:
    if isinstance(value, str):
        return value or None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and math.isfinite(value):
        return str(int(value)) if value.is_integer() else str(value)
    return None


def _optional_int(value) -> int | None:
    """None when absent/unreadable — 0 would read as "no events left"."""
    if _local.number(value) is not None:
        return _local.tokens(value)
    if isinstance(value, str):
        try:
            return _local.tokens(int(value.replace(",", "")))
        except ValueError:
            return None
    return None


def _float(value) -> float | None:
    number = _local.number(value)
    if number is None and isinstance(value, str):
        try:
            number = float(value)
        except ValueError:
            return None
    return number if number is not None and math.isfinite(number) else None


def epoch_date(value: float) -> datetime | None:
    if value > 1_000_000_000_000:
        return _local.from_epoch(value / 1000)
    if value >= 1_000_000_000:
        return _local.from_epoch(value)
    return None


def usage_event_date(event: dict) -> datetime | None:
    raw = event.get("timestamp")
    if isinstance(raw, str) and raw:
        try:
            return epoch_date(float(raw))
        except ValueError:
            return _local.parse_iso(raw)
    number = _local.number(raw)
    return epoch_date(number) if number is not None else None


def parse_usage_event(event: dict, row_index: int, since: datetime) -> Entry | None:
    moment = usage_event_date(event)
    if moment is None or moment < since:
        return None
    model = _string(event.get("model")) or "unknown"
    stable_id = (
        _string(event.get("id")) or _string(event.get("eventId"))
        or _string(event.get("requestId"))
    )
    usage = event.get("tokenUsage")
    usage = usage if isinstance(usage, dict) else {}
    cents = _float(usage.get("totalCents"))
    stamp = _string(event.get("timestamp")) or moment.isoformat()
    entry_id = (
        f"cursor|api|{stable_id}" if stable_id else f"cursor|api|{stamp}|{model}|{row_index}"
    )
    return _local.make_entry(
        entry_id,
        moment,
        model,
        input_=_local.tokens(usage.get("inputTokens"), allow_str=True),
        output=_local.tokens(usage.get("outputTokens"), allow_str=True),
        cache_write=_local.tokens(usage.get("cacheWriteTokens"), allow_str=True),
        cache_read=_local.tokens(usage.get("cacheReadTokens"), allow_str=True),
        explicit_cost=None if cents is None else cents / 100,
    )


def has_next_page(pagination, total_count: int | None, page: int, event_count: int) -> bool:
    if isinstance(pagination, dict):
        explicit = pagination.get("hasNextPage")
        if isinstance(explicit, bool):
            return explicit
        pages = pagination.get("numPages")
        if isinstance(pages, int) and not isinstance(pages, bool):
            return page < pages
    if total_count is not None:
        return page * PAGE_SIZE < total_count
    # Missing pagination metadata — keep going while pages are full.
    return event_count >= PAGE_SIZE


def _millis(moment: datetime) -> str:
    return str(round(moment.timestamp() * 1000))


def fetch_filtered_events(
    token: str,
    since: datetime,
    fetcher: Fetcher,
    now: datetime | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> tuple[list[Entry] | None, str | None]:
    """Every usage event since `since`: `(entries, None)` on success,
    `(None, reason)` on any failure (no partial results)."""
    end = now or datetime.now(UTC)
    deadline = monotonic() + FETCH_DEADLINE
    page = 1
    row_index = 0
    collected: list[Entry] = []
    auth_index = 0
    while page <= MAX_PAGES:
        if monotonic() > deadline:
            return None, f"pagination deadline exceeded after page {page - 1}"
        body = json.dumps({
            "teamId": 0,
            "startDate": _millis(since),
            "endDate": _millis(end),
            "page": page,
            "pageSize": PAGE_SIZE,
        }).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0",
            "Origin": "https://cursor.com",
            "Referer": "https://cursor.com/dashboard/usage",
        }
        if _AUTH_MODES[auth_index] == "cookie":
            headers["Cookie"] = f"WorkosCursorSessionToken={workos_session_cookie(token)}"
        else:
            headers["Authorization"] = f"Bearer {token}"

        response = fetcher(API_URL, headers, body, REQUEST_TIMEOUT)
        if response is None:
            return None, f"transport error on page {page}"
        data, status = response
        if status in (401, 403):
            auth_index += 1
            if auth_index >= len(_AUTH_MODES):
                return None, f"auth rejected for all modes (last http {status})"
            continue
        if not 200 <= status <= 299:
            preview = data[:160].decode("utf-8", errors="replace").replace("\n", " ")
            return None, f"http {status} on page {page} ({len(data)} bytes) {preview}"
        obj = _local.loads(data)
        if not isinstance(obj, dict):
            return None, f"invalid JSON on page {page} ({len(data)} bytes)"
        events = next(
            (obj[k] for k in ("usageEventsDisplay", "usageEvents", "events")
             if isinstance(obj.get(k), list)),
            None,
        )
        if events is None:
            keys = ",".join(sorted(obj))
            return None, f"missing usageEvents/events on page {page} (keys: {keys})"
        for event in events:
            if isinstance(event, dict):
                entry = parse_usage_event(event, row_index, since)
                if entry is not None:
                    collected.append(entry)
            row_index += 1
        if not has_next_page(
            obj.get("pagination"), _optional_int(obj.get("totalUsageEventsCount")),
            page, len(events),
        ):
            return _local.dedup_keep_max(collected), None
        if not events:
            return None, f"pagination indicated next page but page {page} was empty"
        page += 1
    return None, f"pagination exceeded {MAX_PAGES} pages"


def urllib_fetcher(
    url: str, headers: dict[str, str], body: bytes, timeout: float
) -> tuple[bytes, int] | None:
    """The production fetcher: one POST, no cookies stored, no retries."""
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read(), response.status
    except urllib.error.HTTPError as exc:
        return exc.read(), exc.code
    except (urllib.error.URLError, OSError, ValueError):
        return None


# --- local bubbles -------------------------------------------------------------


def parse_bubble(obj, key: str) -> Entry | None:
    if not isinstance(obj, dict):
        return None
    counts = obj.get("tokenCount")
    if not isinstance(counts, dict):
        return None
    input_ = _local.tokens(counts.get("inputTokens"), allow_str=True)
    output = _local.tokens(counts.get("outputTokens"), allow_str=True)
    moment = _local.flexible_date(obj.get("createdAt"))
    if input_ + output <= 0 or moment is None:
        return None
    return _local.make_entry(
        f"cursor|{key}",
        moment,
        _local.text(obj.get("modelType")) or "unknown",
        input_=input_,
        output=output,
        explicit_cost=0.0,
    )


@dataclasses.dataclass
class _BubbleState:
    signature: tuple[float, int]
    high_water: int
    entries: dict[str, Entry]


def _read_bubbles(db_path: Path, previous: _BubbleState | None) -> _BubbleState | None:
    """All bubbles (first read) or only rows past the rowid high-water; None
    when the database couldn't be read this poll."""
    signature = _local.db_signature(db_path)
    if signature is None:
        return None
    if previous is not None and previous.signature == signature:
        return previous
    conn = _local.open_readonly(db_path)
    if conn is None:
        return None
    try:
        try:
            max_rowid = conn.execute("SELECT MAX(rowid) FROM cursorDiskKV").fetchone()[0] or 0
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return _BubbleState(signature, 0, {})
            return None
        incremental = previous is not None and 0 < previous.high_water <= max_rowid
        if incremental:
            rows = conn.execute(
                "SELECT rowid, key, value FROM cursorDiskKV NOT INDEXED"
                " WHERE rowid > ? AND key GLOB 'bubbleId:*'",
                (previous.high_water,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT rowid, key, value FROM cursorDiskKV WHERE key GLOB 'bubbleId:*'"
            ).fetchall()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    entries = dict(previous.entries) if incremental else {}
    for _rowid, key, value in rows:
        if not isinstance(key, str) or not isinstance(value, (str, bytes)):
            continue
        entry = parse_bubble(_local.loads(value), key)
        if entry is None:
            continue
        existing = entries.get(entry.id)
        if existing is None or entry.total > existing.total:
            entries[entry.id] = entry
    return _BubbleState(signature, max_rowid, entries)


# --- provider ------------------------------------------------------------------


@dataclasses.dataclass
class _ApiMemo:
    fetched_at: float
    account: str
    since: datetime
    entries: list[Entry]


class CursorProvider:
    """Cursor usage: dashboard API when signed in, local bubbles otherwise."""

    id = "cursor"
    # User-added scan folders (#177), set by the daemon from config.
    extra_roots: tuple = ()
    display_name = "Cursor"
    reports_cost = True
    PARSER_VERSION = PARSER_VERSION

    def __init__(
        self,
        cache: ScanCache | None = None,
        home: Path | None = None,
        fetcher: Fetcher | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        # The bubble scan keeps its own rowid-incremental state in memory
        # (see the module docstring); `cache` is accepted for a uniform
        # constructor but unused.
        self._cache = cache
        self._home = home
        self._fetcher = fetcher or urllib_fetcher
        self._clock = clock
        self._bubbles: dict[Path, _BubbleState] = {}
        self._memo: _ApiMemo | None = None
        self._last_attempt = -math.inf
        self._last_account: str | None = None
        self.last_error: str | None = None

    def _dashboard_entries(self, scan_roots: list[Path]) -> list[Entry] | None:
        """Authoritative API entries, or None to fall back to bubbles."""
        if os.environ.get("CURSOR_USAGE_API", "").strip() == "0":
            return None
        token = session_token(scan_roots)
        if token is None:
            return None
        account = account_identifier(token)
        now_ts = self._clock()
        now = datetime.fromtimestamp(now_ts, tz=UTC)
        since = _local.scan_window_start(now)
        memo = self._memo if self._memo and self._memo.account == account else None

        due = (
            now_ts - self._last_attempt >= REFRESH_SECONDS
            or account != self._last_account
            or (memo is not None and memo.since > since)
        )
        if due:
            self._last_attempt = now_ts
            self._last_account = account
            entries, reason = fetch_filtered_events(token, since, self._fetcher, now)
            if entries is not None:
                memo = self._memo = _ApiMemo(now_ts, account, since, entries)
                self.last_error = None
            else:
                self.last_error = reason

        if memo is None or now_ts - memo.fetched_at > STALE_MAX_AGE or memo.since > since:
            return None
        return [e for e in memo.entries if e.date >= since]

    def _bubble_entries(self, scan_roots: list[Path]) -> list[Entry]:
        live: dict[Path, _BubbleState] = {}
        for root in scan_roots:
            db_path = database_path(root)
            if db_path in live or not db_path.is_file():
                continue
            state = _read_bubbles(db_path, self._bubbles.get(db_path))
            if state is None:
                state = self._bubbles.get(db_path)  # unreadable: keep last good
            if state is not None:
                live[db_path] = state
        self._bubbles = live
        return _local.dedup_keep_max(e for s in live.values() for e in s.entries.values())

    def scan_entries(self) -> list[Entry]:
        scan_roots = with_extra_roots(roots(home=self._home), self.extra_roots)
        api = self._dashboard_entries(scan_roots)
        if api is not None:
            return api
        return self._bubble_entries(scan_roots)

    def fetch_daily(self, today: str | None = None) -> DailyUsage | None:
        return _local.daily(self.scan_entries(), today)

    def fetch_periods(self, today: str | None = None) -> dict:
        return _local.periods(self.scan_entries(), today)

    def fetch_enrichment(self) -> ProviderEnrichment:
        # Blocks/burn-rate remain unported for every provider; the *_ok flags
        # stay false so callers keep their previous values.
        return ProviderEnrichment()
