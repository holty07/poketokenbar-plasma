"""Cindy usage — ports upstream's Pi reader to Cindy's session layout.

Cindy (the `cindy` coding agent) appends one JSON envelope per line to session
files under `~/.config/CindyGlobal/pi-agent-home/sessions/**/*.jsonl`. The
envelope format is identical to Pi (see poketokenbar.providers.pi), so we reuse
the same parsing logic and only change the root paths.

Paths:
- Default: `~/.config/CindyGlobal/pi-agent-home/sessions`
- `CINDY_CODING_AGENT_DIR` + `/sessions` override
- `CINDY_CODING_AGENT_SESSION_DIR` override
"""

from __future__ import annotations

from pathlib import Path

from ..cache import ScanCache
from ..models import DailyUsage, Entry, ProviderEnrichment
from . import _local
from .base import with_extra_roots
from .pi import (
    PARSER_VERSION,
    _BUCKETS,
    message_date,
    parse_file,
    parse_line,
    source_cost,
    usage_entry,
)

# Re-export the parsing functions from pi so they don't need to be duplicated
# The Cindy format is identical to Pi's, so we can reuse everything.

__all__ = ["session_roots", "CindyProvider"]


def session_roots(home: Path | None = None) -> list[Path]:
    """Default and override roots for Cindy session files."""
    roots = [_local.home_dir(home) / ".config" / "CindyGlobal" / "pi-agent-home" / "sessions"]
    agent_dir = _local.env_path("CINDY_CODING_AGENT_DIR")
    if agent_dir is not None:
        roots.append(agent_dir / "sessions")
    session_dir = _local.env_path("CINDY_CODING_AGENT_SESSION_DIR")
    if session_dir is not None:
        roots.append(session_dir)
    return _local.normalized_roots(roots)


def is_session_file(path: Path) -> bool:
    return path.suffix == ".jsonl"


class CindyProvider:
    """Cindy local usage."""

    id = "cindy"
    # User-added scan folders (#177), set by the daemon from config.
    extra_roots: tuple = ()
    display_name = "Cindy"
    reports_cost = True
    PARSER_VERSION = PARSER_VERSION

    def __init__(self, cache: ScanCache | None = None, home: Path | None = None) -> None:
        self._cache = cache
        self._home = home

    def _files(self) -> list[Path]:
        return [
            path
            for root in with_extra_roots(session_roots(home=self._home), self.extra_roots)
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