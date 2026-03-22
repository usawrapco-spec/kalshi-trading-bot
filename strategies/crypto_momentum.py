"""CryptoMomentum strategy - 15-minute crypto scalping on Kalshi.

Monitors real-time BTC/ETH/SOL prices, detects short-term momentum,
and trades Kalshi 15-min crypto contracts when momentum disagrees
with the market price. Always validates via HyperThink.

PAPER MODE ONLY until win rate > 55% over 288+ settlements.
"""

import re
from datetime import datetime
from strategies.base import BaseStrategy
from utils.logger import setup_logger
from utils.market_helpers import get_yes_price, get_no_price, safe_float, get_volume
from utils.api_resilience import resilient_strategy

logger = setup_logger('crypto_momentum')

CRYPTO_KEYWORDS = ['bitcoin', 'btc', 'ethereum', 'eth', 'solana', 'sol', 'crypto', 'xrp']
SHORT_TERM_KEYWORDS = ['higher', 'lower', '15 min', '15-min', 'next', 'close above', 'close below']

MIN_MOMENTUM = 0.003     # 0.3% minimum move to generate signal
MIN_EDGE = 0.08          # 8% edge over market price
MIN_VOLUME = 100         # Skip illiquid markets
MAX_SIGNALS = 3          # Max signals per cycle
MAX_CRYPTO_DEBATES = 3   # HyperThink limit for crypto per cycle


