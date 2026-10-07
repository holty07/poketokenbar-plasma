"""Shop, bag, and rare-candy grants — ports the economy half of CompanionStore.

Tokens serve two roles at once: an unspendable growth meter
(used_since_install) and a spendable wallet (minus spent_tokens). Buying never
rewinds growth; it only debits the wallet.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import balance, companion
from .balance import Rarity
from .companion import CompanionState


class ShopError(Exception):
    """A purchase that cannot proceed (unknown item, funds, or state)."""


@dataclass(slots=True)
class ShopEntry:
    key: str
    kind: str  # "item" | "egg"
    price: int
    label: str
    owned: bool = False


# Consumables can be bought several at a time (#371); the Shiny Charm is
# passive and eggs replace the companion, so both stay single-purchase.
STACKABLE = ("rareCandy", "mint")


def price(state: CompanionState, base: int) -> int:
    """What the shop actually charges: base price x shop difficulty (#244)."""
    return balance.scaled(base, state.shop_difficulty)


def is_high_value(state: CompanionState) -> bool:
    """A companion worth an extra confirmation before an egg discards it
    (#335): legendary, or visibly shiny. Rare is deliberately excluded —
    a prompt on every reroll trains people to click through it — and a
    disguised Ditto's shininess stays secret."""
    mon = state.active
    return mon is not None and (mon.rarity == Rarity.LEGENDARY or mon.shows_shiny)


def entries(state: CompanionState) -> list[ShopEntry]:
    """Everything on sale, cheapest first."""
    out = [
        ShopEntry("rareCandy", "item", price(state, balance.RARE_CANDY_PRICE), "Rare Candy"),
        ShopEntry("mint", "item", price(state, balance.MINT_PRICE), "Mint"),
        ShopEntry(
            "shinyCharm",
            "item",
            price(state, balance.SHINY_CHARM_PRICE),
            "Shiny Charm",
            # Passive and permanent: held, never consumed, bought once.
            owned=state.inventory.get("shinyCharm", 0) > 0,
        ),
    ]
    for tier in balance.EGG_SHOP_TIERS:
        key = f"egg:{tier}" if tier else "egg"
        label = {
            None: "Pokemon Egg",
            Rarity.UNCOMMON: "Uncommon Egg",
            Rarity.RARE: "Rare Egg",
        }[tier]
        out.append(ShopEntry(key, "egg", price(state, balance.egg_price(tier)), label))
    return sorted(out, key=lambda e: e.price)


def _debit(state: CompanionState, price: int) -> None:
    if state.spendable_tokens < price:
        raise ShopError(
            f"not enough tokens: need {price:,}, have {state.spendable_tokens:,}"
        )
    state.spent_tokens += price


def max_buy_count(state: CompanionState, key: str) -> int:
    """How many of `key` the wallet can afford in one purchase."""
    entry = next((e for e in entries(state) if e.key == key), None)
    if entry is None or entry.price <= 0:
        return 0
    if key not in STACKABLE:
        if entry.kind == "egg" and state.active is None:
            return 0
        return 0 if entry.owned or state.spendable_tokens < entry.price else 1
    return state.spendable_tokens // entry.price


def buy(state: CompanionState, key: str, count: int = 1, confirm: bool = False) -> str:
    """Purchase a shop entry. Returns a short description of what happened.

    `count` > 1 is allowed for stackable consumables only, and is all or
    nothing: an unaffordable count buys none rather than as many as fit.
    `confirm` must be set to discard a high-value companion for an egg.
    """
    entry = next((e for e in entries(state) if e.key == key), None)
    if entry is None:
        raise ShopError(f"unknown shop item: {key}")
    if count < 1:
        raise ShopError("quantity must be at least 1")
    if count > 1 and key not in STACKABLE:
        raise ShopError(f"{entry.label} can only be bought one at a time")

    if entry.kind == "item":
        if entry.key == "shinyCharm" and entry.owned:
            raise ShopError("shiny charm is already held")
        _debit(state, entry.price * count)
        state.inventory[entry.key] = state.inventory.get(entry.key, 0) + count
        return f"bought {entry.label}" + (f" ×{count}" if count > 1 else "")

    if state.active is None:
        # An egg means "release the current Pokémon and reroll" (#261).
        # While incubating there is nothing to release, and buying one would
        # throw away the egg's progress.
        raise ShopError("an egg is already incubating")
    if is_high_value(state) and not confirm:
        raise ShopError("this would release a legendary or shiny Pokémon; confirm to proceed")

    # Eggs replace the current companion outright.
    tier = {"egg": None, f"egg:{Rarity.UNCOMMON}": Rarity.UNCOMMON,
            f"egg:{Rarity.RARE}": Rarity.RARE}[entry.key]
    _debit(state, entry.price)
    # The released companion keeps its Pokédex credit (#242) but is NOT
    # graduated: collected_finals is untouched, so it doesn't count toward
    # completion or repeat-hatch boosts.
    companion.release(state)
    state.egg_usage = 0
    state.egg_tier = tier
    state.pending_hatch_id = None
    return f"bought {entry.label}"


def use_item(state: CompanionState, key: str, rng=None, count: int = 1) -> str:
    """Consume held items. Only Rare Candy can be used in bulk (#328)."""
    held = state.inventory.get(key, 0)
    if held <= 0:
        raise ShopError(f"no {key} in bag")
    if count < 1:
        raise ShopError("quantity must be at least 1")
    if count > 1 and key != "rareCandy":
        raise ShopError(f"{key} can only be used one at a time")
    if count > held:
        raise ShopError(f"only {held} {key} in bag")

    if key == "rareCandy":
        if state.active is None:
            raise ShopError("candy needs a hatched companion")
        used = 0
        # One candy at a time through apply_usage, so carry-over, evolution
        # and graduation behave exactly as real usage does. Stop at
        # graduation: leftover candy stays in the bag rather than feeding an
        # egg it can't help.
        while used < count and state.active is not None:
            state.inventory[key] -= 1
            used += 1
            companion.apply_usage(state, balance.RARE_CANDY_XP, rng=rng)
        return "used Rare Candy" + (f" ×{used}" if used > 1 else "")

    if key == "mint":
        if state.active is None:
            raise ShopError("mint needs a hatched companion")
        import random

        rng = rng or random.Random()
        state.inventory[key] = held - 1
        state.active.nature = companion.roll_nature(rng)
        return f"nature is now {state.active.nature}"

    raise ShopError(f"{key} cannot be used")


# --- rare candy grants -----------------------------------------------------


def window_key(kind: str) -> str:
    """Stable identifier for a limit window.

    Must never include volatile fields such as resets_at. Including them made
    the Swift app re-notify on every refresh, because a rolling weekly window
    reports a new resets_at each time.
    """
    return f"limit:{kind}"


def candy_preview(state: CompanionState, count: int) -> dict:
    """What using `count` Rare Candies would do, without doing it (#328)."""
    import copy
    import random

    sim = copy.deepcopy(state)
    sim.inventory["rareCandy"] = max(count, sim.inventory.get("rareCandy", 0))
    before = sim.active
    if before is None or count < 1:
        return {"count": 0, "evolutions": 0, "graduates": False, "used": 0}
    start_stage = before.stage_index
    dex_before = len(sim.dex)
    used = 0
    while used < count and sim.active is not None:
        sim.inventory["rareCandy"] -= 1
        used += 1
        # Fixed seed: a preview must not consume the real RNG.
        companion.apply_usage(sim, balance.RARE_CANDY_XP, rng=random.Random(0))
    graduates = len(sim.dex) > dex_before
    mon = sim.active
    evolutions = (before.total_forms - 1 - start_stage) if graduates else (
        (mon.stage_index if mon else start_stage) - start_stage
    )
    return {
        "count": count,
        "used": used,
        "evolutions": max(0, evolutions),
        "graduates": graduates,
        "stage_progress": round(min(1.0, mon.used_at_stage / sim.stage_threshold(mon)), 4)
        if mon is not None and not graduates
        else 1.0,
    }


def grant_candy(
    state: CompanionState, windows: dict[str, float], epochs: dict[str, str] | None = None
) -> int:
    """Award candy for maxed limit windows. Edge-triggered.

    `windows` maps kind ("session"/"weekly") to utilization percent;
    `epochs` optionally maps the same kinds to the window's resets_at.
    Returns how many candies were granted.
    """
    granted = 0
    epochs = epochs or {}
    for kind, utilization in windows.items():
        key = window_key(kind)
        epoch = epochs.get(kind)
        if epoch:
            # A new window is a new chance, even when the dip below 100% was
            # never observed (asleep or quit across the reset, #334).
            previous_epoch = state.candy_window_epoch.get(key)
            if previous_epoch is not None and previous_epoch != epoch:
                state.candy_grant_tier.pop(key, None)
            state.candy_window_epoch[key] = epoch
        tier = 2 if utilization >= 100 else (1 if utilization >= 80 else 0)
        previous = state.candy_grant_tier.get(key, 0)

        if tier == 0:
            # Dropped back below the warning line — rearm for next time.
            state.candy_grant_tier.pop(key, None)
            continue
        if tier <= previous:
            continue
        state.candy_grant_tier[key] = tier
        if tier < 2:
            continue  # only a full window pays out

        if not state.candy_feature_seeded:
            # First run must not pay out for a window that was already full
            # before the feature existed.
            continue
        amount = (
            balance.RARE_CANDY_WEEKLY_GRANT
            if kind == "weekly"
            else balance.RARE_CANDY_SESSION_GRANT
        )
        state.inventory["rareCandy"] = state.inventory.get("rareCandy", 0) + amount
        granted += amount

    state.candy_feature_seeded = True
    return granted
