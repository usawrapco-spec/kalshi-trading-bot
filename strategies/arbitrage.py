"""Arbitrage trading strategy - exploits pricing inefficiencies."""

from strategies.base import BaseStrategy
from utils.logger import setup_logger, log_trade

logger = setup_logger('arbitrage_strategy')


class ArbitrageStrategy(BaseStrategy):
    """
    Arbitrage strategy looking for mispriced markets.
    
    Key opportunities:
    1. Yes + No prices sum != 100 (should always equal 100)
    2. Related markets with inconsistent pricing
    3. Large bid-ask spreads
    """
    
    def __init__(self, client, risk_manager, min_edge=0.02):
        """
        Initialize arbitrage strategy.
        
        Args:
            min_edge: Minimum price edge required (e.g., 0.02 = 2 cents)
        """
        super().__init__(client, risk_manager)
        self.min_edge = min_edge
        logger.info(f"Arbitrage strategy initialized (min edge: {min_edge})")
    
    def analyze(self, markets):
        """Find arbitrage opportunities in markets."""
        signals = []
        
        for market in markets:
            ticker = market.get('ticker')
            
            # Skip if market is closed or paused
            if market.get('status') != 'open':
                continue
            
            # Get orderbook
            orderbook = self.client.get_orderbook(ticker)
            if not orderbook:
                continue
            
            # Check yes/no price inefficiency
            signal = self._check_yes_no_arbitrage(ticker, orderbook)
            if signal:
                signals.append(signal)
            
            # Check bid-ask spread opportunities
            signal = self._check_spread_opportunity(ticker, orderbook)
            if signal:
                signals.append(signal)
        
        return signals
    
    def _check_yes_no_arbitrage(self, ticker, orderbook):
        """Check if yes + no prices sum correctly."""
        yes_prices = orderbook.get('yes', [])
        no_prices = orderbook.get('no', [])
        
        if not yes_prices or not no_prices:
            return None
        
        # Get best bid prices
        best_yes_bid = max([p['price'] for p in yes_prices if p['price']], default=None)
        best_no_bid = max([p['price'] for p in no_prices if p['price']], default=None)
        
        if not best_yes_bid or not best_no_bid:
            return None
        
        # Yes + No should equal 100 cents
        total = best_yes_bid + best_no_bid
        
        # If total < 100, there's free money (buy both)
        if total < 100 - self.min_edge:
            edge = 100 - total
            logger.info(f"💰 Arbitrage found on {ticker}: {edge} cent edge")
            
            return {
                'ticker': ticker,
                'action': 'buy',
                'side': 'yes',  # We'd buy both, but start with yes
                'count': 10,
                'reason': f'Yes+No arbitrage: {total} cents (edge: {edge})',
                'confidence': min(edge / 10, 1.0),  # Higher edge = higher confidence
                'type': 'arbitrage'
            }
        
        return None
    
    def _check_spread_opportunity(self, ticker, orderbook):
        """Check for large bid-ask spreads we can profit from."""
        yes_prices = orderbook.get('yes', [])
        
        if not yes_prices or len(yes_prices) < 2:
            return None
        
        # Get best bid and ask
        yes_bids = [p for p in yes_prices if p.get('type') == 'bid']
        yes_asks = [p for p in yes_prices if p.get('type') == 'ask']
        
        if not yes_bids or not yes_asks:
            return None
        
        best_bid = max([p['price'] for p in yes_bids])
        best_ask = min([p['price'] for p in yes_asks])
        
        spread = best_ask - best_bid
        
        # If spread is large, we can potentially profit by market making
        if spread > self.min_edge * 2:
            logger.info(f"📈 Large spread on {ticker}: {spread} cents")
            
            # Buy at bid, sell at ask
            return {
                'ticker': ticker,
                'action': 'buy',
                'side': 'yes',
                'count': 5,
                'reason': f'Spread trading: {spread} cent spread',
                'confidence': min(spread / 20, 0.8),
                'type': 'spread'
            }
        
        return None
    
    def execute(self, signal, dry_run=False):
        """Execute arbitrage trade."""
        if not self.can_execute(signal):
            return None
        
        self.log_signal(signal)
        
        # Create order
        order = self.client.create_order(
            ticker=signal['ticker'],
            action=signal['action'],
            side=signal['side'],
            count=signal['count'],
            order_type='market',
            dry_run=dry_run
        )
        
        if order and not dry_run:
            # Update risk manager
            self.risk_manager.update_position(
                signal['ticker'],
                signal['count'],
                signal['side']
            )
            
            # Log trade to local file
            log_trade({
                'strategy': self.name,
                'signal': signal,
                'order': order
            })
            
            # Log trade to Supabase
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
