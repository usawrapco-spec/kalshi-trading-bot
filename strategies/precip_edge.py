"""PrecipEdge strategy - GFS ensemble precipitation forecasts vs Kalshi rain/snow markets.

Same edge as WeatherEdge (temperature) but for precipitation:
  - GFS 31-member ensemble predicts rain/snow at exact NWS station coordinates
  - Most Kalshi traders check generic city forecasts; we target the exact settlement gauge
  - Precipitation is harder to predict than temperature, so ALWAYS uses HyperThink debate

Covers: KXRAIN, KXRAINFALL, KXSNOW*, and any market with rain/snow/precipitation in title.
"""

import re
import requests
from datetime import datetime
from strategies.base import BaseStrategy
from utils.logger import setup_logger
from utils.market_helpers import get_yes_price, get_volume
from utils.api_resilience import APIResilience, resilient_strategy

logger = setup_logger('precip_edge')

# Reuse the same station coordinates from weather_edge
# These are the exact NWS/ICAO stations Kalshi uses for settlement
PRECIP_CITIES = {
    'New York':      {'lat': 40.7789, 'lon': -73.9692, 'icao': 'KNYC'},
    'Chicago':       {'lat': 41.7868, 'lon': -87.7522, 'icao': 'KMDW'},
    'Miami':         {'lat': 25.7959, 'lon': -80.2870, 'icao': 'KMIA'},
    'Los Angeles':   {'lat': 33.9425, 'lon': -118.4081, 'icao': 'KLAX'},
    'Denver':        {'lat': 39.8561, 'lon': -104.6737, 'icao': 'KDEN'},
    'Austin':        {'lat': 30.1944, 'lon': -97.6700, 'icao': 'KAUS'},
    'Phoenix':       {'lat': 33.4373, 'lon': -112.0078, 'icao': 'KPHX'},
    'San Francisco': {'lat': 37.6213, 'lon': -122.3790, 'icao': 'KSFO'},
    'Atlanta':       {'lat': 33.6407, 'lon': -84.4277, 'icao': 'KATL'},
    'Philadelphia':  {'lat': 39.8744, 'lon': -75.2424, 'icao': 'KPHL'},
    'Washington':    {'lat': 38.8512, 'lon': -77.0402, 'icao': 'KDCA'},
    'Seattle':       {'lat': 47.4502, 'lon': -122.3088, 'icao': 'KSEA'},
    'Houston':       {'lat': 29.6454, 'lon': -95.2789, 'icao': 'KHOU'},
    'Minneapolis':   {'lat': 44.8848, 'lon': -93.2223, 'icao': 'KMSP'},
    'Boston':        {'lat': 42.3656, 'lon': -71.0096, 'icao': 'KBOS'},
    'Las Vegas':     {'lat': 36.0840, 'lon': -115.1537, 'icao': 'KLAS'},
    'Oklahoma City': {'lat': 35.3931, 'lon': -97.6007, 'icao': 'KOKC'},
}

# City name aliases for matching market titles
CITY_ALIASES = {
    'nyc': 'New York', 'new york': 'New York', 'manhattan': 'New York',
    'chicago': 'Chicago', 'chi': 'Chicago',
    'miami': 'Miami', 'mia': 'Miami',
    'los angeles': 'Los Angeles', 'la': 'Los Angeles', 'lax': 'Los Angeles',
    'denver': 'Denver', 'den': 'Denver',
    'austin': 'Austin', 'aus': 'Austin',
    'phoenix': 'Phoenix', 'phx': 'Phoenix',
    'san francisco': 'San Francisco', 'sf': 'San Francisco', 'sfo': 'San Francisco',
    'atlanta': 'Atlanta', 'atl': 'Atlanta',
    'philadelphia': 'Philadelphia', 'philly': 'Philadelphia', 'phl': 'Philadelphia',
    'washington': 'Washington', 'dc': 'Washington', 'washington dc': 'Washington',
    'seattle': 'Seattle', 'sea': 'Seattle',
    'houston': 'Houston', 'hou': 'Houston',
    'minneapolis': 'Minneapolis', 'msp': 'Minneapolis',
    'boston': 'Boston', 'bos': 'Boston',
    'las vegas': 'Las Vegas', 'vegas': 'Las Vegas',
    'oklahoma city': 'Oklahoma City', 'okc': 'Oklahoma City',
}

PRECIP_SERIES = ['KXRAIN', 'KXRAINFALL', 'KXNYCSNOWM', 'KXLAXSNOWM', 'KXCHISNOWM', 'KXDENSNOWM']
PRECIP_KEYWORDS = ['rain', 'snow', 'precipitation', 'inches of', 'measurable']

OPEN_METEO_URL = 'https://ensemble-api.open-meteo.com/v1/ensemble'

MIN_EDGE = 0.08          # Higher than temp — precip is noisier
MAX_ENTRY_PRICE = 0.20   # Allow slightly more expensive contracts
MIN_MODEL_CONFIDENCE = 0.75  # Lower threshold — use HyperThink to validate


