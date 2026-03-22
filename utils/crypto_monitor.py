"""Real-time crypto price monitoring via CoinGecko free API.

Tracks BTC/ETH/SOL prices and calculates momentum + volatility
for the CryptoMomentum strategy. Rate limited to 1 fetch per 30 seconds.
"""

import time
import requests
from collections import deque
from utils.logger import setup_logger

logger = setup_logger('crypto_monitor')

COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price"
MIN_FETCH_INTERVAL = 30  # seconds — CoinGecko free: 30 calls/min


class CryptoPriceMonitor:
    """Track real-time crypto prices and calculate momentum."""

    def __init__(self):
        # Store last 30 minutes of prices (1 per fetch)
        self.price_history = {
            'BTC': deque(maxlen=60),
            'ETH': deque(maxlen=60),
            'SOL': deque(maxlen=60),
        }
        self.last_fetch = 0
        self.last_prices = {}

    def fetch_prices(self):
        """Fetch current prices. Throttled to once per 30 seconds."""
        now = time.time()
        if now - self.last_fetch < MIN_FETCH_INTERVAL:
            return self.last_prices or None

        try:
            resp = requests.get(COINGECKO_URL, params={
                "ids": "bitcoin,ethereum,solana",
                "vs_currencies": "usd",
            }, timeout=5)
            resp.raise_for_status()
            data = resp.json()

            prices = {
                'BTC': data.get('bitcoin', {}).get('usd', 0),
                'ETH': data.get('ethereum', {}).get('usd', 0),
                'SOL': data.get('solana', {}).get('usd', 0),
            }

            for coin, price in prices.items():
                if price > 0:
                    self.price_history[coin].append((now, price))

            self.last_fetch = now
            self.last_prices = prices
            return prices

        except Exception as e:
            logger.debug(f"Crypto price fetch failed: {e}")
            return self.last_prices or None

    def get_momentum(self, coin, minutes=5):
        """Calculate price change over last N minutes. Returns fraction (0.005 = +0.5%)."""
        history = list(self.price_history.get(coin, []))
        if len(history) < 2:
            return 0.0

        now = time.time()
        cutoff = now - (minutes * 60)

        old_price = None
        for ts, price in history:
            if ts >= cutoff:
                old_price = price
                break

        if old_price is None or old_price == 0:
            return 0.0

        current = history[-1][1]
        return (current - old_price) / old_price

    def get_volatility(self, coin, minutes=15):
        """Calculate recent return volatility (std dev)."""
        history = list(self.price_history.get(coin, []))
        if len(history) < 5:
            return 0.0

        now = time.time()
        cutoff = now - (minutes * 60)
        recent = [(ts, p) for ts, p in history if ts >= cutoff]
        if len(recent) < 3:
            return 0.0

        returns = []
        for i in range(1, len(recent)):
            if recent[i - 1][1] > 0:
                ret = (recent[i][1] - recent[i - 1][1]) / recent[i - 1][1]
                returns.append(ret)

        if not returns:
            return 0.0

        mean = sum(returns) / len(returns)
        variance = sum((r - mean) ** 2 for r in returns) / len(returns)
        return variance ** 0.5
