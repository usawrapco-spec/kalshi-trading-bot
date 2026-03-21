"""Base strategy class for all trading strategies."""

from abc import ABC, abstractmethod
from utils.logger import setup_logger

logger = setup_logger('strategy')


class BaseStrategy(ABC):
    """Abstract base class for trading strategies."""

    def __init__(self, client, risk_manager, db=None):
        self.client = client
        self.risk_manager = risk_manager
        self.db = db
        self.name = self.__class__.__name__
        logger.info(f"Initialized strategy: {self.name}")

    @abstractmethod
    def analyze(self, markets):
        """
        Analyze markets and return trading signals.

        Each signal must include:
            ticker, action, side, count, reason, confidence (0-100)
        """
        pass

    @abstractmethod
    def execute(self, signal, dry_run=False):
        """Execute a trading signal."""
        pass

    def can_execute(self, signal):
        """Check risk rules including confidence threshold."""
        ticker = signal.get('ticker')
        count = signal.get('count', 0)
        confidence = signal.get('confidence', 0)

        if not self.risk_manager.can_trade(ticker, count, confidence=confidence):
            logger.warning(f"Risk manager blocked trade for {ticker} (confidence={confidence:.0f})")
            return False
        return True

    def log_signal(self, signal):
        logger.info(
            f"{self.name} Signal: "
            f"{signal['action'].upper()} {signal['count']} "
            f"{signal['side'].upper()} {signal['ticker']} "
            f"(confidence: {signal.get('confidence', 0):.0f}/100) "
            f"- {signal.get('reason', 'No reason')}"
        )
