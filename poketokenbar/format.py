"""Number formatting — ports Sources/PokeTokenBar/Core/TokenFormatter.swift.

Thresholds and decimal counts are copied verbatim; changing them changes what
the panel shows.
"""


def _trim(value: float, decimals: int) -> str:
    s = f"{value:.{decimals}f}"
    s = s.rstrip("0").rstrip(".")
    return s


def compact(value: int) -> str:
    """987 -> '987', 12345 -> '12.3K', 190612940 -> '190.6M', 1.24e9 -> '1.24B'."""
    v = float(abs(value))
    sign = "-" if value < 0 else ""
    if v < 1_000:
        return str(value)
    # K and M show one decimal: promote at the rounding boundary so 999,950
    # reads "1M", not "1000K" (upstream #365).
    if v < 999_950:
        return sign + _trim(v / 1_000, 1) + "K"
    if v < 999_950_000:
        return sign + _trim(v / 1_000_000, 1) + "M"
    return sign + _trim(v / 1_000_000_000, 2) + "B"


def grouped(value: int) -> str:
    """Thousands separators for popup detail — 190612940 -> '190,612,940'."""
    return f"{value:,}"


def cost(usd: float) -> str:
    return f"${usd:.2f}"


def cost_compact(usd: float) -> str:
    """Panel cost: $9.5 / $311 / $12.0K.

    The band is chosen from the *rounded* value (upstream #418): 99.96 is
    "$100", not "$100.0".
    """
    tenths = f"{usd:.1f}"
    if float(tenths) < 100:
        return f"${tenths}"
    whole = f"{usd:.0f}"
    if float(whole) < 10_000:
        return f"${whole}"
    return f"${usd / 1_000:.1f}K"


def percent(value: float) -> str:
    """79.96 -> '80%' (not '80.0%'), 88.35 -> '88.3%' (upstream #418)."""
    tenths = f"{value:.1f}"
    return tenths.removesuffix(".0") + "%"
