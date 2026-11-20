"""Per-generation Pokédex completion and cosmetic badges — ports PokedexRegion.swift (#442).

A generation is named by its home region ("region", not "generation", because
the companion code already uses generation for egg-hatch attempts). Membership
comes from the National Dex number alone, so placing a species never needs a
PokéAPI call.

Purely cosmetic: nothing here touches growth, shop prices or drop rates.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import balance

# (key, first, last) in National Dex order. Gen 1–5, matching pokeapi.MAX_SPECIES_ID.
REGIONS: tuple[tuple[str, int, int], ...] = (
    ("kanto", 1, 151),
    ("johto", 152, 251),
    ("hoenn", 252, 386),
    ("sinnoh", 387, 493),
    ("unova", 494, 649),
)
NATIONAL_FIRST = 1
NATIONAL_LAST = 649
NATIONAL_TOTAL = NATIONAL_LAST - NATIONAL_FIRST + 1

# Medal glyph per badge kind; region medals also show on their chip.
EMOJI = {
    "region": "🏅",
    "regionShiny": "✨",
    "unown": "🔤",
    "national": "🌍",
    "nationalShiny": "✨",
}


def region_for(dex_number: int) -> str | None:
    """The region a National Dex number belongs to, or None outside Gen 1–5."""
    for key, first, last in REGIONS:
        if first <= dex_number <= last:
            return key
    return None


def region_range(key: str) -> tuple[int, int] | None:
    for k, first, last in REGIONS:
        if k == key:
            return first, last
    return None


@dataclass(frozen=True)
class RegionCompletion:
    region: str
    owned: int
    shiny_owned: int
    total: int

    @property
    def is_complete(self) -> bool:
        return self.owned >= self.total

    @property
    def is_shiny_complete(self) -> bool:
        return self.shiny_owned >= self.total


def region_completions(owned: dict[int, bool]) -> list[RegionCompletion]:
    """Owned/total (and shiny-owned) per region, in National Dex order.

    `owned` maps species id -> whether any owned individual of it is shiny.
    """
    out = []
    for key, first, last in REGIONS:
        ids = [sid for sid in owned if first <= sid <= last]
        out.append(
            RegionCompletion(
                region=key,
                owned=len(ids),
                shiny_owned=sum(1 for sid in ids if owned[sid]),
                total=last - first + 1,
            )
        )
    return out


def national_progress(owned: dict[int, bool]) -> tuple[int, int, int]:
    """(owned, shiny_owned, total) across Gen 1–5."""
    ids = [sid for sid in owned if NATIONAL_FIRST <= sid <= NATIONAL_LAST]
    return len(ids), sum(1 for sid in ids if owned[sid]), NATIONAL_TOTAL


def earned_badges(owned: dict[int, bool], unown_forms_collected: int) -> list[str]:
    """Every badge key currently earned, in a stable order: regions, Unown, national.

    Keys are persisted in the "already notified" set, so they must never drift.
    """
    badges: list[str] = []
    for c in region_completions(owned):
        if c.is_complete:
            badges.append(f"region.{c.region}")
        if c.is_shiny_complete:
            badges.append(f"regionShiny.{c.region}")
    if unown_forms_collected >= len(balance.UNOWN_FORMS):
        badges.append("unown")
    n_owned, n_shiny, total = national_progress(owned)
    if n_owned >= total:
        badges.append("national")
    if n_shiny >= total:
        badges.append("nationalShiny")
    return badges


def newly_earned(earned: list[str], already_notified: set[str]) -> list[str]:
    """Badges earned but not yet announced, keeping the stable badge order."""
    return [b for b in earned if b not in already_notified]


def badge_kind(key: str) -> str:
    return key.split(".", 1)[0]


def badge_region(key: str) -> str | None:
    kind, _, region = key.partition(".")
    return region if kind in ("region", "regionShiny") and region else None
