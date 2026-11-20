"""Per-generation Pokédex completion (#442) — mirrors upstream's
PokedexRegionTests and DexCompletionStoreTests."""

import random

from poketokenbar import balance, dex_progress, save
from poketokenbar.balance import Rarity
from poketokenbar.companion import CompanionState, DexEntry
from poketokenbar.companion_store import CompanionStore
from poketokenbar.notify import Notifier

# --- pure region math ------------------------------------------------------


def test_region_boundaries_follow_the_national_dex():
    assert dex_progress.region_for(1) == "kanto"
    assert dex_progress.region_for(151) == "kanto"
    assert dex_progress.region_for(152) == "johto"
    assert dex_progress.region_for(251) == "johto"
    assert dex_progress.region_for(252) == "hoenn"
    assert dex_progress.region_for(386) == "hoenn"
    assert dex_progress.region_for(387) == "sinnoh"
    assert dex_progress.region_for(493) == "sinnoh"
    assert dex_progress.region_for(494) == "unova"
    assert dex_progress.region_for(649) == "unova"
    assert dex_progress.region_for(0) is None
    assert dex_progress.region_for(650) is None


def test_region_totals_add_up_to_the_national_dex():
    totals = [c.total for c in dex_progress.region_completions({})]
    assert totals == [151, 100, 135, 107, 156]
    assert sum(totals) == dex_progress.NATIONAL_TOTAL == 649


def test_completions_count_owned_and_shiny_per_region():
    owned = {1: True, 2: False, 152: False, 700: True}
    by_key = {c.region: c for c in dex_progress.region_completions(owned)}
    assert (by_key["kanto"].owned, by_key["kanto"].shiny_owned) == (2, 1)
    assert by_key["johto"].owned == 1
    assert by_key["unova"].owned == 0
    # Out-of-range ids count toward nothing.
    assert dex_progress.national_progress(owned) == (3, 1, 649)


def test_completing_kanto_earns_its_badge_only():
    owned = {sid: False for sid in range(1, 152)}
    assert dex_progress.earned_badges(owned, 0) == ["region.kanto"]


def test_shiny_tier_and_national_and_unown_badges():
    owned = {sid: True for sid in range(1, 650)}
    badges = dex_progress.earned_badges(owned, len(balance.UNOWN_FORMS))
    assert badges[:2] == ["region.kanto", "regionShiny.kanto"]
    assert badges[-3:] == ["unown", "national", "nationalShiny"]
    assert len(badges) == 5 * 2 + 3


def test_unown_needs_all_28_letters():
    assert "unown" not in dex_progress.earned_badges({}, 27)
    assert "unown" in dex_progress.earned_badges({}, 28)


def test_newly_earned_skips_announced_and_keeps_order():
    earned = ["region.kanto", "region.johto", "unown"]
    assert dex_progress.newly_earned(earned, {"region.kanto"}) == ["region.johto", "unown"]


def test_badge_key_helpers():
    assert dex_progress.badge_kind("regionShiny.hoenn") == "regionShiny"
    assert dex_progress.badge_region("regionShiny.hoenn") == "hoenn"
    assert dex_progress.badge_region("national") is None


# --- store integration -----------------------------------------------------


def _kanto_minus_one():
    return [
        DexEntry(base_id=sid, final_id=sid, chain_order=[sid], rarity=Rarity.COMMON)
        for sid in range(1, 151)
    ]


def _write_save(path, state):
    save.save(state, path)


def test_a_save_that_already_finished_kanto_is_seeded_silently(tmp_path):
    state = CompanionState()
    state.dex = _kanto_minus_one() + [
        DexEntry(base_id=151, final_id=151, chain_order=[151], rarity=Rarity.COMMON)
    ]
    path = tmp_path / "companion.json"
    _write_save(path, state)

    store = CompanionStore(save_path=path, rng=random.Random(1))
    assert store.state.dex_completions_seeded
    assert "region.kanto" in store.state.notified_dex_completions
    store._persist()
    assert store.take_dex_badges() == []


