"""Momentum trading strategy - follows strong price movements."""

from strategies.base import BaseStrategy
from utils.logger import setup_logger, log_trade
import time

logger = setup_logger('momentum_strategy')


class MomentumStrategy(BaseStrategy):
    """
    Conservative momentum strategy requiring confirmation across
    multiple data points before entering a position.
    """

    def __init__(self, client, risk_manager, db, price_change_threshold=0.05):
        super().__init__(client, risk_manager)
        self.db = db
        self.price_change_threshold = price_change_threshold
        self.price_history = {}
        self.min_data_points = 3  # Must have 3+ confirming data points
        logger.info(f"Momentum strategy initialized (threshold: {price_change_threshold}, min points: {self.min_data_points})")

    def analyze(self, markets):
        signals = []

        for market in markets:
            ticker = market.get('ticker')
            if market.get('status') != 'open':
                continue

            current_price = market.get('yes_bid', 0)
            if not current_price:
                continue

            # Track price history
            if ticker not in self.price_history:
                self.price_history[ticker] = {
                    'prices': [current_price],
                    'timestamps': [time.time()],
                    'volume': market.get('volume', 0),
                }
                continue

            history = self.price_history[ticker]
            history['prices'].append(current_price)
            history['timestamps'].append(time.time())
            history['volume'] = market.get('volume', 0)

            # Keep last 10 data points
            if len(history['prices']) > 10:
                history['prices'] = history['prices'][-10:]
                history['timestamps'] = history['timestamps'][-10:]

            # Need at least min_data_points + 1 to confirm across min_data_points moves
            if len(history['prices']) < self.min_data_points + 1:
                continue

            signal = self._check_momentum(ticker, history, market)
            if signal:
                signals.append(signal)

        return signals

    def _check_momentum(self, ticker, history, market):
        prices = history['prices']
        volume = history.get('volume', 0)

        old_price = prices[0]
        current_price = prices[-1]

        if old_price == 0:
            return None

        price_change = (current_price - old_price) / old_price

        if abs(price_change) < self.price_change_threshold:
            return None

        is_upward = price_change > 0

        # Count consecutive confirming moves
        confirming_moves = 0
        total_moves = len(prices) - 1
        for i in range(1, len(prices)):
            if is_upward and prices[i] > prices[i - 1]:
                confirming_moves += 1
            elif not is_upward and prices[i] < prices[i - 1]:
                confirming_moves += 1

        # Require at least min_data_points confirming moves
        if confirming_moves < self.min_data_points:
            return None

        consistency = confirming_moves / total_moves

        # Need 60%+ consistency
        if consistency < 0.6:
            return None

        # Calculate confidence (0-100 scale)
        confidence = 0.0
        # Base: magnitude of price change (up to 40 points)
        confidence += min(abs(price_change) * 400, 40)
        # Consistency bonus (up to 25 points)
        confidence += consistency * 25
        # Data points bonus (up to 15 points for having many confirming points)
        confidence += min(confirming_moves * 5, 15)
        # Volume bonus (up to 20 points)
        if volume > 1000:
            confidence += 20
        elif volume > 500:
            confidence += 12
        elif volume > 100:
            confidence += 5

        confidence = min(confidence, 100)

        logger.info(
            f"Momentum on {ticker}: {price_change * 100:.1f}% change, "
            f"{confirming_moves}/{total_moves} confirming moves, "
            f"confidence={confidence:.0f}, vol={volume}"
        )

        return {
            'ticker': ticker,
            'action': 'buy',
            'side': 'yes' if is_upward else 'no',
            'count': 10,
            'reason': (
                f'Momentum: {price_change * 100:.1f}% {"up" if is_upward else "down"}, '
                f'{confirming_moves}/{total_moves} confirming, '
                f'consistency={consistency:.0%}, vol={volume}'
            ),
            'confidence': confidence,
            'strategy_type': 'momentum',
        }

    def execute(self, signal, dry_run=False):
        if not self.can_execute(signal):
            return None

        self.log_signal(signal)

        order = self.client.create_order(
            ticker=signal['ticker'],
            action=signal['action'],
            side=signal['side'],
            count=signal['count'],
            order_type='market',
            dry_run=dry_run
        )

        if order and not dry_run:
            self.risk_manager.update_position(
                signal['ticker'], signal['count'], signal['side']
            )
            log_trade({'strategy': self.name, 'signal': signal, 'order': order})
            if self.db:
                self.db.log_trade({
                    'ticker': signal['ticker'],
                    'action': signal['action'],
                    'side': signal['side'],
                    'count': signal['count'],
                    'strategy': self.name,
                    'reason': signal.get('reason'),
                    'confidence': signal.get('confidence'),
                    'order_id': order.get('order_id'),
                    'price': order.get('yes_price') or order.get('no_price')
                })

        return order
