import pytest

from poketokenbar import pricing


def test_exact_table_match():
    r = pricing.rate("claude-sonnet-4-6")
    assert r.input == pytest.approx(3 / 1_000_000)
    assert r.output == pytest.approx(15 / 1_000_000)


def test_family_fallback_for_unknown_version():
    # Version drift must not silently zero a real model's cost.
    assert pricing.rate("claude-opus-9-9").input == pytest.approx(5 / 1_000_000)


def test_grok_is_zero_before_any_family_fallback():
    # grok-codex-* would otherwise match the "codex" -> GPT fallback and show a
    # fabricated dollar amount.
    assert pricing.rate("grok-codex-fast") == pricing.ZERO
    assert pricing.rate("grok-4o-mini") == pricing.ZERO


def test_antigravity_prefix_is_zero_even_for_a_priced_model():
    # That CLI calls claude-sonnet-4-6, which would match the exact table
    # without the prefix. It is subscription-billed, so cost must stay 0.
    assert pricing.rate("antigravity/claude-sonnet-4-6") == pricing.ZERO


def test_unknown_model_costs_nothing_rather_than_guessing():
    assert pricing.rate("totally-unknown-model") == pricing.ZERO


def test_gemini_family_fallback():
    assert pricing.rate("gemini-3.0-pro").input == pytest.approx(1.25 / 1_000_000)
    assert pricing.rate("gemini-3.0-flash").input == pytest.approx(0.30 / 1_000_000)


def test_unknown_gemini_variant_is_zero():
    assert pricing.rate("gemini-experimental-x") == pricing.ZERO


def test_cost_sums_all_four_token_kinds():
    # 1M of each against sonnet: 3 + 15 + 3.75 + 0.3
    total = pricing.cost("claude-sonnet-4-6", 1_000_000, 1_000_000, 1_000_000, 1_000_000)
    assert total == pytest.approx(22.05)


def test_cost_of_an_unpriced_model_is_zero():
    assert pricing.cost("totally-unknown-model", 10**9, 10**9, 10**9, 10**9) == 0.0


def test_unpriced_models_are_logged_once(capsys):
    pricing._unpriced_seen.clear()
    pricing.cost("brand-new-model", 1, 1, 0, 0)
    pricing.cost("Brand-New-Model", 1, 1, 0, 0)
    pricing.cost("grok-4", 1, 1, 0, 0)  # zero by design, not a missing row
    err = capsys.readouterr().err
    assert err.count("unpriced model") == 1 and "grok" not in err


def test_claude_5_family_has_exact_rows_not_the_claude_4_fallback():
    assert pricing.rate("claude-opus-5-5").input == pytest.approx(4 / 1_000_000)
    assert pricing.rate("claude-opus-5-5").cache_read == pytest.approx(0.2 / 1_000_000)
    assert pricing.rate("claude-sonnet-5").input == pytest.approx(2 / 1_000_000)
    assert pricing.rate("claude-fable-5").input == pytest.approx(10 / 1_000_000)
    # Fable 5.1 cache reads are a quarter of Fable 5's.
    assert pricing.rate("claude-fable-5-1").cache_read == pytest.approx(0.25 / 1_000_000)


def test_model_key_normalizes_namespace_suffix_and_alias():
    assert pricing.model_key(" Anthropic/claude-opus-5[1m] ") == "claude-opus-5"
    assert pricing.model_key("claude-haiku-4-5") == "claude-haiku-4-5-20251001"
    assert pricing.rate("openai/gpt-5.6") == pricing.TABLE["gpt-5.6-sol"]


def test_flash_lite_is_not_billed_as_flash():
    assert pricing.rate("gemini-2.5-flash-lite").input == pytest.approx(0.10 / 1_000_000)


def test_long_context_gpt_request_bills_input_2x_output_1_5x():
    short = pricing.cost("gpt-5.5", 272_000, 1_000, 0, 0)
    assert short == pytest.approx(272_000 * 5e-6 + 1_000 * 30e-6)
    long = pricing.cost("gpt-5.5", 272_001, 1_000, 0, 0)
    assert long == pytest.approx(272_001 * 5e-6 * 2 + 1_000 * 30e-6 * 1.5)
