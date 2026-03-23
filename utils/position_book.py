"""Position book + exit manager for active paper trading.

Tracks all open paper positions in memory, checks prices every cycle,
and sells when profit/loss thresholds hit. This is what turns buying into trading.
"""

from datetime import datetime, timezone
from utils.logger import setup_logger
from utils.market_helpers import get_yes_price, get_no_price

logger = setup_logger('position_book')


class Position:
    __slots__ = ('trade_id', 'ticker', 'side', 'entry_price', 'count',
                 'strategy', 'timestamp', 'current_price', 'high_water_mark', 'checks')

    def __init__(self, trade_id, ticker, side, entry_price, count, strategy, timestamp):
        self.trade_id = trade_id
        self.ticker = ticker
        self.side = side
        self.entry_price = entry_price
        self.count = count
        self.strategy = strategy
        self.timestamp = timestamp
        self.current_price = entry_price
        self.high_water_mark = entry_price
        self.checks = 0


class PositionBook:
    """In-memory position tracker synced with Supabase."""

    def __init__(self, db):
        self.db = db
        self.positions = {}  # trade_id -> Position
        self.total_realized_pnl = 0.0
        self.wins = 0
        self.losses = 0
        self.sells_today = 0

    def load_from_db(self):
        """Load all open paper positions from Supabase on startup."""
        if not self.db or not self.db.client:
            return
        try:
            result = self.db.client.table('kalshi_trades').select('*').eq(
                'order_id', 'paper'
            ).eq('resolved', False).execute()

            for row in (result.data or []):
                pos = Position(
                    trade_id=row['id'],
                    ticker=row.get('ticker', ''),
                    side=row.get('side', 'yes'),
                    entry_price=float(row.get('price', 0) or 0),
                    count=row.get('count', 1) or 1,
                    strategy=row.get('strategy', 'unknown'),
                    timestamp=row.get('timestamp') or row.get('created_at', ''),
                )
                if pos.entry_price > 0 and pos.ticker:
                    self.positions[row['id']] = pos

            logger.info(f"PositionBook loaded: {len(self.positions)} open paper positions")
        except Exception as e:
            logger.error(f"Failed to load position book: {e}")

    def add(self, trade_id, ticker, side, entry_price, count, strategy):
        """Add a new position after buying."""
        self.positions[trade_id] = Position(
            trade_id=trade_id, ticker=ticker, side=side,
            entry_price=entry_price, count=count, strategy=strategy,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    def close(self, trade_id, exit_price, reason):
        """Close a position — record PnL, update DB, remove from book."""
        if trade_id not in self.positions:
            return 0.0

        pos = self.positions[trade_id]
        pnl_per = exit_price - pos.entry_price
        total_pnl = pnl_per * pos.count

        # Update Supabase: mark buy as resolved
        if self.db and self.db.client:
            try:
                self.db.client.table('kalshi_trades').update({
                    'resolved': True,
                    'exit_price': round(exit_price, 4),
                    'pnl': round(total_pnl, 4),
                    'resolved_at': datetime.now(timezone.utc).isoformat(),
                    'reason': reason,
                }).eq('id', trade_id).execute()
            except Exception as e:
                logger.debug(f"DB update failed on close: {e}")

            # Log the sell as a separate row
            try:
                self.db.client.table('kalshi_trades').insert({
                    'ticker': pos.ticker,
                    'action': 'sell',
                    'side': pos.side,
                    'count': pos.count,
                    'strategy': pos.strategy,
                    'reason': reason,
                    'confidence': 0,
                    'order_id': 'paper',
                    'price': round(exit_price, 4),
                    'edge': round(pnl_per / max(pos.entry_price, 0.01), 4),
                }).execute()
            except Exception as e:
                logger.debug(f"DB sell log failed: {e}")

        # Stats
        if total_pnl > 0:
            self.wins += 1
        else:
            self.losses += 1
        self.total_realized_pnl += total_pnl
        self.sells_today += 1

        del self.positions[trade_id]
        return total_pnl

    def get_by_ticker(self, ticker):
        return [p for p in self.positions.values() if p.ticker == ticker]

    def count_by_strategy(self, strategy):
        return sum(1 for p in self.positions.values() if p.strategy == strategy)

    def total_exposure(self):
        return sum(p.entry_price * p.count for p in self.positions.values())

    def stats(self):
        total = self.wins + self.losses
        return {
            'open': len(self.positions),
            'exposure': self.total_exposure(),
            'realized_pnl': self.total_realized_pnl,
            'wins': self.wins,
            'losses': self.losses,
            'win_rate': (self.wins / total * 100) if total > 0 else 0,
            'sells_today': self.sells_today,
        }


class ExitManager:
    """Runs every cycle. Checks positions against current market prices. Sells actively."""

    # SCALPING THRESHOLDS — Buy cheap, sell the pump
    TAKE_PROFIT = 0.50          # 50% gain — sell (5c -> 7.5c, 10c -> 15c)
    SCALP_CHEAP = 0.30          # 30% on cheap contracts (<$0.15)
    BIG_WIN = 1.00              # 100% always sell (doubled money)
    STOP_LOSS = -0.50           # 50% loss — cut it
    TRAILING_STOP = 0.20        # 20% drop from peak (protect gains)
    CRYPTO_TP = 0.15            # Crypto: 15% take profit
    CRYPTO_SL = -0.25           # Crypto: 25% stop loss
    EXPIRY_HOURS = 1            # Sell if <1h to expiry and up (avoid settlement risk)
    STALE_CHECKS = 150          # Cut stale positions faster
    MAX_SELLS_PER_CYCLE = 75    # Cap per cycle to not overwhelm API

    def __init__(self, kalshi_client, position_book, risk_manager=None):
        self.client = kalshi_client
        self.book = position_book
        self.risk = risk_manager
        self.sells_this_cycle = 0
        self._price_cache = {}

    def run(self):
        """Check all positions and sell what needs selling."""
        self._price_cache = {}
        self.sells_this_cycle = 0

        if not self.book.positions:
            return

        # Group by ticker to minimize API calls
        from collections import defaultdict
        by_ticker = defaultdict(list)
        for tid, pos in list(self.book.positions.items()):
            by_ticker[pos.ticker].append((tid, pos))

        for ticker, positions in by_ticker.items():
            if self.sells_this_cycle >= self.MAX_SELLS_PER_CYCLE:
                break

            market = self._fetch_market(ticker)
            if not market:
                continue

            # Check if already settled
            result = market.get('result', '')
            if result in ('yes', 'no'):
                for tid, pos in positions:
                    exit_price = 1.0 if result == pos.side else 0.0
                    pnl = self.book.close(tid, exit_price,
                        f"[SETTLED] market={result} side={pos.side} {'WIN' if result == pos.side else 'LOSS'}")
                    self._refund_balance(pos, pnl)
                    self.sells_this_cycle += 1
                    logger.info(f"SETTLED: {ticker} {pos.side} -> {result} P&L=${pnl:+.2f}")
                continue

            # Parse close time once per ticker
            hours_left = self._hours_until_close(market)

            for tid, pos in positions:
                if self.sells_this_cycle >= self.MAX_SELLS_PER_CYCLE:
                    break

                # Get current bid price (what we can sell for)
                if pos.side == 'yes':
                    current = get_yes_price(market)
                else:
                    current = get_no_price(market)

                if current <= 0:
                    continue

                pos.current_price = current
                pos.checks += 1
                if current > pos.high_water_mark:
                    pos.high_water_mark = current

                reason = self._check_exit(pos, hours_left)
                if reason:
                    pnl = self.book.close(tid, current, reason)
                    self._refund_balance(pos, pnl)
                    self.sells_this_cycle += 1
                    logger.info(f"EXIT: {ticker} {pos.side} | {reason} | ${pos.entry_price:.2f}->${current:.2f} P&L=${pnl:+.2f}")

        s = self.book.stats()
        logger.info(
            f"EXIT MANAGER: sold {self.sells_this_cycle} | "
            f"open={s['open']} | realized=${s['realized_pnl']:+.2f} | "
            f"win_rate={s['win_rate']:.0f}% ({s['wins']}W/{s['losses']}L)"
        )

    def _check_exit(self, pos, hours_left):
        """Returns reason string if should sell, None if hold."""
        entry = pos.entry_price
        current = pos.current_price
        if entry <= 0:
            return None

        pct = (current - entry) / entry
        is_crypto = any(x in pos.ticker for x in ('KXBTC', 'KXETH', 'KXSOL'))

        # Big win — always
        if pct >= self.BIG_WIN:
            return f"BIG WIN +{pct:.0%} (${entry:.2f}->${current:.2f})"

        # Crypto exits (tighter)
        if is_crypto:
            if pct >= self.CRYPTO_TP:
                return f"CRYPTO TP +{pct:.0%}"
            if pct <= self.CRYPTO_SL:
                return f"CRYPTO SL {pct:.0%}"

        # Scalp cheap contracts
        if entry < 0.15 and pct >= self.SCALP_CHEAP:
            return f"SCALP +{pct:.0%} on ${entry:.2f}"

        # Standard take profit
        if pct >= self.TAKE_PROFIT:
            return f"TAKE PROFIT +{pct:.0%}"

        # Near expiry — sell if up anything
        if 0 < hours_left < self.EXPIRY_HOURS and pct > 0.02:
            return f"EXPIRY +{pct:.0%} ({hours_left:.1f}h left)"

        # Stop loss
        if not is_crypto and pct <= self.STOP_LOSS:
            return f"STOP LOSS {pct:.0%}"

        # Trailing stop: dropped 20% from peak
        if pos.high_water_mark > entry and pos.high_water_mark > 0:
            drop = (current - pos.high_water_mark) / pos.high_water_mark
            if drop <= -self.TRAILING_STOP:
                return f"TRAILING STOP peak=${pos.high_water_mark:.2f}->${current:.2f}"

        # Stale position
        if pos.checks > self.STALE_CHECKS and abs(pct) < 0.05:
            return f"STALE ({pos.checks} checks, {pct:+.1%})"

        return None

    def _refund_balance(self, pos, pnl):
        """Return capital to paper balance after closing."""
        if self.risk:
            # Refund: entry cost + realized pnl
            refund = pos.entry_price * pos.count + pnl
            self.risk.paper_balance += max(refund, 0)

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
