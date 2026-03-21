#!/usr/bin/env python3
"""
Kalshi Trading Bot
Main entry point for the automated trading system.
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
from strategies.arbitrage import ArbitrageStrategy
from strategies.momentum import MomentumStrategy
from strategies.value_betting import ValueBettingStrategy
from dashboard import start_dashboard

logger = setup_logger('main')


class KalshiBot:
    """Main trading bot orchestrator."""
    
    def __init__(self, dry_run=False):
        """
        Initialize the trading bot.
        
        Args:
            dry_run: If True, don't place actual orders
        """
        self.dry_run = dry_run
        logger.info("=" * 60)
        logger.info("KALSHI TRADING BOT STARTING")
        logger.info("=" * 60)
        
        # Validate configuration
        try:
            Config.validate()
        except ValueError as e:
            logger.error(f"Configuration error: {e}")
            logger.error("Please check your .env file")
            sys.exit(1)
        
        # Initialize components
        logger.info("Initializing components...")
        self.client = KalshiAPIClient()
        self.risk_manager = RiskManager()
        self.db = SupabaseDB()  # Add Supabase integration
        
        # Initialize strategies
        self.strategies = []
        self._initialize_strategies()
        
        # Check initial balance
        self._check_balance()
        
        logger.info(f"{'🧪 DRY RUN MODE' if dry_run else '🔴 LIVE TRADING MODE'}")
        logger.info("=" * 60)
    
    def _initialize_strategies(self):
        """Initialize enabled trading strategies."""
        logger.info("Loading strategies...")

        if Config.ENABLE_ARBITRAGE:
            self.strategies.append(ArbitrageStrategy(self.client, self.risk_manager, self.db, min_edge=0.03))
            logger.info("Arbitrage strategy enabled (3% min edge, 2 confirmations)")

        if Config.ENABLE_MOMENTUM:
            self.strategies.append(MomentumStrategy(self.client, self.risk_manager, self.db, price_change_threshold=0.05))
            logger.info("Momentum strategy enabled (5% threshold, 3 confirming points)")

        # Value betting is always enabled - conservative high win-rate strategy
        self.strategies.append(ValueBettingStrategy(self.client, self.risk_manager, self.db))
        logger.info("Value betting strategy enabled (90-97c range)")

        if not self.strategies:
            logger.warning("No strategies enabled!")
    
    def _check_balance(self):
        """Check and log current account balance."""
        balance_data = self.client.get_balance()
        if balance_data:
            balance = balance_data.get('balance', 0) / 100  # Convert cents to dollars
            logger.info(f"💰 Account Balance: ${balance:.2f}")
        else:
            logger.warning("Could not retrieve balance")
    
    def run_cycle(self):
        """Run one iteration of the trading cycle."""
        logger.info("Starting trading cycle...")

        # Update balance for risk calculations
        balance_data = self.client.get_balance()
        balance_cents = balance_data.get('balance', 0) if balance_data else 0
        balance = balance_cents / 100
        self.risk_manager.set_balance(balance_cents)

        # Check if we can still trade today
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

        logger.info(f"Analyzing {len(markets)} markets...")

        # Log top near-miss opportunities for debugging
        self._log_near_misses(markets)

        # Run each strategy
        total_signals = 0
        executed = 0
        for strategy in self.strategies:
            logger.info(f"Running {strategy.name}...")
            signals = strategy.analyze(markets)

            if signals:
                logger.info(f"Found {len(signals)} signals from {strategy.name}")
                total_signals += len(signals)

                # Sort by confidence descending - execute best opportunities first
                signals.sort(key=lambda s: s.get('confidence', 0), reverse=True)

                for signal in signals:
                    conf = signal.get('confidence', 0)
                    logger.info(
                        f"Signal: {signal['ticker']} {signal['action']} {signal['side']} "
                        f"confidence={conf:.0f} - {signal.get('reason', '')}"
                    )
                    if self.dry_run:
                        logger.info(f"[DRY RUN] Would execute: {signal['ticker']} (conf={conf:.0f})")
                    else:
                        result = strategy.execute(signal, dry_run=self.dry_run)
                        if result:
                            executed += 1

        if total_signals == 0:
            logger.info("No trading opportunities found this cycle")
        else:
            logger.info(f"Cycle complete: {total_signals} signals, {executed} executed")

        self._log_status(balance)

    def _log_near_misses(self, markets):
        """Log top 5 closest-to-tradeable opportunities for debugging."""
        candidates = []
        for m in markets:
            if m.get('status') != 'open':
                continue
            ticker = m.get('ticker', '?')
            yes_bid = m.get('yes_bid', 0)
            no_bid = m.get('no_bid', 0)
            volume = m.get('volume', 0)

            # Value bet proximity: how close to 90-97c range
            for side, price in [('YES', yes_bid), ('NO', no_bid)]:
                if 80 <= price <= 99 and price > 0:
                    dist = 0 if 90 <= price <= 97 else min(abs(price - 90), abs(price - 97))
                    candidates.append({
                        'ticker': ticker, 'side': side, 'price': price,
                        'volume': volume, 'distance': dist,
                        'reason': f'{side}={price}c vol={volume} (value bet dist={dist})',
                    })

        # Sort by distance to sweet spot, then volume
        candidates.sort(key=lambda c: (c['distance'], -c['volume']))
        top = candidates[:5]
        if top:
            logger.info("--- Top 5 near-miss opportunities ---")
            for c in top:
                logger.info(f"  {c['ticker']}: {c['reason']}")
        else:
            logger.info("No near-miss candidates found (no markets in 80-99c range)")

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
        logger.info("🚀 Bot is now running. Press Ctrl+C to stop.")
        
        try:
            while True:
                try:
                    self.run_cycle()
                except Exception as e:
                    logger.error(f"Error in trading cycle: {e}", exc_info=True)
                
                # Wait before next cycle
                logger.info(f"Waiting {Config.CHECK_INTERVAL_SECONDS}s until next cycle...")
                time.sleep(Config.CHECK_INTERVAL_SECONDS)
                
        except KeyboardInterrupt:
            logger.info("\n👋 Bot stopped by user")
            self._shutdown()
    
    def _shutdown(self):
        """Clean shutdown."""
        logger.info("Shutting down...")
        self.risk_manager.log_status()
        logger.info("Final balance check...")
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

    # Override config if demo mode
    if args.demo:
        Config.KALSHI_API_HOST = 'https://demo-api.kalshi.co'
        logger.info("Demo mode enabled - using demo API")

    # Create and run bot
    bot = KalshiBot(dry_run=args.dry_run or args.demo)
    bot.run()


if __name__ == '__main__':
    main()