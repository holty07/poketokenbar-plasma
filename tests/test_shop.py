import random

import pytest

from poketokenbar import balance, shop
from poketokenbar.balance import Rarity
from poketokenbar.companion import CompanionState, EvoLine, apply_usage


def _with_mon(tokens=0):
    s = CompanionState()
    line = EvoLine(base_id=1, path_ids=[1, 2, 3], rarity=Rarity.COMMON)
    apply_usage(s, balance.EGG_HATCH_THRESHOLD, line_for_egg=line, rng=random.Random(1))
    s.used_since_install = tokens
    s.spent_tokens = 0
    return s


# --- shop listing ----------------------------------------------------------


def test_entries_are_sorted_by_price():
    prices = [e.price for e in shop.entries(CompanionState())]
    assert prices == sorted(prices)


def test_shiny_charm_shows_as_owned_once_held():
    s = CompanionState()
    s.inventory["shinyCharm"] = 1
    charm = next(e for e in shop.entries(s) if e.key == "shinyCharm")
    assert charm.owned is True


def test_three_egg_tiers_are_offered():
    keys = {e.key for e in shop.entries(CompanionState())}
    assert "egg" in keys and f"egg:{Rarity.UNCOMMON}" in keys and f"egg:{Rarity.RARE}" in keys


# --- buying ----------------------------------------------------------------


def test_buying_debits_the_wallet_but_not_growth():
    s = _with_mon(tokens=balance.RARE_CANDY_PRICE)
    shop.buy(s, "rareCandy")
    assert s.inventory["rareCandy"] == 1
    assert s.spent_tokens == balance.RARE_CANDY_PRICE
    assert s.used_since_install == balance.RARE_CANDY_PRICE  # growth never rewinds
    assert s.spendable_tokens == 0


def test_cannot_buy_without_funds():
    s = CompanionState()
    with pytest.raises(shop.ShopError):
        shop.buy(s, "rareCandy")


def test_unknown_item_is_rejected():
    with pytest.raises(shop.ShopError):
        shop.buy(CompanionState(), "masterball")


def test_shiny_charm_cannot_be_bought_twice():
    s = _with_mon(tokens=balance.SHINY_CHARM_PRICE * 2)
    shop.buy(s, "shinyCharm")
    with pytest.raises(shop.ShopError):
        shop.buy(s, "shinyCharm")


def test_buying_an_egg_releases_the_companion_into_the_dex():
    # #242: a released companion keeps its Pokédex credit, but it was not
    # raised to the end, so collected_finals (completion, repeat boosts)
    # stays untouched.
    s = _with_mon(tokens=balance.FRESH_EGG_PRICE)
    shop.buy(s, "egg")
    assert s.active is None
    [entry] = s.dex
    assert entry.released and entry.chain_order == [1]
    assert s.collected_finals == set()
    assert s.egg_usage == 0


def test_release_credits_only_reached_forms():
    s = _with_mon(tokens=balance.FRESH_EGG_PRICE)
    s.active.stage_index = 1
    shop.buy(s, "egg")
    assert s.dex[0].chain_order == [1, 2]


def test_releasing_a_legendary_or_shiny_needs_confirmation():
    for make_precious in (
        lambda m: setattr(m, "rarity", Rarity.LEGENDARY),
        lambda m: setattr(m, "is_shiny", True),
    ):
        s = _with_mon(tokens=balance.FRESH_EGG_PRICE)
        make_precious(s.active)
        with pytest.raises(shop.ShopError):
            shop.buy(s, "egg")
        assert s.active is not None and s.spent_tokens == 0
        shop.buy(s, "egg", confirm=True)
        assert s.active is None


def test_rare_and_disguised_shiny_ditto_need_no_confirmation():
    s = _with_mon(tokens=balance.FRESH_EGG_PRICE * 2)
    s.active.rarity = Rarity.RARE
    assert not shop.is_high_value(s)
    s.active.rarity = Rarity.COMMON
    s.active.is_shiny = True
    s.active.ditto_disguise = 1  # the secret must not leak through a prompt
    assert not shop.is_high_value(s)
    shop.buy(s, "egg")
    assert s.dex[0].is_shiny is False


def test_premium_egg_records_its_guarantee():
    s = _with_mon(tokens=balance.egg_price(Rarity.RARE))
    shop.buy(s, f"egg:{Rarity.RARE}")
    assert s.egg_tier == Rarity.RARE


# --- using items -----------------------------------------------------------


def test_candy_grows_the_companion():
    s = _with_mon()
    s.inventory["rareCandy"] = 1
    before = s.used_since_install
    shop.use_item(s, "rareCandy")
    # used_since_install, not used_at_stage: a candy can carry the companion
    # across a stage boundary (see companion.py's overflow handling), and
    # this growth ledger never rewinds regardless of where it lands.
    assert s.used_since_install == before + balance.RARE_CANDY_XP
    assert s.inventory["rareCandy"] == 0


def test_candy_cannot_be_used_on_an_egg():
    s = CompanionState()
    s.inventory["rareCandy"] = 1
    with pytest.raises(shop.ShopError):
        shop.use_item(s, "rareCandy")


def test_using_an_item_you_lack_is_rejected():
    with pytest.raises(shop.ShopError):
        shop.use_item(_with_mon(), "rareCandy")


