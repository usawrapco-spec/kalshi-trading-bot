"""Base strategy class for all trading strategies."""

from abc import ABC, abstractmethod
from utils.logger import setup_logger

logger = setup_logger('strategy')


class BaseStrategy(ABC):
    """Abstract base class for trading strategies."""
    
    def __init__(self, client, risk_manager, db=None):
        """
        Initialize strategy.
        
        Args:
            client: KalshiAPIClient instance
            risk_manager: RiskManager instance
            db: SupabaseDB instance (optional)
        """
        self.client = client
        self.risk_manager = risk_manager
        self.db = db
        self.name = self.__class__.__name__
        logger.info(f"Initialized strategy: {self.name}")
    
    @abstractmethod
    def analyze(self, markets):
        """
        Analyze markets and identify trading opportunities.
        
        Args:
            markets: List of market data from Kalshi
            
        Returns:
            List of trading signals: [
                {
                    'ticker': str,
                    'action': 'buy' or 'sell',
                    'side': 'yes' or 'no',
                    'count': int,
                    'reason': str,
                    'confidence': float (0-1)
                }
            ]
        """
        pass
    
    @abstractmethod
    def execute(self, signal, dry_run=False):
        """
        Execute a trading signal.
        
        Args:
            signal: Trading signal dict from analyze()
            dry_run: If True, don't actually place orders
            
        Returns:
            Order result or None
        """
        pass
    
    def can_execute(self, signal):
        """Check if we can execute this trade based on risk rules."""
        ticker = signal.get('ticker')
        count = signal.get('count', 0)
        
        if not self.risk_manager.can_trade(ticker, count):
            logger.warning(f"Risk manager blocked trade for {ticker}")
            return False
        
        return True
    
    def log_signal(self, signal):
        """Log a trading signal."""
        logger.info(
            f"📊 {self.name} Signal: "
            f"{signal['action'].upper()} {signal['count']} "
            f"{signal['side'].upper()} {signal['ticker']} "
            f"(confidence: {signal.get('confidence', 0):.2f}) "
            f"- {signal.get('reason', 'No reason')}"
        )
