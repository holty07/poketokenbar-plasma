import json

import pytest

from poketokenbar import save, snapshots, transfer
from poketokenbar.balance import Rarity
from poketokenbar.companion import CompanionState, DexEntry


def _state(tokens=1000, dex=1):
    s = CompanionState()
    s.used_since_install = tokens
    s.dex = [DexEntry(base_id=i, final_id=i, chain_order=[i], rarity=Rarity.COMMON) for i in range(dex)]
    return s


def test_snapshots_are_listed_newest_first_and_are_valid_imports(tmp_path):
    p = tmp_path / "companion.json"
    snapshots.create(_state(tokens=1), p, now=1_000)
    snapshots.create(_state(tokens=2), p, now=2_000)
    listed = snapshots.list_snapshots(p)
    assert [s.lifetime_tokens for s in listed] == [2, 1]
    # A snapshot is an export envelope, so it imports like one.
    raw = json.loads(listed[0].path.read_text())
    assert transfer.decode(raw).used_since_install == 2


def test_same_second_snapshots_do_not_overwrite_each_other(tmp_path):
    p = tmp_path / "companion.json"
    snapshots.create(_state(tokens=1), p, now=1_000)
    snapshots.create(_state(tokens=2), p, now=1_000)
    assert len(snapshots.list_snapshots(p)) == 2


def test_only_the_newest_ten_are_kept(tmp_path):
    p = tmp_path / "companion.json"
    for i in range(12):
        snapshots.create(_state(tokens=i), p, now=1_000 + i * 60)
    kept = snapshots.list_snapshots(p)
    assert len(kept) == snapshots.MAX_KEEP
    assert kept[-1].lifetime_tokens == 2


def test_auto_snapshot_waits_twelve_hours_and_skips_empty_saves(tmp_path):
    p = tmp_path / "companion.json"
    t0 = 1_700_000_000
    assert snapshots.auto_snapshot_if_due(CompanionState(), p, now=t0) is None
    assert snapshots.auto_snapshot_if_due(_state(), p, now=t0) is not None
    assert snapshots.auto_snapshot_if_due(_state(), p, now=t0 + snapshots.AUTO_INTERVAL - 1) is None
    assert snapshots.auto_snapshot_if_due(_state(), p, now=t0 + snapshots.AUTO_INTERVAL) is not None


def test_a_corrupt_save_recovers_from_the_newest_snapshot(tmp_path):
    p = tmp_path / "companion.json"
    snapshots.create(_state(tokens=42, dex=3), p, now=1_000)
    p.write_text("{not json", encoding="utf-8")
    loaded = save.load(p)
    assert loaded.used_since_install == 42 and len(loaded.dex) == 3
    assert (tmp_path / "companion.json.corrupt").exists()
    # The recovered state was written back, so the next load is clean.
    assert save.load(p).used_since_install == 42


def test_snapshot_ids_cannot_escape_the_folder(tmp_path):
    p = tmp_path / "companion.json"
    for bad in ("../companion.json", "companion-snapshot-/../../x.json", "other.json"):
        with pytest.raises(transfer.TransferError):
            snapshots.load(p, bad)


def test_restore_takes_a_safety_snapshot_and_keeps_todays_baseline(tmp_path):
    import random

    from poketokenbar.companion_store import CompanionStore

    store = CompanionStore(save_path=tmp_path / "companion.json", rng=random.Random(1))
    store.update({"claude_code": 100}, today="2026-10-07")  # seeds the baseline
    old = snapshots.create(_state(tokens=7, dex=2), tmp_path / "companion.json", now=1_000)
    store.state.used_since_install = 999
    store.restore_snapshot(old.id)
    assert store.state.used_since_install == 7
    assert store.state.claimed_today_tokens_by_provider == {"claude_code": 100}
    assert store.state.last_date == "2026-10-07"
    # The pre-restore state was snapshotted too.
    assert any(s.lifetime_tokens == 999 for s in snapshots.list_snapshots(tmp_path / "companion.json"))
    # No re-credit of today's usage after the restore.
    store.update({"claude_code": 100}, today="2026-10-07")
    assert store.state.used_since_install == 7
