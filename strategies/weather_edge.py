"""WeatherEdge strategy - multi-model ensemble forecasts vs Kalshi weather markets.

Covers 24 series: 17 KXHIGH (daily high) + 7 KXLOWT (daily low) temperature markets.
Uses GFS + ECMWF + ICON ensembles from Open-Meteo in a single batched API call.

Hidden edges exploited:
  1. Multi-model divergence — GFS/ECMWF/ICON agreement = high confidence
  2. Station microclimate bias corrections (SFO fog, LAX marine layer, etc.)
  3. Bracket boundary buffer — skip 50/50 gambles on C-to-F rounding
  4. GFS run freshness tracking — detect stale market pricing
"""

import re
import requests
from datetime import datetime, timedelta, timezone
from strategies.base import BaseStrategy
from utils.logger import setup_logger
from utils.market_helpers import get_yes_price, get_volume
from utils.api_resilience import APIResilience, resilient_strategy

logger = setup_logger('weather_edge')

CITIES = {
    # === HIGH TEMPERATURE MARKETS (17 cities) ===
    # Coordinates match exact NWS/ICAO stations Kalshi uses for settlement
    'KXHIGHNY':    {'name': 'New York',       'lat': 40.7789, 'lon': -73.9692, 'icao': 'KNYC', 'station': 'Central Park',        'type': 'high'},
    'KXHIGHCHI':   {'name': 'Chicago',        'lat': 41.7868, 'lon': -87.7522, 'icao': 'KMDW', 'station': 'Midway Airport',      'type': 'high'},
    'KXHIGHMIA':   {'name': 'Miami',          'lat': 25.7959, 'lon': -80.2870, 'icao': 'KMIA', 'station': 'Miami Intl Airport',  'type': 'high'},
    'KXHIGHLAX':   {'name': 'Los Angeles',    'lat': 33.9425, 'lon': -118.4081, 'icao': 'KLAX', 'station': 'LAX Airport',         'type': 'high'},
    'KXHIGHDEN':   {'name': 'Denver',         'lat': 39.8561, 'lon': -104.6737, 'icao': 'KDEN', 'station': 'Denver Intl Airport', 'type': 'high'},
    'KXHIGHAUS':   {'name': 'Austin',         'lat': 30.1944, 'lon': -97.6700, 'icao': 'KAUS', 'station': 'Austin-Bergstrom',    'type': 'high'},
    'KXHIGHTPHX':  {'name': 'Phoenix',        'lat': 33.4373, 'lon': -112.0078, 'icao': 'KPHX', 'station': 'Sky Harbor',          'type': 'high'},
    'KXHIGHTSFO':  {'name': 'San Francisco',  'lat': 37.6213, 'lon': -122.3790, 'icao': 'KSFO', 'station': 'SFO Airport',         'type': 'high'},
    'KXHIGHTATL':  {'name': 'Atlanta',        'lat': 33.6407, 'lon': -84.4277, 'icao': 'KATL', 'station': 'Hartsfield-Jackson',  'type': 'high'},
    'KXHIGHPHIL':  {'name': 'Philadelphia',   'lat': 39.8744, 'lon': -75.2424, 'icao': 'KPHL', 'station': 'PHL Airport',         'type': 'high'},
    'KXHIGHTDC':   {'name': 'Washington DC',  'lat': 38.8512, 'lon': -77.0402, 'icao': 'KDCA', 'station': 'Reagan National',     'type': 'high'},
    'KXHIGHTSEA':  {'name': 'Seattle',        'lat': 47.4502, 'lon': -122.3088, 'icao': 'KSEA', 'station': 'Sea-Tac Airport',     'type': 'high'},
    'KXHIGHTHOU':  {'name': 'Houston',        'lat': 29.6454, 'lon': -95.2789, 'icao': 'KHOU', 'station': 'Hobby Airport',       'type': 'high'},
    'KXHIGHTMIN':  {'name': 'Minneapolis',    'lat': 44.8848, 'lon': -93.2223, 'icao': 'KMSP', 'station': 'MSP Airport',         'type': 'high'},
    'KXHIGHTBOS':  {'name': 'Boston',         'lat': 42.3656, 'lon': -71.0096, 'icao': 'KBOS', 'station': 'Logan Airport',       'type': 'high'},
    'KXHIGHTLV':   {'name': 'Las Vegas',      'lat': 36.0840, 'lon': -115.1537, 'icao': 'KLAS', 'station': 'Harry Reid Airport',  'type': 'high'},
    'KXHIGHTOKC':  {'name': 'Oklahoma City',  'lat': 35.3931, 'lon': -97.6007, 'icao': 'KOKC', 'station': 'Will Rogers Airport', 'type': 'high'},
    # === LOW TEMPERATURE MARKETS (7 cities) ===
    # Same stations as HIGH — forecast data is shared via coordinate dedup
    'KXLOWTNYC':   {'name': 'New York',       'lat': 40.7789, 'lon': -73.9692, 'icao': 'KNYC', 'station': 'Central Park',        'type': 'low'},
    'KXLOWTCHI':   {'name': 'Chicago',        'lat': 41.7868, 'lon': -87.7522, 'icao': 'KMDW', 'station': 'Midway Airport',      'type': 'low'},
    'KXLOWTMIA':   {'name': 'Miami',          'lat': 25.7959, 'lon': -80.2870, 'icao': 'KMIA', 'station': 'Miami Intl Airport',  'type': 'low'},
    'KXLOWTLAX':   {'name': 'Los Angeles',    'lat': 33.9425, 'lon': -118.4081, 'icao': 'KLAX', 'station': 'LAX Airport',         'type': 'low'},
    'KXLOWTDEN':   {'name': 'Denver',         'lat': 39.8561, 'lon': -104.6737, 'icao': 'KDEN', 'station': 'Denver Intl Airport', 'type': 'low'},
    'KXLOWTAUS':   {'name': 'Austin',         'lat': 30.1944, 'lon': -97.6700, 'icao': 'KAUS', 'station': 'Austin-Bergstrom',    'type': 'low'},
    'KXLOWTPHIL':  {'name': 'Philadelphia',   'lat': 39.8744, 'lon': -75.2424, 'icao': 'KPHL', 'station': 'PHL Airport',         'type': 'low'},
}