def test_mint_rerolls_the_nature():
    s = _with_mon()
    s.inventory["mint"] = 1
    s.active.nature = "hardy"
    shop.use_item(s, "mint", rng=random.Random(5))
    assert s.active.nature in balance.NATURES
    assert s.inventory["mint"] == 0


# --- candy grants ----------------------------------------------------------


def test_first_run_does_not_pay_out_for_an_already_full_window():
    # Installing while a window sits at 100% must not backfill candy.
    s = CompanionState()
    assert shop.grant_candy(s, {"session": 100.0}) == 0
    assert s.inventory.get("rareCandy", 0) == 0


def test_full_session_window_grants_one_candy():
    s = CompanionState()
    shop.grant_candy(s, {"session": 10.0})  # seeds the feature
    assert shop.grant_candy(s, {"session": 100.0}) == balance.RARE_CANDY_SESSION_GRANT


def test_full_weekly_window_grants_five():
    s = CompanionState()
    shop.grant_candy(s, {"weekly": 10.0})
    assert shop.grant_candy(s, {"weekly": 100.0}) == balance.RARE_CANDY_WEEKLY_GRANT


def test_grant_is_edge_triggered_not_repeated_every_poll():
    s = CompanionState()
    shop.grant_candy(s, {"session": 10.0})
    first = shop.grant_candy(s, {"session": 100.0})
    second = shop.grant_candy(s, {"session": 100.0})
    third = shop.grant_candy(s, {"session": 100.0})
    assert first > 0
    assert second == 0 and third == 0


def test_window_rearms_after_dropping_below_the_warning_line():
    s = CompanionState()
    shop.grant_candy(s, {"session": 10.0})
    shop.grant_candy(s, {"session": 100.0})
    shop.grant_candy(s, {"session": 5.0})  # window reset
    assert shop.grant_candy(s, {"session": 100.0}) > 0


def test_window_key_excludes_volatile_fields():
    # A rolling weekly window reports a new resets_at on every fetch. Keying on
    # it re-fired the notification each refresh in the Swift app.
    assert shop.window_key("weekly") == "limit:weekly"
    assert "resets" not in shop.window_key("weekly")


# --- difficulty, bulk ------------------------------------------------------


def test_shop_difficulty_scales_every_price():
    s = CompanionState()
    s.shop_difficulty = 0.5
    by_key = {e.key: e.price for e in shop.entries(s)}
    assert by_key["rareCandy"] == balance.RARE_CANDY_PRICE // 2
    assert by_key[f"egg:{Rarity.RARE}"] == balance.egg_price(Rarity.RARE) // 2


def test_bulk_buy_is_all_or_nothing():
    s = _with_mon(tokens=balance.MINT_PRICE * 3)
    assert shop.max_buy_count(s, "mint") == 3
    with pytest.raises(shop.ShopError):
        shop.buy(s, "mint", count=4)
    assert s.inventory.get("mint", 0) == 0 and s.spent_tokens == 0
    shop.buy(s, "mint", count=3)
    assert s.inventory["mint"] == 3


def test_passive_items_and_eggs_cannot_be_bulk_bought():
    s = _with_mon(tokens=balance.SHINY_CHARM_PRICE * 5)
    for key in ("shinyCharm", "egg"):
        with pytest.raises(shop.ShopError):
            shop.buy(s, key, count=2)
    assert shop.max_buy_count(s, "shinyCharm") == 1


def test_bulk_candy_stops_at_graduation_and_keeps_the_rest():
    s = _with_mon()
    s.inventory["rareCandy"] = 20
    # Common 3-form line: 375M total at default balance, so 4 candies graduate.
    preview = shop.candy_preview(s, 20)
    assert preview["graduates"] is True and preview["used"] == 4
    shop.use_item(s, "rareCandy", count=20)
    assert s.active is None
    assert s.inventory["rareCandy"] == 16
    assert len(s.dex) == 1


def test_candy_preview_does_not_change_state():
    s = _with_mon()
    s.inventory["rareCandy"] = 1
    before = (s.active.used_at_stage, s.active.stage_index, s.inventory["rareCandy"])
    preview = shop.candy_preview(s, 1)
    assert preview["evolutions"] == 1
    assert (s.active.used_at_stage, s.active.stage_index, s.inventory["rareCandy"]) == before


def test_candy_grant_rearms_on_a_new_window_epoch():
    s = CompanionState()
    s.candy_feature_seeded = True
    assert shop.grant_candy(s, {"session": 100}, {"session": "t1"}) == 1
    # Still 100% but never seen dipping: a new resets_at is a new window.
    assert shop.grant_candy(s, {"session": 100}, {"session": "t1"}) == 0
    assert shop.grant_candy(s, {"session": 100}, {"session": "t2"}) == 1


def test_eggs_cannot_be_bought_while_incubating():
    # #261: there is nothing to release, and the egg's progress would be lost.
    s = CompanionState()
    s.used_since_install = balance.FRESH_EGG_PRICE * 2
    s.egg_usage = 1_000
    with pytest.raises(shop.ShopError):
        shop.buy(s, "egg")
    assert s.egg_usage == 1_000 and s.spent_tokens == 0
    assert shop.max_buy_count(s, "egg") == 0
