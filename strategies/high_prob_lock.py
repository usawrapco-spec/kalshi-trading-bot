"""HighProbLock strategy - buys near-locked-in markets at 92-98c for bond-like returns.

When YES is 92-98c, the market is saying >92% probability. If we're confident
the outcome is correct, buying YES yields 2-8% ROI - better than treasury bills.
Focus on weather settling today, decided sports, and other high-confidence categories.
"""

from datetime import datetime, timezone, timedelta
from strategies.base import BaseStrategy
from utils.logger import setup_logger
from utils.market_helpers import get_yes_price, get_no_price, get_volume, safe_float

logger = setup_logger('high_prob_lock')

LOCK_MIN = 0.92  # 92c minimum YES price
LOCK_MAX = 0.98  # 98c maximum (above this, ROI too thin)

# Categories/keywords that are high-confidence when at 92c+
HIGH_CONF_KEYWORDS = [
    'temperature', 'weather', 'high temp', 'kxhigh',
    'final score', 'already', 'currently', 'has been',
    'gdp', 'jobs report', 'unemployment', 'cpi', 'inflation',
    'fed rate', 'interest rate',
]


class HighProbLockStrategy(BaseStrategy):
    """Buy YES at 92-98c on high-confidence markets for bond-like ROI."""

    def __init__(self, client, risk_manager, db):
        super().__init__(client, risk_manager, db)
        logger.info(f"HighProbLock initialized (YES {LOCK_MIN:.0%}-{LOCK_MAX:.0%}, bond-like returns)")

    def analyze(self, markets):
        signals = []
        now = datetime.now(timezone.utc)
        checked = 0
        in_range = 0

        for m in markets:
            if m.get('status', 'open') != 'open':
                continue

            yes_price = get_yes_price(m)
            no_price = get_no_price(m)
            volume = get_volume(m)

            # Check YES side 92-98c
            if LOCK_MIN <= yes_price <= LOCK_MAX:
                in_range += 1
                sig = self._evaluate(m, 'yes', yes_price, volume, now)
                if sig:
                    signals.append(sig)
                    continue

            # Also check NO side 92-98c
            if LOCK_MIN <= no_price <= LOCK_MAX:
                in_range += 1
                sig = self._evaluate(m, 'no', no_price, volume, now)
                if sig:
                    signals.append(sig)

            checked += 1

        # Cap to top 3 - each costs ~$0.95 so 3 = ~$2.85 from $10 balance
        signals.sort(key=lambda s: s.get('confidence', 0), reverse=True)
        signals = signals[:3]
        logger.info(f"HighProbLock: {in_range} markets in {LOCK_MIN:.0%}-{LOCK_MAX:.0%} range, {len(signals)} signals (top 3)")
        return signals

    def _evaluate(self, m, side, price, volume, now):
        ticker = m.get('ticker', '')
        title = (m.get('title') or '').lower()
        close_time = m.get('close_time') or m.get('expiration_time') or ''

        # Calculate ROI
        roi = (1.0 - price) / price  # e.g. buy at 0.95, win 1.00 = 5.3% ROI

        # Higher confidence for markets closing soon
        hours_left = 999
        if close_time:
            try:
                close_dt = datetime.fromisoformat(close_time.replace('Z', '+00:00'))
                hours_left = max(0, (close_dt - now).total_seconds() / 3600)
            except Exception:
                pass

        # Confidence scoring
        confidence = price * 50  # Base: 46-49 from price

        # Bonus for settling soon (more certain at this price)
        if hours_left < 6:
            confidence += 25
        elif hours_left < 12:
            confidence += 18
        elif hours_left < 24:
            confidence += 12
        elif hours_left < 48:
            confidence += 5

        # Bonus for high-confidence categories
        is_high_conf = any(kw in title for kw in HIGH_CONF_KEYWORDS)
        if is_high_conf:
            confidence += 10

        # Bonus for volume (liquid = reliable price)
        if volume > 500:
            confidence += 10
        elif volume > 100:
            confidence += 5

        confidence = min(confidence, 100)

        # Require decent confidence - don't lock in money on low-confidence
        if confidence < 65:
            return None

        # Require market closing within 48h for lock trades
        if hours_left > 48:
            return None

        logger.info(
            f"HighProbLock: {ticker} {side.upper()} at {price:.0%}, ROI={roi:.1%}, "
            f"{hours_left:.1f}h left, conf={confidence:.0f}"
        )

        return {
            'ticker': ticker, 'title': m.get('title', ''), 'action': 'buy',
            'side': side, 'count': 1, 'confidence': confidence,
            'strategy_type': 'high_prob_lock',
            'edge': roi, 'model_prob': price,
            'reason': f"HighProbLock: {side.upper()} at {price:.0%}, ROI={roi:.1%}, {hours_left:.1f}h left",
        }

    def execute(self, signal, dry_run=False):
        if not self.can_execute(signal):
            return None
        self.log_signal(signal)
        return self.client.create_order(
            ticker=signal['ticker'], action='buy', side=signal['side'],
            count=signal['count'], order_type='market', dry_run=dry_run,
        )