def test_completing_a_region_queues_one_notification(tmp_path):
    store = CompanionStore(save_path=tmp_path / "companion.json", rng=random.Random(1))
    store.state.dex.extend(_kanto_minus_one())
    store._persist()
    assert store.take_dex_badges() == []

    store.state.dex.append(
        DexEntry(base_id=151, final_id=151, chain_order=[151], rarity=Rarity.COMMON)
    )
    store._persist()
    assert store.take_dex_badges() == ["region.kanto"]
    # Announced once: later saves and a restart stay quiet.
    store._persist()
    assert store.take_dex_badges() == []
    reloaded = CompanionStore(save_path=tmp_path / "companion.json", rng=random.Random(1))
    reloaded._persist()
    assert reloaded.take_dex_badges() == []
    assert "region.kanto" in reloaded.state.notified_dex_completions


def test_adopting_a_legacy_save_does_not_announce_its_old_completions(tmp_path):
    store = CompanionStore(save_path=tmp_path / "companion.json", rng=random.Random(1))
    incoming = CompanionState()
    incoming.dex = _kanto_minus_one() + [
        DexEntry(base_id=151, final_id=151, chain_order=[151], rarity=Rarity.COMMON)
    ]
    assert not incoming.dex_completions_seeded
    store.adopt(incoming)
    assert store.take_dex_badges() == []
    assert "region.kanto" in store.state.notified_dex_completions


def test_save_round_trips_the_completion_fields(tmp_path):
    state = CompanionState()
    state.notified_dex_completions = {"region.kanto", "unown"}
    state.dex_completions_seeded = True
    path = tmp_path / "companion.json"
    save.save(state, path)
    loaded = save.load(path)
    assert loaded.notified_dex_completions == {"region.kanto", "unown"}
    assert loaded.dex_completions_seeded is True


def test_progress_payload_is_localised_and_lists_medals(tmp_path):
    store = CompanionStore(save_path=tmp_path / "companion.json", rng=random.Random(1))
    store.state.language = "de"
    store.state.dex.extend(_kanto_minus_one())
    store.state.dex.append(
        DexEntry(base_id=600, final_id=600, chain_order=[600], rarity=Rarity.COMMON,
                 is_shiny=True)
    )
    payload = store.dex_progress_payload()
    regions = {r["key"]: r for r in payload["regions"]}
    assert regions["kanto"]["owned"] == 150 and not regions["kanto"]["complete"]
    assert regions["unova"]["name"] == "Einall"
    assert regions["unova"]["shiny_owned"] == 1
    assert (regions["unova"]["first"], regions["unova"]["last"]) == (494, 649)
    assert payload["national"]["owned"] == 151
    assert payload["badges"] == []

    store.state.dex.append(
        DexEntry(base_id=151, final_id=151, chain_order=[151], rarity=Rarity.COMMON)
    )
    payload = store.dex_progress_payload()
    kanto = payload["regions"][0]
    assert kanto["complete"] and kanto["badge_label"] == "Kanto-Dex komplett"
    assert payload["badges"][0]["key"] == "region.kanto"


def test_notification_text_fills_in_the_region(tmp_path):
    store = CompanionStore(save_path=tmp_path / "companion.json", rng=random.Random(1))
    title, body = store.dex_notification("regionShiny.unova")
    assert title == "✨ Shiny Unova Dex complete"
    assert body == "You've caught a shiny of every Pokémon in Unova!"
    store.state.language = "fr"
    title, body = store.dex_notification("unown")
    assert title == "🔤 Collection Zarbi terminée"


def test_notifier_sends_one_per_badge():
    sent = []
    Notifier(lambda title, body="", urgency="normal": sent.append((title, body))).dex_completions(
        [("🏅 Kanto Dex complete", "body")]
    )
    assert sent == [("🏅 Kanto Dex complete", "body")]


def test_daemon_announces_a_completion_once(tmp_path):
    from poketokenbar.daemon import Daemon

    sent = []
    store = CompanionStore(save_path=tmp_path / "companion.json", rng=random.Random(1))
    store.state.claimed_today_tokens_by_provider = {}
    store.state.dex.extend(_kanto_minus_one())
    d = Daemon(
        state_path=tmp_path / "state.json",
        config_path=tmp_path / "config.json",
        cache=None,
        providers=[],
        companion_store=store,
        notifier=Notifier(lambda title, body="", urgency="normal": sent.append(title)),
    )
    d.spool = tmp_path / "spool"
    d.poll_once()
    store.state.dex.append(
        DexEntry(base_id=151, final_id=151, chain_order=[151], rarity=Rarity.COMMON)
    )
    payload = d.poll_once()
    d.poll_once()
    assert [t for t in sent if "Dex" in t] == ["🏅 Kanto Dex complete"]
    assert payload["dex_progress"]["regions"][0]["complete"] is True
