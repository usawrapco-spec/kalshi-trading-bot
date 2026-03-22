#!/usr/bin/env python3
"""
Kalshi Trading Bot - Paper Trading System

Strategies:
  - WeatherEdge: Open-Meteo GFS ensemble vs KXHIGH/KXLOWT temperature markets (24 cities)
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
from strategies.precip_edge import PrecipEdgeStrategy
from strategies.crypto_momentum import CryptoMomentumStrategy
from utils.hyperthink import HyperThink
from utils.crypto_monitor import CryptoPriceMonitor
from dashboard import start_dashboard
from utils.market_helpers import get_yes_price as get_yes_price_dollars, get_volume
from utils.ai_debate import run_debate
from utils.live_validator import is_live_strategy, validate_live_trade
from utils.signal_tier import rate_signal, size_by_tier, check_portfolio_limits, hours_until_close, TIER_CONFIG, MAX_LIVE_TRADES_PER_CYCLE
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

        # Reconstruct paper state from Supabase (survives deploys)
        self._reconstruct_state_from_db()

        self.strategies = []
        self._swing_cycle_offset = 0  # Rotate through swing positions across cycles
        self._init_strategies()

        self._check_balance()
        logger.info(f"Paper balance: ${self.risk.paper_balance:.2f}")
        if self.real_balance_cents:
            logger.info(f"Real balance: ${self.real_balance_cents / 100:.2f}")
        logger.info("=" * 60)

    def _init_strategies(self):
        logger.info("Loading strategies...")

        # Shared HyperThink consensus engine (Grok + Claude debate)
        self.hyperthink = HyperThink(db=self.db)

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
            self.strategies.append(ProbabilityArbStrategy(self.client, self.risk, self.db, hyperthink=self.hyperthink))
        if Config.ENABLE_SPORTS_NO:
            self.strategies.append(SportsNOStrategy(self.client, self.risk, self.db))
        if Config.ENABLE_NEAR_CERTAINTY:
            self.strategies.append(NearCertaintyStrategy(self.client, self.risk, self.db))
        if Config.ENABLE_WEATHER:
            self.strategies.append(WeatherEdgeStrategy(self.client, self.risk, self.db))
        if Config.ENABLE_PRECIP:
            self.strategies.append(PrecipEdgeStrategy(self.client, self.risk, self.db, hyperthink=self.hyperthink))
        if Config.ENABLE_CRYPTO:
            self.crypto_monitor = CryptoPriceMonitor()
            self.strategies.append(CryptoMomentumStrategy(
                self.client, self.risk, self.db,
                crypto_monitor=self.crypto_monitor, hyperthink=self.hyperthink,
            ))
        else:
            self.crypto_monitor = None
        logger.info(f"{len(self.strategies)} strategies loaded (HyperThink enabled)")

    def _reconstruct_state_from_db(self):
        """Reconstruct paper + live state from Supabase so deploys don't reset state."""
        if not self.db or not self.db.client:
            return
        try:
            # Paper trades
            paper_result = self.db.client.table('kalshi_trades').select('*').eq('order_id', 'paper').execute()
            paper_trades = paper_result.data or []
            total_paper_cost = sum(t.get('price', 0) * t.get('count', 0) for t in paper_trades)
            reconstructed_balance = 100.0 - total_paper_cost
            if paper_trades:
                self.risk.paper_balance = reconstructed_balance
                logger.info(f"Reconstructed paper balance from {len(paper_trades)} trades: ${reconstructed_balance:.2f}")

            # Live trades
            live_result = self.db.client.table('kalshi_trades').select('*').neq('order_id', 'paper').execute()
            live_trades = live_result.data or []
            self.open_live_positions = [
                {'ticker': t['ticker'], 'side': t.get('side', 'yes'),
                 'count': t.get('count', 1), 'cost': t.get('price', 0) * t.get('count', 1),
                 'strategy': t.get('strategy', 'unknown')}
                for t in live_trades
                if not any(k in (t.get('reason') or '').upper() for k in ('WIN', 'LOSS', 'SETTLED'))
            ]
            if live_trades:
                logger.info(f"Reconstructed {len(self.open_live_positions)} open live positions from {len(live_trades)} live trades")
        except Exception as e:
            logger.error(f"Failed to reconstruct state from DB: {e}")

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

    def _has_open_live_position(self, ticker, side):
        """Check if we already have an open live position on this ticker+side."""
        if not self.db:
            return False
        try:
            result = self.db.client.table('kalshi_trades').select('*').eq('ticker', ticker).neq('order_id', 'paper').execute()
            return len(result.data) > 0
        except Exception as e:
            logger.error(f"Failed to check open live positions: {e}")
            return False

    def _place_live_order(self, sig, price_for_side, strategy_name):
        """Attempt to place a real order on Kalshi. Returns (success, order_id_or_reason)."""
        ticker = sig['ticker']
        side = sig['side']
        count = sig['count']
        price_cents = int(price_for_side * 100)

        # CHECK FOR DUPLICATE POSITIONS - PREVENT REAL MONEY BLEEDING
        if self._has_open_live_position(ticker, side):
            logger.warning(f"SKIP LIVE DUPLICATE: Already have open live position on {ticker} {side.upper()}")
            return False, "duplicate_position"

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

    def _take_portfolio_snapshot(self):
        """Record current portfolio state. Throttled to every 5 minutes."""
        import time as _time
        now = _time.time()
        if not hasattr(self, '_last_snapshot_time'):
            self._last_snapshot_time = 0
        if now - self._last_snapshot_time < 300:
            return
        self._last_snapshot_time = now

        if not self.db or not self.db.client:
            return
        try:
            from utils.market_helpers import get_yes_price, get_no_price

            # 1. Get real Kalshi cash balance
            cash = 0.0
            try:
                balance_response = self.client.get_balance()
                cash = (balance_response or {}).get('balance', 0) / 100.0
            except Exception:
                if self.real_balance_cents:
                    cash = self.real_balance_cents / 100.0

            # 2. Get open live trades and fetch market prices
            live_open = self.db.client.table('kalshi_trades').select('*').neq('order_id', 'paper').eq('resolved', False).execute()
            live_trades = live_open.data or []

            cost_basis = 0.0
            market_value = 0.0
            checked = 0

            for trade in live_trades:
                trade_cost = (trade.get('price', 0) or 0) * (trade.get('count', 0) or 0)
                cost_basis += trade_cost
                if checked < 20:
                    try:
                        checked += 1
                        market_data = self.client.get_market(trade.get('ticker', ''))
                        if not market_data:
                            market_value += trade_cost
                            continue
                        market = market_data.get('market', market_data)
                        if trade.get('side') == 'yes':
                            current = get_yes_price(market)
                        else:
                            current = get_no_price(market)
                        if current > 0:
                            market_value += current * (trade.get('count', 0) or 0)
                        else:
                            market_value += trade_cost
                    except Exception:
                        market_value += trade_cost
                else:
                    market_value += trade_cost

            unrealized = market_value - cost_basis

            # 3. Realized P&L
            settled = self.db.client.table('kalshi_trades').select('pnl').neq('order_id', 'paper').eq('resolved', True).execute()
            realized = sum((t.get('pnl', 0) or 0) for t in (settled.data or []))

            # 4. Paper stats
            paper_open = self.db.client.table('kalshi_trades').select('price,count').eq('order_id', 'paper').eq('resolved', False).execute()
            paper_cost = sum((t.get('price', 0) or 0) * (t.get('count', 0) or 0) for t in (paper_open.data or []))
            paper_settled = self.db.client.table('kalshi_trades').select('pnl').eq('order_id', 'paper').eq('resolved', True).execute()
            paper_realized = sum((t.get('pnl', 0) or 0) for t in (paper_settled.data or []))

            total = cash + market_value

            # 5. Save snapshot
            self.db.client.table('portfolio_snapshots').insert({
                'kalshi_total': round(total, 2),
                'kalshi_cash': round(cash, 2),
                'kalshi_positions_market_value': round(market_value, 2),
                'positions_cost_basis': round(cost_basis, 2),
                'unrealized_pnl': round(unrealized, 2),
                'realized_pnl': round(realized, 2),
                'open_live_trades': len(live_trades),
                'open_paper_trades': len(paper_open.data or []),
                'paper_balance': round(100.0 - paper_cost + paper_realized, 2),
            }).execute()

            logger.info(
                f"SNAPSHOT: Total=${total:.2f} Cash=${cash:.2f} "
                f"Positions=${market_value:.2f} (cost=${cost_basis:.2f}) "
                f"Unrealized=${unrealized:+.2f}"
            )
        except Exception as e:
            logger.error(f"Snapshot failed: {e}")

    def _check_settlements(self):
        """Check unresolved trades for settlement. Max 20 API calls per cycle."""
        if not self.db or not self.db.client:
            return
        try:
            unresolved = self.db.client.table('kalshi_trades').select('*').eq('resolved', False).execute()
            if not unresolved.data:
                return

            settled_count = 0
            checked = 0
            for trade in unresolved.data:
                if checked >= 20:
                    break
                ticker = trade.get('ticker', '')
                if not ticker:
                    continue

                try:
                    checked += 1
                    market_data = self.client.get_market(ticker)
                    if not market_data:
                        continue
                    market = market_data.get('market', market_data)
                    result = market.get('result', '')
                    if not result or result == '':
                        continue

                    trade_side = trade.get('side', 'yes')
                    won = (trade_side == 'yes' and result == 'yes') or \
                          (trade_side == 'no' and result == 'no')

                    entry_price = trade.get('price', 0)
                    count = trade.get('count', 1)
                    if won:
                        exit_price = 1.00
                        pnl = (1.00 - entry_price) * count
                    else:
                        exit_price = 0.00
                        pnl = -(entry_price * count)

                    self.db.client.table('kalshi_trades').update({
                        'resolved': True,
                        'exit_price': exit_price,
                        'pnl': round(pnl, 4),
                        'resolved_at': datetime.utcnow().isoformat(),
                        'reason': f"{'WIN' if won else 'LOSS'} settled={result} pnl=${pnl:+.2f}",
                    }).eq('id', trade['id']).execute()

                    is_live = trade.get('order_id') not in (None, 'paper', 'forced_paper')
                    trade_type = "LIVE" if is_live else "PAPER"
                    result_emoji = "WIN" if won else "LOSS"
                    logger.info(
                        f"{trade_type} SETTLED: {ticker} {result_emoji} "
                        f"P&L=${pnl:+.2f} ({count}x @ ${entry_price:.2f})"
                    )
                    settled_count += 1
                except Exception as e:
                    logger.debug(f"Settlement check failed for {ticker}: {e}")
                    continue

            if settled_count > 0:
                logger.info(f"Settled {settled_count} trades this cycle (checked {checked})")
        except Exception as e:
            logger.error(f"Settlement check failed: {e}")

    def _evaluate_exits(self):
        """Evaluate open live positions for profit-taking / loss-cutting."""
        if not self.db or not self.db.client:
            return 0.0
        try:
            live_open = self.db.client.table('kalshi_trades').select('*').neq('order_id', 'paper').eq('resolved', False).execute()
            if not live_open.data:
                return 0.0

            exits_taken = 0
            capital_freed = 0.0
            checked = 0

            for trade in live_open.data:
                if checked >= 15:  # Limit API calls
                    break
                ticker = trade.get('ticker', '')
                if not ticker:
                    continue

                try:
                    checked += 1
                    market_data = self.client.get_market(ticker)
                    if not market_data:
                        continue
                    market = market_data.get('market', market_data)

                    # Already settled? Skip — _check_settlements handles that
                    if market.get('result'):
                        continue

                    side = trade.get('side', 'yes')
                    cost_basis = trade.get('price', 0)
                    count = trade.get('count', 1)

                    if side == 'yes':
                        raw_bid = market.get('yes_bid', market.get('yes_bid_dollars', 0))
                    else:
                        raw_bid = market.get('no_bid', market.get('no_bid_dollars', 0))
                    current_bid = float(raw_bid) if raw_bid else 0
                    if current_bid > 1.0:
                        current_bid = current_bid / 100.0

                    if current_bid <= 0.01 or cost_basis <= 0:
                        continue  # No liquidity or bad data

                    unrealized_pct = (current_bid - cost_basis) / cost_basis

                    # --- Get fresh model edge from weather strategy cache ---
                    current_edge = None
                    for strat in self.strategies:
                        if hasattr(strat, '_cache_high'):
                            # Re-evaluate using the strategy's _evaluate method
                            sig = strat._evaluate(market)
                            if sig:
                                current_edge = sig.get('edge', 0)
                            break

                    # --- DECISION MATRIX ---
                    should_sell = False
                    reason = ""

                    # Rule 1: Up 100%+ and edge shrunk below 20%
                    if unrealized_pct >= 1.00 and current_edge is not None and current_edge < 0.20:
                        should_sell = True
                        reason = f"TAKE PROFIT: +{unrealized_pct:.0%}, edge={current_edge:.0%}"

                    # Rule 2: Up 50%+ and edge < 10%
                    if unrealized_pct >= 0.50 and current_edge is not None and current_edge < 0.10:
                        should_sell = True
                        reason = f"EDGE GONE: +{unrealized_pct:.0%}, edge={current_edge:.0%}"

                    # Rule 3: Edge reversed (model says we're wrong)
                    if current_edge is not None and current_edge < -0.10:
                        should_sell = True
                        reason = f"EDGE REVERSED: edge={current_edge:.0%}"

                    # Rule 4: Bought cheap (≤15c), now worth ≥50c — lock big gain
                    if current_bid >= 0.50 and cost_basis <= 0.15:
                        should_sell = True
                        reason = f"LOCK BIG GAIN: ${cost_basis:.2f}->${current_bid:.2f} (+{unrealized_pct:.0%})"

                    # Rule 5: HOLD override — model still very confident with big edge
                    if current_edge is not None and current_edge >= 0.30:
                        # Check model prob from the signal
                        for strat in self.strategies:
                            if hasattr(strat, '_cache_high'):
                                sig = strat._evaluate(market)
                                if sig and sig.get('model_prob', 0) >= 0.90:
                                    should_sell = False
                                    reason = f"HOLD: model={sig['model_prob']:.0%}, edge={current_edge:.0%}"
                                break

                    if should_sell:
                        logger.info(f"EXIT: {ticker} — {reason}")
                        logger.info(f"  Cost: ${cost_basis:.2f} -> Bid: ${current_bid:.2f} ({unrealized_pct:+.0%})")

                        price_cents = int(current_bid * 100)
                        sell_params = {
                            'ticker': ticker, 'action': 'sell', 'side': side,
                            'count': count, 'order_type': 'limit',
                        }
                        if side == 'yes':
                            sell_params['yes_price'] = price_cents
                        else:
                            sell_params['no_price'] = price_cents

                        result = self.client.create_order(**sell_params)
                        if result and result.get('order_id'):
                            pnl = (current_bid - cost_basis) * count
                            self.db.client.table('kalshi_trades').update({
                                'resolved': True,
                                'exit_price': current_bid,
                                'pnl': round(pnl, 4),
                                'resolved_at': datetime.utcnow().isoformat(),
                                'reason': f"[EXIT] {reason}",
                            }).eq('id', trade['id']).execute()

                            exits_taken += 1
                            capital_freed += current_bid * count
                            logger.info(f"SOLD: {ticker} P&L=${pnl:+.2f} (freed ${current_bid * count:.2f})")
                        else:
                            logger.warning(f"Sell order failed for {ticker}")
                    else:
                        logger.debug(f"HOLD: {ticker} cost=${cost_basis:.2f} bid=${current_bid:.2f} edge={current_edge}")

                except Exception as e:
                    logger.debug(f"Exit eval failed for {ticker}: {e}")
                    continue

            if exits_taken > 0:
                logger.info(f"EXITS: Sold {exits_taken} positions, freed ${capital_freed:.2f}")
            return capital_freed
        except Exception as e:
            logger.error(f"Exit evaluation failed: {e}")
            return 0.0

    def _monitor_swings(self):
        """Monitor non-weather paper positions for swing trade exits (paper only)."""
        if not self.db or not self.db.client:
            return
        try:
            open_trades = self.db.client.table('kalshi_trades').select('*').eq('order_id', 'paper').eq('resolved', False).execute()
            if not open_trades.data:
                return

            non_weather = [t for t in open_trades.data
                          if t.get('strategy') != 'weather_edge'
                          and not (t.get('ticker') or '').startswith('KXHIGH')
                          and not (t.get('ticker') or '').startswith('KXLOWT')]

            if not non_weather:
                return

            # Rotate through positions: check 15 per cycle (was 10)
            batch_size = 15
            start = self._swing_cycle_offset % max(len(non_weather), 1)
            batch = non_weather[start:start + batch_size]
            if len(batch) < batch_size:
                batch += non_weather[:batch_size - len(batch)]
            self._swing_cycle_offset += batch_size

            from utils.market_helpers import get_yes_price, get_no_price

            summary = {"sells": 0, "holds": 0, "cuts": 0, "pnl": 0.0}

            for trade in batch:
                ticker = trade.get('ticker', '')
                try:
                    market_data = self.client.get_market(ticker)
                    if not market_data:
                        continue
                    market = market_data.get('market', market_data) if market_data else {}

                    if trade.get('side') == 'yes':
                        current_price = get_yes_price(market)
                    else:
                        current_price = get_no_price(market)

                    entry = trade.get('price', 0)
                    count = trade.get('count', 1)
                    if entry <= 0 or current_price <= 0:
                        continue

                    pct_change = (current_price - entry) / entry
                    dollar_change = (current_price - entry) * count

                    action = "HOLD"
                    reason = ""
                    trade_strategy = trade.get('strategy', '')

                    # CRYPTO SCALPS: Super tight exits (5% take profit, 3% stop loss)
                    if 'crypto' in trade_strategy.lower() or 'market_making_scalp' in trade_strategy.lower():
                        if pct_change >= 0.05:
                            action, reason = "SELL", f"CRYPTO TP: +{pct_change:.0%} (5% target hit)"
                        elif pct_change <= -0.03:
                            action, reason = "CUT", f"CRYPTO SL: {pct_change:.0%} (3% stop hit)"
                    # Tiered profit/loss thresholds by contract price
                    elif entry >= 0.70:
                        if pct_change >= 0.07:
                            action, reason = "SELL", f"TAKE PROFIT: +{pct_change:.0%} on ${entry:.2f} contract"
                        elif pct_change <= -0.05:
                            action, reason = "CUT", f"STOP LOSS: {pct_change:.0%} on ${entry:.2f} contract"
                    elif entry >= 0.50:
                        if pct_change >= 0.10:
                            action, reason = "SELL", f"TAKE PROFIT: +{pct_change:.0%} on ${entry:.2f} contract"
                        elif pct_change <= -0.07:
                            action, reason = "CUT", f"STOP LOSS: {pct_change:.0%} on ${entry:.2f} contract"
                    elif entry >= 0.30:
                        if pct_change >= 0.15:
                            action, reason = "SELL", f"TAKE PROFIT: +{pct_change:.0%} on ${entry:.2f} contract"
                        elif pct_change <= -0.10:
                            action, reason = "CUT", f"STOP LOSS: {pct_change:.0%} on ${entry:.2f} contract"
                    elif entry >= 0.10:
                        if pct_change >= 0.20:
                            action, reason = "SELL", f"TAKE PROFIT: +{pct_change:.0%} on ${entry:.2f} contract"
                        elif pct_change <= -0.25:
                            action, reason = "CUT", f"STOP LOSS: {pct_change:.0%}"
                    else:
                        # < 10c: lottery tickets — only sell at +50%, never cut
                        if pct_change >= 0.50:
                            action, reason = "SELL", f"TAKE PROFIT: +{pct_change:.0%} on cheap contract"

                    if action in ("SELL", "CUT"):
                        pnl = dollar_change
                        self.db.client.table('kalshi_trades').update({
                            'resolved': True,
                            'exit_price': current_price,
                            'pnl': round(pnl, 4),
                            'resolved_at': datetime.utcnow().isoformat(),
                            'reason': f"[SWING {action}] {reason}",
                        }).eq('id', trade['id']).execute()

                        emoji = "\U0001f4c8" if action == "SELL" else "\U0001f4c9"
                        logger.info(f"{emoji} SWING {action}: {ticker} {reason} | Entry=${entry:.2f} Exit=${current_price:.2f} P&L=${pnl:+.2f}")
                        summary["sells" if action == "SELL" else "cuts"] += 1
                        summary["pnl"] += pnl
                    else:
                        logger.debug(f"SWING HOLD: {ticker} entry=${entry:.2f} now=${current_price:.2f} ({pct_change:+.0%})")
                        summary["holds"] += 1

                except Exception as e:
                    logger.debug(f"Swing check failed for {ticker}: {e}")
                    continue

            if summary["sells"] > 0 or summary["cuts"] > 0:
                logger.info(f"\U0001f4ca SWING SUMMARY: {summary['sells']} sells, {summary['cuts']} cuts, {summary['holds']} holds, P&L=${summary['pnl']:+.2f}")

        except Exception as e:
            logger.error(f"Swing monitor failed: {e}")

    def _check_crypto_settlements(self):
        """Check if any crypto paper trades have settled."""
        if not self.db or not self.db.client:
            return
        try:
            unresolved = self.db.client.table('crypto_signals').select('*').eq('resolved', False).execute()
            if not unresolved.data:
                return

            settled_count = 0
            for trade in unresolved.data[:20]:
                ticker = trade.get('ticker', '')
                if not ticker:
                    continue
                try:
                    market_data = self.client.get_market(ticker)
                    if not market_data:
                        continue
                    market = market_data.get('market', market_data)
                    result = market.get('result', '')
                    if not result:
                        continue

                    won = (trade.get('side') == 'yes' and result == 'yes') or \
                          (trade.get('side') == 'no' and result == 'no')
                    entry = trade.get('price', 0)
                    count = trade.get('count', 1)
                    pnl = (1.0 - entry) * count if won else -(entry * count)

                    # Get current crypto price for the record
                    btc_settlement = 0
                    if hasattr(self, 'crypto_monitor') and self.crypto_monitor:
                        prices = self.crypto_monitor.last_prices
                        btc_settlement = prices.get('BTC', 0)

                    self.db.client.table('crypto_signals').update({
                        'resolved': True,
                        'exit_price': 1.0 if won else 0.0,
                        'pnl': round(pnl, 4),
                        'resolved_at': datetime.utcnow().isoformat(),
                        'btc_price_at_settlement': btc_settlement,
                    }).eq('id', trade['id']).execute()

                    # Also update in kalshi_trades
                    try:
                        self.db.client.table('kalshi_trades').update({
                            'resolved': True,
                            'exit_price': 1.0 if won else 0.0,
                            'pnl': round(pnl, 4),
                            'resolved_at': datetime.utcnow().isoformat(),
                        }).eq('ticker', ticker).eq('strategy', 'crypto_momentum').eq('resolved', False).execute()
                    except Exception:
                        pass

                    emoji = "WIN" if won else "LOSS"
                    logger.info(f"CRYPTO SETTLED: {ticker} {emoji} P&L=${pnl:+.2f}")
                    settled_count += 1
                except Exception as e:
                    logger.debug(f"Crypto settlement check failed for {ticker}: {e}")
                    continue

            if settled_count > 0:
                logger.info(f"Crypto: settled {settled_count} trades")
        except Exception as e:
            logger.error(f"Crypto settlement check failed: {e}")

    def _find_intraday_weather_opportunities(self, markets):
        """Find weather markets settling today where price moved significantly."""
        signals = []
        now = datetime.utcnow()

        for m in markets:
            ticker = m.get('ticker', '')
            # Only look at weather markets
            if not any(prefix in ticker.upper() for prefix in ('KXHIGH', 'KXLOWT')):
                continue

            # Only markets closing in the next 12 hours
            close_time_str = m.get('close_time') or m.get('expiration_time') or ''
            if not close_time_str:
                continue
            try:
                close_dt = datetime.fromisoformat(close_time_str.replace('Z', '+00:00')).replace(tzinfo=None)
                hours_left = (close_dt - now).total_seconds() / 3600
            except Exception:
                continue

            if hours_left <= 0 or hours_left > 12:
                continue

            yes_price = get_yes_price_dollars(m)
            if yes_price <= 0.01 or yes_price >= 0.99:
                continue

            # Look for cheap contracts that could pay off
            # Price dropped to cheap levels near settlement = possible overreaction
            if yes_price <= 0.15 and hours_left < 6:
                signals.append({
                    'ticker': ticker,
                    'title': m.get('title', ''),
                    'action': 'buy',
                    'side': 'yes',
                    'count': 3,
                    'confidence': 55,
                    'strategy_type': 'weather_intraday',
                    'edge': 0.05,
                    'model_prob': 0.20,
                    'reason': f"[WEATHER INTRADAY] Cheap YES=${yes_price:.2f} settling in {hours_left:.1f}h",
                })

            # Cheap NO contracts near settlement
            no_price = 1.0 - yes_price
            if no_price <= 0.15 and hours_left < 6:
                signals.append({
                    'ticker': ticker,
                    'title': m.get('title', ''),
                    'action': 'buy',
                    'side': 'no',
                    'count': 3,
                    'confidence': 55,
                    'strategy_type': 'weather_intraday',
                    'edge': 0.05,
                    'model_prob': 0.20,
                    'reason': f"[WEATHER INTRADAY] Cheap NO=${no_price:.2f} settling in {hours_left:.1f}h",
                })

        signals.sort(key=lambda s: s.get('edge', 0), reverse=True)
        return signals[:5]

    def _force_close_stale_crypto(self):
        """Force-close any crypto paper positions older than 20 minutes."""
        if not self.db or not self.db.client:
            return
        try:
            open_crypto = self.db.client.table('kalshi_trades').select('*') \
                .eq('order_id', 'paper').eq('resolved', False) \
                .eq('strategy', 'crypto_momentum').execute()

            if not open_crypto.data:
                return

            now = datetime.utcnow()
            force_closed = 0

            for trade in open_crypto.data:
                ts_str = trade.get('timestamp') or trade.get('created_at') or ''
                if not ts_str:
                    continue
                try:
                    trade_time = datetime.fromisoformat(ts_str.replace('Z', '+00:00')).replace(tzinfo=None)
                    age_minutes = (now - trade_time).total_seconds() / 60
                except Exception:
                    continue

                if age_minutes > 20:
                    # Force close at estimated current price
                    entry = trade.get('price', 0)
                    count = trade.get('count', 1)
                    # Estimate: assume 50/50 outcome for stale positions
                    exit_price = entry  # Flat close assumption
                    pnl = 0.0

                    try:
                        # Try to get actual current price
                        market_data = self.client.get_market(trade.get('ticker', ''))
                        if market_data:
                            market = market_data.get('market', market_data)
                            if trade.get('side') == 'yes':
                                bid = market.get('yes_bid', market.get('yes_bid_dollars', 0))
                            else:
                                bid = market.get('no_bid', market.get('no_bid_dollars', 0))
                            current = float(bid) if bid else 0
                            if current > 1.0:
                                current = current / 100.0
                            if current > 0:
                                exit_price = current
                                pnl = (current - entry) * count
                    except Exception:
                        pass

                    self.db.client.table('kalshi_trades').update({
                        'resolved': True,
                        'exit_price': exit_price,
                        'pnl': round(pnl, 4),
                        'resolved_at': now.isoformat(),
                        'reason': f"[FORCE CLOSE] Crypto position aged {age_minutes:.0f}min > 20min limit P&L=${pnl:+.2f}",
                    }).eq('id', trade['id']).execute()

                    force_closed += 1
                    logger.info(f"FORCE CLOSE: {trade.get('ticker', '?')} after {age_minutes:.0f}min P&L=${pnl:+.2f}")

            if force_closed > 0:
                logger.info(f"Force-closed {force_closed} stale crypto positions")
        except Exception as e:
            logger.error(f"Force close stale crypto failed: {e}")

    def run_cycle(self):
        logger.info("=" * 40)
        logger.info(f"Cycle at {datetime.now().isoformat()}")

        # Reset HyperThink debate counter each cycle (max 5 per cycle)
        self.hyperthink.reset_cycle()

        # === PHASE 0: Housekeeping ===
        # Check settlements FIRST, before generating new signals
        self._check_settlements()

        # Check crypto settlements (15-min markets settle fast)
        self._check_crypto_settlements()

        # Force-close stale crypto positions (>20 min old)
        self._force_close_stale_crypto()

        # Take portfolio snapshot (throttled to every 5 min)
        self._take_portfolio_snapshot()

        # Evaluate exits on open positions (profit-taking / loss-cutting)
        self._evaluate_exits()

        # Monitor non-weather paper positions for swing exits
        self._monitor_swings()

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

            # 3. Weather series (all 24: 17 high + 7 low temp cities)
            from strategies.weather_edge import CITIES as WEATHER_CITIES
            for series in WEATHER_CITIES:
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

        logger.info(f"Running {len(self.strategies)} strategies in parallel...")
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(run_strategy, s, markets): s for s in self.strategies}
            for future in concurrent.futures.as_completed(futures):
                name, signals = future.result()
                if signals:
                    logger.info(f"{name}: {len(signals)} signals")
                    all_signals.extend(signals)
                else:
                    logger.info(f"{name}: 0 signals")

        # === FAST SCALP SIGNALS (no debate, added every cycle) ===
        # 1. Crypto spread opportunities (market-making scalps)
        if hasattr(self, 'crypto_monitor') and self.crypto_monitor:
            for strat in self.strategies:
                if hasattr(strat, 'find_spread_opportunities'):
                    try:
                        spread_signals = strat.find_spread_opportunities()
                        if spread_signals:
                            logger.info(f"CryptoSpread: {len(spread_signals)} spread opportunities")
                            all_signals.extend(spread_signals)
                    except Exception as e:
                        logger.debug(f"Spread scan failed: {e}")
                    break

        # 2. Weather intraday scalps (cheap contracts near settlement)
        try:
            intraday_signals = self._find_intraday_weather_opportunities(markets)
            if intraday_signals:
                logger.info(f"WeatherIntraday: {len(intraday_signals)} intraday opportunities")
                all_signals.extend(intraday_signals)
        except Exception as e:
            logger.debug(f"Weather intraday scan failed: {e}")

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
                # Scalp strategies skip debate entirely — speed > consensus
                strategy_name = sig.get('strategy_type', 'unknown')
                SKIP_DEBATE_STRATEGIES = {
                    'weather_edge', 'weather_intraday',
                    'crypto_momentum', 'market_making_scalp',
                    'prob_arb', 'market_making', 'orderbook_edge',
                    'near_certainty', 'high_prob_lock', 'sports_no',
                    'forced_paper', 'precip_edge', 'cross_platform',
                }
                DEBATE_STRATEGIES = {'GrokNewsAnalysis', 'grok_news', 'MentionMarkets', 'mention_markets'}

                # Skip debate if this is a scalp/data-driven strategy
                if strategy_name.lower() in SKIP_DEBATE_STRATEGIES:
                    needs_debate = False
                else:
                    needs_debate = any(ds.lower() in strategy_name.lower() for ds in DEBATE_STRATEGIES)

                if needs_debate and not is_learning_trade and debates_used < MAX_DEBATES:
                    should_trade, sig, debate_log = run_debate(sig, price)
                    debates_used += 2  # Each debate uses 2 API calls

                    # Log debate to Supabase
                    if self.db:
                        try:
                            self.db.client.table('debate_log').insert({
                                'timestamp': datetime.utcnow().isoformat(),
                                'ticker': sig.get('ticker', ''),
                                'market_title': sig.get('title', sig.get('ticker', '')),
                                'grok_probability': sig.get('model_prob'),
                                'grok_recommendation': 'TRADE' if should_trade else 'SKIP',
                                'claude_probability': sig.get('claude_probability'),
                                'claude_recommendation': sig.get('claude_recommendation'),
                                'agreement': sig.get('debate_agreement', False),
                                'final_decision': 'TRADE' if should_trade else 'SKIP',
                                'size_modifier': sig.get('count', 1) / max(sig.get('original_count', sig.get('count', 1)), 1),
                                'votes': debate_log.split(':')[0] if ':' in debate_log else debate_log,
                            }).execute()
                        except Exception as e:
                            logger.error(f"Failed to log debate: {e}")

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
                tier = None

                if go_live:
                    # TIERED SIZING for live trades
                    self._refresh_real_balance()
                    balance_dollars = self.real_balance_cents / 100.0

                    # Calculate hours to close for tier scoring
                    market_obj = next((m for m in markets if m.get('ticker') == sig['ticker']), {})
                    htc = hours_until_close(market_obj.get('close_time') or market_obj.get('expiration_time'))

                    tier_signal = {
                        'model_prob': sig.get('model_prob', 0.5),
                        'edge': sig.get('edge', 0),
                        'confidence': sig.get('confidence', 0),
                        'entry_price': price_for_side,
                    }
                    tier, tier_score, tier_reasons = rate_signal(tier_signal, htc)
                    tier_count = size_by_tier(tier, price_for_side, balance_dollars)

                    # Check portfolio-level limits
                    try:
                        open_live = self.db.client.table('kalshi_trades').select('price,count').neq('order_id', 'paper').eq('resolved', False).execute()
                        current_exposure = sum(t.get('price', 0) * t.get('count', 0) for t in (open_live.data or []))
                    except Exception:
                        current_exposure = sum(p.get('cost', 0) for p in self.open_live_positions)

                    tier_cost = tier_count * price_for_side
                    allowed, max_cost, limit_reason = check_portfolio_limits(tier_cost, balance_dollars, current_exposure)

                    if not allowed:
                        logger.info(f"  SKIP LIVE {sig['ticker']}: {limit_reason}")
                        go_live = False
                    else:
                        if max_cost < tier_cost:
                            tier_count = max(1, int(max_cost / price_for_side))
                        sig['count'] = tier_count
                        cost = sig['count'] * price_for_side

                        cfg_label = TIER_CONFIG[tier]['label']
                        logger.info(
                            f"  {cfg_label} LIVE: {sig['ticker']} {sig['side'].upper()} "
                            f"x{sig['count']} @ ${price_for_side:.2f} = ${cost:.2f} "
                            f"[score={tier_score}, {', '.join(tier_reasons)}]"
                        )

                        success, order_id_or_reason = self._place_live_order(
                            sig, price_for_side, strategy_name,
                        )
                        if not success:
                            logger.info(f"  Live order rejected, falling back to paper: {order_id_or_reason}")
                            go_live = False

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
                    tier_tag = f"[{tier}] " if tier else ""
                    if go_live:
                        reason = f"[LIVE] {tier_tag}{reason}"
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

    def _check_settlements(self, markets=None):
        """Check if any open paper trades have settled and calculate P&L."""
        if not self.risk.positions:
            return

        # Build ticker -> market lookup from current data
        market_map = {m.get('ticker'): m for m in markets} if markets else {}

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
