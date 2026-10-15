"""Jcode usage — ports upstream's local log readers (Pi, omp, Kimi Code).

Jcode (the `jcode` coding agent) appends one JSON envelope per line to session
journal files under `~/.jcode/sessions/**/*.journal.jsonl`. Usage lives on
assistant messages inside `append_messages`:

    {"meta": {"model": "fcm", "provider_key": "openai-compatible", ...},
     "append_messages": [{"id": "message_123", "role": "assistant",
         "timestamp": "2026-10-13T07:00:29.869Z",
         "token_usage": {"prompt_tokens": 30530, "input_tokens": 30530,
                         "output_tokens": 534, "cache_read_input_tokens": 12288}}]}

`input_tokens` equals `prompt_tokens` (cache reads already included), so we
subtract `cache_read_input_tokens` from `input` to avoid double-counting.
Older lines may lack `cache_read_input_tokens` or `cache_creation_input_tokens`.

The journal is append-only. A session may span multiple journal lines; each
line's `meta` carries the model/provider for its messages.
"""

from __future__ import annotations

from pathlib import Path

from ..cache import ScanCache
from ..models import DailyUsage, Entry, ProviderEnrichment
from . import _local
from .base import with_extra_roots

PARSER_VERSION = 1


def journal_roots(home: Path | None = None) -> list[Path]:
    roots = [_local.home_dir(home) / ".jcode" / "sessions"]
    return _local.normalized_roots(roots)


def is_journal_file(path: Path) -> bool:
    return path.suffix == ".jsonl" and path.name.endswith(".journal.jsonl")


def parse_line(line: str) -> list[Entry]:
    obj = _local.loads(line)
    if not isinstance(obj, dict):
        return []
    meta = obj.get("meta") or {}
    model = _local.text(meta.get("model")) or "unknown"
    provider_key = _local.text(meta.get("provider_key"))
    model_display = f"{provider_key}:{model}" if provider_key else model
    entries: list[Entry] = []
    for msg in obj.get("append_messages") or []:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") != "assistant":
            continue
        usage = msg.get("token_usage")
        if not isinstance(usage, dict):
            continue
        msg_id = _local.text(msg.get("id"))
        if msg_id is None:
            continue
        # message timestamp is ISO string with fractional seconds
        moment = _local.parse_iso(msg.get("timestamp"))
        if moment is None:
            continue
        # input_tokens == prompt_tokens (includes cache reads); subtract cache_read
        prompt = _local.tokens(usage.get("prompt_tokens"))
        input_raw = _local.tokens(usage.get("input_tokens"))
        cache_read = _local.tokens(usage.get("cache_read_input_tokens"))
        cache_write = _local.tokens(usage.get("cache_creation_input_tokens"))
        output = _local.tokens(usage.get("output_tokens"))
        # input_tokens already includes cache_read; subtract to get true input
        true_input = input_raw - cache_read
        if true_input < 0:
            true_input = 0
        if true_input + output + cache_write + cache_read <= 0:
            continue
        entry_id = f"jcode|{msg_id}"
        entries.append(
            _local.make_entry(
                entry_id,
                moment,
                model_display,
                input_=true_input,
                output=output,
                cache_write=cache_write,
                cache_read=cache_read,
            )
        )
    return entries


def parse_file(path: Path) -> list[Entry] | None:
    content = _local.read_text(path)
    if content is None:
        return None
    all_entries: list[Entry] = []
    for line in content.splitlines():
        if '"token_usage"' not in line:
            continue
        all_entries.extend(parse_line(line))
    return _local.dedup_keep_max(all_entries)


class JcodeProvider:
    """Jcode local usage."""

    id = "jcode"
    # User-added scan folders (#177), set by the daemon from config.
    extra_roots: tuple = ()
    display_name = "Jcode"
    reports_cost = True
    PARSER_VERSION = PARSER_VERSION

    def __init__(self, cache: ScanCache | None = None, home: Path | None = None) -> None:
        self._cache = cache
        self._home = home

    def _files(self) -> list[Path]:
        return [
            path
            for root in with_extra_roots(journal_roots(home=self._home), self.extra_roots)
            for path in _local.walk_files(root, is_journal_file)
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