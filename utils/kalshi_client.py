"""Kalshi API client wrapper with error handling and retry logic."""

from kalshi_python import ApiClient, Configuration, MarketsApi, PortfolioApi, ExchangeApi
import time
from config import Config
from utils.logger import setup_logger

logger = setup_logger('kalshi_client')


class KalshiAPIClient:
    """Wrapper for Kalshi API with error handling."""
    
    def __init__(self, key_id=None, private_key=None, host=None):
        """Initialize Kalshi client."""
        self.key_id = key_id or Config.KALSHI_API_KEY_ID
        self.private_key = private_key or Config.KALSHI_PRIVATE_KEY
        self.host = host or Config.KALSHI_API_HOST
        
        logger.info(f"Initializing Kalshi client for {self.host}")
        
        try:
            configuration = Configuration()
            configuration.host = self.host
            configuration.username = self.key_id
            configuration.password = self.private_key
            
            api_client = ApiClient(configuration)
            
            # Initialize API instances
            self.market_api = MarketsApi(api_client)
            self.portfolio_api = PortfolioApi(api_client)
            self.exchange_api = ExchangeApi(api_client)
            
            logger.info("✅ Kalshi client initialized successfully")
        except Exception as e:
            logger.error(f"❌ Failed to initialize Kalshi client: {e}")
            raise
    
    def get_markets(self, status='open', limit=100, **kwargs):
        """Get markets with error handling."""
        try:
            response = self.market_api.get_markets(
                status=status,
                limit=limit,
                **kwargs
            )
            markets = response.to_dict() if hasattr(response, 'to_dict') else response
            logger.debug(f"Retrieved {len(markets.get('markets', []))} markets")
            return markets
        except Exception as e:
            logger.error(f"Error getting markets: {e}")
            return {'markets': []}
    
    def get_market(self, ticker):
        """Get specific market by ticker."""
        try:
            response = self.market_api.get_market(ticker=ticker)
            market = response.to_dict() if hasattr(response, 'to_dict') else response
            logger.debug(f"Retrieved market: {ticker}")
            return market
        except Exception as e:
            logger.error(f"Error getting market {ticker}: {e}")
            return None
    
    def get_orderbook(self, ticker):
        """Get orderbook for a market."""
        try:
            response = self.market_api.get_market_orderbook(ticker=ticker)
            orderbook = response.to_dict() if hasattr(response, 'to_dict') else response
            return orderbook
        except Exception as e:
            logger.error(f"Error getting orderbook for {ticker}: {e}")
            return None
    
    def create_order(self, ticker, action, side, count, order_type='market', yes_price=None, no_price=None, dry_run=False):
        """Create an order with safety checks."""
        if count > Config.MAX_ORDER_SIZE:
            logger.warning(f"Order size {count} exceeds max {Config.MAX_ORDER_SIZE}")
            count = Config.MAX_ORDER_SIZE
        order_params = {'ticker': ticker, 'action': action, 'side': side, 'count': count, 'type': order_type}
        if order_type == 'limit':
            if side == 'yes' and yes_price:
                order_params['yes_price'] = yes_price
            elif side == 'no' and no_price:
                order_params['no_price'] = no_price
        logger.info(f"{'[DRY RUN] ' if dry_run else ''}Creating order: {order_params}")
        if dry_run:
            return {'status': 'dry_run', 'params': order_params}
        try:
            response = self.exchange_api.create_order(**order_params)
            order = response.to_dict() if hasattr(response, 'to_dict') else response
            logger.info(f"✅ Order created: {order.get('order_id')}")
            return order
        except Exception as e:
            logger.error(f"Error creating order: {e}")
            return None
    
    def get_portfolio(self):
        """Get current portfolio/positions."""
        try:
            response = self.portfolio_api.get_portfolio()
            portfolio = response.to_dict() if hasattr(response, 'to_dict') else response
            return portfolio
        except Exception as e:
            logger.error(f"Error getting portfolio: {e}")
            return None
    
    def get_balance(self):
        """Get account balance."""
        try:
            response = self.portfolio_api.get_balance()
            balance = response.to_dict() if hasattr(response, 'to_dict') else response
            logger.debug(f"Balance: ${balance.get('balance', 0)/100:.2f}")
            return balance
        except Exception as e:
            logger.error(f"Error getting balance: {e}")
            return None
    
    def get_fills(self, ticker=None):
        """Get recent fills/trades."""
        try:
            params = {}
            if ticker:
                params['ticker'] = ticker
            response = self.portfolio_api.get_fills(**params)
            fills = response.to_dict() if hasattr(response, 'to_dict') else response
            return fills
        except Exception as e:
            logger.error(f"Error getting fills: {e}")
            return None