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

import os
import sys
import time
import argparse
import concurrent.futures
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
from strategies.market_making import MarketMakingStrategy
from dashboard import start_dashboard
from utils.market_helpers import get_yes_price as get_yes_price_dollars, get_volume
from utils.ai_debate import run_debate
from utils.live_validator import is_live_strategy, validate_live_trade
from self_improver import SelfImprover

logger = setup_logger('main')


class KalshiBot:
    """Main trading bot orchestrator with paper trading."""

    def __init__(self, dry_run=True):
        self.dry_run = dry_run

        # OPERATING MODE DETECTION
        self.operating_mode = os.environ.get('OPERATING_MODE', 'live_paper').lower()
        mode_names = {
            'data_collection': 'DATA COLLECTION MODE (Learning)',
            'live_paper': 'LIVE PAPER TRADING MODE',
            'real': 'REAL MONEY TRADING MODE'
        }
        mode_name = mode_names.get(self.operating_mode, f'UNKNOWN MODE: {self.operating_mode}')

        logger.info("=" * 60)
        logger.info(f"KALSHI TRADING BOT - {mode_name}")
        logger.info("=" * 60)

        # Mode-specific settings
        if self.operating_mode == 'data_collection':
            logger.info("🎯 MAXIMUM DATA COLLECTION: Evaluating ALL markets, logging ALL signals")
            logger.info("💰 Virtual trading with $1 positions - NO real money at risk")
            logger.info("📊 Goal: Generate maximum signal volume for self-improvement analysis")
        elif self.operating_mode == 'live_paper':
            logger.info("📈 LIVE PAPER TRADING: Risk-managed paper trading with learned parameters")
        elif self.operating_mode == 'real':
            logger.info("💰 REAL MONEY TRADING: Production mode with tight risk controls")
        else:
            logger.warning(f"⚠️  Unknown operating mode: {self.operating_mode}. Defaulting to live_paper")

        try:
            Config.validate()
        except ValueError as e:
            logger.error(f"Config error: {e}")
            sys.exit(1)

        self.client = KalshiAPIClient()
        self.risk = RiskManager()
        self.db = SupabaseDB()

        # Live trading state
        self.open_live_positions = []  # list of {ticker, side, count, cost, strategy}
        self.real_balance_cents = 0
        if Config.ENABLE_TRADING and Config.LIVE_STRATEGIES:
            logger.info(f"LIVE STRATEGIES: {Config.LIVE_STRATEGIES}")
            self._refresh_real_balance()
        else:
            logger.info("ALL PAPER MODE — no live strategies configured")

        # Fresh start: clear old paper trades from Supabase (unless in data_collection mode)
        if self.operating_mode != 'data_collection':
            self._clear_old_trades()

        self.strategies = []
        self._init_strategies()

        self._check_balance()
        logger.info(f"Paper balance: ${self.risk.paper_balance:.2f}")
        if self.real_balance_cents:
            logger.info(f"Real balance: ${self.real_balance_cents / 100:.2f}")
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
        if Config.ENABLE_MARKET_MAKING:
            self.strategies.append(MarketMakingStrategy(self.client, self.risk, self.db))
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

    def _refresh_real_balance(self):
        """Fetch real Kalshi balance (cents)."""
        try:
            bal = self.client.get_balance()
            if bal:
                self.real_balance_cents = bal.get('balance', 0)
                logger.info(f"Real Kalshi balance: ${self.real_balance_cents / 100:.2f}")
        except Exception as e:
            logger.error(f"Failed to fetch real balance: {e}")

    def _place_live_order(self, sig, price_for_side, strategy_name):
        """Attempt to place a real order on Kalshi. Returns (success, order_id_or_reason)."""
        ticker = sig['ticker']
        side = sig['side']
        count = sig['count']
        price_cents = int(price_for_side * 100)

        logger.info(
            f"LIVE ORDER ATTEMPT: {ticker} {side.upper()} x{count} "
            f"@ ${price_for_side:.2f} [{strategy_name}]"
        )

        # Validate against live limits
        signal_data = {
            'entry_price': price_for_side,
            'count': count,
            'edge': sig.get('edge', 0),
            'confidence': sig.get('confidence', 0),
            'side': side,
        }
        approved, reason = validate_live_trade(
            signal_data, self.real_balance_cents, self.open_live_positions
        )
        logger.info(f"LIVE VALIDATION: {'APPROVED' if approved else 'REJECTED'} - {reason}")

        if not approved:
            return False, reason

        # Place real limit order — prices in CENTS
        try:
            order_params = {
                'ticker': ticker,
                'action': 'buy',
                'side': side,
                'count': count,
                'order_type': 'limit',
            }
            if side == 'yes':
                order_params['yes_price'] = price_cents
            else:
                order_params['no_price'] = price_cents

            result = self.client.create_order(**order_params)

            if result and result.get('order', {}).get('order_id'):
                order_id = result['order']['order_id']
                cost = count * price_for_side
                self.open_live_positions.append({
                    'ticker': ticker, 'side': side, 'count': count,
                    'cost': cost, 'strategy': strategy_name, 'order_id': order_id,
                })
                # Refresh balance after trade
                self._refresh_real_balance()
                logger.info(
                    f"LIVE ORDER PLACED: order_id={order_id} "
                    f"{ticker} {side.upper()} x{count} @ ${price_for_side:.2f}"
                )
                return True, order_id
            else:
                err = result.get('error', str(result)) if result else 'No response'
                logger.error(f"LIVE ORDER FAILED: {err}")
                return False, err
        except Exception as e:
            logger.error(f"LIVE ORDER FAILED: {e}")
            return False, str(e)

    def run_cycle(self):
        logger.info("=" * 40)
        logger.info(f"Cycle at {datetime.now().isoformat()}")

        if not self.risk.check_daily_loss_limit():
            logger.warning("Daily loss stop - halting")
            self._log_status()
            return

        if not self.risk.check_circuit_breaker():
            logger.warning("Circuit breaker tripped - halting")
            self._log_status()
            return

        # OPERATING MODE: Different market fetching strategies
        if self.operating_mode == 'data_collection':
            # DATA COLLECTION MODE: Fetch ALL markets for maximum signal volume
            logger.info("🎯 DATA COLLECTION: Fetching ALL markets for maximum signal volume...")
            seen_tickers = set()
            all_markets = []
            total_added = 0
            total_skipped_resolved = 0

            def _add_all_markets(batch, source):
                nonlocal total_added, total_skipped_resolved
                if not batch:
                    return
                added = 0
                skipped_resolved = 0
                skipped_dupes = 0
                for m in batch:
                    ticker = m.get('ticker')
                    if not ticker:
                        continue
                    if ticker in seen_tickers:
                        skipped_dupes += 1
                        continue
                    if ticker.startswith('KXMVE'):
                        continue  # Skip multivariate parlays
                    # In data collection mode, we want to evaluate markets that will resolve
                    # So we include recently resolved ones for settlement tracking
                    yes_p = get_yes_price_dollars(m)
                    m['status'] = 'open' if not m.get('result') else 'resolved'
                    all_markets.append(m)
                    seen_tickers.add(ticker)
                    added += 1

                total_added += added
                total_skipped_resolved += skipped_resolved

                msg = f"  +{added} from {source}"
                if skipped_dupes:
                    msg += f" ({skipped_dupes} duplicates)"
                logger.info(msg)

            # Fetch ALL markets with pagination
            try:
                # Get all events first
                events = self.client.get_events(limit=500)  # Higher limit for data collection
                event_markets = []
                for evt in events:
                    for m in (evt.get('markets') or []):
                        event_markets.append(m)
                _add_all_markets(event_markets, f"events ({len(event_markets)} markets)")
            except Exception as e:
                logger.error(f"Events fetch failed: {e}")

            # Paginate through all markets
            cursor = None
            page_count = 0
            while page_count < 10:  # Limit to 10 pages to avoid infinite loops
                try:
                    data = self.client.get_markets(limit=100, cursor=cursor)
                    markets_batch = data.get('markets', [])
                    if not markets_batch:
                        break
                    _add_all_markets(markets_batch, f"markets page {page_count + 1}")
                    cursor = data.get('cursor')
                    page_count += 1
                    if not cursor:
                        break
                except Exception as e:
                    logger.error(f"Markets page {page_count + 1} fetch failed: {e}")
                    break

            markets = all_markets
            logger.info(f"📊 DATA COLLECTION: Evaluating {len(markets)} total markets")

        else:
            # LIVE PAPER/REAL MODE: Use market trimming for focused trading
            logger.info("Fetching markets with volume-based trimming...")
            seen_tickers = set()
            all_markets = []
            total_added = 0
            total_skipped_resolved = 0

            def _add_markets(batch, source):
                nonlocal total_added, total_skipped_resolved
                if not batch:
                    return
                added = 0
                skipped_resolved = 0
                skipped_dupes = 0
                for m in batch:
                    ticker = m.get('ticker')
                    if not ticker:
                        continue
                    if ticker in seen_tickers:
                        skipped_dupes += 1
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
                    all_markets.append(m)
                    seen_tickers.add(ticker)
                    added += 1

                total_added += added
                total_skipped_resolved += skipped_resolved

                msg = f"  +{added} from {source}"
                if skipped_resolved:
                    msg += f" ({skipped_resolved} resolved)"
                if skipped_dupes:
                    msg += f" ({skipped_dupes} duplicates)"
                if added or skipped_resolved or skipped_dupes:
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

            if not all_markets:
                logger.warning("No markets returned")
                self._log_status()
                return

            # MARKET TRIMMING: Take top 50 most liquid markets only
            # Sort by volume descending, then take top 50 for focused trading
            all_markets.sort(key=lambda m: get_volume(m), reverse=True)
            markets = all_markets  # Top 200 most liquid markets

            logger.info(f"Market trimming: {len(all_markets)} total -> {len(markets)} top volume markets")

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

        # Phase 1: Collect signals from ALL strategies (PARALLEL EXECUTION)
        all_signals = []

        def run_strategy(strategy, markets):
            """Run a single strategy and return its signals."""
            try:
                signals = strategy.analyze(markets)
                return strategy.name, signals or []
            except Exception as e:
                logger.error(f"{strategy.name} crashed: {e}", exc_info=True)
                return strategy.name, []

        logger.info(f"🚀 Running {len(self.strategies)} strategies in parallel...")
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(run_strategy, s, markets): s for s in self.strategies}
            for future in concurrent.futures.as_completed(futures):
                name, signals = future.result()
                if signals:
                    logger.info(f"{name}: {len(signals)} signals")
                    all_signals.extend(signals)
                else:
                    logger.info(f"{name}: 0 signals")

        # Phase 2: MODE-SPECIFIC SIGNAL PROCESSING
        if self.operating_mode == 'data_collection':
            # DATA COLLECTION MODE: Log ALL signals, virtual trading only
            logger.info("🎯 DATA COLLECTION: Processing all signals for learning...")

            # SPEED OPTIMIZATION: Batch Supabase writes instead of individual inserts
            batch_signals = []
            signals_logged = 0
            virtual_trades = 0

            for sig in all_signals:
                try:
                    # Get market data
                    market = next((m for m in markets if m.get('ticker') == sig['ticker']), {})
                    yes_price = get_yes_price_dollars(market) or 0.50
                    no_price = 1.0 - yes_price
                    price_for_side = yes_price if sig['side'] == 'yes' else no_price

                    # Calculate potential trade size (virtual)
                    edge = sig.get('edge', 0)
                    prob = sig.get('model_prob', 0.5)
                    virtual_size = 1.00  # $1 virtual position for all trades

                    # Calculate reward-to-risk
                    potential_profit = 1.0 - price_for_side if sig['side'] == 'yes' else price_for_side
                    potential_loss = price_for_side if sig['side'] == 'yes' else 1.0 - price_for_side
                    reward_to_risk = potential_profit / max(potential_loss, 0.01)

                    # Determine action
                    passes_asymmetric = reward_to_risk >= 2.0
                    action = 'VIRTUAL_TRADE' if passes_asymmetric else 'SKIP'
                    skip_reason = 'Fails asymmetric reward check' if not passes_asymmetric else None

                    # Prepare signal data for batch insert
                    signal_data = {
                        'cycle_id': f"cycle_{int(time.time())}",
                        'strategy': sig.get('strategy_type', 'unknown'),
                        'ticker': sig['ticker'],
                        'market_title': sig.get('title', ''),
                        'event_ticker': market.get('event_ticker', ''),
                        'side': sig['side'],
                        'yes_price': yes_price,
                        'no_price': no_price,
                        'spread': abs(yes_price - no_price),
                        'volume_24h': get_volume(market),
                        'time_to_close_hours': None,  # Could calculate from close_date
                        'our_probability': prob,
                        'market_probability': price_for_side,
                        'edge': edge,
                        'confidence': sig.get('confidence', 0),
                        'action': action,
                        'skip_reason': skip_reason,
                        'virtual_trade_size': virtual_size if action == 'VIRTUAL_TRADE' else None,
                        'virtual_entry_price': price_for_side if action == 'VIRTUAL_TRADE' else None,
                        'potential_profit': potential_profit,
                        'potential_loss': potential_loss,
                        'reward_to_risk': reward_to_risk,
                        'kelly_fraction': 0.1,  # Default
                    }

                    # Add AI opinions if available
                    if hasattr(sig, 'grok_probability'):
                        signal_data.update({
                            'grok_probability': sig.get('grok_probability'),
                            'grok_recommendation': sig.get('grok_recommendation'),
                            'claude_probability': sig.get('claude_probability'),
                            'claude_recommendation': sig.get('claude_recommendation'),
                            'debate_agreement': sig.get('debate_agreement', False),
                        })

                    batch_signals.append(signal_data)
                    signals_logged += 1

                    if action == 'VIRTUAL_TRADE':
                        virtual_trades += 1
                        logger.info(
                            f"📊 VIRTUAL TRADE: {sig['ticker']} {sig['side'].upper()} "
                            f"edge={edge:+.2f} conf={sig.get('confidence', 0):.0f} "
                            f"R:R={reward_to_risk:.1f} [{sig.get('strategy_type', '?')}]"
                        )
                    else:
                        logger.debug(
                            f"📊 SKIP: {sig['ticker']} {skip_reason} "
                            f"edge={edge:+.2f} R:R={reward_to_risk:.1f}"
                        )

                except Exception as e:
                    logger.error(f"Error processing signal for {sig.get('ticker', 'unknown')}: {e}")
                    continue

            # BATCH INSERT: Insert all signals at once for speed
            if batch_signals and self.db:
                try:
                    self.db.client.table('signal_evaluations').insert(batch_signals).execute()
                    logger.info(f"🚀 BATCH INSERT: {len(batch_signals)} signal evaluations logged in one operation")
                except Exception as e:
                    logger.error(f"Batch insert failed, falling back to individual inserts: {e}")
                    # Fallback to individual inserts
                    for item in batch_signals:
                        try:
                            self.db.client.table('signal_evaluations').insert(item).execute()
                        except:
                            pass

            logger.info(f"📊 DATA COLLECTION: Processed {signals_logged} signals, {virtual_trades} virtual trades")

        else:
            # LIVE PAPER/REAL MODE: Normal trading with risk management
            logger.info("📈 LIVE TRADING: Processing signals with risk management...")

            # LEARNING ALLOCATION SYSTEM
            LEARNING_ALLOCATION = 0.15  # 15% of balance for learning
            PRODUCTION_ALLOCATION = 0.85  # 85% for production

            learning_budget = self.risk.paper_balance * LEARNING_ALLOCATION
            production_budget = self.risk.paper_balance * PRODUCTION_ALLOCATION

            MAX_TRADES = int(os.environ.get("MAX_TRADES_PER_CYCLE", 50))
            MAX_CYCLE_SPEND = min(10.00, production_budget)  # Cap at production budget
            all_signals.sort(key=lambda s: s.get('confidence', 0), reverse=True)
            logger.info(f"Total signals across all strategies: {len(all_signals)}")
            logger.info(f"Learning budget: ${learning_budget:.2f}, Production budget: ${production_budget:.2f}")

            trades_placed = 0
            cycle_spent = 0.0
            learning_spent = 0.0
            production_spent = 0.0
            debates_used = 0
            MAX_DEBATES = 4  # Max 4 AI calls per cycle (2 per trade x 2 trades)

            for sig in all_signals:
                if trades_placed >= MAX_TRADES:
                    break

                # Determine if this is a learning trade or production trade
                is_learning_trade = (learning_spent < learning_budget and
                                   sig.get('confidence', 0) < 70)  # Lower confidence = learning

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

                # Check budget constraints
                if is_learning_trade:
                    if learning_spent + cost > learning_budget:
                        remaining = learning_budget - learning_spent
                        if remaining < price_for_side:
                            continue
                        sig['count'] = max(1, int(remaining / price_for_side))
                        cost = sig['count'] * price_for_side
                else:
                    if production_spent + cost > production_budget:
                        remaining = production_budget - production_spent
                        if remaining < price_for_side:
                            continue
                        sig['count'] = max(1, int(remaining / price_for_side))
                        cost = sig['count'] * price_for_side

                # Asymmetric reward filter (Soros Rule)
                if not self.risk.passes_asymmetric_check(price_for_side, sig['side'], sig.get('confidence', 0)):
                    logger.info(f"  SKIP {sig['ticker']}: fails asymmetric reward check (not 2:1 reward-to-risk)")
                    continue

                trade_type = "LEARNING" if is_learning_trade else "PRODUCTION"
                conf = sig.get('confidence', 0)
                logger.info(
                    f"{trade_type} PICK: {sig['ticker']} BUY {sig['side'].upper()} x{sig['count']} "
                    f"conf={conf:.0f} edge={edge:+.2f} cost=${cost:.2f} "
                    f"[{sig.get('strategy_type', '?')}]"
                )

                # SPEED OPTIMIZATION: Skip AI debate for speed-sensitive strategies
                # Only run debate for AI opinion strategies (GrokNews, MentionMarkets)
                strategy_name = sig.get('strategy_type', 'unknown')
                DEBATE_STRATEGIES = {'GrokNewsAnalysis', 'grok_news', 'MentionMarkets', 'mention_markets'}

                needs_debate = any(ds.lower() in strategy_name.lower() for ds in DEBATE_STRATEGIES)

                if needs_debate and not is_learning_trade and debates_used < MAX_DEBATES:
                    should_trade, sig, debate_log = run_debate(sig, price)
                    debates_used += 2  # Each debate uses 2 API calls
                    if not should_trade:
                        logger.info(f"  Trade vetoed by debate: {debate_log}")
                        continue
                elif needs_debate and is_learning_trade:
                    logger.info(f"  Learning trade - skipping debate to save API costs")
                elif needs_debate:
                    logger.info(f"  Debate limit reached, trading without debate")
                else:
                    logger.info(f"  Data-driven strategy ({strategy_name}) - skipping debate for speed")

                # Mark strategy for learning vs production
                strategy_name = sig.get('strategy_type', 'unknown')
                if is_learning_trade:
                    strategy_name = f"{strategy_name}_LEARNING"

                # --- LIVE vs PAPER routing ---
                raw_strategy = sig.get('strategy_type', 'unknown')
                go_live = is_live_strategy(raw_strategy)
                order_id_or_reason = None

                if go_live:
                    # REAL ORDER PATH
                    self._refresh_real_balance()
                    success, order_id_or_reason = self._place_live_order(
                        sig, price_for_side, strategy_name,
                    )
                    if not success:
                        logger.info(f"  Live order rejected, falling back to paper: {order_id_or_reason}")
                        go_live = False  # fall through to paper

                if not go_live:
                    # PAPER ORDER PATH (default, or live fallback)
                    traded = self.risk.record_paper_trade(
                        ticker=sig['ticker'],
                        side=sig['side'],
                        count=sig['count'],
                        entry_price=price_for_side,
                        strategy=strategy_name,
                        title=sig.get('title', ''),
                    )
                    if not traded:
                        continue

                trades_placed += 1
                cycle_spent += cost

                if is_learning_trade:
                    learning_spent += cost
                else:
                    production_spent += cost

                if self.db:
                    reason = sig.get('reason', '')
                    if go_live:
                        reason = f"[LIVE] {reason}"
                    elif not is_learning_trade and debates_used > 0:
                        reason = f"[DEBATED] {reason}"
                    elif is_learning_trade:
                        reason = f"[LEARNING] {reason}"

                    self.db.log_trade({
                        'ticker': sig['ticker'],
                        'action': 'buy',
                        'side': sig['side'],
                        'count': sig['count'],
                        'strategy': strategy_name,
                        'reason': reason,
                        'confidence': sig.get('confidence', conf),
                        'order_id': order_id_or_reason if go_live else 'paper',
                        'price': price_for_side,
                        'is_live': go_live,
                    })

            # Log ALL signal evaluations (both traded and skipped) for learning
            if self.db:
                for sig in all_signals:
                    try:
                        was_traded = any(
                            sig.get('ticker') == t.get('ticker')
                            for t in [s for s in all_signals[:trades_placed]]
                        )
                        self.db.client.table('signal_evaluations').insert({
                            'strategy': sig.get('strategy_type', 'unknown'),
                            'ticker': sig.get('ticker', ''),
                            'side': sig.get('side', ''),
                            'edge': sig.get('edge', 0),
                            'confidence': sig.get('confidence', 0),
                            'action': 'TRADE' if was_traded else 'SKIP',
                        }).execute()
                    except Exception:
                        pass  # Never crash the bot over logging

            logger.info(f"Cycle done: {trades_placed} trades, ${cycle_spent:.2f} spent")

        # Forced paper trade if nothing fired
        if len(all_signals) == 0:
            logger.info("No signals from any strategy - forcing paper trade")
            self._forced_paper_trade(markets)

        # Check for settled paper trades and calculate P&L
        self._check_settlements(markets)

        # In data collection mode, also check for settled virtual trades
        if self.operating_mode == 'data_collection':
            self._check_virtual_settlements()

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

    def _check_virtual_settlements(self):
        """Check if any virtual trades from signal_evaluations have settled."""
        if not self.db:
            return

        try:
            # Get all unsettled virtual trades
            unsettled = self.db.client.table('signal_evaluations') \
                .select('id, ticker, side, virtual_entry_price, virtual_trade_size, potential_loss') \
                .eq('settled', False) \
                .eq('action', 'VIRTUAL_TRADE') \
                .limit(200) \
                .execute()

            if not unsettled.data:
                return

            settled_count = 0
            for signal in unsettled.data:
                try:
                    # Check if market has settled via Kalshi API
                    market_data = self.client.get_market(signal['ticker'])
                    if not market_data or 'market' not in market_data:
                        continue

                    market = market_data['market']
                    result = market.get('result')
                    settlement = market.get('settlement_value_dollars')

                    if result is None and settlement is None:
                        continue

                    # Determine settlement price
                    if result == 'yes' or result is True:
                        settlement_price = 1.0
                    elif result == 'no' or result is False:
                        settlement_price = 0.0
                    elif settlement is not None:
                        settlement_price = float(settlement) if settlement else 0.0
                    else:
                        continue

                    # Calculate virtual P&L
                    entry_price = signal['virtual_entry_price'] or 0
                    trade_size = signal['virtual_trade_size'] or 1.0
                    side = signal['side']

                    if side == 'yes':
                        virtual_pnl = (settlement_price - entry_price) * trade_size
                    else:  # side == 'no'
                        virtual_pnl = (entry_price - settlement_price) * trade_size

                    was_correct = virtual_pnl > 0
                    risk_amount = signal['potential_loss'] or entry_price
                    r_multiple = virtual_pnl / max(risk_amount, 0.01) if risk_amount else 0

                    # Update the signal evaluation record
                    self.db.client.table('signal_evaluations').update({
                        'settled': True,
                        'settlement_price': settlement_price,
                        'virtual_pnl': virtual_pnl,
                        'settled_at': datetime.utcnow().isoformat(),
                        'was_correct': was_correct,
                        'r_multiple': r_multiple
                    }).eq('id', signal['id']).execute()

                    settled_count += 1

                    # Log settlement
                    logger.info(
                        f"📊 VIRTUAL SETTLEMENT: {signal['ticker']} {side.upper()} "
                        f"P&L=${virtual_pnl:+.2f} {'✅' if was_correct else '❌'} "
                        f"R={r_multiple:.1f}"
                    )

                except Exception as e:
                    logger.error(f"Error settling virtual trade {signal['ticker']}: {e}")
                    continue

            if settled_count > 0:
                logger.info(f"📊 Settled {settled_count} virtual trades")

        except Exception as e:
            logger.error(f"Virtual settlement check failed: {e}")

    def _log_status(self):
        self.risk.log_status()
        status = self.risk.get_status()

        # Live status logging
        if Config.ENABLE_TRADING and Config.LIVE_STRATEGIES:
            self._refresh_real_balance()
            live_exposure = sum(p.get('cost', 0) for p in self.open_live_positions)
            logger.info(
                f"LIVE STATUS: Real balance=${self.real_balance_cents / 100:.2f}, "
                f"Open live positions={len(self.open_live_positions)}, "
                f"Live exposure=${live_exposure:.2f}"
            )

        if self.db:
            self.db.log_bot_status({
                'is_running': True,
                'daily_pnl': status['daily_pnl'],
                'trades_today': status['trades_today'],
                'balance': status['paper_balance'],
                'active_positions': len(status['positions']),
                'real_balance': self.real_balance_cents / 100.0 if self.real_balance_cents else None,
                'live_positions': len(self.open_live_positions),
            })
            # Log equity snapshot
            try:
                self.db.client.table('equity_snapshots').insert({
                    'balance': self.risk.paper_balance,
                    'open_positions': len(self.risk.positions),
                }).execute()
            except Exception:
                pass  # Never crash the bot over logging

    def run(self):
        logger.info("Bot running. Ctrl+C to stop.")

        # SELF-IMPROVEMENT AUTO-RUN SETUP
        last_analysis = None
        ANALYSIS_INTERVAL_HOURS = 6

        try:
            while True:
                try:
                    self.run_cycle()

                    # AUTO SELF-IMPROVEMENT: Run analysis every 6 hours
                    if (last_analysis is None or
                        (datetime.utcnow() - last_analysis).total_seconds() > ANALYSIS_INTERVAL_HOURS * 3600):
                        try:
                            logger.info("🤖 Running self-improvement analysis...")
                            improver = SelfImprover(self.db)

                            # Run full analysis
                            results = improver.run_full_analysis(lookback_days=7)

                            # Apply new parameters (only in live_paper mode)
                            if self.operating_mode == 'live_paper':
                                success = improver.apply_parameters(results['new_parameters'])
                                if success:
                                    logger.info("✅ New parameters applied to live trading")
                                else:
                                    logger.warning("⚠️ Failed to apply new parameters")
                            elif self.operating_mode == 'data_collection':
                                logger.info("📊 Data collection mode - parameters logged but not applied")

                            last_analysis = datetime.utcnow()
                            logger.info(f"📈 Self-improvement complete. Next analysis in {ANALYSIS_INTERVAL_HOURS}h.")

                        except Exception as e:
                            logger.error(f"Self-improvement analysis failed: {e}")

                except Exception as e:
                    logger.error(f"Cycle error: {e}", exc_info=True)
                logger.info(f"Next cycle in {Config.CHECK_INTERVAL_SECONDS}s...")
                time.sleep(Config.CHECK_INTERVAL_SECONDS)
        except KeyboardInterrupt:
            logger.info("Bot stopped by user")
            self.risk.log_status()


def main():
    parser = argparse.ArgumentParser(description='Kalshi Trading Bot')
    parser.add_argument('--demo', action='store_true', help='Use demo API')
    parser.add_argument('--dry-run', action='store_true', help='Paper trading mode')
    args = parser.parse_args()

    if args.demo:
        Config.KALSHI_API_HOST = 'https://demo-api.kalshi.co'
        logger.info("Demo mode - using demo API")

    bot = KalshiBot(dry_run=True)  # Always paper trading for now

    # Start Flask dashboard in background daemon thread
    start_dashboard()

    # Run bot trading loop on main thread (keeps process alive)
    bot.run()


if __name__ == '__main__':
    main()
