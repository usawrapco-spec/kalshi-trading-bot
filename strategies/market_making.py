"""MarketMaking strategy - provides liquidity by placing limit orders on both sides.

Unlike OrderBookEdge which trades existing imbalance, this strategy ACTUALLY MAKES MARKETS
by placing bid/ask limit orders simultaneously. Profits from the bid-ask spread while
providing liquidity to the market. Uses inventory management and dynamic spread adjustment.

This is a sophisticated strategy that most retail traders don't implement, but can provide
consistent returns by earning the spread rather than gambling on direction.
"""

import time
from datetime import datetime, timezone, timedelta
from strategies.base import BaseStrategy
from utils.logger import setup_logger
from utils.market_helpers import get_yes_price, get_volume, safe_float

logger = setup_logger('market_making')

# Strategy parameters
BASE_SPREAD = 0.03  # 3% base bid-ask spread
MIN_SPREAD = 0.01   # 1% minimum spread
MAX_SPREAD = 0.08   # 8% maximum spread
ORDER_SIZE = 2      # Contracts per side
MAX_INVENTORY = 10  # Max net position before stopping
INVENTORY_REBALANCE = 5  # Rebalance when inventory hits this
VOLATILITY_WINDOW = 20  # Markets to check for volatility
MIN_VOLUME = 50    # Minimum volume for market making
MAX_MARKETS = 3    # Max markets to make markets in per cycle

# Eligible categories - focus on liquid, short-term markets
ELIGIBLE_CATEGORIES = [
    'crypto', 'weather', 'sports', 'finance', 'politics'
]

ELIGIBLE_KEYWORDS = [
    'btc', 'bitcoin', 'eth', 'ethereum', 'crypto', 'solana', 'sol',
    'temperature', 'weather', 'kxhigh', 'rain', 'snow',
    'nba', 'nfl', 'mlb', 'nhl', 'basketball', 'football', 'baseball',
    's&p', 'nasdaq', 'dow', 'stock', 'fed', 'rate', 'gdp',
    'president', 'senate', 'election', 'trump', 'biden',
]


