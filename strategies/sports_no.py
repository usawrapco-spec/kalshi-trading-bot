"""SportsNO strategy - fades sports favorites by buying the NO side.

Based on data from ryanfrigo/kalshi-ai-trading-bot showing NCAAB NO-side
had 74% win rate. Public betting bias systematically overprices favorites.
"""

from strategies.base import BaseStrategy
from utils.logger import setup_logger
from utils.market_helpers import get_yes_cents, get_volume

logger = setup_logger('sports_no')

FADE_MIN = 60
FADE_MAX = 85

SPORTS_KEYWORDS = [
    'ncaa', 'ncaab', 'ncaaf', 'nba', 'nfl', 'nhl', 'mlb', 'mls',
    'college basketball', 'college football', 'march madness',
    'basketball', 'football', 'hockey', 'baseball', 'soccer',
    'spread', 'moneyline', 'over under', 'total points',
    'win', 'beat', 'defeat', 'score', 'game', 'match', 'playoff',
    'champion', 'tournament', 'series', 'super bowl', 'world series',
]


class SportsNOStrategy(BaseStrategy):
    """Find sports markets with YES 60-85c and buy the NO side (fade favorites)."""

    def __init__(self, client, risk_manager, db):
        super().__init__(client, risk_manager, db)
        logger.info(f"SportsNO initialized (fade YES {FADE_MIN}-{FADE_MAX}c, buy NO)")

    def analyze(self, markets):
        signals = []
        sports_count = 0
        in_range = 0
        rejects = {}

        for m in markets:
            if m.get('status') != 'open':
                continue
            if not self._is_sports(m):
                continue

            sports_count += 1
            ticker = m.get('ticker', '')
            yes_cents = get_yes_cents(m)

            if not (FADE_MIN <= yes_cents <= FADE_MAX):
                if yes_cents > 0:
                    bucket = f"<{FADE_MIN}" if yes_cents < FADE_MIN else f">{FADE_MAX}"
                    rejects[bucket] = rejects.get(bucket, 0) + 1
                continue

            in_range += 1
            volume = get_volume(m)
            no_cents = 100 - yes_cents

            # Confidence: more expensive favorite = more confident fade
            confidence = 40 + (yes_cents - FADE_MIN) * 0.8
            if volume > 500:
                confidence += 15
            elif volume > 100:
                confidence += 10
            elif volume > 10:
                confidence += 5
            confidence = min(confidence, 100)

            # Edge: historical 74% win rate on NO vs implied probability
            implied_no = no_cents / 100.0
            est_no_prob = min(0.55 + (yes_cents - FADE_MIN) * 0.005, 0.74)
            edge = est_no_prob - implied_no

            logger.info(
                f"SportsNO: {ticker} YES={yes_cents}c -> buy NO={no_cents}c, "
                f"est_win={est_no_prob:.0%} vs implied={implied_no:.0%}, edge={edge:+.0%} "
                f"-> PAPER BUY NO"
            )

            signals.append({
                'ticker': ticker, 'title': m.get('title', ''), 'action': 'buy',
                'side': 'no', 'count': 5, 'confidence': confidence,
                'strategy_type': 'sports_no',
                'edge': max(edge, 0.01), 'model_prob': est_no_prob,
                'reason': f"SportsNO: fade YES={yes_cents}c, buy NO={no_cents}c, est_win={est_no_prob:.0%}, edge={edge:+.0%}",
            })

        reject_str = ', '.join(f"{v}x {k}" for k, v in sorted(rejects.items(), key=lambda x: -x[1])[:5])
        logger.info(
            f"SportsNO: {sports_count} sports markets, {in_range} in {FADE_MIN}-{FADE_MAX}c range, "
            f"{len(signals)} signals. Rejects: {reject_str or 'none'}"
        )
        return signals

    def _is_sports(self, m):
        ticker = (m.get('ticker') or '').lower()
        title = (m.get('title') or '').lower()
        category = (m.get('category') or '').lower()
        combined = f"{ticker} {title} {category}"
        return any(kw in combined for kw in SPORTS_KEYWORDS)

    def execute(self, signal, dry_run=False):
        if not self.can_execute(signal):
            return None
        self.log_signal(signal)
        return self.client.create_order(
            ticker=signal['ticker'], action='buy', side=signal['side'],
            count=signal['count'], order_type='market', dry_run=dry_run,
        )
