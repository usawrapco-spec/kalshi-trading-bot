"""Kalshi API client using direct REST API calls."""

import requests
import time
from config import Config
from utils.logger import setup_logger

logger = setup_logger('kalshi_client')


class KalshiAPIClient:
    """Direct REST API wrapper for Kalshi."""
    
    def __init__(self, key_id=None, private_key=None, host=None):
        """Initialize Kalshi client."""
        self.key_id = key_id or Config.KALSHI_API_KEY_ID
        self.private_key = private_key or Config.KALSHI_PRIVATE_KEY
        self.host = (host or Config.KALSHI_API_HOST).rstrip('/')
        self.session = requests.Session()
        self.session.auth = (self.key_id, self.private_key)
        logger.info(f"✅ Kalshi client initialized for {self.host}")
    
    def get_markets(self, status='open', limit=100, **kwargs):
        """Get markets."""
        try:
            params = {'status': status, 'limit': limit, **kwargs}
            response = self.session.get(f"{self.host}/trade-api/v2/markets", params=params)
            response.raise_for_status()
            data = response.json()
            logger.debug(f"Retrieved {len(data.get('markets', []))} markets")
            return data
        except Exception as e:
            logger.error(f"Error getting markets: {e}")
            return {'markets': []}
    
    def get_market(self, ticker):
        """Get specific market."""
        try:
            response = self.session.get(f"{self.host}/trade-api/v2/markets/{ticker}")
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"Error getting market {ticker}: {e}")
            return None
    
    def get_orderbook(self, ticker):
        """Get orderbook."""
        try:
            response = self.session.get(f"{self.host}/trade-api/v2/markets/{ticker}/orderbook")
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"Error getting orderbook for {ticker}: {e}")
            return None
    
    def create_order(self, ticker, action, side, count, order_type='market', yes_price=None, no_price=None, dry_run=False):
        """Create order."""
        if count > Config.MAX_ORDER_SIZE:
            logger.warning(f"Order size {count} exceeds max {Config.MAX_ORDER_SIZE}")
            count = Config.MAX_ORDER_SIZE
        order_data = {'ticker': ticker, 'action': action, 'side': side, 'count': count, 'type': order_type}
        if order_type == 'limit':
            if side == 'yes' and yes_price:
                order_data['yes_price'] = yes_price
            elif side == 'no' and no_price:
                order_data['no_price'] = no_price
        logger.info(f"{'[DRY RUN] ' if dry_run else ''}Creating order: {order_data}")
        if dry_run:
            return {'status': 'dry_run', 'params': order_data}
        try:
            response = self.session.post(f"{self.host}/trade-api/v2/orders", json=order_data)
            response.raise_for_status()
            order = response.json()
            logger.info(f"✅ Order created: {order.get('order_id')}")
            return order
        except Exception as e:
            logger.error(f"Error creating order: {e}")
            return None
    
    def get_portfolio(self):
        """Get portfolio."""
        try:
            response = self.session.get(f"{self.host}/trade-api/v2/portfolio")
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"Error getting portfolio: {e}")
            return None
    
    def get_balance(self):
        """Get balance."""
        try:
            response = self.session.get(f"{self.host}/trade-api/v2/portfolio/balance")
            response.raise_for_status()
            balance = response.json()
            logger.debug(f"Balance: ${balance.get('balance', 0)/100:.2f}")
            return balance
        except Exception as e:
            logger.error(f"Error getting balance: {e}")
            return None
    
    def get_fills(self, ticker=None):
        """Get fills."""
        try:
            params = {'ticker': ticker} if ticker else {}
            response = self.session.get(f"{self.host}/trade-api/v2/portfolio/fills", params=params)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"Error getting fills: {e}")
            return None