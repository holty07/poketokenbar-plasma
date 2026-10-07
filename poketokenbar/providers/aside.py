"""Aside usage — ports upstream's LocalAsideUsageReader (PokeTokenBar #283).

Aside keeps one SQLite database per user profile at `~/.aside/u/<user>/state.db`
(a home dot-dir, so the macOS path carries over to Linux unchanged). Only
usage metadata is selected — never conversation bodies or credentials:

    session_turns.id                         -- AUTOINCREMENT
    session_turns.token_usage                -- JSON: {"input", "output",
                                             --   "cacheRead", "cacheWrite",
                                             --   "totalTokens", "cost": {"total"}}
    session_turns.finished_at, .last_message_timestamp   -- epoch seconds
    sessions.model                           -- JSON: {"modelId": ...}

A turn runs for a long time and its `token_usage` grows in place, so it is
dated by its last activity (`finished_at`, else `last_message_timestamp`) —
tokens land on the day they were generated. Zero-token turns (aborted before
any response) are not usage. A turn with only `totalTokens` keeps that
aggregate as input without inventing a cache split.

`sessions.model` is the session's *current* model, not a per-turn record, so
historical turns are never priced from it: the cost is Aside's own
`cost.total` when recorded, else $0 (upstream marks it "unavailable"; this
port has no such state).

Deleting an Aside session cascades to its turns, and turn aggregates are
rewritten in place. Like upstream, each scan is merged keep-max with every
entry already seen this process (cleared at a month change), so a deleted
session's already-counted tokens don't vanish from today's total. Entry ids
carry the file's inode: a recreated state.db restarts AUTOINCREMENT at 1, and
its new turns must not hide behind the cached pre-reset ids.

A database that can't be opened or queried (an unmigrated profile, a foreign
file, a busy WAL recovery) is skipped without blanking the healthy ones, and
is not cached, so the next poll retries it.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from ..cache import ScanCache
from ..models import DailyUsage, Entry, ProviderEnrichment
from . import _local

PARSER_VERSION = 1

_BUCKETS = ("input", "output", "cacheRead", "cacheWrite")
_QUERY = """
    SELECT t.id, t.token_usage, COALESCE(t.finished_at, t.last_message_timestamp), s.model
      FROM session_turns t LEFT JOIN sessions s ON s.id = t.session_id
"""


def roots(home: Path | None = None) -> list[Path]:
    return [_local.home_dir(home) / ".aside" / "u"]


def databases(scan_roots: list[Path]) -> list[Path]:
    """`state.db` directly in a root or in any of its child folders."""
    found: set[Path] = set()
    for root in scan_roots:
        try:
            children = [p for p in root.iterdir() if p.is_dir()] if root.is_dir() else []
        except OSError:
            children = []
        for directory in [root, *children]:
            db = directory / "state.db"
            if db.is_file():
                found.add(Path(os.path.realpath(db)))
    return sorted(found)


def _model(raw) -> str:
    metadata = _local.loads(raw) if isinstance(raw, (str, bytes)) else None
    if isinstance(metadata, dict):
        return _local.text(metadata.get("modelId")) or "aside"
    return "aside"


def parse_row(store: str, row: tuple) -> Entry | None:
    turn_id, raw_usage, activity, raw_model = row
    usage = _local.loads(raw_usage) if isinstance(raw_usage, (str, bytes)) else None
    if not isinstance(usage, dict):
        return None
    moment = _local.epoch_date(_local.number(activity))
    if moment is None:
        return None
    has_buckets = any(_local.number(usage.get(name)) is not None for name in _BUCKETS)
    cost = usage.get("cost")
    reported = _local.number(cost.get("total")) if isinstance(cost, dict) else None
    return _local.make_entry(
        f"{store}:{turn_id}",
        moment,
        _model(raw_model),
        input_=_local.tokens(usage.get("input") if has_buckets else usage.get("totalTokens")),
        output=_local.tokens(usage.get("output")),
        cache_write=_local.tokens(usage.get("cacheWrite")),
        cache_read=_local.tokens(usage.get("cacheRead")),
        explicit_cost=reported if reported is not None and reported >= 0 else 0.0,
    )


def parse_database(db_path: Path) -> list[Entry] | None:
    """Every turn in one state.db, or None when it can't be read — a scan
    that fails part-way discards that database's partial rows."""
    try:
        inode = db_path.stat().st_ino
    except OSError:
        return None
    conn = _local.open_readonly(db_path)
    if conn is None:
        return None
    store = f"aside|{db_path}#{inode}"
    try:
        rows = conn.execute(_QUERY).fetchall()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    entries = []
    for row in rows:
        entry = parse_row(store, row)
        if entry is not None:
            entries.append(entry)
    return entries


class AsideProvider:
    """Aside local usage."""

    id = "aside"
    display_name = "Aside"
    reports_cost = True
    PARSER_VERSION = PARSER_VERSION

    def __init__(self, cache: ScanCache | None = None, home: Path | None = None) -> None:
        self._cache = cache
        self._home = home
        self._seen = _local.SeenEntries()

    def scan_entries(self) -> list[Entry]:
        scanned = _local.scan_cached(
            self._cache,
            self.id,
            self.PARSER_VERSION,
            databases(roots(home=self._home)),
            parse_database,
            signature=_local.db_signature,
        )
        return self._seen.merge(scanned)

    def fetch_daily(self, today: str | None = None) -> DailyUsage | None:
        return _local.daily(self.scan_entries(), today)

    def fetch_periods(self, today: str | None = None) -> dict:
        return _local.periods(self.scan_entries(), today)

    def fetch_enrichment(self) -> ProviderEnrichment:
        # Blocks/burn-rate remain unported for every provider; the *_ok flags
        # stay false so callers keep their previous values.
        return ProviderEnrichment()
