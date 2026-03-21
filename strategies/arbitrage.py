"""Arbitrage trading strategy - exploits pricing inefficiencies."""

from strategies.base import BaseStrategy
from utils.logger import setup_logger, log_trade

logger = setup_logger('arbitrage_strategy')


class ArbitrageStrategy(BaseStrategy):
    """
    Conservative arbitrage strategy looking for mispriced markets.

    Key opportunities:
    1. Yes + No prices sum != 100 (should always equal 100)
    2. Large bid-ask spreads

    Requires edge to persist across multiple checks to filter glitches.
    """

    def __init__(self, client, risk_manager, db, min_edge=0.03):
        super().__init__(client, risk_manager)
        self.db = db
        self.min_edge = min_edge
        # Track opportunities across cycles to confirm persistence
        # {ticker: {'edge': float, 'count': int, 'type': str}}
        self.confirmed_opportunities = {}
        self.required_confirmations = 2
        logger.info(f"Arbitrage strategy initialized (min edge: {min_edge}, confirmations: {self.required_confirmations})")

    def analyze(self, markets):
        signals = []
        seen_tickers = set()

        for market in markets:
            ticker = market.get('ticker')
            if market.get('status') != 'open':
                continue

            orderbook = self.client.get_orderbook(ticker)
            if not orderbook:
                continue

            # Check yes/no price inefficiency
            opp = self._check_yes_no_arbitrage(ticker, orderbook, market)
            if opp:
                seen_tickers.add(ticker)
                signal = self._confirm_opportunity(ticker, opp)
                if signal:
                    signals.append(signal)

            # Check bid-ask spread opportunities
            opp = self._check_spread_opportunity(ticker, orderbook, market)
            if opp:
                seen_tickers.add(ticker)
                signal = self._confirm_opportunity(ticker, opp)
                if signal:
                    signals.append(signal)

        # Decay opportunities that weren't seen this cycle
        stale = [t for t in self.confirmed_opportunities if t not in seen_tickers]
        for t in stale:
            del self.confirmed_opportunities[t]

        return signals

    def _confirm_opportunity(self, ticker, opportunity):
        """Require the opportunity to persist across multiple checks."""
        key = f"{ticker}:{opportunity['type']}"
        prev = self.confirmed_opportunities.get(key)

        if prev and prev['type'] == opportunity['type']:
            prev['count'] += 1
            prev['edge'] = opportunity['edge']
        else:
            self.confirmed_opportunities[key] = {
                'edge': opportunity['edge'],
                'count': 1,
                'type': opportunity['type'],
            }

        entry = self.confirmed_opportunities[key]
        if entry['count'] >= self.required_confirmations:
            logger.info(f"Confirmed {opportunity['type']} on {ticker} after {entry['count']} checks (edge: {entry['edge']})")
            # Reset so we don't keep re-signaling
            del self.confirmed_opportunities[key]
            return opportunity['signal']

        logger.debug(f"Tracking {opportunity['type']} on {ticker}: check {entry['count']}/{self.required_confirmations}")
        return None

    def _check_yes_no_arbitrage(self, ticker, orderbook, market):
        yes_prices = orderbook.get('yes', [])
        no_prices = orderbook.get('no', [])

        if not yes_prices or not no_prices:
            return None

        best_yes_bid = max([p['price'] for p in yes_prices if p.get('price')], default=None)
        best_no_bid = max([p['price'] for p in no_prices if p.get('price')], default=None)

        if not best_yes_bid or not best_no_bid:
            return None

        total = best_yes_bid + best_no_bid

        if total < 100 - (self.min_edge * 100):
            edge = 100 - total
            volume = market.get('volume', 0)

            # Confidence: edge size + volume bonus + persistence will be checked
            confidence = min(edge / 5, 0.5) * 100  # base 0-50 from edge
            if volume > 1000:
                confidence += 15
            elif volume > 500:
                confidence += 10
            confidence = min(confidence + 20, 100)  # +20 for confirmed arb

            return {
                'type': 'yes_no_arb',
                'edge': edge,
                'signal': {
                    'ticker': ticker,
                    'action': 'buy',
                    'side': 'yes',
                    'count': 10,
                    'reason': f'Yes+No arb: total={total}c, edge={edge}c, vol={volume}',
                    'confidence': confidence,
                    'strategy_type': 'arbitrage',
                }
            }
        return None

    def _check_spread_opportunity(self, ticker, orderbook, market):
        yes_prices = orderbook.get('yes', [])

        if not yes_prices or len(yes_prices) < 2:
            return None

        yes_bids = [p for p in yes_prices if p.get('type') == 'bid']
        yes_asks = [p for p in yes_prices if p.get('type') == 'ask']

        if not yes_bids or not yes_asks:
            return None

        best_bid = max([p['price'] for p in yes_bids])
        best_ask = min([p['price'] for p in yes_asks])
        spread = best_ask - best_bid

        if spread > self.min_edge * 200:  # 3% edge = 6 cent spread minimum
            volume = market.get('volume', 0)
            confidence = min(spread / 10, 0.5) * 100
            if volume > 1000:
                confidence += 15
            elif volume > 500:
                confidence += 10
            confidence = min(confidence + 15, 100)

            return {
                'type': 'spread',
                'edge': spread,
                'signal': {
                    'ticker': ticker,
                    'action': 'buy',
                    'side': 'yes',
                    'count': 5,
                    'reason': f'Spread: {spread}c spread, bid={best_bid}, ask={best_ask}, vol={volume}',
                    'confidence': confidence,
                    'strategy_type': 'arbitrage',
                }
            }
        return None

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
