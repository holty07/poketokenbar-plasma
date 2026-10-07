"""Hermes Agent usage — reads the local session database Hermes itself writes.

Hermes keeps one SQLite database, `state.db`, at its home directory (which
respects `$HERMES_HOME` and `$HERMES_DATA_DIR_SUFFIX`, mirroring Hermes's own
`get_hermes_home()`) — except that newer Hermes builds that use named
profiles keep it at `<home>/profiles/<name>/state.db` instead, one per
profile. Either way, each session row already carries that session's own
cumulative token counters and Hermes-computed dollar cost:

    sessions.input_tokens / output_tokens / cache_read_tokens /
             cache_write_tokens / reasoning_tokens
    sessions.estimated_cost_usd, sessions.actual_cost_usd
    sessions.started_at, sessions.last_activity_at   -- unix epoch seconds
    sessions.parent_session_id                        -- set for compression/
                                                        -- branch children

One Entry per session, not per-call: `messages.token_count` (the only
per-turn field) is not populated in practice, so there is no reliable
finer-grained timeline to split a session's tokens across days. A session
that happens to straddle midnight is attributed whole to the day of its last
activity — the same precision tradeoff opencode's provider accepts for
multi-day conversations.

`parent_session_id IS NULL` mirrors Hermes's own `usage_totals()` query:
compression/branch children get their own row but are not top-level spend in
Hermes's own accounting, so counting them too would double the total.

Model ids are used bare, with no provider prefix. Hermes routes a single
session through many different billing backends (a user's own Anthropic key,
OpenRouter, Nous's free-tier models, ...); a name that matches pricing.TABLE
(e.g. a session routed straight to "claude-sonnet-4-6") prices the same as
that provider's own sessions, while the many gateway-specific names (e.g.
"upstage/solar-mini4:free") correctly fall through to zero.
"""

from __future__ import annotations

import os
import sqlite3
import urllib.parse
from datetime import date as _date
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import pricing
from ..cache import ScanCache
from ..models import DailyUsage, Entry, ProviderEnrichment

PARSER_VERSION = 1


def default_home(home: Path | None = None) -> Path:
    """`$HERMES_HOME`, else `~/.hermes<$HERMES_DATA_DIR_SUFFIX>` — Hermes's
    own default, ported from hermes_constants.get_hermes_home().

    ``home`` substitutes for the user's own home directory (this is how
    tests, and the daemon's own isolation, construct a Hermes home without
    touching the real one); it plays no part when `$HERMES_HOME` is set,
    since that is an absolute override regardless of whose home it is.
    """
    override = os.environ.get("HERMES_HOME", "").strip()
    if override:
        return Path(os.path.expanduser(os.path.expandvars(override)))
    suffix = os.environ.get("HERMES_DATA_DIR_SUFFIX", "")
    base = home or Path.home()
    return base / (".hermes" + suffix)


def default_db(home: Path | None = None) -> Path | None:
    """`state.db` under this Hermes home.

    Newer Hermes builds keep per-profile state at `profiles/<name>/state.db`
    instead of (or alongside) one top-level database — e.g. an "overseer"
    profile used for an always-on agent. Of every database that actually
    exists, the most recently written one is the one accumulating today's
    usage; an older file left behind by a since-completed profile migration
    must not shadow it.
    """
    root = default_home(home=home)
    candidates: list[Path] = []
    direct = root / "state.db"
    if direct.is_file():
        candidates.append(direct)
    try:
        for child in (root / "profiles").iterdir():
            db = child / "state.db"
            if child.is_dir() and db.is_file():
                candidates.append(db)
    except OSError:
        pass
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _signature(db_path: Path) -> tuple[float, int] | None:
    """`(mtime, size)` across the database and its `-wal` sibling — a WAL
    commit lands in the sibling and leaves the main file's stat alone."""
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
            conn = sqlite3.connect(f"file:{escaped}?{params}", uri=True)
            conn.execute("SELECT count(*) FROM sqlite_master")
            return conn
        except sqlite3.Error:
            if conn is not None:
                conn.close()
    return None


MAX_TOKENS = 10**12


