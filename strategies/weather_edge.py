"""WeatherEdge strategy - compares Open-Meteo GFS ensemble forecasts to Kalshi KXHIGH markets.

Inspired by suislanchez/polymarket-kalshi-weather-bot which made $1.8k profit
using ensemble weather forecasts to find mispriced temperature markets.
"""

import re
import requests
from datetime import datetime, timedelta
from strategies.base import BaseStrategy
from utils.logger import setup_logger
from utils.market_helpers import get_yes_price, get_volume
from utils.api_resilience import APIResilience, resilient_strategy

logger = setup_logger('weather_edge')

CITIES = {
    'KXHIGHNY':  {'lat': 40.71, 'lon': -74.01, 'name': 'New York'},
    'KXHIGHCHI': {'lat': 41.88, 'lon': -87.63, 'name': 'Chicago'},
    'KXHIGHMIA': {'lat': 25.76, 'lon': -80.19, 'name': 'Miami'},
    'KXHIGHLAX': {'lat': 34.05, 'lon': -118.24, 'name': 'Los Angeles'},
    'KXHIGHDEN': {'lat': 39.74, 'lon': -104.99, 'name': 'Denver'},
}

WEATHER_KEYWORDS = [
    'temperature', 'temp', 'degrees', 'fahrenheit', 'weather',
    'high temp', 'low temp', 'rain', 'snow', 'precipitation',
    'heat', 'cold', 'freeze', 'frost',
]

OPEN_METEO_URL = 'https://ensemble-api.open-meteo.com/v1/ensemble'
MIN_EDGE = 0.05
MAX_ENTRY_PRICE = 0.15       # NEVER buy contracts above 15 cents
MIN_MODEL_CONFIDENCE = 0.85  # Only trade when model is 85%+ confident


