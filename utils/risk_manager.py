"""Risk management for trading bot."""

from datetime import datetime, timedelta
from config import Config
from utils.logger import setup_logger

logger = setup_logger('risk_manager')


class RiskManager:
    """Manages trading risk and position limits."""
    
    def __init__(self):
        """Initialize risk manager."""
        self.daily_pnl = 0
        self.positions = {}
        self.daily_reset_time = datetime.now().date()
        self.trades_today = 0
        logger.info("Risk manager initialized")
    
    def check_daily_loss_limit(self):
        """Check if daily loss limit has been exceeded."""
        self._reset_if_new_day()
        
        if abs(self.daily_pnl) >= Config.MAX_DAILY_LOSS:
            logger.warning(f"⚠️  Daily loss limit reached: ${abs(self.daily_pnl)}")
            return False
        return True
    
    def check_position_size(self, ticker, additional_contracts):
        """Check if adding position would exceed limits."""
        current_size = self.positions.get(ticker, 0)
        new_size = current_size + additional_contracts
        
        if abs(new_size) > Config.MAX_POSITION_SIZE:
            logger.warning(
                f"Position size limit exceeded for {ticker}: "
                f"{new_size} > {Config.MAX_POSITION_SIZE}"
            )
            return False
        return True
    
    def check_order_size(self, count):
        """Check if order size is within limits."""
        if count > Config.MAX_ORDER_SIZE:
            logger.warning(
                f"Order size {count} exceeds max {Config.MAX_ORDER_SIZE}"
            )
            return False
        return True
    
    def can_trade(self, ticker, count):
        """Master check - can we execute this trade?"""
        checks = [
            ("Daily loss limit", self.check_daily_loss_limit()),
            ("Position size", self.check_position_size(ticker, count)),
            ("Order size", self.check_order_size(count))
        ]
        
        for check_name, passed in checks:
            if not passed:
                logger.warning(f"❌ Trade blocked: {check_name} check failed")
                return False
        
        return True
    
    def update_position(self, ticker, count, side):
        """Update position tracking after a trade."""
        multiplier = 1 if side == 'yes' else -1
        self.positions[ticker] = self.positions.get(ticker, 0) + (count * multiplier)
        self.trades_today += 1
        logger.info(f"Position updated: {ticker} = {self.positions[ticker]} contracts")
    
    def update_pnl(self, pnl):
        """Update daily P&L."""
        self._reset_if_new_day()
        self.daily_pnl += pnl
        logger.info(f"Daily P&L: ${self.daily_pnl:.2f}")
    
    def _reset_if_new_day(self):
        """Reset daily counters if new day."""
        today = datetime.now().date()
        if today > self.daily_reset_time:
            logger.info("📅 New trading day - resetting counters")
            self.daily_pnl = 0
            self.trades_today = 0
            self.daily_reset_time = today
    
    def get_status(self):
        """Get current risk status."""
        return {
            'daily_pnl': self.daily_pnl,
            'trades_today': self.trades_today,
            'positions': self.positions,
            'can_trade': self.check_daily_loss_limit()
        }
    
    def log_status(self):
        """Log current risk status."""
        status = self.get_status()
        logger.info("=" * 50)
        logger.info("RISK STATUS")
        logger.info(f"Daily P&L: ${status['daily_pnl']:.2f}")
        logger.info(f"Trades Today: {status['trades_today']}")
        logger.info(f"Active Positions: {len(status['positions'])}")
        for ticker, size in status['positions'].items():
            logger.info(f"  {ticker}: {size} contracts")
        logger.info("=" * 50)
