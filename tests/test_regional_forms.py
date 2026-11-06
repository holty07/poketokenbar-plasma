"""Regional forms (Alolan, Galarian, Hisuian, Paldean)."""

import random
from collections import Counter
from pathlib import Path

from poketokenbar import balance, l10n, regional_forms, save
from poketokenbar.balance import Rarity
from poketokenbar.companion import CompanionState, DexEntry, EvoLine, MonState, apply_usage
from poketokenbar.companion_store import CompanionStore

MEOWTH, PERSIAN = 52, 53
PICHU, PIKACHU, RAICHU = 172, 25, 26
SNEASEL, WEAVILE = 215, 461
WOOPER, QUAGSIRE = 194, 195


def test_every_form_is_a_known_region_on_a_gen_1_to_5_species():
    for (species_id, region), pokemon_id in regional_forms.FORM_POKEMON_IDS.items():
        assert region in regional_forms.REGIONS
        assert 1 <= species_id <= 649
        assert pokemon_id > 10000


def test_valid_regions_follow_the_real_evolutions():
    v = regional_forms.valid_regions
    # Alolan Meowth becomes Alolan Persian; Galarian Meowth only becomes Perrserker.
    assert v([MEOWTH, PERSIAN]) == ["alola"]
    assert v([MEOWTH]) == ["alola", "galar"]
    # The form may first appear at a later stage.
    assert v([PICHU, PIKACHU, RAICHU]) == ["alola"]
    # Hisuian Sneasel and Paldean Wooper evolve into species above #649.
    assert v([SNEASEL, WEAVILE]) == []
    assert v([WOOPER, QUAGSIRE]) == []
    assert v([1, 2, 3]) == []


def test_resolved_drops_unknown_or_impossible_regions():
    assert regional_forms.resolved([MEOWTH, PERSIAN], "alola") == "alola"
    assert regional_forms.resolved([MEOWTH, PERSIAN], "galar") is None
    assert regional_forms.resolved([1], "alola") is None
    assert regional_forms.resolved([MEOWTH], 7) is None


def test_roll_is_standard_or_a_valid_region_evenly():
    rng = random.Random(4)
    counts = Counter(regional_forms.roll(rng, [MEOWTH]) for _ in range(9000))
    assert set(counts) == {None, "alola", "galar"}
    for n in counts.values():
        assert 2700 < n < 3300


def test_the_form_roll_leaves_shiny_nature_and_stats_unchanged():
    """The form is rolled after everything else, so the same seed gives the
    same individual whether or not its line has regional forms."""
    plain = EvoLine(base_id=1, path_ids=[1], rarity=Rarity.COMMON)
    formed = EvoLine(base_id=MEOWTH, path_ids=[MEOWTH], rarity=Rarity.COMMON)
    for seed in range(20):
        a, b = CompanionState(), CompanionState()
        apply_usage(a, balance.EGG_HATCH_THRESHOLD, line_for_egg=plain, rng=random.Random(seed))
        apply_usage(b, balance.EGG_HATCH_THRESHOLD, line_for_egg=formed, rng=random.Random(seed))
        assert a.active.regional_form is None
        assert (a.active.is_shiny, a.active.nature) == (b.active.is_shiny, b.active.nature)
        b.active.profile.instance_id = a.active.profile.instance_id  # a fresh uuid, not seeded
        assert a.active.profile == b.active.profile


def test_form_hatches_survives_graduation_and_save(tmp_path):
    line = EvoLine(base_id=MEOWTH, path_ids=[MEOWTH, PERSIAN], rarity=Rarity.COMMON)
    seen = set()
    for seed in range(40):
        s = CompanionState()
        apply_usage(s, balance.EGG_HATCH_THRESHOLD, line_for_egg=line, rng=random.Random(seed))
        seen.add(s.active.regional_form)
    assert seen == {None, "alola"}

    s = CompanionState()
    apply_usage(s, balance.EGG_HATCH_THRESHOLD, line_for_egg=line, rng=random.Random(0))
    s.active.regional_form = "alola"
    p = tmp_path / "c.json"
    save.save(s, p)
    assert save.load(p).active.regional_form == "alola"
    apply_usage(s, 10**12)
    assert s.dex[0].regional_form == "alola"
    save.save(s, p)
    assert save.load(p).dex[0].regional_form == "alola"


