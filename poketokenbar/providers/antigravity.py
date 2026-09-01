"""Antigravity usage — ports LocalAntigravityUsageReader.swift.

Antigravity keeps one SQLite database per conversation under
`~/.gemini/antigravity{,-cli,-ide}/conversations/<id>.db`, with the per-call
token ledger inside a protobuf blob in the `gen_metadata` table. The field
numbers below are the writer's own contract — read out of the CLI binary's
embedded `FileDescriptorProto` pool, not inferred from sample values:

    gen_metadata.data              exa.cortex_pb.CortexStepGeneratorMetadata
      1     chat_model               exa.cortex_pb.ChatModelMetadata
      1.4     usage                  exa.codeium_common_pb.ModelUsageStats
      1.4.2     input_tokens         prompt tokens, cache reads NOT included
      1.4.3     output_tokens        thinking_output_tokens + response_output_tokens
      1.4.4     cache_write_tokens   declared, never written by this CLI
      1.4.5     cache_read_tokens    prompt cache hit
      1.4.11    response_id          globally unique per call
      1.9     chat_start_metadata    exa.cortex_pb.ChatStartMetadata
      1.9.4     created_at           google.protobuf.Timestamp, absent since Antigravity 2.0
      1.19    response_model         e.g. "gemini-3.6-flash"
      4       execution_id           the execution these tokens were spent in

Records written by Antigravity 2.0 omit `created_at`; the date then comes
from the `steps` table's own `StepMetadata` blob (field 8 finished_at, else
field 1 created_at), correlated by `response_id` and falling back to
`execution_id` + file mtime.

Antigravity is subscription-billed and reports no dollar amount. Every
entry's model is stored as `antigravity/<model>` so pricing.rate() zeroes it
via its dedicated prefix check, rather than accidentally pricing an
underlying `claude-sonnet-4-6` call at Anthropic's rate.
"""

from __future__ import annotations

import sqlite3
import urllib.parse
from collections.abc import Iterator
from datetime import date as _date
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import pricing
from ..cache import ScanCache
from ..models import DailyUsage, Entry, ProviderEnrichment
from .claude import dedup_keep_max

PARSER_VERSION = 1

# A per-call counter this large is a sentinel, not a count — discard it
# rather than let it dominate every aggregate it reaches (today's total, the
# companion). Mirrors AntigravityProto.tokenCeiling.
TOKEN_CEILING = 1_000_000_000

_ABSENT = object()
_INVALID = object()


# --- protobuf wire format: just enough to walk the Cascade metadata blobs ---


def _read_varint(data: bytes, index: int) -> tuple[int, int] | None:
    result = 0
    shift = 0
    n = len(data)
    while index < n:
        byte = data[index]
        index += 1
        result |= (byte & 0x7F) << shift
        if byte & 0x80 == 0:
            return result, index
        shift += 7
        if shift > 63:  # a varint is at most ten bytes
            return None
    return None


def _walk(data: bytes) -> Iterator[tuple[int, int, bytes | None]]:
    """Yield (field, value, payload) for each top-level field in order.

    `value` is set for varints (wire type 0); `payload` for length-delimited
    fields (wire type 2). Fixed32/64 are skipped. Groups (wire types 3/4)
    were removed from proto3, so meeting one means these bytes are not the
    message they were taken for — stop.
    """
    index = 0
    n = len(data)
    while index < n:
        key = _read_varint(data, index)
        if key is None:
            return
        key_value, index = key
        field = key_value >> 3
        wire_type = key_value & 7
        if field <= 0:
            return
        if wire_type == 0:
            parsed = _read_varint(data, index)
            if parsed is None:
                return
            value, index = parsed
            yield field, value, None
        elif wire_type == 1:
            if n - index < 8:
                return
            index += 8
        elif wire_type == 2:
            parsed = _read_varint(data, index)
            if parsed is None:
                return
            length, after_length = parsed
            if length > n - after_length:
                return
            end = after_length + length
            yield field, 0, data[after_length:end]
            index = end
        elif wire_type == 5:
            if n - index < 4:
                return
            index += 4
        else:
            return


def _pb_varint(data: bytes, field: int) -> int | None:
    for f, value, payload in _walk(data):
        if f == field and payload is None:
            return value
    return None


def _pb_message(data: bytes, field: int) -> bytes | None:
    for f, _value, payload in _walk(data):
        if f == field and payload is not None:
            return payload
    return None


def _pb_string(data: bytes, field: int) -> str | None:
    payload = _pb_message(data, field)
    if not payload:
        return None
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return text or None


