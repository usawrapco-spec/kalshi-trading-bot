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
            self.strategies.append(ArbitrageStrategy(self.client, self.risk_manager, self.db, min_edge=0.02))
            logger.info("✅ Arbitrage strategy enabled")

        if Config.ENABLE_MOMENTUM:
            self.strategies.append(MomentumStrategy(self.client, self.risk_manager, self.db, price_change_threshold=0.05))
            logger.info("✅ Momentum strategy enabled")
        
        if not self.strategies:
            logger.warning("⚠️  No strategies enabled!")
    
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
        logger.info("🔄 Starting trading cycle...")
        
        # Check if we can still trade today
        if not self.risk_manager.check_daily_loss_limit():
            logger.warning("Daily loss limit reached - pausing trading")
            return
        
        # Get open markets
        logger.info("Fetching markets...")
        markets_data = self.client.get_markets(status='open', limit=100)
        markets = markets_data.get('markets', [])
        
        if not markets:
            logger.warning("No open markets found")
            return
        
        logger.info(f"Analyzing {len(markets)} markets...")
        
        # Run each strategy
        total_signals = 0
        for strategy in self.strategies:
            logger.info(f"Running {strategy.name}...")
            signals = strategy.analyze(markets)
            
            if signals:
                logger.info(f"Found {len(signals)} signals from {strategy.name}")
                total_signals += len(signals)
                
                # Execute signals
                for signal in signals:
                    if self.dry_run:
                        logger.info(f"[DRY RUN] Would execute: {signal}")
                    else:
                        strategy.execute(signal, dry_run=self.dry_run)
        
        if total_signals == 0:
            logger.info("No trading opportunities found this cycle")
        
        # Log risk status
        self.risk_manager.log_status()
        
        # Log bot status to Supabase
        balance_data = self.client.get_balance()
        balance = balance_data.get('balance', 0) / 100 if balance_data else 0
        
        status = self.risk_manager.get_status()
        self.db.log_bot_status({
            'is_running': True,
            'daily_pnl': status['daily_pnl'],
            'trades_today': status['trades_today'],
            'balance': balance,
            'active_positions': len(status['positions'])
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
        logger.info("🧪 Demo mode enabled - using demo API")
    
    # Create and run bot
    bot = KalshiBot(dry_run=args.dry_run or args.demo)
    bot.run()


if __name__ == '__main__':
    main()