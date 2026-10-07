"""Pi Agent usage — ports upstream's Pi reader (PokeTokenBar #189, #225).

Pi (the `pi` coding agent) appends one JSON envelope per line to session
files under `~/.pi/agent/sessions/**/*.jsonl`. Usage lives on three envelope
types:

    {"type": "message", "id": "...", "timestamp": "2026-08-17T10:00:00Z",
     "model": "...",                                   # forks (omp) put it here
     "message": {"role": "assistant", "model": "...",  # vanilla Pi puts it here
                 "timestamp": 1755424800000, "stopReason": "stop",
                 "usage": {"input": N, "output": N, "cacheWrite": N,
                           "cacheRead": N, "totalTokens": N,
                           "cost": {"total": 0.0123}}}}
    {"type": "compaction" | "branch_summary", "id": ..., "timestamp": ...,
     "usage": {...}}

`reasoning` is already a subset of `output`, so it is not added again.
Aborted and errored messages are skipped; a compaction's `retainedTail` is
copied context, not new usage. A usage object with only `totalTokens` has no
recoverable split, so the aggregate is kept as input. Pi records its own
model-price estimate in `usage.cost.total`; that wins over pricing.TABLE.

Forks copy envelopes (same id) into new session files, so dedup is global on
the envelope id across every scanned file.

Paths: `~/.pi/agent/sessions` is a home dot-dir and identical on Linux; Pi's
own `$PI_CODING_AGENT_DIR` (+ `/sessions`) and `$PI_CODING_AGENT_SESSION_DIR`
overrides are honoured as extra roots.
"""

from __future__ import annotations

from pathlib import Path

from ..cache import ScanCache
from ..models import DailyUsage, Entry, ProviderEnrichment
from . import _local

PARSER_VERSION = 1

_BUCKETS = ("input", "output", "cacheWrite", "cacheRead")


def session_roots(home: Path | None = None) -> list[Path]:
    roots = [_local.home_dir(home) / ".pi" / "agent" / "sessions"]
    agent_dir = _local.env_path("PI_CODING_AGENT_DIR")
    if agent_dir is not None:
        roots.append(agent_dir / "sessions")
    session_dir = _local.env_path("PI_CODING_AGENT_SESSION_DIR")
    if session_dir is not None:
        roots.append(session_dir)
    return _local.normalized_roots(roots)


def is_session_file(path: Path) -> bool:
    return path.suffix == ".jsonl"


def message_date(message: dict, envelope: dict):
    """The message's own epoch-millis timestamp, else the envelope's ISO one."""
    millis = _local.number(message.get("timestamp"))
    if millis is not None and millis > 0:
        moment = _local.from_epoch(millis / 1000)
        if moment is not None:
            return moment
    return _local.parse_iso(envelope.get("timestamp"))


def source_cost(usage: dict) -> float | None:
    cost = usage.get("cost")
    if not isinstance(cost, dict):
        return None
    value = _local.number(cost.get("total"))
    return value if value is not None and value >= 0 else None


def usage_entry(entry_id: str, moment, model: str, usage: dict) -> Entry | None:
    """Granular buckets when any is a number; else a total-only aggregate."""
    if any(_local.number(usage.get(name)) is not None for name in _BUCKETS):
        return _local.make_entry(
            entry_id,
            moment,
            model,
            input_=_local.tokens(usage.get("input")),
            output=_local.tokens(usage.get("output")),
            cache_write=_local.tokens(usage.get("cacheWrite")),
            cache_read=_local.tokens(usage.get("cacheRead")),
            explicit_cost=source_cost(usage),
        )
    if _local.number(usage.get("totalTokens")) is None:
        return None
    return _local.make_entry(
        entry_id, moment, model,
        input_=_local.tokens(usage.get("totalTokens")),
        explicit_cost=source_cost(usage),
    )


def parse_line(line: str) -> Entry | None:
    envelope = _local.loads(line)
    if not isinstance(envelope, dict):
        return None
    entry_id = _local.text(envelope.get("id"))
    kind = envelope.get("type")
    if entry_id is None or not isinstance(kind, str):
        return None
    if kind == "message":
        message = envelope.get("message")
        if not isinstance(message, dict) or message.get("stopReason") in ("aborted", "error"):
            return None
        usage = message.get("usage")
        moment = message_date(message, envelope)
        model = _local.text(envelope.get("model")) or _local.text(message.get("model")) or "pi"
    elif kind in ("compaction", "branch_summary"):
        usage = envelope.get("usage")
        moment = _local.parse_iso(envelope.get("timestamp"))
        model = "pi"
    else:
        return None
    if not isinstance(usage, dict) or moment is None:
        return None
    return usage_entry(entry_id, moment, model, usage)


def parse_file(path: Path) -> list[Entry] | None:
    content = _local.read_text(path)
    if content is None:
        return None
    entries = []
    for line in content.splitlines():
        if '"usage"' not in line:
            continue
        entry = parse_line(line)
        if entry is not None:
            entries.append(entry)
    return _local.dedup_keep_max(entries)


class PiProvider:
    """Pi Agent local usage."""

    id = "pi"
    display_name = "Pi"
    reports_cost = True
    PARSER_VERSION = PARSER_VERSION

    def __init__(self, cache: ScanCache | None = None, home: Path | None = None) -> None:
        self._cache = cache
        self._home = home

    def _files(self) -> list[Path]:
        return [
            path
            for root in session_roots(home=self._home)
            for path in _local.walk_files(root, is_session_file)
        ]

    def scan_entries(self) -> list[Entry]:
        entries = _local.scan_cached(
            self._cache, self.id, self.PARSER_VERSION, self._files(), parse_file
        )
        return _local.dedup_keep_max(entries)

    def fetch_daily(self, today: str | None = None) -> DailyUsage | None:
        return _local.daily(self.scan_entries(), today)

    def fetch_periods(self, today: str | None = None) -> dict:
        return _local.periods(self.scan_entries(), today)

    def fetch_enrichment(self) -> ProviderEnrichment:
        # Blocks/burn-rate remain unported for every provider; the *_ok flags
        # stay false so callers keep their previous values.
        return ProviderEnrichment()
