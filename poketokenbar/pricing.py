"""Per-model token rates — ports ModelPricing.swift.

Rates are USD per million tokens, matching ccusage's offline LiteLLM snapshot.
An unpriced model costs 0, exactly as ccusage treats it — showing a guessed
price would be worse than showing none.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ModelRate:
    input: float = 0.0
    output: float = 0.0
    cache_write: float = 0.0
    cache_read: float = 0.0


def per_million(
    input_: float, output: float, cache_write: float, cache_read: float
) -> ModelRate:
    m = 1_000_000
    return ModelRate(input_ / m, output / m, cache_write / m, cache_read / m)


ZERO = ModelRate()

TABLE: dict[str, ModelRate] = {
    # Claude rates per platform.claude.com/docs/en/about-claude/pricing
    # (base input, output, 5m cache write, cache hit), synced from upstream.
    # The Claude 5 family needs exact rows: the "opus"/"sonnet" family
    # fallback below carries Claude 4 prices, which overstate every one of them.
    "claude-opus-5": per_million(5, 25, 6.25, 0.5),
    "claude-opus-5-5": per_million(4, 20, 5, 0.2),
    "claude-sonnet-5": per_million(2, 10, 2.5, 0.2),
    "claude-sonnet-5-5": per_million(2, 10, 2.5, 0.2),
    "claude-fable-5": per_million(10, 50, 12.5, 1.0),
    "claude-fable-5-1": per_million(10, 50, 12.5, 0.25),
    "claude-opus-4-20250514": per_million(15, 75, 18.75, 1.5),
    "claude-sonnet-4-20250514": per_million(3, 15, 3.75, 0.3),
    "claude-sonnet-4-5-20250929": per_million(3, 15, 3.75, 0.3),
    "claude-opus-4-8": per_million(5, 25, 6.25, 0.5),
    "claude-opus-4-7": per_million(5, 25, 6.25, 0.5),
    "claude-sonnet-4-6": per_million(3, 15, 3.75, 0.3),
    "claude-haiku-4-5-20251001": per_million(1, 5, 1.25, 0.1),
    # OpenAI standard API rates (developers.openai.com/api/docs/pricing).
    "gpt-6-astra": per_million(10, 50, 12.5, 1),
    "gpt-6-sol": per_million(2, 10, 2.5, 0.2),
    "gpt-6.1-sol": per_million(2, 10, 2.5, 0.1),
    "gpt-6-luna": per_million(0.1, 0.5, 0.125, 0.01),
    "gpt-5.6-sol": per_million(4, 20, 5, 0.4),
    "gpt-5.6-terra": per_million(2, 12, 2.5, 0.2),
    "gpt-5.6-luna": per_million(0.2, 1.2, 0.25, 0.02),
    "gpt-5": per_million(1.25, 10, 0, 0.125),
    "gpt-5-codex": per_million(1.25, 10, 0, 0.125),
    "gpt-5.1": per_million(1.25, 10, 0, 0.125),
    "gpt-5.1-codex": per_million(1.25, 10, 0, 0.125),
    "gpt-5.2": per_million(1.75, 14, 0, 0.175),
    "gpt-5.2-codex": per_million(1.75, 14, 0, 0.175),
    "gpt-5.3-codex": per_million(1.75, 14, 0, 0.175),
    "gpt-5.4": per_million(2.5, 15, 0, 0.25),
    "gpt-5.5": per_million(5, 30, 0, 0.5),
    # Gemini official API rates (base tier). Cache is the read rate only;
    # storage-time charges are not modelled.
    "gemini-2.5-pro": per_million(1.25, 10, 0, 0.125),
    "gemini-2.5-flash": per_million(0.30, 2.5, 0, 0.03),
    # A distinct SKU — the "flash" family fallback would bill it at Flash rates.
    "gemini-2.5-flash-lite": per_million(0.10, 0.40, 0, 0.01),
    "gemini-2.0-flash": per_million(0.10, 0.4, 0, 0.025),
}

# Documented model identities only (ports ModelPricing.aliases).
ALIASES: dict[str, str] = {
    "gpt-5.6": "gpt-5.6-sol",
    "claude-sonnet-4": "claude-sonnet-4-20250514",
    "claude-opus-4": "claude-opus-4-20250514",
    "claude-sonnet-4-5": "claude-sonnet-4-5-20250929",
    "claude-haiku-4-5": "claude-haiku-4-5-20251001",
    "gpt-5-2025-08-07": "gpt-5",
    "gpt-5.1-2025-11-13": "gpt-5.1",
    "gpt-5.2-2025-12-11": "gpt-5.2",
    "gpt-5.4-2026-03-05": "gpt-5.4",
    "gpt-5.5-2026-04-23": "gpt-5.5",
}

_NAMESPACES = ("openai/", "anthropic/", "google/", "models/")

# Above 272K prompt tokens these bill input at 2x and output at 1.5x.
_LONG_CONTEXT_GPT = frozenset(
    {
        "gpt-6-astra", "gpt-6-sol", "gpt-6.1-sol", "gpt-6-luna",
        "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5", "gpt-5.4",
    }
)


def model_key(model: str) -> str:
    """Normalize a model id to its TABLE key: trim, lowercase, drop one
    provider namespace, strip a context-window suffix like "[1m]", alias."""
    key = (model or "").strip().lower()
    for prefix in _NAMESPACES:
        if key.startswith(prefix):
            key = key[len(prefix):]
            break
    if key.endswith("]") and "[" in key:
        key = key[: key.rindex("[")]
    return ALIASES.get(key, key)


def rate(model: str) -> ModelRate:
    """Exact match first, then a family fallback for version drift."""
    m = model_key(model)
    exact = TABLE.get(m)
    if exact is not None:
        return exact

    # Grok reports its own cost; there is no rate card. This must precede the
    # family fallbacks so names like grok-codex-* are not priced as GPT.
    if m.startswith("grok"):
        return ZERO
    # Antigravity is subscription-billed and reports no amount. Its
    # "antigravity/" prefix also dodges the exact table, which matters because
    # that CLI calls models like claude-sonnet-4-6 that would otherwise match.
    if m.startswith("antigravity/"):
        return ZERO

    if "opus" in m:
        return per_million(5, 25, 6.25, 0.5)
    if "sonnet" in m:
        return per_million(3, 15, 3.75, 0.3)
    if "haiku" in m:
        return per_million(1, 5, 1.25, 0.1)
    if "gpt" in m or "codex" in m or "o4" in m or "o3" in m:
        return per_million(5, 30, 0, 0.5)
    if m.startswith("gemini"):
        if "pro" in m:
            return per_million(1.25, 10, 0, 0.125)
        if "flash" in m:
            return per_million(0.30, 2.5, 0, 0.03)
    return ZERO


_unpriced_seen: set[str] = set()


def note_unpriced(model: str) -> None:
    """Log each model the table can't price, once per process (upstream
    #361) — otherwise a new model just shows $0 and goes unnoticed. The
    daemon's stderr lands in the journal under systemd."""
    key = model_key(model)
    if not key or key in _unpriced_seen:
        return
    _unpriced_seen.add(key)
    print(f"poketokend: unpriced model {model!r} — its cost counts as $0 "
          "until pricing.TABLE has a row", file=sys.stderr)


def entry_cost(entry) -> float:
    """An entry's cost: the source-reported amount when there is one, else
    the price-table estimate."""
    if entry.explicit_cost is not None:
        return entry.explicit_cost
    return cost(entry.model, entry.input, entry.output, entry.cache_write, entry.cache_read)


def cost(model: str, input_: int, output: int, cache_write: int, cache_read: int) -> float:
    """One request's cost. Pass per-request buckets, never daily aggregates:
    request size decides long-context pricing."""
    r = rate(model)
    key = model_key(model)
    prompt = input_ + cache_write + cache_read
    # Grok and Antigravity are zero by design (they bill elsewhere); anything
    # else at zero with tokens is a missing price row.
    if r == ZERO and prompt + output > 0 and not key.startswith(("grok", "antigravity/")):
        note_unpriced(model)
    long_context = (key in _LONG_CONTEXT_GPT and prompt > 272_000) or (
        key == "gemini-2.5-pro" and prompt > 200_000
    )
    in_mult = 2.0 if long_context else 1.0
    out_mult = 1.5 if long_context else 1.0
    return (
        input_ * r.input + cache_write * r.cache_write + cache_read * r.cache_read
    ) * in_mult + output * r.output * out_mult
