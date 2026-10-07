"""Per-Pokémon individual values — ports PokemonProfile.swift (upstream #264).

Every hatch rolls values that make it *that* Pokémon: six IVs now, and gender,
ability and an automatic moveset once its PokéAPI details are known. The
values persist in the save, so the same creature stays the same across
restarts, and they ride along into the Pokédex when it graduates or is
released.

The profile's level follows growth in *standard* balance units — independent
of difficulty and repeat boosts — and never goes down: a difficulty change or
an import cannot undo a level that was earned.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from . import balance
from .balance import Rarity

STAT_ORDER = ("hp", "attack", "defense", "special-attack", "special-defense", "speed")
SHEDINJA_SPECIES_ID = 292  # main-series rule: HP is always 1 (#422)
# Hatchable species stop at Gen V, so learnsets come from that generation.
VERSION_GROUP = "black-2-white-2"
MAX_TOKENS = 10**15

_MASK = (1 << 64) - 1

# nature -> (raised stat, lowered stat); neutral natures are absent.
NATURE_MODIFIERS: dict[str, tuple[str, str]] = {
    "lonely": ("attack", "defense"),
    "brave": ("attack", "speed"),
    "adamant": ("attack", "special-attack"),
    "naughty": ("attack", "special-defense"),
    "bold": ("defense", "attack"),
    "relaxed": ("defense", "speed"),
    "impish": ("defense", "special-attack"),
    "lax": ("defense", "special-defense"),
    "timid": ("speed", "attack"),
    "hasty": ("speed", "defense"),
    "jolly": ("speed", "special-attack"),
    "naive": ("speed", "special-defense"),
    "modest": ("special-attack", "attack"),
    "mild": ("special-attack", "defense"),
    "quiet": ("special-attack", "speed"),
    "rash": ("special-attack", "special-defense"),
    "calm": ("special-defense", "attack"),
    "gentle": ("special-defense", "defense"),
    "sassy": ("special-defense", "speed"),
    "careful": ("special-defense", "special-attack"),
}


def nature_modifier(nature: str | None, stat: str) -> float:
    pair = NATURE_MODIFIERS.get(nature or "")
    if pair is None:
        return 1.0
    if stat == pair[0]:
        return 1.1
    if stat == pair[1]:
        return 0.9
    return 1.0


class ProfileRNG:
    """SplitMix64, bit-for-bit with upstream's ProfileRNG so a seed means the
    same Pokémon in both apps."""

    def __init__(self, seed: int) -> None:
        self.state = (seed & _MASK) or 0x9E3779B97F4A7C15

    def next(self) -> int:
        self.state = (self.state + 0x9E3779B97F4A7C15) & _MASK
        z = self.state
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & _MASK
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & _MASK
        return z ^ (z >> 31)


def stable_seed(text: str) -> int:
    """FNV-1a 64 — stable across processes, for migrating pre-profile saves."""
    h = 0xCBF29CE484222325
    for byte in text.encode("utf-8"):
        h = ((h ^ byte) * 0x100000001B3) & _MASK
    return h


@dataclass(slots=True)
class Profile:
    instance_id: str
    seed: int
    ivs: dict[str, int]
    gender: str | None = None  # "male" | "female" | "genderless"
    ability_slot: int | None = None
    ability_name: str | None = None
    ability_hidden: bool = False
    level: int = 5
    growth_tokens: int = 0
    moves: list[dict] = field(default_factory=list)  # [{"name", "level"}]

    # --- growth --------------------------------------------------------------

    def advance_growth(self, candidate: int, rarity: Rarity) -> None:
        """Move toward `candidate` standard-balance tokens, never backwards."""
        self.growth_tokens = min(MAX_TOKENS, max(self.growth_tokens, max(0, candidate)))
        total = max(1, balance.graduation_total(rarity))
        progress = min(1.0, self.growth_tokens / total)
        self.level = min(100, max(self.level, 5 + int(progress * 95)))

    def rebase(self, old_rarity: Rarity, rarity: Rarity) -> None:
        """A revealed Ditto is a different species identity, not an evolution:
        keep the IVs, carry the growth fraction over, and reroll everything
        species-dependent once Ditto's own details are known."""
        fraction = min(1.0, self.growth_tokens / balance.graduation_total(old_rarity))
        self.gender = None
        self.ability_slot = None
        self.ability_name = None
        self.ability_hidden = False
        self.moves = []
        self.growth_tokens = int(fraction * balance.graduation_total(rarity))
        self.advance_growth(self.growth_tokens, rarity)

    # --- species-dependent fields --------------------------------------------

    def enrich(self, details: dict) -> None:
        """Fill gender / ability / moves from PokéAPI details. Values already
        rolled are kept; the moveset follows the current level."""
        rng = ProfileRNG(self.seed ^ 0xA11B1E5D9EED)
        if self.gender is None:
            rate = details.get("gender_rate", -1)
            if rate is None or rate < 0:
                self.gender = "genderless"
            else:
                self.gender = "female" if rng.next() % 8 < rate else "male"

        abilities = sorted(details.get("abilities", []), key=lambda a: a["slot"])
        normal = [a for a in abilities if not a["hidden"]]
        hidden = [a for a in abilities if a["hidden"]]
        if self.ability_slot is None:
            # Hidden abilities are deliberately rare (1/128).
            if hidden and rng.next() % 128 == 0:
                pick = hidden[rng.next() % len(hidden)]
            elif normal:
                pick = normal[rng.next() % len(normal)]
            else:
                pick = abilities[0] if abilities else None
            if pick is not None:
                self.ability_slot, self.ability_hidden = pick["slot"], pick["hidden"]
        chosen = next(
            (a for a in abilities
             if a["slot"] == self.ability_slot and a["hidden"] == self.ability_hidden),
            None,
        ) or next((a for a in abilities if a["slot"] == self.ability_slot), None) or (
            normal[0] if normal else (hidden[0] if hidden else None)
        )
        self.ability_name = chosen["name"] if chosen else None
        if chosen is not None:
            self.ability_slot, self.ability_hidden = chosen["slot"], chosen["hidden"]

        # Automatic moveset: the four most recently learned level-up moves.
        self.moves = level_up_moves(details, self.level)[-4:]

    def sanitize(self) -> None:
        self.level = min(100, max(5, self.level))
        self.growth_tokens = min(MAX_TOKENS, max(0, self.growth_tokens))
        self.ivs = {stat: min(31, max(0, int(self.ivs.get(stat, 0)))) for stat in STAT_ORDER}
        self.moves = [
            {"name": str(m.get("name", ""))[:80], "level": min(100, max(0, int(m.get("level", 0))))}
            for m in self.moves[:4]
            if isinstance(m, dict)
        ]
        if not self.instance_id:
            self.instance_id = str(uuid.uuid4())


