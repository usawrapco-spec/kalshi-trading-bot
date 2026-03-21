"""MentionMarkets strategy - trades celebrity/pop culture mention markets.

These markets are frequently mispriced due to low volume. Uses Grok to
assess true probability of mentions, tweets, and trending topics.
"""

import os
import re
import json
import requests
from strategies.base import BaseStrategy
from utils.logger import setup_logger
from utils.market_helpers import get_yes_price, get_volume, safe_float

logger = setup_logger('mention_markets')

XAI_URL = 'https://api.x.ai/v1/chat/completions'
MODEL = 'grok-4-1-fast-non-reasoning'

MENTION_KEYWORDS = [
    'mention', 'say', 'tweet', 'post', 'search', 'google', 'trending',
    'talk about', 'refer to', 'comment on', 'respond', 'reply',
    'announce', 'endorse', 'criticize', 'praise', 'call out',
]

# Buy YES at 25-60c when Grok says higher, buy NO at 70-90c on near-certainties
YES_BUY_MIN = 0.25
YES_BUY_MAX = 0.60
NO_BUY_MIN = 0.70
NO_BUY_MAX = 0.90
MAX_GROK_CALLS = 10


class MentionMarketsStrategy(BaseStrategy):
    """Find mention/pop-culture markets and use Grok to assess true probability."""

    def __init__(self, client, risk_manager, db):
        super().__init__(client, risk_manager, db)
        self.api_key = os.getenv('XAI_API_KEY')
        if not self.api_key:
            logger.warning("XAI_API_KEY not set - MentionMarkets disabled")
        else:
            logger.info("MentionMarkets initialized (Grok-powered mention analysis)")

    def analyze(self, markets):
        if not self.api_key:
            return []

        mention_mkts = [m for m in markets if self._is_mention(m)]
        logger.info(f"MentionMarkets: {len(mention_mkts)} mention/pop-culture markets found")

        if not mention_mkts:
            return []

        # Sort by volume, take top candidates
        mention_mkts.sort(key=lambda m: get_volume(m), reverse=True)
        candidates = mention_mkts[:MAX_GROK_CALLS]

        signals = []
        for m in candidates:
            sig = self._evaluate(m)
            if sig:
                signals.append(sig)

        logger.info(f"MentionMarkets: {len(signals)} signals from {len(candidates)} candidates")
        return signals

    def _is_mention(self, m):
        title = (m.get('title') or '').lower()
        subtitle = (m.get('subtitle') or '').lower()
        combined = f"{title} {subtitle}"
        return any(kw in combined for kw in MENTION_KEYWORDS)

    def _evaluate(self, m):
        ticker = m.get('ticker', '')
        title = m.get('title', '')
        subtitle = m.get('subtitle', '')
        yes_price = get_yes_price(m)
        volume = get_volume(m)

        if yes_price <= 0:
            return None

        desc = title
        if subtitle:
            desc += f' ({subtitle})'

        # Determine trade direction based on price range
        if YES_BUY_MIN <= yes_price <= YES_BUY_MAX:
            # Potential YES buy - ask Grok if probability is higher
            mode = 'yes_candidate'
        elif NO_BUY_MIN <= yes_price <= NO_BUY_MAX:
            # Potential NO buy - "junk bond" near-certainty fade
            mode = 'no_candidate'
        else:
            return None

        prompt = (
            f"You are analyzing a Kalshi prediction market about mentions/social media. "
            f"Market: \"{desc}\". Current YES price: ${yes_price:.2f} (implying {yes_price*100:.0f}% probability). "
            f"Based on X/Twitter activity, recent behavior patterns, and current events, "
            f"what is the TRUE probability this resolves YES? "
            f"Respond with ONLY a JSON object: "
            f'{{\"probability\": 0.XX, \"confidence\": 0.XX, \"reasoning\": \"one sentence\"}}'
        )

        try:
            resp = requests.post(XAI_URL, headers={
                'Authorization': f'Bearer {self.api_key}',
                'Content-Type': 'application/json',
            }, json={
                'model': MODEL,
                'messages': [{'role': 'user', 'content': prompt}],
                'temperature': 0.1,
                'max_tokens': 200,
            }, timeout=30)
            resp.raise_for_status()
            text = resp.json()['choices'][0]['message']['content'].strip()
        except Exception as e:
            logger.debug(f"MentionMarkets Grok error for {ticker}: {e}")
            return None

        try:
            clean = text
            if '```' in clean:
                clean = re.sub(r'```\w*\n?', '', clean).strip()
            result = json.loads(clean)
        except json.JSONDecodeError:
            prob_match = re.search(r'"probability"\s*:\s*([\d.]+)', text)
            if prob_match:
                result = {'probability': float(prob_match.group(1)), 'confidence': 0.5, 'reasoning': text[:80]}
            else:
                return None

        grok_prob = result.get('probability', 0.5)
        grok_conf = result.get('confidence', 0.5)
        reasoning = result.get('reasoning', '')[:100]

        if mode == 'yes_candidate':
            # Buy YES if Grok thinks probability is higher than market
            edge = grok_prob - yes_price
            if edge < 0.08 or grok_conf < 0.6:
                logger.debug(f"MentionMarkets SKIP YES {ticker}: grok={grok_prob:.2f} market={yes_price:.2f} edge={edge:.2f}")
                return None
            side = 'yes'
            model_prob = grok_prob
        else:
            # Buy NO if Grok confirms the YES side is overpriced
            edge = (1 - grok_prob) - (1 - yes_price)
            if edge < 0.05 or grok_conf < 0.5:
                logger.debug(f"MentionMarkets SKIP NO {ticker}: grok={grok_prob:.2f} market={yes_price:.2f}")
                return None
            side = 'no'
            model_prob = 1 - grok_prob

        confidence = min(45 + edge * 150 + grok_conf * 25, 100)

        logger.info(
            f"MentionMarkets: {ticker} {side.upper()} grok={grok_prob:.2f} market={yes_price:.2f} "
            f"edge={edge:+.2f} - {reasoning}"
        )

        return {
            'ticker': ticker, 'title': title, 'action': 'buy', 'side': side,
            'count': 3, 'confidence': confidence, 'strategy_type': 'mention_markets',
            'edge': edge, 'model_prob': model_prob,
            'reason': f"MentionMarkets: {side.upper()} grok={grok_prob:.0%} vs market={yes_price:.0%}, edge={edge:+.0%} - {reasoning}",
        }

    def execute(self, signal, dry_run=False):
        if not self.can_execute(signal):
            return None
        self.log_signal(signal)
        return self.client.create_order(
            ticker=signal['ticker'], action='buy', side=signal['side'],
            count=signal['count'], order_type='market', dry_run=dry_run,
        )
