"""Multi-model debate: Grok is primary decision maker, Claude is second opinion.

Grok has 75% accuracy on prediction markets (highest of any AI) so it has
VETO power. Claude provides a second opinion that affects position sizing.

Flow:
1. Ask Grok for probability + trade/no-trade decision
2. If Grok says NO TRADE: always skip (Grok has veto)
3. If Grok says TRADE: ask Claude for second opinion
4. If Claude agrees: FULL SIZE trade
5. If Claude disagrees: HALF SIZE trade (Grok overrides but we're cautious)
"""

import os
import re
import json
import requests
from utils.logger import setup_logger

logger = setup_logger('ai_debate')

GROK_URL = 'https://api.x.ai/v1/chat/completions'
GROK_MODEL = 'grok-4.1-fast'
CLAUDE_URL = 'https://api.anthropic.com/v1/messages'
CLAUDE_MODEL = 'claude-sonnet-4-20250514'


def _parse_json_response(text):
    """Extract JSON from an AI response, handling markdown fences."""
    clean = text.strip()
    if '```' in clean:
        clean = re.sub(r'```\w*\n?', '', clean).strip()
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        prob = re.search(r'"probability"\s*:\s*([\d.]+)', text)
        conf = re.search(r'"confidence"\s*:\s*([\d.]+)', text)
        reason = re.search(r'"reasoning"\s*:\s*"([^"]*)"', text)
        if prob:
            return {
                'probability': float(prob.group(1)),
                'confidence': float(conf.group(1)) if conf else 50,
                'reasoning': reason.group(1) if reason else '',
            }
        return None


def _ask_grok(title, price, side, edge, api_key):
    """Ask Grok for probability assessment."""
    prompt = (
        f"Market: \"{title}\". Current YES price: ${price:.2f} (implying {price*100:.0f}% probability). "
        f"Strategy says BUY {side.upper()} with {edge*100:.0f}% edge. "
        f"What is the true probability this resolves YES? "
        f"Reply with ONLY a JSON object: "
        f'{{\"probability\": 0.XX, \"confidence\": 0-100, \"reasoning\": \"one sentence\"}}'
    )
    try:
        resp = requests.post(GROK_URL, headers={
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
        }, json={
            'model': GROK_MODEL,
            'messages': [{'role': 'user', 'content': prompt}],
            'temperature': 0.1,
            'max_tokens': 200,
        }, timeout=30)
        resp.raise_for_status()
        text = resp.json()['choices'][0]['message']['content'].strip()
        return _parse_json_response(text)
    except Exception as e:
        logger.debug(f"Grok debate error: {e}")
        return None


def _ask_claude(title, price, side, edge, grok_prob, grok_reasoning, api_key):
    """Ask Claude for second opinion, sharing Grok's assessment."""
    prompt = (
        f"Market: \"{title}\". Current YES price: ${price:.2f} (implying {price*100:.0f}% probability). "
        f"Strategy says BUY {side.upper()} with {edge*100:.0f}% edge. "
        f"Grok estimates probability at {grok_prob:.0%} with reasoning: \"{grok_reasoning}\". "
        f"Do you agree or disagree? "
        f"Reply with ONLY a JSON object: "
        f'{{\"probability\": 0.XX, \"confidence\": 0-100, \"reasoning\": \"one sentence\"}}'
    )
    try:
        resp = requests.post(CLAUDE_URL, headers={
            'x-api-key': api_key,
            'anthropic-version': '2023-06-01',
            'content-type': 'application/json',
        }, json={
            'model': CLAUDE_MODEL,
            'max_tokens': 200,
            'temperature': 0.2,
            'messages': [{'role': 'user', 'content': prompt}],
        }, timeout=30)
        resp.raise_for_status()
        text = resp.json()['content'][0]['text'].strip()
        return _parse_json_response(text)
    except Exception as e:
        logger.debug(f"Claude debate error: {e}")
        return None


def run_debate(signal, market_price):
    """Grok-primary debate. Grok decides, Claude adjusts sizing.

    Returns: (should_trade: bool, adjusted_signal: dict, debate_log: str)
    """
    grok_key = os.getenv('XAI_API_KEY')
    claude_key = os.getenv('ANTHROPIC_API_KEY')

    title = signal.get('title') or signal.get('ticker', '')
    side = signal.get('side', 'yes')
    edge = signal.get('edge', 0)

    if not grok_key:
        return True, signal, "DEBATE: no Grok API key, skipped"

    # Step 1: Grok decides (primary decision maker, 75% accuracy)
    grok_result = _ask_grok(title, market_price, side, edge, grok_key)
    if not grok_result:
        return True, signal, "DEBATE: Grok unavailable, proceeding without debate"

    grok_prob = grok_result.get('probability', 0.5)
    grok_conf = grok_result.get('confidence', 50)
    grok_reason = grok_result.get('reasoning', '')[:80]

    # Grok's trade decision: does the edge hold?
    if side == 'yes':
        grok_edge = grok_prob - market_price
    else:
        grok_edge = (1 - grok_prob) - (1 - market_price)

    # GROK VETO: if Grok sees no edge, always skip
    if grok_edge < 0.03:
        debate_log = (
            f"GROK DECIDES: {grok_prob:.0%} prob, edge={grok_edge:+.0%} -> NO TRADE (veto). "
            f"Reason: {grok_reason}"
        )
        logger.info(debate_log)
        return False, signal, debate_log

    # Grok says TRADE - update signal with Grok's probability
    signal['model_prob'] = grok_prob if side == 'yes' else (1 - grok_prob)
    signal['edge'] = grok_edge
    logger.info(f"GROK DECIDES: {grok_prob:.0%} prob, edge={grok_edge:+.0%} -> TRADE. Reason: {grok_reason}")

    # Step 2: Ask Claude for second opinion (if available)
    if not claude_key:
        debate_log = (
            f"GROK DECIDES: {grok_prob:.0%} prob -> TRADE. Claude unavailable -> FULL SIZE"
        )
        logger.info(debate_log)
        return True, signal, debate_log

    claude_result = _ask_claude(
        title, market_price, side, edge,
        grok_prob, grok_reason, claude_key
    )

    if not claude_result:
        debate_log = (
            f"GROK DECIDES: {grok_prob:.0%} prob -> TRADE. Claude error -> FULL SIZE"
        )
        logger.info(debate_log)
        return True, signal, debate_log

    claude_prob = claude_result.get('probability', 0.5)
    claude_reason = claude_result.get('reasoning', '')[:80]

    # Does Claude agree with the trade direction?
    if side == 'yes':
        claude_edge = claude_prob - market_price
    else:
        claude_edge = (1 - claude_prob) - (1 - market_price)

    claude_agrees = claude_edge > 0.03  # Claude also sees positive edge

    if claude_agrees:
        # Both agree: FULL SIZE, boost confidence
        signal['confidence'] = min(signal.get('confidence', 50) + 15, 100)
        debate_log = (
            f"GROK DECIDES: {grok_prob:.0%} prob -> TRADE. "
            f"Claude confirms: {claude_prob:.0%} -> FULL SIZE"
        )
        logger.info(debate_log)
        return True, signal, debate_log
    else:
        # Grok says trade, Claude disagrees: HALF SIZE
        signal['count'] = max(1, signal.get('count', 1) // 2)
        debate_log = (
            f"GROK DECIDES: {grok_prob:.0%} prob -> TRADE. "
            f"Claude disagrees: {claude_prob:.0%} -> HALF SIZE (count={signal['count']}). "
            f"Claude: {claude_reason}"
        )
        logger.info(debate_log)
        return True, signal, debate_log
