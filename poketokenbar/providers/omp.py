"""omp (oh-my-pi) usage — ports upstream's omp reader (PokeTokenBar #214, #276).

omp is a Pi fork and writes Pi-format session JSONL under
`~/.omp/agent/sessions/**/*.jsonl`. Every `type:"message"` line whose
`message.role` is "assistant" is one API response; its `message.usage`
(`input` is already the non-cached input) is summed with no branch filter —
rewound branches were billed too. `compaction` / `branch_summary` envelope
usage counts as well (model "omp", since it names none), and aborted/errored
turns are skipped, mirroring the Pi provider. Per-model attribution reads
`message.model` (#276).

Message ids are 8 hex chars, unique only within a session, so entry ids are
scoped by file name. A usage line without an id falls back to its line number
— deterministic, so the scan cache and keep-max dedup stay stable across
rescans (upstream used a random UUID).

Subagent sessions (`<id>/__advisor.jsonl` etc.) live in their own files and
are picked up by the recursive scan. Anything under a `bridge/` folder is a
conversion copy pi-session-manager made from another source (Claude, Codex,
omp itself) whose usage is already counted where it came from, so it is
skipped. The check is made relative to the scan root, so a `bridge` directory
somewhere in the user's home path doesn't hide everything.

omp records its own model-price estimate in `usage.cost.total` (an explicit
zero is kept); that wins over pricing.TABLE.

Paths: `~/.omp/agent/sessions` is a home dot-dir, identical on Linux; omp's
`$OMP_CODING_AGENT_DIR` (+ `/sessions`) is honoured. Unlike Pi there is no
separate session-dir variable.
"""

from __future__ import annotations

from pathlib import Path

from ..cache import ScanCache
from ..models import DailyUsage, Entry, ProviderEnrichment
from . import _local
from .pi import message_date, usage_entry

PARSER_VERSION = 1


def session_roots(home: Path | None = None) -> list[Path]:
    roots = [_local.home_dir(home) / ".omp" / "agent" / "sessions"]
    agent_dir = _local.env_path("OMP_CODING_AGENT_DIR")
    if agent_dir is not None:
        roots.append(agent_dir / "sessions")
    return _local.normalized_roots(roots)


def is_usage_file(path: Path, root: Path) -> bool:
    if path.suffix != ".jsonl":
        return False
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        parts = path.parts
    return "bridge" not in parts


def parse_line(line: str, file_name: str, line_number: int) -> Entry | None:
    envelope = _local.loads(line)
    if not isinstance(envelope, dict):
        return None
    kind = envelope.get("type")
    if kind == "message":
        message = envelope.get("message")
        if (
            not isinstance(message, dict)
            or message.get("role") != "assistant"
            or message.get("stopReason") in ("aborted", "error")
        ):
            return None
        usage = message.get("usage")
        model = _local.text(message.get("model")) or "omp"
        moment = message_date(message, envelope)
    elif kind in ("compaction", "branch_summary"):
        usage = envelope.get("usage")
        model = "omp"
        moment = _local.parse_iso(envelope.get("timestamp"))
    else:
        return None
    if not isinstance(usage, dict) or not usage or moment is None:
        return None
    message_id = _local.text(envelope.get("id")) or f"#{line_number}"
    return usage_entry(f"omp|{file_name}|{message_id}", moment, model, usage)


def parse_file(path: Path) -> list[Entry] | None:
    content = _local.read_text(path)
    if content is None:
        return None
    entries = []
    for number, line in enumerate(content.splitlines()):
        # user/toolResult/custom lines carry no usage — skip before parsing.
        if '"usage"' not in line:
            continue
        entry = parse_line(line, path.name, number)
        if entry is not None:
            entries.append(entry)
    return _local.dedup_keep_max(entries)


class OmpProvider:
    """oh-my-pi (omp) local usage."""

    id = "omp"
    display_name = "omp"
    reports_cost = True
    PARSER_VERSION = PARSER_VERSION

    def __init__(self, cache: ScanCache | None = None, home: Path | None = None) -> None:
        self._cache = cache
        self._home = home

    def _files(self) -> list[Path]:
        files: list[Path] = []
        for root in session_roots(home=self._home):
            files.extend(
                _local.walk_files(root, lambda path, root=root: is_usage_file(path, root))
            )
        return files

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
