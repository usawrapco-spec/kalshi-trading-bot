"""ProbabilityArbitrage strategy - finds events where YES+NO prices don't sum to $1.00.

Inspired by vladmeer/kalshi-arbitrage-bot. Checks orderbook spreads and
cross-market pricing within the same event for risk-free-ish opportunities.
"""

from strategies.base import BaseStrategy
from utils.logger import setup_logger
from utils.market_helpers import get_price, get_yes_price, get_no_price
from utils.api_resilience import resilient_strategy

logger = setup_logger('prob_arb')

# Kalshi charges ~3.5% fee on the winning position
FEE_RATE = 0.035

# Minimum profitable gap after fees (conservative estimate)
MIN_PROFITABLE_GAP = 0.08  # 8% gap needed to overcome fees + slippage


def kalshi_fee(cost_basis):
    """Estimate Kalshi fees on a position. Kalshi charges ~3.5% on winning positions."""
    if cost_basis <= 0:
        return 0
    # Conservative estimate: fees are ~3.5% of the position value
    return cost_basis * FEE_RATE


class ProbabilityArbStrategy(BaseStrategy):
    """Find events where YES+NO prices don't sum to $1.00, or orderbook spread opportunities."""

    def __init__(self, client, risk_manager, db):
        super().__init__(client, risk_manager, db)
        logger.info("ProbabilityArb initialized (fee-adjusted YES+NO mispricing)")

    @resilient_strategy
    def analyze(self, markets):
        signals = []

        # Group markets by event_ticker for cross-market arb detection
        events = {}
        arb_checked = 0
        arb_found = 0

        for m in markets:
            if m.get('status', 'open') != 'open':
                continue

            yes_price = get_price(m, ['yes_bid', 'yes_bid_dollars', 'last_price', 'last_price_dollars'])
            no_price = get_price(m, ['no_bid', 'no_bid_dollars'])

            # If we have both yes and no prices, check if they sum to less than $1
            if yes_price > 0 and no_price > 0 and yes_price < 0.95 and no_price < 0.95:
                arb_checked += 1
                total = yes_price + no_price
                gap = 1.0 - total

                # Only consider if gap is large enough to overcome fees
                if gap >= MIN_PROFITABLE_GAP:
                    # Estimate profit: buy both sides, one wins
                    # Cost: $1 total, get back $1 from winner minus fees
                    est_fee = kalshi_fee(1.0)  # Fee on winning $1 position
                    net_profit = gap - est_fee

                    if net_profit > 0.01:  # At least 1 cent profit per dollar
                        arb_found += 1
                        ticker = m.get('ticker', '')
                        signals.append({
                            'ticker': ticker, 'title': m.get('title', ''), 'action': 'buy',
                            'side': 'yes', 'count': 1, 'confidence': 95,
                            'strategy_type': 'prob_arb',
                            'edge': net_profit, 'model_prob': 0.99,
                            'reason': f"ProbArb: YES={yes_price:.2f}+NO={no_price:.2f}={total:.2f}, gap={gap:.1%}, net={net_profit:.3f} after {est_fee:.3f} fees",
                        })

            # Spread opportunity
            yes_ask = get_price(m, ['yes_ask', 'yes_ask_dollars'])
            no_bid = get_price(m, ['no_bid', 'no_bid_dollars'])
            if yes_ask > 0 and no_bid > 0:
                implied_yes_from_no = 1.0 - no_bid
                spread = implied_yes_from_no - yes_ask
                if spread > 0.03:
                    arb_found += 1
                    ticker = m.get('ticker', '')
                    signals.append({
                        'ticker': ticker, 'title': m.get('title', ''), 'action': 'buy',
                        'side': 'yes', 'count': 1, 'confidence': 85,
                        'strategy_type': 'prob_arb',
                        'edge': spread, 'model_prob': implied_yes_from_no,
                        'reason': f"ProbArb spread: yes_ask={yes_ask:.2f} vs 1-no_bid={implied_yes_from_no:.2f}, spread={spread:.3f}",
                    })

            evt = m.get('event_ticker')
            if evt:
                events.setdefault(evt, []).append(m)

        # Sort by edge, keep top 5 only
        signals.sort(key=lambda s: s.get('edge', 0), reverse=True)
        top_edge = signals[0]['edge'] if signals else 0
        signals = signals[:5]
        logger.info(f"ProbArb: checked {arb_checked}, found {arb_found} opportunities, top edge={top_edge:.1%}, returning {len(signals)} signals")
        return signals

    def execute(self, signal, dry_run=False):
        if not self.can_execute(signal):
            return None
        self.log_signal(signal)
        return self.client.create_order(
            ticker=signal['ticker'], action='buy', side=signal['side'],
            count=signal['count'], order_type='market', dry_run=dry_run,
        )
