"""Provider extension point — ports UsageProvider.swift.

Adding a source means adding an implementation and a PROVIDERS entry. Never
branch on a provider id in shared code; see docs/reference/provider-extension.md.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path
from typing import Protocol

from ..models import DailyUsage, ProviderEnrichment


def with_extra_roots(defaults: Iterable[Path], extras: Iterable) -> list[Path]:
    """Built-in roots plus the user's extra scan folders (upstream #177).

    Extras are only ever added — a custom folder never replaces a built-in
    one — and folders that don't exist are skipped. Duplicates and folders
    nested inside another root are dropped so no file is scanned twice.
    """
    from ._local import normalized_roots

    extra = [Path(os.path.expanduser(str(p))) for p in extras or ()]
    return normalized_roots([*defaults, *(p for p in extra if p.exists())])


class UsageProvider(Protocol):
    id: str
    display_name: str
    reports_cost: bool

    def fetch_daily(self, today: str | None = None) -> DailyUsage | None:
        """Today's totals. None when the source is absent or unused today."""
        ...

    def fetch_enrichment(self) -> ProviderEnrichment:
        """Blocks and period totals. Best effort; never raises."""
        ...
