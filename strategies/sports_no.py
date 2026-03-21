"""SportsNO strategy - fades favorites in sports markets by buying NO.

Based on real data from ryanfrigo/kalshi-ai-trading-bot showing NCAAB
NO-side trading had a 74% win rate. Sports favorites are systematically
overpriced due to public betting bias.
"""

from strategies.base import BaseStrategy
from utils.logger import setup_logger

logger = setup_logger('sports_no')

# YES price range where we fade (buy NO): favorites priced 60-85c
FADE_YES_MIN = 60
FADE_YES_MAX = 85

# Sports keywords to match in ticker or title
SPORTS_KEYWORDS = [
    # Leagues
    'ncaa', 'ncaab', 'ncaaf', 'nba', 'nfl', 'nhl', 'mlb', 'mls',
    'college basketball', 'college football', 'march madness',
    # Generic sports
    'basketball', 'football', 'hockey', 'baseball', 'soccer',
    # Market types
    'spread', 'moneyline', 'over under', 'total points',
    'win', 'beat', 'defeat', 'score',
    # Kalshi sports ticker prefixes
    'KXNBA', 'KXNFL', 'KXNHL', 'KXMLB', 'KXNCAA', 'KXSPORT',
]


class SportsNOStrategy(BaseStrategy):
    """
    Fades sports favorites by buying the NO side when YES is priced 60-85c.

    Rationale: public betting bias inflates favorites. Data from real
    Kalshi trading bots shows NCAAB NO-side had 74% win rate. The edge
    comes from the public systematically overvaluing favorites.
    """

    def __init__(self, client, risk_manager, db):
        super().__init__(client, risk_manager, db)
        logger.info(
            f"SportsNO strategy initialized "
            f"(fade YES {FADE_YES_MIN}-{FADE_YES_MAX}c, buy NO)"
        )

    def analyze(self, markets):
        signals = []
        sports_count = 0
        in_range_count = 0
        rejected_reasons = {}

        for market in markets:
            if market.get('status') != 'open':
                continue

            if not self._is_sports_market(market):
                continue

            sports_count += 1
            ticker = market.get('ticker', '')
            yes_price = (
                market.get('yes_bid')
                or market.get('yes_ask')
                or market.get('last_price')
                or 0
            )
            volume = market.get('volume') or 0

            if not (FADE_YES_MIN <= yes_price <= FADE_YES_MAX):
                if yes_price > 0:
                    bucket = (
                        f"price {yes_price}c outside {FADE_YES_MIN}-{FADE_YES_MAX}c"
                    )
                    rejected_reasons[bucket] = rejected_reasons.get(bucket, 0) + 1
                continue

            in_range_count += 1

            # NO price is roughly 100 - yes_price
            no_price = 100 - yes_price
            profit_per_contract = yes_price  # if NO wins, we get 100c - no_price paid

            # Confidence: based on how overpriced the favorite looks + volume
            # Higher YES price = more overpriced favorite = more confident fade
            confidence = 40.0
            confidence += (yes_price - FADE_YES_MIN) * 0.8  # 0-20 points from price
            if volume > 500:
                confidence += 15
            elif volume > 100:
                confidence += 10
            elif volume > 10:
                confidence += 5
            confidence = min(confidence, 100)

            # Edge estimate: historical 74% win rate on NO at these prices
            # vs implied probability of (100 - yes_price)%
            implied_no_prob = no_price / 100.0
            estimated_no_prob = 0.55 + (yes_price - FADE_YES_MIN) * 0.005
            estimated_no_prob = min(estimated_no_prob, 0.74)
            edge = estimated_no_prob - implied_no_prob

            logger.info(
                f"SportsNO: {ticker} YES={yes_price}c -> buy NO at {no_price}c, "
                f"est_win={estimated_no_prob:.0%} vs implied={implied_no_prob:.0%}, "
                f"edge={edge:.0%}, vol={volume}, conf={confidence:.0f}"
            )

            signals.append({
                'ticker': ticker,
                'action': 'buy',
                'side': 'no',
                'count': 5,
                'reason': (
                    f'SportsNO: fade favorite YES={yes_price}c, '
                    f'buy NO={no_price}c, est_win={estimated_no_prob:.0%}, '
                    f'edge={edge:.0%}, vol={volume}'
                ),
                'confidence': confidence,
                'strategy_type': 'sports_no',
                'edge': max(edge, 0.01),
                'model_prob': estimated_no_prob,
            })

        # Diagnostic logging
        reject_summary = ', '.join(
            f"{v}x {k}" for k, v in sorted(
                rejected_reasons.items(), key=lambda x: -x[1]
            )[:5]
        )
        logger.info(
            f"SportsNO scan: {sports_count} sports markets found, "
            f"{in_range_count} in {FADE_YES_MIN}-{FADE_YES_MAX}c range, "
            f"{len(signals)} signals. "
            f"Rejections: {reject_summary or 'none'}"
        )

        return signals

    def _is_sports_market(self, market):
        """Check if market is sports-related via keyword matching."""
        ticker = market.get('ticker', '').upper()
        title = market.get('title', '').lower()
        category = market.get('category', '').lower()
        combined = f"{ticker} {title} {category}"

        for kw in SPORTS_KEYWORDS:
            if kw.lower() in combined.lower():
                return True
        return False

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
