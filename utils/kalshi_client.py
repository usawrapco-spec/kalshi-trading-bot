"""Kalshi API client using direct REST API calls with RSA-PSS auth."""

import base64
import time
import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from config import Config
from utils.logger import setup_logger

logger = setup_logger('kalshi_client')


class KalshiAPIClient:
    """Direct REST API wrapper for Kalshi with RSA-PSS authentication."""

    def __init__(self, key_id=None, private_key_str=None, host=None):
        """Initialize Kalshi client."""
        self.key_id = key_id or Config.KALSHI_API_KEY_ID
        self.host = (host or Config.KALSHI_API_HOST).rstrip('/')
        self.session = requests.Session()

        # Load RSA private key
        raw_key = private_key_str or Config.KALSHI_PRIVATE_KEY
        self._private_key = self._load_private_key(raw_key)
        logger.info(f"Kalshi client initialized for {self.host}")

    def _load_private_key(self, raw_key):
        import re
        key_str = raw_key.strip().strip('"').strip("'")

        # Replace literal \n with real newlines
        key_str = key_str.replace('\\n', '\n')

        # Remove any stray backslashes
        key_str = key_str.replace('\\', '')

        # Extract the key type and base64 content between BEGIN and END
        match = re.search(r'-----BEGIN ([A-Z ]+)-----(.+?)-----END \1-----', key_str, re.DOTALL)
        if not match:
            raise ValueError("Could not find PEM header/footer in key")

        key_type = match.group(1)  # e.g. "RSA PRIVATE KEY", "EC PRIVATE KEY", "PRIVATE KEY"
        header = f"-----BEGIN {key_type}-----"
        footer = f"-----END {key_type}-----"
        base64_content = match.group(2)

        # Remove ALL whitespace and non-base64 characters from the middle
        base64_clean = re.sub(r'[^A-Za-z0-9+/=]', '', base64_content)

        # Rebuild proper PEM with 64-char lines
        lines = [base64_clean[i:i+64] for i in range(0, len(base64_clean), 64)]
        pem_key = header + "\n" + "\n".join(lines) + "\n" + footer

        logger.info(f"Key loaded: type={key_type}, base64_len={len(base64_clean)}")

        return serialization.load_pem_private_key(pem_key.encode('utf-8'), password=None)

    def _sign_request(self, method, path):
        """Create auth headers with RSA-PSS signature for a request."""
        timestamp = str(int(time.time() * 1000))
        path_without_query = path.split('?')[0]
        message = f"{timestamp}{method}{path_without_query}".encode('utf-8')
        signature = self._private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH
            ),
            hashes.SHA256()
        )
        return {
            'KALSHI-ACCESS-KEY': self.key_id,
            'KALSHI-ACCESS-TIMESTAMP': timestamp,
            'KALSHI-ACCESS-SIGNATURE': base64.b64encode(signature).decode('utf-8'),
            'Content-Type': 'application/json',
        }

    def _request(self, method, path, **kwargs):
        """Make an authenticated request to the Kalshi API."""
        url = f"{self.host}{path}"
        headers = self._sign_request(method, path)
        response = self.session.request(method, url, headers=headers, **kwargs)
        if response.status_code == 401:
            logger.error(f"401 Unauthorized: {method} {url}")
            logger.error(f"Response: {response.text}")
            logger.error(f"Key ID used: {self.key_id[:8]}...")
        response.raise_for_status()
        return response.json()
    
    def get_markets(self, status='open', limit=100, **kwargs):
        """Get markets."""
        try:
            params = {'status': status, 'limit': limit, **kwargs}
            data = self._request('GET', '/trade-api/v2/markets', params=params)
            logger.debug(f"Retrieved {len(data.get('markets', []))} markets")
            return data
        except Exception as e:
            logger.error(f"Error getting markets: {e}")
            return {'markets': []}

    def get_markets_by_series(self, series_ticker, status='open'):
        """Get markets for a specific series (e.g. KXHIGHNY)."""
        try:
            params = {'series_ticker': series_ticker, 'status': status, 'limit': 100}
            data = self._request('GET', '/trade-api/v2/markets', params=params)
            return data.get('markets', [])
        except Exception as e:
            logger.error(f"Error getting series {series_ticker}: {e}")
            return []

    def get_market(self, ticker):
        """Get specific market."""
        try:
            return self._request('GET', f'/trade-api/v2/markets/{ticker}')
        except Exception as e:
            logger.error(f"Error getting market {ticker}: {e}")
            return None

    def get_orderbook(self, ticker):
        """Get orderbook."""
        try:
            return self._request('GET', f'/trade-api/v2/markets/{ticker}/orderbook')
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
            order = self._request('POST', '/trade-api/v2/orders', json=order_data)
            logger.info(f"Order created: {order.get('order_id')}")
            return order
        except Exception as e:
            logger.error(f"Error creating order: {e}")
            return None

    def get_portfolio(self):
        """Get portfolio."""
        try:
            return self._request('GET', '/trade-api/v2/portfolio')
        except Exception as e:
            logger.error(f"Error getting portfolio: {e}")
            return None

    def get_balance(self):
        """Get balance."""
        try:
            balance = self._request('GET', '/trade-api/v2/portfolio/balance')
            logger.debug(f"Balance: ${balance.get('balance', 0)/100:.2f}")
            return balance
        except Exception as e:
            logger.error(f"Error getting balance: {e}")
            return None

    def get_fills(self, ticker=None):
        """Get fills."""
        try:
            params = {'ticker': ticker} if ticker else {}
            return self._request('GET', '/trade-api/v2/portfolio/fills', params=params)
        except Exception as e:
            logger.error(f"Error getting fills: {e}")
            return None