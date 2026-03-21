"""CrossPlatformEdge strategy - compares Kalshi prices to Polymarket for cross-platform arbitrage.

Fetches Polymarket's free public API (no auth needed) and matches markets
to Kalshi by title/keyword similarity. When the same event is priced
differently on both platforms (>5% gap), trades on Kalshi using Polymarket
as a "second opinion" on fair value.
"""

import re
import requests
from difflib import SequenceMatcher
from strategies.base import BaseStrategy
from utils.logger import setup_logger
from utils.market_helpers import get_yes_price, get_volume

logger = setup_logger('cross_platform')

POLYMARKET_URL = 'https://gamma-api.polymarket.com/markets'
MIN_EDGE = 0.05  # 5% minimum price difference to trade
MIN_SIMILARITY = 0.40  # Title similarity threshold for matching
MAX_MATCHES = 20  # Max markets to compare per cycle


def normalize_title(title):
    """Normalize a market title for comparison."""
    if not title:
        return ''
    t = title.lower()
    # Remove common filler words and punctuation
    t = re.sub(r'[^a-z0-9\s]', ' ', t)
    t = re.sub(r'\b(will|the|be|a|an|in|on|of|to|by|before|after|for|is|it|this|that)\b', ' ', t)
    t = re.sub(r'\s+', ' ', t).strip()
    return t


def title_similarity(a, b):
    """Calculate similarity between two market titles."""
    na, nb = normalize_title(a), normalize_title(b)
    if not na or not nb:
        return 0.0
    # Check for key phrase overlap first
    words_a = set(na.split())
    words_b = set(nb.split())
    if len(words_a) < 2 or len(words_b) < 2:
        return 0.0
    overlap = words_a & words_b
    # Need at least 2 meaningful words in common
    if len(overlap) < 2:
        return 0.0
    return SequenceMatcher(None, na, nb).ratio()


class CrossPlatformEdgeStrategy(BaseStrategy):
    """Compare Kalshi vs Polymarket prices, trade on Kalshi when edge >5%."""

    def __init__(self, client, risk_manager, db):
        super().__init__(client, risk_manager, db)
        self._poly_cache = []
        self._poly_cache_time = None
        logger.info("CrossPlatformEdge initialized (Polymarket price comparison, >5% edge)")

    def analyze(self, markets):
        signals = []

        # Fetch Polymarket data (cache for 5 min)
        poly_markets = self._fetch_polymarket()
        if not poly_markets:
            logger.info("CrossPlatform: 0 Polymarket markets fetched")
            return signals

        logger.info(f"CrossPlatform: {len(poly_markets)} Polymarket markets, matching against {len(markets)} Kalshi markets")

        # Build Polymarket lookup: normalized title -> {title, price, volume}
        poly_lookup = []
        for pm in poly_markets:
            question = pm.get('question') or pm.get('title') or ''
            # Polymarket prices: outcomePrices is a JSON string like "[\"0.65\",\"0.35\"]"
            price = self._extract_poly_price(pm)
            if price is None or price <= 0:
                continue
            poly_lookup.append({
                'title': question,
                'normalized': normalize_title(question),
                'price': price,
                'volume': float(pm.get('volume', 0) or 0),
                'slug': pm.get('slug', ''),
            })

        if not poly_lookup:
            logger.info("CrossPlatform: 0 Polymarket markets with valid prices")
            return signals

        # Match Kalshi markets to Polymarket
        matches = 0
        for m in markets:
            kalshi_title = m.get('title') or ''
            kalshi_price = get_yes_price(m)
            if kalshi_price <= 0.02 or kalshi_price >= 0.98:
                continue  # Skip near-resolved

            best_match = None
            best_sim = 0
            for pm in poly_lookup:
                sim = title_similarity(kalshi_title, pm['title'])
                if sim > best_sim and sim >= MIN_SIMILARITY:
                    best_sim = sim
                    best_match = pm

            if not best_match:
                continue

            matches += 1
            poly_price = best_match['price']
            edge = poly_price - kalshi_price  # Positive = Kalshi underpriced vs Polymarket

            if abs(edge) < MIN_EDGE:
                continue

            ticker = m.get('ticker', '')

            if edge > MIN_EDGE:
                # Polymarket says higher -> buy YES on Kalshi
                side = 'yes'
                model_prob = poly_price
            else:
                # Polymarket says lower -> buy NO on Kalshi
                side = 'no'
                edge = -edge
                model_prob = 1 - poly_price

            confidence = min(45 + edge * 150 + best_sim * 20, 100)

            logger.info(
                f"CrossPlatform: {ticker} Kalshi={kalshi_price:.2f} Polymarket={poly_price:.2f} "
                f"edge={edge:+.2f} sim={best_sim:.2f} -> BUY {side.upper()} "
                f"(\"{kalshi_title[:50]}\" ~ \"{best_match['title'][:50]}\")"
            )

            signals.append({
                'ticker': ticker,
                'title': kalshi_title,
                'action': 'buy',
                'side': side,
                'count': 3,
                'confidence': confidence,
                'strategy_type': 'cross_platform',
                'edge': edge,
                'model_prob': model_prob,
                'reason': (
                    f"CrossPlatform: Kalshi={kalshi_price:.0%} vs Polymarket={poly_price:.0%}, "
                    f"edge={edge:+.0%}, similarity={best_sim:.0%}"
                ),
            })

            if len(signals) >= MAX_MATCHES:
                break

        logger.info(f"CrossPlatform: {matches} title matches, {len(signals)} signals with edge>{MIN_EDGE:.0%}")
        return signals

    def _fetch_polymarket(self):
        """Fetch active markets from Polymarket's free public API."""
        import time as _time
        now = _time.time()
        # Cache for 5 minutes
        if self._poly_cache and self._poly_cache_time and (now - self._poly_cache_time) < 300:
            return self._poly_cache

        try:
            resp = requests.get(POLYMARKET_URL, params={
                'active': 'true',
                'closed': 'false',
                'limit': 100,
                'order': 'volume',
                'ascending': 'false',
            }, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            # API may return a list directly or nested
            if isinstance(data, list):
                self._poly_cache = data
            elif isinstance(data, dict):
                self._poly_cache = data.get('markets', data.get('data', []))
            else:
                self._poly_cache = []
            self._poly_cache_time = now
            logger.info(f"CrossPlatform: fetched {len(self._poly_cache)} Polymarket markets")
            return self._poly_cache
        except Exception as e:
            logger.error(f"CrossPlatform: Polymarket fetch failed: {e}")
            return self._poly_cache or []

    def _extract_poly_price(self, pm):
        """Extract YES price from Polymarket market data."""
        # Try outcomePrices field (JSON string like '["0.65","0.35"]')
        prices_str = pm.get('outcomePrices')
        if prices_str:
            try:
                import json
                prices = json.loads(prices_str) if isinstance(prices_str, str) else prices_str
                if prices and len(prices) > 0:
                    return float(prices[0])
            except (json.JSONDecodeError, ValueError, IndexError):
                pass

        # Try direct price fields
        for f in ('bestBid', 'lastTradePrice', 'price', 'yes_price'):
            v = pm.get(f)
            if v is not None:
                try:
                    fv = float(v)
                    if 0 < fv <= 1:
                        return fv
                except (ValueError, TypeError):
                    pass

        return None

    def execute(self, signal, dry_run=False):
        if not self.can_execute(signal):
            return None
        self.log_signal(signal)
        return self.client.create_order(
            ticker=signal['ticker'], action='buy', side=signal['side'],
            count=signal['count'], order_type='market', dry_run=dry_run,
        )
