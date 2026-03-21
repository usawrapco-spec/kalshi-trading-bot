"""WeatherEdge strategy - compares Open-Meteo ensemble forecasts to Kalshi temperature markets."""

import requests
from datetime import datetime, timedelta
from strategies.base import BaseStrategy
from utils.logger import setup_logger

logger = setup_logger('weather_edge')

# City configs: Kalshi KXHIGH ticker prefix -> Open-Meteo coordinates
CITIES = {
    'NYC': {'lat': 40.7128, 'lon': -74.0060, 'name': 'New York'},
    'CHI': {'lat': 41.8781, 'lon': -87.6298, 'name': 'Chicago'},
    'MIA': {'lat': 25.7617, 'lon': -80.1918, 'name': 'Miami'},
    'LA':  {'lat': 34.0522, 'lon': -118.2437, 'name': 'Los Angeles'},
    'DEN': {'lat': 39.7392, 'lon': -104.9903, 'name': 'Denver'},
}

OPEN_METEO_URL = 'https://ensemble-api.open-meteo.com/v1/ensemble'
MIN_EDGE = 0.05  # 5% minimum edge to trade


class WeatherEdgeStrategy(BaseStrategy):
    """
    Fetches GFS ensemble forecasts from Open-Meteo (31 members, no API key)
    and compares calculated probabilities to Kalshi KXHIGH temperature markets.
    Trades when our probability differs from market price by >5%.
    """

    def __init__(self, client, risk_manager, db):
        super().__init__(client, risk_manager, db)
        self._forecast_cache = {}
        self._cache_time = None
        logger.info("WeatherEdge strategy initialized")

    def analyze(self, markets):
        signals = []

        # Filter to temperature/weather markets with broad keyword search
        temp_markets = [m for m in markets if self._is_temp_market(m)]

        # Log diagnostic info regardless
        kxhigh_count = sum(1 for m in markets if 'KXHIGH' in m.get('ticker', ''))
        weather_kw_count = sum(1 for m in markets if self._has_weather_keywords(m))
        logger.info(
            f"WeatherEdge scan: {kxhigh_count} KXHIGH tickers, "
            f"{weather_kw_count} weather keyword matches, "
            f"{len(temp_markets)} total weather markets"
        )

        if not temp_markets:
            # Log some sample tickers so we can see what's available
            sample_tickers = [m.get('ticker', '?') for m in markets[:20]]
            logger.info(f"WeatherEdge: no weather markets. Sample tickers: {sample_tickers}")
            return signals

        logger.info(f"Found {len(temp_markets)} temperature/weather markets")

        # Refresh forecasts if stale (>30 min)
        now = datetime.utcnow()
        if not self._cache_time or (now - self._cache_time).seconds > 1800:
            self._refresh_forecasts()

        for market in temp_markets:
            signal = self._evaluate_market(market)
            if signal:
                signals.append(signal)

        return signals

    def _is_temp_market(self, market):
        ticker = market.get('ticker', '').upper()
        title = market.get('title', '').lower()
        # Match KXHIGH tickers directly
        if 'KXHIGH' in ticker:
            return True
        # Match any weather/temperature keywords in ticker or title
        return self._has_weather_keywords(market)

    def _has_weather_keywords(self, market):
        """Check if market relates to weather/temperature via broad keyword search."""
        ticker = market.get('ticker', '').upper()
        title = market.get('title', '').lower()
        combined = ticker + ' ' + title
        weather_keywords = [
            'temperature', 'temp ', 'degrees', 'fahrenheit', 'celsius',
            'weather', 'high temp', 'low temp', 'rain', 'snow', 'precipitation',
            'KXHIGH', 'KXLOW', 'KXRAIN', 'KXSNOW', 'KXWEATHER',
            'heat', 'cold', 'freeze', 'frost',
        ]
        for kw in weather_keywords:
            if kw.lower() in combined.lower():
                return True
        return False

    def _refresh_forecasts(self):
        """Fetch GFS ensemble forecasts for all cities."""
        self._forecast_cache = {}
        for city_code, city in CITIES.items():
            try:
                forecast = self._fetch_ensemble(city['lat'], city['lon'])
                if forecast:
                    self._forecast_cache[city_code] = forecast
                    logger.info(f"Forecast loaded for {city['name']}: {len(forecast)} days")
            except Exception as e:
                logger.error(f"Failed to fetch forecast for {city['name']}: {e}")
        self._cache_time = datetime.utcnow()

    def _fetch_ensemble(self, lat, lon):
        """Fetch 31-member GFS ensemble from Open-Meteo."""
        params = {
            'latitude': lat,
            'longitude': lon,
            'daily': 'temperature_2m_max',
            'temperature_unit': 'fahrenheit',
            'forecast_days': 7,
            'models': 'gfs_seamless',
        }
        resp = requests.get(OPEN_METEO_URL, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        daily = data.get('daily', {})
        dates = daily.get('time', [])

        # Collect all ensemble member columns
        member_keys = [k for k in daily if k.startswith('temperature_2m_max')]
        if not member_keys:
            return None

        # Build {date: [temp1, temp2, ...]} from all members
        result = {}
        for i, date_str in enumerate(dates):
            temps = []
            for key in member_keys:
                vals = daily[key]
                if i < len(vals) and vals[i] is not None:
                    temps.append(vals[i])
            if temps:
                result[date_str] = temps

        return result

    def _calc_probability_above(self, temps, threshold):
        """Fraction of ensemble members above threshold."""
        if not temps:
            return 0.5
        above = sum(1 for t in temps if t >= threshold)
        return above / len(temps)

    def _parse_market_threshold(self, market):
        """Extract city code, date, and temperature threshold from market ticker/title."""
        ticker = market.get('ticker', '')
        title = market.get('title', '').lower()

        # Try to find city
        city_code = None
        for code in CITIES:
            if code in ticker or CITIES[code]['name'].lower() in title:
                city_code = code
                break
        if not city_code:
            return None

        # Try to extract threshold temperature from title
        # e.g. "Will NYC high temp be 80°F or above?"
        import re
        temp_match = re.search(r'(\d{2,3})\s*(?:°|degrees|f\b)', title)
        if not temp_match:
            temp_match = re.search(r'above\s+(\d{2,3})', title)
        if not temp_match:
            temp_match = re.search(r'(\d{2,3})\s*or\s*(?:above|higher|more)', title)
        if not temp_match:
            return None

        threshold = int(temp_match.group(1))

        # Try to find the target date from close_time
        close_time = market.get('close_time', '')
        target_date = None
        if close_time:
            try:
                dt = datetime.fromisoformat(close_time.replace('Z', '+00:00'))
                target_date = dt.strftime('%Y-%m-%d')
            except Exception:
                pass

        return {'city': city_code, 'threshold': threshold, 'date': target_date}

    def _evaluate_market(self, market):
        """Compare our ensemble probability to the market price."""
        parsed = self._parse_market_threshold(market)
        if not parsed:
            return None

        city = parsed['city']
        threshold = parsed['threshold']
        target_date = parsed['date']

        forecasts = self._forecast_cache.get(city)
        if not forecasts:
            return None

        # Find matching date or closest
        temps = None
        if target_date and target_date in forecasts:
            temps = forecasts[target_date]
        else:
            # Use first available date
            for d in sorted(forecasts.keys()):
                if not target_date or d >= target_date:
                    temps = forecasts[d]
                    break

        if not temps:
            return None

        our_prob = self._calc_probability_above(temps, threshold)
        raw_price = market.get('yes_bid') or market.get('yes_ask') or market.get('last_price') or 0
        market_yes_price = raw_price / 100.0  # convert cents to probability
        market_no_price = 1.0 - market_yes_price

        if market_yes_price <= 0:
            return None

        # Calculate edge
        yes_edge = our_prob - market_yes_price
        no_edge = (1 - our_prob) - market_no_price

        ticker = market.get('ticker', '')
        ensemble_agreement = max(our_prob, 1 - our_prob)

        if yes_edge > MIN_EDGE:
            confidence = min(ensemble_agreement * 80 + abs(yes_edge) * 100, 100)
            logger.info(
                f"WeatherEdge YES: {ticker} prob={our_prob:.1%} vs market={market_yes_price:.1%} "
                f"edge={yes_edge:.1%} agreement={ensemble_agreement:.1%}"
            )
            return {
                'ticker': ticker,
                'action': 'buy',
                'side': 'yes',
                'count': 5,
                'reason': (
                    f'WeatherEdge: ensemble prob={our_prob:.0%} vs market={market_yes_price:.0%}, '
                    f'edge={yes_edge:.0%}, {len(temps)} members, '
                    f'{CITIES[city]["name"]} >={threshold}F'
                ),
                'confidence': confidence,
                'strategy_type': 'weather_edge',
                'edge': yes_edge,
                'model_prob': our_prob,
            }

        if no_edge > MIN_EDGE:
            confidence = min(ensemble_agreement * 80 + abs(no_edge) * 100, 100)
            logger.info(
                f"WeatherEdge NO: {ticker} prob_no={1-our_prob:.1%} vs market_no={market_no_price:.1%} "
                f"edge={no_edge:.1%} agreement={ensemble_agreement:.1%}"
            )
            return {
                'ticker': ticker,
                'action': 'buy',
                'side': 'no',
                'count': 5,
                'reason': (
                    f'WeatherEdge: ensemble prob_no={1-our_prob:.0%} vs market_no={market_no_price:.0%}, '
                    f'edge={no_edge:.0%}, {len(temps)} members, '
                    f'{CITIES[city]["name"]} <{threshold}F'
                ),
                'confidence': confidence,
                'strategy_type': 'weather_edge',
                'edge': no_edge,
                'model_prob': 1 - our_prob,
            }

        return None

    def execute(self, signal, dry_run=False):
        if not self.can_execute(signal):
            return None

        self.log_signal(signal)

        order = self.client.create_order(
            ticker=signal['ticker'],
            action=signal['action'],
            side=signal['side'],
            count=signal['count'],
            order_type='market',
            dry_run=dry_run
        )

        if order and not dry_run:
            self.risk_manager.update_position(
                signal['ticker'], signal['count'], signal['side']
            )
            if self.db:
                self.db.log_trade({
                    'ticker': signal['ticker'],
                    'action': signal['action'],
                    'side': signal['side'],
                    'count': signal['count'],
                    'strategy': self.name,
                    'reason': signal.get('reason'),
                    'confidence': signal.get('confidence'),
                    'order_id': order.get('order_id'),
                    'price': order.get('yes_price') or order.get('no_price'),
                })

        return order
