"""Shared helpers for parsing Kalshi market data.

Kalshi API returns prices and volumes as strings (e.g. "0.5600", "123.00").
All helpers safely coerce to float before any comparison or arithmetic.
"""


def safe_float(v):
    """Convert any value to float, returning 0 on failure."""
    if v is None:
        return 0.0
    try:
        return float(v)
    except (ValueError, TypeError):
        return 0.0


def get_yes_price(m):
    """Extract YES price in dollars (0.0-1.0) trying all known field names."""
    for f in ('yes_bid_dollars', 'yes_bid', 'yes_ask_dollars', 'yes_ask',
              'last_price_dollars', 'last_price'):
        v = safe_float(m.get(f))
        if v > 0:
            return v / 100.0 if v > 1 else v
    return 0.0


def get_no_price(m):
    """Extract NO price in dollars (0.0-1.0) trying all known field names."""
    for f in ('no_bid_dollars', 'no_bid', 'no_ask_dollars', 'no_ask'):
        v = safe_float(m.get(f))
        if v > 0:
            return v / 100.0 if v > 1 else v
    # Fallback: derive from YES
    yes = get_yes_price(m)
    return 1.0 - yes if yes > 0 else 0.0


def get_yes_cents(m):
    """Extract YES price in cents (0-100)."""
    return int(round(get_yes_price(m) * 100))


def get_no_cents(m):
    """Extract NO price in cents (0-100)."""
    return int(round(get_no_price(m) * 100))


def get_volume(m):
    """Extract volume from any known field (all may be strings)."""
    for f in ('volume_24h_fp', 'volume_24h', 'volume_fp', 'volume'):
        v = safe_float(m.get(f))
        if v > 0:
            return v
    return 0.0


def get_price(m, field_list):
    """Try multiple field names, normalize to 0-1 dollars."""
    for f in field_list:
        v = safe_float(m.get(f))
        if v > 0:
            return v / 100.0 if v > 1 else v
    return 0.0
