"""opencode usage — reads the local session database opencode itself writes.

opencode keeps one SQLite database at `~/.local/share/opencode/opencode.db`
(respecting `$XDG_DATA_HOME`), with one row per turn in `message`. Each
assistant row's `data` column is a JSON blob carrying that turn's own token
breakdown and dollar cost already computed by opencode:

    {"role": "assistant", "cost": 0.0138, "modelID": "claude-sonnet-4-6",
     "providerID": "anthropic", "time": {"created": 1730000000000, ...},
     "tokens": {"input": N, "output": N, "reasoning": N,
                "cache": {"write": N, "read": N}}}

`reasoning` tokens are counted into output — opencode bills them there.
`message.time_created` mirrors `data.time.created` exactly (same epoch
milliseconds), so the column is used directly rather than re-parsing the JSON.

Model ids are used bare, with no provider prefix. opencode's own bundled
models (e.g. "muse-spark-1.2-contributor-free") don't collide with any entry
in pricing.TABLE and correctly price at zero — which is right, they're free —
while a session routed through a real upstream model (e.g.
"claude-sonnet-4-6") prices exactly as that provider's own sessions do.
"""

from __future__ import annotations

import json
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


def _int(value) -> int:
    return value if isinstance(value, int) else 0


def default_db(home: Path | None = None) -> Path | None:
    home = home or Path.home()
    data_home = os.environ.get("XDG_DATA_HOME")
    base = Path(data_home) if data_home else home / ".local" / "share"
    db = base / "opencode" / "opencode.db"
    return db if db.is_file() else None


def _signature(db_path: Path) -> tuple[float, int] | None:
    """`(mtime, size)` across the database and its `-wal` sibling, matching
    the antigravity provider's reasoning: a WAL commit lands in the sibling
    and leaves the main file's stat alone."""
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


def _parse_message(msg_id: str, time_created, data: str) -> Entry | None:
    try:
        obj = json.loads(data)
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict) or obj.get("role") != "assistant":
        return None
    tokens = obj.get("tokens")
    if not isinstance(tokens, dict):
        return None
    millis = time_created if isinstance(time_created, int) else None
    if millis is None:
        return None
    date = datetime.fromtimestamp(millis / 1000, tz=timezone.utc)

    cache = tokens.get("cache")
    cache = cache if isinstance(cache, dict) else {}
    input_tokens = _int(tokens.get("input"))
    output_tokens = _int(tokens.get("output")) + _int(tokens.get("reasoning"))
    cache_write = _int(cache.get("write"))
    cache_read = _int(cache.get("read"))
    if input_tokens + output_tokens + cache_write + cache_read <= 0:
        return None

    return Entry(
        id=f"opencode|{msg_id}",
        date=date,
        local_day=date.astimezone().strftime("%Y-%m-%d"),
        model=obj.get("modelID") or "unknown",
        input=input_tokens,
        output=output_tokens,
        cache_write=cache_write,
        cache_read=cache_read,
    )


def _parse_database(db_path: Path) -> list[Entry] | None:
    """Every entry in the database, or None when it could not be read this
    poll (BUSY, damaged) — the caller must not cache that as "no usage"."""
    conn = _open_readonly(db_path)
    if conn is None:
        return None
    try:
        rows = conn.execute(
            "SELECT id, time_created, data FROM message WHERE data IS NOT NULL"
        ).fetchall()
    except sqlite3.OperationalError as exc:
        # "no such table" is a permanent property of the file (it isn't an
        # opencode database) — anything else is this moment failing to read
        # a store that may well be one.
        if "no such table" in str(exc):
            return []
        return None
    except sqlite3.Error:
        return None
    finally:
        conn.close()

    entries: list[Entry] = []
    for msg_id, time_created, data in rows:
        entry = _parse_message(msg_id, time_created, data)
        if entry is not None:
            entries.append(entry)
    return entries


class OpencodeProvider:
    """opencode local usage."""

    id = "opencode"
    display_name = "opencode"
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