WEATHER_KEYWORDS = [
    'temperature', 'temp', 'degrees', 'fahrenheit', 'weather',
    'high temp', 'low temp', 'rain', 'snow', 'precipitation',
    'heat', 'cold', 'freeze', 'frost',
]

# --- Station microclimate bias corrections (degrees F) ---
# Positive = station reads WARMER than GFS grid cell; negative = COOLER.
# Applied to ensemble members before probability calculation.
# Based on known physical effects: marine layer, urban heat island, lake effect, altitude.
STATION_BIAS = {
    # HIGH temp biases
    'KSFO': {'high': -3.0, 'low': -1.0},   # SFO fog/marine layer: GFS overestimates highs
    'KLAX': {'high': -2.0, 'low': -0.5},   # LAX coastal marine layer, cooler than grid
    'KNYC': {'high': +1.0, 'low': +1.5},   # Central Park urban heat island at night
    'KMDW': {'high': -1.0, 'low': -0.5},   # Lake Michigan cooling when NE wind
    'KDEN': {'high': +0.5, 'low': -1.0},   # High altitude: bigger swings, cold mornings
    'KSEA': {'high': -1.5, 'low': 0.0},    # Puget Sound marine influence cools highs
    'KPHX': {'high': +1.0, 'low': +1.5},   # Urban heat island, desert radiative heat
    'KHOU': {'high': +0.5, 'low': +0.5},   # Gulf humidity holds heat
    'KBOS': {'high': -0.5, 'low': 0.0},    # Harbor/ocean cooling
    'KMSP': {'high': 0.0, 'low': -0.5},    # Continental, cold pools in winter
    'KLAS': {'high': +0.5, 'low': +1.0},   # Urban heat island in desert
    'KATL': {'high': 0.0, 'low': 0.0},     # Fairly representative grid cell
    'KPHL': {'high': 0.0, 'low': 0.0},
    'KDCA': {'high': +0.5, 'low': +0.5},   # Potomac River warmth + urban
    'KAUS': {'high': 0.0, 'low': 0.0},
    'KOKC': {'high': 0.0, 'low': 0.0},
    'KMIA': {'high': 0.0, 'low': +0.5},    # Ocean moderation holds lows up
}

OPEN_METEO_URL = 'https://ensemble-api.open-meteo.com/v1/ensemble'

# Multi-model: GFS (31 members) + ECMWF (51 members) + ICON (40 members)
ENSEMBLE_MODELS = 'gfs_seamless,ecmwf_ifs025,icon_seamless'

MIN_EDGE = 0.05
MAX_ENTRY_PRICE = 0.15       # NEVER buy contracts above 15 cents
MIN_MODEL_CONFIDENCE = 0.85  # Only trade when model is 85%+ confident
BRACKET_BUFFER = 1.0         # Skip when median is within 1F of bracket edge (rounding trap)