class MarketMakingStrategy(BaseStrategy):
    """Professional market making with inventory management and dynamic spreads."""

    def __init__(self, client, risk_manager, db):
        super().__init__(client, risk_manager, db)
        self.inventory = {}  # ticker -> net position (positive = long YES)
        self.active_orders = {}  # ticker -> {'yes_order_id': id, 'no_order_id': id}
        logger.info("MarketMaking initialized (liquidity provision with inventory management)")

    def analyze(self, markets):
        signals = []
        now = datetime.now(timezone.utc)

        # Find eligible markets
        eligible = []
        for m in markets:
            if m.get('status', 'open') != 'open':
                continue

            ticker = m.get('ticker', '')
            volume = get_volume(m)
            if volume < MIN_VOLUME:
                continue

            # Check keywords
            title = (m.get('title') or '').lower()
            if not any(kw in f"{ticker.lower()} {title}" for kw in ELIGIBLE_KEYWORDS):
                continue

            # Check time to expiration (prefer liquid active markets)
            close_time = m.get('close_time') or m.get('expiration_time') or ''
            hours_left = 999
            if close_time:
                try:
                    close_dt = datetime.fromisoformat(close_time.replace('Z', '+00:00'))
                    hours_left = (close_dt - now).total_seconds() / 3600
                except Exception:
                    continue

            # Focus on markets with 1-48 hours left (active but not last minute)
            if hours_left < 1 or hours_left > 48:
                continue

            eligible.append((m, volume, hours_left))

        # Sort by volume and take top candidates
        eligible.sort(key=lambda x: x[1], reverse=True)
        candidates = eligible[:MAX_MARKETS]

        logger.info(f"MarketMaking: {len(eligible)} eligible markets, making markets in top {len(candidates)}")

        for m, volume, hours_left in candidates:
            sig = self._make_market(m, volume, hours_left)
            if sig:
                signals.append(sig)

        logger.info(f"MarketMaking: {len(signals)} market making signals")
        return signals

    def _make_market(self, m, volume, hours_left):
        ticker = m.get('ticker', '')
        yes_price = get_yes_price(m)

        if yes_price <= 0.05 or yes_price >= 0.95:
            return None  # Too close to 0/1

        # Get current inventory for this market
        current_inv = self.inventory.get(ticker, 0)

        # Cancel existing orders if inventory is too large
        if abs(current_inv) >= MAX_INVENTORY:
            self._cancel_orders(ticker)
            logger.info(f"MarketMaking: {ticker} inventory {current_inv} >= {MAX_INVENTORY}, stopping market making")
            return None

        # Calculate dynamic spread based on volatility and time
        spread = self._calculate_spread(m, volume, hours_left)

        # Set bid/ask prices
        mid_price = yes_price
        half_spread = spread / 2

        yes_bid = max(0.01, mid_price - half_spread)
        yes_ask = min(0.99, mid_price + half_spread)
        no_bid = max(0.01, (1 - mid_price) - half_spread)
        no_ask = min(0.99, (1 - mid_price) + half_spread)

        # Adjust for inventory (lean against position)
        inventory_skew = current_inv * 0.005  # 0.5% adjustment per contract
        yes_bid -= inventory_skew
        yes_ask -= inventory_skew
        no_bid += inventory_skew  # Opposite for NO side
        no_ask += inventory_skew

        # Ensure valid prices
        yes_bid = max(0.01, min(0.99, yes_bid))
        yes_ask = max(0.01, min(0.99, yes_ask))
        no_bid = max(0.01, min(0.99, no_bid))
        no_ask = max(0.01, min(0.99, no_ask))

        # Calculate expected edge (theoretical profit from round trip)
        expected_edge = (yes_ask - yes_bid) + (no_ask - no_bid) - spread  # Fee deduction

        logger.info(
            f"MarketMaking: {ticker} mid={mid_price:.3f} spread={spread:.1%} "
            f"YES: {yes_bid:.3f}-{yes_ask:.3f} NO: {no_bid:.3f}-{no_ask:.3f} "
            f"inv={current_inv} edge={expected_edge:.1%}"
        )

        return {
            'ticker': ticker,
            'title': m.get('title', ''),
            'action': 'make_market',
            'side': 'both',  # Special action for market making
            'count': ORDER_SIZE,
            'confidence': 75,  # Market making is lower risk
            'strategy_type': 'market_making',
            'edge': expected_edge,
            'model_prob': mid_price,
            'reason': f"MarketMaking: spread={spread:.1%} YES:{yes_bid:.3f}-{yes_ask:.3f} NO:{no_bid:.3f}-{no_ask:.3f} inv={current_inv}",
            'market_data': {  # Extra data for execution
                'yes_bid': yes_bid,
                'yes_ask': yes_ask,
                'no_bid': no_bid,
                'no_ask': no_ask,
            }
        }

    def _calculate_spread(self, m, volume, hours_left):
        """Calculate dynamic spread based on market conditions."""
        base_spread = BASE_SPREAD

        # Increase spread for low volume
        if volume < 100:
            base_spread *= 1.5
        elif volume < 500:
            base_spread *= 1.2

        # Increase spread for short time left (higher urgency)
        if hours_left < 6:
            base_spread *= 1.3
        elif hours_left < 12:
            base_spread *= 1.1

        # Estimate volatility from price distance from 0.5
        yes_price = get_yes_price(m)
        volatility_factor = abs(yes_price - 0.5) * 2  # 0 at 0.5, 1 at extremes
        base_spread *= (1 + volatility_factor)

        return max(MIN_SPREAD, min(MAX_SPREAD, base_spread))

    def _cancel_orders(self, ticker):
        """Cancel existing orders for a ticker."""
        if ticker not in self.active_orders:
            return

        orders = self.active_orders[ticker]
        for side, order_id in orders.items():
            if order_id:
                try:
                    self.client.cancel_order(order_id)
                    logger.info(f"MarketMaking: cancelled {side} order {order_id} for {ticker}")
                except Exception as e:
                    logger.error(f"MarketMaking: failed to cancel {side} order {order_id}: {e}")

        del self.active_orders[ticker]

    def execute(self, signal, dry_run=False):
        """Execute market making by placing limit orders on both sides."""
        if not self.can_execute(signal):
            return None

        ticker = signal['ticker']
        market_data = signal.get('market_data', {})

        if not market_data:
            logger.error(f"MarketMaking: no market_data for {ticker}")
            return None

        # Cancel any existing orders first
        self._cancel_orders(ticker)

        # Place new limit orders
        yes_bid = market_data['yes_bid']
        yes_ask = market_data['yes_ask']
        no_bid = market_data['no_bid']
        no_ask = market_data['no_ask']

        orders_placed = {}

        try:
            # Place YES bid (buy YES at lower price)
            yes_bid_order = self.client.create_order(
                ticker=ticker, action='buy', side='yes',
                count=signal['count'], order_type='limit',
                price=yes_bid, dry_run=dry_run,
            )
            if yes_bid_order:
                orders_placed['yes_bid'] = yes_bid_order.get('order_id')

            # Place YES ask (sell YES at higher price)
            yes_ask_order = self.client.create_order(
                ticker=ticker, action='sell', side='yes',
                count=signal['count'], order_type='limit',
                price=yes_ask, dry_run=dry_run,
            )
            if yes_ask_order:
                orders_placed['yes_ask'] = yes_ask_order.get('order_id')

            # Place NO bid (buy NO at lower price)
            no_bid_order = self.client.create_order(
                ticker=ticker, action='buy', side='no',
                count=signal['count'], order_type='limit',
                price=no_bid, dry_run=dry_run,
            )
            if no_bid_order:
                orders_placed['no_bid'] = no_bid_order.get('order_id')

            # Place NO ask (sell NO at higher price)
            no_ask_order = self.client.create_order(
                ticker=ticker, action='sell', side='no',
                count=signal['count'], order_type='limit',
                price=no_ask, dry_run=dry_run,
            )
            if no_ask_order:
                orders_placed['no_ask'] = no_ask_order.get('order_id')

            self.active_orders[ticker] = orders_placed

            logger.info(f"MarketMaking: placed orders for {ticker}: {orders_placed}")

            # Log the signal for tracking
            self.log_signal(signal)

            return {'status': 'orders_placed', 'orders': orders_placed}

        except Exception as e:
            logger.error(f"MarketMaking: failed to place orders for {ticker}: {e}")
            # Cancel any orders that were placed
            for order_id in orders_placed.values():
                try:
                    self.client.cancel_order(order_id)
                except:
                    pass
            return None

    def update_inventory(self, ticker, side, quantity, price):
        """Update inventory when orders are filled."""
        if side == 'yes':
            self.inventory[ticker] = self.inventory.get(ticker, 0) + quantity
        else:  # no
            self.inventory[ticker] = self.inventory.get(ticker, 0) - quantity

        logger.info(f"MarketMaking: {ticker} inventory updated to {self.inventory[ticker]}")

    def get_inventory_report(self):
        """Get current inventory across all markets."""
        return dict(self.inventory)