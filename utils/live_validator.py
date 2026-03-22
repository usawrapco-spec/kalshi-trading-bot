"""Live trade validation — additional safety gate for real-money orders."""

from config import Config
from utils.logger import setup_logger

logger = setup_logger('live_validator')

# Hard limits for live weather trades (dollars)
LIVE_LIMITS = {
    'max_per_trade_dollars': 2.00,
    'max_total_exposure_dollars': 5.00,
    'max_contract_price': 0.15,
    'max_contracts_per_trade': 20,
    'min_cash_reserve_pct': 0.50,
    'min_edge_pct': 0.20,
    'min_model_confidence': 85,
}


def is_live_strategy(strategy_name):
    """Check if a strategy should place real orders."""
    if not Config.ENABLE_TRADING:
        return False
    if not Config.LIVE_STRATEGIES:
        return False
    return strategy_name in Config.LIVE_STRATEGIES


def validate_live_trade(signal, balance_cents, open_live_positions):
    """Validate a live trade against hard safety limits.

    Args:
        signal: dict with keys: entry_price (dollars), count, edge, confidence, side
        balance_cents: real Kalshi balance in cents
        open_live_positions: list of dicts with 'cost' key (dollars)

    Returns:
        (approved: bool, reason: str)
    """
    balance = balance_cents / 100.0
    entry_price = signal.get('entry_price', 0)
    count = signal.get('count', 1)
    trade_cost = entry_price * count
    total_open_cost = sum(p.get('cost', 0) for p in open_live_positions)

    # Contract price cap
    if entry_price > LIVE_LIMITS['max_contract_price']:
        return False, f"Contract price ${entry_price:.2f} exceeds ${LIVE_LIMITS['max_contract_price']} max"

    # Per-trade dollar cap
    if trade_cost > LIVE_LIMITS['max_per_trade_dollars']:
        return False, f"Trade cost ${trade_cost:.2f} exceeds ${LIVE_LIMITS['max_per_trade_dollars']:.2f} max"

    # Total exposure cap
    if total_open_cost + trade_cost > LIVE_LIMITS['max_total_exposure_dollars']:
        return False, (
            f"Total exposure would be ${total_open_cost + trade_cost:.2f}, "
            f"exceeds ${LIVE_LIMITS['max_total_exposure_dollars']:.2f} max"
        )

    # Contract count cap
    if count > LIVE_LIMITS['max_contracts_per_trade']:
        return False, f"Contract count {count} exceeds {LIVE_LIMITS['max_contracts_per_trade']} max"

    # Cash reserve check
    if balance > 0 and (total_open_cost + trade_cost) / balance > (1 - LIVE_LIMITS['min_cash_reserve_pct']):
        return False, (
            f"Would exceed {LIVE_LIMITS['min_cash_reserve_pct']:.0%} cash reserve "
            f"(${total_open_cost + trade_cost:.2f}/${balance:.2f})"
        )

    # Edge minimum
    edge = abs(signal.get('edge', 0))
    if edge < LIVE_LIMITS['min_edge_pct']:
        return False, f"Edge {edge:.0%} below {LIVE_LIMITS['min_edge_pct']:.0%} minimum for real money"

    # Confidence minimum
    confidence = signal.get('confidence', 0)
    if confidence < LIVE_LIMITS['min_model_confidence']:
        return False, f"Confidence {confidence:.0f} below {LIVE_LIMITS['min_model_confidence']} minimum for real money"

    return True, "Approved"