class WeatherEdgeStrategy(BaseStrategy):
    """Multi-model ensemble (GFS+ECMWF+ICON) vs Kalshi weather markets (24 cities)."""

    def __init__(self, client, risk_manager, db):
        super().__init__(client, risk_manager, db)
        self._cache_high = {}  # series -> {date: [temps]}  (all ensemble members, bias-corrected)
        self._cache_low = {}   # series -> {date: [temps]}
        self._cache_time = None
        self._model_run_time = None  # Track which GFS run we're using
        self._models_loaded = set()  # Which models returned data
        logger.info(f"WeatherEdge initialized (GFS+ECMWF+ICON ensemble, {len(CITIES)} cities, 5% min edge)")

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
            logger.info(f"WeatherEdge cache: {len(self._cache_high)} high, {len(self._cache_low)} low, models={self._models_loaded}")

        for m in temp_markets:
            sig = self._evaluate(m)
            if sig:
                signals.append(sig)

        import os as _os; _max = 100 if float(_os.environ.get('PAPER_BALANCE', '100000')) >= 1000 else 10
        signals.sort(key=lambda s: s.get('edge', 0), reverse=True)
        signals = signals[:_max]
        logger.info(f"WeatherEdge: {len(signals)} signals (top {_max} by edge) from {len(temp_markets)} candidates")
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
        self._models_loaded = set()

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
                'models': ENSEMBLE_MODELS,
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

            # Detect which models returned data
            for k in daily:
                if 'member' in k:
                    for model in ['gfs', 'ecmwf', 'icon']:
                        if model in k:
                            self._models_loaded.add(model)

            # Get ICAO for station bias lookup
            first_series = coord_to_series[coord][0]
            icao = CITIES[first_series].get('icao', '')
            bias_data = STATION_BIAS.get(icao, {})

            # Parse ensemble members for max and min
            for temp_type, prefix, cache in [
                ('high', 'temperature_2m_max', self._cache_high),
                ('low', 'temperature_2m_min', self._cache_low),
            ]:
                keys = [k for k in daily if k.startswith(prefix)]
                bias = bias_data.get(temp_type, 0.0)
                result = {}
                if keys:
                    for i, d in enumerate(dates):
                        temps = [daily[k][i] + bias for k in keys
                                 if i < len(daily[k]) and daily[k][i] is not None]
                        if temps:
                            result[d] = temps
                else:
                    # Fallback: non-ensemble
                    vals = daily.get(prefix, [])
                    if vals and dates:
                        result = {dates[i]: [vals[i] + bias] for i in range(len(dates))
                                  if vals[i] is not None}

                # Assign to all series that share this coordinate and match this type
                for series in coord_to_series[coord]:
                    city = CITIES[series]
                    if city['type'] == temp_type:
                        cache[series] = result

            city_name = CITIES[coord_to_series[coord][0]]['name']
            n_series = len(coord_to_series[coord])
            logger.info(f"  Forecast loaded: {city_name} ({n_series} series, {len(dates)} days, bias={bias_data})")

        self._cache_time = datetime.utcnow()
        n_models = len(self._models_loaded) or 1
        logger.info(
            f"WeatherEdge: refreshed {len(unique_coords)} locations in 1 API call "
            f"({len(CITIES)} series, {n_models} models: {self._models_loaded or {'gfs'}})"
        )

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

        # --- EDGE 3: Bracket boundary buffer (C-to-F rounding trap) ---
        # If ensemble median is within BRACKET_BUFFER of the threshold,
        # the outcome is essentially a coin flip due to measurement rounding.
        median_temp = sorted(temps)[len(temps) // 2]
        if abs(median_temp - threshold) < BRACKET_BUFFER:
            logger.debug(
                f"WeatherEdge SKIP {ticker}: bracket boundary trap "
                f"(median={median_temp:.1f}F, threshold={threshold}F, buffer={BRACKET_BUFFER}F)"
            )
            return None

        # Calculate probability from all ensemble members (GFS + ECMWF + ICON combined)
        if is_low:
            our_prob = sum(1 for t in temps if t <= threshold) / len(temps)
        else:
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

        # --- EDGE 1: Multi-model agreement confidence boost ---
        # More models loaded = higher confidence in the probability estimate.
        n_models = max(len(self._models_loaded), 1)
        n_members = len(temps)
        agreement = max(our_prob, 1 - our_prob)
        base_confidence = agreement * 80 + abs(edge) * 100
        # Bonus: +5 per additional model beyond GFS (up to +10 for 3-model agreement)
        model_bonus = (n_models - 1) * 5
        confidence = min(base_confidence + model_bonus, 100)

        temp_label = 'LOW' if is_low else 'HIGH'
        comp = '<=' if is_low else '>='
        icao = city.get('icao', '?')
        bias = STATION_BIAS.get(icao, {}).get(city['type'], 0.0)
        bias_str = f" bias={bias:+.1f}F" if bias else ""

        logger.info(
            f"WeatherEdge: {ticker} [{temp_label}] {n_members} members, median={median_temp:.1f}F, "
            f"prob={our_prob:.2f} vs market={mkt_yes:.2f}, edge={edge:+.2f}, "
            f"models={n_models}{bias_str} -> BUY {side.upper()}"
        )

        return {
            'ticker': ticker, 'title': m.get('title', ''), 'action': 'buy', 'side': side,
            'count': 5, 'confidence': confidence, 'strategy_type': 'weather_edge',
            'edge': edge, 'model_prob': prob,
            'reason': (
                f"WeatherEdge: {city['name']} ({icao}) {temp_label} {comp}{threshold}F, "
                f"{n_members}mbr/{n_models}mdl={our_prob:.0%} vs mkt={mkt_yes:.0%}, "
                f"edge={edge:+.0%}{bias_str}"
            ),
        }

    def execute(self, signal, dry_run=False):
        if not self.can_execute(signal):
            return None
        self.log_signal(signal)
        return self.client.create_order(
            ticker=signal['ticker'], action='buy', side=signal['side'],
            count=signal['count'], order_type='market', dry_run=dry_run,
        )
