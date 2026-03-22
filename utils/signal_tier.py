"""Signal tiering system — rate weather signals AAA/AA/A/B and size bets accordingly.

Only applies to LIVE trades. Paper trading sizing is unchanged.
"""

from utils.logger import setup_logger

logger = setup_logger('signal_tier')

TIER_CONFIG = {
    'AAA': {'max_pct': 0.25, 'max_dollars': 5.00, 'max_contracts': 50, 'label': 'AAA'},
    'AA':  {'max_pct': 0.15, 'max_dollars': 3.00, 'max_contracts': 30, 'label': 'AA'},
    'A':   {'max_pct': 0.05, 'max_dollars': 1.00, 'max_contracts': 10, 'label': 'A'},
    'B':   {'max_pct': 0.01, 'max_dollars': 0.15, 'max_contracts': 1,  'label': 'B'},
}

# Portfolio-level limits for live trading
MAX_TOTAL_EXPOSURE_PCT = 0.75   # Use up to 75% of balance
MAX_SINGLE_MARKET_PCT = 0.25    # No more than 25% in one market
MAX_LIVE_TRADES_PER_CYCLE = 5   # Don't blow everything at once


def rate_signal(signal, hours_to_close=None):
    """Rate a weather signal from B to AAA.

    Args:
        signal: dict with keys: model_prob, edge, confidence, entry_price (price_for_side)
        hours_to_close: hours until market closes (None = unknown)

    Returns:
        (tier: str, score: int, reasons: list[str])
    """
    score = 0
    reasons = []

    # Factor 1: Model confidence (ensemble agreement)
    prob = signal.get('model_prob', 0.5)
    if prob >= 0.95:
        score += 4
        reasons.append(f"ensemble={prob:.0%}")
    elif prob >= 0.85:
        score += 3
        reasons.append(f"ensemble={prob:.0%}")
    elif prob >= 0.70:
        score += 2

    # Factor 2: Edge size
    edge = abs(signal.get('edge', 0))
    if edge >= 0.50:
        score += 3
        reasons.append(f"edge={edge:.0%}")
    elif edge >= 0.30:
        score += 2
        reasons.append(f"edge={edge:.0%}")
    elif edge >= 0.20:
        score += 1

    # Factor 3: Forecast horizon (closer = more accurate)
    hours = hours_to_close or 48
    if hours <= 18:
        score += 3
        reasons.append("today/tmrw")
    elif hours <= 48:
        score += 2
    elif hours <= 120:
        score += 1

    # Factor 4: Contract price (cheaper = better reward/risk)
    entry = signal.get('entry_price', 0.10)
    if entry <= 0.05:
        score += 2
        reasons.append(f"cheap@{entry:.0f}c")
    elif entry <= 0.10:
        score += 1

    # Determine tier
    if score >= 10:
        tier = 'AAA'
    elif score >= 7:
        tier = 'AA'
    elif score >= 4:
        tier = 'A'
    else:
        tier = 'B'

    return tier, score, reasons


def size_by_tier(tier, entry_price, balance):
    """Calculate number of contracts based on tier and balance.

    Args:
        tier: 'AAA', 'AA', 'A', or 'B'
        entry_price: price per contract in dollars
        balance: total portfolio value in dollars

    Returns:
        int: number of contracts (at least 1)
    """
    cfg = TIER_CONFIG.get(tier, TIER_CONFIG['B'])
    if entry_price <= 0 or balance <= 0:
        return 1

    max_spend = min(balance * cfg['max_pct'], cfg['max_dollars'])
    contracts = int(max_spend / entry_price)
    contracts = min(contracts, cfg['max_contracts'])
    return max(contracts, 1)


def check_portfolio_limits(trade_cost, balance, current_exposure):
    """Check if a trade fits within portfolio limits.

    Returns:
        (allowed: bool, max_allowed_cost: float, reason: str)
    """
    if balance <= 0:
        return False, 0, "No balance"

    max_exposure = balance * MAX_TOTAL_EXPOSURE_PCT
    remaining = max_exposure - current_exposure

    if remaining <= 0:
        return False, 0, f"75% exposure limit (${current_exposure:.2f}/${max_exposure:.2f})"

    if trade_cost > remaining:
        return True, remaining, f"Reduced to fit (${remaining:.2f} remaining of ${max_exposure:.2f})"

    return True, trade_cost, "OK"


def hours_until_close(close_time_str):
    """Calculate hours from now until market close time."""
    if not close_time_str:
        return None
    try:
        from datetime import datetime
        close = datetime.fromisoformat(close_time_str.replace('Z', '+00:00'))
        now = datetime.utcnow().replace(tzinfo=close.tzinfo)
        delta = (close - now).total_seconds() / 3600
        return max(0, delta)
    except Exception:
        return None
