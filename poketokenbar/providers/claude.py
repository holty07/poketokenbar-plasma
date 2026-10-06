"""Claude Code usage — ports the Claude half of LocalUsageReader.swift.

Rule: keep `type == "assistant"` rows, sum the four token fields of
`message.usage`, deduplicate on `(message.id, requestId)` keeping the entry
with the LARGEST total, and bucket by local date from `timestamp`.
"""

from __future__ import annotations

import math
import os
from collections.abc import Iterator, Mapping
from datetime import date as _date
from datetime import datetime
from pathlib import Path

from .. import pricing
from ..cache import ScanCache
from ..models import DailyUsage, Entry, ProviderEnrichment

try:  # orjson is ~2x faster on this workload but must not be required
    import orjson

    def _loads(raw: str | bytes):
        return orjson.loads(raw)

except ModuleNotFoundError:  # pragma: no cover - exercised on hosts without orjson
    import json

    def _loads(raw: str | bytes):
        return json.loads(raw)


def _int(value) -> int:
    return value if isinstance(value, int) else 0


def _parse_timestamp(raw: str) -> datetime | None:
    """ISO-8601 with a trailing 'Z' and optional fractional seconds."""
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def parse_line(line: str) -> Entry | None:
    try:
        obj = _loads(line)
    except Exception:
        return None
    if not isinstance(obj, dict) or obj.get("type") != "assistant":
        return None
    msg = obj.get("message")
    if not isinstance(msg, dict):
        return None
    usage = msg.get("usage")
    if not isinstance(usage, dict):
        return None
    date = _parse_timestamp(obj.get("timestamp", ""))
    if date is None:
        return None
    return Entry(
        id=f"{msg.get('id') or ''}|{obj.get('requestId') or ''}",
        date=date,
        local_day=date.astimezone().strftime("%Y-%m-%d"),
        model=msg.get("model") or "unknown",
        # Only top-level fields. usage["iterations"] repeats these numbers.
        input=_int(usage.get("input_tokens")),
        output=_int(usage.get("output_tokens")),
        cache_write=_int(usage.get("cache_creation_input_tokens")),
        cache_read=_int(usage.get("cache_read_input_tokens")),
    )


def dedup_keep_max(entries: list[Entry]) -> list[Entry]:
    """Keep the largest-total entry per id — the completed one."""
    by_id: dict[str, Entry] = {}
    for e in entries:
        existing = by_id.get(e.id)
        if existing is None or e.total > existing.total:
            by_id[e.id] = e
    return list(by_id.values())


def cost_model_key(model: str) -> str:
    """`modelUsage` keys carry a context-window variant (`claude-opus-5[1m]`)
    that `message.model` never has; both pool under the base id."""
    cut = model.find("[")
    return model if cut < 0 else model[:cut]


def parse_cost_state_line(line: str) -> dict[str, float] | None:
    """Per-model session cost from one `type:"cost-state"` record.

    Claude Code appends this ledger cumulatively through a session, so the
    last record in a file holds the session's authoritative totals.
    """
    try:
        obj = _loads(line)
    except ValueError:  # json and orjson decode errors are both ValueErrors
        return None
    if not isinstance(obj, dict) or obj.get("type") != "cost-state":
        return None
    usage = obj.get("modelUsage")
    if not isinstance(usage, dict):
        return None
    by_model: dict[str, float] = {}
    for model, fields in usage.items():
        if not isinstance(fields, dict):
            continue
        value = fields.get("costUSD")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        value = float(value)
        if not math.isfinite(value) or value < 0:
            continue
        key = cost_model_key(model)
        by_model[key] = by_model.get(key, 0.0) + value
    return by_model or None


def apply_reported_cost(entries: list[Entry], cost_by_model: dict[str, float]) -> None:
    """Spread each model's reported session total across that model's entries
    in proportion to tokens. The session sum stays exact; only the split
    within the session is inferred. A model with no parsed entry is skipped
    rather than reassigned, so it never inflates another model's day."""
    by_model: dict[str, list[Entry]] = {}
    for e in entries:
        if e.total > 0:
            by_model.setdefault(cost_model_key(e.model), []).append(e)
    for model, reported in cost_by_model.items():
        group = by_model.get(model)
        if not group:
            continue
        tokens = sum(e.total for e in group)
        remaining = reported
        for n, e in enumerate(group):
            # The last entry absorbs the rounding remainder.
            share = remaining if n == len(group) - 1 else reported * e.total / tokens
            e.explicit_cost = max(0.0, share)
            remaining -= share


