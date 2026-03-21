#!/usr/bin/env python3
"""
Kalshi Trading Bot - Paper Trading System

Strategies:
  - WeatherEdge: Open-Meteo GFS ensemble vs KXHIGH temperature markets
  - GrokNewsAnalysis: xAI Grok-3 evaluates top 20 liquid markets (vol>=10)
  - ProbabilityArbitrage: YES+NO mispricing and orderbook spread detection
  - SportsNO: fade sports favorites (YES 60-85c) by buying NO
  - NearCertainty: 85-97c near expiry + 3-15c cheap contrarian
  - MentionMarkets: Grok-powered mention/pop-culture market analysis
  - HighProbLock: buy YES at 92-98c on high-confidence markets for bond-like ROI
  - OrderBookEdge: bid/ask imbalance on short-term crypto/weather markets
  - ForcedPaperTrade: highest-volume market fallback (always fires)
"""

import sys
import time
import argparse
from datetime import datetime

from config import Config
from utils.logger import setup_logger
from utils.kalshi_client import KalshiAPIClient
from utils.risk_manager import RiskManager
from utils.supabase_db import SupabaseDB
from strategies.weather_edge import WeatherEdgeStrategy
from strategies.grok_news import GrokNewsStrategy
from strategies.prob_arb import ProbabilityArbStrategy
from strategies.sports_no import SportsNOStrategy
from strategies.near_certainty import NearCertaintyStrategy
from strategies.mention_markets import MentionMarketsStrategy
from strategies.high_prob_lock import HighProbLockStrategy
from strategies.orderbook_edge import OrderBookEdgeStrategy
from strategies.cross_platform import CrossPlatformEdgeStrategy
from dashboard import start_dashboard
from utils.market_helpers import get_yes_price as get_yes_price_dollars, get_volume

logger = setup_logger('main')


