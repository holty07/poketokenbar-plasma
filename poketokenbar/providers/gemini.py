"""Gemini CLI usage — reads the local OpenTelemetry log gemini-cli writes
when `telemetry.outfile` is set in `~/.gemini/settings.json`.

Unlike Claude Code, Codex, opencode, and Antigravity, gemini-cli persists no
local usage data by default — telemetry is off unless the user opts in, and
even then a network endpoint (GCP or an OTLP collector) is the default
target. Pointing `telemetry.outfile` at a path switches the exporter to a
plain local file instead — no network calls — verified by reading
`packages/core/src/telemetry/*` in the installed CLI (v0.59.0): with an
outfile set, `initializeTelemetry()` picks `FileLogExporter` over the OTLP/GCP
branches regardless of `telemetry.target`.

Each export batch appends one *pretty-printed* JSON object per log record —
`JSON.stringify(record, null, 2) + "\n"`, records concatenated directly, not
one-object-per-line — so this is parsed with a streaming decoder
(`raw_decode`) rather than a per-line `json.loads` like the other providers.

The record that matters is `attributes["event.name"] ==
"gemini_cli.api_response"` (from `ApiResponseEvent.toLogRecord` in
packages/core/src/telemetry/types.ts), carrying per-turn:
    input_token_count, output_token_count, cached_content_token_count,
    thoughts_token_count, tool_token_count, model, event.timestamp,
    prompt_id

`thoughts_token_count` is folded into output — Gemini bills thinking there,
same as opencode's `reasoning`. `tool_token_count` (tokens spent describing
available tools) is folded into input, since that's prompt content, not
reply content.
"""

from __future__ import annotations

import json
import os
from datetime import date as _date
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import pricing
from ..cache import ScanCache
from ..models import DailyUsage, Entry, ProviderEnrichment

PARSER_VERSION = 1

EVENT_API_RESPONSE = "gemini_cli.api_response"


def _int(value) -> int:
    return int(value) if isinstance(value, (int, float)) else 0


def default_log(home: Path | None = None) -> Path | None:
    home = home or Path.home()
    base = Path(os.environ.get("GEMINI_CLI_HOME") or (home / ".gemini"))
    path = base / "telemetry.log"
    return path if path.is_file() else None


def iter_records(text: str):
    """Yield each concatenated pretty-printed JSON object in `text`.

    `raw_decode` parses one value and reports where it ended; a trailing
    partial object (the exporter mid-write) fails to decode and is dropped
    rather than raising, since the next poll will see it complete.
    """
    decoder = json.JSONDecoder()
    idx = 0
    n = len(text)
    while idx < n:
        while idx < n and text[idx].isspace():
            idx += 1
        if idx >= n:
            return
        try:
            obj, end = decoder.raw_decode(text, idx)
        except ValueError:
            return
        yield obj
        idx = end


def _parse_record(record, index: int) -> Entry | None:
    if not isinstance(record, dict):
        return None
    attributes = record.get("attributes")
    if not isinstance(attributes, dict):
        return None
    if attributes.get("event.name") != EVENT_API_RESPONSE:
        return None
    timestamp = attributes.get("event.timestamp")
    if not isinstance(timestamp, str):
        return None
    try:
        date = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None

    input_tokens = _int(attributes.get("input_token_count")) + _int(attributes.get("tool_token_count"))
    output_tokens = _int(attributes.get("output_token_count")) + _int(attributes.get("thoughts_token_count"))
    cache_read = _int(attributes.get("cached_content_token_count"))
    if input_tokens + output_tokens + cache_read <= 0:
        return None

    prompt_id = attributes.get("prompt_id")
    identity = f"gemini|{prompt_id}" if prompt_id else f"gemini|{index}|{timestamp}"

    return Entry(
        id=identity,
        date=date,
        local_day=date.astimezone().strftime("%Y-%m-%d"),
        model=attributes.get("model") or "unknown",
        input=input_tokens,
        output=output_tokens,
        cache_write=0,
        cache_read=cache_read,
    )


def parse_log(path: Path) -> list[Entry] | None:
    """Every response entry in the log, or None when it could not be read
    this poll — the caller must not cache that as "no usage"."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    entries: list[Entry] = []
    for index, record in enumerate(iter_records(text)):
        entry = _parse_record(record, index)
        if entry is not None:
            entries.append(entry)
    return entries


class GeminiProvider:
    """gemini-cli local usage, via its own OpenTelemetry file exporter."""

    id = "gemini"
    display_name = "Gemini CLI"
    reports_cost = True
    PARSER_VERSION = PARSER_VERSION

    def __init__(self, cache: ScanCache | None = None, home: Path | None = None) -> None:
        self._cache = cache
        self._home = home

    def scan_entries(self) -> list[Entry]:
        path = default_log(home=self._home)
        live: set[str] = set()
        entries: list[Entry] = []
        if path is not None:
            try:
                stat = path.stat()
            except OSError:
                stat = None
            if stat is not None:
                live.add(str(path))
                cached = None
                if self._cache is not None:
                    cached = self._cache.get(
                        self.id, path, stat.st_mtime, stat.st_size, self.PARSER_VERSION
                    )
                if cached is None:
                    parsed = parse_log(path)
                    # Unreadable this poll (permissions, mid-write race).
                    # Skip without caching so the next poll retries under a
                    # signature that hasn't been spent on a wrong answer.
                    if parsed is not None:
                        entries = parsed
                        if self._cache is not None:
                            self._cache.put(
                                self.id, path, stat.st_mtime, stat.st_size, self.PARSER_VERSION, entries
                            )
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
        return ProviderEnrichment()
