"""Risk management with Kelly Criterion position sizing and paper trading."""

import math
from datetime import datetime
from config import Config
from utils.logger import setup_logger

logger = setup_logger('risk_manager')

MIN_CONFIDENCE = 30
MAX_OPEN_POSITIONS = 50
MAX_TRADE_PCT = 0.10       # Max 10% of starting balance per single trade ($10)
MAX_STRATEGY_PCT = 0.20    # Max 20% of starting balance per strategy per cycle ($20)
CASH_RESERVE_PCT = 0.30    # Never go below 30% of starting balance ($30)
DAILY_LOSS_STOP = -50.0    # 50% of $100 paper balance
KELLY_FRACTION = 0.25      # Quarter-Kelly for conservative sizing
STARTING_BALANCE = 100.0   # Reference for percentage calculations


class RiskManager:
    """Manages risk, Kelly sizing, and paper balance."""

    def __init__(self):
        self.paper_balance = 100.00  # Hardcoded $100 starting balance
        self.daily_pnl = 0.0
        self.positions = {}  # ticker -> {side, count, entry_price, strategy}
        self.daily_reset = datetime.now().date()
        self.trades_today = 0
        self.stopped = False
        self.total_trades = 0
        self.total_wins = 0
        self.total_pnl = 0.0
        self.strategy_stats = {}  # strategy -> {trades, wins, pnl}
        logger.info(
            f"RiskManager: paper_balance=${self.paper_balance:.2f}, "
            f"kelly={KELLY_FRACTION}, max_positions={MAX_OPEN_POSITIONS}, "
            f"daily_stop=${abs(DAILY_LOSS_STOP)}"
        )

    def set_balance(self, balance_cents):
        """Update from real API balance (not used in paper mode)."""
        pass  # Paper mode tracks its own balance

    def kelly_size(self, edge, probability, price_cents=50):
        """Kelly Criterion position sizing: bankroll * 0.25 * (edge * confidence) / (1 - market_price)."""
        if edge <= 0 or probability <= 0 or probability >= 1 or price_cents <= 0:
            return 1

        price_dollars = price_cents / 100.0 if price_cents > 1 else price_cents
        if price_dollars >= 1:
            return 1

        # Kelly formula: f* = (b*p - q) / b where b = payout ratio
        payout = (1.0 - price_dollars) / price_dollars  # e.g. pay 30c to win 70c = 2.33x
        q = 1 - probability
        kelly_f = (payout * probability - q) / payout

        if kelly_f <= 0:
            return 1

        fraction = kelly_f * KELLY_FRACTION
        bankroll_dollars = self.paper_balance
        risk_amount = bankroll_dollars * fraction

        # Cap at 20% of balance
        risk_amount = min(risk_amount, bankroll_dollars * MAX_TRADE_PCT)

        contracts = max(1, int(risk_amount / price_dollars))
        contracts = min(contracts, Config.MAX_ORDER_SIZE)

        logger.info(
            f"Kelly: edge={edge:.1%} prob={probability:.1%} price={price_dollars:.2f} "
            f"kelly_f={kelly_f:.3f} fraction={fraction:.3f} -> {contracts} contracts"
        )
        return contracts

    def check_daily_loss_limit(self):
        self._reset_if_new_day()
        if self.stopped:
            return False
        if self.daily_pnl <= DAILY_LOSS_STOP:
            logger.warning(f"Daily loss stop: ${self.daily_pnl:.2f} <= ${DAILY_LOSS_STOP}")
            self.stopped = True
            return False
        return True

    def check_max_positions(self):
        active = len(self.positions)
        if active >= MAX_OPEN_POSITIONS:
            logger.warning(f"Max positions: {active}/{MAX_OPEN_POSITIONS}")
            return False
        return True

    def check_confidence(self, confidence):
        if confidence < MIN_CONFIDENCE:
            logger.info(f"Confidence {confidence:.0f} < {MIN_CONFIDENCE}")
            return False
        return True

    def can_trade(self, ticker, count, confidence=0, price_cents=50):
        checks = [
            ("Daily loss", self.check_daily_loss_limit()),
            ("Confidence", self.check_confidence(confidence)),
            ("Max positions", self.check_max_positions()),
        ]
        for name, ok in checks:
            if not ok:
                logger.warning(f"Trade blocked: {name}")
                return False
        return True

    def record_paper_trade(self, ticker, side, count, entry_price, strategy, title=''):
        """Record a paper trade entry. Returns False if blocked."""
        # Hard guards - these cannot be bypassed
        if ticker in self.positions:
            logger.info(f"SKIP {ticker}: already have open position")
            return False
        if len(self.positions) >= MAX_OPEN_POSITIONS:
            logger.info(f"SKIP {ticker}: max positions ({MAX_OPEN_POSITIONS}) reached")
            return False

        # Cash reserve: never go below 30% of starting balance
        cash_floor = STARTING_BALANCE * CASH_RESERVE_PCT
        if self.paper_balance <= cash_floor:
            logger.info(f"SKIP {ticker}: balance ${self.paper_balance:.2f} <= cash reserve ${cash_floor:.2f}")
            return False

        # Max 10% of starting balance per single trade
        max_per_trade = STARTING_BALANCE * MAX_TRADE_PCT
        cost = count * entry_price
        if cost > max_per_trade:
            # Reduce count to fit within limit
            count = max(1, int(max_per_trade / entry_price))
            cost = count * entry_price

        if cost > self.paper_balance - cash_floor:
            logger.info(f"SKIP {ticker}: cost ${cost:.2f} would breach cash reserve")
            return False

        # Max 20% of starting balance per strategy per cycle
        max_per_strategy = STARTING_BALANCE * MAX_STRATEGY_PCT
        strategy_spent = sum(
            p['count'] * p['entry_price']
            for p in self.positions.values()
            if p.get('strategy') == strategy
        )
        if strategy_spent + cost > max_per_strategy:
            logger.info(f"SKIP {ticker}: strategy {strategy} at ${strategy_spent:.2f} + ${cost:.2f} > ${max_per_strategy:.2f} limit")
            return False

        self.positions[ticker] = {
            'side': side, 'count': count,
            'entry_price': entry_price,
            'strategy': strategy, 'title': title,
            'timestamp': datetime.now().isoformat(),
        }
        self.paper_balance -= cost
        self.trades_today += 1
        self.total_trades += 1

        if strategy not in self.strategy_stats:
            self.strategy_stats[strategy] = {'trades': 0, 'wins': 0, 'pnl': 0.0}
        self.strategy_stats[strategy]['trades'] += 1

        logger.info(
            f"PAPER TRADE: BUY {count}x {side.upper()} {ticker} @ ${entry_price:.2f} "
            f"(cost=${cost:.2f}, balance=${self.paper_balance:.2f}, "
            f"positions={len(self.positions)}/{MAX_OPEN_POSITIONS})"
        )
        return True

    def settle_paper_trade(self, ticker, resolved_yes):
        """Settle a paper trade. resolved_yes = True if YES won."""
        if ticker not in self.positions:
            return
        pos = self.positions[ticker]
        side = pos['side']
        count = pos['count']
        entry = pos['entry_price']
        strategy = pos['strategy']

        won = (side == 'yes' and resolved_yes) or (side == 'no' and not resolved_yes)
        payout = count * 1.0 if won else 0.0  # Each winning contract pays $1
        cost = count * entry
        pnl = payout - cost

        self.paper_balance += payout
        self.daily_pnl += pnl
        self.total_pnl += pnl
        if won:
            self.total_wins += 1
            self.strategy_stats[strategy]['wins'] += 1
        self.strategy_stats[strategy]['pnl'] += pnl

        del self.positions[ticker]
        logger.info(
            f"PAPER SETTLE: {ticker} {'WIN' if won else 'LOSS'} pnl=${pnl:+.2f} "
            f"balance=${self.paper_balance:.2f}"
        )

    def update_position(self, ticker, count, side):
        """Legacy compatibility."""
        self.trades_today += 1

    def update_pnl(self, pnl):
        self._reset_if_new_day()
        self.daily_pnl += pnl

    def _reset_if_new_day(self):
        today = datetime.now().date()
        if today > self.daily_reset:
            logger.info("New trading day - resetting daily counters")
            self.daily_pnl = 0
            self.trades_today = 0
            self.daily_reset = today
            self.stopped = False

    def get_status(self):
        win_rate = (self.total_wins / self.total_trades * 100) if self.total_trades > 0 else 0
        return {
            'paper_balance': self.paper_balance,
            'daily_pnl': self.daily_pnl,
            'total_pnl': self.total_pnl,
            'trades_today': self.trades_today,
            'total_trades': self.total_trades,
            'total_wins': self.total_wins,
            'win_rate': win_rate,
            'positions': self.positions,
            'stopped': self.stopped,
            'strategy_stats': self.strategy_stats,
        }

    def log_status(self):
        s = self.get_status()
        logger.info("=" * 50)
        logger.info("RISK STATUS")
        logger.info(f"Paper Balance: ${s['paper_balance']:.2f}")
        logger.info(f"Daily P&L: ${s['daily_pnl']:+.2f}")
        logger.info(f"Total P&L: ${s['total_pnl']:+.2f}")
        logger.info(f"Win Rate: {s['win_rate']:.1f}% ({s['total_wins']}/{s['total_trades']})")
        logger.info(f"Open Positions: {len(s['positions'])}/{MAX_OPEN_POSITIONS}")
        for strat, stats in s['strategy_stats'].items():
            wr = (stats['wins'] / stats['trades'] * 100) if stats['trades'] > 0 else 0
            logger.info(f"  {strat}: {stats['trades']} trades, {wr:.0f}% win, ${stats['pnl']:+.2f}")
        logger.info("=" * 50)