class KalshiBot:
    """Main trading bot orchestrator with paper trading."""

    def __init__(self, dry_run=True):
        self.dry_run = dry_run
        logger.info("=" * 60)
        logger.info("KALSHI TRADING BOT - PAPER TRADING MODE")
        logger.info("=" * 60)

        try:
            Config.validate()
        except ValueError as e:
            logger.error(f"Config error: {e}")
            sys.exit(1)

        self.client = KalshiAPIClient()
        self.risk = RiskManager()
        self.db = SupabaseDB()

        # Fresh start: clear old paper trades from Supabase
        self._clear_old_trades()

        self.strategies = []
        self._init_strategies()

        self._check_balance()
        logger.info(f"Paper balance: ${self.risk.paper_balance:.2f}")
        logger.info("=" * 60)

    def _init_strategies(self):
        logger.info("Loading strategies...")
        # Order matters: run scarce-signal strategies first so they get position slots
        # before WeatherEdge floods with 30+ signals
        if Config.ENABLE_GROK:
            self.strategies.append(GrokNewsStrategy(self.client, self.risk, self.db))
        if Config.ENABLE_MENTION:
            self.strategies.append(MentionMarketsStrategy(self.client, self.risk, self.db))
        if Config.ENABLE_HIGH_PROB:
            self.strategies.append(HighProbLockStrategy(self.client, self.risk, self.db))
        if Config.ENABLE_CROSS_PLATFORM:
            self.strategies.append(CrossPlatformEdgeStrategy(self.client, self.risk, self.db))
        if Config.ENABLE_ORDERBOOK:
            self.strategies.append(OrderBookEdgeStrategy(self.client, self.risk, self.db))
        if Config.ENABLE_PROB_ARB:
            self.strategies.append(ProbabilityArbStrategy(self.client, self.risk, self.db))
        if Config.ENABLE_SPORTS_NO:
            self.strategies.append(SportsNOStrategy(self.client, self.risk, self.db))
        if Config.ENABLE_NEAR_CERTAINTY:
            self.strategies.append(NearCertaintyStrategy(self.client, self.risk, self.db))
        if Config.ENABLE_WEATHER:
            self.strategies.append(WeatherEdgeStrategy(self.client, self.risk, self.db))
        logger.info(f"{len(self.strategies)} strategies loaded")

    def _clear_old_trades(self):
        """Clear all old paper trades from Supabase for a fresh start."""
        if not self.db or not self.db.client:
            return
        try:
            # Delete all old paper trades
            self.db.client.table('kalshi_trades').delete().neq('id', 0).execute()
            logger.info("Cleared all old paper trades from Supabase - fresh start")
        except Exception as e:
            logger.error(f"Failed to clear old trades: {e}")

    def _check_balance(self):
        bal = self.client.get_balance()
        if bal:
            logger.info(f"Kalshi balance: ${bal.get('balance', 0)/100:.2f}")

    def run_cycle(self):
        logger.info("=" * 40)
        logger.info(f"Cycle at {datetime.now().isoformat()}")

        if not self.risk.check_daily_loss_limit():
            logger.warning("Daily loss stop - halting")
            self._log_status()
            return

        # Fetch markets from multiple sources to get real binary markets
        # The default /markets endpoint is flooded with KXMVE parlays,
        # so we also use /events and series-specific fetches.
        logger.info("Fetching markets from multiple sources...")
        seen_tickers = set()
        markets = []

        def _add_markets(batch, source):
            added = 0
            skipped_resolved = 0
            for m in batch:
                ticker = m.get('ticker')
                if not ticker or ticker in seen_tickers:
                    continue
                if ticker.startswith('KXMVE'):
                    continue  # Skip multivariate parlays
                # Skip already-resolved markets (result field is set, or price is 0/1.00)
                if m.get('result'):
                    skipped_resolved += 1
                    continue
                yes_p = get_yes_price_dollars(m)
                if yes_p >= 0.99 or (yes_p <= 0.01 and yes_p > 0):
                    skipped_resolved += 1
                    continue
                m['status'] = 'open'
                markets.append(m)
                seen_tickers.add(ticker)
                added += 1
            msg = f"  +{added} from {source}"
            if skipped_resolved:
                msg += f" ({skipped_resolved} resolved/settled skipped)"
            if added or skipped_resolved:
                logger.info(msg)

        # 1. Events endpoint - returns categorized binary markets
        try:
            events = self.client.get_events(status='open', limit=200)
            event_markets = []
            for evt in events:
                for m in (evt.get('markets') or []):
                    event_markets.append(m)
            _add_markets(event_markets, f"events ({len(events)} events)")
        except Exception as e:
            logger.error(f"Events fetch failed: {e}")

        # 2. Direct markets fetch (will get some non-KXMVE binary markets)
        try:
            data = self.client.get_markets(status='open', limit=1000)
            _add_markets(data.get('markets', []), "markets endpoint")
        except Exception as e:
            logger.error(f"Markets fetch failed: {e}")

        # 3. Weather series
        for series in ('KXHIGHNY', 'KXHIGHCHI', 'KXHIGHMIA', 'KXHIGHLAX', 'KXHIGHDEN'):
            try:
                _add_markets(self.client.get_markets_by_series(series), series)
            except Exception as e:
                logger.debug(f"  {series} failed: {e}")

        if not markets:
            logger.warning("No markets returned")
            self._log_status()
            return

        # Sort all markets by volume descending so strategies analyze liquid markets first
        markets.sort(key=lambda m: get_volume(m), reverse=True)

        logger.info(f"Scanned {len(markets)} markets")

        # Debug: log first 10 markets (now sorted by volume)
        logger.info("--- First 10 markets ---")
        for m in markets[:10]:
            ticker = m.get('ticker', '?')
            title = (m.get('title') or '?')[:55]
            yes_p = get_yes_price_dollars(m)
            vol = get_volume(m)
            cat = m.get('category', '')
            logger.info(f"  {ticker}: yes=${yes_p:.2f} vol={vol:.0f} cat={cat} \"{title}\"")
        if markets:
            logger.info(f"  Keys: {list(markets[0].keys())}")

        # Phase 1: Collect signals from ALL strategies
        all_signals = []
        for strategy in self.strategies:
            logger.info(f"--- Running {strategy.name} ---")
            try:
                signals = strategy.analyze(markets)
            except Exception as e:
                logger.error(f"{strategy.name} crashed: {e}", exc_info=True)
                signals = []

            if signals:
                logger.info(f"{strategy.name}: {len(signals)} signals")
                all_signals.extend(signals)
            else:
                logger.info(f"{strategy.name}: 0 signals")

        # Phase 2: Pick top 2 signals by confidence, max $5 total spend
        MAX_TRADES = 2
        MAX_CYCLE_SPEND = 5.00
        all_signals.sort(key=lambda s: s.get('confidence', 0), reverse=True)
        logger.info(f"Total signals across all strategies: {len(all_signals)}")

        trades_placed = 0
        cycle_spent = 0.0
        for sig in all_signals:
            if trades_placed >= MAX_TRADES:
                break

            # Calculate price and Kelly sizing
            edge = sig.get('edge', 0)
            prob = sig.get('model_prob', 0.5)
            price = get_yes_price_dollars(
                next((m for m in markets if m.get('ticker') == sig['ticker']), {})
            ) or 0.50
            price_for_side = price if sig['side'] == 'yes' else (1 - price)

            if edge > 0 and prob > 0:
                sig['count'] = self.risk.kelly_size(edge, prob, int(price_for_side * 100))

            cost = sig['count'] * price_for_side
            if cycle_spent + cost > MAX_CYCLE_SPEND:
                # Reduce count to fit within cycle budget
                remaining = MAX_CYCLE_SPEND - cycle_spent
                if remaining < price_for_side:
                    continue  # Can't even afford 1 contract
                sig['count'] = max(1, int(remaining / price_for_side))
                cost = sig['count'] * price_for_side

            conf = sig.get('confidence', 0)
            logger.info(
                f"TOP PICK: {sig['ticker']} BUY {sig['side'].upper()} x{sig['count']} "
                f"conf={conf:.0f} edge={edge:+.2f} cost=${cost:.2f} "
                f"[{sig.get('strategy_type', '?')}] - {sig.get('reason', '')}"
            )

            traded = self.risk.record_paper_trade(
                ticker=sig['ticker'],
                side=sig['side'],
                count=sig['count'],
                entry_price=price_for_side,
                strategy=sig.get('strategy_type', 'unknown'),
                title=sig.get('title', ''),
            )

            if not traded:
                continue

            trades_placed += 1
            cycle_spent += cost

            if self.db:
                self.db.log_trade({
                    'ticker': sig['ticker'],
                    'action': 'buy',
                    'side': sig['side'],
                    'count': sig['count'],
                    'strategy': sig.get('strategy_type', 'unknown'),
                    'reason': sig.get('reason', ''),
                    'confidence': conf,
                    'order_id': 'paper',
                    'price': price_for_side,
                })

        logger.info(f"Cycle done: {trades_placed} trades, ${cycle_spent:.2f} spent")

        # Forced paper trade if nothing fired
        if len(all_signals) == 0:
            logger.info("No signals from any strategy - forcing paper trade")
            self._forced_paper_trade(markets)

        # Check for settled paper trades and calculate P&L
        self._check_settlements(markets)

        self._log_status()

    def _forced_paper_trade(self, markets):
        """Pick highest-volume market and paper trade it. NEVER fails."""
        if not markets:
            logger.warning("ForcedPaper: no markets at all")
            return

        sorted_m = sorted(
            markets,
            key=lambda m: (get_volume(m)),
            reverse=True,
        )
        m = sorted_m[0]
        ticker = m.get('ticker', 'UNKNOWN')
        title = (m.get('title') or '')[:60]
        yes_price = get_yes_price_dollars(m) or 0.50
        volume = get_volume(m)

        # Pick the side closest to 50c (most uncertain = most potential)
        side = 'yes' if yes_price <= 0.50 else 'no'
        entry = yes_price if side == 'yes' else (1 - yes_price)
        entry = max(entry, 0.01)  # Never zero

        logger.info(
            f"ForcedPaper: {ticker} BUY {side.upper()} @ ${entry:.2f}, vol={volume} \"{title}\""
        )

        self.risk.record_paper_trade(
            ticker=ticker, side=side, count=1,
            entry_price=entry, strategy='forced_paper', title=title,
        )

        if self.db:
            self.db.log_trade({
                'ticker': ticker, 'action': 'buy', 'side': side,
                'count': 1, 'strategy': 'forced_paper',
                'reason': f"ForcedPaper: highest vol market, {side.upper()} @ ${entry:.2f}, vol={volume}",
                'confidence': 0, 'order_id': 'forced_paper', 'price': entry,
            })

    def _check_settlements(self, markets):
        """Check if any open paper trades have settled and calculate P&L."""
        if not self.risk.positions:
            return

        # Build ticker -> market lookup from current data
        market_map = {m.get('ticker'): m for m in markets}

        settled = []
        for ticker, pos in list(self.risk.positions.items()):
            m = market_map.get(ticker)
            if not m:
                # Market not in current fetch - try fetching it directly
                try:
                    m = self.client.get_market(ticker)
                    if m and 'market' in m:
                        m = m['market']
                except Exception:
                    continue

            if not m:
                continue

            # Check if market has resolved
            result = m.get('result')
            settlement = m.get('settlement_value_dollars')

            if result is None and settlement is None:
                continue

            # Determine if YES won
            if result == 'yes' or result is True:
                resolved_yes = True
            elif result == 'no' or result is False:
                resolved_yes = False
            elif settlement is not None:
                sv = float(settlement) if settlement else 0
                resolved_yes = sv > 0.50
            else:
                continue

            # Settle the paper trade
            self.risk.settle_paper_trade(ticker, resolved_yes)
            settled.append(ticker)

            # Log settlement to Supabase
            if self.db:
                side = pos['side']
                won = (side == 'yes' and resolved_yes) or (side == 'no' and not resolved_yes)
                entry = pos['entry_price']
                count = pos['count']
                pnl = (count * 1.0 - count * entry) if won else (-count * entry)
                try:
                    self.db.log_trade({
                        'ticker': ticker,
                        'action': 'settle',
                        'side': pos['side'],
                        'count': count,
                        'strategy': pos.get('strategy', 'unknown'),
                        'reason': f"SETTLED {'WIN' if won else 'LOSS'}: pnl=${pnl:+.2f}, result={result}",
                        'confidence': 100 if won else 0,
                        'order_id': 'settlement',
                        'price': 1.0 if won else 0.0,
                    })
                except Exception as e:
                    logger.error(f"Failed to log settlement for {ticker}: {e}")

        if settled:
            logger.info(f"Settled {len(settled)} paper trades: {settled}")

    def _log_status(self):
        self.risk.log_status()
        status = self.risk.get_status()
        if self.db:
            self.db.log_bot_status({
                'is_running': True,
                'daily_pnl': status['daily_pnl'],
                'trades_today': status['trades_today'],
                'balance': status['paper_balance'],
                'active_positions': len(status['positions']),
            })

    def run(self):
        logger.info("Bot running. Ctrl+C to stop.")
        try:
            while True:
                try:
                    self.run_cycle()
                except Exception as e:
                    logger.error(f"Cycle error: {e}", exc_info=True)
                logger.info(f"Next cycle in {Config.CHECK_INTERVAL_SECONDS}s...")
                time.sleep(Config.CHECK_INTERVAL_SECONDS)
        except KeyboardInterrupt:
            logger.info("Bot stopped by user")
            self.risk.log_status()


def main():
    start_dashboard()

    parser = argparse.ArgumentParser(description='Kalshi Trading Bot')
    parser.add_argument('--demo', action='store_true', help='Use demo API')
    parser.add_argument('--dry-run', action='store_true', help='Paper trading mode')
    args = parser.parse_args()

    if args.demo:
        Config.KALSHI_API_HOST = 'https://demo-api.kalshi.co'
        logger.info("Demo mode - using demo API")

    bot = KalshiBot(dry_run=True)  # Always paper trading for now
    bot.run()


if __name__ == '__main__':
    main()
