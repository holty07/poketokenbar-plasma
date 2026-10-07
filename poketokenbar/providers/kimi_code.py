"""Kimi Code usage — ports upstream's Kimi Code reader (PokeTokenBar #403).

Kimi Code (MoonshotAI/kimi-code) records one `usage.record` line per LLM
request in `sessions/<workDirKey>/<sessionId>/agents/<agent>/wire.jsonl`
under its data root:

    {"type": "usage.record", "model": "kimi-k2", "usageScope": "turn",
     "time": 1779256800302,
     "usage": {"inputOther": N, "output": N,
               "inputCacheRead": N, "inputCacheCreation": N}}

`usageScope` is "turn" inside a turn and "session" for requests outside one
(e.g. compaction); both are per-request deltas, as is a record with no scope.
Unknown future scopes are skipped rather than guessed. Every agent (main and
each subagent) writes its own wire file, so all of them count; other jsonl in
the session tree (context/state logs) does not.

Records carry no id. `/fork` copies a session's history into a new session
directory, so the id is built from the record's own content, which folds the
copy back onto the original instead of counting it twice.

Paths. The standalone CLI keeps its data root at `~/.kimi-code` (or
`$KIMI_CODE_HOME`) on every OS — it is a home dot-dir, so the macOS default
carries over unchanged. Upstream also scans the macOS desktop app's embedded
runtime under `~/Library/Application Support/kimi-desktop/...`; an Electron
app's Linux equivalent of that directory is `$XDG_CONFIG_HOME/kimi-desktop`,
so that path is scanned too (unverified — Kimi ships no Linux desktop build
we could check — and harmless when absent).

Kimi records no cost and its models are not in pricing.TABLE, so cost is $0.
"""

from __future__ import annotations

from pathlib import Path

from ..cache import ScanCache
from ..models import DailyUsage, Entry, ProviderEnrichment
from . import _local
from .base import with_extra_roots

PARSER_VERSION = 1

_DESKTOP_RUNTIME = "kimi-desktop/daimon-share/daimon/runtime/kimi-code/home"
_SCOPES = (None, "turn", "session")


def session_roots(home: Path | None = None) -> list[Path]:
    """`<root>/sessions` of the CLI home, the desktop runtime home, and
    `$KIMI_CODE_HOME`."""
    roots = [
        _local.home_dir(home) / ".kimi-code" / "sessions",
        _local.xdg_config_home(home) / _DESKTOP_RUNTIME / "sessions",
    ]
    override = _local.env_path("KIMI_CODE_HOME")
    if override is not None:
        roots.append(override / "sessions")
    return _local.normalized_roots(roots)


def is_usage_file(path: Path) -> bool:
    return path.name == "wire.jsonl"


def parse_line(line: str) -> Entry | None:
    record = _local.loads(line)
    if not isinstance(record, dict) or record.get("type") != "usage.record":
        return None
    usage = record.get("usage")
    millis = _local.number(record.get("time"))
    if not isinstance(usage, dict) or millis is None or millis <= 0:
        return None
    scope = record.get("usageScope")
    if scope not in _SCOPES:
        return None
    moment = _local.from_epoch(millis / 1000)
    if moment is None:
        return None
    input_ = _local.tokens(usage.get("inputOther"))
    output = _local.tokens(usage.get("output"))
    cache_read = _local.tokens(usage.get("inputCacheRead"))
    cache_write = _local.tokens(usage.get("inputCacheCreation"))
    model = _local.text(record.get("model")) or "kimi-code"
    entry_id = (
        f"kimi|{int(millis)}|{model}|{scope or '-'}"
        f"|{input_}|{output}|{cache_read}|{cache_write}"
    )
    return _local.make_entry(entry_id, moment, model, input_, output, cache_write, cache_read)


def parse_file(path: Path) -> list[Entry] | None:
    """Every usage record in one wire file; None when it can't be read."""
    content = _local.read_text(path)
    if content is None:
        return None
    entries = []
    for line in content.splitlines():
        # Most lines are messages and traces — skip them before JSON parsing.
        if '"usage.record"' not in line:
            continue
        entry = parse_line(line)
        if entry is not None:
            entries.append(entry)
    return entries


class KimiCodeProvider:
    """Kimi Code CLI local usage."""

    id = "kimi_code"
    # User-added scan folders (#177), set by the daemon from config.
    extra_roots: tuple = ()
    display_name = "Kimi Code"
    reports_cost = False
    PARSER_VERSION = PARSER_VERSION

    def __init__(self, cache: ScanCache | None = None, home: Path | None = None) -> None:
        self._cache = cache
        self._home = home

    def _files(self) -> list[Path]:
        return [
            path
            for root in with_extra_roots(session_roots(home=self._home), self.extra_roots)
            for path in _local.walk_files(root, is_usage_file)
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
