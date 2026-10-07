"""Local save snapshots — ports SaveSnapshots.swift (upstream #330).

Point-in-time copies of the companion save, written in the export envelope
format so a snapshot is also a valid import file. They live next to the save:
`<save dir>/.snapshots/<save file name>/companion-snapshot-<stamp>.json`.

Automatic snapshots are taken at most every 12 hours; ten are kept. When the
save itself can't be read, the newest valid snapshot is restored instead of
starting from nothing.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from . import transfer
from .companion import CompanionState

PREFIX = "companion-snapshot-"
MAX_KEEP = 10
AUTO_INTERVAL = 12 * 3600


@dataclass(slots=True)
class Snapshot:
    id: str
    path: Path
    date: float
    dex_count: int
    lifetime_tokens: int
    species_id: int | None
    is_shiny: bool

    def payload(self) -> dict:
        return {
            "id": self.id,
            "date": self.date,
            "date_text": datetime.fromtimestamp(self.date, tz=UTC).astimezone().strftime("%Y-%m-%d %H:%M"),
            "dex_count": self.dex_count,
            "lifetime_tokens": self.lifetime_tokens,
            "species_id": self.species_id,
            "is_shiny": self.is_shiny,
        }


def directory(save_path: Path) -> Path:
    return save_path.parent / ".snapshots" / save_path.name


def _describe(snapshot_id: str, path: Path, date: float, state: CompanionState) -> Snapshot:
    active = state.active
    return Snapshot(
        id=snapshot_id,
        path=path,
        date=date,
        dex_count=len(state.dex),
        lifetime_tokens=state.used_since_install,
        species_id=active.current_id if active else None,
        is_shiny=active.shows_shiny if active else False,
    )


# path -> ((mtime_ns, size), result). Listing runs every poll; without this
# each one re-decoded every snapshot just to read its date and summary.
_READ_CACHE: dict[Path, tuple[tuple[int, int], tuple[float, CompanionState] | None]] = {}


def _read(path: Path) -> tuple[float, CompanionState] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    signature = (stat.st_mtime_ns, stat.st_size)
    cached = _READ_CACHE.get(path)
    if cached is not None and cached[0] == signature:
        return cached[1]
    result = _read_uncached(path)
    _READ_CACHE[path] = (signature, result)
    return result


def _read_uncached(path: Path) -> tuple[float, CompanionState] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        state = transfer.decode(raw)
    except (OSError, ValueError, transfer.TransferError):
        return None
    date = raw.get("exported_at")
    if isinstance(date, bool) or not isinstance(date, (int, float)):
        try:
            date = path.stat().st_mtime
        except OSError:
            date = 0.0
    return float(date), state


def list_snapshots(save_path: Path) -> list[Snapshot]:
    """Valid snapshots, newest first. Unreadable files are skipped."""
    folder = directory(save_path)
    if not folder.is_dir():
        return []
    out = []
    for path in folder.glob(f"{PREFIX}*.json"):
        read = _read(path)
        if read is not None:
            out.append(_describe(path.name, path, read[0], read[1]))
    out.sort(key=lambda s: (s.date, s.id), reverse=True)
    return out


def create(state: CompanionState, save_path: Path, now: float | None = None) -> Snapshot:
    now = time.time() if now is None else now
    folder = directory(save_path)
    folder.mkdir(parents=True, exist_ok=True)
    stem = PREFIX + datetime.fromtimestamp(now, tz=UTC).astimezone().strftime("%Y-%m-%d-%H%M%S")
    target = folder / f"{stem}.json"
    counter = 1
    while target.exists():
        target = folder / f"{stem}-{counter}.json"
        counter += 1
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(transfer.encode(state, now=now), indent=2), encoding="utf-8")
    tmp.replace(target)
    prune(save_path)
    return _describe(target.name, target, now, state)


def prune(save_path: Path, keep: int = MAX_KEEP) -> None:
    for old in list_snapshots(save_path)[keep:]:
        old.path.unlink(missing_ok=True)
        _READ_CACHE.pop(old.path, None)


def auto_snapshot_if_due(
    state: CompanionState, save_path: Path, now: float | None = None
) -> Snapshot | None:
    """Snapshot when the newest one is at least AUTO_INTERVAL old. A brand new
    save with nothing in it isn't worth keeping."""
    now = time.time() if now is None else now
    if state.used_since_install <= 0 and not state.dex and state.active is None:
        return None
    snapshots = list_snapshots(save_path)
    if snapshots and now - snapshots[0].date < AUTO_INTERVAL:
        return None
    return create(state, save_path, now)


def load(save_path: Path, snapshot_id: str) -> CompanionState:
    """Read one snapshot by id. Ids are file names inside the snapshot
    folder; anything that would escape it is refused."""
    if "/" in snapshot_id or "\\" in snapshot_id or not snapshot_id.startswith(PREFIX):
        raise transfer.TransferError(f"not a snapshot: {snapshot_id}")
    read = _read_uncached(directory(save_path) / snapshot_id)
    if read is None:
        raise transfer.TransferError(f"snapshot unreadable or missing: {snapshot_id}")
    return read[1]


def latest_valid(save_path: Path) -> CompanionState | None:
    snapshots = list_snapshots(save_path)
    if not snapshots:
        return None
    read = _read_uncached(snapshots[0].path)
    return read[1] if read else None
