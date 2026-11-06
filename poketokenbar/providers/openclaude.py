"""OpenClaude provider — reads ~/.openclaude/projects/**/*.jsonl transcripts.

OpenClaude writes the same JSONL transcript format as Claude Code (assistant
rows with `message.usage`, `requestId`, `cost-state` ledger), so this reuses
the Claude parser, dedup, caching and pricing and only changes the roots.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from .claude import ClaudeProvider


def project_roots(
    home: Path | None = None, env: Mapping[str, str] | None = None
) -> list[Path]:
    """Existing OpenClaude project roots."""
    home = home or Path.home()
    env = os.environ if env is None else env

    candidates = [home / ".openclaude" / "projects"]
    configured = env.get("OPENCLAUDE_CONFIG_DIR")
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


class OpenClaudeProvider(ClaudeProvider):
    """OpenClaude local usage."""

    id = "openclaude"
    display_name = "OpenClaude"
    extra_roots: tuple = ()

    def _project_roots(self) -> list[Path]:
        return project_roots(home=self._home)
