"""GrokNewsAnalysis strategy - uses xAI Grok-3 to evaluate market mispricings.

Inspired by ajwann/kalshi-genai-trading-bot and ryanfrigo/kalshi-ai-trading-bot.
Sends top 20 markets by volume to Grok for probability assessment.
"""

import os
import re
import json
import requests
from strategies.base import BaseStrategy
from utils.logger import setup_logger

logger = setup_logger('grok_news')

XAI_URL = 'https://api.x.ai/v1/chat/completions'
MODEL = 'grok-3'
MAX_PER_CYCLE = 20
MIN_EDGE = 0.10
MIN_CONFIDENCE = 0.70


def get_yes_price(m):
    for f in ('yes_bid', 'yes_bid_dollars', 'yes_ask', 'yes_ask_dollars', 'last_price', 'last_price_dollars'):
        v = m.get(f)
        if v is not None and float(v) > 0:
            v = float(v)
            return v / 100.0 if v > 1 else v
    return 0.0


class GrokNewsStrategy(BaseStrategy):
    """Top 20 markets by volume -> Grok-3 for probability -> trade on >10% edge."""

    def __init__(self, client, risk_manager, db):
        super().__init__(client, risk_manager, db)
        self.api_key = os.getenv('XAI_API_KEY')
        if not self.api_key:
            logger.warning("XAI_API_KEY not set - GrokNews disabled")
        else:
            logger.info(f"GrokNews initialized (model={MODEL}, max {MAX_PER_CYCLE}/cycle, edge>{MIN_EDGE:.0%})")

    def analyze(self, markets):
        if not self.api_key:
            return []

        # No pre-filtering. Sort by 24h volume, then total volume, take top 20.
        open_mkts = [m for m in markets if m.get('status') == 'open']
        open_mkts.sort(key=lambda m: (m.get('volume_24h') or m.get('volume_24h_fp') or m.get('volume') or 0), reverse=True)
        candidates = open_mkts[:MAX_PER_CYCLE]

        logger.info(
            f"GrokNews: {len(open_mkts)} open markets, sending top {len(candidates)} to Grok. "
            f"Top: {candidates[0].get('ticker', '?')} vol={candidates[0].get('volume', 0) if candidates else 0}"
        )

        signals = []
        for m in candidates:
            sig = self._ask_grok(m)
            if sig:
                signals.append(sig)

        logger.info(f"GrokNews: {len(signals)} signals from {len(candidates)} candidates")
        return signals

    def _ask_grok(self, m):
        ticker = m.get('ticker', '')
        title = m.get('title', 'Unknown')
        subtitle = m.get('subtitle', '')
        yes_price = get_yes_price(m)
        volume = m.get('volume_24h') or m.get('volume_24h_fp') or m.get('volume') or 0
        close_time = m.get('close_time') or m.get('expiration_time') or ''

        desc = title
        if subtitle:
            desc += f' ({subtitle})'

        prompt = (
            f"You are an expert prediction market trader. "
            f"Market: \"{desc}\". Current YES price: ${yes_price:.2f} (implying {yes_price*100:.0f}% probability). "
            f"Volume: {volume}. Closes: {close_time}. "
            f"Based on your knowledge of current events and X/Twitter sentiment, "
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
                'max_tokens': 250,
            }, timeout=30)
            resp.raise_for_status()
            text = resp.json()['choices'][0]['message']['content'].strip()
        except Exception as e:
            logger.debug(f"GrokNews API error for {ticker}: {e}")
            return None

        # Parse JSON from response
        try:
            # Strip markdown fences
            clean = text
            if '```' in clean:
                clean = re.sub(r'```\w*\n?', '', clean).strip()
            result = json.loads(clean)
        except json.JSONDecodeError:
            # Fallback: extract number
            prob_match = re.search(r'"probability"\s*:\s*([\d.]+)', text)
            conf_match = re.search(r'"confidence"\s*:\s*([\d.]+)', text)
            if prob_match:
                result = {
                    'probability': float(prob_match.group(1)),
                    'confidence': float(conf_match.group(1)) if conf_match else 0.5,
                    'reasoning': text[:100],
                }
            else:
                logger.debug(f"GrokNews parse failed for {ticker}: {text[:80]}")
                return None

        grok_prob = result.get('probability', 0.5)
        grok_conf = result.get('confidence', 0.5)
        reasoning = result.get('reasoning', '')[:120]

        if yes_price <= 0:
            return None

        edge = abs(grok_prob - yes_price)

        if edge < MIN_EDGE:
            logger.debug(f"GrokNews SKIP {ticker}: grok={grok_prob:.2f} market={yes_price:.2f} edge={edge:.2f} < {MIN_EDGE}")
            return None
        if grok_conf < MIN_CONFIDENCE:
            logger.debug(f"GrokNews SKIP {ticker}: confidence={grok_conf:.2f} < {MIN_CONFIDENCE}")
            return None

        if grok_prob > yes_price:
            side = 'yes'
            model_prob = grok_prob
            mkt_price = yes_price
        else:
            side = 'no'
            model_prob = 1 - grok_prob
            mkt_price = 1 - yes_price

        actual_edge = model_prob - mkt_price
        confidence = min(50 + actual_edge * 200 + grok_conf * 30, 100)

        logger.info(
            f"GrokNews: {ticker} grok_prob={grok_prob:.2f} market={yes_price:.2f} "
            f"edge={actual_edge:+.2f} conf={grok_conf:.2f} -> PAPER BUY {side.upper()} - {reasoning}"
        )

        return {
            'ticker': ticker, 'title': title, 'action': 'buy', 'side': side,
            'count': 5, 'confidence': confidence, 'strategy_type': 'grok_news',
            'edge': actual_edge, 'model_prob': model_prob,
            'reason': f"GrokNews: {side.upper()} grok={grok_prob:.0%} vs market={yes_price:.0%}, edge={actual_edge:+.0%}, conf={grok_conf:.0%} - {reasoning}",
        }

    def execute(self, signal, dry_run=False):
        if not self.can_execute(signal):
            return None
        self.log_signal(signal)
        return self.client.create_order(
            ticker=signal['ticker'], action='buy', side=signal['side'],
            count=signal['count'], order_type='market', dry_run=dry_run,
        )
