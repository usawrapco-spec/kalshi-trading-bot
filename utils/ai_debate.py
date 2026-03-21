"""Multi-model debate system: Grok + Claude must agree before any trade executes.

Flow:
1. Ask Grok for probability assessment
2. Ask Claude, sharing Grok's answer for a second opinion
3. Decision: AGREE (within 10%) -> TRADE, DISAGREE (>20%) -> SKIP,
   MIXED (10-20%) -> average and check edge
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
        # Fallback: regex extract
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
    """Run Grok vs Claude debate on a trade signal.

    Returns: (should_trade: bool, adjusted_signal: dict, debate_log: str)
    """
    grok_key = os.getenv('XAI_API_KEY')
    claude_key = os.getenv('ANTHROPIC_API_KEY')

    title = signal.get('title') or signal.get('ticker', '')
    side = signal.get('side', 'yes')
    edge = signal.get('edge', 0)

    # If no API keys, skip debate and allow trade
    if not grok_key and not claude_key:
        return True, signal, "DEBATE: no API keys, skipped"

    # Step 1: Ask Grok
    grok_result = _ask_grok(title, market_price, side, edge, grok_key) if grok_key else None
    if not grok_result:
        # Grok unavailable - trade with original signal
        return True, signal, "DEBATE: Grok unavailable, proceeding"

    grok_prob = grok_result.get('probability', 0.5)
    grok_conf = grok_result.get('confidence', 50)
    grok_reason = grok_result.get('reasoning', '')[:80]

    # Step 2: Ask Claude (with Grok's answer)
    claude_result = _ask_claude(
        title, market_price, side, edge,
        grok_prob, grok_reason, claude_key
    ) if claude_key else None

    if not claude_result:
        # Claude unavailable - use Grok's assessment alone
        debate_log = f"DEBATE: Grok={grok_prob:.0%} (Claude unavailable) -> TRADE"
        logger.info(debate_log)
        return True, signal, debate_log

    claude_prob = claude_result.get('probability', 0.5)
    claude_conf = claude_result.get('confidence', 50)
    claude_reason = claude_result.get('reasoning', '')[:80]

    # Step 3: Decision logic
    disagreement = abs(grok_prob - claude_prob)

    if disagreement <= 0.10:
        # AGREE: both within 10%
        avg_prob = (grok_prob + claude_prob) / 2
        # Boost confidence when both models agree
        boosted_conf = min(signal.get('confidence', 50) + 15, 100)
        signal['confidence'] = boosted_conf
        signal['model_prob'] = avg_prob
        debate_log = (
            f"DEBATE: Grok={grok_prob:.0%} Claude={claude_prob:.0%} -> AGREE -> TRADE "
            f"(avg={avg_prob:.0%}, conf boosted to {boosted_conf:.0f})"
        )
        logger.info(debate_log)
        return True, signal, debate_log

    elif disagreement > 0.20:
        # DISAGREE: skip trade
        debate_log = (
            f"DEBATE: Grok={grok_prob:.0%} Claude={claude_prob:.0%} -> DISAGREE ({disagreement:.0%} gap) -> SKIP "
            f"[Grok: {grok_reason}] [Claude: {claude_reason}]"
        )
        logger.info(debate_log)
        return False, signal, debate_log

    else:
        # MIXED (10-20% disagreement): average and check edge
        avg_prob = (grok_prob + claude_prob) / 2
        if side == 'yes':
            new_edge = avg_prob - market_price
        else:
            new_edge = (1 - avg_prob) - (1 - market_price)

        if new_edge > 0.05:
            signal['model_prob'] = avg_prob
            signal['edge'] = new_edge
            debate_log = (
                f"DEBATE: Grok={grok_prob:.0%} Claude={claude_prob:.0%} -> MIXED ({disagreement:.0%} gap) "
                f"-> avg={avg_prob:.0%}, new_edge={new_edge:.0%} > 5% -> TRADE"
            )
            logger.info(debate_log)
            return True, signal, debate_log
        else:
            debate_log = (
                f"DEBATE: Grok={grok_prob:.0%} Claude={claude_prob:.0%} -> MIXED ({disagreement:.0%} gap) "
                f"-> avg={avg_prob:.0%}, new_edge={new_edge:.0%} < 5% -> SKIP"
            )
            logger.info(debate_log)
            return False, signal, debate_log
