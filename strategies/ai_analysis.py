"""GrokNewsAnalysis strategy - uses xAI Grok to evaluate if Kalshi markets are mispriced."""

import os
import json
import requests
from strategies.base import BaseStrategy
from utils.logger import setup_logger

logger = setup_logger('grok_analysis')

XAI_API_URL = 'https://api.x.ai/v1/chat/completions'
GROK_MODEL = 'grok-3'
MAX_MARKETS_PER_CYCLE = 20  # Rate limit: max 20 API calls per cycle
MIN_EDGE = 0.10  # Only trade when Grok's probability differs by >10%


class GrokNewsAnalysisStrategy(BaseStrategy):
    """
    For the top 20 highest-volume markets each cycle, calls Grok to evaluate
    whether the current price is mispriced based on current events and world
    knowledge. Uses OpenAI-compatible API format at api.x.ai.
    Trades when Grok's probability differs from market price by >10%.
    """

    def __init__(self, client, risk_manager, db):
        super().__init__(client, risk_manager, db)
        self.api_key = os.getenv('XAI_API_KEY')
        if not self.api_key:
            logger.warning("XAI_API_KEY not set - Grok analysis disabled")
        else:
            logger.info("GrokNewsAnalysis strategy initialized (grok-3, max 20 calls/cycle)")

    def analyze(self, markets):
        if not self.api_key:
            return []

        signals = []
        candidates = self._select_candidates(markets)
        logger.info(f"Grok analyzing {len(candidates)} candidate markets")

        for market in candidates:
            signal = self._analyze_with_grok(market)
            if signal:
                signals.append(signal)

        return signals

    def _select_candidates(self, markets):
        """Pick top 20 highest-volume markets. No price filtering - send everything to Grok."""
        open_markets = [m for m in markets if m.get('status') == 'open']
        # Log how many passed the filter
        with_volume = [m for m in open_markets if (m.get('volume') or 0) > 0]
        logger.info(
            f"Grok candidate pool: {len(open_markets)} open markets, "
            f"{len(with_volume)} with volume > 0"
        )
        # Sort by volume, but don't exclude zero-volume markets if we don't have enough
        open_markets.sort(key=lambda m: m.get('volume') or 0, reverse=True)
        selected = open_markets[:MAX_MARKETS_PER_CYCLE]
        if selected:
            logger.info(
                f"Grok selected {len(selected)} markets, "
                f"top volume={selected[0].get('volume', 0)}, "
                f"top ticker={selected[0].get('ticker', '?')}"
            )
        return selected

    def _analyze_with_grok(self, market):
        """Call Grok API to analyze a single market."""
        ticker = market.get('ticker', '')
        title = market.get('title', 'Unknown')
        subtitle = market.get('subtitle', '')
        # Try multiple price fields - Kalshi API may use different names
        yes_bid = market.get('yes_bid') or market.get('yes_ask') or market.get('last_price') or 0
        volume = market.get('volume') or 0
        close_time = market.get('close_time', '')

        market_desc = title
        if subtitle:
            market_desc += f' ({subtitle})'

        prompt = (
            f"You are a prediction market analyst. Given this market: \"{market_desc}\", "
            f"current YES price: {yes_bid} cents (out of 100). "
            f"Volume: {volume} contracts. Closes: {close_time}. "
            f"Based on your knowledge and current events, what is the true probability "
            f"this resolves YES? Reply with just a number 0-100 and one sentence explanation."
        )

        try:
            resp = requests.post(
                XAI_API_URL,
                headers={
                    'Authorization': f'Bearer {self.api_key}',
                    'Content-Type': 'application/json',
                },
                json={
                    'model': GROK_MODEL,
                    'messages': [{'role': 'user', 'content': prompt}],
                    'temperature': 0.1,
                    'max_tokens': 200,
                },
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            text = data['choices'][0]['message']['content'].strip()

        except Exception as e:
            logger.debug(f"Grok API failed for {ticker}: {e}")
            return None

        # Parse the probability number from Grok's response
        grok_prob = self._parse_probability(text)
        if grok_prob is None:
            logger.debug(f"Could not parse probability from Grok response for {ticker}: {text[:100]}")
            return None

        # Extract explanation (everything after the number)
        explanation = text
        import re
        num_match = re.search(r'\d+', text)
        if num_match:
            explanation = text[num_match.end():].strip().lstrip('.,:;-').strip()

        # Calculate edge on both sides
        market_yes = yes_bid / 100.0
        model_yes = grok_prob / 100.0

        yes_edge = model_yes - market_yes
        no_edge = (1 - model_yes) - (1 - market_yes)  # same magnitude, opposite sign

        if abs(yes_edge) < MIN_EDGE:
            logger.debug(f"Grok skip {ticker}: prob={grok_prob} vs market={yes_bid}c edge={yes_edge:.1%} < {MIN_EDGE:.0%}")
            return None

        # Determine side
        if yes_edge > MIN_EDGE:
            side = 'yes'
            edge = yes_edge
            model_prob = model_yes
            market_price = yes_bid
        elif yes_edge < -MIN_EDGE:
            side = 'no'
            edge = -yes_edge
            model_prob = 1 - model_yes
            market_price = 100 - yes_bid
        else:
            return None

        # Confidence based on edge size and Grok's implied certainty
        confidence = min(50 + edge * 200 + abs(grok_prob - 50) * 0.3, 100)

        logger.info(
            f"Grok signal: {ticker} {side.upper()} grok_prob={grok_prob}% "
            f"vs market={yes_bid}c edge={edge:.1%} - {explanation[:80]}"
        )

        return {
            'ticker': ticker,
            'action': 'buy',
            'side': side,
            'count': 5,
            'reason': (
                f'Grok: {side.upper()} prob={model_prob:.0%} vs market={market_price}c, '
                f'edge={edge:.0%} - {explanation[:100]}'
            ),
            'confidence': confidence,
            'strategy_type': 'grok_analysis',
            'edge': edge,
            'model_prob': model_prob,
        }

    def _parse_probability(self, text):
        """Extract a probability number (0-100) from Grok's response."""
        import re
        # Try to find a standalone number at the start or after common patterns
        patterns = [
            r'^(\d{1,3})(?:\s|%|\.)',  # Number at start
            r'probability[:\s]+(\d{1,3})',
            r'(\d{1,3})\s*%',
            r'^(\d{1,3})$',
        ]
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                val = int(match.group(1))
                if 0 <= val <= 100:
                    return val
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
