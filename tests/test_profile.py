"""Mirrors upstream PokemonProfileTests.swift — same seeds, same expected
values — so a seed means the same Pokémon in both apps."""

import json
import random

from poketokenbar import balance, companion, pokeapi, profile, save, snapshots
from poketokenbar.balance import Rarity, graduation_total
from poketokenbar.companion import CompanionState, EvoLine, apply_usage

SLOWPOKE = {
    "species_id": 79,
    "gender_rate": 8,
    "types": ["water", "psychic"],
    "stats": {"hp": 90, "attack": 65, "defense": 65,
              "special-attack": 40, "special-defense": 40, "speed": 15},
    "abilities": [{"name": "oblivious", "slot": 1, "hidden": False}],
    "moves": [
        {"name": "tackle", "learn": [{"method": "level-up", "level": 1}]},
        {"name": "curse", "learn": [{"method": "level-up", "level": 1}]},
        {"name": "yawn", "learn": [{"method": "level-up", "level": 1}]},
        {"name": "growl", "learn": [{"method": "level-up", "level": 5}]},
        {"name": "water-gun", "learn": [{"method": "level-up", "level": 9}]},
        {"name": "surf", "learn": [{"method": "machine", "level": 0}]},
    ],
}


def _details(gender_rate, abilities):
    return {"species_id": 999, "gender_rate": gender_rate, "stats": {"hp": 50},
            "abilities": abilities, "moves": []}


def _ab(name, slot, hidden):
    return {"name": name, "slot": slot, "hidden": hidden}


def test_generation_is_deterministic_and_ivs_stay_in_range():
    a = profile.generate(42, instance_id="same")
    assert a == profile.generate(42, instance_id="same")
    assert all(0 <= a.ivs[s] <= 31 for s in profile.STAT_ORDER)


def test_enrichment_rolls_persistent_identity_and_four_legal_moves():
    p = profile.generate(7, instance_id="p")
    p.level = 9
    p.enrich(SLOWPOKE)
    assert p.gender == "female"
    assert (p.ability_name, p.ability_slot) == ("oblivious", 1)
    names = [m["name"] for m in p.moves]
    assert len(names) == 4 and "water-gun" in names and "surf" not in names
    before = (p.gender, p.ability_name, list(p.moves))
    p.enrich(SLOWPOKE)
    assert (p.gender, p.ability_name, p.moves) == before  # never rerolls


def test_genderless_and_male_branches():
    g = profile.generate(1, instance_id="g")
    g.enrich(_details(-1, []))
    assert g.gender == "genderless"
    m = profile.generate(1, instance_id="m")
    m.enrich(_details(0, []))
    assert m.gender == "male"


def test_hidden_ability_roll_matches_upstream_seed_115():
    p = profile.generate(115, instance_id="hidden-roll")
    p.enrich(_details(-1, [_ab("hidden-one", 3, True), _ab("hidden-two", 4, True)]))
    assert (p.ability_name, p.ability_slot, p.ability_hidden) == ("hidden-two", 4, True)


def test_all_hidden_abilities_fall_back_when_the_rare_roll_misses():
    p = profile.generate(1, instance_id="hidden-fallback")
    p.enrich(_details(-1, [_ab("first-hidden", 2, True), _ab("second-hidden", 3, True)]))
    assert (p.ability_name, p.ability_slot, p.ability_hidden) == ("first-hidden", 2, True)


def test_ability_fallback_chain_repairs_persisted_selections():
    normal, hidden = _ab("normal", 1, False), _ab("hidden", 3, True)
    p = profile.generate(2, instance_id="slot-only")
    p.ability_slot, p.ability_hidden = 1, True
    p.enrich(_details(-1, [normal]))
    assert (p.ability_name, p.ability_hidden) == ("normal", False)
    p = profile.generate(3, instance_id="normal-fallback")
    p.ability_slot, p.ability_hidden = 99, True
    p.enrich(_details(-1, [hidden, normal]))
    assert (p.ability_name, p.ability_hidden) == ("normal", False)
    p = profile.generate(4, instance_id="hidden-chain")
    p.ability_slot = 99
    p.enrich(_details(-1, [hidden]))
    assert (p.ability_name, p.ability_hidden) == ("hidden", True)