class PrecipEdgeStrategy(BaseStrategy):
    """GFS ensemble precipitation forecasts vs Kalshi rain/snow markets."""

    def __init__(self, client, risk_manager, db, hyperthink=None):
        super().__init__(client, risk_manager, db)
        self.hyperthink = hyperthink
        self._cache = {}       # city_name -> {date: {'rain': [members], 'snow': [members]}}
        self._cache_time = None
        logger.info(f"PrecipEdge initialized ({len(PRECIP_CITIES)} cities, HyperThink={'ON' if hyperthink else 'OFF'})")

    @resilient_strategy
    def analyze(self, markets):
        signals = []

        precip_markets = [m for m in markets if self._is_precip(m)]
        logger.info(f"PrecipEdge: {len(precip_markets)} precipitation markets out of {len(markets)}")

        if not precip_markets:
            return signals

        # Refresh forecasts every 30 min
        now = datetime.utcnow()
        if not self._cache_time or (now - self._cache_time).total_seconds() > 1800:
            self._refresh_forecasts()

        for m in precip_markets:
            sig = self._evaluate(m)
            if sig:
                signals.append(sig)

        signals.sort(key=lambda s: s.get('edge', 0), reverse=True)
        signals = signals[:5]
        logger.info(f"PrecipEdge: {len(signals)} signals from {len(precip_markets)} candidates")
        return signals

    def _is_precip(self, m):
        ticker = (m.get('ticker') or '').upper()
        series = (m.get('series_ticker') or '').upper()
        title = (m.get('title') or '').lower()

        for prefix in PRECIP_SERIES:
            if prefix in ticker or prefix in series:
                return True
        for kw in PRECIP_KEYWORDS:
            if kw in title:
                return True
        return False

    def _refresh_forecasts(self):
        self._cache = {}

        # Deduplicate coordinates
        coords = []
        coord_to_cities = {}
        for city_name, info in PRECIP_CITIES.items():
            key = (info['lat'], info['lon'])
            coord_to_cities.setdefault(key, []).append(city_name)
            if key not in [c for c, _ in [(k, None) for k in coord_to_cities if coord_to_cities[k]]]:
                coords.append(key)
        coords = list(coord_to_cities.keys())

        lats = ','.join(str(c[0]) for c in coords)
        lons = ','.join(str(c[1]) for c in coords)

        def api_call(timeout):
            resp = requests.get(OPEN_METEO_URL, params={
                'latitude': lats,
                'longitude': lons,
                'daily': 'precipitation_sum,snowfall_sum',
                'models': 'gfs_seamless',
                'precipitation_unit': 'inch',
                'forecast_days': 7,
            }, timeout=timeout)
            resp.raise_for_status()
            return resp.json()

        forecast_data = APIResilience.open_meteo_call(api_call)
        if not forecast_data:
            logger.warning("PrecipEdge: forecast unavailable")
            self._cache_time = datetime.utcnow()
            return

        if isinstance(forecast_data, dict):
            forecast_data = [forecast_data]

        for idx, coord in enumerate(coords):
            if idx >= len(forecast_data):
                break
            location = forecast_data[idx]
            daily = location.get('daily', {})
            dates = daily.get('time', [])

            # Parse ensemble members for precipitation
            rain_keys = [k for k in daily if k.startswith('precipitation_sum')]
            snow_keys = [k for k in daily if k.startswith('snowfall_sum')]

            result = {}
            for i, d in enumerate(dates):
                rain_members = []
                for k in rain_keys:
                    if i < len(daily[k]) and daily[k][i] is not None:
                        rain_members.append(daily[k][i])
                # Fallback: non-ensemble (single value)
                if not rain_members:
                    val = daily.get('precipitation_sum', [])
                    if i < len(val) and val[i] is not None:
                        rain_members = [val[i]]

                snow_members = []
                for k in snow_keys:
                    if i < len(daily[k]) and daily[k][i] is not None:
                        snow_members.append(daily[k][i])
                if not snow_members:
                    val = daily.get('snowfall_sum', [])
                    if i < len(val) and val[i] is not None:
                        snow_members = [val[i]]

                result[d] = {'rain': rain_members, 'snow': snow_members}

            for city_name in coord_to_cities[coord]:
                self._cache[city_name] = result

        self._cache_time = datetime.utcnow()
        logger.info(f"PrecipEdge: refreshed forecasts for {len(self._cache)} cities, {len(coords)} API locations")

    def _evaluate(self, m):
        ticker = m.get('ticker', '')
        title = (m.get('title') or '').lower()

        parsed = self._parse_precip_market(m)
        if not parsed:
            return None

        precip_type = parsed['type']
        threshold = parsed['threshold']
        city_name = parsed['city']

        if city_name not in self._cache:
            logger.debug(f"PrecipEdge SKIP {ticker}: no forecast for {city_name}")
            return None

        forecasts = self._cache[city_name]

        # Find target date
        close_time = m.get('close_time') or m.get('expiration_time') or ''
        target_date = None
        if close_time:
            try:
                target_date = datetime.fromisoformat(close_time.replace('Z', '+00:00')).strftime('%Y-%m-%d')
            except Exception:
                pass

        day_data = None
        if target_date and target_date in forecasts:
            day_data = forecasts[target_date]
        else:
            for d in sorted(forecasts):
                if not target_date or d >= target_date:
                    day_data = forecasts[d]
                    break
        if not day_data:
            return None

        members = day_data.get(precip_type, [])
        if not members:
            return None

        # Calculate probability of exceeding threshold
        exceed_count = sum(1 for val in members if val >= threshold)
        our_prob = exceed_count / len(members)

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
            logger.debug(f"PrecipEdge SKIP {ticker}: prob={our_prob:.2f} mkt={mkt_yes:.2f} edge={yes_edge:+.2f}")
            return None

        entry_price = mkt_yes if side == 'yes' else (1 - mkt_yes)
        if entry_price > MAX_ENTRY_PRICE:
            logger.debug(f"PrecipEdge SKIP {ticker}: entry {entry_price:.2f} > max {MAX_ENTRY_PRICE}")
            return None
        if prob < MIN_MODEL_CONFIDENCE:
            logger.debug(f"PrecipEdge SKIP {ticker}: prob {prob:.2f} < min {MIN_MODEL_CONFIDENCE}")
            return None

        # ALWAYS run HyperThink for precipitation (less reliable than temp)
        size_multiplier = 1.0
        confidence_label = "DATA_ONLY"
        if self.hyperthink:
            context = (
                f"GFS {len(members)}-member ensemble precipitation forecast. "
                f"Type: {precip_type}, threshold: {threshold} inches, city: {city_name}. "
                f"GFS says {our_prob:.0%} probability of exceeding {threshold} inches. "
                f"Note: GFS often over-predicts light rain and under-predicts heavy events."
            )
            avg_prob, confidence_label, size_multiplier = self.hyperthink.evaluate(
                m, side, mkt_yes, data_prob=our_prob, context=context
            )
            if size_multiplier <= 0:
                logger.info(f"PrecipEdge SKIP {ticker}: HyperThink says {confidence_label}")
                return None
            # Use consensus probability for edge calculation
            if confidence_label in ("UNANIMOUS", "STRONG"):
                prob = avg_prob if side == 'yes' else (1 - avg_prob)
                edge = prob - entry_price

        count = max(1, int(3 * size_multiplier))
        median_precip = sorted(members)[len(members) // 2]
        confidence = min(prob * 80 + abs(edge) * 100, 100)

        logger.info(
            f"PrecipEdge: {ticker} [{precip_type.upper()}] {city_name} "
            f"{len(members)}mbr, median={median_precip:.2f}in, threshold={threshold}in, "
            f"prob={our_prob:.2f} vs mkt={mkt_yes:.2f}, edge={edge:+.2f}, "
            f"HyperThink={confidence_label} -> BUY {side.upper()} x{count}"
        )

        return {
            'ticker': ticker, 'title': m.get('title', ''), 'action': 'buy', 'side': side,
            'count': count, 'confidence': confidence, 'strategy_type': 'precip_edge',
            'edge': edge, 'model_prob': prob,
            'reason': (
                f"PrecipEdge: {city_name} {precip_type} >={threshold}in, "
                f"{len(members)}mbr={our_prob:.0%} vs mkt={mkt_yes:.0%}, "
                f"edge={edge:+.0%}, HT={confidence_label}"
            ),
        }

    def _parse_precip_market(self, m):
        title = (m.get('title') or '').lower()

        # Detect type
        if 'snow' in title:
            precip_type = 'snow'
        elif 'rain' in title or 'precipitation' in title or 'inches' in title:
            precip_type = 'rain'
        else:
            return None

        # Extract threshold (look for numbers followed by "inch")
        threshold_match = re.search(r'(\d+\.?\d*)\s*inch', title)
        if threshold_match:
            threshold = float(threshold_match.group(1))
        elif 'any' in title or 'measurable' in title or 'will it' in title:
            threshold = 0.01  # "any rain/snow" = trace amount
        else:
            threshold = 0.01  # Default to "any measurable"

        # Match city
        city_name = self._match_city(title)
        if not city_name:
            return None

        return {'type': precip_type, 'threshold': threshold, 'city': city_name}

    def _match_city(self, title):
        title_lower = title.lower()
        # Try longest alias first to avoid partial matches
        for alias in sorted(CITY_ALIASES, key=len, reverse=True):
            if alias in title_lower:
                return CITY_ALIASES[alias]
        return None

    def execute(self, signal, dry_run=False):
        if not self.can_execute(signal):
            return None
        self.log_signal(signal)
        return self.client.create_order(
            ticker=signal['ticker'], action='buy', side=signal['side'],
            count=signal['count'], order_type='market', dry_run=dry_run,
        )
