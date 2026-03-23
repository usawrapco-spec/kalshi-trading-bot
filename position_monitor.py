"""Position Monitor — Scalping Engine.

Buy cheap, sell the pump. Monitors ALL open positions (paper + live)
and sells when profit thresholds hit. Runs every cycle.

Sell Rules:
  - Up 100%+: SELL immediately (doubled your money)
  - Up 50%+ held 2+ hours: SELL (lock profit)
  - Up 30%+ market closes in <1 hour: SELL (avoid settlement risk)
  - Down 50%+: SELL (stop loss)
  - Settled market: mark resolved with P&L
"""

from datetime import datetime, timezone
from utils.logger import setup_logger
from utils.market_helpers import get_yes_price, get_no_price

logger = setup_logger('position_monitor')


# Scalping thresholds
TAKE_PROFIT_BIG = 1.00       # 100% gain -> sell immediately
TAKE_PROFIT_TIME = 0.50      # 50% gain + held 2h -> sell
TAKE_PROFIT_EXPIRY = 0.30    # 30% gain + <1h to close -> sell
STOP_LOSS = -0.50            # 50% loss -> cut
SCALP_CHEAP = 0.15           # 15% gain on cheap (<15c) contracts
TRAILING_STOP_DROP = 0.25    # 25% drop from high water mark

MAX_CHECKS_PER_CYCLE = 100   # Limit API calls per cycle