def test_nature_raises_and_lowers_exactly_one_stat_each():
    neutral = profile.generate(1, instance_id="n")
    neutral.level = 50
    base = {s["name"]: s["value"] for s in profile.stats(SLOWPOKE, neutral, "hardy")}
    modest = {s["name"]: s["value"] for s in profile.stats(SLOWPOKE, neutral, "modest")}
    assert modest["special-attack"] == int(base["special-attack"] * 1.1)
    assert modest["attack"] == int(base["attack"] * 0.9)
    for nature, (up, down) in profile.NATURE_MODIFIERS.items():
        assert up != down, nature
    assert len(profile.NATURE_MODIFIERS) == 20  # + 5 neutral natures = 25


def test_shedinja_always_has_one_hp():
    shedinja = dict(SLOWPOKE, species_id=profile.SHEDINJA_SPECIES_ID)
    for level in (5, 50, 100):
        p = profile.generate(3, instance_id="shedinja")
        p.level = level
        hp = next(s for s in profile.stats(shedinja, p, None) if s["name"] == "hp")
        assert hp["value"] == 1


def test_growth_maps_hatch_to_five_and_graduation_to_hundred_and_never_drops():
    p = profile.generate(1, instance_id="growth")
    assert p.level == 5
    p.advance_growth(graduation_total(Rarity.COMMON), Rarity.COMMON)
    assert p.level == 100
    p.advance_growth(0, Rarity.COMMON)
    assert p.level == 100


