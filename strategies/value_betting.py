"""Value betting strategy - small positions on highly likely outcomes."""

from strategies.base import BaseStrategy
from utils.logger import setup_logger, log_trade

logger = setup_logger('value_betting_strategy')


class ValueBettingStrategy(BaseStrategy):
    """
    Value betting strategy targeting markets where one side is priced
    at 90-97 cents (very likely outcome). Takes small positions on the
    likely side for consistent, high win-rate returns.
    """

    def __init__(self, client, risk_manager, db, min_price=90, max_price=97):
        super().__init__(client, risk_manager)
        self.db = db
        self.min_price = min_price  # Minimum price in cents (90 = 90%)
        self.max_price = max_price  # Maximum price in cents (97 = 97%)
        logger.info(f"Value betting strategy initialized (range: {min_price}-{max_price}c)")

    def analyze(self, markets):
        signals = []

        for market in markets:
            ticker = market.get('ticker')
            if market.get('status') != 'open':
                continue

            signal = self._check_value_bet(ticker, market)
            if signal:
                signals.append(signal)

        return signals

    def _check_value_bet(self, ticker, market):
        yes_bid = market.get('yes_bid', 0)
        no_bid = market.get('no_bid', 0)
        volume = market.get('volume', 0)
        close_time = market.get('close_time', '')

        # Check YES side: priced 90-97c means market thinks YES is very likely
        if self.min_price <= yes_bid <= self.max_price:
            return self._build_signal(ticker, 'yes', yes_bid, volume)

        # Check NO side: priced 90-97c means market thinks NO is very likely
        if self.min_price <= no_bid <= self.max_price:
            return self._build_signal(ticker, 'no', no_bid, volume)

        return None

    def _build_signal(self, ticker, side, price, volume):
        # Higher price = more likely = higher confidence
        # But also smaller profit margin
        implied_prob = price / 100.0

        # Confidence based on implied probability and volume
        confidence = 0.0
        # Base: implied probability gives 40-55 points (for 90-97c range)
        confidence += implied_prob * 55
        # Volume: liquid markets are more reliable
        if volume > 1000:
            confidence += 25
        elif volume > 500:
            confidence += 18
        elif volume > 100:
            confidence += 10
        else:
            # Low volume = unreliable pricing, skip
            return None

        confidence = min(confidence, 100)

        profit_per_contract = 100 - price  # cents profit if correct

        logger.info(
            f"Value bet: {ticker} {side.upper()} at {price}c, "
            f"profit/contract={profit_per_contract}c, vol={volume}, "
            f"confidence={confidence:.0f}"
        )

        return {
            'ticker': ticker,
            'action': 'buy',
            'side': side,
            'count': 5,  # Small position sizes for value bets
            'reason': (
                f'Value bet: {side.upper()} at {price}c '
                f'(implied {implied_prob:.0%}), '
                f'profit/contract={profit_per_contract}c, vol={volume}'
            ),
            'confidence': confidence,
            'strategy_type': 'value_betting',
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
