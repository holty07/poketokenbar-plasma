"""Shared plumbing for the local-log providers ported from upstream's
LocalUsageReader / LocalAdditionalUsageProvider (Kimi Code, Pi, omp, Aside,
Kiro, Cursor).

Nothing here knows about any one tool. It holds the pieces those six modules
would otherwise each copy: defensive number parsing, the per-file scan-cache
loop, keep-max dedup, and the daily / week / month folds. The older provider
modules (opencode, hermes, ...) predate it and keep their own copies.

Token clamp (upstream #307): every token field read from an external log goes
through `tokens()`. Upstream saturated absurd values at a ceiling so Swift's
`Int` add could not trap; Python cannot overflow, but a corrupt row (`1e30`, a
negative count, a hand edit) would still wreck every total it touches. So a
value that is negative, non-finite, or above `MAX_TOKENS` (10**12 — some
thousand times a heavy user's whole month) is treated as unreadable and
counts as 0 for that field, rather than being saturated into a trillion-token
day.
"""

from __future__ import annotations

import dataclasses
import json
import math
import os
import sqlite3
import urllib.parse
from collections.abc import Callable, Iterable, Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from .. import pricing
from ..cache import ScanCache
from ..models import DailyUsage, Entry

MAX_TOKENS = 10**12


# --- number / date parsing -------------------------------------------------


def number(value) -> float | None:
    """A JSON number as a finite float; None for absent, null, bool, text."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def tokens(value, *, allow_str: bool = False) -> int:
    """A token count, clamped: 0 for anything absent, negative, non-finite or
    absurd (> MAX_TOKENS). `allow_str` also accepts "1,234"-style digits."""
    raw = number(value)
    if raw is None and allow_str and isinstance(value, str):
        try:
            raw = float(int(value.strip().replace(",", "")))
        except ValueError:
            raw = None
    if raw is None or raw <= 0 or raw > MAX_TOKENS:
        return 0
    return int(raw)


def tokens_or_none(value) -> int | None:
    """`tokens()` when the field holds a number at all, else None — lets a
    parser tell "present but zero/garbage" from "missing"."""
    return None if number(value) is None else tokens(value)


def text(value) -> str | None:
    """A trimmed, non-empty string, else None."""
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def from_epoch(seconds: float) -> datetime | None:
    try:
        return datetime.fromtimestamp(seconds, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def parse_iso(raw) -> datetime | None:
    """ISO-8601 with optional fractional seconds and `Z`; naive means UTC."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw.strip())
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def epoch_date(raw: float | None) -> datetime | None:
    """Epoch seconds or milliseconds (>= 1e11 reads as millis)."""
    if raw is None or not math.isfinite(raw) or raw <= 0:
        return None
    return from_epoch(raw / 1000 if raw >= 100_000_000_000 else raw)


def flexible_date(value) -> datetime | None:
    """ISO-8601 text, or an epoch number (seconds or millis), or digits."""
    if isinstance(value, str):
        stripped = value.strip()
        try:
            return epoch_date(float(stripped))
        except ValueError:
            return parse_iso(stripped)
    return epoch_date(number(value))


def local_day(moment: datetime) -> str:
    return moment.astimezone().strftime("%Y-%m-%d")


def loads(line: str | bytes):
    try:
        return json.loads(line)
    except (ValueError, TypeError):
        return None


def read_text(path: Path) -> str | None:
    """Whole file as UTF-8, or None when it can't be read or decoded — the
    caller must not cache that as "no usage"."""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


# --- entries ---------------------------------------------------------------