class PositionMonitor:
    """Unified position monitor for paper + live trades."""

    def __init__(self, kalshi_client, db, risk_manager=None):
        self.client = kalshi_client
        self.db = db
        self.risk = risk_manager
        self._price_cache = {}  # ticker -> market data (reset each cycle)

    def run(self):
        """Check all open positions, sell winners and losers."""
        if not self.db or not self.db.client:
            return

        self._price_cache = {}

        try:
            open_trades = self.db.client.table('kalshi_trades').select('*').eq(
                'resolved', False
            ).execute()

            if not open_trades.data:
                logger.info("POSITION MONITOR: 0 open positions")
                return

            trades = open_trades.data
            summary = {'sells': 0, 'cuts': 0, 'holds': 0, 'settled': 0,
                       'pnl': 0.0, 'live_sells': 0, 'paper_sells': 0}

            # Group by ticker to minimize API calls
            from collections import defaultdict
            by_ticker = defaultdict(list)
            for trade in trades:
                ticker = trade.get('ticker', '')
                if ticker:
                    by_ticker[ticker].append(trade)

            checked = 0
            for ticker, ticker_trades in by_ticker.items():
                if checked >= MAX_CHECKS_PER_CYCLE:
                    break

                market = self._fetch_market(ticker)
                if not market:
                    continue
                checked += 1

                # Check if market already settled
                market_result = market.get('result', '')
                if market_result in ('yes', 'no'):
                    for trade in ticker_trades:
                        self._handle_settlement(trade, market_result, summary)
                    continue

                # Parse hours until close
                hours_left = self._hours_until_close(market)

                for trade in ticker_trades:
                    self._evaluate_position(trade, market, hours_left, summary)

            total = len(trades)
            logger.info(
                f"POSITION MONITOR: {total} open | checked {checked} tickers | "
                f"{summary['sells']} sells, {summary['cuts']} cuts, "
                f"{summary['settled']} settled, {summary['holds']} holds | "
                f"P&L=${summary['pnl']:+.2f} | "
                f"live={summary['live_sells']}, paper={summary['paper_sells']}"
            )

        except Exception as e:
            logger.error(f"Position monitor failed: {e}")

    def _evaluate_position(self, trade, market, hours_left, summary):
        """Check a single position against sell rules."""
        side = trade.get('side', 'yes')
        entry_price = float(trade.get('price', 0) or 0)
        count = trade.get('count', 1) or 1
        ticker = trade.get('ticker', '')
        is_live = trade.get('order_id') not in (None, 'paper', 'forced_paper')

        if entry_price <= 0:
            return

        # Get current bid (what we can sell for)
        if side == 'yes':
            current_price = get_yes_price(market)
        else:
            current_price = get_no_price(market)

        if current_price <= 0:
            return

        # Calculate P&L
        pct_gain = (current_price - entry_price) / entry_price

        # Calculate hours held
        hours_held = self._hours_held(trade)

        # === SELL RULES ===
        should_sell = False
        sell_reason = ""
        action_type = "SELL"

        # Rule 1: Up 100%+ -> SELL immediately (doubled your money)
        if pct_gain >= TAKE_PROFIT_BIG:
            should_sell = True
            sell_reason = f"TAKE PROFIT: +{pct_gain:.0%} (${entry_price:.2f} -> ${current_price:.2f})"

        # Rule 2: Up 50%+ and held 2+ hours -> SELL (lock profit)
        elif pct_gain >= TAKE_PROFIT_TIME and hours_held >= 2:
            should_sell = True
            sell_reason = f"PROFIT LOCK: +{pct_gain:.0%} after {hours_held:.1f}h"

        # Rule 3: Up 30%+ and market closes in <1 hour -> SELL
        elif pct_gain >= TAKE_PROFIT_EXPIRY and 0 < hours_left < 1:
            should_sell = True
            sell_reason = f"EXPIRY LOCK: +{pct_gain:.0%} with {hours_left:.1f}h left"

        # Rule 4: Cheap contract scalp — 15%+ on entries below 15c
        elif entry_price < 0.15 and pct_gain >= SCALP_CHEAP:
            should_sell = True
            sell_reason = f"SCALP: +{pct_gain:.0%} on ${entry_price:.2f} entry"

        # Rule 5: Stop loss at -50%
        elif pct_gain <= STOP_LOSS:
            should_sell = True
            sell_reason = f"STOP LOSS: {pct_gain:.0%} (${entry_price:.2f} -> ${current_price:.2f})"
            action_type = "CUT"

        # Rule 6: Near expiry — sell if up anything with <2h left
        elif pct_gain > 0.05 and 0 < hours_left < 2:
            should_sell = True
            sell_reason = f"EXPIRY: +{pct_gain:.0%} with {hours_left:.1f}h left"

        if not should_sell:
            summary['holds'] += 1
            return

        # Execute the sell
        dollar_pnl = (current_price - entry_price) * count

        if is_live:
            # Place real sell order on Kalshi
            success = self._place_live_sell(ticker, side, count, current_price)
            if not success:
                summary['holds'] += 1
                return
            summary['live_sells'] += 1
        else:
            summary['paper_sells'] += 1

        # Update trade in Supabase as resolved
        try:
            self.db.client.table('kalshi_trades').update({
                'resolved': True,
                'exit_price': round(current_price, 4),
                'pnl': round(dollar_pnl, 4),
                'resolved_at': datetime.now(timezone.utc).isoformat(),
                'reason': f"[SCALP {action_type}] {sell_reason}",
            }).eq('id', trade['id']).execute()
        except Exception as e:
            logger.error(f"DB update failed for {ticker}: {e}")

        # Refund paper balance
        if not is_live and self.risk:
            refund = entry_price * count + dollar_pnl
            self.risk.paper_balance += max(refund, 0)

        trade_type = "LIVE" if is_live else "PAPER"
        logger.info(
            f"{trade_type} {action_type}: {ticker} {side.upper()} | "
            f"{sell_reason} | P&L=${dollar_pnl:+.2f}"
        )

        if action_type == "CUT":
            summary['cuts'] += 1
        else:
            summary['sells'] += 1
        summary['pnl'] += dollar_pnl

    def _handle_settlement(self, trade, market_result, summary):
        """Handle a trade whose market has settled."""
        side = trade.get('side', 'yes')
        entry_price = float(trade.get('price', 0) or 0)
        count = trade.get('count', 1) or 1
        ticker = trade.get('ticker', '')
        is_live = trade.get('order_id') not in (None, 'paper', 'forced_paper')

        won = (side == market_result)
        exit_price = 1.0 if won else 0.0
        pnl = (exit_price - entry_price) * count

        try:
            self.db.client.table('kalshi_trades').update({
                'resolved': True,
                'exit_price': exit_price,
                'pnl': round(pnl, 4),
                'resolved_at': datetime.now(timezone.utc).isoformat(),
                'reason': f"[SETTLED] market={market_result} side={side} {'WIN' if won else 'LOSS'}",
            }).eq('id', trade['id']).execute()
        except Exception as e:
            logger.error(f"Settlement DB update failed for {ticker}: {e}")

        if not is_live and self.risk:
            refund = entry_price * count + pnl
            self.risk.paper_balance += max(refund, 0)

        trade_type = "LIVE" if is_live else "PAPER"
        result_label = "WIN" if won else "LOSS"
        logger.info(f"{trade_type} SETTLED: {ticker} {side} -> {market_result} {result_label} P&L=${pnl:+.2f}")
        summary['settled'] += 1
        summary['pnl'] += pnl

    def _place_live_sell(self, ticker, side, count, price):
        """Place a real limit sell order on Kalshi."""
        try:
            price_cents = int(price * 100)
            sell_params = {
                'ticker': ticker,
                'action': 'sell',
                'side': side,
                'count': count,
                'order_type': 'limit',
            }
            if side == 'yes':
                sell_params['yes_price'] = price_cents
            else:
                sell_params['no_price'] = price_cents

            result = self.client.create_order(**sell_params)
            if result and (result.get('order', {}).get('order_id') or result.get('order_id')):
                logger.info(f"LIVE SELL ORDER PLACED: {ticker} {side} x{count} @ ${price:.2f}")
                return True
            else:
                err = result.get('error', str(result)) if result else 'No response'
                logger.warning(f"LIVE SELL FAILED: {ticker} - {err}")
                return False
        except Exception as e:
            logger.error(f"LIVE SELL ERROR: {ticker} - {e}")
            return False

    def _fetch_market(self, ticker):
        """Fetch market data with caching."""
        if ticker in self._price_cache:
            return self._price_cache[ticker]
        try:
            resp = self.client.get_market(ticker)
            if not resp:
                return None
            market = resp.get('market', resp)
            self._price_cache[ticker] = market
            return market
        except Exception:
            return None

    def _hours_until_close(self, market):
        """Parse close time, return hours left or 999."""
        close_time = market.get('close_time') or market.get('expiration_time') or ''
        if not close_time:
            return 999
        try:
            close_dt = datetime.fromisoformat(close_time.replace('Z', '+00:00'))
            now = datetime.now(timezone.utc)
            return (close_dt - now).total_seconds() / 3600
        except Exception:
            return 999

    def _hours_held(self, trade):
        """Calculate how many hours a position has been held."""
        ts_str = trade.get('timestamp') or trade.get('created_at') or ''
        if not ts_str:
            return 0
        try:
            trade_time = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
            now = datetime.now(timezone.utc)
            return (now - trade_time).total_seconds() / 3600
        except Exception:
            return 0