def test_rebase_keeps_ivs_and_clears_species_fields():
    p = profile.generate(7, instance_id="ditto")
    p.enrich(SLOWPOKE)
    ivs = dict(p.ivs)
    p.advance_growth(graduation_total(Rarity.COMMON) // 2, Rarity.COMMON)
    p.rebase(Rarity.COMMON, Rarity.UNCOMMON)
    assert p.ivs == ivs and p.instance_id == "ditto"
    assert p.gender is None and p.ability_name is None and p.moves == []
    assert p.growth_tokens == graduation_total(Rarity.UNCOMMON) // 2


def test_decode_clamps_hostile_values_and_drops_garbage():
    raw = profile.encode(profile.generate(5, instance_id="x"))
    raw.update(level=999, growth_tokens=-5, ivs={"hp": 99, "attack": -3},
               moves=[{"name": "a" * 200, "level": 500}] * 9)
    p = profile.decode(raw)
    assert p.level == 100 and p.growth_tokens == 0
    assert p.ivs["hp"] == 31 and p.ivs["attack"] == 0 and p.ivs["speed"] == 0
    assert len(p.moves) == 4 and len(p.moves[0]["name"]) == 80 and p.moves[0]["level"] == 100
    assert profile.decode("nope") is None
    assert profile.decode({"seed": True, "ivs": {}}) is None


# --- integration with the game -------------------------------------------



def _hatch(state=None):
    s = state or CompanionState()
    line = EvoLine(base_id=79, path_ids=[79, 80], rarity=Rarity.COMMON)
    apply_usage(s, balance.EGG_HATCH_THRESHOLD, line_for_egg=line, rng=random.Random(5))
    return s


def test_hatch_rolls_a_profile_and_growth_raises_its_level():
    s = _hatch()
    p = s.active.profile
    assert p is not None and p.level == 5
    apply_usage(s, s.stage_threshold(s.active))  # evolves: half the line done
    assert s.active.stage_index == 1
    assert 5 < p.level < 100


def test_graduation_and_release_carry_the_individual_into_the_dex():
    s = _hatch()
    p = s.active.profile
    apply_usage(s, balance.graduation_total(Rarity.COMMON))
    assert s.dex[-1].profile is p and p.level == 100
    s = _hatch()
    p = s.active.profile
    companion.release(s)
    assert s.dex[-1].profile is p


def test_difficulty_does_not_change_the_level_earned():
    easy, normal = _hatch(), _hatch()
    easy.growth_difficulty = 0.5
    apply_usage(easy, easy.stage_threshold(easy.active) // 2)
    apply_usage(normal, normal.stage_threshold(normal.active) // 2)
    assert easy.active.profile.level == normal.active.profile.level


def test_old_saves_migrate_once_deterministically_with_a_snapshot(tmp_path):
    from poketokenbar.companion_store import CompanionStore

    path = tmp_path / "companion.json"
    path.write_text(json.dumps({
        "active": {"base_id": 1, "path_ids": [1, 2], "stage_index": 0, "rarity": "common",
                   "total_forms": 2, "hatched_at": 10.0},
        "dex": [{"base_id": 4, "final_id": 6, "chain_order": [4, 5, 6], "rarity": "rare"}],
    }))
    store = CompanionStore(save_path=path)
    assert store.state.active.profile is not None
    assert store.state.dex[0].profile.level == 100  # graduated → full growth
    ivs = store.state.active.profile.ivs
    assert len(snapshots.list_snapshots(path)) == 1  # pre-migration copy
    # Running the migration on the same old data yields the same Pokémon.
    again = save.decode(json.loads(snapshots.list_snapshots(path)[0].path.read_text())["save"])
    companion.ensure_profiles(again)
    assert again.active.profile.ivs == ivs


class DetailsAPI:
    def __init__(self):
        self.calls = 0

    def details_cached(self, species_id):
        return None

    def details(self, species_id):
        self.calls += 1
        return dict(SLOWPOKE, species_id=species_id)

    def species(self, species_id):
        return {"names": []}


def test_store_enriches_profiles_within_a_network_budget(tmp_path):
    from poketokenbar.companion_store import CompanionStore

    store = CompanionStore(save_path=tmp_path / "c.json", api=DetailsAPI(), rng=random.Random(1))
    store.state = _hatch()
    for i in range(5):
        store.state.dex.append(companion.DexEntry(
            base_id=i, final_id=i, chain_order=[i], rarity=Rarity.COMMON,
            profile=profile.generate(i)))
    store.enrich_profiles()
    assert store.api.calls == store.DETAIL_FETCH_BUDGET
    assert store.state.active.profile.ability_name == "oblivious"


def test_detail_normalization_keeps_only_the_gen_five_learnset():
    mon = {
        "name": "slowpoke", "height": 12, "weight": 360,
        "types": [{"slot": 2, "type": {"name": "psychic"}}, {"slot": 1, "type": {"name": "water"}}],
        "stats": [{"stat": {"name": "hp"}, "base_stat": 90}],
        "abilities": [{"ability": {"name": "regenerator"}, "slot": 3, "is_hidden": True},
                      {"ability": {"name": "oblivious"}, "slot": 1, "is_hidden": False}],
        "moves": [
            {"move": {"name": "tackle"}, "version_group_details": [
                {"version_group": {"name": "black-2-white-2"},
                 "move_learn_method": {"name": "level-up"}, "level_learned_at": 1},
                {"version_group": {"name": "scarlet-violet"},
                 "move_learn_method": {"name": "level-up"}, "level_learned_at": 1}]},
            {"move": {"name": "chilling-water"}, "version_group_details": [
                {"version_group": {"name": "scarlet-violet"},
                 "move_learn_method": {"name": "machine"}, "level_learned_at": 0}]},
        ],
    }
    d = pokeapi.normalize_details(79, mon, {"gender_rate": 4})
    assert d["types"] == ["water", "psychic"]
    assert [a["name"] for a in d["abilities"]] == ["oblivious", "regenerator"]
    assert d["moves"] == [{"name": "tackle", "learn": [{"method": "level-up", "level": 1}]}]
    assert d["gender_rate"] == 4
