"""WeatherEdge strategy - compares Open-Meteo GFS ensemble forecasts to Kalshi weather markets.

Covers 24 series: 17 KXHIGH (daily high) + 7 KXLOWT (daily low) temperature markets.
Uses a single batched Open-Meteo API call for all unique city coordinates.

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
    # === HIGH TEMPERATURE MARKETS (17 cities) ===
    'KXHIGHNY':    {'name': 'New York',       'lat': 40.7789, 'lon': -73.9692, 'station': 'Central Park',           'type': 'high'},
    'KXHIGHCHI':   {'name': 'Chicago',         'lat': 41.7868, 'lon': -87.7522, 'station': 'Midway Airport',         'type': 'high'},
    'KXHIGHMIA':   {'name': 'Miami',           'lat': 25.7959, 'lon': -80.2870, 'station': 'Miami Intl Airport',     'type': 'high'},
    'KXHIGHLAX':   {'name': 'Los Angeles',     'lat': 33.9425, 'lon': -118.4081, 'station': 'LAX Airport',            'type': 'high'},
    'KXHIGHDEN':   {'name': 'Denver',          'lat': 39.8561, 'lon': -104.6737, 'station': 'Denver Intl Airport',    'type': 'high'},
    'KXHIGHAUS':   {'name': 'Austin',          'lat': 30.1975, 'lon': -97.6664, 'station': 'Austin-Bergstrom Intl',  'type': 'high'},
    'KXHIGHTPHX':  {'name': 'Phoenix',         'lat': 33.4373, 'lon': -112.0078, 'station': 'Phoenix Sky Harbor',     'type': 'high'},
    'KXHIGHTSFO':  {'name': 'San Francisco',   'lat': 37.6213, 'lon': -122.3790, 'station': 'SFO Airport',            'type': 'high'},
    'KXHIGHTATL':  {'name': 'Atlanta',         'lat': 33.6407, 'lon': -84.4277, 'station': 'Hartsfield-Jackson',     'type': 'high'},
    'KXHIGHPHIL':  {'name': 'Philadelphia',    'lat': 39.8744, 'lon': -75.2424, 'station': 'PHL Airport',            'type': 'high'},
    'KXHIGHTDC':   {'name': 'Washington DC',   'lat': 38.8512, 'lon': -77.0402, 'station': 'Reagan National',        'type': 'high'},
    'KXHIGHTSEA':  {'name': 'Seattle',         'lat': 47.4502, 'lon': -122.3088, 'station': 'Sea-Tac Airport',        'type': 'high'},
    'KXHIGHTHOU':  {'name': 'Houston',         'lat': 29.9902, 'lon': -95.3368, 'station': 'Houston Hobby/IAH',      'type': 'high'},
    'KXHIGHTMIN':  {'name': 'Minneapolis',     'lat': 44.8848, 'lon': -93.2223, 'station': 'MSP Airport',            'type': 'high'},
    'KXHIGHTBOS':  {'name': 'Boston',          'lat': 42.3656, 'lon': -71.0096, 'station': 'Logan Airport',          'type': 'high'},
    'KXHIGHTLV':   {'name': 'Las Vegas',       'lat': 36.0840, 'lon': -115.1537, 'station': 'McCarran/Harry Reid',    'type': 'high'},
    'KXHIGHTOKC':  {'name': 'Oklahoma City',   'lat': 35.3931, 'lon': -97.6007, 'station': 'Will Rogers Airport',    'type': 'high'},
    # === LOW TEMPERATURE MARKETS (7 cities) ===
    'KXLOWTNYC':   {'name': 'New York',        'lat': 40.7789, 'lon': -73.9692, 'station': 'Central Park',           'type': 'low'},
    'KXLOWTCHI':   {'name': 'Chicago',         'lat': 41.7868, 'lon': -87.7522, 'station': 'Midway Airport',         'type': 'low'},
    'KXLOWTMIA':   {'name': 'Miami',           'lat': 25.7959, 'lon': -80.2870, 'station': 'Miami Intl Airport',     'type': 'low'},
    'KXLOWTLAX':   {'name': 'Los Angeles',     'lat': 33.9425, 'lon': -118.4081, 'station': 'LAX Airport',            'type': 'low'},
    'KXLOWTDEN':   {'name': 'Denver',          'lat': 39.8561, 'lon': -104.6737, 'station': 'Denver Intl Airport',    'type': 'low'},
    'KXLOWTAUS':   {'name': 'Austin',          'lat': 30.1975, 'lon': -97.6664, 'station': 'Austin-Bergstrom Intl',  'type': 'low'},
    'KXLOWTPHIL':  {'name': 'Philadelphia',    'lat': 39.8744, 'lon': -75.2424, 'station': 'PHL Airport',            'type': 'low'},
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
    """31-member GFS ensemble from Open-Meteo vs Kalshi KXHIGH/KXLOWT temperature markets (24 cities)."""

    def __init__(self, client, risk_manager, db):
        super().__init__(client, risk_manager, db)
        self._cache_high = {}  # series -> {date: [temps]}
        self._cache_low = {}   # series -> {date: [temps]}
        self._cache_time = None
        logger.info(f"WeatherEdge initialized (Open-Meteo GFS ensemble, {len(CITIES)} cities, 5% min edge)")

    @resilient_strategy
    def analyze(self, markets):
        signals = []

        # Find weather markets: match series_ticker, ticker prefix, or keywords
        temp_markets = [m for m in markets if self._is_weather(m)]

        kx_count = sum(1 for m in markets if any(s in (m.get('ticker') or m.get('series_ticker') or '') for s in CITIES))
        logger.info(f"WeatherEdge: {kx_count} KXHIGH/KXLOWT tickers, {len(temp_markets)} total weather markets out of {len(markets)}")

        if not temp_markets:
            sample = [m.get('ticker', '?') for m in markets[:15]]
            logger.info(f"WeatherEdge: 0 candidates. Sample tickers: {sample}")
            return signals

        # Refresh forecasts every 30 min
        now = datetime.utcnow()
        if not self._cache_time or (now - self._cache_time).total_seconds() > 1800:
            self._refresh()
            logger.info(f"WeatherEdge cache: {len(self._cache_high)} high series, {len(self._cache_low)} low series")

        for m in temp_markets:
            sig = self._evaluate(m)
            if sig:
                signals.append(sig)

        # Cap to best 10 signals to leave room for other strategies
        signals.sort(key=lambda s: s.get('edge', 0), reverse=True)
        signals = signals[:10]
        logger.info(f"WeatherEdge: {len(signals)} signals (top 10 by edge) from {len(temp_markets)} candidates")
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
        self._cache_high = {}
        self._cache_low = {}

        # Deduplicate coordinates: group series by (lat, lon)
        coord_to_series = {}
        for series, city in CITIES.items():
            key = (city['lat'], city['lon'])
            coord_to_series.setdefault(key, []).append(series)

        unique_coords = list(coord_to_series.keys())
        lats = ','.join(str(c[0]) for c in unique_coords)
        lons = ','.join(str(c[1]) for c in unique_coords)

        def api_call(timeout):
            resp = requests.get(OPEN_METEO_URL, params={
                'latitude': lats,
                'longitude': lons,
                'daily': 'temperature_2m_max,temperature_2m_min',
                'models': 'gfs_seamless',
                'temperature_unit': 'fahrenheit',
                'forecast_days': 7,
            }, timeout=timeout)
            resp.raise_for_status()
            return resp.json()

        forecast_data = APIResilience.open_meteo_call(api_call)
        if not forecast_data:
            logger.warning("Batch forecast unavailable - keeping stale cache")
            self._cache_time = datetime.utcnow()
            return

        # Open-Meteo returns a list for multi-location, single dict for one location
        if isinstance(forecast_data, dict):
            forecast_data = [forecast_data]

        for idx, coord in enumerate(unique_coords):
            if idx >= len(forecast_data):
                break
            location_data = forecast_data[idx]
            daily = location_data.get('daily', {})
            dates = daily.get('time', [])

            # Parse ensemble members for max and min
            for temp_type, prefix, cache in [
                ('high', 'temperature_2m_max', self._cache_high),
                ('low', 'temperature_2m_min', self._cache_low),
            ]:
                keys = [k for k in daily if k.startswith(prefix)]
                result = {}
                if keys:
                    for i, d in enumerate(dates):
                        temps = [daily[k][i] for k in keys if i < len(daily[k]) and daily[k][i] is not None]
                        if temps:
                            result[d] = temps
                else:
                    # Fallback: non-ensemble
                    vals = daily.get(prefix, [])
                    if vals and dates:
                        result = {dates[i]: [vals[i]] for i in range(len(dates)) if vals[i] is not None}

                # Assign to all series that share this coordinate and match this type
                for series in coord_to_series[coord]:
                    city = CITIES[series]
                    if city['type'] == temp_type:
                        cache[series] = result

            city_name = CITIES[coord_to_series[coord][0]]['name']
            n_series = len(coord_to_series[coord])
            logger.info(f"  Forecast loaded: {city_name} ({n_series} series, {len(dates)} days)")

        self._cache_time = datetime.utcnow()
        logger.info(f"WeatherEdge: refreshed {len(unique_coords)} locations in 1 API call ({len(CITIES)} series total)")

    def _evaluate(self, m):
        ticker = m.get('ticker', '')
        title = (m.get('title') or '').lower()
        series = (m.get('series_ticker') or ticker).upper()

        # Match city - try longest prefix first to avoid partial matches
        city_series = None
        for prefix in sorted(CITIES, key=len, reverse=True):
            if prefix in series or prefix in ticker.upper():
                city_series = prefix
                break
        if not city_series:
            return None

        city = CITIES[city_series]
        is_low = city['type'] == 'low'
        cache = self._cache_low if is_low else self._cache_high
        if city_series not in cache:
            return None

        # Extract threshold from title
        # HIGH: "above 80", "80°F or higher", "80 to 84"
        # LOW:  "below 40", "40°F or lower", "38 to 40"
        threshold = None
        for pat in [r'(\d{2,3})\s*(?:°|degrees|f\b)', r'(?:above|below)\s+(\d{2,3})',
                     r'(\d{2,3})\s*or\s*(?:above|higher|more|below|lower|less)',
                     r'(\d{2,3})\s*to\s*(\d{2,3})']:
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

        forecasts = cache[city_series]
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

        # Calculate probability from ensemble members
        if is_low:
            # LOW market: "will the low be X or below?" -> count members <= threshold
            our_prob = sum(1 for t in temps if t <= threshold) / len(temps)
        else:
            # HIGH market: "will the high be X or above?" -> count members >= threshold
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

        temp_label = 'LOW' if is_low else 'HIGH'
        comp = '<=' if is_low else '>='
        logger.info(
            f"WeatherEdge: {ticker} [{temp_label}] forecast_prob={our_prob:.2f} market={mkt_yes:.2f} "
            f"edge={edge:+.2f} -> PAPER BUY {side.upper()}"
        )

        return {
            'ticker': ticker, 'title': m.get('title', ''), 'action': 'buy', 'side': side,
            'count': 5, 'confidence': confidence, 'strategy_type': 'weather_edge',
            'edge': edge, 'model_prob': prob,
            'reason': f"WeatherEdge: {city['name']} {temp_label} {comp}{threshold}F, ensemble={our_prob:.0%} vs market={mkt_yes:.0%}, edge={edge:+.0%}",
        }

    def execute(self, signal, dry_run=False):
        if not self.can_execute(signal):
            return None
        self.log_signal(signal)
        return self.client.create_order(
            ticker=signal['ticker'], action='buy', side=signal['side'],
            count=signal['count'], order_type='market', dry_run=dry_run,
        )
