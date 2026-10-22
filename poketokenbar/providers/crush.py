"""Crush usage — reads the per-project session databases Crush writes.

Crush (charmbracelet/crush) keeps one SQLite database per project at
`<project>/.crush/crush.db`, and indexes every project it has opened in
`~/.local/share/crush/projects.json` (respecting `$XDG_DATA_HOME`):

    {"projects": [{"path": "/home/me/app", "data_dir": "/home/me/app/.crush",
                   "last_accessed": "2026-10-22T07:03:34Z"}]}

Crush records no per-message usage. Its only usage record is the `sessions`
row: `prompt_tokens`, `completion_tokens`, `cost` and `updated_at` (epoch
seconds). `cost` accumulates across the session, but the token columns are
overwritten each turn with that turn's request — the last request's size,
never a running total.

So per-request usage is *estimated*. Every assistant message is one request,
and its prompt is everything said before it. A request's input is taken as
the session's final `prompt_tokens` scaled by how much of the conversation
(the summed `length(parts)` of earlier messages) existed when it was sent:

    input_k = prompt_tokens * context_bytes_k / context_bytes_last

The last request therefore reproduces the measured counter exactly, and the
earlier ones shrink with the context. After a summary message
(`is_summary_message`) Crush sends only the summary onward, so the context
restarts from it. Output is `completion_tokens` for the last request and the
message's own size at ~4 bytes per token for the others. The system prompt
is not in `parts`, so early requests run low: the estimate errs under. The
session's accumulated `cost`, when non-zero, is shared out by estimated
tokens.

Each estimate is its own entry, dated by its message, so a session spanning
days splits across them. Sub-agent sessions are separate requests with their
own row and are estimated the same way. A session with no usable messages
falls back to one entry carrying its counters, dated by its last update.
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

PARSER_VERSION = 2  # v2: per-request estimates from context growth


MAX_TOKENS = 10**12
BYTES_PER_TOKEN = 4


def _int(value) -> int:
    """A token count, or 0 when it is not a sane one (upstream #307)."""
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value if 0 <= value <= MAX_TOKENS else 0


def _data_home(home: Path | None = None) -> Path:
    data_home = os.environ.get("XDG_DATA_HOME")
    return Path(data_home) if data_home else (home or Path.home()) / ".local" / "share"


def default_dbs(home: Path | None = None) -> list[Path]:
    """Every project database Crush's index lists, deduplicated, that exists."""
    index = _data_home(home) / "crush" / "projects.json"
    try:
        data = json.loads(index.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    projects = data.get("projects") if isinstance(data, dict) else None
    if not isinstance(projects, list):
        return []
    dbs: list[Path] = []
    seen: set[str] = set()
    for project in projects:
        if not isinstance(project, dict):
            continue
        data_dir = project.get("data_dir")
        if not isinstance(data_dir, str) or not data_dir:
            path = project.get("path")
            if not isinstance(path, str) or not path:
                continue
            data_dir = os.path.join(path, ".crush")
        db = Path(data_dir) / "crush.db"
        key = str(db)
        if key in seen or not db.is_file():
            continue
        seen.add(key)
        dbs.append(db)
    return dbs


def _signature(db_path: Path) -> tuple[float, int] | None:
    """`(mtime, size)` across the database and its `-wal` sibling: a WAL
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


_SESSIONS_SQL = """
SELECT s.id, s.prompt_tokens, s.completion_tokens, s.cost, s.updated_at,
       (SELECT m.model FROM messages m
         WHERE m.session_id = s.id AND m.role = 'assistant' AND m.model IS NOT NULL
         ORDER BY m.created_at DESC LIMIT 1)
FROM sessions s
"""

_MESSAGES_SQL = """
SELECT session_id, id, role, model, created_at, length(parts), {summary}
FROM messages ORDER BY session_id, created_at, rowid
"""


def _day(seconds: int) -> tuple[datetime, str]:
    date = datetime.fromtimestamp(seconds, tz=timezone.utc)
    return date, date.astimezone().strftime("%Y-%m-%d")


def _parse_session(session_id, prompt, completion, cost, updated_at, model) -> Entry | None:
    """The fallback: one entry carrying the session's own counters."""
    if not isinstance(session_id, str) or not session_id:
        return None
    if isinstance(updated_at, bool) or not isinstance(updated_at, int) or updated_at <= 0:
        return None
    input_tokens = _int(prompt)
    output_tokens = _int(completion)
    if input_tokens + output_tokens <= 0:
        return None
    date, local_day = _day(updated_at)
    explicit = _cost(cost)
    return Entry(
        id=f"crush|{session_id}",
        date=date,
        local_day=local_day,
        model=model if isinstance(model, str) and model else "unknown",
        input=input_tokens,
        output=output_tokens,
        cache_write=0,
        cache_read=0,
        # Zero means Crush had no price for the model, not that it was free;
        # fall back to the price table then.
        explicit_cost=explicit if explicit > 0 else None,
    )


def _cost(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        return 0.0
    return float(value) if value > 0 else 0.0


def _estimate_session(session_row, messages) -> list[Entry]:
    """Per-request estimates for one session; `messages` are its
    `(id, role, model, created_at, size, is_summary)` rows in order."""
    session_id, prompt, completion, cost, _updated, last_model = session_row
    final_prompt = _int(prompt)
    final_output = _int(completion)

    # (msg_id, model, created_at, context_bytes, own_bytes) per request.
    requests = []
    context = 0
    for msg_id, role, model, created_at, size, is_summary in messages:
        size = size if isinstance(size, int) and size > 0 else 0
        if is_summary:
            context = 0
        if (
            role == "assistant"
            and not is_summary
            and isinstance(msg_id, str)
            and isinstance(created_at, int)
            and not isinstance(created_at, bool)
            and created_at > 0
        ):
            requests.append((msg_id, model, created_at, context, size))
        context += size

    last_context = requests[-1][3] if requests else 0
    if final_prompt <= 0 or last_context <= 0:
        entry = _parse_session(*session_row)
        return [entry] if entry is not None else []

    fallback_model = last_model if isinstance(last_model, str) and last_model else "unknown"
    estimates = []
    for index, (msg_id, model, created_at, context_bytes, own_bytes) in enumerate(requests):
        last = index == len(requests) - 1
        input_tokens = final_prompt if last else final_prompt * context_bytes // last_context
        output_tokens = final_output if last else own_bytes // BYTES_PER_TOKEN
        if input_tokens + output_tokens <= 0:
            continue
        estimates.append((msg_id, model, created_at, input_tokens, output_tokens))

    total_cost = _cost(cost)
    total_tokens = sum(i + o for *_, i, o in estimates)
    entries = []
    for msg_id, model, created_at, input_tokens, output_tokens in estimates:
        date, local_day = _day(created_at)
        share = total_cost * (input_tokens + output_tokens) / total_tokens if total_tokens else 0.0
        entries.append(
            Entry(
                id=f"crush|{session_id}|{msg_id}",
                date=date,
                local_day=local_day,
                model=model if isinstance(model, str) and model else fallback_model,
                input=input_tokens,
                output=output_tokens,
                cache_write=0,
                cache_read=0,
                explicit_cost=share if total_cost > 0 else None,
            )
        )
    return entries


def _parse_database(db_path: Path) -> list[Entry] | None:
    """Every estimated entry in the database, or None when it could not be
    read this poll (BUSY, damaged) — the caller must not cache that as "no
    usage"."""
    conn = _open_readonly(db_path)
    if conn is None:
        return None
    entries: list[Entry] = []
    try:
        try:
            sessions = conn.execute(_SESSIONS_SQL).fetchall()
            columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
            summary = "is_summary_message" if "is_summary_message" in columns else "0"
            rows = conn.execute(_MESSAGES_SQL.format(summary=summary)).fetchall()
        except sqlite3.OperationalError as exc:
            # Not a Crush database (or an older schema) is permanent.
            if "no such" in str(exc):
                return []
            return None
        by_session: dict[str, list] = {}
        for session_id, *rest in rows:
            by_session.setdefault(session_id, []).append(rest)
        for session_row in sessions:
            if not isinstance(session_row[0], str) or not session_row[0]:
                continue
            entries.extend(_estimate_session(session_row, by_session.get(session_row[0], [])))
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return entries


class CrushProvider:
    """Crush local usage."""

    id = "crush"
    display_name = "Crush"
    reports_cost = True
    PARSER_VERSION = PARSER_VERSION

    def __init__(self, cache: ScanCache | None = None, home: Path | None = None) -> None:
        self._cache = cache
        self._home = home

    def scan_entries(self) -> list[Entry]:
        live: set[str] = set()
        entries: list[Entry] = []
        for db_path in default_dbs(home=self._home):
            sig = _signature(db_path)
            if sig is None:
                continue
            mtime, size = sig
            live.add(str(db_path))
            cached = None
            if self._cache is not None:
                cached = self._cache.get(self.id, db_path, mtime, size, self.PARSER_VERSION)
            if cached is None:
                parsed = _parse_database(db_path)
                # Unreadable this poll: skip without caching so the next poll
                # retries under a signature that hasn't been spent.
                if parsed is None:
                    continue
                cached = parsed
                if self._cache is not None:
                    self._cache.put(self.id, db_path, mtime, size, self.PARSER_VERSION, parsed)
            entries.extend(cached)
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
            daily.total_cost += pricing.entry_cost(e)
        daily.total_tokens = daily.input_tokens + daily.output_tokens
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
            cost = pricing.entry_cost(e)
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
        return ProviderEnrichment()
