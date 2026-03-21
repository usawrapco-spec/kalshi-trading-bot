#!/usr/bin/env python3
"""
Kalshi Trading Bot
Main entry point for the automated trading system.

Strategies:
  - WeatherEdge: Open-Meteo ensemble vs KXHIGH temperature markets
  - GrokNewsAnalysis: xAI Grok evaluates market mispricings
  - NearCertainty: 90-97c markets settling <24h + cheap 3-10c contrarian
  - ForcedPaperTrade: best available opportunity if nothing else fires
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
from strategies.ai_analysis import GrokNewsAnalysisStrategy
from strategies.near_certainty import NearCertaintyStrategy
from dashboard import start_dashboard

logger = setup_logger('main')


class KalshiBot:
    """Main trading bot orchestrator."""

    def __init__(self, dry_run=False):
        self.dry_run = dry_run
        logger.info("=" * 60)
        logger.info("KALSHI TRADING BOT STARTING")
        logger.info("=" * 60)

        try:
            Config.validate()
        except ValueError as e:
            logger.error(f"Configuration error: {e}")
            logger.error("Please check your .env file")
            sys.exit(1)

        logger.info("Initializing components...")
        self.client = KalshiAPIClient()
        self.risk_manager = RiskManager()
        self.db = SupabaseDB()

        self.strategies = []
        self._initialize_strategies()

        self._check_balance()

        logger.info(f"{'DRY RUN MODE' if dry_run else 'LIVE TRADING MODE'}")
        logger.info("=" * 60)

    def _initialize_strategies(self):
        """Initialize enabled trading strategies."""
        logger.info("Loading strategies...")

        if Config.ENABLE_WEATHER:
            self.strategies.append(WeatherEdgeStrategy(self.client, self.risk_manager, self.db))
            logger.info("WeatherEdge strategy enabled (Open-Meteo ensemble, 5% min edge)")

        if Config.ENABLE_GROK:
            self.strategies.append(GrokNewsAnalysisStrategy(self.client, self.risk_manager, self.db))
            logger.info("GrokNewsAnalysis strategy enabled (grok-3, 10% min edge, 20 markets/cycle)")

        if Config.ENABLE_NEAR_CERTAINTY:
            self.strategies.append(NearCertaintyStrategy(self.client, self.risk_manager, self.db))
            logger.info("NearCertainty strategy enabled (90-97c <24h + 3-10c contrarian)")

        if not self.strategies:
            logger.warning("No strategies enabled!")

    def _check_balance(self):
        balance_data = self.client.get_balance()
        if balance_data:
            balance = balance_data.get('balance', 0) / 100
            logger.info(f"Account Balance: ${balance:.2f}")
        else:
            logger.warning("Could not retrieve balance")

    def run_cycle(self):
        """Run one iteration of the trading cycle."""
        logger.info("=" * 40)
        logger.info(f"Trading cycle starting at {datetime.now().isoformat()}")

        # Update balance for risk calculations
        balance_data = self.client.get_balance()
        balance_cents = balance_data.get('balance', 0) if balance_data else 0
        balance = balance_cents / 100
        self.risk_manager.set_balance(balance_cents)

        if not self.risk_manager.check_daily_loss_limit():
            logger.warning("Trading stopped for the day")
            self._log_status(balance)
            return

        # Get open markets (scan 500 for more opportunities)
        logger.info("Fetching markets...")
        markets_data = self.client.get_markets(status='open', limit=500)
        markets = markets_data.get('markets', [])

        if not markets:
            logger.warning("No open markets found")
            self._log_status(balance)
            return

        logger.info(f"Scanning {len(markets)} markets...")

        # Run each strategy
        total_signals = 0
        executed = 0
        all_signals = []

        for strategy in self.strategies:
            logger.info(f"Running {strategy.name}...")
            try:
                signals = strategy.analyze(markets)
            except Exception as e:
                logger.error(f"Strategy {strategy.name} failed: {e}", exc_info=True)
                signals = []

            if signals:
                logger.info(f"{strategy.name}: {len(signals)} signals found")
                total_signals += len(signals)
                all_signals.extend(signals)

                # Sort by confidence, execute best first
                signals.sort(key=lambda s: s.get('confidence', 0), reverse=True)

                for signal in signals:
                    # Apply Kelly Criterion sizing if signal has edge/probability info
                    edge = signal.get('edge', 0)
                    model_prob = signal.get('model_prob', 0)
                    price = signal.get('price', 50)
                    if edge > 0 and model_prob > 0:
                        kelly_count = self.risk_manager.kelly_size(edge, model_prob, price)
                        signal['count'] = kelly_count

                    conf = signal.get('confidence', 0)
                    logger.info(
                        f"Signal: {signal['ticker']} {signal['action']} {signal['side']} "
                        f"x{signal['count']} confidence={conf:.0f} - {signal.get('reason', '')}"
                    )
                    if self.dry_run:
                        logger.info(f"[DRY RUN] Would execute: {signal['ticker']} (conf={conf:.0f})")
                        # Log dry run trades to Supabase too
                        if self.db:
                            self.db.log_trade({
                                'ticker': signal['ticker'],
                                'action': signal['action'],
                                'side': signal['side'],
                                'count': signal['count'],
                                'strategy': signal.get('strategy_type', 'unknown'),
                                'reason': signal.get('reason'),
                                'confidence': conf,
                                'order_id': 'dry_run',
                                'price': 0,
                            })
                    else:
                        result = strategy.execute(signal, dry_run=self.dry_run)
                        if result:
                            executed += 1

        if total_signals == 0:
            logger.info("No trading opportunities found - attempting forced paper trade")
            self._force_paper_trade(markets)
        else:
            logger.info(f"Cycle complete: {total_signals} signals, {executed} executed")

        self._log_status(balance)

    def _force_paper_trade(self, markets):
        """Force a paper trade on the single best available opportunity."""
        best = None
        best_score = -1

        for m in markets:
            if m.get('status') != 'open':
                continue
            ticker = m.get('ticker', '')
            yes_bid = m.get('yes_bid', 0)
            no_bid = m.get('no_bid', 0)
            volume = m.get('volume', 0)

            for side_name, price in [('yes', yes_bid), ('no', no_bid)]:
                if price <= 0:
                    continue
                # Score: prefer liquid markets with extreme prices
                extremity = max(price, 100 - price)
                score = volume * 0.01 + extremity
                if score > best_score:
                    best_score = score
                    best = {'ticker': ticker, 'side': side_name, 'price': price, 'volume': volume}

        if not best:
            logger.warning("forced-paper-trade: no viable markets found")
            return

        signal = {
            'ticker': best['ticker'],
            'action': 'buy',
            'side': best['side'],
            'count': 1,
            'reason': (
                f'forced-paper-trade: best available {best["side"].upper()} '
                f'at {best["price"]}c, vol={best["volume"]}'
            ),
            'confidence': 0,
            'strategy_type': 'forced_paper',
        }

        logger.info(
            f"forced-paper-trade: {signal['ticker']} {signal['side']} "
            f"at {best['price']}c vol={best['volume']}"
        )

        if self.dry_run:
            logger.info(f"[DRY RUN] forced-paper-trade: {signal['ticker']}")
        if self.db:
            self.db.log_trade({
                'ticker': signal['ticker'],
                'action': signal['action'],
                'side': signal['side'],
                'count': signal['count'],
                'strategy': 'forced_paper',
                'reason': signal.get('reason'),
                'confidence': 0,
                'order_id': 'forced_paper',
                'price': best['price'],
            })

    def _log_status(self, balance):
        """Log risk status and bot status to Supabase."""
        self.risk_manager.log_status()
        status = self.risk_manager.get_status()
        active = sum(1 for v in status['positions'].values() if v != 0)
        self.db.log_bot_status({
            'is_running': True,
            'daily_pnl': status['daily_pnl'],
            'trades_today': status['trades_today'],
            'balance': balance,
            'active_positions': active,
        })

    def run(self):
        """Run the bot continuously."""
        logger.info("Bot is now running. Press Ctrl+C to stop.")

        try:
            while True:
                try:
                    self.run_cycle()
                except Exception as e:
                    logger.error(f"Error in trading cycle: {e}", exc_info=True)

                logger.info(f"Waiting {Config.CHECK_INTERVAL_SECONDS}s until next cycle...")
                time.sleep(Config.CHECK_INTERVAL_SECONDS)

        except KeyboardInterrupt:
            logger.info("Bot stopped by user")
            self._shutdown()

    def _shutdown(self):
        logger.info("Shutting down...")
        self.risk_manager.log_status()
        self._check_balance()
        logger.info("=" * 60)
        logger.info("BOT SHUTDOWN COMPLETE")
        logger.info("=" * 60)


def main():
    """Main entry point."""
    # Start web dashboard FIRST so Railway health checks pass immediately
    start_dashboard()

    parser = argparse.ArgumentParser(description='Kalshi Trading Bot')
    parser.add_argument(
        '--demo',
        action='store_true',
        help='Use demo API (recommended for testing)'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Dry run mode - analyze but don\'t place orders'
    )

    args = parser.parse_args()

    if args.demo:
        Config.KALSHI_API_HOST = 'https://demo-api.kalshi.co'
        logger.info("Demo mode enabled - using demo API")

    bot = KalshiBot(dry_run=args.dry_run or args.demo)
    bot.run()


if __name__ == '__main__':
    main()
