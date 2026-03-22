"""HyperThink: 3-agent consensus system for high-stakes trading decisions.

Upgrades the existing ai_debate system into a reusable consensus engine.
Agents: DATA model (GFS ensemble, arb calc, etc.) + Grok + Claude.
Confidence is based on agreement spread across agents.

Rate-limited to 5 debates per cycle to avoid API costs.
"""

import os
import re
import json
import requests
from datetime import datetime
from utils.logger import setup_logger
from utils.api_resilience import APIResilience

logger = setup_logger('hyperthink')

GROK_URL = 'https://api.x.ai/v1/chat/completions'
GROK_MODEL = 'grok-4-1-fast-non-reasoning'
CLAUDE_URL = 'https://api.anthropic.com/v1/messages'
CLAUDE_MODEL = 'claude-sonnet-4-20250514'

MAX_DEBATES_PER_CYCLE = 5


def _parse_json_response(text):
    """Extract JSON from AI response, handling markdown fences."""
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


def _extract_prob(result):
    """Normalize probability to 0-1 range from parsed AI response."""
    if not result:
        return None
    p = result.get('probability', 0)
    if p > 1.0:
        p = p / 100.0
    return max(0.0, min(1.0, p))


class HyperThink:
    """3-agent consensus system for high-stakes decisions."""

    def __init__(self, db=None):
        self.db = db
        self.debates_this_cycle = 0
        self._grok_key = os.getenv('XAI_API_KEY', '')
        self._claude_key = os.getenv('ANTHROPIC_API_KEY', '')

    def reset_cycle(self):
        self.debates_this_cycle = 0

    def evaluate(self, market, side, market_price, data_prob=None, context=""):
        """Run multi-agent evaluation on a trading decision.

        Args:
            market: market dict with ticker, title
            side: 'yes' or 'no'
            market_price: current YES price (0-1)
            data_prob: probability from data model (optional anchor)
            context: extra context string for the AI prompts

        Returns:
            (avg_prob, confidence_label, size_multiplier)
            confidence_label: UNANIMOUS/STRONG/MODERATE/SPLIT/DATA_ONLY
            size_multiplier: 0.0-1.0 (0 = skip, 1 = full size)
        """
        if self.debates_this_cycle >= MAX_DEBATES_PER_CYCLE:
            logger.info("HyperThink limit reached, using data probability only")
            if data_prob is not None and data_prob > 0.60:
                return data_prob, "DATA_ONLY", 0.5
            return data_prob or 0.5, "DATA_ONLY", 0.0

        self.debates_this_cycle += 1
        ticker = market.get('ticker', '?')
        title = market.get('title', ticker)

        agents = {}

        if data_prob is not None:
            agents['DATA'] = data_prob

        # Grok (primary — has X/Twitter real-time data)
        grok_reasoning = ""
        if self._grok_key:
            try:
                grok_result = self._ask_grok(title, market_price, side, data_prob, context)
                grok_p = _extract_prob(grok_result)
                if grok_p is not None:
                    agents['GROK'] = grok_p
                    grok_reasoning = (grok_result or {}).get('reasoning', '')[:100]
            except Exception as e:
                logger.debug(f"HyperThink Grok failed: {e}")

        # Claude (second opinion — pattern recognition)
        if self._claude_key:
            try:
                claude_result = self._ask_claude(
                    title, market_price, side, data_prob,
                    agents.get('GROK'), grok_reasoning, context
                )
                claude_p = _extract_prob(claude_result)
                if claude_p is not None:
                    agents['CLAUDE'] = claude_p
            except Exception as e:
                logger.debug(f"HyperThink Claude failed: {e}")

        if not agents:
            return 0.5, "NO_DATA", 0.0

        probs = list(agents.values())
        avg = sum(probs) / len(probs)
        spread = max(probs) - min(probs)

        if spread < 0.10:
            confidence = "UNANIMOUS"
            multiplier = 1.0
        elif spread < 0.20:
            confidence = "STRONG"
            multiplier = 0.75
        elif spread < 0.30:
            confidence = "MODERATE"
            multiplier = 0.50
        else:
            confidence = "SPLIT"
            multiplier = 0.0

        agent_str = " | ".join(f"{k}={v:.0%}" for k, v in agents.items())
        logger.info(
            f"HYPERTHINK: [{confidence}] {ticker} {agent_str} "
            f"-> avg={avg:.0%} spread={spread:.0%} mult={multiplier}"
        )

        # Log to debate_log table
        self._log_debate(ticker, title, agents, avg, spread, confidence, multiplier)

        return avg, confidence, multiplier

    def validate_arb(self, ticker, title, yes_ask, no_ask, total, gap_pct, volume, contracts):
        """Validate whether a probability arb opportunity is real or a trap.

        Returns True if agents agree it's a real arb.
        """
        if self.debates_this_cycle >= MAX_DEBATES_PER_CYCLE:
            logger.info("HyperThink limit reached, skipping arb validation")
            return True  # Default to trusting the numbers

        self.debates_this_cycle += 1

        prompt = (
            f'Potential arbitrage on Kalshi:\n'
            f'Market: "{title}"\n'
            f'YES ask: ${yes_ask:.2f}, NO ask: ${no_ask:.2f}\n'
            f'Total: ${total:.2f} (gap: {gap_pct:.1f}%)\n'
            f'24h volume: {volume:.0f}, Available contracts: {contracts}\n\n'
            f'Is this a real arbitrage or a trap? Consider:\n'
            f'1. Could the prices be stale/unfillable?\n'
            f'2. Is there a reason both sides are cheap?\n'
            f'3. Is the volume high enough to actually fill both sides?\n'
            f'4. Any settlement risk (market could be voided)?\n\n'
            f'Reply with ONLY a JSON object: '
            f'{{"verdict": "REAL" or "TRAP", "reasoning": "one sentence"}}'
        )

        votes_real = 0
        voters = []

        if self._grok_key:
            try:
                grok_text = self._raw_ask_grok(prompt)
                if grok_text and 'REAL' in grok_text.upper():
                    votes_real += 1
                    voters.append("Grok=REAL")
                else:
                    voters.append("Grok=TRAP")
            except Exception:
                voters.append("Grok=ERR")

        if self._claude_key:
            try:
                claude_text = self._raw_ask_claude(prompt)
                if claude_text and 'REAL' in claude_text.upper():
                    votes_real += 1
                    voters.append("Claude=REAL")
                else:
                    voters.append("Claude=TRAP")
            except Exception:
                voters.append("Claude=ERR")

        is_real = votes_real >= 1  # At least one says REAL
        voter_str = ", ".join(voters)
        logger.info(f"HYPERTHINK ARB: {ticker} {voter_str} -> {'TRADE' if is_real else 'SKIP'}")

        self._log_debate(
            ticker, title,
            {'ARB_GAP': gap_pct / 100},
            gap_pct / 100, 0, "ARB_REAL" if is_real else "ARB_TRAP",
            1.0 if is_real else 0.0
        )

        return is_real

    def _ask_grok(self, title, market_price, side, data_prob, context):
        data_str = f"Our data model estimates {data_prob:.0%} probability. " if data_prob else ""
        ctx_str = f"\nAdditional context: {context}" if context else ""
        prompt = (
            f'Market: "{title}". Current YES price: ${market_price:.2f} '
            f'(implying {market_price*100:.0f}% probability). '
            f'{data_str}'
            f'What is the true probability this resolves YES?{ctx_str} '
            f'Reply with ONLY a JSON object: '
            f'{{"probability": 0.XX, "confidence": 0-100, "reasoning": "one sentence"}}'
        )

        def api_call(timeout):
            resp = requests.post(GROK_URL, headers={
                'Authorization': f'Bearer {self._grok_key}',
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

    def _ask_claude(self, title, market_price, side, data_prob, grok_prob, grok_reasoning, context):
        data_str = f"Data model: {data_prob:.0%}. " if data_prob else ""
        grok_str = f"Grok estimates {grok_prob:.0%}: \"{grok_reasoning}\". " if grok_prob else ""
        ctx_str = f"\nAdditional context: {context}" if context else ""
        prompt = (
            f'Market: "{title}". Current YES price: ${market_price:.2f} '
            f'(implying {market_price*100:.0f}% probability). '
            f'{data_str}{grok_str}'
            f'Do you agree? What is the true probability?{ctx_str} '
            f'Reply with ONLY a JSON object: '
            f'{{"probability": 0.XX, "confidence": 0-100, "reasoning": "one sentence"}}'
        )

        def api_call(timeout):
            resp = requests.post(CLAUDE_URL, headers={
                'x-api-key': self._claude_key,
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

    def _raw_ask_grok(self, prompt):
        def api_call(timeout):
            resp = requests.post(GROK_URL, headers={
                'Authorization': f'Bearer {self._grok_key}',
                'Content-Type': 'application/json',
            }, json={
                'model': GROK_MODEL,
                'messages': [{'role': 'user', 'content': prompt}],
                'temperature': 0.1,
                'max_tokens': 200,
            }, timeout=timeout)
            resp.raise_for_status()
            return resp.json()['choices'][0]['message']['content'].strip()
        return APIResilience.grok_call(api_call)

    def _raw_ask_claude(self, prompt):
        def api_call(timeout):
            resp = requests.post(CLAUDE_URL, headers={
                'x-api-key': self._claude_key,
                'anthropic-version': '2023-06-01',
                'content-type': 'application/json',
            }, json={
                'model': CLAUDE_MODEL,
                'max_tokens': 200,
                'temperature': 0.2,
                'messages': [{'role': 'user', 'content': prompt}],
            }, timeout=timeout)
            resp.raise_for_status()
            return resp.json()['content'][0]['text'].strip()
        return APIResilience.claude_call(api_call)

    def _log_debate(self, ticker, title, agents, avg, spread, confidence, multiplier):
        if not self.db:
            return
        try:
            agent_str = " | ".join(f"{k}={v:.0%}" for k, v in agents.items())
            self.db.client.table('debate_log').insert({
                'timestamp': datetime.utcnow().isoformat(),
                'ticker': ticker,
                'market_title': title[:200],
                'grok_probability': agents.get('GROK'),
                'claude_probability': agents.get('CLAUDE'),
                'agreement': spread < 0.15,
                'final_decision': f"{confidence} avg={avg:.0%}",
                'size_modifier': multiplier,
                'votes': agent_str,
            }).execute()
        except Exception as e:
            logger.debug(f"Failed to log debate: {e}")