def make_entry(
    entry_id: str,
    moment: datetime,
    model: str,
    input_: int = 0,
    output: int = 0,
    cache_write: int = 0,
    cache_read: int = 0,
    explicit_cost: float | None = None,
) -> Entry | None:
    """An Entry, or None when it carries no tokens at all."""
    if input_ + output + cache_write + cache_read <= 0:
        return None
    if explicit_cost is not None and (not math.isfinite(explicit_cost) or explicit_cost < 0):
        explicit_cost = None
    return Entry(
        id=entry_id,
        date=moment,
        local_day=local_day(moment),
        model=model,
        input=input_,
        output=output,
        cache_write=cache_write,
        cache_read=cache_read,
        explicit_cost=explicit_cost,
    )


def dedup_keep_max(entries: Iterable[Entry]) -> list[Entry]:
    """One entry per id: the largest total (the completed one), dated at the
    earliest sighting — a fork can rewrite the same turn later. Never mutates
    its inputs; they may be shared with the scan cache."""
    by_id: dict[str, Entry] = {}
    for e in entries:
        existing = by_id.get(e.id)
        if existing is None:
            by_id[e.id] = e
            continue
        kept = e if e.total > existing.total else existing
        earliest = e if e.date < existing.date else existing
        if kept.date != earliest.date:
            kept = dataclasses.replace(kept, date=earliest.date, local_day=earliest.local_day)
        by_id[e.id] = kept
    return list(by_id.values())


class SeenEntries:
    """Process-lifetime keep-max memory for sources that *delete* history
    (Kiro's `/clear`, Aside's cascading session delete): a turn already counted
    stays counted instead of silently dropping out of today's total. Cleared on
    a month change, mirroring upstream's month-keyed cache."""

    def __init__(self) -> None:
        self._month = ""
        self._entries: dict[str, Entry] = {}

    def merge(self, scanned: Iterable[Entry], month: str | None = None) -> list[Entry]:
        month = month or datetime.now().astimezone().strftime("%Y-%m")
        if month != self._month:
            self._month = month
            self._entries = {}
        merged = dedup_keep_max([*self._entries.values(), *scanned])
        self._entries = {e.id: e for e in merged}
        return merged


# --- paths -----------------------------------------------------------------


def home_dir(home: Path | None) -> Path:
    return home or Path.home()


def expand(raw: str) -> Path:
    return Path(os.path.expanduser(os.path.expandvars(raw.strip())))


def env_path(key: str) -> Path | None:
    raw = os.environ.get(key, "")
    return expand(raw) if raw.strip() else None


def env_paths(key: str) -> list[Path] | None:
    """A comma-separated path list from the environment; None when unset."""
    raw = os.environ.get(key)
    if raw is None:
        return None
    paths = [expand(part) for part in raw.split(",") if part.strip()]
    return paths or None


def xdg_data_home(home: Path | None) -> Path:
    override = os.environ.get("XDG_DATA_HOME", "").strip()
    return Path(override) if override else home_dir(home) / ".local" / "share"


def xdg_config_home(home: Path | None) -> Path:
    override = os.environ.get("XDG_CONFIG_HOME", "").strip()
    return Path(override) if override else home_dir(home) / ".config"


def normalized_roots(roots: Iterable[Path]) -> list[Path]:
    """Order-preserving, duplicate-free, and with any root nested inside
    another dropped — a nested root would scan the same files twice."""
    unique: list[Path] = []
    for root in roots:
        resolved = Path(os.path.abspath(root))
        if resolved not in unique:
            unique.append(resolved)
    return [
        r for r in unique
        if not any(other != r and r.is_relative_to(other) for other in unique)
    ]


def walk_files(root: Path, accept: Callable[[Path], bool]) -> Iterator[Path]:
    """Regular files under `root` (recursive, hidden entries skipped, like
    Foundation's `.skipsHiddenFiles`) that `accept` keeps. A file root is
    yielded as itself."""
    if root.is_file():
        if accept(root):
            yield root
        return
    if not root.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            path = Path(dirpath) / name
            if accept(path) and path.is_file():
                yield path


# --- SQLite / signatures ---------------------------------------------------


def file_signature(path: Path) -> tuple[float, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_mtime, stat.st_size)