def _token_count(data: bytes, field: int) -> int | None:
    """None means the field was there and its value cannot be a count — not
    the same as the field being a legitimate, absent-therefore-zero field
    like `cache_write_tokens`, which this CLI declares but never writes."""
    value = _pb_varint(data, field)
    if value is None:
        return 0
    return value if value <= TOKEN_CEILING else None


def _pb_timestamp(data: bytes, field: int) -> datetime | None:
    """A `google.protobuf.Timestamp` at `field`: seconds (1) + nanos (2),
    plausibility-checked the way a malformed varint carrying the whole
    uint64 range would otherwise overflow the arithmetic downstream."""
    stamp = _pb_message(data, field)
    if stamp is None:
        return None
    seconds = _pb_varint(stamp, 1)
    if seconds is None or not (1_000_000_000 <= seconds <= 4_102_444_800):
        return None
    nanos_raw = _pb_varint(stamp, 2)
    nanos = nanos_raw if nanos_raw is not None and nanos_raw < 1_000_000_000 else 0
    return datetime.fromtimestamp(seconds + nanos / 1_000_000_000, tz=timezone.utc)


def _chat_start_created_at(chat_model: bytes):
    """`chat_start_metadata.created_at`. Distinguishes "not present" (try the
    steps-table fallback) from "present but implausible" (drop the record)."""
    start = _pb_message(chat_model, 9)
    if start is None:
        return _ABSENT
    stamp = _pb_message(start, 4)
    if stamp is None:
        return _ABSENT
    seconds = _pb_varint(stamp, 1)
    if seconds is None or not (1_000_000_000 <= seconds <= 4_102_444_800):
        return _INVALID
    nanos_raw = _pb_varint(stamp, 2)
    nanos = nanos_raw if nanos_raw is not None and nanos_raw < 1_000_000_000 else 0
    return datetime.fromtimestamp(seconds + nanos / 1_000_000_000, tz=timezone.utc)


# --- reading one conversation store ---


def default_roots(home: Path | None = None) -> list[Path]:
    """Known conversation directories across Antigravity editions (2.0/Core,
    CLI, IDE). A directory is absent unless that flavor has run."""
    home = home or Path.home()
    candidates = [
        home / ".gemini" / "antigravity" / "conversations",
        home / ".gemini" / "antigravity-cli" / "conversations",
        home / ".gemini" / "antigravity-ide" / "conversations",
    ]
    return [p for p in candidates if p.is_dir()]


def conversation_databases(root: Path) -> Iterator[Path]:
    try:
        names = sorted(p.name for p in root.iterdir())
    except OSError:
        return
    for name in names:
        if name.endswith(".db"):
            yield root / name


def _signature(db_path: Path) -> tuple[float, int] | None:
    """`(mtime, size)` across the database and its `-wal` sibling. A WAL
    commit lands in the sibling and leaves the main file's stat alone, so
    keying on the `.db` alone would miss stores that had just moved."""
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
    """`mode=ro` cannot create the `-shm` file a WAL database needs and fails
    outright on a conversation with no `-wal` sibling yet; `immutable=1`
    reads those. Opening is lazy, so a trivial query forces the failure to
    happen here instead of on the first real read."""
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


