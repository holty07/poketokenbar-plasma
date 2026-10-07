"""Codex usage — ports the Codex half of LocalUsageReader.swift.

Rollout files at ~/.codex/sessions/**/rollout-*.jsonl (and, once Codex
archives a session, ~/.codex/archived_sessions/rollout-*.jsonl) carry
`payload.type == "token_count"` events. Each event's `info.last_token_usage`
is the delta for that turn, so entries are summed rather than max-reduced.

Turn identity is `(file, turn index)`. A forked or resumed session copies its
parent's events verbatim, so the same turn can appear in several files; the
provider deduplicates on the session id it finds in the file metadata.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date as _date
from pathlib import Path

from .. import pricing
from ..cache import ScanCache
from ..models import DailyUsage, Entry, ProviderEnrichment
from .base import with_extra_roots
from .claude import _parse_timestamp, jsonl_files

PARSER_VERSION = 2  # v2: count total-only turns (#278)


MAX_TOKENS = 10**12


def _int(value) -> int:
    """A token count, or 0 when it is not a sane one. A negative or absurd
    value (> MAX_TOKENS) from a corrupt log line must not wreck the day's
    totals (upstream #307)."""
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value if 0 <= value <= MAX_TOKENS else 0


@dataclass(slots=True)
class ParsedRollout:
    entries: list[Entry]
    session_id: str | None


def parse_rollout(path: Path) -> ParsedRollout:
    """Parse one rollout file into per-turn entries."""
    entries: list[Entry] = []
    session_id: str | None = None
    model = "gpt-5.5"
    turn = 0
    name = path.name
    previous_cumulative: int | None = None

    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"session_id"' in line or '"model"' in line:
                    obj = _load(line)
                    if isinstance(obj, dict):
                        found = _find_session_id(obj)
                        if found and session_id is None:
                            session_id = found
                        found_model = _find_model(obj)
                        if found_model:
                            model = found_model
                if "token_count" not in line:
                    continue
                obj = _load(line)
                if not isinstance(obj, dict):
                    continue
                payload = obj.get("payload")
                if not isinstance(payload, dict) or payload.get("type") != "token_count":
                    continue
                info = payload.get("info")
                if not isinstance(info, dict):
                    continue
                last = info.get("last_token_usage")
                if not isinstance(last, dict):
                    continue
                date = _parse_timestamp(obj.get("timestamp", ""))
                if date is None:
                    continue

                input_total = _int(last.get("input_tokens"))
                cached = _int(last.get("cached_input_tokens"))
                output = _int(last.get("output_tokens"))
                non_cached = max(0, input_total - cached)
                cumulative = info.get("total_token_usage")
                cumulative = cumulative if isinstance(cumulative, dict) else None
                prior = previous_cumulative
                if cumulative is not None:
                    previous_cumulative = _int(cumulative.get("total_tokens"))
                last_total = _last_total(last)
                if non_cached + cached + output == 0 and last_total > 0 and _trust_total_only(
                    last_total, cumulative, prior
                ):
                    # #278: every component is 0 but total_tokens is set. The
                    # bucket split is unknown, so the total lands in input.
                    non_cached, cached, output = last_total, 0, 0
                entries.append(
                    Entry(
                        # Keyed by (cumulative, delta), not by file position or
                        # timestamp. A fork replays the parent's turns with
                        # fresh timestamps but an identical cumulative
                        # sequence, so this collapses the copies while keeping
                        # the fork's own turns. The delta is part of the key
                        # because a fork emits a zero-delta turn that repeats
                        # the parent's final cumulative value.
                        id=f"codex|{_cumulative(info)}|{_last_total(last)}",
                        date=date,
                        local_day=date.astimezone().strftime("%Y-%m-%d"),
                        model=model,
                        input=non_cached,
                        output=output,
                        cache_write=0,
                        cache_read=cached,
                    )
                )
                turn += 1
    except OSError:
        return ParsedRollout([], None)

    return ParsedRollout(entries, session_id)


def _components(usage: dict) -> int:
    input_total = _int(usage.get("input_tokens"))
    cached = _int(usage.get("cached_input_tokens"))
    return max(0, input_total - cached) + cached + _int(usage.get("output_tokens"))


def _trust_total_only(last_total: int, cumulative: dict | None, prior: int | None) -> bool:
    """Whether a zero-component `last_token_usage` should count its total.

    Trusted when the total is the only signal (no cumulative, or a cumulative
    that is itself total-only), when this turn is the whole session, or when
    the cumulative total grew since the previous turn in this file. A fork's
    post-replay zero-context turn repeats the parent's cumulative without
    growing it, so it stays at 0.
    """
    if cumulative is None:
        return True
    cum_total = _int(cumulative.get("total_tokens"))
    if _components(cumulative) == 0 and cum_total > 0:
        return True
    if cum_total == last_total:
        return True
    return prior is not None and cum_total > prior


def _cumulative(info: dict) -> int:
    total = info.get("total_token_usage")
    return _int(total.get("total_tokens")) if isinstance(total, dict) else 0


def _last_total(last: dict) -> int:
    return _int(last.get("total_tokens"))


def _load(line: str):
    try:
        return json.loads(line)
    except ValueError:
        return None


def _find_session_id(obj: dict) -> str | None:
    for key in ("session_id", "sessionId", "id"):
        value = obj.get(key)
        if isinstance(value, str) and value:
            return value
    payload = obj.get("payload")
    if isinstance(payload, dict):
        return _find_session_id(payload)
    return None


def _find_model(obj: dict) -> str | None:
    value = obj.get("model")
    if isinstance(value, str) and value:
        return value
    payload = obj.get("payload")
    if isinstance(payload, dict):
        return _find_model(payload)
    return None


def session_roots(home: Path | None = None) -> list[Path]:
    """Live and archived rollout roots. Archiving moves a rollout out of
    sessions/, so scanning only that folder silently dropped its history.
    The same turn found in both (mid-move) collapses on its content id."""
    home = home or Path.home()
    seen: set[Path] = set()
    roots: list[Path] = []
    for root in (home / ".codex" / "sessions", home / ".codex" / "archived_sessions"):
        if not root.is_dir():
            continue
        resolved = root.resolve()
        if resolved not in seen:
            seen.add(resolved)
            roots.append(root)
    return roots


class CodexProvider:
    id = "codex"
    # User-added scan folders (#177), set by the daemon from config.
    extra_roots: tuple = ()
    display_name = "Codex"
    reports_cost = True
    PARSER_VERSION = PARSER_VERSION

    def __init__(self, cache: ScanCache | None = None, home: Path | None = None) -> None:
        self._cache = cache
        self._home = home

    def scan_entries(self) -> list[Entry]:
        by_id: dict[str, Entry] = {}
        for root in with_extra_roots(session_roots(self._home), self.extra_roots):
            for path in sorted(jsonl_files(root)):
                for entry in parse_rollout(path).entries:
                    by_id.setdefault(entry.id, entry)
        return list(by_id.values())

    @staticmethod
    def dedup(entries: list[Entry]) -> list[Entry]:
        by_id: dict[str, Entry] = {}
        for e in entries:
            by_id.setdefault(e.id, e)
        return list(by_id.values())

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
            daily.total_cost += pricing.cost(
                e.model, e.input, e.output, e.cache_write, e.cache_read
            )
        daily.total_tokens = (
            daily.input_tokens
            + daily.output_tokens
            + daily.cache_creation_tokens
            + daily.cache_read_tokens
        )
        return daily

    def fetch_enrichment(self) -> ProviderEnrichment:
        return ProviderEnrichment()
