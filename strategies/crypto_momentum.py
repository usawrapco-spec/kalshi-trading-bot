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


MIN_MOMENTUM = 0.0005    # 0.05% — catches micro-moves (was 0.3%)
MIN_EDGE = 0.01          # 1% edge minimum (was 8%, then 3%) — paper wants volume
MIN_VOLUME = 0           # Trade ALL markets (was 10)
MAX_SIGNALS = 100        # Max signals per cycle — go ham
MAX_CRYPTO_DEBATES = 0   # Skip HyperThink for crypto scalps — speed > consensus

# Timeframe config: series ticker -> (coin, duration_minutes, name)
CRYPTO_TIMEFRAMES = {
    'KXBTC15M': {'coin': 'BTC', 'duration': 15,  'name': 'BTC 15-min'},
    'KXETH15M': {'coin': 'ETH', 'duration': 15,  'name': 'ETH 15-min'},
    'KXSOL15M': {'coin': 'SOL', 'duration': 15,  'name': 'SOL 15-min'},
    'KXBTC':    {'coin': 'BTC', 'duration': 60,   'name': 'BTC hourly'},
    'KXETH':    {'coin': 'ETH', 'duration': 60,   'name': 'ETH hourly'},
    'KXSOL':    {'coin': 'SOL', 'duration': 60,   'name': 'SOL hourly'},
}

# Extreme move thresholds for mean reversion
EXTREME_DIP = -0.005     # -0.5% in momentum window -> bounce play
EXTREME_SPIKE = 0.005    # +0.5% in momentum window -> pullback play


