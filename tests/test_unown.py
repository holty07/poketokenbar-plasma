"""Unown letter forms (upstream #288) and collection weighting."""

import json
import random
from collections import Counter

import pytest

from poketokenbar import balance, pokeapi, save, sprites
from poketokenbar.balance import Rarity
from poketokenbar.companion import (
    CompanionState,
    DexEntry,
    EvoLine,
    apply_usage,
    roll_unown_form,
)
from poketokenbar.companion_store import CompanionStore

UNOWN = balance.UNOWN_SPECIES_ID


def _unown_line():
    return EvoLine(base_id=UNOWN, path_ids=[UNOWN], rarity=Rarity.UNCOMMON)


def test_there_are_28_letters_with_readable_symbols():
    assert len(balance.UNOWN_FORMS) == 28
    assert balance.unown_symbol("b") == "B"
    assert balance.unown_symbol("exclamation") == "!"
    assert balance.unown_symbol("question") == "?"


def test_uncollected_letters_weigh_twice_collected_ones():
    collected = set(balance.UNOWN_FORMS[:14])
    rng = random.Random(1)
    counts = Counter(roll_unown_form(rng, collected) in collected for _ in range(28_000))
    # 14 letters at weight 1 vs 14 at weight 2 -> collected ~1/3 of rolls.
    assert 0.30 < counts[True] / 28_000 < 0.37


def test_unown_hatches_with_a_letter_and_others_do_not():
    s = CompanionState()
    apply_usage(s, balance.EGG_HATCH_THRESHOLD, line_for_egg=_unown_line(), rng=random.Random(2))
    assert s.active.unown_form in balance.UNOWN_FORMS
    t = CompanionState()
    line = EvoLine(base_id=1, path_ids=[1], rarity=Rarity.COMMON)
    apply_usage(t, balance.EGG_HATCH_THRESHOLD, line_for_egg=line, rng=random.Random(2))
    assert t.active.unown_form is None


def test_letter_survives_graduation_and_save_and_legacy_unown_is_a(tmp_path):
    s = CompanionState()
    apply_usage(s, balance.EGG_HATCH_THRESHOLD, line_for_egg=_unown_line(), rng=random.Random(2))
    letter = s.active.unown_form
    apply_usage(s, balance.graduation_total(Rarity.UNCOMMON))
    assert s.dex[0].unown_form == letter
    p = tmp_path / "c.json"
    save.save(s, p)
    assert save.load(p).dex[0].unown_form == letter
    p.write_text(json.dumps({"dex": [
        {"base_id": UNOWN, "final_id": UNOWN, "chain_order": [UNOWN]},
        {"base_id": 1, "final_id": 1, "chain_order": [1], "unown_form": "b"},
    ]}))
    loaded = save.load(p)
    assert loaded.dex[0].unown_form == "a"  # old Unown keeps its old look
    assert loaded.dex[1].unown_form is None  # a letter on another species is ignored


def test_sprite_names_and_cache_keys_keep_the_a_form_unchanged():
    assert sprites.sprite_url(UNOWN, True, False, "b").endswith("/animated/201-b.gif")
    assert sprites.sprite_url(UNOWN, False, True, "question").endswith("/shiny/201-question.png")
    assert sprites.sprite_url(UNOWN, False, False, "a").endswith("/201.png")
    assert sprites.cache_key(UNOWN, False, False, "a") == sprites.cache_key(UNOWN, False, False)
    assert sprites.cache_key(UNOWN, False, False, "b") != sprites.cache_key(UNOWN, False, False)


def _store_with_letters(tmp_path, letters, shiny=()):
    store = CompanionStore(save_path=tmp_path / "c.json")
    store.state = CompanionState()
    store.state.dex = [
        DexEntry(base_id=UNOWN, final_id=UNOWN, chain_order=[UNOWN], rarity=Rarity.UNCOMMON,
                 unown_form=f, is_shiny=f in shiny)
        for f in letters
    ]
    return store


def test_pokedex_shows_one_unown_cell_counting_letters(tmp_path):
    store = _store_with_letters(tmp_path, ["a", "b", "b", "question"], shiny=("b",))
    [row] = store.dex_payload()
    assert row["unown_count"] == 3 and row["unown_total"] == 28
    by_form = {f["form"]: f for f in row["unown_forms"]}
    assert by_form["b"]["collected"] and by_form["b"]["is_shiny"]
    assert not by_form["c"]["collected"]


def test_pinning_an_unown_letter_requires_owning_that_letter(tmp_path):
    store = _store_with_letters(tmp_path, ["a", "b"])
    with pytest.raises(ValueError):
        store.set_representative(UNOWN, "z")
    store.set_representative(UNOWN, "b")
    assert store.state.representative_unown_form == "b"
    [row] = store.dex_payload()
    assert next(f for f in row["unown_forms"] if f["form"] == "b")["is_representative"]


def test_species_roll_halves_the_weight_of_completed_lines(tmp_path):
    api = pokeapi.PokeAPI(cache_dir=tmp_path)
    api._index_file.write_text(json.dumps([{"id": 1, "capture_rate": 100},
                                           {"id": 4, "capture_rate": 100}]))
    rng = random.Random(3)
    picks = Counter(api.roll_base_species(rng, collected_bases={1}) for _ in range(9000))
    # weights 50 vs 100 -> line 1 about a third of the time
    assert 0.30 < picks[1] / 9000 < 0.37


def test_pinning_unown_without_a_letter_uses_one_that_is_owned(tmp_path):
    store = _store_with_letters(tmp_path, ["question"])
    store.set_representative(UNOWN)
    assert store.state.representative_unown_form == "question"