def _count(value) -> int:
    """A column's token count, or 0 when it isn't a sane one (NULL, a
    non-integer, negative, or absurd) — a corrupt row must not wreck the
    day's totals (upstream #307)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    # SQLite can hand back a REAL; NaN fails both comparisons below.
    return int(value) if 0 <= value <= MAX_TOKENS else 0


def _parse_row(row: tuple) -> Entry | None:
    (
        session_id, started_at, last_activity_at, model,
        input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
        reasoning_tokens,
    ) = row

    epoch = last_activity_at if last_activity_at is not None else started_at
    if epoch is None:
        return None
    date = datetime.fromtimestamp(epoch, tz=timezone.utc)

    input_tokens = _count(input_tokens)
    # reasoning has no dedicated Entry field — folded into output, the same
    # choice opencode's provider makes for its own reasoning counter.
    output_tokens = _count(output_tokens) + _count(reasoning_tokens)
    cache_write = _count(cache_write_tokens)
    cache_read = _count(cache_read_tokens)
    if input_tokens + output_tokens + cache_write + cache_read <= 0:
        return None

    return Entry(
        id=f"hermes|{session_id}",
        date=date,
        local_day=date.astimezone().strftime("%Y-%m-%d"),
        model=model or "unknown",
        input=input_tokens,
        output=output_tokens,
        cache_write=cache_write,
        cache_read=cache_read,
    )


def _parse_database(db_path: Path) -> list[Entry] | None:
    """Every top-level session's usage, or None when it could not be read
    this poll (BUSY, damaged) — the caller must not cache that as "no usage"."""
    conn = _open_readonly(db_path)
    if conn is None:
        return None
    try:
        rows = conn.execute(
            """SELECT id, started_at, last_activity_at, model,
                      input_tokens, output_tokens, cache_read_tokens,
                      cache_write_tokens, reasoning_tokens
                 FROM sessions
                WHERE parent_session_id IS NULL"""
        ).fetchall()
    except sqlite3.OperationalError as exc:
        # "no such table" is a permanent property of the file (it isn't a
        # Hermes state database) — anything else is this moment failing to
        # read a store that may well be one.
        if "no such table" in str(exc):
            return []
        return None
    except sqlite3.Error:
        return None
    finally:
        conn.close()

    entries: list[Entry] = []
    for row in rows:
        entry = _parse_row(row)
        if entry is not None:
            entries.append(entry)
    return entries


class HermesProvider:
    """Hermes Agent local usage."""

    id = "hermes"
    display_name = "Hermes Agent"
    reports_cost = True
    PARSER_VERSION = PARSER_VERSION

    def __init__(self, cache: ScanCache | None = None, home: Path | None = None) -> None:
        self._cache = cache
        self._home = home

    def scan_entries(self) -> list[Entry]:
        db_path = default_db(home=self._home)
        sig = _signature(db_path) if db_path is not None else None
        live: set[str] = set()
        entries: list[Entry] = []
        if sig is not None:
            mtime, size = sig
            live.add(str(db_path))
            cached = None
            if self._cache is not None:
                cached = self._cache.get(self.id, db_path, mtime, size, self.PARSER_VERSION)
            if cached is None:
                parsed = _parse_database(db_path)
                # Unreadable this poll (BUSY, damaged) means `parsed` is None;
                # skip without caching so the next poll retries under a
                # signature that hasn't been spent on a wrong answer.
                if parsed is not None:
                    entries = parsed
                    if self._cache is not None:
                        self._cache.put(self.id, db_path, mtime, size, self.PARSER_VERSION, entries)
            else:
                entries = cached
        if self._cache is not None:
            self._cache.prune(self.id, live)
        return entries

    def fetch_daily(self, today: str | None = None) -> DailyUsage | None:
        day = today or _date.today().strftime("%Y-%m-%d")
        entries = [e for e in self.scan_entries() if e.local_day == day]
        if not entries:
            return None
        daily = DailyUsage(date=day)
        for e in entries:
            daily.input_tokens += e.input
            daily.output_tokens += e.output
            daily.cache_creation_tokens += e.cache_write
            daily.cache_read_tokens += e.cache_read
            daily.total_cost += pricing.cost(e.model, e.input, e.output, e.cache_write, e.cache_read)
        daily.total_tokens = (
            daily.input_tokens
            + daily.output_tokens
            + daily.cache_creation_tokens
            + daily.cache_read_tokens
        )
        return daily

    def fetch_periods(self, today: str | None = None) -> dict:
        """Week-to-date and month-to-date totals. Week starts Monday."""
        day = today or _date.today().strftime("%Y-%m-%d")
        anchor = datetime.strptime(day, "%Y-%m-%d").date()
        week_start = anchor - timedelta(days=anchor.weekday())
        month_prefix = day[:7]

        week = {"tokens": 0, "cost": 0.0}
        month = {"tokens": 0, "cost": 0.0}
        for e in self.scan_entries():
            cost = pricing.cost(e.model, e.input, e.output, e.cache_write, e.cache_read)
            if e.local_day[:7] == month_prefix:
                month["tokens"] += e.total
                month["cost"] += cost
            try:
                entry_day = datetime.strptime(e.local_day, "%Y-%m-%d").date()
            except ValueError:
                continue
            if week_start <= entry_day <= anchor:
                week["tokens"] += e.total
                week["cost"] += cost
        return {"week": week, "month": month}

    def fetch_enrichment(self) -> ProviderEnrichment:
        # Blocks/burn-rate remain unported for every provider, not just this
        # one; the *_ok flags stay false so callers keep their previous
        # values rather than zeroing them.
        return ProviderEnrichment()
