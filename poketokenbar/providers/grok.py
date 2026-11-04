"""Grok Build CLI usage — ports upstream's Grok reader (PokeTokenBar).

Grok Build CLI keeps session updates under `~/.grok/sessions/**/updates.jsonl`
(honours `$GROK_HOME`). Each line is an ACP durable-wire envelope:

    {"timestamp":1785000000,"method":"_x.ai/session/update",
     "params":{"sessionId":"s1","update":{"sessionUpdate":"turn_completed",
     "prompt_id":"p-1","stop_reason":"end_turn","usage":{
       "inputTokens":41203,"outputTokens":812,"totalTokens":42015,
       "cachedReadTokens":38400,"reasoningTokens":260,
       "modelUsage":{"grok-build-1":{"inputTokens":41203,...}}}},
     "_meta":{"eventId":"ev-p-1","agentTimestampMs":1785000010000}}}

Only `turn_completed` updates count. Streaming `agent_message_chunk` lines
carry `_meta.totalTokens` = context-window size, never usage — they are skipped.

`inputTokens` **includes** cached reads, so uncached input = inputTokens −
cachedReadTokens. `outputTokens` **includes** reasoningTokens (not extra).
Grok folds cache writes into the prompt, so cache_write = 0.
`costUsdTicks` (1e-10 dollars per tick) is the server-reported cost, trusted
only when `costIsPartial` and `usageIsIncomplete` are both false.
Grok has no rate card — models price at $0 unless explicit_cost is set.

A snake_case headless variant (`input_tokens`, already cache-adjusted) is
accepted as a defensive path — it must not be cache-adjusted again.

Subagent sessions (`summary.json` session_kind of `subagent`/`subagent_fork`)
are skipped: their tokens are already folded into the parent turn.
`worktree` sessions are kept. Dedup is by `prompt_id` globally, so a forked
copy of a session is counted once.
`isReplay: true` lines are dropped entirely.
"""

from __future__ import annotations

import os
from datetime import datetime, UTC
from pathlib import Path

from ..cache import ScanCache
from ..models import DailyUsage, Entry, ProviderEnrichment
from . import _local
from .base import with_extra_roots

PARSER_VERSION = 1

_SESSION_KINDS_SKIP = frozenset({"subagent", "subagent_fork"})


def session_roots(home: Path | None = None) -> list[Path]:
    """`<root>/sessions` under ~/.grok, or `$GROK_HOME/sessions`."""
    roots = [_local.home_dir(home) / ".grok" / "sessions"]
    override = _local.env_path("GROK_HOME")
    if override is not None:
        roots.append(override / "sessions")
    return _local.normalized_roots(roots)


def is_updates_file(path: Path) -> bool:
    return path.name == "updates.jsonl"


