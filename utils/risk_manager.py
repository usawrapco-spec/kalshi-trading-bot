"""Risk management for trading bot."""

from datetime import datetime
from config import Config
from utils.logger import setup_logger

logger = setup_logger('risk_manager')

# Confidence threshold: only execute trades with confidence >= 70
MIN_CONFIDENCE = 70
# Never more than 3 open positions
MAX_OPEN_POSITIONS = 3
# Max 20% of balance in a single trade
MAX_BALANCE_PERCENT_PER_TRADE = 0.20
# Stop trading if paper P&L drops below this
DAILY_LOSS_STOP = -2.0  # dollars


class RiskManager:
    """Manages trading risk and position limits."""

    def __init__(self):
        self.daily_pnl = 0
        self.positions = {}
        self.daily_reset_time = datetime.now().date()
        self.trades_today = 0
        self.current_balance_cents = 0  # Updated each cycle
        self.stopped_for_day = False
        logger.info(
            f"Risk manager initialized: "
            f"min_confidence={MIN_CONFIDENCE}, "
            f"max_positions={MAX_OPEN_POSITIONS}, "
            f"max_per_trade={MAX_BALANCE_PERCENT_PER_TRADE:.0%}, "
            f"daily_loss_stop=${abs(DAILY_LOSS_STOP)}"
        )

    def set_balance(self, balance_cents):
        """Update current balance (called each cycle)."""
        self.current_balance_cents = balance_cents

    def check_daily_loss_limit(self):
        self._reset_if_new_day()

        if self.stopped_for_day:
            return False

        if self.daily_pnl <= DAILY_LOSS_STOP:
            logger.warning(f"Daily loss stop hit: ${self.daily_pnl:.2f} <= ${DAILY_LOSS_STOP}")
            self.stopped_for_day = True
            return False

        if abs(self.daily_pnl) >= Config.MAX_DAILY_LOSS:
            logger.warning(f"Daily loss limit reached: ${abs(self.daily_pnl):.2f}")
            self.stopped_for_day = True
            return False

        return True

    def check_position_size(self, ticker, additional_contracts):
        current_size = self.positions.get(ticker, 0)
        new_size = current_size + additional_contracts

        if abs(new_size) > Config.MAX_POSITION_SIZE:
            logger.warning(f"Position size limit for {ticker}: {new_size} > {Config.MAX_POSITION_SIZE}")
            return False
        return True

    def check_order_size(self, count):
        if count > Config.MAX_ORDER_SIZE:
            logger.warning(f"Order size {count} exceeds max {Config.MAX_ORDER_SIZE}")
            return False
        return True

    def check_max_positions(self):
        """Check if we already have the max number of open positions."""
        active = sum(1 for v in self.positions.values() if v != 0)
        if active >= MAX_OPEN_POSITIONS:
            logger.warning(f"Max open positions reached: {active}/{MAX_OPEN_POSITIONS}")
            return False
        return True

    def check_trade_size_vs_balance(self, count, price_cents):
        """Ensure single trade doesn't exceed 20% of balance."""
        if self.current_balance_cents <= 0:
            return True  # Can't check without balance data
        trade_cost = count * price_cents
        max_allowed = self.current_balance_cents * MAX_BALANCE_PERCENT_PER_TRADE
        if trade_cost > max_allowed:
            logger.warning(
                f"Trade cost ${trade_cost / 100:.2f} exceeds "
                f"{MAX_BALANCE_PERCENT_PER_TRADE:.0%} of balance "
                f"(${max_allowed / 100:.2f})"
            )
            return False
        return True

    def check_confidence(self, confidence):
        """Only allow trades with confidence >= MIN_CONFIDENCE."""
        if confidence < MIN_CONFIDENCE:
            logger.info(f"Signal rejected: confidence {confidence:.0f} < {MIN_CONFIDENCE}")
            return False
        return True

    def can_trade(self, ticker, count, confidence=0, price_cents=50):
        """Master check - can we execute this trade?"""
        checks = [
            ("Daily loss limit", self.check_daily_loss_limit()),
            ("Confidence threshold", self.check_confidence(confidence)),
            ("Max open positions", self.check_max_positions()),
            ("Position size", self.check_position_size(ticker, count)),
            ("Order size", self.check_order_size(count)),
            ("Trade size vs balance", self.check_trade_size_vs_balance(count, price_cents)),
        ]

        for check_name, passed in checks:
            if not passed:
                logger.warning(f"Trade blocked: {check_name}")
                return False

        return True

    def max_contracts_for_balance(self, price_cents):
        """Calculate max contracts we can buy within the 20% balance rule."""
        if self.current_balance_cents <= 0 or price_cents <= 0:
            return Config.MAX_ORDER_SIZE
        max_cost = self.current_balance_cents * MAX_BALANCE_PERCENT_PER_TRADE
        return max(1, min(int(max_cost / price_cents), Config.MAX_ORDER_SIZE))

    def update_position(self, ticker, count, side):
        multiplier = 1 if side == 'yes' else -1
        self.positions[ticker] = self.positions.get(ticker, 0) + (count * multiplier)
        self.trades_today += 1
        logger.info(f"Position updated: {ticker} = {self.positions[ticker]} contracts")

    def update_pnl(self, pnl):
        self._reset_if_new_day()
        self.daily_pnl += pnl
        logger.info(f"Daily P&L: ${self.daily_pnl:.2f}")

    def _reset_if_new_day(self):
        today = datetime.now().date()
        if today > self.daily_reset_time:
            logger.info("New trading day - resetting counters")
            self.daily_pnl = 0
            self.trades_today = 0
            self.daily_reset_time = today
            self.stopped_for_day = False

    def get_status(self):
        return {
            'daily_pnl': self.daily_pnl,
            'trades_today': self.trades_today,
            'positions': self.positions,
            'can_trade': self.check_daily_loss_limit(),
            'stopped_for_day': self.stopped_for_day,
        }

    def log_status(self):
        status = self.get_status()
        active = sum(1 for v in status['positions'].values() if v != 0)
        logger.info("=" * 50)
        logger.info("RISK STATUS")
        logger.info(f"Daily P&L: ${status['daily_pnl']:.2f}")
        logger.info(f"Trades Today: {status['trades_today']}")
        logger.info(f"Active Positions: {active}/{MAX_OPEN_POSITIONS}")
        logger.info(f"Stopped for day: {status['stopped_for_day']}")
        for ticker, size in status['positions'].items():
            if size != 0:
                logger.info(f"  {ticker}: {size} contracts")
        logger.info("=" * 50)