def parse_file(path: Path) -> list[Entry]:
    out: list[Entry] = []
    cost_state: dict[str, float] | None = None
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"cost-state"' in line:
                    state = parse_cost_state_line(line)
                    if state is not None:
                        cost_state = state  # cumulative: the last one wins
                    continue
                # Substring prefilter before JSON decode — the cold scan reads
                # hundreds of MB and most lines are not assistant turns.
                if '"usage"' not in line or '"assistant"' not in line:
                    continue
                entry = parse_line(line)
                if entry is not None:
                    out.append(entry)
    except OSError:
        return []
    deduped = dedup_keep_max(out)
    if cost_state is not None:
        apply_reported_cost(deduped, cost_state)
    return deduped


def project_roots(
    home: Path | None = None, env: Mapping[str, str] | None = None
) -> list[Path]:
    """Existing Claude project roots, symlink-deduplicated.

    macOS also probes ~/Library/Application Support/Claude for Claude Desktop
    embedded sessions. That path cannot exist on Linux, so it is omitted rather
    than branched on.
    """
    home = home or Path.home()
    env = os.environ if env is None else env

    candidates = [home / ".claude" / "projects", home / ".config" / "claude" / "projects"]
    configured = env.get("CLAUDE_CONFIG_DIR")
    if configured:
        candidates.append(Path(configured) / "projects")

    seen: set[Path] = set()
    roots: list[Path] = []
    for path in candidates:
        if not path.is_dir():
            continue
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        roots.append(path)
    return roots


def jsonl_files(root: Path) -> Iterator[Path]:
    """Every *.jsonl under root, including inside hidden directories."""
    yield from root.rglob("*.jsonl")


class ClaudeProvider:
    """Claude Code local usage."""

    id = "claude_code"
    display_name = "Claude Code"
    reports_cost = True
    # Bump when parse_line changes shape, to invalidate cached blobs.
    # v2: adopt the cost-state ledger's reported cost over table estimates.
    PARSER_VERSION = 2

    def __init__(self, cache: ScanCache | None = None, home: Path | None = None) -> None:
        self._cache = cache
        self._home = home

    def scan_entries(self) -> list[Entry]:
        """Every parsed entry across all roots, globally deduplicated."""
        all_entries: list[Entry] = []
        live: set[str] = set()
        for root in project_roots(home=self._home):
            for path in jsonl_files(root):
                try:
                    stat = path.stat()
                except OSError:
                    continue
                live.add(str(path))
                entries = None
                if self._cache is not None:
                    entries = self._cache.get(
                        self.id, path, stat.st_mtime, stat.st_size, self.PARSER_VERSION
                    )
                if entries is None:
                    entries = parse_file(path)
                    if self._cache is not None:
                        self._cache.put(
                            self.id,
                            path,
                            stat.st_mtime,
                            stat.st_size,
                            self.PARSER_VERSION,
                            entries,
                        )
                all_entries.extend(entries)
        if self._cache is not None:
            self._cache.prune(self.id, live)
        # Global dedup — the same turn may appear under overlapping roots.
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
            # Priced per entry, because a day mixes models with different rates.
            daily.total_cost += pricing.entry_cost(e)
        daily.total_tokens = (
            daily.input_tokens
            + daily.output_tokens
            + daily.cache_creation_tokens
            + daily.cache_read_tokens
        )
        return daily

    def fetch_periods(self, today: str | None = None) -> dict:
        """Week-to-date and month-to-date totals.

        The week starts Monday, matching the Swift period grouping.
        """
        from datetime import datetime, timedelta

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
        # Blocks/burn-rate remain unported; the *_ok flags stay false so callers
        # keep their previous values rather than zeroing.
        return ProviderEnrichment()
