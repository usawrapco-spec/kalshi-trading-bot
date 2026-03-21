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
MIN_SIMILARITY = 0.70  # 70% title similarity required
MIN_SHARED_WORDS = 3   # At least 3 meaningful words in common
MAX_MATCHES = 20

# Words to exclude from keyword matching (not meaningful for matching events)
STOP_WORDS = {
    'will', 'the', 'be', 'a', 'an', 'in', 'on', 'of', 'to', 'by',
    'before', 'after', 'for', 'is', 'it', 'this', 'that', 'or', 'and',
    'at', 'not', 'no', 'yes', 'any', 'has', 'have', 'been', 'was',
    'are', 'do', 'does', 'did', 'than', 'more', 'most', 'next',
}

# Category keywords for same-category matching
CATEGORY_MAP = {
    'politics': ['president', 'election', 'senate', 'congress', 'trump', 'biden', 'democrat', 'republican', 'party', 'governor', 'vote', 'poll', 'pardon', 'impeach'],
    'weather': ['temperature', 'temp', 'weather', 'high', 'low', 'degrees', 'fahrenheit', 'rain', 'snow'],
    'sports': ['nba', 'nfl', 'ncaa', 'mlb', 'nhl', 'game', 'score', 'championship', 'playoff', 'match', 'win', 'beat'],
    'crypto': ['bitcoin', 'btc', 'ethereum', 'eth', 'crypto', 'solana'],
    'finance': ['s&p', 'sp500', 'nasdaq', 'dow', 'stock', 'market', 'fed', 'rate', 'gdp', 'inflation', 'cpi'],
    'world': ['ukraine', 'russia', 'china', 'taiwan', 'nato', 'war', 'sanctions', 'treaty'],
}


def get_meaningful_words(title):
    """Extract meaningful words from a title, excluding stop words."""
    if not title:
        return set()
    t = re.sub(r'[^a-z0-9\s]', ' ', title.lower())
    return {w for w in t.split() if w not in STOP_WORDS and len(w) > 1}


def detect_category(title):
    """Detect the category of a market by keyword matching."""
    words = get_meaningful_words(title)
    for cat, keywords in CATEGORY_MAP.items():
        if any(kw in words or kw in title.lower() for kw in keywords):
            return cat
    return 'other'


def title_similarity(a, b):
    """Calculate similarity requiring 3+ shared meaningful words and 70%+ match."""
    words_a = get_meaningful_words(a)
    words_b = get_meaningful_words(b)
    if len(words_a) < 2 or len(words_b) < 2:
        return 0.0, set()
    overlap = words_a & words_b
    if len(overlap) < MIN_SHARED_WORDS:
        return 0.0, overlap
    # SequenceMatcher on the full normalized strings
    na = ' '.join(sorted(words_a))
    nb = ' '.join(sorted(words_b))
    sim = SequenceMatcher(None, na, nb).ratio()
    return sim, overlap


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

        # Build Polymarket lookup with category detection
        poly_lookup = []
        for pm in poly_markets:
            question = pm.get('question') or pm.get('title') or ''
            price = self._extract_poly_price(pm)
            if price is None or price <= 0:
                continue
            poly_lookup.append({
                'title': question,
                'price': price,
                'volume': float(pm.get('volume', 0) or 0),
                'category': detect_category(question),
            })

        if not poly_lookup:
            logger.info("CrossPlatform: 0 Polymarket markets with valid prices")
            return signals

        # Match Kalshi markets to Polymarket (same category + 70% similarity + 3 shared words)
        matches = 0
        rejected_cat = 0
        rejected_sim = 0
        for m in markets:
            kalshi_title = m.get('title') or ''
            kalshi_price = get_yes_price(m)
            if kalshi_price <= 0.02 or kalshi_price >= 0.98:
                continue

            kalshi_cat = detect_category(kalshi_title)

            best_match = None
            best_sim = 0
            best_overlap = set()
            for pm in poly_lookup:
                # Must be same category (or both 'other')
                if kalshi_cat != pm['category']:
                    continue

                sim, overlap = title_similarity(kalshi_title, pm['title'])
                if sim > best_sim and sim >= MIN_SIMILARITY:
                    best_sim = sim
                    best_match = pm
                    best_overlap = overlap

            if not best_match:
                continue

            matches += 1
            poly_price = best_match['price']
            edge = poly_price - kalshi_price

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

            shared = ', '.join(sorted(best_overlap)[:5])
            logger.info(
                f"CrossPlatform MATCH [{kalshi_cat}]: sim={best_sim:.0%} shared=[{shared}]\n"
                f"  Kalshi:     \"{kalshi_title}\" @ ${kalshi_price:.2f}\n"
                f"  Polymarket: \"{best_match['title']}\" @ ${poly_price:.2f}\n"
                f"  -> BUY {side.upper()} edge={edge:+.0%}"
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