def generate(seed: int, growth_tokens: int = 0, instance_id: str | None = None) -> Profile:
    rng = ProfileRNG(seed)
    return Profile(
        instance_id=instance_id or str(uuid.uuid4()),
        seed=seed & _MASK,
        ivs={stat: rng.next() % 32 for stat in STAT_ORDER},
        growth_tokens=max(0, growth_tokens),
    )


def level_up_moves(details: dict, level: int) -> list[dict]:
    best: dict[str, int] = {}
    for move in details.get("moves", []):
        levels = [
            m["level"] for m in move.get("learn", [])
            if m.get("method") == "level-up" and m.get("level", 0) <= level
        ]
        if levels:
            best[move["name"]] = min(levels)
    return [
        {"name": name, "level": lvl}
        for name, lvl in sorted(best.items(), key=lambda kv: (kv[1], kv[0]))
    ]


def stats(details: dict, profile: Profile, nature: str | None) -> list[dict]:
    """Main-series stat formula at the profile's level (no EVs)."""
    out = []
    base_stats = details.get("stats", {})
    for name in STAT_ORDER:
        base = base_stats.get(name)
        if base is None:
            continue
        iv = profile.ivs.get(name, 0)
        level = profile.level
        if name == "hp":
            if details.get("species_id") == SHEDINJA_SPECIES_ID:
                value = 1
            else:
                value = ((2 * base + iv) * level) // 100 + level + 10
        else:
            neutral = ((2 * base + iv) * level) // 100 + 5
            value = int(neutral * nature_modifier(nature, name))
        out.append({"name": name, "base": base, "iv": iv, "value": value})
    return out


def reconstructed_growth(rarity: Rarity, total_forms: int, completed_stages: int) -> int:
    """Standard-balance tokens for the stages already completed."""
    forms = max(1, total_forms)
    return sum(
        balance.phase_threshold(rarity, forms, i) for i in range(min(completed_stages, forms))
    )


def encode(p: Profile | None) -> dict | None:
    if p is None:
        return None
    return {
        "instance_id": p.instance_id,
        "seed": p.seed,
        "ivs": p.ivs,
        "gender": p.gender,
        "ability_slot": p.ability_slot,
        "ability_name": p.ability_name,
        "ability_hidden": p.ability_hidden,
        "level": p.level,
        "growth_tokens": p.growth_tokens,
        "moves": p.moves,
    }


def decode(raw) -> Profile | None:
    """Lenient: a damaged profile is dropped (and later regenerated), never
    allowed to take the save down with it."""
    if not isinstance(raw, dict):
        return None
    seed = raw.get("seed")
    ivs = raw.get("ivs")
    if isinstance(seed, bool) or not isinstance(seed, int) or not isinstance(ivs, dict):
        return None
    try:
        p = Profile(
            instance_id=str(raw.get("instance_id") or ""),
            seed=seed & _MASK,
            ivs={k: v for k, v in ivs.items() if isinstance(v, int) and not isinstance(v, bool)},
            gender=raw.get("gender") if raw.get("gender") in ("male", "female", "genderless") else None,
            ability_slot=raw.get("ability_slot") if isinstance(raw.get("ability_slot"), int) else None,
            ability_name=raw.get("ability_name") if isinstance(raw.get("ability_name"), str) else None,
            ability_hidden=raw.get("ability_hidden") is True,
            level=raw.get("level") if isinstance(raw.get("level"), int) else 5,
            growth_tokens=raw.get("growth_tokens") if isinstance(raw.get("growth_tokens"), int) else 0,
            moves=raw.get("moves") if isinstance(raw.get("moves"), list) else [],
        )
        p.sanitize()
    except (TypeError, ValueError):
        return None
    return p
