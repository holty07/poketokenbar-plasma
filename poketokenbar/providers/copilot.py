"""Copilot CLI usage — ports upstream's Copilot reader (PokeTokenBar).

Copilot CLI keeps one SQLite store at `~/.copilot/session-store.db`
(honours `$COPILOT_HOME`, which may name several roots). The table
`assistant_usage_events` has one row per API call:

    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model TEXT NOT NULL,
    input_tokens INTEGER,      -- the whole prompt, cache reads/writes included
    output_tokens INTEGER,
    cache_read_tokens INTEGER,
    cache_write_tokens INTEGER,
    reasoning_tokens INTEGER,  -- a breakdown of output, not an extra charge
    created_at TEXT DEFAULT (datetime('now'))   -- ISO-8601 with Z or "YYYY-MM-DD HH:MM:SS"

`input_tokens` is the full prompt with cached reads/writes folded in, so the
uncached prompt is `input_tokens − cache_read_tokens − cache_write_tokens`.
Adding the columns as-is would triple-count every cached prompt — that is the
accounting fault this parser exists to avoid.

`total = input + cache_read + cache_write + output`, i.e. prompt plus
completion counted once. Copilot is premium-request billed and no cost is
estimated (`reports_cost = False`).

Entry ids are `copilot|<db_path>|<row_id>` so two stores (both numbered from 1)
cannot collide on row id. The database is opened read-only and re-parsed when
its `(mtime, size)` changes, same as the opencode and Hermes providers.
"""

from __future__ import annotations

import os
import sqlite3
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from ..cache import ScanCache
from ..models import DailyUsage, Entry, ProviderEnrichment
from . import _local

MAX_TOKENS = 10**12


def default_roots(home: Path | None = None) -> list[Path]:
    """`$COPILOT_HOME` (comma-separated) or `~/.copilot`."""
    raw = os.environ.get("COPILOT_HOME", "").strip()
    if raw:
        return [Path(p.strip()) for p in raw.split(",") if p.strip()]
    base = home or Path.home()
    return [base / ".copilot"]


def database_for_root(root: Path) -> Path | None:
    """A `session-store.db` directly under root, or root itself when it is a db
    path. Returns the candidate path; existence is checked at open time."""
    if root.name == "session-store.db":
        return root
    if root.is_dir():
        return root / "session-store.db"
    return None


def _signature(db_path: Path) -> tuple[float, int] | None:
    newest: float | None = None
    size = 0
    found = False
    for path in (db_path, Path(str(db_path) + "-wal")):
        try:
            stat = path.stat()
        except OSError:
            continue
        found = True
        newest = stat.st_mtime if newest is None else max(newest, stat.st_mtime)
        size += stat.st_size
    return (newest, size) if found else None


def _open_readonly(db_path: Path) -> sqlite3.Connection | None:
    if not db_path.exists():
        return None
    escaped = urllib.parse.quote(str(db_path), safe="/:")
    for params in ("mode=ro", "immutable=1"):
        conn = None
        try:
            conn = sqlite3.connect(f"file:{escaped}?{params}", uri=True, timeout=1.0)
            conn.execute("SELECT count(*) FROM sqlite_master")
            return conn
        except sqlite3.Error:
            if conn is not None:
                conn.close()
    return None


def _count(value) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value) if 0 <= value <= MAX_TOKENS else 0


def _parse_row(db_path: Path, row: tuple) -> Entry | None:
    (
        row_id, model,
        input_tokens, output_tokens,
        cache_read_tokens, cache_write_tokens,
        reasoning_tokens, created_at,
    ) = row

    input_tokens = _count(input_tokens)
    output_tokens = _count(output_tokens)
    cache_read = _count(cache_read_tokens)
    cache_write = _count(cache_write_tokens)
    reasoning = _count(reasoning_tokens)

    # input_tokens is the whole prompt including cache; subtract cached reads
    # and writes so the uncached prompt is counted once.
    input_ = max(input_tokens - cache_read - cache_write, 0)
    # output already includes reasoning; do not add it again.
    output = output_tokens
    total = input_ + cache_read + cache_write + output
    if total <= 0:
        return None

    moment = _parse_created_at(created_at)
    if moment is None:
        return None

    return Entry(
        id=f"copilot|{db_path}|{row_id}",
        date=moment,
        local_day=moment.astimezone().strftime("%Y-%m-%d"),
        model=model or "unknown",
        input=input_,
        output=output,
        cache_write=cache_write,
        cache_read=cache_read,
    )


def _parse_created_at(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped:
        return None
    try:
        parsed = datetime.fromisoformat(stripped.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _parse_database(db_path: Path) -> list[Entry] | None:
    """Every entry in the database, or None when it could not be read this poll."""
    conn = _open_readonly(db_path)
    if conn is None:
        return None
    try:
        try:
            rows = conn.execute(
                """SELECT id, model, input_tokens, output_tokens,
                          cache_read_tokens, cache_write_tokens,
                          reasoning_tokens, created_at
                     FROM assistant_usage_events"""
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return []
            return None
    except sqlite3.Error:
        return None
    finally:
        conn.close()

    entries: list[Entry] = []
    for row in rows:
        entry = _parse_row(db_path, row)
        if entry is not None:
            entries.append(entry)
    return entries


class CopilotProvider:
    """Copilot CLI local usage."""

    id = "copilot"
    display_name = "Copilot CLI"
    reports_cost = False
    PARSER_VERSION = 1

    # User-added scan folders (#177), set by the daemon from config.
    extra_roots: tuple = ()

    def __init__(self, cache: ScanCache | None = None, home: Path | None = None) -> None:
        self._cache = cache
        self._home = home

    def _databases(self) -> list[Path]:
        roots = list(default_roots(home=self._home))
        # Extra scan folders can point at a directory or a database file.
        for extra in self.extra_roots:
            p = Path(extra)
            if p.exists():
                roots.append(p)
        seen: set[str] = set()
        dbs: list[Path] = []
        for root in roots:
            db = database_for_root(root)
            if db is None or str(db) in seen:
                continue
            seen.add(str(db))
            dbs.append(db)
        return dbs

    def scan_entries(self) -> list[Entry]:
        """Every entry across all Copilot stores, cached by (mtime, size)."""
        entries: list[Entry] = []
        live: set[str] = set()
        for db in self._databases():
            sig = _signature(db)
            if sig is None:
                continue
            mtime, size = sig
            live.add(str(db))
            cached = None
            if self._cache is not None:
                cached = self._cache.get(self.id, db, mtime, size, self.PARSER_VERSION)
            if cached is None:
                parsed = _parse_database(db)
                # Unreadable this poll (BUSY, damaged) means `parsed` is None;
                # skip without caching so the next poll retries under a
                # signature that hasn't been spent on a wrong answer.
                if parsed is not None:
                    entries = parsed
                    if self._cache is not None:
                        self._cache.put(
                            self.id, db, mtime, size, self.PARSER_VERSION, entries
                        )
            else:
                entries = cached
        if self._cache is not None:
            self._cache.prune(self.id, live)
        return entries

    def fetch_daily(self, today: str | None = None) -> DailyUsage | None:
        return _local.daily(self.scan_entries(), today)

    def fetch_periods(self, today: str | None = None) -> dict:
        return _local.periods(self.scan_entries(), today)

    def fetch_enrichment(self) -> ProviderEnrichment:
        return ProviderEnrichment()