class CryptoMomentumStrategy(BaseStrategy):
    """15-minute crypto scalping using momentum + HyperThink consensus."""

    def __init__(self, client, risk_manager, db, crypto_monitor=None, hyperthink=None):
        super().__init__(client, risk_manager, db)
        self.monitor = crypto_monitor
        self.hyperthink = hyperthink
        self._crypto_debates_this_cycle = 0
        logger.info(f"CryptoMomentum initialized (monitor={'ON' if crypto_monitor else 'OFF'}, HyperThink={'ON' if hyperthink else 'OFF'})")

    @resilient_strategy
    def analyze(self, markets):
        signals = []

        if not self.monitor:
            return signals

        # Always fetch prices to build history (throttled internally)
        prices = self.monitor.fetch_prices()
        if not prices:
            return signals

        # Reset per-cycle debate counter
        self._crypto_debates_this_cycle = 0

        # Find crypto markets from the full market list
        crypto_markets = self._find_crypto_markets(markets)
        if not crypto_markets:
            return signals

        logger.info(f"CryptoMomentum: {len(crypto_markets)} crypto markets, BTC=${prices.get('BTC', 0):,.0f}")

        for cm in crypto_markets[:10]:
            coin = cm['coin']
            if not coin or coin not in prices:
                continue

            current_price = prices[coin]
            m5 = self.monitor.get_momentum(coin, 5)
            m15 = self.monitor.get_momentum(coin, 15)
            vol = self.monitor.get_volatility(coin, 15)

            yes_price = cm['yes_price']
            no_price = cm['no_price']

            if cm['volume'] < MIN_VOLUME:
                continue

            signal = self._evaluate_momentum(cm, coin, current_price, m5, m15, vol, yes_price, no_price)
            if not signal:
                continue

            # HyperThink validation (max 3 per cycle for crypto)
            if self.hyperthink and self._crypto_debates_this_cycle < MAX_CRYPTO_DEBATES:
                self._crypto_debates_this_cycle += 1
                ht = self._run_hyperthink(signal, cm)
                if ht['consensus'] not in ('UNANIMOUS', 'MAJORITY'):
                    logger.info(f"CryptoMomentum SKIP {cm['ticker']}: HyperThink={ht['consensus']}")
                    continue
                signal['count'] = 5 if ht['consensus'] == 'UNANIMOUS' else 2
                signal['ht_consensus'] = ht['consensus']
                signal['ht_grok'] = ht['grok_sentiment']
                signal['ht_claude'] = ht['claude_sentiment']
            else:
                signal['count'] = 2
                signal['ht_consensus'] = 'SKIPPED'
                signal['ht_grok'] = ''
                signal['ht_claude'] = ''

            signals.append(signal)

        signals.sort(key=lambda s: s.get('edge', 0), reverse=True)
        signals = signals[:MAX_SIGNALS]

        if signals:
            logger.info(f"CryptoMomentum: {len(signals)} signals, best edge={signals[0]['edge']:.0%}")
        return signals

    def _find_crypto_markets(self, markets):
        """Filter markets to crypto short-term contracts."""
        crypto_markets = []
        for m in markets:
            title = (m.get('title') or '').lower()
            ticker = m.get('ticker', '')

            is_crypto = any(kw in title for kw in CRYPTO_KEYWORDS)
            is_short = any(kw in title for kw in SHORT_TERM_KEYWORDS)

            if not (is_crypto and is_short):
                continue

            coin = None
            if 'bitcoin' in title or 'btc' in title:
                coin = 'BTC'
            elif 'ethereum' in title or 'eth' in title:
                coin = 'ETH'
            elif 'solana' in title or 'sol' in title:
                coin = 'SOL'

            yes_p = get_yes_price(m)
            no_p = get_no_price(m)
            volume = safe_float(m.get('volume_24h_fp', m.get('volume_24h', m.get('volume', 0))))

            crypto_markets.append({
                'ticker': ticker,
                'title': m.get('title', ''),
                'coin': coin,
                'yes_price': yes_p,
                'no_price': no_p,
                'volume': volume,
                'close_time': m.get('close_time', ''),
                'market': m,
            })

        crypto_markets.sort(key=lambda x: x['volume'], reverse=True)
        return crypto_markets

    def _evaluate_momentum(self, cm, coin, current_price, m5, m15, vol, yes_price, no_price):
        """Generate signal based on momentum vs market price."""
        signal_side = None
        edge = 0
        reasoning = ""

        # Strong upward momentum -> buy YES
        if m5 > MIN_MOMENTUM and m15 > 0:
            our_prob = 0.50 + min(m5 * 20, 0.30)
            market_prob = yes_price
            edge = our_prob - market_prob
            if edge > MIN_EDGE:
                signal_side = 'yes'
                reasoning = f"UP momentum: 5m={m5:+.2%} 15m={m15:+.2%} prob={our_prob:.0%} vs mkt={market_prob:.0%}"

        # Strong downward momentum -> buy NO
        elif m5 < -MIN_MOMENTUM and m15 < 0:
            our_prob = 0.50 + min(abs(m5) * 20, 0.30)
            market_prob = no_price
            edge = our_prob - market_prob
            if edge > MIN_EDGE:
                signal_side = 'no'
                reasoning = f"DOWN momentum: 5m={m5:+.2%} 15m={m15:+.2%} prob={our_prob:.0%} vs mkt={market_prob:.0%}"

        # Mean reversion: short dip in uptrend -> buy YES
        elif m5 < -0.01 and m15 > 0:
            our_prob = 0.60
            market_prob = yes_price
            edge = our_prob - market_prob
            if edge > MIN_EDGE:
                signal_side = 'yes'
                reasoning = f"BOUNCE: 5m dip={m5:+.2%} in 15m uptrend={m15:+.2%}"

        # Mean reversion: short spike in downtrend -> buy NO
        elif m5 > 0.01 and m15 < 0:
            our_prob = 0.60
            market_prob = no_price
            edge = our_prob - market_prob
            if edge > MIN_EDGE:
                signal_side = 'no'
                reasoning = f"PULLBACK: 5m spike={m5:+.2%} in 15m downtrend={m15:+.2%}"

        if not signal_side or edge <= 0:
            return None

        entry_price = yes_price if signal_side == 'yes' else no_price

        return {
            'ticker': cm['ticker'],
            'title': cm['title'],
            'action': 'buy',
            'side': signal_side,
            'count': 2,
            'confidence': min(50 + edge * 200, 95),
            'strategy_type': 'crypto_momentum',
            'edge': edge,
            'model_prob': 0.50 + min(abs(m5) * 20, 0.30),
            'coin': coin,
            'btc_price': current_price,
            'momentum_5m': m5,
            'momentum_15m': m15,
            'volatility': vol,
            'reason': f"[CRYPTO] {reasoning}",
        }

    def _run_hyperthink(self, signal, cm):
        """Run HyperThink validation for crypto signal."""
        coin = signal['coin']
        side_word = 'higher' if signal['side'] == 'yes' else 'lower'

        context = (
            f"15-minute {coin} prediction market. "
            f"Current {coin}: ${signal['btc_price']:,.2f}. "
            f"5-min momentum: {signal['momentum_5m']:+.2%}, "
            f"15-min momentum: {signal['momentum_15m']:+.2%}, "
            f"Volatility: {signal['volatility']:.3%}. "
            f"Signal: {coin} will be {side_word}. "
            f"Consider: crypto Twitter sentiment, order flow, trend vs fakeout, "
            f"session timing (Asia/Europe/US)."
        )

        yes_price = cm['yes_price']
        data_prob = signal['model_prob']

        avg_prob, confidence, multiplier = self.hyperthink.evaluate(
            cm['market'], signal['side'], yes_price,
            data_prob=data_prob, context=context,
        )

        # Map HyperThink output to consensus labels
        if confidence == "UNANIMOUS" or confidence == "STRONG":
            consensus = "UNANIMOUS" if multiplier >= 0.9 else "MAJORITY"
        elif confidence == "MODERATE":
            consensus = "MAJORITY"
        else:
            consensus = "AGAINST"

        return {
            'consensus': consensus,
            'confidence': multiplier,
            'grok_sentiment': '',
            'claude_sentiment': '',
        }

    def execute(self, signal, dry_run=False):
        """Execute crypto trade — paper only, also log to crypto_signals."""
        if not self.can_execute(signal):
            return None
        self.log_signal(signal)

        # Log to crypto_signals table
        if self.db:
            try:
                self.db.client.table('crypto_signals').insert({
                    'ticker': signal['ticker'],
                    'side': signal['side'],
                    'price': signal.get('model_prob', 0.5),
                    'count': signal['count'],
                    'btc_price_at_entry': signal.get('btc_price', 0),
                    'btc_momentum_5m': signal.get('momentum_5m', 0),
                    'btc_momentum_15m': signal.get('momentum_15m', 0),
                    'grok_sentiment': signal.get('ht_grok', ''),
                    'claude_sentiment': signal.get('ht_claude', ''),
                    'hyperthink_consensus': signal.get('ht_consensus', ''),
                    'hyperthink_confidence': signal.get('confidence', 0),
                    'order_id': 'paper',
                }).execute()
            except Exception as e:
                logger.debug(f"Failed to log crypto signal: {e}")

        return self.client.create_order(
            ticker=signal['ticker'], action='buy', side=signal['side'],
            count=signal['count'], order_type='market', dry_run=dry_run,
        )
