#!/usr/bin/env python3
"""
Kalshi Trading Bot - Paper Trading System

Strategies:
  - WeatherEdge: Open-Meteo GFS ensemble vs KXHIGH temperature markets
  - GrokNewsAnalysis: xAI Grok-3 evaluates top 20 markets by volume
  - ProbabilityArbitrage: YES+NO mispricing and orderbook spread detection
  - SportsNO: fade sports favorites (YES 60-85c) by buying NO
  - NearCertainty: 85-97c near expiry + 3-15c cheap contrarian
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

        self.strategies = []
        self._init_strategies()

        self._check_balance()
        logger.info(f"Paper balance: ${self.risk.paper_balance:.2f}")
        logger.info("=" * 60)

    def _init_strategies(self):
        logger.info("Loading strategies...")
        if Config.ENABLE_WEATHER:
            self.strategies.append(WeatherEdgeStrategy(self.client, self.risk, self.db))
        if Config.ENABLE_GROK:
            self.strategies.append(GrokNewsStrategy(self.client, self.risk, self.db))
        if Config.ENABLE_PROB_ARB:
            self.strategies.append(ProbabilityArbStrategy(self.client, self.risk, self.db))
        if Config.ENABLE_SPORTS_NO:
            self.strategies.append(SportsNOStrategy(self.client, self.risk, self.db))
        if Config.ENABLE_NEAR_CERTAINTY:
            self.strategies.append(NearCertaintyStrategy(self.client, self.risk, self.db))
        logger.info(f"{len(self.strategies)} strategies loaded")

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

        # Fetch markets
        logger.info("Fetching markets (limit=1000)...")
        data = self.client.get_markets(status='open', limit=1000)
        raw_markets = data.get('markets', [])

        # Filter out KXMVE multivariate combo/parlay markets
        before = len(raw_markets)
        markets = [
            m for m in raw_markets
            if not (m.get('ticker') or '').startswith('KXMVE')
        ]
        filtered = before - len(markets)
        logger.info(f"Filtered {filtered} KXMVE combo markets ({len(markets)} binary remaining)")

        # Fetch KXHIGH weather series separately (they don't appear in the default list)
        seen_tickers = {m.get('ticker') for m in markets}
        for series in ('KXHIGHNY', 'KXHIGHCHI', 'KXHIGHMIA', 'KXHIGHLAX', 'KXHIGHDEN'):
            try:
                weather = self.client.get_markets_by_series(series)
                added = 0
                for wm in weather:
                    if wm.get('ticker') not in seen_tickers:
                        wm.setdefault('status', 'open')  # series fetch implies open
                        markets.append(wm)
                        seen_tickers.add(wm.get('ticker'))
                        added += 1
                if added:
                    logger.info(f"  +{added} from {series}")
            except Exception as e:
                logger.debug(f"  {series} fetch failed: {e}")

        if not markets:
            logger.warning("No markets returned")
            self._log_status()
            return

        logger.info(f"Scanned {len(markets)} markets")

        # Debug: log first 10 markets
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

        # Run strategies
        total_signals = 0
        for strategy in self.strategies:
            logger.info(f"--- Running {strategy.name} ---")
            try:
                signals = strategy.analyze(markets)
            except Exception as e:
                logger.error(f"{strategy.name} crashed: {e}", exc_info=True)
                signals = []

            if not signals:
                logger.info(f"{strategy.name}: 0 signals")
                continue

            signals.sort(key=lambda s: s.get('confidence', 0), reverse=True)
            total_signals += len(signals)
            logger.info(f"{strategy.name}: {len(signals)} signals")

            for sig in signals:
                # Kelly sizing
                edge = sig.get('edge', 0)
                prob = sig.get('model_prob', 0.5)
                price = get_yes_price_dollars(
                    next((m for m in markets if m.get('ticker') == sig['ticker']), {})
                ) or 0.50
                price_for_side = price if sig['side'] == 'yes' else (1 - price)

                if edge > 0 and prob > 0:
                    sig['count'] = self.risk.kelly_size(edge, prob, int(price_for_side * 100))

                conf = sig.get('confidence', 0)
                logger.info(
                    f"Signal: {sig['ticker']} BUY {sig['side'].upper()} x{sig['count']} "
                    f"conf={conf:.0f} edge={edge:+.2f} - {sig.get('reason', '')}"
                )

                # Check position limits before paper trading
                if not self.risk.check_max_positions():
                    logger.info(f"  Skipping {sig['ticker']} - max positions reached")
                    break

                # Paper trade
                self.risk.record_paper_trade(
                    ticker=sig['ticker'],
                    side=sig['side'],
                    count=sig['count'],
                    entry_price=price_for_side,
                    strategy=sig.get('strategy_type', 'unknown'),
                    title=sig.get('title', ''),
                )

                # Log to Supabase
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

        # Forced paper trade if nothing fired
        if total_signals == 0:
            logger.info("No signals from any strategy - forcing paper trade")
            self._forced_paper_trade(markets)

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