def test_old_and_tampered_saves_load_as_standard(tmp_path):
    p = tmp_path / "c.json"
    s = CompanionState()
    s.dex = [
        DexEntry(base_id=MEOWTH, final_id=PERSIAN, chain_order=[MEOWTH, PERSIAN],
                 rarity=Rarity.COMMON, regional_form="galar"),
        DexEntry(base_id=1, final_id=1, chain_order=[1], rarity=Rarity.COMMON),
    ]
    save.save(s, p)
    loaded = save.load(p)
    assert loaded.dex[0].regional_form is None  # Galarian Persian does not exist
    assert loaded.dex[1].regional_form is None  # key absent / no forms


class _FakeAPI:
    NAMES = {MEOWTH: "Meowth", PERSIAN: "Persian", PICHU: "Pichu", PIKACHU: "Pikachu",
             RAICHU: "Raichu", balance.DITTO_SPECIES_ID: "Ditto"}

    def species(self, species_id):
        return {"names": [{"language": {"name": "en"}, "name": self.NAMES[species_id]},
                          {"language": {"name": "de"}, "name": self.NAMES[species_id]}]}


class _FakeSprites:
    def __init__(self):
        self.calls = []

    def path(self, species_id, animated=True, shiny=False, form=None):
        self.calls.append((species_id, animated, shiny, form))
        return Path(f"/sprites/{species_id}{'-sh' if shiny else ''}.png")


def _store(tmp_path, mon):
    store = CompanionStore(save_path=tmp_path / "c.json", api=_FakeAPI(),
                           sprite_store=_FakeSprites())
    store.state = CompanionState()
    store.state.active = mon
    return store


def _mon(path, stage, region, **kw):
    return MonState(base_id=path[0], path_ids=list(path), planned_path_ids=list(path),
                    stage_index=stage, used_at_stage=0, rarity=Rarity.COMMON,
                    total_forms=len(path), regional_form=region, **kw)


def test_names_and_sprites_use_the_form_only_on_stages_that_have_it(tmp_path):
    store = _store(tmp_path, _mon([PICHU, PIKACHU, RAICHU], 2, "alola", is_shiny=True))
    assert store.display_name(RAICHU, region="alola") == "Alolan Raichu"
    assert store.display_name(PIKACHU, region="alola") == "Pikachu"
    assert store.sprite_path() == "/sprites/10100-sh.png"
    chain = store.catch_log_payload()[0]["chain"]
    assert [c["name"] for c in chain] == ["Pichu", "Pikachu", "Alolan Raichu"]
    assert chain[2]["sprite_path"] == "/sprites/10100-sh.png"
    assert chain[0]["sprite_path"] == f"/sprites/{PICHU}-sh.png"


def test_names_are_localized(tmp_path):
    store = _store(tmp_path, _mon([MEOWTH], 0, "galar"))
    store.state.language = "de"
    assert store.display_name(MEOWTH, region="galar") == "Galar-Meowth"
    for region in regional_forms.REGIONS:
        for lang in l10n.LANGUAGES:
            assert "%1" in l10n.t(f"form_{region}", lang)


def test_graduated_entry_keeps_its_form_in_the_catch_log(tmp_path):
    store = _store(tmp_path, None)
    store.state.dex = [DexEntry(base_id=MEOWTH, final_id=PERSIAN, chain_order=[MEOWTH, PERSIAN],
                                rarity=Rarity.COMMON, regional_form="alola")]
    chain = store.catch_log_payload()[0]["chain"]
    assert [c["name"] for c in chain] == ["Alolan Meowth", "Alolan Persian"]
    assert [c["sprite_path"] for c in chain] == ["/sprites/10107.png", "/sprites/10108.png"]


def test_revealed_ditto_shows_plain_ditto(tmp_path):
    mon = _mon([MEOWTH, PERSIAN], 1, "alola", ditto_disguise=MEOWTH, ditto_revealed=True)
    store = _store(tmp_path, mon)
    assert store.sprite_path() == f"/sprites/{balance.DITTO_SPECIES_ID}.png"