def db_signature(path: Path) -> tuple[float, int] | None:
    """`(mtime, size)` across a database and its `-wal` sibling — a WAL commit
    lands in the sibling and leaves the main file's stat alone. `-shm` churns
    on every read and is deliberately ignored."""
    newest: float | None = None
    size = 0
    for candidate in (path, Path(str(path) + "-wal")):
        sig = file_signature(candidate)
        if sig is None:
            continue
        newest = sig[0] if newest is None else max(newest, sig[0])
        size += sig[1]
    return None if newest is None else (newest, size)


def open_readonly(db_path: Path) -> sqlite3.Connection | None:
    if not db_path.is_file():
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


# --- scanning --------------------------------------------------------------


def scan_cached(
    cache: ScanCache | None,
    provider_id: str,
    parser_version: int,
    paths: Iterable[Path],
    parse: Callable[[Path], list[Entry] | None],
    signature: Callable[[Path], tuple[float, int] | None] = file_signature,
) -> list[Entry]:
    """Every path's entries, re-parsing only files whose `(mtime, size)`
    changed. `parse` returns None for "unreadable this poll" (BUSY, torn
    write, bad encoding) — skipped and *not* cached, so the next poll retries
    under a signature that hasn't been spent on a wrong answer."""
    live: set[str] = set()
    out: list[Entry] = []
    for path in paths:
        sig = signature(path)
        if sig is None:
            continue
        mtime, size = sig
        live.add(str(path))
        cached = None
        if cache is not None:
            cached = cache.get(provider_id, path, mtime, size, parser_version)
        if cached is None:
            cached = parse(path)
            if cached is None:
                continue
            if cache is not None:
                cache.put(provider_id, path, mtime, size, parser_version, cached)
        out.extend(cached)
    if cache is not None:
        cache.prune(provider_id, live)
    return out


# --- aggregation -----------------------------------------------------------


def today_key() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d")


def daily(entries: Iterable[Entry], today: str | None = None) -> DailyUsage | None:
    """Today's totals, or None when nothing landed today."""
    day = today or today_key()
    todays = [e for e in entries if e.local_day == day]
    if not todays:
        return None
    result = DailyUsage(date=day)
    for e in todays:
        result.input_tokens += e.input
        result.output_tokens += e.output
        result.cache_creation_tokens += e.cache_write
        result.cache_read_tokens += e.cache_read
        result.total_cost += pricing.entry_cost(e)
    result.total_tokens = (
        result.input_tokens
        + result.output_tokens
        + result.cache_creation_tokens
        + result.cache_read_tokens
    )
    return result


def periods(entries: Iterable[Entry], today: str | None = None) -> dict:
    """Week-to-date and month-to-date totals. Week starts Monday."""
    day = today or today_key()
    anchor = date.fromisoformat(day)
    week_start = anchor - timedelta(days=anchor.weekday())
    month_prefix = day[:7]

    week = {"tokens": 0, "cost": 0.0}
    month = {"tokens": 0, "cost": 0.0}
    for e in entries:
        cost = pricing.entry_cost(e)
        if e.local_day[:7] == month_prefix:
            month["tokens"] += e.total
            month["cost"] += cost
        try:
            entry_day = date.fromisoformat(e.local_day)
        except ValueError:
            continue
        if week_start <= entry_day <= anchor:
            week["tokens"] += e.total
            week["cost"] += cost
    return {"week": week, "month": month}


def scan_window_start(now: datetime | None = None) -> datetime:
    """Earliest instant any period view needs: the start of this month, this
    (Monday) week, or seven days back, whichever is earliest — upstream's
    `enrichmentScanStart`, so a week straddling a month edge isn't cut short."""
    now = (now or datetime.now(UTC)).astimezone()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = midnight.replace(day=1)
    week_start = midnight - timedelta(days=midnight.weekday())
    return min(month_start, week_start, midnight - timedelta(days=7))