class CryptoMomentumStrategy(BaseStrategy):
    """Multi-timeframe crypto scalping — 15-min + hourly markets, no debate for speed."""

    def __init__(self, client, risk_manager, db, crypto_monitor=None, hyperthink=None):
        super().__init__(client, risk_manager, db)
        self.monitor = crypto_monitor
        self.hyperthink = hyperthink
        self._crypto_debates_this_cycle = 0
        self._cached_markets = []
        self._market_cache_time = 0
        self._market_cache_ttl = 30  # Refresh crypto markets every 30 seconds
        logger.info(f"CryptoMomentum initialized (monitor={'ON' if crypto_monitor else 'OFF'}, timeframes={len(CRYPTO_TIMEFRAMES)})")

    @resilient_strategy
    def analyze(self, markets):
        signals = []

        if not self.monitor:
            return signals

        # Always fetch prices to build history (throttled internally)
        prices = self.monitor.fetch_prices()
        if not prices:
            logger.info("CryptoMomentum: price fetch returned None, skipping")
            return signals

        # Reset per-cycle debate counter
        self._crypto_debates_this_cycle = 0

        # Fetch crypto markets (cached, refreshed every 30s for speed)
        import time as _time
        now = _time.time()
        if now - self._market_cache_time > self._market_cache_ttl or not self._cached_markets:
            self._cached_markets = self._fetch_crypto_markets()
            self._market_cache_time = now

        crypto_markets = self._cached_markets

        m5 = self.monitor.get_momentum('BTC', 5)
        m15 = self.monitor.get_momentum('BTC', 15)
        hist_len = len(self.monitor.price_history.get('BTC', []))
        logger.info(
            f"CryptoMomentum: prices={{BTC=${prices.get('BTC', 0):,.0f} ETH=${prices.get('ETH', 0):,.0f} SOL=${prices.get('SOL', 0):,.0f}}} "
            f"markets_found={len(crypto_markets)} history={hist_len}pts BTC_mom5m={m5:+.3%} BTC_mom15m={m15:+.3%}"
        )

        if not crypto_markets:
            return signals

        for cm in crypto_markets:  # Check ALL markets — no cap
            coin = cm['coin']
            if not coin or coin not in prices:
                continue

            current_price = prices[coin]
            duration = cm.get('duration', 15)

            # Adaptive momentum windows based on contract duration
            if duration <= 15:
                mom_window = 5    # 5-min momentum for 15-min markets
                vol_window = 15
            elif duration <= 60:
                mom_window = 15   # 15-min momentum for hourly markets
                vol_window = 30
            else:
                mom_window = 60   # 60-min momentum for daily markets
                vol_window = 60

            m_short = self.monitor.get_momentum(coin, mom_window)
            m_long = self.monitor.get_momentum(coin, vol_window)
            vol_short = self.monitor.get_volatility(coin, mom_window)
            vol_long = self.monitor.get_volatility(coin, vol_window)

            yes_price = cm['yes_price']
            no_price = cm['no_price']

            # Try all signal types: momentum, volatility expansion, extreme mean reversion
            for signal in self._generate_all_signals(cm, coin, current_price,
                                                      m_short, m_long, vol_short, vol_long,
                                                      yes_price, no_price, duration):
                # Skip HyperThink for crypto scalps — speed matters
                signal['count'] = 3
                signal['ht_consensus'] = 'SKIPPED'
                signal['ht_grok'] = ''
                signal['ht_claude'] = ''
                signals.append(signal)

        signals.sort(key=lambda s: s.get('edge', 0), reverse=True)
        signals = signals[:MAX_SIGNALS]

        if signals:
            logger.info(f"CryptoMomentum: {len(signals)} signals, best edge={signals[0]['edge']:.0%}")
        return signals

    def _fetch_crypto_markets(self):
        """Fetch crypto markets by series ticker — they don't appear in general list."""
        crypto_markets = []
        seen = set()

        for series_ticker, tf_config in CRYPTO_TIMEFRAMES.items():
            coin = tf_config['coin']
            duration = tf_config['duration']
            try:
                batch = self.client.get_markets_by_series(series_ticker, status='open')
                added = 0
                for m in batch:
                    ticker = m.get('ticker', '')
                    if ticker in seen:
                        continue
                    seen.add(ticker)

                    # Skip already-resolved
                    if m.get('result'):
                        continue

                    yes_p = get_yes_price(m)
                    no_p = get_no_price(m)
                    volume = safe_float(m.get('volume_24h_fp', m.get('volume_24h', m.get('volume', 0))))

                    crypto_markets.append({
                        'ticker': ticker,
                        'title': m.get('title', ''),
                        'coin': coin,
                        'duration': duration,
                        'series': series_ticker,
                        'yes_price': yes_p,
                        'no_price': no_p,
                        'volume': volume,
                        'close_time': m.get('close_time', ''),
                        'yes_bid': safe_float(m.get('yes_bid', m.get('yes_bid_dollars', 0))),
                        'yes_ask': safe_float(m.get('yes_ask', m.get('yes_ask_dollars', 0))),
                        'market': m,
                    })
                    added += 1

                if batch:
                    logger.info(f"CryptoMomentum: {series_ticker} ({tf_config['name']}) -> {added} open markets (of {len(batch)} fetched)")
                else:
                    logger.debug(f"CryptoMomentum: {series_ticker} -> 0 markets")
            except Exception as e:
                logger.debug(f"CryptoMomentum: {series_ticker} fetch failed: {e}")

        crypto_markets.sort(key=lambda x: x['volume'], reverse=True)
        return crypto_markets

    def _generate_all_signals(self, cm, coin, current_price, m_short, m_long, vol_short, vol_long, yes_price, no_price, duration):
        """Generate ALL signal types: bracket, momentum, volatility expansion, mean reversion."""
        signals = []

        # --- SIGNAL TYPE 0: Bracket-based direct price comparison (highest volume) ---
        sig = self._check_bracket(cm, coin, current_price, yes_price, no_price, duration)
        if sig:
            signals.append(sig)

        # --- SIGNAL TYPE 1: Momentum (lowered thresholds) ---
        sig = self._check_momentum(cm, coin, current_price, m_short, m_long, yes_price, no_price, duration)
        if sig:
            signals.append(sig)

        # --- SIGNAL TYPE 2: Volatility expansion ---
        sig = self._check_volatility_expansion(cm, coin, current_price, m_short, vol_short, vol_long, yes_price, no_price, duration)
        if sig:
            signals.append(sig)

        # --- SIGNAL TYPE 3: Extreme mean reversion ---
        sig = self._check_extreme_reversion(cm, coin, current_price, m_short, yes_price, no_price, duration)
        if sig:
            signals.append(sig)

        return signals

    def _check_bracket(self, cm, coin, current_price, yes_price, no_price, duration):
        """Parse bracket threshold from ticker and compare to current price."""
        ticker = cm['ticker']
        title = cm.get('title', '')

        # Try to extract threshold from title (e.g., "Will BTC be above $85,000 at ...")
        threshold = self._parse_threshold(ticker, title, coin)
        if not threshold or threshold <= 0:
            return None

        # Calculate distance from bracket
        distance_pct = (current_price - threshold) / threshold

        # Determine if this is an "above" or "below" market
        title_lower = title.lower()
        is_above = 'above' in title_lower or 'higher' in title_lower or 'over' in title_lower or 'up' in title_lower
        is_below = 'below' in title_lower or 'lower' in title_lower or 'under' in title_lower or 'down' in title_lower

        # Default to "above" if we can't tell
        if not is_above and not is_below:
            is_above = True

        if is_above:
            if distance_pct > 0.02:  # Price well above threshold → YES likely
                our_prob = min(0.55 + distance_pct * 3, 0.95)
                edge = our_prob - yes_price
                if edge > MIN_EDGE and yes_price < 0.95:
                    return self._make_signal(cm, coin, current_price, 0, 0, 0, 'yes', edge, our_prob,
                        f"BRACKET: {coin}=${current_price:,.0f} is {distance_pct:+.1%} above ${threshold:,.0f}", duration)
            elif distance_pct < -0.02:  # Price well below threshold → NO likely
                our_prob = min(0.55 + abs(distance_pct) * 3, 0.95)
                edge = our_prob - no_price
                if edge > MIN_EDGE and no_price < 0.95:
                    return self._make_signal(cm, coin, current_price, 0, 0, 0, 'no', edge, our_prob,
                        f"BRACKET: {coin}=${current_price:,.0f} is {distance_pct:+.1%} below ${threshold:,.0f}", duration)
            else:
                # Near boundary — buy the cheaper side for exploration
                if yes_price < no_price and yes_price < 0.50:
                    return self._make_signal(cm, coin, current_price, 0, 0, 0, 'yes', 0.02, 0.52,
                        f"BRACKET NEAR: {coin}=${current_price:,.0f} near ${threshold:,.0f}, exploring YES", duration)
                elif no_price < 0.50:
                    return self._make_signal(cm, coin, current_price, 0, 0, 0, 'no', 0.02, 0.52,
                        f"BRACKET NEAR: {coin}=${current_price:,.0f} near ${threshold:,.0f}, exploring NO", duration)
        elif is_below:
            if distance_pct < -0.02:  # Price below threshold → YES for "below" market
                our_prob = min(0.55 + abs(distance_pct) * 3, 0.95)
                edge = our_prob - yes_price
                if edge > MIN_EDGE and yes_price < 0.95:
                    return self._make_signal(cm, coin, current_price, 0, 0, 0, 'yes', edge, our_prob,
                        f"BRACKET BELOW: {coin}=${current_price:,.0f} below ${threshold:,.0f}", duration)
            elif distance_pct > 0.02:
                our_prob = min(0.55 + distance_pct * 3, 0.95)
                edge = our_prob - no_price
                if edge > MIN_EDGE and no_price < 0.95:
                    return self._make_signal(cm, coin, current_price, 0, 0, 0, 'no', edge, our_prob,
                        f"BRACKET ABOVE: {coin}=${current_price:,.0f} above ${threshold:,.0f}", duration)

        return None

    def _parse_threshold(self, ticker, title, coin):
        """Extract price threshold from ticker or title."""
        import re as _re

        # Try title first: "Will BTC be above $85,000" or "Bitcoin above $85000"
        # Match patterns like $85,000 or $85000 or $85.5K
        price_match = _re.search(r'\$([0-9,]+\.?[0-9]*)\s*[Kk]?', title)
        if price_match:
            try:
                val = price_match.group(1).replace(',', '')
                threshold = float(val)
                if 'K' in title[price_match.end():price_match.end()+2].upper() or 'k' in title[price_match.end():price_match.end()+2]:
                    threshold *= 1000
                # Sanity check based on coin
                if coin == 'BTC' and 10000 < threshold < 500000:
                    return threshold
                elif coin == 'ETH' and 100 < threshold < 50000:
                    return threshold
                elif coin == 'SOL' and 1 < threshold < 5000:
                    return threshold
                # If no sanity check matched, still return if reasonable
                if threshold > 0:
                    return threshold
            except Exception:
                pass

        # Try ticker: KXBTC15M-26MAR22-T68000 or -B68000 or -T68K
        parts = ticker.split('-')
        for part in reversed(parts):
            if not part:
                continue
            prefix = part[0].upper()
            if prefix in ('T', 'B'):
                try:
                    num_str = part[1:]
                    if num_str.upper().endswith('K'):
                        return float(num_str[:-1]) * 1000
                    elif num_str.upper().endswith('M'):
                        return float(num_str[:-1]) * 1000000
                    else:
                        val = float(num_str)
                        # If small number for BTC, probably in thousands
                        if coin == 'BTC' and val < 1000:
                            return val * 1000
                        return val
                except Exception:
                    continue

        return None

    def _make_signal(self, cm, coin, current_price, m_short, m_long, vol, side, edge, our_prob, reasoning, duration):
        """Build a standard signal dict."""
        return {
            'ticker': cm['ticker'],
            'title': cm['title'],
            'action': 'buy',
            'side': side,
            'count': 3,
            'confidence': min(50 + edge * 200, 95),
            'strategy_type': 'crypto_momentum',
            'edge': edge,
            'model_prob': our_prob,
            'coin': coin,
            'btc_price': current_price,
            'momentum_5m': m_short,
            'momentum_15m': m_long,
            'volatility': vol,
            'duration': duration,
            'reason': f"[CRYPTO {duration}m] {reasoning}",
        }

    def _check_momentum(self, cm, coin, current_price, m_short, m_long, yes_price, no_price, duration):
        """Standard momentum signal with lowered thresholds."""
        # Strong upward momentum -> buy YES
        if m_short > MIN_MOMENTUM and m_long >= 0:
            our_prob = 0.50 + min(abs(m_short) * 30, 0.35)
            edge = our_prob - yes_price
            if edge > MIN_EDGE:
                return self._make_signal(cm, coin, current_price, m_short, m_long, 0, 'yes', edge, our_prob,
                    f"UP: short={m_short:+.3%} long={m_long:+.3%} prob={our_prob:.0%} vs mkt={yes_price:.0%}", duration)

        # Strong downward momentum -> buy NO
        elif m_short < -MIN_MOMENTUM and m_long <= 0:
            our_prob = 0.50 + min(abs(m_short) * 30, 0.35)
            edge = our_prob - no_price
            if edge > MIN_EDGE:
                return self._make_signal(cm, coin, current_price, m_short, m_long, 0, 'no', edge, our_prob,
                    f"DOWN: short={m_short:+.3%} long={m_long:+.3%} prob={our_prob:.0%} vs mkt={no_price:.0%}", duration)

        # Mean reversion: short dip in uptrend -> buy YES
        elif m_short < -0.003 and m_long > 0:
            our_prob = 0.58
            edge = our_prob - yes_price
            if edge > MIN_EDGE:
                return self._make_signal(cm, coin, current_price, m_short, m_long, 0, 'yes', edge, our_prob,
                    f"BOUNCE: dip={m_short:+.3%} in uptrend={m_long:+.3%}", duration)

        # Mean reversion: short spike in downtrend -> buy NO
        elif m_short > 0.003 and m_long < 0:
            our_prob = 0.58
            edge = our_prob - no_price
            if edge > MIN_EDGE:
                return self._make_signal(cm, coin, current_price, m_short, m_long, 0, 'no', edge, our_prob,
                    f"PULLBACK: spike={m_short:+.3%} in downtrend={m_long:+.3%}", duration)

        return None

    def _check_volatility_expansion(self, cm, coin, current_price, m_short, vol_short, vol_long, yes_price, no_price, duration):
        """When volatility suddenly increases, momentum is about to happen."""
        if vol_long <= 0:
            return None

        # Volatility expanding: short-term vol > 1.5x long-term vol
        if vol_short > vol_long * 1.5:
            # Direction = whatever the short-term momentum is
            if m_short > 0:
                our_prob = 0.58
                edge = our_prob - yes_price
                if edge > MIN_EDGE:
                    return self._make_signal(cm, coin, current_price, m_short, 0, vol_short, 'yes', edge, our_prob,
                        f"VOL EXPANSION: short_vol={vol_short:.4f} > long_vol={vol_long:.4f}, direction=UP", duration)
            elif m_short < 0:
                our_prob = 0.58
                edge = our_prob - no_price
                if edge > MIN_EDGE:
                    return self._make_signal(cm, coin, current_price, m_short, 0, vol_short, 'no', edge, our_prob,
                        f"VOL EXPANSION: short_vol={vol_short:.4f} > long_vol={vol_long:.4f}, direction=DOWN", duration)

        return None

    def _check_extreme_reversion(self, cm, coin, current_price, m_short, yes_price, no_price, duration):
        """Extreme moves trigger mean reversion bets."""
        # Extreme dip -> bounce expected, buy YES
        if m_short < EXTREME_DIP:
            our_prob = 0.60
            edge = our_prob - yes_price
            if edge > MIN_EDGE:
                return self._make_signal(cm, coin, current_price, m_short, 0, 0, 'yes', edge, our_prob,
                    f"EXTREME DIP: {m_short:+.2%} -> bounce expected", duration)

        # Extreme spike -> pullback expected, buy NO
        if m_short > EXTREME_SPIKE:
            our_prob = 0.60
            edge = our_prob - no_price
            if edge > MIN_EDGE:
                return self._make_signal(cm, coin, current_price, m_short, 0, 0, 'no', edge, our_prob,
                    f"EXTREME SPIKE: {m_short:+.2%} -> pullback expected", duration)

        return None

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

    def get_cached_markets(self):
        """Return cached crypto markets for use by other strategies (e.g., spread scalper)."""
        return self._cached_markets

    def find_spread_opportunities(self):
        """Find crypto markets with wide enough spreads for market-making scalps."""
        signals = []
        for market in self._cached_markets[:20]:
            yes_bid = market.get('yes_bid', 0)
            yes_ask = market.get('yes_ask', 0)

            # Normalize to dollar values
            if yes_bid > 1.0:
                yes_bid = yes_bid / 100.0
            if yes_ask > 1.0:
                yes_ask = yes_ask / 100.0

            if yes_bid <= 0 or yes_ask <= 0:
                continue

            spread = yes_ask - yes_bid

            # Need at least 3c spread to cover fees on both sides
            if spread >= 0.03 and yes_bid > 0.05 and yes_ask < 0.95:
                buy_price = yes_bid + 0.01
                sell_price = yes_ask - 0.01
                net_spread = sell_price - buy_price

                # Fee estimate: 7% of price * (1-price)
                buy_fee = 0.07 * buy_price * (1 - buy_price)
                sell_fee = 0.07 * sell_price * (1 - sell_price)
                net_profit = net_spread - buy_fee - sell_fee

                if net_profit > 0.005:  # At least half a cent profit
                    signals.append({
                        'ticker': market['ticker'],
                        'title': market.get('title', ''),
                        'action': 'buy',
                        'side': 'yes',
                        'count': 2,
                        'confidence': 65,
                        'strategy_type': 'market_making_scalp',
                        'edge': net_profit,
                        'model_prob': (yes_bid + yes_ask) / 2,
                        'buy_price': buy_price,
                        'sell_price': sell_price,
                        'spread': spread,
                        'net_profit': net_profit,
                        'reason': f"[SPREAD] {market['ticker']} bid=${yes_bid:.2f} ask=${yes_ask:.2f} spread={spread:.2f} net=${net_profit:.3f}",
                    })
                    logger.info(f"SPREAD: {market['ticker']} bid=${yes_bid:.2f} ask=${yes_ask:.2f} spread={spread:.2f} net_profit=${net_profit:.3f}")

        signals.sort(key=lambda x: x['net_profit'], reverse=True)
        return signals[:5]

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
