"""NearCertainty strategy - trades markets near expiry where outcome is almost decided.

Buys the near-certain side (85-97c) on markets closing within 24h for small
guaranteed-ish profit, and buys cheap contracts (3-15c) for asymmetric upside.
"""

import re
import requests
from datetime import datetime, timezone, timedelta
from strategies.base import BaseStrategy
from utils.logger import setup_logger
from utils.market_helpers import get_yes_cents, get_no_cents, get_volume

logger = setup_logger('near_certainty')


class NearCertaintyStrategy(BaseStrategy):
    """Markets closing <24h: buy 85-97c side or cheap 3-15c contrarian."""

    def __init__(self, client, risk_manager, db):
        super().__init__(client, risk_manager, db)
        logger.info("NearCertainty initialized (85-97c near-certain, 3-15c contrarian, <24h)")

    def analyze(self, markets):
        signals = []
        now = datetime.now(timezone.utc)
        cutoff = now + timedelta(hours=24)

        # Diagnostic counters
        nc_count = 0      # 85-97c range
        nc_wide = 0       # 80-99c range
        cheap_count = 0   # 3-15c range
        closing_soon = 0

        for m in markets:
            if m.get('status') != 'open':
                continue

            yes_c = get_yes_cents(m)
            no_c = get_no_cents(m)

            # Count ranges for diagnostics
            for p in [yes_c, no_c]:
                if 85 <= p <= 97: nc_count += 1
                if 80 <= p <= 99: nc_wide += 1
                if 3 <= p <= 15: cheap_count += 1

            close_time = m.get('close_time') or m.get('expiration_time') or ''

            # Mode 1: Near-certain (requires closing <24h)
            if close_time:
                try:
                    close_dt = datetime.fromisoformat(close_time.replace('Z', '+00:00'))
                    if now < close_dt <= cutoff:
                        closing_soon += 1
                        hours_left = (close_dt - now).total_seconds() / 3600
                        sig = self._check_near_certain(m, yes_c, no_c, hours_left)
                        if sig:
                            signals.append(sig)
                            continue
                except Exception:
                    pass

            # Mode 2: Cheap contrarian (any timeframe)
            sig = self._check_cheap(m, yes_c, no_c)
            if sig:
                signals.append(sig)

        logger.info(
            f"NearCertainty: {closing_soon} closing <24h, "
            f"{nc_count} in 85-97c ({nc_wide} in 80-99c), "
            f"{cheap_count} in 3-15c, {len(signals)} signals"
        )
        return signals

    def _check_near_certain(self, m, yes_c, no_c, hours_left):
        ticker = m.get('ticker', '')
        volume = get_volume(m)

        side, price = None, 0
        if 85 <= yes_c <= 97:
            side, price = 'yes', yes_c
        elif 85 <= no_c <= 97:
            side, price = 'no', no_c
        # Expanded fallback
        elif 80 <= yes_c <= 99:
            side, price = 'yes', yes_c
        elif 80 <= no_c <= 99:
            side, price = 'no', no_c
        else:
            return None

        profit = 100 - price
        implied = price / 100.0

        # Confidence: price + time proximity + volume
        confidence = implied * 55
        if hours_left < 4: confidence += 25
        elif hours_left < 8: confidence += 18
        elif hours_left < 16: confidence += 10
        else: confidence += 5
        if volume > 500: confidence += 15
        elif volume > 100: confidence += 8
        elif volume > 10: confidence += 3
        confidence = min(confidence, 100)

        # Weather cross-reference for temperature markets
        confirmed = 'market consensus'
        title_lower = (m.get('title') or '').lower()
        if any(kw in title_lower for kw in ['temperature', 'high temp', 'degrees']):
            ok, src = self._verify_weather(title_lower)
            if ok:
                confirmed = src
                confidence = min(confidence + 10, 100)

        logger.info(
            f"NearCertainty: {ticker} {side.upper()} at {price}c, {hours_left:.1f}h left, "
            f"profit={profit}c, confirmed={confirmed} -> PAPER BUY {side.upper()}"
        )

        return {
            'ticker': ticker, 'title': m.get('title', ''), 'action': 'buy',
            'side': side, 'count': 10, 'confidence': confidence,
            'strategy_type': 'near_certainty',
            'edge': profit / 100.0, 'model_prob': implied,
            'reason': f"NearCertainty: {side.upper()} at {price}c, {hours_left:.1f}h to close, profit={profit}c, confirmed={confirmed}",
        }

    def _check_cheap(self, m, yes_c, no_c):
        ticker = m.get('ticker', '')
        volume = get_volume(m)

        side, price = None, 0
        if 3 <= yes_c <= 15:
            side, price = 'yes', yes_c
        elif 3 <= no_c <= 15:
            side, price = 'no', no_c
        # Expanded: 1-2c too
        elif 1 <= yes_c <= 2 and volume > 50:
            side, price = 'yes', yes_c
        elif 1 <= no_c <= 2 and volume > 50:
            side, price = 'no', no_c
        else:
            return None

        profit_potential = 100 - price
        confidence = 30 + min(volume / 100, 20)

        logger.info(
            f"NearCertainty cheap: {ticker} {side.upper()} at {price}c, "
            f"potential={profit_potential}c, vol={volume}"
        )

        return {
            'ticker': ticker, 'title': m.get('title', ''), 'action': 'buy',
            'side': side, 'count': 3, 'confidence': confidence,
            'strategy_type': 'near_certainty',
            'edge': 0.05, 'model_prob': price / 100.0,
            'reason': f"NearCertainty cheap: {side.upper()} at {price}c, potential={profit_potential}c, vol={volume}",
        }

    def _verify_weather(self, title):
        coords = {
            'new york': (40.71, -74.01), 'nyc': (40.71, -74.01),
            'chicago': (41.88, -87.63), 'miami': (25.76, -80.19),
            'los angeles': (34.05, -118.24), 'denver': (39.74, -104.99),
        }
        lat, lon = None, None
        for name, c in coords.items():
            if name in title:
                lat, lon = c
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
            resp = requests.get('https://api.open-meteo.com/v1/forecast', params={
                'latitude': lat, 'longitude': lon,
                'daily': 'temperature_2m_max', 'temperature_unit': 'fahrenheit', 'forecast_days': 2,
            }, timeout=10)
            resp.raise_for_status()
            temps = resp.json().get('daily', {}).get('temperature_2m_max', [])
            if temps and abs(temps[0] - threshold) >= 3:
                return True, f'Open-Meteo {temps[0]:.0f}F vs {threshold}F'
        except Exception:
            pass
        return False, ''

    def execute(self, signal, dry_run=False):
        if not self.can_execute(signal):
            return None
        self.log_signal(signal)
        return self.client.create_order(
            ticker=signal['ticker'], action='buy', side=signal['side'],
            count=signal['count'], order_type='market', dry_run=dry_run,
        )
