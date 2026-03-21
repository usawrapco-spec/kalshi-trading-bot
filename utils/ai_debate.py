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
from utils.api_resilience import APIResilience
from utils.gemini_client import ask_gemini

logger = setup_logger('ai_debate')

GROK_URL = 'https://api.x.ai/v1/chat/completions'
GROK_MODEL = 'grok-4-1-fast-non-reasoning'
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

    def api_call(timeout):
        resp = requests.post(GROK_URL, headers={
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
        }, json={
            'model': GROK_MODEL,
            'messages': [{'role': 'user', 'content': prompt}],
            'temperature': 0.1,
            'max_tokens': 200,
        }, timeout=timeout)
        resp.raise_for_status()
        text = resp.json()['choices'][0]['message']['content'].strip()
        return _parse_json_response(text)

    return APIResilience.grok_call(api_call)


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

    def api_call(timeout):
        resp = requests.post(CLAUDE_URL, headers={
            'x-api-key': api_key,
            'anthropic-version': '2023-06-01',
            'content-type': 'application/json',
        }, json={
            'model': CLAUDE_MODEL,
            'max_tokens': 200,
            'temperature': 0.2,
            'messages': [{'role': 'user', 'content': prompt}],
        }, timeout=timeout)
        resp.raise_for_status()
        text = resp.json()['content'][0]['text'].strip()
        return _parse_json_response(text)

    return APIResilience.claude_call(api_call)


def _ask_gemini_vote(title, price, side, edge):
    """Ask Gemini for third opinion vote."""
    gemini_key = os.getenv('GEMINI_API_KEY')
    if not gemini_key:
        return None

    prompt = (
        f"Market: \"{title}\". Current YES price: ${price:.2f} (implying {price*100:.0f}% probability). "
        f"Strategy says BUY {side.upper()} with {edge*100:.0f}% edge. "
        f"What is the true probability this resolves YES? "
        f"Reply with ONLY a JSON object: "
        f'{{"probability": 0.XX, "confidence": 0-100, "reasoning": "one sentence"}}'
    )
    text = ask_gemini(prompt, max_tokens=200)
    if text:
        return _parse_json_response(text)
    return None


def run_debate(signal, market_price):
    """Three-brain debate: Grok + Claude + Gemini (optional).

    Voting: 3/3 = full size, 2/3 = 75%, 1/3 = 25%, 0/3 = skip.
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

    # Count votes: does each AI see positive edge?
    votes = 0
    voters = []

    if grok_edge > 0.03:
        votes += 1
        voters.append(f"Grok={grok_prob:.0%} YES")
    else:
        voters.append(f"Grok={grok_prob:.0%} NO")

    # Update signal with Grok's probability
    signal['model_prob'] = grok_prob if side == 'yes' else (1 - grok_prob)
    signal['edge'] = grok_edge
    logger.info(f"GROK VOTE: {grok_prob:.0%} prob, edge={grok_edge:+.0%}. Reason: {grok_reason}")

    # Step 2: Ask Claude for second opinion (if available)
    if claude_key:
        claude_result = _ask_claude(
            title, market_price, side, edge,
            grok_prob, grok_reason, claude_key
        )
        if claude_result:
            claude_prob = claude_result.get('probability', 0.5)
            claude_reason = claude_result.get('reasoning', '')[:80]
            if side == 'yes':
                claude_edge = claude_prob - market_price
            else:
                claude_edge = (1 - claude_prob) - (1 - market_price)

            if claude_edge > 0.03:
                votes += 1
                voters.append(f"Claude={claude_prob:.0%} YES")
            else:
                voters.append(f"Claude={claude_prob:.0%} NO")
            logger.info(f"CLAUDE VOTE: {claude_prob:.0%} prob, edge={claude_edge:+.0%}. Reason: {claude_reason}")

    # Step 3: Ask Gemini for third opinion (if GEMINI_API_KEY set)
    gemini_result = _ask_gemini_vote(title, market_price, side, edge)
    if gemini_result:
        gemini_prob = gemini_result.get('probability', 0.5)
        gemini_reason = gemini_result.get('reasoning', '')[:80]
        if side == 'yes':
            gemini_edge = gemini_prob - market_price
        else:
            gemini_edge = (1 - gemini_prob) - (1 - market_price)

        if gemini_edge > 0.03:
            votes += 1
            voters.append(f"Gemini={gemini_prob:.0%} YES")
        else:
            voters.append(f"Gemini={gemini_prob:.0%} NO")
        logger.info(f"GEMINI VOTE: {gemini_prob:.0%} prob, edge={gemini_edge:+.0%}. Reason: {gemini_reason}")

    # Voting decision: 3/3=full, 2/3=75%, 1/3=25%, 0/3=skip
    voter_str = ', '.join(voters)
    if votes == 0:
        debate_log = f"DEBATE {votes}/3: {voter_str} -> NO TRADE (unanimous reject)"
        logger.info(debate_log)
        return False, signal, debate_log
    elif votes == 1:
        original_count = signal.get('count', 1)
        signal['count'] = max(1, int(original_count * 0.25))
        debate_log = f"DEBATE {votes}/3: {voter_str} -> 25% SIZE (count={signal['count']})"
        logger.info(debate_log)
        return True, signal, debate_log
    elif votes == 2:
        original_count = signal.get('count', 1)
        signal['count'] = max(1, int(original_count * 0.75))
        debate_log = f"DEBATE {votes}/3: {voter_str} -> 75% SIZE (count={signal['count']})"
        logger.info(debate_log)
        return True, signal, debate_log
    else:  # votes == 3
        signal['confidence'] = min(signal.get('confidence', 50) + 15, 100)
        debate_log = f"DEBATE {votes}/3: {voter_str} -> FULL SIZE (unanimous)"
        logger.info(debate_log)
        return True, signal, debate_log
