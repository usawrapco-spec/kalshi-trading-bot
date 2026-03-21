"""ProbabilityArbitrage strategy - finds events where YES+NO prices don't sum to $1.00.

Inspired by vladmeer/kalshi-arbitrage-bot. Checks orderbook spreads and
cross-market pricing within the same event for risk-free-ish opportunities.
"""

from strategies.base import BaseStrategy
from utils.logger import setup_logger

logger = setup_logger('prob_arb')

# Kalshi fee formula: round_up(0.07 * contracts * price * (1-price))
FEE_RATE = 0.07


def kalshi_fee(contracts, price):
    """Estimate Kalshi fee for a trade."""
    if price <= 0 or price >= 1:
        return 0
    return round(FEE_RATE * contracts * price * (1 - price) + 0.005, 2)


def get_price(m, field_list):
    """Try multiple field names, normalize to 0-1 dollars."""
    for f in field_list:
        v = m.get(f)
        if v is not None and v > 0:
            return v / 100.0 if v > 1 else v
    return 0.0


class ProbabilityArbStrategy(BaseStrategy):
    """Find events where YES+NO prices don't sum to $1.00, or orderbook spread opportunities."""

    def __init__(self, client, risk_manager, db):
        super().__init__(client, risk_manager, db)
        logger.info("ProbabilityArb initialized (fee-adjusted YES+NO mispricing)")

    def analyze(self, markets):
        signals = []

        # Group markets by event_ticker for cross-market arb detection
        events = {}
        arb_checked = 0
        arb_found = 0

        for m in markets:
            if m.get('status') != 'open':
                continue

            yes_price = get_price(m, ['yes_bid', 'yes_bid_dollars', 'last_price', 'last_price_dollars'])
            no_price = get_price(m, ['no_bid', 'no_bid_dollars'])

            # If we have both yes and no prices, check if they sum to less than $1
            if yes_price > 0 and no_price > 0:
                arb_checked += 1
                total = yes_price + no_price
                if total < 0.98:  # Meaningful gap (>2 cents after fees)
                    gap = 1.0 - total
                    # Check if gap exceeds fees for buying both sides
                    fee_yes = kalshi_fee(1, yes_price)
                    fee_no = kalshi_fee(1, no_price)
                    net_profit = gap - fee_yes - fee_no
                    if net_profit > 0:
                        arb_found += 1
                        ticker = m.get('ticker', '')
                        logger.info(
                            f"ProbArb: {ticker} YES={yes_price:.2f}+NO={no_price:.2f}={total:.2f}, "
                            f"gap={gap:.3f}, fees={fee_yes+fee_no:.3f}, net={net_profit:.3f} -> PAPER BUY BOTH"
                        )
                        signals.append({
                            'ticker': ticker, 'title': m.get('title', ''), 'action': 'buy',
                            'side': 'yes',  # We'd buy both, but log as YES for tracking
                            'count': 10, 'confidence': 95,  # Near-certain profit
                            'strategy_type': 'prob_arb',
                            'edge': net_profit, 'model_prob': 0.99,
                            'reason': f"ProbArb: YES={yes_price:.2f}+NO={no_price:.2f}={total:.2f}, net_profit={net_profit:.3f}/contract",
                        })

            # Also check: is YES ask significantly below (1 - NO bid)? Spread opportunity
            yes_ask = get_price(m, ['yes_ask', 'yes_ask_dollars'])
            no_bid = get_price(m, ['no_bid', 'no_bid_dollars'])
            if yes_ask > 0 and no_bid > 0:
                implied_yes_from_no = 1.0 - no_bid
                spread = implied_yes_from_no - yes_ask
                if spread > 0.03:  # 3 cent spread minimum
                    arb_found += 1
                    ticker = m.get('ticker', '')
                    logger.info(
                        f"ProbArb spread: {ticker} yes_ask={yes_ask:.2f} < 1-no_bid={implied_yes_from_no:.2f}, "
                        f"spread={spread:.3f} -> PAPER BUY YES"
                    )
                    signals.append({
                        'ticker': ticker, 'title': m.get('title', ''), 'action': 'buy',
                        'side': 'yes', 'count': 5, 'confidence': 85,
                        'strategy_type': 'prob_arb',
                        'edge': spread, 'model_prob': implied_yes_from_no,
                        'reason': f"ProbArb spread: yes_ask={yes_ask:.2f} vs 1-no_bid={implied_yes_from_no:.2f}, spread={spread:.3f}",
                    })

            # Group by event for multi-market analysis
            evt = m.get('event_ticker')
            if evt:
                events.setdefault(evt, []).append(m)

        # Check multi-market events: probabilities within an event should sum to ~1
        for evt, evt_markets in events.items():
            if len(evt_markets) < 2:
                continue
            total_yes = sum(get_price(em, ['yes_bid', 'yes_bid_dollars', 'last_price', 'last_price_dollars']) for em in evt_markets)
            if total_yes > 0 and abs(total_yes - 1.0) > 0.10:
                logger.info(f"ProbArb event: {evt} has {len(evt_markets)} markets summing to {total_yes:.2f} (should be ~1.0)")

        logger.info(f"ProbArb: checked {arb_checked} markets, found {arb_found} opportunities, {len(signals)} signals")
        return signals

    def execute(self, signal, dry_run=False):
        if not self.can_execute(signal):
            return None
        self.log_signal(signal)
        return self.client.create_order(
            ticker=signal['ticker'], action='buy', side=signal['side'],
            count=signal['count'], order_type='market', dry_run=dry_run,
        )
