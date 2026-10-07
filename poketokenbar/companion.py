"""Companion state and growth — ports CompanionModel/CompanionStore.swift.

Pure functions over CompanionState with no I/O, so the whole game is testable
without a network or a filesystem. Species data arrives through an injected
line provider; persistence lives in save.py.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from . import balance
from . import profile as _profile
from .balance import Rarity


@dataclass(slots=True)
class EvoLine:
    """One evolution line: ordered species ids from base to final."""

    base_id: int
    path_ids: list[int]
    rarity: Rarity
    names: dict[int, dict[str, str]] = field(default_factory=dict)

    @property
    def total_forms(self) -> int:
        return len(self.path_ids)


@dataclass(slots=True)
class MonState:
    base_id: int
    path_ids: list[int]
    planned_path_ids: list[int]
    stage_index: int = 0
    used_at_stage: int = 0
    rarity: Rarity = Rarity.COMMON
    total_forms: int = 1
    is_shiny: bool = False
    nature: str | None = None
    # Repeat hatch of a line already graduated: grows REPEAT_GROWTH_MULTIPLIER
    # times faster (#254).
    has_growth_boost: bool = False
    ditto_disguise: int | None = None
    ditto_revealed: bool = False
    hatched_at: float | None = None
    # Individual values (#264); None only on saves migrated lazily.
    profile: _profile.Profile | None = None
    # Unown's letter (#288); None for every other species.
    unown_form: str | None = None

    @property
    def current_id(self) -> int:
        """Species currently displayed.

        Falls back to base_id when path_ids is empty so a damaged save cannot
        crash rendering, which happens on every frame.
        """
        if self.ditto_revealed:
            return balance.DITTO_SPECIES_ID
        if not self.path_ids:
            return self.base_id
        return self.path_ids[min(self.stage_index, len(self.path_ids) - 1)]

    @property
    def is_final_form(self) -> bool:
        return self.stage_index >= len(self.path_ids) - 1

    @property
    def shows_shiny(self) -> bool:
        """Shininess as the player may see it. A disguised Ditto keeps its
        secret — shiny included — until the reveal (#420)."""
        return self.is_shiny and (self.ditto_disguise is None or self.ditto_revealed)

    @property
    def phase_threshold(self) -> int:
        """Default-difficulty threshold for the current stage."""
        return balance.phase_threshold(
            self.rarity,
            self.total_forms,
            self.stage_index,
            balance.REPEAT_GROWTH_MULTIPLIER if self.has_growth_boost else 1,
        )


@dataclass(slots=True)
class DexEntry:
    base_id: int
    final_id: int
    chain_order: list[int]
    rarity: Rarity
    is_shiny: bool = False
    nature: str | None = None
    # Epoch seconds. None on entries written before this was tracked; those
    # sort last rather than pretending to be ancient.
    caught_at: float | None = None
    raised_seconds: float | None = None
    # Set when the companion was released by buying an egg rather than
    # graduated (#242). None means graduated, so old saves need no migration.
    released_at: float | None = None
    profile: _profile.Profile | None = None
    unown_form: str | None = None

    @property
    def released(self) -> bool:
        return self.released_at is not None


@dataclass(slots=True)
class CompanionState:
    # Tokens are only counted from install onward.
    install_baseline_set: bool = False
    used_since_install: int = 0
    # Ledger of tokens spent in the shop. Spendable = used_since_install
    # - spent_tokens. The growth meter (used_since_install) never rewinds.
    spent_tokens: int = 0
    # Tokens absorbed by the current egg; resets per egg.
    egg_usage: int = 0
    # Rarity floor a premium egg guarantees. Persisted because the species roll
    # needs the network, which may be unavailable at purchase time.
    egg_tier: Rarity | None = None
    pending_hatch_id: int | None = None
    claimed_today_tokens_by_provider: dict[str, int] | None = None
    last_date: str = ""
    active: MonState | None = None
    dex: list[DexEntry] = field(default_factory=list)
    collected_finals: set[str] = field(default_factory=set)
    language: str = "en"
    inventory: dict[str, int] = field(default_factory=dict)
    candy_grant_tier: dict[str, int] = field(default_factory=dict)
    # Window key -> last seen resets_at. A new epoch rearms the grant even if
    # the dip below 100% was never observed (asleep across the reset, #334).
    candy_window_epoch: dict[str, str] = field(default_factory=dict)
    candy_feature_seeded: bool = False
    # Difficulty actually applied to this save. Kept here, not only in config,
    # so a change can rescale banked progress exactly once.
    growth_difficulty: float = balance.DEFAULT_DIFFICULTY
    shop_difficulty: float = balance.DEFAULT_DIFFICULTY
    # Species pinned as the panel/pet representative (#158). None = current.
    representative_id: int | None = None
    representative_unown_form: str | None = None
    # Which appearance to show when both are owned (#345). None = shiny if
    # owned shiny (the old behaviour).
    representative_shiny: bool | None = None

    @property
    def spendable_tokens(self) -> int:
        return max(0, self.used_since_install - self.spent_tokens)

    def has_collected_final(self, base_id: int) -> bool:
        """Whether any line starting at base_id was graduated (keys are
        "<base>-<final>")."""
        prefix = f"{base_id}-"
        return any(key.startswith(prefix) for key in self.collected_finals)

    def collected_unown_forms(self) -> set[str]:
        """Letters owned — graduated, released or reached by the companion;
        normal and shiny of a letter count once."""
        forms = {
            balance.resolved_unown_form(balance.UNOWN_SPECIES_ID, e.unown_form)
            for e in self.dex
            if balance.UNOWN_SPECIES_ID in e.chain_order
        }
        mon = self.active
        if mon is not None and balance.UNOWN_SPECIES_ID in mon.path_ids[: mon.stage_index + 1]:
            forms.add(balance.resolved_unown_form(balance.UNOWN_SPECIES_ID, mon.unown_form))
        return forms

    def egg_threshold(self) -> int:
        return balance.scaled(balance.EGG_HATCH_THRESHOLD, self.growth_difficulty)

    def stage_threshold(self, mon: MonState) -> int:
        """Difficulty-scaled threshold. Never call balance.phase_threshold
        directly for growth — a caller that forgets the multiplier would
        silently fall back to default difficulty."""
        return max(1, balance.scaled(mon.phase_threshold, self.growth_difficulty))


@dataclass(slots=True)
class GrowthEvents:
    """What happened during one apply_usage call, for notifications."""

    hatched: int | None = None
    evolved_to: int | None = None
    graduated: DexEntry | None = None
    ditto_revealed: bool = False


def display_state(
    state: CompanionState,
    today_tokens: int,
    limit_warning: bool = False,
    just_evolved: bool = False,
) -> str:
    """Which mood the companion is in — ports computeState().

    Order matters: a level-up beats a limit warning, which beats sleep. Any
    other ordering hides the celebration behind a warning.
    """
    if state.active is None:
        return "egg"
    if just_evolved:
        return "levelUp"
    if limit_warning:
        return "tired"
    if today_tokens <= 0:
        return "sleep"
    # Burn tiers, in tokens/day equivalents.
    if today_tokens >= 150_000_000:
        return "focus"
    if today_tokens >= 20_000_000:
        return "working"
    return "idle"


STATUS_MESSAGE = {
    "egg": "An egg is warming up.",
    "idle": "Keeping quiet today.",
    "working": "Today's work is piling up.",
    "focus": "In focus mode now.",
    "tired": "Careful — the limit is close.",
    "sleep": "Sleeping now.",
    "levelUp": "It grew!",
}


def roll_shiny(rng: random.Random, has_charm: bool) -> bool:
    return rng.randrange(balance.shiny_denominator(has_charm)) == 0


def roll_nature(rng: random.Random) -> str:
    return rng.choice(balance.NATURES)


def roll_unown_form(rng: random.Random, collected: set[str]) -> str:
    """Uncollected letters weigh 2, collected 1 — only after Unown itself was
    rolled, so species odds are untouched. Uniform once all 28 are owned."""
    weights = [
        balance.collection_weight(2, form in collected) for form in balance.UNOWN_FORMS
    ]
    return rng.choices(balance.UNOWN_FORMS, weights=weights, k=1)[0]


def roll_ditto(rng: random.Random, line: EvoLine) -> bool:
    """Whether this hatch is secretly a disguised Ditto.

    Restricted to common lines with 2+ forms, matching the Swift rule: the joke
    only lands when the disguise is something ordinary that visibly "evolves"
    before the reveal.
    """
    if line.rarity != Rarity.COMMON or line.total_forms < 2:
        return False
    return rng.randrange(balance.DITTO_DISGUISE_DENOMINATOR) == 0


def hatch(state: CompanionState, line: EvoLine, rng: random.Random) -> MonState:
    """Turn the egg into a companion. Shiny and nature are fixed here."""
    has_charm = state.inventory.get("shinyCharm", 0) > 0
    mon = MonState(
        base_id=line.base_id,
        path_ids=list(line.path_ids),
        planned_path_ids=list(line.path_ids),
        stage_index=0,
        used_at_stage=0,
        rarity=line.rarity,
        total_forms=line.total_forms,
        is_shiny=roll_shiny(rng, has_charm),
        nature=roll_nature(rng),
        has_growth_boost=state.has_collected_final(line.base_id),
        hatched_at=__import__("time").time(),
        profile=_profile.generate(rng.getrandbits(64)),
        unown_form=roll_unown_form(rng, state.collected_unown_forms())
        if line.base_id == balance.UNOWN_SPECIES_ID
        else None,
        # The disguise stores the species being impersonated; the reveal swaps
        # the display to Ditto while keeping this for the "it was Ditto!" moment.
        ditto_disguise=line.base_id if roll_ditto(rng, line) else None,
    )
    state.active = mon
    # The guarantee is consumed by the hatch it paid for.
    state.egg_tier = None
    state.pending_hatch_id = None
    state.egg_usage = 0
    return mon


def graduate(state: CompanionState, mon: MonState, now: float | None = None) -> DexEntry:
    """Archive a completed companion and clear the slot for a fresh egg."""
    import time as _time

    now = _time.time() if now is None else now
    if mon.profile is not None:
        mon.profile.advance_growth(balance.graduation_total(mon.rarity), mon.rarity)
    entry = DexEntry(
        base_id=mon.base_id,
        final_id=mon.current_id,
        chain_order=list(mon.path_ids),
        rarity=mon.rarity,
        is_shiny=mon.is_shiny,
        nature=mon.nature,
        caught_at=now,
        raised_seconds=(now - mon.hatched_at) if mon.hatched_at else None,
        profile=mon.profile,
        unown_form=mon.unown_form,
    )
    state.dex.append(entry)
    state.collected_finals.add(f"{mon.base_id}-{mon.current_id}")
    state.active = None
    state.egg_usage = 0
    return entry


def release(state: CompanionState, now: float | None = None) -> DexEntry | None:
    """Send the current companion off (buying an egg) without losing its
    Pokédex credit (#242).

    Only reached forms are credited — the planned path would make an egg a
    shortcut to evolutions never reached. Shiny follows shows_shiny so a
    disguised Ditto isn't exposed. collected_finals is untouched: it wasn't
    raised to the end, so it must not count toward completion or repeat
    boosts.
    """
    import time as _time

    mon = state.active
    if mon is None:
        return None
    now = _time.time() if now is None else now
    reached = mon.path_ids[: mon.stage_index + 1] or [mon.current_id]
    if mon.ditto_revealed:
        reached = [balance.DITTO_SPECIES_ID]
    entry = DexEntry(
        base_id=mon.base_id,
        final_id=reached[-1],
        chain_order=list(reached),
        rarity=mon.rarity,
        is_shiny=mon.shows_shiny,
        nature=mon.nature,
        caught_at=mon.hatched_at or now,
        raised_seconds=(now - mon.hatched_at) if mon.hatched_at else None,
        released_at=now,
        profile=mon.profile,
        unown_form=mon.unown_form,
    )
    state.dex.append(entry)
    state.active = None
    return entry


def apply_usage(
    state: CompanionState,
    tokens: int,
    line_for_egg=None,
    rng: random.Random | None = None,
) -> GrowthEvents:
    """Feed tokens to the companion.

    Overflow always carries forward, so a single large delta can hatch and then
    immediately advance a stage rather than being clipped.
    """
    events = GrowthEvents()
    if tokens <= 0:
        return events
    rng = rng or random.Random()

    state.used_since_install += tokens

    # --- egg ---
    if state.active is None:
        state.egg_usage += tokens
        egg_threshold = state.egg_threshold()
        if state.egg_usage < egg_threshold:
            return events
        if line_for_egg is None:
            # No species data (offline). Hold the tokens in the egg and hatch
            # once a line is available — never discard progress.
            return events
        overflow = state.egg_usage - egg_threshold
        mon = hatch(state, line_for_egg, rng)
        events.hatched = mon.current_id
        tokens = overflow
        if tokens <= 0:
            return events

    # --- growth ---
    mon = state.active
    mon.used_at_stage += tokens
    while True:
        threshold = state.stage_threshold(mon)
        if mon.used_at_stage < threshold:
            break
        mon.used_at_stage -= threshold
        if mon.is_final_form:
            events.graduated = graduate(state, mon)
            break
        mon.stage_index += 1
        events.evolved_to = mon.current_id
        # A disguised Ditto reveals itself on its first evolution — the moment
        # the "evolution" would have to actually happen.
        if mon.ditto_disguise is not None and not mon.ditto_revealed:
            mon.ditto_revealed = True
            events.ditto_revealed = True
            # The boost was the disguise's line's; Ditto earns its own (#378).
            mon.has_growth_boost = state.has_collected_final(balance.DITTO_SPECIES_ID)
            # Same individual, different species: species fields reroll.
            if mon.profile is not None:
                mon.profile.rebase(mon.rarity, mon.rarity)

    if state.active is not None:
        reconcile_profile_growth(state)
    return events


def reconcile_profile_growth(state: CompanionState) -> None:
    """Move the companion's profile level to its earned growth, in standard
    balance units: completed stages count in full, and the current stage by
    the fraction earned at the current difficulty. Never lowers a level."""
    mon = state.active
    if mon is None or mon.profile is None:
        return
    completed = _profile.reconstructed_growth(mon.rarity, mon.total_forms, mon.stage_index)
    standard_phase = balance.phase_threshold(mon.rarity, mon.total_forms, mon.stage_index)
    fraction = min(1.0, max(0.0, mon.used_at_stage / max(1, state.stage_threshold(mon))))
    candidate = min(
        balance.graduation_total(mon.rarity), completed + int(standard_phase * fraction)
    )
    mon.profile.advance_growth(candidate, mon.rarity)


def ensure_profiles(state: CompanionState) -> bool:
    """Give pre-#264 companions and dex entries a profile, deterministically
    seeded so a migration run twice yields the same Pokémon. Returns True
    when anything was added."""
    changed = False
    mon = state.active
    if mon is not None and mon.profile is None:
        key = f"active:{mon.base_id}:{','.join(map(str, mon.path_ids))}:{mon.hatched_at}"
        mon.profile = _profile.generate(_profile.stable_seed(key))
        changed = True
    for index, entry in enumerate(state.dex):
        if entry.profile is not None:
            continue
        if entry.released:
            # Only reached forms were kept; treat them as the line (an upper
            # bound when it was released before its planned final).
            forms = max(1, len(entry.chain_order))
            growth = _profile.reconstructed_growth(entry.rarity, forms, forms - 1)
        else:
            growth = balance.graduation_total(entry.rarity)
        p = _profile.generate(
            _profile.stable_seed(f"dex:{index}:{entry.base_id}:{entry.final_id}:{entry.caught_at}"),
            growth_tokens=growth,
        )
        p.advance_growth(growth, entry.rarity)
        entry.profile = p
        changed = True
    reconcile_profile_growth(state)
    return changed


def set_growth_difficulty(state: CompanionState, value: float) -> bool:
    """Apply a new growth multiplier, keeping the earned *fraction* of the
    current egg or stage. Never hatches, evolves or graduates by itself —
    settings changes are not usage. Returns True when anything changed."""
    new = balance.clamp_difficulty(value)
    old = state.growth_difficulty
    if new == old:
        return False

    def rescaled(credits: int, base: int) -> int:
        old_threshold = max(1, round(base * old))
        new_threshold = max(1, round(base * new))
        value = int(credits / old_threshold * new_threshold)
        value = max(0, value)
        # Rounding must never turn an incomplete stage into a completed one.
        return min(new_threshold - 1, value) if credits < old_threshold else value

    if state.active is not None:
        state.active.used_at_stage = rescaled(
            state.active.used_at_stage, state.active.phase_threshold
        )
    else:
        state.egg_usage = rescaled(state.egg_usage, balance.EGG_HATCH_THRESHOLD)
    state.growth_difficulty = new
    return True


def set_shop_difficulty(state: CompanionState, value: float) -> bool:
    new = balance.clamp_difficulty(value)
    if new == state.shop_difficulty:
        return False
    state.shop_difficulty = new
    return True