def _generation_step_dates(
    conn: sqlite3.Connection,
) -> tuple[dict[str, datetime], dict[str, list[datetime]]] | None:
    """Dates for records Antigravity 2.0 left undated in `gen_metadata`
    itself. Returns None when the table can't be read (BUSY, damaged) —
    the caller drops the whole conversation for this poll rather than
    dating its records wrong."""
    try:
        rows = conn.execute(
            "SELECT metadata FROM steps WHERE metadata IS NOT NULL ORDER BY idx"
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return {}, {}
        return None

    by_response: dict[str, datetime] = {}
    by_execution: dict[str, list[datetime]] = {}
    for (blob,) in rows:
        if not blob:
            continue
        blob = bytes(blob)
        date = _pb_timestamp(blob, 8) or _pb_timestamp(blob, 1)
        if date is None:
            continue
        model = _pb_message(blob, 9)
        if model is not None:
            response_id = _pb_string(model, 11)
            if response_id:
                by_response[response_id] = date
        execution_id = _pb_string(blob, 12)
        if execution_id:
            by_execution.setdefault(execution_id, []).append(date)
    return by_response, by_execution


def _parse_generation_metadata(blob: bytes, conversation: str, index: int, fallback_date) -> Entry | None:
    chat_model = _pb_message(blob, 1)
    if chat_model is None:
        return None
    usage = _pb_message(chat_model, 4)
    if usage is None:
        return None

    response_id = _pb_string(usage, 11)
    execution_id = _pb_string(blob, 4)

    created = _chat_start_created_at(chat_model)
    if created is _ABSENT:
        date = fallback_date(response_id, execution_id)
        if date is None:
            return None
    elif created is _INVALID:
        return None
    else:
        date = created

    # The turn's own id, not the file it happens to sit in — a copied
    # conversation must not read as fresh spend. response_id is populated
    # on every recorded call.
    identity = f"antigravity|{response_id}" if response_id else f"antigravity|{conversation}|{index}"

    # response_model names the model that answered; the rate lookup is
    # short-circuited by the "antigravity/" prefix in pricing.rate().
    model_name = _pb_string(chat_model, 19) or "unknown"

    input_tokens = _token_count(usage, 2) or 0
    output_tokens = _token_count(usage, 3) or 0
    cache_write = _token_count(usage, 4) or 0
    cache_read = _token_count(usage, 5) or 0
    if input_tokens + output_tokens + cache_write + cache_read <= 0:
        return None

    return Entry(
        id=identity,
        date=date,
        local_day=date.astimezone().strftime("%Y-%m-%d"),
        model=f"antigravity/{model_name}",
        input=input_tokens,
        output=output_tokens,
        cache_write=cache_write,
        cache_read=cache_read,
    )


def _parse_conversation(db_path: Path) -> list[Entry] | None:
    """One store's entries, or None when it could not be read this poll
    (BUSY, damaged) — the caller must not cache that as "no usage"."""
    conn = _open_readonly(db_path)
    if conn is None:
        return None
    try:
        step_dates = _generation_step_dates(conn)
        if step_dates is None:
            return None
        by_response, by_execution = step_dates

        conversation = db_path.stem
        sig = _signature(db_path)
        store_mtime = (
            datetime.fromtimestamp(sig[0], tz=timezone.utc) if sig else datetime.now(timezone.utc)
        )
        taken_by_execution: dict[str, int] = {}

        def fallback_date(response_id: str | None, execution_id: str | None) -> datetime:
            if response_id and response_id in by_response:
                return by_response[response_id]
            if execution_id and by_execution.get(execution_id):
                dates = by_execution[execution_id]
                ordinal = taken_by_execution.get(execution_id, 0)
                taken_by_execution[execution_id] = ordinal + 1
                return dates[min(ordinal, len(dates) - 1)]
            return store_mtime

        try:
            rows = conn.execute(
                "SELECT idx, data FROM gen_metadata WHERE data IS NOT NULL ORDER BY idx"
            ).fetchall()
        except sqlite3.OperationalError as exc:
            # SQLITE_ERROR here is "no such table" — a permanent property of
            # the file (it isn't a conversation store) — anything else is
            # this moment failing to read a store that may well be one.
            if "no such table" in str(exc):
                return []
            return None

        entries: list[Entry] = []
        for index, blob in rows:
            if not blob:
                continue
            entry = _parse_generation_metadata(bytes(blob), conversation, index, fallback_date)
            if entry is not None:
                entries.append(entry)
        return entries
    except sqlite3.Error:
        return None
    finally:
        conn.close()


class AntigravityProvider:
    """Antigravity local usage."""

    id = "antigravity"
    display_name = "Antigravity"
    reports_cost = False
    PARSER_VERSION = PARSER_VERSION

    def __init__(self, cache: ScanCache | None = None, home: Path | None = None) -> None:
        self._cache = cache
        self._home = home

    def scan_entries(self) -> list[Entry]:
        """Every parsed entry across all roots, globally deduplicated."""
        all_entries: list[Entry] = []
        live: set[str] = set()
        for root in default_roots(home=self._home):
            for db_path in conversation_databases(root):
                sig = _signature(db_path)
                if sig is None:
                    continue
                mtime, size = sig
                key = str(db_path)
                live.add(key)
                entries = None
                if self._cache is not None:
                    entries = self._cache.get(self.id, db_path, mtime, size, self.PARSER_VERSION)
                if entries is None:
                    parsed = _parse_conversation(db_path)
                    if parsed is None:
                        # Unreadable this poll (BUSY, damaged). Skip without
                        # caching so the next poll retries under a signature
                        # that hasn't been spent on a wrong answer.
                        continue
                    entries = parsed
                    if self._cache is not None:
                        self._cache.put(self.id, db_path, mtime, size, self.PARSER_VERSION, entries)
                all_entries.extend(entries)
        if self._cache is not None:
            self._cache.prune(self.id, live)
        # Global dedup — a copied conversation may repeat rows across stores.
        return dedup_keep_max(all_entries)

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
            # Always 0 in practice — pricing.rate() zeroes the "antigravity/"
            # prefix — but computed the same way every other provider is.
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
