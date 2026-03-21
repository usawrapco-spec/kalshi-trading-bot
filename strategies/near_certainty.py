"""NearCertainty strategy - trades markets settling within 24h where one side is 90-97c,
and also looks for cheap 3-10c contrarian buys."""

import re
import requests
from datetime import datetime, timezone, timedelta
from strategies.base import BaseStrategy
from utils.logger import setup_logger

logger = setup_logger('near_certainty')

NEAR_CERTAIN_MIN = 90
NEAR_CERTAIN_MAX = 97
CHEAP_MIN = 3
CHEAP_MAX = 10


class NearCertaintyStrategy(BaseStrategy):
    """
    Two modes:
    1. Buy the near-certain side (90-97c) on markets settling within 24 hours.
       Cross-references weather data when applicable. Small profit, high win rate.
    2. Buy cheap NO on 3-10c markets for asymmetric upside.
    """

    def __init__(self, client, risk_manager, db):
        super().__init__(client, risk_manager, db)
        logger.info("NearCertainty strategy initialized (90-97c near-certain, 3-10c cheap contrarian)")

    def analyze(self, markets):
        signals = []
        now = datetime.now(timezone.utc)
        cutoff = now + timedelta(hours=24)

        # Diagnostic counters
        near_certain_count = 0  # markets in 90-97c range
        near_certain_wide_count = 0  # markets in 85-99c range
        cheap_count = 0  # markets in 3-10c range
        cheap_wide_count = 0  # markets in 1-15c range
        closing_soon_count = 0

        for market in markets:
            if market.get('status') != 'open':
                continue

            # Try multiple price fields
            yes_bid = market.get('yes_bid') or market.get('yes_ask') or market.get('last_price') or 0
            no_bid = market.get('no_bid') or market.get('no_ask') or 0
            # Store resolved prices back so downstream methods see them
            market['_yes_price'] = yes_bid
            market['_no_price'] = no_bid

            # Count price ranges for diagnostics
            for price in [yes_bid, no_bid]:
                if 90 <= price <= 97:
                    near_certain_count += 1
                if 85 <= price <= 99:
                    near_certain_wide_count += 1
                if 3 <= price <= 10:
                    cheap_count += 1
                if 1 <= price <= 15:
                    cheap_wide_count += 1

            close_time = market.get('close_time', '')

            # Mode 1: Near-certain trades (require <24h to close)
            if close_time:
                try:
                    close_dt = datetime.fromisoformat(close_time.replace('Z', '+00:00'))
                    if now < close_dt <= cutoff:
                        closing_soon_count += 1
                        hours_left = (close_dt - now).total_seconds() / 3600
                        signal = self._check_near_certain(market, hours_left)
                        if signal:
                            signals.append(signal)
                            continue
                except Exception:
                    pass

            # Mode 2: Cheap contrarian buys (any timeframe)
            signal = self._check_cheap_contrarian(market)
            if signal:
                signals.append(signal)

        logger.info(
            f"NearCertainty scan: {closing_soon_count} closing <24h, "
            f"{near_certain_count} in 90-97c ({near_certain_wide_count} in 85-99c), "
            f"{cheap_count} in 3-10c ({cheap_wide_count} in 1-15c), "
            f"{len(signals)} signals"
        )

        return signals

    def _check_near_certain(self, market, hours_left):
        """Buy the near-certain side of markets settling within 24h."""
        ticker = market.get('ticker', '')
        title = market.get('title', '').lower()
        yes_bid = market.get('_yes_price', 0)
        no_bid = market.get('_no_price', 0)
        volume = market.get('volume') or 0

        side = None
        price = 0
        # Try strict range first (90-97), then expanded range (85-99)
        if NEAR_CERTAIN_MIN <= yes_bid <= NEAR_CERTAIN_MAX:
            side = 'yes'
            price = yes_bid
        elif NEAR_CERTAIN_MIN <= no_bid <= NEAR_CERTAIN_MAX:
            side = 'no'
            price = no_bid
        elif 85 <= yes_bid <= 99:
            side = 'yes'
            price = yes_bid
        elif 85 <= no_bid <= 99:
            side = 'no'
            price = no_bid
        else:
            return None

        # Cross-reference weather markets with Open-Meteo
        confirmed_by = 'market consensus'
        if any(kw in title for kw in ['temperature', 'high temp', 'degrees', 'kxhigh']):
            weather_ok, source = self._verify_weather(title)
            if weather_ok:
                confirmed_by = source
            elif price < 94:
                # Weather unconfirmed and price not high enough to trust alone
                return None

        profit_per_contract = 100 - price
        implied_prob = price / 100.0

        # Confidence: higher price + less time + more volume = more confident
        confidence = implied_prob * 55
        if hours_left < 4:
            confidence += 25
        elif hours_left < 8:
            confidence += 18
        elif hours_left < 16:
            confidence += 10
        else:
            confidence += 5
        if volume > 500:
            confidence += 15
        elif volume > 100:
            confidence += 8
        elif volume > 10:
            confidence += 3
        confidence = min(confidence, 100)

        logger.info(
            f"NearCertainty: {ticker} {side.upper()} at {price}c, "
            f"{hours_left:.1f}h left, profit={profit_per_contract}c, "
            f"confirmed by {confirmed_by}, conf={confidence:.0f}"
        )

        return {
            'ticker': ticker,
            'action': 'buy',
            'side': side,
            'count': 10,
            'reason': (
                f'NearCertainty: {side.upper()} at {price}c, '
                f'{hours_left:.1f}h to close, profit={profit_per_contract}c/contract, '
                f'confirmed={confirmed_by}, vol={volume}'
            ),
            'confidence': confidence,
            'strategy_type': 'near_certainty',
            'edge': profit_per_contract / 100.0,
            'model_prob': implied_prob,
        }

    def _check_cheap_contrarian(self, market):
        """Buy cheap contracts (1-15c) for asymmetric upside."""
        ticker = market.get('ticker', '')
        yes_bid = market.get('_yes_price', 0)
        no_bid = market.get('_no_price', 0)
        volume = market.get('volume') or 0

        side = None
        price = 0

        # Expanded range: 1-15c, no volume gate
        if 1 <= yes_bid <= 15:
            side = 'yes'
            price = yes_bid
        elif 1 <= no_bid <= 15:
            side = 'no'
            price = no_bid
        else:
            return None

        profit_potential = 100 - price
        confidence = 30 + min(volume / 100, 20)  # Low confidence, high reward

        logger.info(
            f"Cheap contrarian: {ticker} {side.upper()} at {price}c, "
            f"potential profit={profit_potential}c, vol={volume}"
        )

        return {
            'ticker': ticker,
            'action': 'buy',
            'side': side,
            'count': 3,  # Small size for speculative bets
            'reason': (
                f'Cheap contrarian: {side.upper()} at {price}c, '
                f'potential profit={profit_potential}c, vol={volume}'
            ),
            'confidence': confidence,
            'strategy_type': 'near_certainty',
            'edge': 0.05,  # Nominal edge
            'model_prob': price / 100.0,
        }

    def _verify_weather(self, title):
        """Quick weather verification using Open-Meteo deterministic forecast."""
        city_coords = {
            'new york': (40.7128, -74.0060), 'nyc': (40.7128, -74.0060),
            'chicago': (41.8781, -87.6298),
            'miami': (25.7617, -80.1918),
            'los angeles': (34.0522, -118.2437),
            'denver': (39.7392, -104.9903),
        }

        lat, lon = None, None
        for city_name, coords in city_coords.items():
            if city_name in title:
                lat, lon = coords
                break
        if lat is None:
            return False, ''

        temp_match = re.search(r'(\d{2,3})\s*(?:°|degrees|f\b)', title)
        if not temp_match:
            temp_match = re.search(r'(\d{2,3})\s*or\s*(?:above|higher)', title)
        if not temp_match:
            return False, ''

        threshold = int(temp_match.group(1))

        try:
            resp = requests.get(
                'https://api.open-meteo.com/v1/forecast',
                params={
                    'latitude': lat, 'longitude': lon,
                    'daily': 'temperature_2m_max',
                    'temperature_unit': 'fahrenheit',
                    'forecast_days': 2,
                },
                timeout=10,
            )
            resp.raise_for_status()
            temps = resp.json().get('daily', {}).get('temperature_2m_max', [])
            if temps:
                forecast = temps[0]
                margin = forecast - threshold
                if abs(margin) >= 3:
                    return True, f'Open-Meteo forecast {forecast:.0f}F vs {threshold}F'
        except Exception:
            pass

        return False, ''

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
