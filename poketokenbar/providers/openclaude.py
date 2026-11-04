"""OpenClaude provider — reads ~/.openclaude/projects/**/*.jsonl transcripts.

Each project directory contains JSONL files with conversation history.
Assistant messages with tool_use type have token usage in message.usage.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from datetime import date
from typing import Iterator

from .base import UsageProvider
from ._local import dedup_keep_max, expand
from ..models import DailyUsage, Entry


class OpenClaudeProvider(UsageProvider):
    id = "openclaude"
    display_name = "OpenClaude"
    reports_cost = False

    def __init__(self, cache=None, extra_roots: list[Path] | None = None):
        self.extra_roots = extra_roots or []

    def _default_roots(self) -> list[Path]:
        base = expand("~/.openclaude/projects")
        return [base] if base.exists() else []

    def _scan_roots(self) -> list[Path]:
        roots = self._default_roots()
        roots.extend(self.extra_roots)
        return [r for r in roots if r.exists()]

    def fetch_daily(self, today: date) -> DailyUsage | None:
        entries = list(self.scan_entries(today, today))
        if not entries:
            return None
        # Deduplicate by requestId (same as Claude provider)
        entries = dedup_keep_max(entries, key=lambda e: e.request_id or e.message_id)
        return self._aggregate(entries)

    def scan_entries(self, start: date, end: date) -> Iterator[Entry]:
        """Scan all project JSONL files for entries in [start, end]."""
        for root in self._scan_roots():
            try:
                project_dirs = list(root.iterdir())
            except OSError:
                # Skip roots we cannot list (permissions, transient race).
                continue
            for project_dir in project_dirs:
                if not project_dir.is_dir():
                    continue
                try:
                    jsonl_files = list(project_dir.glob("*.jsonl"))
                except OSError:
                    # Skip project directories we cannot list.
                    continue
                for jsonl_file in jsonl_files:
                    try:
                        yield from self._parse_file(jsonl_file, start, end)
                    except Exception:
                        # Skip malformed files
                        continue

    def _parse_file(self, path: Path, start: date, end: date) -> Iterator[Entry]:
        """Parse a single JSONL file, yielding Entry objects."""
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # OpenClaude JSONL structure: each line is a message
                # Look for assistant messages with tool_use that have usage data
                msg_type = data.get("type")
                if msg_type != "assistant":
                    continue

                message = data.get("message", {})
                # Tool use messages have usage data
                if message.get("role") != "assistant":
                    continue

                # Check for usage in the message
                usage = message.get("usage")
                if not usage:
                    continue

                # Parse timestamp
                timestamp_str = data.get("timestamp")
                if not timestamp_str:
                    continue
                try:
                    # Format: "2026-10-27T10:30:00.000Z"
                    from datetime import datetime
                    dt = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
                    entry_date = dt.date()
                except Exception:
                    continue

                if not (start <= entry_date <= end):
                    continue

                # Extract tokens (same fields as Claude)
                input_tokens = usage.get("input_tokens", 0) or 0
                output_tokens = usage.get("output_tokens", 0) or 0
                cache_creation = usage.get("cache_creation_input_tokens", 0) or 0
                cache_read = usage.get("cache_read_input_tokens", 0) or 0

                total = input_tokens + output_tokens + cache_creation + cache_read
                if total == 0:
                    continue

                # Use message id as request_id for dedup
                message_id = data.get("message", {}).get("id") or data.get("uuid")
                request_id = data.get("requestId") or message_id

                yield Entry(
                    date=entry_date,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cache_creation_tokens=cache_creation,
                    cache_read_tokens=cache_read,
                    cost=0.0,  # OpenClaude doesn't report cost
                    model=data.get("model", "unknown"),
                    message_id=message_id,
                    request_id=request_id,
                )

    def _aggregate(self, entries: list[Entry]) -> DailyUsage:
        total_tokens = sum(e.total_tokens for e in entries)
        input_tokens = sum(e.input_tokens for e in entries)
        output_tokens = sum(e.output_tokens for e in entries)
        cache_creation = sum(e.cache_creation_tokens for e in entries)
        cache_read = sum(e.cache_read_tokens for e in entries)
        total_cost = sum(e.cost for e in entries)
        return DailyUsage(
            total_tokens=total_tokens,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_tokens=cache_creation,
            cache_read_tokens=cache_read,
            total_cost=total_cost,
        )