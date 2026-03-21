"""OrderBookEdge strategy - trades based on bid/ask imbalance in short-term markets.

For crypto, weather, and other short-term markets, fetches the orderbook
and calculates bid vs ask size imbalance. Trades in direction of imbalance
when it's >60/40 skewed and market settles within hours.
"""

from datetime import datetime, timezone, timedelta
from strategies.base import BaseStrategy
from utils.logger import setup_logger
from utils.market_helpers import get_yes_price, get_volume, safe_float

logger = setup_logger('orderbook_edge')

IMBALANCE_THRESHOLD = 0.60  # 60% on one side to trigger
MAX_HOURS_TO_CLOSE = 12  # Only short-term markets
MAX_ORDERBOOK_CHECKS = 15  # Limit API calls per cycle

ELIGIBLE_KEYWORDS = [
    'btc', 'bitcoin', 'eth', 'ethereum', 'crypto', 'solana', 'sol',
    'temperature', 'weather', 'kxhigh', 'rain', 'snow',
    'price', 'above', 'below', 'over', 'under',
    's&p', 'nasdaq', 'dow', 'stock', 'index',
]


class OrderBookEdgeStrategy(BaseStrategy):
    """Trade bid/ask imbalance on short-term markets."""

    def __init__(self, client, risk_manager, db):
        super().__init__(client, risk_manager, db)
        logger.info("OrderBookEdge initialized (bid/ask imbalance, <12h markets)")

    def analyze(self, markets):
        signals = []
        now = datetime.now(timezone.utc)
        eligible = []

        for m in markets:
            if m.get('status', 'open') != 'open':
                continue

            # Must be short-term
            close_time = m.get('close_time') or m.get('expiration_time') or ''
            if not close_time:
                continue
            try:
                close_dt = datetime.fromisoformat(close_time.replace('Z', '+00:00'))
                hours_left = (close_dt - now).total_seconds() / 3600
            except Exception:
                continue

            if hours_left <= 0 or hours_left > MAX_HOURS_TO_CLOSE:
                continue

            # Must match eligible keywords
            ticker = (m.get('ticker') or '').lower()
            title = (m.get('title') or '').lower()
            if not any(kw in f"{ticker} {title}" for kw in ELIGIBLE_KEYWORDS):
                continue

            volume = get_volume(m)
            eligible.append((m, hours_left, volume))

        # Sort by volume, check top N orderbooks
        eligible.sort(key=lambda x: x[2], reverse=True)
        to_check = eligible[:MAX_ORDERBOOK_CHECKS]

        logger.info(f"OrderBookEdge: {len(eligible)} eligible short-term markets, checking top {len(to_check)} orderbooks")

        for m, hours_left, volume in to_check:
            sig = self._check_orderbook(m, hours_left)
            if sig:
                signals.append(sig)

        logger.info(f"OrderBookEdge: {len(signals)} signals")
        return signals

    def _check_orderbook(self, m, hours_left):
        ticker = m.get('ticker', '')

        ob = self.client.get_orderbook(ticker, depth=10)
        if not ob:
            return None

        # Parse orderbook - Kalshi returns {yes: [...], no: [...]}
        yes_orders = ob.get('yes', []) or []
        no_orders = ob.get('no', []) or []

        # Calculate total size on each side
        yes_size = sum(safe_float(o.get('quantity') or o.get('size') or 0) for o in yes_orders)
        no_size = sum(safe_float(o.get('quantity') or o.get('size') or 0) for o in no_orders)

        total = yes_size + no_size
        if total < 10:  # Not enough liquidity
            return None

        yes_pct = yes_size / total
        no_pct = no_size / total

        # Need >60% imbalance
        if yes_pct >= IMBALANCE_THRESHOLD:
            side = 'yes'
            imbalance = yes_pct
        elif no_pct >= IMBALANCE_THRESHOLD:
            side = 'no'
            imbalance = no_pct
        else:
            return None

        yes_price = get_yes_price(m)
        edge = imbalance - 0.50  # How much better than coin flip
        confidence = min(40 + imbalance * 40 + (12 - hours_left) * 2, 100)

        price_for_side = yes_price if side == 'yes' else (1 - yes_price)

        logger.info(
            f"OrderBookEdge: {ticker} {side.upper()} imbalance={imbalance:.0%} "
            f"(yes_size={yes_size:.0f} no_size={no_size:.0f}), "
            f"{hours_left:.1f}h left, price={price_for_side:.2f}"
        )

        return {
            'ticker': ticker, 'title': m.get('title', ''), 'action': 'buy',
            'side': side, 'count': 5, 'confidence': confidence,
            'strategy_type': 'orderbook_edge',
            'edge': edge, 'model_prob': imbalance,
            'reason': f"OrderBookEdge: {side.upper()} imbalance={imbalance:.0%}, {hours_left:.1f}h left, yes={yes_size:.0f}/no={no_size:.0f}",
        }

    def execute(self, signal, dry_run=False):
        if not self.can_execute(signal):
            return None
        self.log_signal(signal)
        return self.client.create_order(
            ticker=signal['ticker'], action='buy', side=signal['side'],
            count=signal['count'], order_type='market', dry_run=dry_run,
        )