def _read_session_kind(session_dir: Path) -> str | None:
    """`session_kind` from summary.json beside updates.jsonl, or None."""
    summary = session_dir / "summary.json"
    if not summary.is_file():
        return None
    try:
        obj = _local.loads(summary.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    if isinstance(obj, dict):
        kind = obj.get("session_kind")
        return kind if isinstance(kind, str) else None
    return None


def _moment_from_meta(meta: dict, envelope_ts) -> float | None:
    """Turn timestamp: _meta.agentTimestampMs (millis) wins; falls back to
    envelope `timestamp` (seconds)."""
    if isinstance(meta, dict):
        ms = meta.get("agentTimestampMs")
        if isinstance(ms, (int, float)) and not isinstance(ms, bool):
            return float(ms) / 1000.0
    if isinstance(envelope_ts, (int, float)) and not isinstance(envelope_ts, bool):
        return float(envelope_ts)
    return None


def _coerce_int(value) -> int | None:
    """An int from JSON: null, bool, text → None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _get_usage_field(usage: dict, camel: str, snake: str) -> int | None:
    """Read a usage field: camelCase preferred; fall back to snake_case. A
    null camel value means "use snake", not zero."""
    if isinstance(usage, dict):
        v = usage.get(camel)
        if v is not None:
            return _coerce_int(v)
        v = usage.get(snake)
        if v is not None:
            return _coerce_int(v)
    return None


def _parse_updates_line(line: str) -> Entry | None:
    """One `turn_completed` line → Entry, or None (not a turn, replay, etc.)."""
    if '"turn_completed"' not in line:
        return None
    record = _local.loads(line)
    if not isinstance(record, dict):
        return None
    params = record.get("params")
    if not isinstance(params, dict):
        return None
    update = params.get("update")
    if not isinstance(update, dict):
        return None
    if update.get("sessionUpdate") != "turn_completed":
        return None
    meta = params.get("_meta")
    if not isinstance(meta, dict):
        meta = {}
    if meta.get("isReplay") is True:
        return None

    prompt_id = update.get("prompt_id")
    prompt_id = _local.text(prompt_id) if prompt_id is not None else None
    if not prompt_id:
        return None

    usage = update.get("usage")
    if not isinstance(usage, dict):
        return None

    input_tokens = _get_usage_field(usage, "inputTokens", "input_tokens")
    output_tokens = _get_usage_field(usage, "outputTokens", "output_tokens")
    cached_read = _get_usage_field(usage, "cachedReadTokens", "cached_read_tokens")
    total_tokens = _get_usage_field(usage, "totalTokens", "total_tokens")
    cost_ticks = _get_usage_field(usage, "costUsdTicks", None)
    cost_is_partial = usage.get("costIsPartial") is True
    usage_is_incomplete = usage.get("usageIsIncomplete") is True

    model_usage = usage.get("modelUsage")
    model = "grok"
    if isinstance(model_usage, dict):
        for name, mu in model_usage.items():
            if isinstance(name, str) and name:
                model = name
                break

    moment = _moment_from_meta(meta, record.get("timestamp"))
    if moment is None:
        return None
    moment_dt = datetime.fromtimestamp(moment, tz=UTC)

    # inputTokens includes cached reads; subtract them. Snake_case input is
    # already cache-adjusted — use as-is.
    snake_input = _get_usage_field(usage, None, "input_tokens")
    if snake_input is not None and _get_usage_field(usage, "inputTokens", None) is None:
        # Pure snake_case payload: input_tokens already excludes cache.
        input_ = snake_input
        cache_read = _get_usage_field(usage, "cachedReadTokens", "cached_read_tokens")
        if cache_read is None:
            cache_read = 0
    else:
        if input_tokens is None:
            return None
        cache_read = _get_usage_field(usage, "cachedReadTokens", "cached_read_tokens")
        if cache_read is None:
            cache_read = 0
        # Clamp cache read to the prompt total so a bad payload can't make
        # input negative.
        cache_read = min(cache_read, input_tokens)
        input_ = input_tokens - cache_read

    if output_tokens is None:
        return None
    output = output_tokens  # includes reasoningTokens
    cache_write = 0  # Grok folds cache writes into the prompt

    if total_tokens is None:
        total = input_ + output + cache_write + (cache_read or 0)
    else:
        total = total_tokens
        # Residual against the reported total goes to output so the entry
        # total always matches the source.
        bucket_sum = input_ + output + cache_write + (cache_read or 0)
        if bucket_sum < total:
            output += total - bucket_sum

    explicit_cost = None
    if cost_ticks is not None and not cost_is_partial and not usage_is_incomplete:
        explicit_cost = cost_ticks / 10_000_000_000.0

    return _local.make_entry(
        f"grok|{prompt_id}",
        moment_dt,
        model,
        input_,
        output,
        cache_write,
        cache_read or 0,
        explicit_cost=explicit_cost,
    )


def _parse_file(path: Path) -> list[Entry] | None:
    """Every turn_completed entry in one updates.jsonl; None when unreadable."""
    session_dir = path.parent
    skip_kind = _read_session_kind(session_dir) in _SESSION_KINDS_SKIP
    content = _local.read_text(path)
    if content is None:
        return None
    entries: list[Entry] = []
    for line in content.splitlines():
        entry = _parse_updates_line(line)
        if entry is None:
            continue
        if skip_kind:
            continue
        entries.append(entry)
    return entries


class GrokProvider:
    """Grok Build CLI local usage."""

    id = "grok"
    display_name = "Grok Build CLI"
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
            for root in with_extra_roots(session_roots(home=self._home), self.extra_roots)
            for path in _local.walk_files(root, is_updates_file)
        ]

    def scan_entries(self) -> list[Entry]:
        entries = _local.scan_cached(
            self._cache, self.id, self.PARSER_VERSION, self._files(), _parse_file
        )
        return _local.dedup_keep_max(entries)

    def fetch_daily(self, today: str | None = None) -> DailyUsage | None:
        return _local.daily(self.scan_entries(), today)

    def fetch_periods(self, today: str | None = None) -> dict:
        return _local.periods(self.scan_entries(), today)

    def fetch_enrichment(self) -> ProviderEnrichment:
        return ProviderEnrichment()
