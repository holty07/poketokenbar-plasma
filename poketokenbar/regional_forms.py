"""Regional forms (Alolan, Galarian, Hisuian, Paldean).

A regional form belongs to an individual companion, not to a species: the same
Meowth line can hatch standard or Alolan. Each species that has a form maps to
the PokeAPI *pokemon* id of that variety (e.g. meowth-alola = 10107), which is
also the file name of its sprite.

The table is generated from PokeAPI's `pokemon-species/{id}.varieties` for
species 1-649 (the game's MAX_SPECIES_ID), keeping only true regional forms:
totem Raticate, cap Pikachu and Zen Mode Darmanitan are left out, and Paldean
Tauros uses its Combat Breed. Every id has a normal and a shiny sprite.

Region-only evolutions (Perrserker, Sirfetch'd, Obstagoon, Sneasler, Clodsire…)
are all above #649, so lines never reach them; `valid_regions` instead rejects
a form whose next stage could not follow it (Galarian Meowth never becomes
Persian).
"""

from __future__ import annotations

import random
from collections.abc import Sequence

REGIONS = ("alola", "galar", "hisui", "paldea")

# (species id, region) -> PokeAPI pokemon id of the regional variety.
FORM_POKEMON_IDS: dict[tuple[int, str], int] = {
    (19, "alola"): 10091,  # Rattata
    (20, "alola"): 10092,  # Raticate
    (26, "alola"): 10100,  # Raichu
    (27, "alola"): 10101,  # Sandshrew
    (28, "alola"): 10102,  # Sandslash
    (37, "alola"): 10103,  # Vulpix
    (38, "alola"): 10104,  # Ninetales
    (50, "alola"): 10105,  # Diglett
    (51, "alola"): 10106,  # Dugtrio
    (52, "alola"): 10107,  # Meowth
    (52, "galar"): 10161,  # Meowth
    (53, "alola"): 10108,  # Persian
    (58, "hisui"): 10229,  # Growlithe
    (59, "hisui"): 10230,  # Arcanine
    (74, "alola"): 10109,  # Geodude
    (75, "alola"): 10110,  # Graveler
    (76, "alola"): 10111,  # Golem
    (77, "galar"): 10162,  # Ponyta
    (78, "galar"): 10163,  # Rapidash
    (79, "galar"): 10164,  # Slowpoke
    (80, "galar"): 10165,  # Slowbro
    (83, "galar"): 10166,  # Farfetch'd
    (88, "alola"): 10112,  # Grimer
    (89, "alola"): 10113,  # Muk
    (100, "hisui"): 10231,  # Voltorb
    (101, "hisui"): 10232,  # Electrode
    (103, "alola"): 10114,  # Exeggutor
    (105, "alola"): 10115,  # Marowak
    (110, "galar"): 10167,  # Weezing
    (122, "galar"): 10168,  # Mr. Mime
    (128, "paldea"): 10250,  # Tauros (Combat Breed)
    (144, "galar"): 10169,  # Articuno
    (145, "galar"): 10170,  # Zapdos
    (146, "galar"): 10171,  # Moltres
    (157, "hisui"): 10233,  # Typhlosion
    (194, "paldea"): 10253,  # Wooper
    (199, "galar"): 10172,  # Slowking
    (211, "hisui"): 10234,  # Qwilfish
    (215, "hisui"): 10235,  # Sneasel
    (222, "galar"): 10173,  # Corsola
    (263, "galar"): 10174,  # Zigzagoon
    (264, "galar"): 10175,  # Linoone
    (503, "hisui"): 10236,  # Samurott
    (549, "hisui"): 10237,  # Lilligant
    (554, "galar"): 10176,  # Darumaka
    (555, "galar"): 10177,  # Darmanitan (Standard Mode)
    (562, "galar"): 10179,  # Yamask
    (570, "hisui"): 10238,  # Zorua
    (571, "hisui"): 10239,  # Zoroark
    (618, "galar"): 10180,  # Stunfisk
    (628, "hisui"): 10240,  # Braviary
}


def form_pokemon_id(species_id: int, region: str | None) -> int | None:
    """The variety's pokemon id when this species has that region's form."""
    if region is None:
        return None
    return FORM_POKEMON_IDS.get((species_id, region))


def valid_regions(path_ids: Sequence[int]) -> list[str]:
    """Regions an individual on this evolution path can belong to.

    A region qualifies when some stage has its form and, once a stage has it,
    every later stage does too. Earlier stages may lack it (Pichu -> Pikachu ->
    Alolan Raichu), but a form never drops back to standard on evolving.
    """
    out = []
    for region in REGIONS:
        has = [(sid, region) in FORM_POKEMON_IDS for sid in path_ids]
        if not any(has):
            continue
        first = has.index(True)
        if all(has[first:]):
            out.append(region)
    return out


def resolved(path_ids: Sequence[int], region) -> str | None:
    """A stored region when it is valid for the path, else None (standard)."""
    if isinstance(region, str) and region in valid_regions(path_ids):
        return region
    return None


def roll(rng: random.Random, path_ids: Sequence[int]) -> str | None:
    """Standard or one of the path's regions, each equally likely.

    Lines without regional forms consume no randomness, so their hatches roll
    exactly as before this feature existed.
    """
    regions = valid_regions(path_ids)
    if not regions:
        return None
    pick = rng.randrange(len(regions) + 1)
    return None if pick == len(regions) else regions[pick]
