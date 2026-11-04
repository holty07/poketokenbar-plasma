"""Gemini CLI usage — ports upstream's Gemini reader (PokeTokenBar).

Gemini CLI keeps session transcripts under `~/.gemini/tmp/**/chats/`,
two shapes:

New `.jsonl` (chat recording service), one JSON record per line:

    {"type":"gemini","id":"m2","timestamp":"2026-07-03T01:00:10.000Z",
     "model":"gemini-2.5-pro",
     "tokens":{"input":1000,"output":50,"cached":600,"thoughts":30,"tool":20,"total":1100}}
    {"type":"message_update","id":"m3","tokens":{"input":10,"output":8,"cached":0,
     "thoughts":2,"tool":0,"total":20}}

`tokens.input` **includes** cached reads, so uncached input = input − cached.
`output` **includes** thoughts. `tool` tokens are added to input.
`message_update` for an id replaces that message's tokens (final values win).

Legacy `.json`:

    {"sessionId":"s0","startTime":"2026-07-02T00:00:00.000Z","messages":[
      {"id":"a1","type":"gemini","timestamp":"...","model":"gemini-2.5-pro",
       "tokens":{"input":100,"output":10,"cached":0,"thoughts":0,"tool":0,"total":110}}
    ]}

Model ids are used bare and price against pricing.TABLE's gemini rows;
undocumented variants fall through to $0 (never guessed).
"""

from __future__ import annotations

from pathlib import Path

from ..cache import ScanCache
from ..models import DailyUsage, Entry, ProviderEnrichment
from . import _local
from .base import with_extra_roots

PARSER_VERSION = 2  # v2: jsonl + legacy json, message_update replaces


def chat_roots(home: Path | None = None) -> list[Path]:
    """`<root>/tmp/chats` under ~/.gemini."""
    roots = [_local.home_dir(home) / ".gemini" / "tmp" / "chats"]
    return _local.normalized_roots(roots)


def is_chat_file(path: Path) -> bool:
    return path.suffix in (".jsonl", ".json")


def _map_tokens(model: str, tokens: dict) -> tuple[int, int, int, int, int]:
    """Map Gemini's tokens block to (input, output, cache_write, cache_read, total).

    `input` includes cached reads; subtract them. `output` includes thoughts.
    `tool` tokens are added to input. Gemini has no separate cache-write bucket.
    """
    if not isinstance(tokens, dict):
        return 0, 0, 0, 0, 0
    gross_input = _local.tokens(tokens.get("input"))
    cached = _local.tokens(tokens.get("cached"))
    output = _local.tokens(tokens.get("output"))
    thoughts = _local.tokens(tokens.get("thoughts"))
    tool = _local.tokens(tokens.get("tool"))
    total = _local.tokens(tokens.get("total"))
    input_ = max(gross_input - cached, 0) + tool
    output += thoughts
    return input_, output, 0, cached, total


def _make_gemini_entry(
    message_id: str, moment, model: str, tokens: dict
) -> Entry | None:
    input_, output, cache_write, cache_read, total = _map_tokens(model, tokens)
    if input_ + output + cache_write + cache_read <= 0:
        return None
    return _local.make_entry(
        f"gemini|{message_id}",
        moment,
        model,
        input_,
        output,
        cache_write,
        cache_read,
    )


def _parse_jsonl(path: Path) -> list[Entry] | None:
    content = _local.read_text(path)
    if content is None:
        return []
    entries: list[Entry] = []
    pending_updates: dict[str, dict] = {}
    for line in content.splitlines():
        if '"type"' not in line:
            continue
        record = _local.loads(line)
        if not isinstance(record, dict):
            continue
        rtype = record.get("type")
        if rtype == "message_update":
            mid = _local.text(record.get("id"))
            tokens = record.get("tokens")
            if mid and isinstance(tokens, dict):
                pending_updates["gemini|" + mid] = tokens
            continue
        if rtype != "gemini":
            continue
        mid = _local.text(record.get("id"))
        moment = _local.flexible_date(record.get("timestamp"))
        model = _local.text(record.get("model")) or "gemini"
        tokens = record.get("tokens")
        if not mid or moment is None or not isinstance(tokens, dict):
            continue
        entry = _make_gemini_entry(mid, moment, model, tokens)
        if entry is not None:
            entries.append(entry)
    # Apply message_updates: same id, final values win. Replace the original
    # entry's buckets with the update's, keeping the original moment/model.
    if pending_updates:
        out: list[Entry] = []
        for e in entries:
            if e.id in pending_updates:
                tokens = pending_updates[e.id]
                input_, output, cache_write, cache_read, _ = _map_tokens(
                    e.model, tokens
                )
                total = input_ + output + cache_write + cache_read
                if total > 0:
                    out.append(
                        _local.make_entry(
                            e.id, e.date, e.model, input_, output,
                            cache_write, cache_read,
                        )
                    )
            else:
                out.append(e)
        entries = out
    return entries


def _parse_json(path: Path) -> list[Entry] | None:
    content = _local.read_text(path)
    if content is None:
        return []
    try:
        obj = _local.loads(content)
    except (ValueError, TypeError):
        return []
    if not isinstance(obj, dict):
        return []
    messages = obj.get("messages")
    if not isinstance(messages, list):
        return []
    entries: list[Entry] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if msg.get("type") != "gemini":
            continue
        mid = _local.text(msg.get("id"))
        moment = _local.flexible_date(msg.get("timestamp"))
        model = _local.text(msg.get("model")) or "gemini"
        tokens = msg.get("tokens")
        if not mid or moment is None or not isinstance(tokens, dict):
            continue
        entry = _make_gemini_entry(mid, moment, model, tokens)
        if entry is not None:
            entries.append(entry)
    return entries


def parse_file(path: Path) -> list[Entry] | None:
    """Every entry in one transcript file; None when it can't be read."""
    try:
        if path.suffix == ".jsonl":
            return _parse_jsonl(path)
        if path.suffix == ".json":
            return _parse_json(path)
    except (OSError, ValueError):
        return None
    return None


class GeminiProvider:
    """Gemini CLI local usage."""

    id = "gemini"
    display_name = "Gemini CLI"
    reports_cost = True
    PARSER_VERSION = PARSER_VERSION

    # User-added scan folders (#177), set by the daemon from config.
    extra_roots: tuple = ()

    def __init__(self, cache: ScanCache | None = None, home: Path | None = None) -> None:
        self._cache = cache
        self._home = home

    def _files(self) -> list[Path]:
        return [
            path
            for root in with_extra_roots(chat_roots(home=self._home), self.extra_roots)
            for path in _local.walk_files(root, is_chat_file)
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
        return ProviderEnrichment()