class WeatherEdgeStrategy(BaseStrategy):
    """31-member GFS ensemble from Open-Meteo vs Kalshi KXHIGH temperature markets."""

    def __init__(self, client, risk_manager, db):
        super().__init__(client, risk_manager, db)
        self._cache = {}
        self._cache_time = None
        logger.info("WeatherEdge initialized (Open-Meteo GFS ensemble, 5% min edge)")

    @resilient_strategy
    def analyze(self, markets):
        signals = []

        # Find weather markets: match series_ticker, ticker prefix, or keywords
        temp_markets = [m for m in markets if self._is_weather(m)]

        kxhigh = sum(1 for m in markets if any(s in (m.get('ticker') or m.get('series_ticker') or '') for s in CITIES))
        logger.info(f"WeatherEdge: {kxhigh} KXHIGH tickers, {len(temp_markets)} total weather markets out of {len(markets)}")

        if not temp_markets:
            sample = [m.get('ticker', '?') for m in markets[:15]]
            logger.info(f"WeatherEdge: 0 candidates. Sample tickers: {sample}")
            return signals

        # Refresh forecasts every 30 min
        now = datetime.utcnow()
        if not self._cache_time or (now - self._cache_time).total_seconds() > 1800:
            self._refresh()

        for m in temp_markets:
            sig = self._evaluate(m)
            if sig:
                signals.append(sig)

        # Cap to best 5 signals to leave room for other strategies
        signals.sort(key=lambda s: s.get('edge', 0), reverse=True)
        signals = signals[:5]
        logger.info(f"WeatherEdge: {len(signals)} signals (top 5 by edge) from {len(temp_markets)} candidates")
        return signals

    def _is_weather(self, m):
        ticker = (m.get('ticker') or '').upper()
        series = (m.get('series_ticker') or '').upper()
        title = (m.get('title') or '').lower()
        # Direct series match
        for prefix in CITIES:
            if prefix in ticker or prefix in series:
                return True
        # Keyword match
        for kw in WEATHER_KEYWORDS:
            if kw in title:
                return True
        return False

    def _refresh(self):
        self._cache = {}
        for series, city in CITIES.items():
            def api_call(timeout):
                resp = requests.get(OPEN_METEO_URL, params={
                    'latitude': city['lat'], 'longitude': city['lon'],
                    'daily': 'temperature_2m_max',
                    'models': 'gfs_seamless',
                    'temperature_unit': 'fahrenheit',
                    'forecast_days': 7,
                }, timeout=timeout)
                resp.raise_for_status()
                return resp.json()

            forecast_data = APIResilience.open_meteo_call(api_call)
            if not forecast_data:
                logger.warning(f"  Forecast unavailable for {city['name']} - using cached data if available")
                continue

            daily = forecast_data.get('daily', {})
            dates = daily.get('time', [])
            # Collect all ensemble member columns
            keys = [k for k in daily if k.startswith('temperature_2m_max')]
            if not keys:
                # Fallback: non-ensemble endpoint
                vals = daily.get('temperature_2m_max', [])
                if vals and dates:
                    self._cache[series] = {dates[i]: [vals[i]] for i in range(len(dates)) if vals[i] is not None}
                continue
            result = {}
            for i, d in enumerate(dates):
                temps = [daily[k][i] for k in keys if i < len(daily[k]) and daily[k][i] is not None]
                if temps:
                    result[d] = temps
            self._cache[series] = result
            logger.info(f"  Forecast loaded: {city['name']} ({len(result)} days, {len(keys)} members)")
        self._cache_time = datetime.utcnow()

    def _evaluate(self, m):
        ticker = m.get('ticker', '')
        title = (m.get('title') or '').lower()
        series = (m.get('series_ticker') or ticker).upper()

        # Match city
        city_series = None
        for prefix in CITIES:
            if prefix in series or prefix in ticker.upper():
                city_series = prefix
                break
        if not city_series or city_series not in self._cache:
            return None

        # Extract threshold from title: "75°F", "above 80", "80 or higher"
        threshold = None
        for pat in [r'(\d{2,3})\s*(?:°|degrees|f\b)', r'above\s+(\d{2,3})', r'(\d{2,3})\s*or\s*(?:above|higher|more)']:
            match = re.search(pat, title)
            if match:
                threshold = int(match.group(1))
                break
        if threshold is None:
            return None

        # Find target date from close_time
        close_time = m.get('close_time') or m.get('expiration_time') or ''
        target_date = None
        if close_time:
            try:
                target_date = datetime.fromisoformat(close_time.replace('Z', '+00:00')).strftime('%Y-%m-%d')
            except Exception:
                pass

        forecasts = self._cache[city_series]
        temps = None
        if target_date and target_date in forecasts:
            temps = forecasts[target_date]
        else:
            for d in sorted(forecasts):
                if not target_date or d >= (target_date or ''):
                    temps = forecasts[d]
                    break
        if not temps:
            return None

        our_prob = sum(1 for t in temps if t >= threshold) / len(temps)
        mkt_yes = get_yes_price(m)
        if mkt_yes <= 0:
            return None

        yes_edge = our_prob - mkt_yes
        no_edge = (1 - our_prob) - (1 - mkt_yes)

        if yes_edge > MIN_EDGE:
            side, edge, prob = 'yes', yes_edge, our_prob
        elif no_edge > MIN_EDGE:
            side, edge, prob = 'no', no_edge, 1 - our_prob
        else:
            logger.debug(f"WeatherEdge SKIP {ticker}: forecast_prob={our_prob:.2f} market={mkt_yes:.2f} edge={yes_edge:+.2f}")
            return None

        # QUANT RULE: Only buy cheap contracts with high model confidence
        entry_price = mkt_yes if side == 'yes' else (1 - mkt_yes)
        if entry_price > MAX_ENTRY_PRICE:
            logger.info(f"WeatherEdge SKIP {ticker}: entry {entry_price:.2f} > max {MAX_ENTRY_PRICE} (too expensive)")
            return None
        if prob < MIN_MODEL_CONFIDENCE:
            logger.info(f"WeatherEdge SKIP {ticker}: model prob {prob:.2f} < min {MIN_MODEL_CONFIDENCE}")
            return None

        agreement = max(our_prob, 1 - our_prob)
        confidence = min(agreement * 80 + abs(edge) * 100, 100)

        logger.info(
            f"WeatherEdge: {ticker} forecast_prob={our_prob:.2f} market={mkt_yes:.2f} "
            f"edge={edge:+.2f} -> PAPER BUY {side.upper()}"
        )

        return {
            'ticker': ticker, 'title': m.get('title', ''), 'action': 'buy', 'side': side,
            'count': 5, 'confidence': confidence, 'strategy_type': 'weather_edge',
            'edge': edge, 'model_prob': prob,
            'reason': f"WeatherEdge: {CITIES[city_series]['name']} >={threshold}F, ensemble={our_prob:.0%} vs market={mkt_yes:.0%}, edge={edge:+.0%}",
        }

    def execute(self, signal, dry_run=False):
        if not self.can_execute(signal):
            return None
        self.log_signal(signal)
        return self.client.create_order(
            ticker=signal['ticker'], action='buy', side=signal['side'],
            count=signal['count'], order_type='market', dry_run=dry_run,
        )
