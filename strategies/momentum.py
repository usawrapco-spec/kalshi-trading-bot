"""Momentum trading strategy - follows strong price movements."""

from strategies.base import BaseStrategy
from utils.logger import setup_logger, log_trade
import time

logger = setup_logger('momentum_strategy')


class MomentumStrategy(BaseStrategy):
    """
    Momentum strategy that identifies and trades strong price movements.
    
    Looks for:
    1. Rapid price changes
    2. High volume surges
    3. Strong directional movement
    """
    
    def __init__(self, client, risk_manager, price_change_threshold=0.05):
        """
        Initialize momentum strategy.
        
        Args:
            price_change_threshold: Minimum price change % to trigger (e.g., 0.05 = 5%)
        """
        super().__init__(client, risk_manager)
        self.price_change_threshold = price_change_threshold
        self.price_history = {}  # Track price changes over time
        logger.info(f"Momentum strategy initialized (threshold: {price_change_threshold})")
    
    def analyze(self, markets):
        """Find momentum trading opportunities."""
        signals = []
        
        for market in markets:
            ticker = market.get('ticker')
            
            # Skip if market is closed
            if market.get('status') != 'open':
                continue
            
            # Get current price
            current_price = market.get('yes_bid', 0)  # Using yes_bid as proxy for "current price"
            
            if not current_price:
                continue
            
            # Track price history
            if ticker not in self.price_history:
                self.price_history[ticker] = {
                    'prices': [current_price],
                    'timestamps': [time.time()]
                }
                continue
            
            # Add current price to history
            history = self.price_history[ticker]
            history['prices'].append(current_price)
            history['timestamps'].append(time.time())
            
            # Keep only recent history (last 10 data points)
            if len(history['prices']) > 10:
                history['prices'] = history['prices'][-10:]
                history['timestamps'] = history['timestamps'][-10:]
            
            # Need at least 3 data points for momentum
            if len(history['prices']) < 3:
                continue
            
            # Check for momentum
            signal = self._check_momentum(ticker, history)
            if signal:
                signals.append(signal)
        
        return signals
    
    def _check_momentum(self, ticker, history):
        """Check if there's strong momentum."""
        prices = history['prices']
        
        # Calculate price change
        old_price = prices[0]
        current_price = prices[-1]
        
        if old_price == 0:
            return None
        
        price_change = (current_price - old_price) / old_price
        
        # Check if price change exceeds threshold
        if abs(price_change) < self.price_change_threshold:
            return None
        
        # Determine direction
        is_upward = price_change > 0
        
        # Check if momentum is consistent (not just noise)
        consecutive_moves = 0
        for i in range(1, len(prices)):
            if is_upward:
                if prices[i] > prices[i-1]:
                    consecutive_moves += 1
            else:
                if prices[i] < prices[i-1]:
                    consecutive_moves += 1
        
        # Need at least 60% consistency
        consistency = consecutive_moves / (len(prices) - 1)
        
        if consistency < 0.6:
            return None
        
        logger.info(
            f"🚀 Momentum detected on {ticker}: "
            f"{price_change*100:.1f}% change, "
            f"{consistency*100:.0f}% consistency"
        )
        
        # Generate signal
        return {
            'ticker': ticker,
            'action': 'buy',
            'side': 'yes' if is_upward else 'no',
            'count': 10,
            'reason': f'Momentum: {price_change*100:.1f}% {"up" if is_upward else "down"}',
            'confidence': min(abs(price_change) * 5, 0.9),  # Higher change = higher confidence
            'type': 'momentum'
        }
    
    def execute(self, signal, dry_run=False):
        """Execute momentum trade."""
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
