"""
RAZOR — Crypto scalper bot.
Buy YES-side crypto contracts settling within 15 min.
25% of pre-window cash per round. Ride to settlement.
Tracks everything in local DB. Dashboard shows real positions + market prices.
"""

import os, time, logging, traceback, math
from datetime import datetime, timezone
from flask import Flask, jsonify
import psycopg2
from psycopg2.extras import RealDictCursor
from kalshi_auth import KalshiAuth
import requests

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# === CONFIG ===
KALSHI_HOST = os.environ.get('KALSHI_API_HOST', 'https://api.elections.kalshi.com')
DATABASE_URL = os.environ.get('DATABASE_URL', 'postgresql://kalshi:kalshi@localhost:5432/kalshi')
ENABLE_TRADING = os.environ.get('ENABLE_TRADING', 'false').lower() in ('true', '1', 'yes')

# === STRATEGY ===
BUY_MIN = 0.01
BUY_MAX = 0.99
TAKER_FEE_RATE = 0.07
MAX_MINS_TO_EXPIRY = 15
MIN_MINS_TO_BUY = 10          # only buy when 10-15 min left (first 5 min of window)
CYCLE_SECONDS = 2
CONTRACTS = 1
MAX_POSITIONS = 20
MAX_BUYS_PER_WINDOW = 3       # max NEW positions per 15-min round
ROUND_BUDGET_PCT = 0.25       # spend max 25% of STARTING_BALANCE per round (hard cap)
SIDE_STRATEGY = os.environ.get('SIDE_STRATEGY', 'cheapest')  # 'yes', 'no', or 'cheapest'
CUT_WHEN_MINS_LEFT = 5        # start cutting when 5 min left in window
CUT_LOSS_THRESHOLD = -0.70
TAKE_PROFIT_THRESHOLD = 1.00  # sell at +100% gain
STARTING_BALANCE = 50.00      # paper mode starting balance

CRYPTO_SERIES = ['KXBTC15M', 'KXETH15M', 'KXSOL15M', 'KXXRP15M']  # dropped DOGE (19% win rate, -$9.59)

# === INIT ===
auth = KalshiAuth()
app = Flask(__name__)

# Cache for market data and Kalshi portfolio — filled by bot cycle, read by dashboard
_cache = {
    'markets': {},
    'balance': {},
    'positions': [],
    'updated': 0,
}

# Last known good balance — fallback when API fails
_last_known_balance = 0

# Round-start balance tracking — snapshot once per 15-min window
_round = {
    'start_balance': 0,
    'spent': 0,
    'buys': 0,
    'window_id': -1,
}


def sf(val):
    try:
        return float(val) if val is not None else 0.0
    except:
        return 0.0


def kalshi_fee(price, count):
    return min(math.ceil(TAKER_FEE_RATE * count * price * (1 - price) * 100) / 100, 0.02 * count)


def mins_left_in_window():
    """Minutes remaining in the current 15-min window."""
    now = datetime.now(timezone.utc)
    mins_in = now.minute % 15 + now.second / 60
    return 15 - mins_in


# === DATABASE ===

def get_db():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    return conn


def init_razor_db():
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS scraper_trades (
                    id SERIAL PRIMARY KEY,
                    ticker TEXT NOT NULL,
                    side TEXT NOT NULL,
                    price NUMERIC NOT NULL,
                    count INTEGER DEFAULT 1,
                    current_bid NUMERIC DEFAULT 0,
                    pnl NUMERIC,
                    fees NUMERIC DEFAULT 0,
                    status TEXT DEFAULT 'open',
                    bought_at TIMESTAMPTZ DEFAULT NOW(),
                    closed_at TIMESTAMPTZ,
                    close_reason TEXT
                )
            """)
            # Add fees column if missing
            try:
                cur.execute("ALTER TABLE scraper_trades ADD COLUMN fees NUMERIC DEFAULT 0")
            except:
                pass
    finally:
        conn.close()
    logger.info("RAZOR: Database initialized")


# === KALSHI API ===

def kalshi_get(path):
    url = f"{KALSHI_HOST}/trade-api/v2{path}"
    headers = auth.get_headers("GET", f"/trade-api/v2{path}")
    resp = requests.get(url, headers=headers, timeout=10)
    resp.raise_for_status()
    return resp.json()


def kalshi_post(path, data):
    url = f"{KALSHI_HOST}/trade-api/v2{path}"
    headers = auth.get_headers("POST", f"/trade-api/v2{path}")
    headers['Content-Type'] = 'application/json'
    resp = requests.post(url, headers=headers, json=data, timeout=10)
    resp.raise_for_status()
    return resp.json()


def get_market(ticker):
    try:
        resp = kalshi_get(f"/markets/{ticker}")
        return resp.get('market', resp)
    except:
        return None


def place_order(ticker, side, action, price, count):
    if not ENABLE_TRADING:
        logger.info(f"RAZOR PAPER {action}: {ticker} {side} x{count} @ ${price:.2f}")
        return ('paper', count, price)

    price_cents = int(round(price * 100))
    try:
        resp = kalshi_post('/portfolio/orders', {
            'ticker': ticker, 'action': action, 'side': side,
            'type': 'limit', 'count': count,
            'yes_price' if side == 'yes' else 'no_price': price_cents,
        })
        order = resp.get('order', {})
        order_id = order.get('order_id', '')
        filled = order.get('place_count', 0) - order.get('remaining_count', 0)
        if filled <= 0:
            filled = count if order.get('status') in ('executed', 'filled') else 0
        if filled <= 0:
            logger.warning(f"RAZOR ORDER NOT FILLED: {ticker} {side} {action} x{count} @ ${price:.2f} -- status={order.get('status')}, remaining={order.get('remaining_count')}")
            return None

        # Get actual fill price from fills API
        fill_price = price
        try:
            fills_resp = kalshi_get(f'/portfolio/fills?order_id={order_id}&limit=10')
            fills = fills_resp.get('fills', [])
            if fills:
                if side == 'yes':
                    fill_price = sf(fills[0].get('yes_price_dollars', str(price)))
                else:
                    fill_price = sf(fills[0].get('no_price_dollars', str(price)))
                if fill_price <= 0:
                    fill_price = price
                logger.info(f"RAZOR FILL: asked ${price:.2f}, filled @ ${fill_price:.2f}")
        except:
            pass

        return (order_id, filled, fill_price)
    except Exception as e:
        logger.error(f"RAZOR ORDER FAILED: {action} {ticker} -- {e}")
        return None


# === CORE LOGIC ===

def get_open_positions():
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM scraper_trades WHERE status = 'open' ORDER BY bought_at")
            return cur.fetchall()
    finally:
        conn.close()


def fetch_all_markets():
    all_markets = []
    for series in CRYPTO_SERIES:
        try:
            cursor = None
            while True:
                url = f'/markets?series_ticker={series}&status=open&limit=200'
                if cursor:
                    url += f'&cursor={cursor}'
                resp = kalshi_get(url)
                batch = resp.get('markets', [])
                all_markets.extend(batch)
                cursor = resp.get('cursor')
                if not cursor or not batch:
                    break
        except Exception as e:
            logger.error(f"RAZOR Fetch {series} failed: {e}")
    return all_markets


def find_cheapest(markets):
    now = datetime.now(timezone.utc)
    candidates = []

    for market in markets:
        ticker = market.get('ticker', '')
        close_time = market.get('close_time') or market.get('expected_expiration_time')
        if not close_time:
            continue
        try:
            close_dt = datetime.fromisoformat(close_time.replace('Z', '+00:00'))
            mins_left = (close_dt - now).total_seconds() / 60
            if mins_left < MIN_MINS_TO_BUY or mins_left > MAX_MINS_TO_EXPIRY:
                continue
        except:
            continue

        yes_ask = sf(market.get('yes_ask_dollars', '999'))
        no_ask = sf(market.get('no_ask_dollars', '999'))

        if SIDE_STRATEGY in ('cheapest', 'yes'):
            if BUY_MIN <= yes_ask <= BUY_MAX:
                candidates.append({'ticker': ticker, 'side': 'yes', 'price': yes_ask, 'mins_left': mins_left})
        if SIDE_STRATEGY in ('cheapest', 'no'):
            if BUY_MIN <= no_ask <= BUY_MAX:
                candidates.append({'ticker': ticker, 'side': 'no', 'price': no_ask, 'mins_left': mins_left})

    candidates.sort(key=lambda x: x['price'])
    return candidates


def check_sells():
    """Update bids from Kalshi, handle settlements, cut 70%+ losers at 5min."""
    open_positions = get_open_positions()
    if not open_positions:
        return

    now = datetime.now(timezone.utc)
    conn = get_db()

    try:
        for trade in open_positions:
            ticker = trade['ticker']
            side = trade['side']
            entry = sf(trade['price'])
            count = trade.get('count') or 1
            trade_id = trade['id']

            if entry <= 0:
                continue

            market = get_market(ticker)
            if not market:
                continue

            result_val = market.get('result', '')
            status = market.get('status', '')

            # === SETTLED ===
            if result_val:
                buy_fee = kalshi_fee(entry, count)
                if result_val == side:
                    pnl = round((1.0 - entry) * count - buy_fee, 4)
                    reason = 'win'
                else:
                    pnl = round(-entry * count - buy_fee, 4)
                    reason = 'loss'
                logger.info(f"RAZOR SETTLED: {ticker} {side} ${entry:.2f} -> {reason} pnl=${pnl:.4f}")
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE scraper_trades SET pnl=%s, fees=%s, status='closed', closed_at=NOW(), close_reason=%s, current_bid=%s WHERE id=%s",
                        (float(pnl), float(buy_fee), reason, 1.0 if reason == 'win' else 0.0, trade_id)
                    )
                continue

            if status in ('closed', 'settled', 'finalized'):
                continue

            # Get current bid from live market
            if side == 'yes':
                current_bid = sf(market.get('yes_bid_dollars', '0'))
            else:
                current_bid = sf(market.get('no_bid_dollars', '0'))

            # Always update current bid
            with conn.cursor() as cur:
                cur.execute("UPDATE scraper_trades SET current_bid=%s WHERE id=%s",
                            (float(current_bid), trade_id))

            if current_bid <= 0:
                continue

            # === NO TAKE PROFIT, NO CUT — ride everything to settlement ===
    finally:
        conn.close()


def buy_cheapest(markets):
    global _round
    open_positions = get_open_positions()

    # Check round budget: max 25% of STARTING_BALANCE (hard cap, never exceeds original cash)
    max_spend = STARTING_BALANCE * ROUND_BUDGET_PCT
    if max_spend <= 0:
        logger.info("RAZOR Waiting for round balance snapshot")
        return
    if _round['spent'] >= max_spend:
        logger.info(f"RAZOR Round budget spent: ${_round['spent']:.2f} / ${max_spend:.2f}")
        return

    # Check per-window buy cap
    if _round['buys'] >= MAX_BUYS_PER_WINDOW:
        logger.info(f"RAZOR Max buys this window ({MAX_BUYS_PER_WINDOW}) reached")
        return

    if len(open_positions) >= MAX_POSITIONS:
        logger.info(f"RAZOR Max positions ({MAX_POSITIONS}) reached")
        return

    held_tickers = {t['ticker'] for t in open_positions}
    candidates = find_cheapest(markets)
    candidates = [c for c in candidates if c['ticker'] not in held_tickers]

    if not candidates:
        logger.info("RAZOR No buy candidates")
        return

    best = candidates[0]
    buy_cost = best['price'] * CONTRACTS

    # Check if this buy would exceed round budget
    if _round['spent'] + buy_cost > max_spend:
        logger.info(f"RAZOR Round budget: ${_round['spent'] + buy_cost:.2f} would exceed ${max_spend:.2f}")
        return

    logger.info(f"RAZOR BEST: {best['ticker']} {best['side']} @ ${best['price']:.2f} ({best['mins_left']:.1f}min left) [strategy={SIDE_STRATEGY}]")

    result = place_order(best['ticker'], best['side'], 'buy', best['price'], CONTRACTS)
    if not result:
        return

    order_id, filled, fill_price = result
    if filled <= 0:
        return

    buy_fee = kalshi_fee(fill_price, filled)
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO scraper_trades (ticker, side, price, count, current_bid, fees) VALUES (%s, %s, %s, %s, %s, %s)",
                (best['ticker'], best['side'], float(fill_price), filled, float(fill_price), float(buy_fee))
            )
        _round['spent'] += fill_price * filled
        _round['buys'] += 1
        logger.info(f"RAZOR BOUGHT: {best['ticker']} {best['side']} x{filled} @ ${fill_price:.2f} fee=${buy_fee:.4f} | round: ${_round['spent']:.2f}/${max_spend:.2f} buys={_round['buys']}/{MAX_BUYS_PER_WINDOW}")
    finally:
        conn.close()


# === MAIN CYCLE ===

def refresh_cache():
    """Refresh cached Kalshi data for the dashboard."""
    global _cache
    try:
        resp = kalshi_get('/portfolio/balance')
        _cache['balance'] = {
            'balance': resp.get('balance', 0) / 100.0,
            'portfolio_value': resp.get('portfolio_value', 0) / 100.0,
        }
    except:
        pass

    try:
        all_pos = []
        cursor = None
        while True:
            url = '/portfolio/positions?limit=200&count_filter=position'
            if cursor:
                url += f'&cursor={cursor}'
            resp = kalshi_get(url)
            batch = resp.get('market_positions', [])
            all_pos.extend(batch)
            cursor = resp.get('cursor')
            if not cursor or not batch:
                break
        _cache['positions'] = all_pos
    except:
        pass

    _cache['updated'] = time.time()


def get_paper_balance():
    """Calculate balance from DB for paper mode."""
    try:
        conn = get_db()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT price, count FROM scraper_trades WHERE status='open'")
            open_trades = cur.fetchall()
            cur.execute("SELECT pnl FROM scraper_trades WHERE status='closed' AND pnl IS NOT NULL")
            closed_trades = cur.fetchall()
        conn.close()
        buy_cost = sum(sf(t['price']) * (t.get('count') or 1) for t in open_trades)
        total_pnl = sum(sf(t['pnl']) for t in closed_trades)
        return max(0, STARTING_BALANCE - buy_cost + total_pnl)
    except Exception as e:
        logger.error(f"Paper balance calc failed: {e}")
        return STARTING_BALANCE


def fetch_balance():
    """Get current cash balance — live from API or calculated for paper."""
    global _last_known_balance
    if ENABLE_TRADING:
        for attempt in range(3):
            try:
                resp = kalshi_get('/portfolio/balance')
                cash = resp.get('balance', 0) / 100.0
                _last_known_balance = cash
                return cash
            except Exception as e:
                logger.warning(f"RAZOR Balance fetch attempt {attempt+1}/3 failed: {e}")
                if attempt < 2:
                    time.sleep(1)
        # All retries failed — use last known
        if _last_known_balance > 0:
            logger.warning(f"RAZOR Using last known balance: ${_last_known_balance:.2f}")
            return _last_known_balance
        return 0
    else:
        return get_paper_balance()


def sync_positions():
    """Reconcile Kalshi API positions with local DB."""
    if not ENABLE_TRADING:
        return
    try:
        all_pos = []
        cursor = None
        while True:
            url = '/portfolio/positions?limit=200&count_filter=position'
            if cursor:
                url += f'&cursor={cursor}'
            resp = kalshi_get(url)
            batch = resp.get('market_positions', [])
            all_pos.extend(batch)
            cursor = resp.get('cursor')
            if not cursor or not batch:
                break

        conn = get_db()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT ticker FROM scraper_trades WHERE status = 'open'")
                db_tickers = {row['ticker'] for row in cur.fetchall()}

            # Import orphan Kalshi positions not in our DB
            for pos in all_pos:
                ticker = pos.get('ticker', '')
                position_fp = sf(pos.get('position_fp', '0'))
                if position_fp == 0 or '15M' not in ticker:
                    continue
                if ticker not in db_tickers:
                    side = 'yes' if position_fp > 0 else 'no'
                    count = int(abs(position_fp))
                    cost = abs(sf(pos.get('total_traded_dollars', '0')))
                    entry_per = cost / count if count > 0 else 0
                    if entry_per > 0:
                        with conn.cursor() as cur2:
                            cur2.execute(
                                "INSERT INTO scraper_trades (ticker, side, price, count, current_bid, fees, status) VALUES (%s, %s, %s, %s, %s, 0, 'open')",
                                (ticker, side, float(entry_per), count, float(entry_per))
                            )
                        logger.info(f"RAZOR SYNC: Imported orphan {ticker} {side} x{count} @ ${entry_per:.2f}")
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"RAZOR SYNC error: {e}")


def run_cycle():
    global _round

    mode = "LIVE" if ENABLE_TRADING else "PAPER"

    # Detect new 15-min window and snapshot balance
    now = datetime.now(timezone.utc)
    current_window = (now.hour * 4) + (now.minute // 15)
    if current_window != _round['window_id']:
        cash = fetch_balance()
        _round['start_balance'] = cash
        _round['spent'] = 0
        _round['buys'] = 0
        _round['window_id'] = current_window
        max_spend = STARTING_BALANCE * ROUND_BUDGET_PCT
        logger.info(f"RAZOR NEW ROUND [{mode}]: cash=${cash:.2f}, max spend=${max_spend:.2f} (25% of ${STARTING_BALANCE:.0f}), max buys={MAX_BUYS_PER_WINDOW}, side={SIDE_STRATEGY}")
        sync_positions()

    open_pos = get_open_positions()
    total_cost = sum(sf(t['price']) * (t.get('count') or 1) for t in open_pos)
    total_value = sum(sf(t.get('current_bid', 0)) * (t.get('count') or 1) for t in open_pos)
    max_spend = STARTING_BALANCE * ROUND_BUDGET_PCT
    logger.info(f"=== RAZOR [{mode}] === {len(open_pos)} pos | cost=${total_cost:.2f} | value=${total_value:.2f} | round: ${_round['spent']:.2f}/${max_spend:.2f} buys={_round['buys']}/{MAX_BUYS_PER_WINDOW}")
    check_sells()
    markets = fetch_all_markets()
    for m in markets:
        _cache['markets'][m.get('ticker', '')] = m
    logger.info(f"RAZOR Fetched {len(markets)} markets")
    buy_cheapest(markets)
    refresh_cache()


# === DASHBOARD API ===

@app.route('/')
def health():
    return 'OK'


@app.route('/api/status')
def api_status():
    try:
        # Balance: live from Kalshi cache, or paper from DB
        if ENABLE_TRADING:
            bal = _cache.get('balance', {})
            cash = bal.get('balance', 0)
            positions_value = bal.get('portfolio_value', 0)
        else:
            cash = get_paper_balance()
            open_trades = get_open_positions()
            positions_value = sum(sf(t.get('current_bid', 0)) * (t.get('count') or 1) for t in open_trades)
        portfolio = cash + positions_value

        # Open count from DB (works for both paper and live)
        open_positions = get_open_positions()
        total_fees = 0

        # Win/loss/cuts from OUR bot's DB only
        wins = 0
        losses = 0
        cuts = 0
        avg_win = 0
        avg_loss = 0
        bot_pnl = 0
        bot_fees = 0
        try:
            conn = get_db()
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT * FROM scraper_trades WHERE status='closed'")
                closed = cur.fetchall()
            conn.close()
            wins = sum(1 for t in closed if t.get('close_reason') == 'win')
            losses = sum(1 for t in closed if t.get('close_reason') == 'loss')
            cuts = sum(1 for t in closed if t.get('close_reason') == 'cut_loss')
            win_pnls = [sf(t['pnl']) for t in closed if sf(t.get('pnl', 0)) > 0]
            loss_pnls = [sf(t['pnl']) for t in closed if sf(t.get('pnl', 0)) < 0]
            avg_win = round(sum(win_pnls) / len(win_pnls), 4) if win_pnls else 0
            avg_loss = round(sum(loss_pnls) / len(loss_pnls), 4) if loss_pnls else 0
            bot_pnl = round(sum(sf(t['pnl']) for t in closed if t.get('pnl') is not None), 4)
            bot_fees = round(sum(sf(t.get('fees', 0)) for t in closed), 4)
            total_fees = bot_fees
        except:
            pass

        mode = "LIVE" if ENABLE_TRADING else "PAPER"
        return jsonify({
            'cash': round(cash, 2),
            'positions_value': round(positions_value, 2),
            'portfolio': round(portfolio, 2),
            'open_count': len(open_positions),
            'realized_pnl': bot_pnl,
            'total_fees': round(total_fees, 4),
            'wins': wins,
            'losses': losses,
            'cuts': cuts,
            'avg_win': avg_win,
            'avg_loss': avg_loss,
            'mode': mode,
        })
    except Exception as e:
        logger.error(f"RAZOR API status error: {e}")
        return jsonify({'error': str(e)})


@app.route('/api/positions')
def api_positions():
    """Open positions from DB with live market bids."""
    try:
        wml = mins_left_in_window()
        now = datetime.now(timezone.utc)
        results = []

        # Source of truth: our DB
        open_trades = get_open_positions()

        for trade in open_trades:
            ticker = trade['ticker']
            side = trade['side']
            entry_per = sf(trade['price'])
            count = trade.get('count') or 1
            fees = sf(trade.get('fees', 0))

            if entry_per <= 0:
                continue

            cost = entry_per * count

            # Live bid from cached market data
            market = _cache['markets'].get(ticker, {})
            current_bid = sf(trade.get('current_bid', 0))
            if market:
                if side == 'yes':
                    current_bid = sf(market.get('yes_bid_dollars', '0')) or current_bid
                else:
                    current_bid = sf(market.get('no_bid_dollars', '0')) or current_bid

            current_value = current_bid * count if current_bid > 0 else 0
            unrealized = round(current_value - cost, 4) if current_bid > 0 else 0
            gain_pct = ((current_bid - entry_per) / entry_per * 100) if entry_per > 0 and current_bid > 0 else 0

            bought_at = trade.get('bought_at')
            mins_held = 0
            if bought_at:
                if bought_at.tzinfo is None:
                    bought_at = bought_at.replace(tzinfo=timezone.utc)
                mins_held = (now - bought_at).total_seconds() / 60

            results.append({
                'ticker': ticker,
                'side': side,
                'count': count,
                'cost': round(cost, 2),
                'entry_per': round(entry_per, 2),
                'current_bid': round(current_bid, 2),
                'current_value': round(current_value, 2),
                'unrealized': unrealized,
                'fees': round(fees, 4),
                'gain_pct': round(gain_pct, 1),
                'mins_held': round(mins_held, 1),
                'window_mins_left': round(wml, 1),
                'cut_eligible': wml <= CUT_WHEN_MINS_LEFT,
            })

        results.sort(key=lambda x: x['gain_pct'], reverse=True)
        return jsonify(results)
    except Exception as e:
        logger.error(f"RAZOR API positions error: {e}")
        return jsonify([])


@app.route('/api/closed')
def api_closed():
    try:
        conn = get_db()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM scraper_trades WHERE status='closed' ORDER BY closed_at DESC LIMIT 50")
            trades = cur.fetchall()
        conn.close()

        results = []
        for t in trades:
            entry = sf(t['price'])
            bid = sf(t.get('current_bid', 0))
            count = t.get('count', 1) or 1
            pnl = sf(t.get('pnl', 0))
            fees = sf(t.get('fees', 0))
            cost = entry * count
            gain_pct = (pnl / cost * 100) if cost > 0 else 0
            results.append({
                'ticker': t['ticker'],
                'side': t['side'],
                'count': count,
                'entry': entry,
                'exit': bid,
                'pnl': pnl,
                'fees': fees,
                'gain_pct': round(gain_pct, 1),
                'reason': t.get('close_reason', ''),
                'closed_at': str(t.get('closed_at', '')),
            })
        return jsonify(results)
    except Exception as e:
        logger.error(f"RAZOR API closed error: {e}")
        return jsonify([])


@app.route('/api/analysis')
def api_analysis():
    """Win rate analysis by side, coin, and price bucket."""
    try:
        conn = get_db()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM scraper_trades WHERE status='closed'")
            trades = cur.fetchall()
        conn.close()

        if not trades:
            return jsonify({'message': 'No closed trades yet', 'total_trades': 0, 'by_side': {}, 'by_coin': {}, 'by_price_bucket': {}})

        def bucket(price):
            p = sf(price)
            if p <= 0.05: return '$0.01-0.05'
            if p <= 0.10: return '$0.06-0.10'
            if p <= 0.20: return '$0.11-0.20'
            if p <= 0.30: return '$0.21-0.30'
            return '$0.31-0.45'

        def coin_from_ticker(ticker):
            for s in CRYPTO_SERIES:
                prefix = s.replace('15M', '')
                if ticker.startswith(prefix):
                    return s
            return 'OTHER'

        def analyze_group(group):
            total = len(group)
            wins = sum(1 for t in group if t.get('close_reason') == 'win')
            losses = sum(1 for t in group if t.get('close_reason') == 'loss')
            pnl = round(sum(sf(t.get('pnl', 0)) for t in group), 4)
            fees = round(sum(sf(t.get('fees', 0)) for t in group), 4)
            return {
                'total': total, 'wins': wins, 'losses': losses,
                'win_rate': round(wins / total * 100, 1) if total > 0 else 0,
                'pnl': pnl, 'fees': fees,
            }

        by_side = {}
        for side in ('yes', 'no'):
            group = [t for t in trades if t.get('side') == side]
            if group:
                by_side[side] = analyze_group(group)

        by_coin = {}
        for t in trades:
            coin = coin_from_ticker(t.get('ticker', ''))
            by_coin.setdefault(coin, []).append(t)
        by_coin = {k: analyze_group(v) for k, v in by_coin.items()}

        by_bucket = {}
        for t in trades:
            b = bucket(t.get('price', 0))
            by_bucket.setdefault(b, []).append(t)
        by_bucket = {k: analyze_group(v) for k, v in by_bucket.items()}

        return jsonify({
            'total_trades': len(trades),
            'overall': analyze_group(trades),
            'by_side': by_side,
            'by_coin': by_coin,
            'by_price_bucket': by_bucket,
        })
    except Exception as e:
        logger.error(f"RAZOR API analysis error: {e}")
        return jsonify({'error': str(e)})


@app.route('/dashboard')
def dashboard():
    return DASHBOARD_HTML


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>RAZOR - Live</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&display=swap');
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0a0a0a;color:#e0e0e0;font-family:'JetBrains Mono',monospace;padding:16px 20px;font-size:13px}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
.live-dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:#00d673;margin-right:6px;animation:pulse 2s infinite}
.header{text-align:center;margin-bottom:14px;color:#555;font-size:11px}
.nav-link{position:absolute;top:16px;right:20px}

/* === TOP STATS === */
.pnl-box{background:#111;border:2px solid #1a1a1a;border-radius:8px;padding:20px;margin-bottom:14px;text-align:center}
.pnl-box .label{font-size:10px;text-transform:uppercase;letter-spacing:1px;color:#555;margin-bottom:4px}
.pnl-box .big{font-size:28px;font-weight:700}
.pnl-box.negative{border-color:#ff444444}
.pnl-box.positive{border-color:#00d67344}

.stat-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:14px}
.stat-card{background:#111;border:1px solid #1a1a1a;border-radius:6px;padding:12px 14px}
.stat-card .label{font-size:9px;text-transform:uppercase;letter-spacing:.5px;color:#555;margin-bottom:4px}
.stat-card .value{font-size:16px;font-weight:700}

.stat-grid-3{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:14px}

/* === PANELS === */
.panel{background:#111;border:1px solid #1a1a1a;border-radius:6px;overflow:hidden;margin-bottom:14px}
.panel-header{padding:10px 14px;border-bottom:1px solid #1a1a1a;display:flex;justify-content:space-between;align-items:center}
.panel-header h2{color:#ffaa00;font-size:12px;text-transform:uppercase;letter-spacing:1px}
.panel-header .count{color:#555;font-size:11px}
.panel-body{max-height:400px;overflow-y:auto}
table{width:100%;border-collapse:collapse;font-size:11px}
th{color:#555;text-align:left;padding:6px 8px;border-bottom:1px solid #222;text-transform:uppercase;font-size:9px;letter-spacing:.5px;position:sticky;top:0;background:#111}
td{padding:5px 8px;border-bottom:1px solid #141414}
tr:hover{background:#1a1a1a}

.green{color:#00d673}.red{color:#ff4444}.gray{color:#555}.orange{color:#ffaa00}
.tag{display:inline-block;padding:1px 6px;border-radius:3px;font-size:9px;font-weight:700}
.tag-win{background:#00d67322;color:#00d673}
.tag-loss{background:#ff444422;color:#ff4444}
.tag-cut{background:#ffaa0022;color:#ffaa00}
.tag-yes{background:#00d67322;color:#00d673}
.tag-no{background:#ff444422;color:#ff4444}
.tag-buy{background:#00d67322;color:#00d673}
.tag-sell{background:#ff444422;color:#ff4444}

.cut-bar{height:3px;background:#222;border-radius:2px;margin-top:3px;overflow:hidden}
.cut-fill{height:100%;border-radius:2px;transition:width .3s}
/* === ROUND TIMER === */
.round-timer{background:#111;border:1px solid #1a1a1a;border-radius:6px;padding:14px 16px;margin-bottom:14px}
.round-phases{display:flex;gap:8px;margin-bottom:8px}
.phase{flex:1;background:#0a0a0a;border:1px solid #222;border-radius:4px;padding:8px 10px;text-align:center}
.phase-label{font-size:8px;text-transform:uppercase;letter-spacing:.5px;color:#555;margin-bottom:4px}
.phase-time{font-size:18px;font-weight:700;color:#555}
.phase.active{border-color:#ffaa00}
.phase.active .phase-time{color:#ffaa00}
.phase-buy.active{border-color:#00d673}.phase-buy.active .phase-time{color:#00d673}
.phase-hold.active{border-color:#ffaa00}.phase-hold.active .phase-time{color:#ffaa00}
.phase-settle.active{border-color:#ff4444}.phase-settle.active .phase-time{color:#ff4444}
.phase.done{opacity:.4}
.round-bar{height:4px;background:#222;border-radius:2px;overflow:hidden;margin-bottom:6px}
.round-fill{height:100%;border-radius:2px;transition:width 1s linear;background:linear-gradient(90deg,#00d673,#ffaa00,#ff4444)}
.round-label{text-align:center;font-size:10px;color:#555}

.footer{text-align:center;color:#333;font-size:9px;margin-top:8px}
.panel-body::-webkit-scrollbar{width:4px}
.panel-body::-webkit-scrollbar-track{background:#111}
.panel-body::-webkit-scrollbar-thumb{background:#333;border-radius:2px}

@media(max-width:600px){
  .stat-grid{grid-template-columns:repeat(2,1fr)}
  .stat-grid-3{grid-template-columns:repeat(2,1fr)}
}
</style>
</head>
<body>


<div class="header">
  <span class="live-dot" id="mode-dot"></span>
  <span id="mode-label">RAZOR</span> &mdash; buy $0.01-$0.99, 100% settle, 25% budget cap
  &mdash; <span id="last-update">--</span>
</div>

<div class="round-timer">
  <div class="round-phases">
    <div class="phase phase-buy" id="phase-buy">
      <div class="phase-label">BUY WINDOW</div>
      <div class="phase-time" id="buy-time">--:--</div>
    </div>
    <div class="phase phase-hold" id="phase-hold">
      <div class="phase-label">HOLDING</div>
      <div class="phase-time" id="hold-time">--:--</div>
    </div>
    <div class="phase phase-settle" id="phase-settle">
      <div class="phase-label">CUT CHECK</div>
      <div class="phase-time" id="settle-time">--:--</div>
    </div>
  </div>
  <div class="round-bar"><div class="round-fill" id="round-fill"></div></div>
  <div class="round-label">
    <span id="round-status">--</span>
    &mdash; Next round: <span id="next-round" style="color:#ffaa00;font-weight:700">--:--</span>
  </div>
</div>

<div class="pnl-box" id="pnl-box">
  <div class="label">Portfolio</div>
  <div class="big" id="portfolio">$0.00</div>
  <div style="display:flex;justify-content:center;gap:32px;margin-top:8px;font-size:13px">
    <span>Cash: <span style="color:#fff;font-weight:700" id="cash">$0.00</span></span>
    <span>Positions: <span style="color:#fff;font-weight:700" id="positions-value">$0.00</span></span>
  </div>
</div>

<div class="stat-grid">
  <div class="stat-card">
    <div class="label">Open Positions</div>
    <div class="value orange" id="open-count">0</div>
  </div>
  <div class="stat-card">
    <div class="label">Record</div>
    <div class="value"><span class="green" id="wins">0</span>W / <span class="red" id="losses">0</span>L</div>
  </div>
  <div class="stat-card">
    <div class="label">Realized P&L</div>
    <div class="value" id="realized-pnl">$0.00</div>
  </div>
  <div class="stat-card">
    <div class="label">Fees Paid</div>
    <div class="value red" id="fees">$0.00</div>
  </div>
</div>

<div class="stat-grid-3">
  <div class="stat-card">
    <div class="label">Avg Win</div>
    <div class="value green" id="avg-win">$0.00</div>
  </div>
  <div class="stat-card">
    <div class="label">Avg Loss</div>
    <div class="value red" id="avg-loss">$0.00</div>
  </div>
  <div class="stat-card">
    <div class="label">Cuts</div>
    <div class="value orange" id="cuts">0</div>
  </div>
</div>

<div class="panel">
  <div class="panel-header"><h2>Open Positions</h2><div class="count" id="pos-label"></div></div>
  <div class="panel-body"><table><thead><tr>
    <th>Ticker</th><th>Side</th><th>Qty</th><th>Entry</th><th>Cost</th><th>Bid</th><th>Value</th><th>P&L</th><th>Gain</th><th>Held</th><th>Cut</th>
  </tr></thead><tbody id="pos-body"><tr><td colspan="11" class="gray" style="text-align:center;padding:20px">Loading...</td></tr></tbody></table></div>
</div>

<div class="panel">
  <div class="panel-header"><h2>Closed Trades</h2><div class="count" id="closed-label"></div></div>
  <div class="panel-body"><table><thead><tr>
    <th>Ticker</th><th>Side</th><th>Qty</th><th>Entry</th><th>Exit</th><th>P&L</th><th>Gain%</th><th>Fees</th><th>Result</th><th>When</th>
  </tr></thead><tbody id="closed-body"><tr><td colspan="10" class="gray" style="text-align:center;padding:20px">Loading...</td></tr></tbody></table></div>
</div>

<div class="footer"><span id="footer-mode">RAZOR</span> &mdash; live market prices from Kalshi API &mdash; auto-refresh 2s</div>

<script>
function $(id){return document.getElementById(id)}
function cls(v){return v>0?'green':v<0?'red':'gray'}
function esc(s){var d=document.createElement('div');d.textContent=s;return d.innerHTML}
function timeAgo(s){
  if(!s||s==='None')return '--';
  var diff=Math.floor((Date.now()-new Date(s).getTime())/1000);
  if(diff<0)diff=0;
  if(diff<60)return diff+'s ago';if(diff<3600)return Math.floor(diff/60)+'m ago';
  return Math.floor(diff/3600)+'h ago';
}

async function refresh(){
  try{
    var [status,positions,closed]=await Promise.all([
      fetch('/api/status').then(r=>r.json()),
      fetch('/api/positions').then(r=>r.json()),
      fetch('/api/closed').then(r=>r.json())
    ]);

    if(status&&!status.error){
      $('portfolio').textContent='$'+(status.portfolio||0).toFixed(2);
      $('cash').textContent='$'+(status.cash||0).toFixed(2);
      $('positions-value').textContent='$'+(status.positions_value||0).toFixed(2);

      $('open-count').textContent=status.open_count||0;
      $('wins').textContent=status.wins||0;
      $('losses').textContent=status.losses||0;
      var rp=status.realized_pnl||0;
      $('realized-pnl').textContent=(rp>=0?'+$':'-$')+Math.abs(rp).toFixed(4);
      $('realized-pnl').className='value '+cls(rp);
      $('fees').textContent='-$'+(status.total_fees||0).toFixed(4);
      $('avg-win').textContent='+$'+(status.avg_win||0).toFixed(4);
      $('avg-loss').textContent='-$'+Math.abs(status.avg_loss||0).toFixed(4);
      $('cuts').textContent=status.cuts||0;
      var mode=status.mode||'PAPER';
      $('mode-label').textContent='RAZOR ('+mode+')';
      $('footer-mode').textContent='RAZOR ('+mode+')';
      var dot=$('mode-dot');
      if(mode==='LIVE'){dot.style.background='#00d673'}else{dot.style.background='#ffaa00'}
    }

    if(positions){
      $('pos-label').textContent=positions.length+' positions';
      var h='';
      positions.forEach(function(p){
        var gc=cls(p.gain_pct);
        var uc=cls(p.unrealized);
        var wml=p.window_mins_left||15;
        var cutActive=wml<=5;
        var cutColor=cutActive?(p.gain_pct<=-70?'#ff4444':'#00d673'):'#ffaa00';
        var cutPct=cutActive?100:Math.min((10-wml)/5*100,100);
        h+='<tr>';
        h+='<td style="font-size:10px">'+esc(p.ticker)+'</td>';
        h+='<td><span class="tag tag-'+p.side+'">'+p.side.toUpperCase()+'</span></td>';
        h+='<td>'+p.count+'</td>';
        h+='<td>$'+p.entry_per.toFixed(2)+'</td>';
        h+='<td>$'+p.cost.toFixed(2)+'</td>';
        h+='<td>'+(p.current_bid>0?'$'+p.current_bid.toFixed(2):'--')+'</td>';
        h+='<td>'+(p.current_bid>0?'$'+p.current_value.toFixed(2):'--')+'</td>';
        h+='<td class="'+uc+'">'+(p.unrealized>=0?'+':'')+p.unrealized.toFixed(4)+'</td>';
        h+='<td class="'+gc+'">'+(p.gain_pct>=0?'+':'')+p.gain_pct.toFixed(0)+'%</td>';
        h+='<td>'+p.mins_held.toFixed(1)+'m</td>';
        h+='<td style="min-width:60px"><div class="cut-bar"><div class="cut-fill" style="width:'+cutPct+'%;background:'+cutColor+'"></div></div>';
        if(cutActive){
          h+=p.gain_pct<=-70?'<span class="red" style="font-size:9px">CUT</span>':'<span class="green" style="font-size:9px">OK</span>';
        }else{
          h+='<span class="gray" style="font-size:9px">'+wml.toFixed(0)+'m left</span>';
        }
        h+='</td></tr>';
      });
      $('pos-body').innerHTML=h||'<tr><td colspan="11" class="gray" style="text-align:center;padding:20px">No open positions</td></tr>';
    }

    if(closed){
      $('closed-label').textContent=closed.length+' trades';
      var h='';
      closed.forEach(function(t){
        var pc=cls(t.pnl);
        var gc=cls(t.gain_pct);
        var tag=t.reason==='win'?'tag-win':t.reason==='take_profit'?'tag-win':t.reason==='cut_loss'?'tag-cut':'tag-loss';
        var label=t.reason==='win'?'WIN':t.reason==='take_profit'?'TP':t.reason==='cut_loss'?'CUT':'LOSS';
        h+='<tr>';
        h+='<td style="font-size:10px">'+esc(t.ticker)+'</td>';
        h+='<td><span class="tag tag-'+t.side+'">'+t.side.toUpperCase()+'</span></td>';
        h+='<td>'+(t.count||1)+'</td>';
        h+='<td>$'+t.entry.toFixed(2)+'</td>';
        h+='<td>$'+(t.exit||0).toFixed(2)+'</td>';
        h+='<td class="'+pc+'">'+(t.pnl>=0?'+':'')+t.pnl.toFixed(4)+'</td>';
        h+='<td class="'+gc+'">'+(t.gain_pct>=0?'+':'')+t.gain_pct.toFixed(0)+'%</td>';
        h+='<td class="red">$'+(t.fees||0).toFixed(4)+'</td>';
        h+='<td><span class="tag '+tag+'">'+label+'</span></td>';
        h+='<td>'+timeAgo(t.closed_at)+'</td>';
        h+='</tr>';
      });
      $('closed-body').innerHTML=h||'<tr><td colspan="10" class="gray" style="text-align:center;padding:20px">No trades yet</td></tr>';
    }

    $('last-update').textContent=new Date().toLocaleTimeString();
  }catch(e){console.error(e)}
}

refresh();
setInterval(refresh,2000);

/* === ROUND TIMER === */
function updateRoundTimer(){
  var now=new Date();
  var utcMins=now.getUTCMinutes();
  var utcSecs=now.getUTCSeconds();

  /* 15-min windows: :00-:15, :15-:30, :30-:45, :45-:00 */
  var windowStart=Math.floor(utcMins/15)*15;
  var minsIntoWindow=utcMins-windowStart;
  var secsIntoWindow=minsIntoWindow*60+utcSecs;
  var totalWindowSecs=15*60;
  var secsLeft=totalWindowSecs-secsIntoWindow;

  /* Phases: Buy 0-5min, Hold 5-10min, Cut Check 10-15min (5 min left) */
  var buyEnd=5*60;      /* stop buying at 10min left = 5min in */
  var holdEnd=10*60;    /* cut check starts at 5 min left = 10min in */

  var pctDone=secsIntoWindow/totalWindowSecs*100;
  $('round-fill').style.width=pctDone+'%';

  /* Next round */
  var nextMins=Math.floor(secsLeft/60);
  var nextSecs=secsLeft%60;
  $('next-round').textContent=nextMins+':'+nextSecs.toString().padStart(2,'0');

  /* Phase times */
  var buyLeft=Math.max(0,buyEnd-secsIntoWindow);
  var holdLeft=Math.max(0,holdEnd-secsIntoWindow);
  var settleLeft=Math.max(0,totalWindowSecs-secsIntoWindow);

  function fmt(s){return Math.floor(s/60)+':'+Math.floor(s%60).toString().padStart(2,'0')}

  $('buy-time').textContent=buyLeft>0?fmt(buyLeft):'DONE';
  $('hold-time').textContent=secsIntoWindow>=buyEnd?(holdLeft>0?fmt(holdLeft):'DONE'):'--:--';
  $('settle-time').textContent=secsIntoWindow>=holdEnd?fmt(settleLeft):'--:--';

  /* Active/done states */
  var pb=$('phase-buy'),ph=$('phase-hold'),ps=$('phase-settle');
  pb.className='phase phase-buy'+(secsIntoWindow<buyEnd?' active':' done');
  ph.className='phase phase-hold'+(secsIntoWindow>=buyEnd&&secsIntoWindow<holdEnd?' active':(secsIntoWindow>=holdEnd?' done':''));
  ps.className='phase phase-settle'+(secsIntoWindow>=holdEnd?' active':'');

  /* Status text */
  if(secsIntoWindow<buyEnd){
    $('round-status').innerHTML='<span class="green">BUYING</span> \u2014 '+fmt(buyLeft)+' left';
  }else if(secsIntoWindow<holdEnd){
    $('round-status').innerHTML='<span class="orange">HOLDING</span> \u2014 '+fmt(holdLeft)+' to cut check';
  }else{
    $('round-status').innerHTML='<span class="red">CUT CHECK</span> \u2014 '+fmt(settleLeft)+' to settle';
  }
}
updateRoundTimer();
setInterval(updateRoundTimer,1000);
</script>
</body>
</html>"""


# === MAIN LOOP ===

PORT = int(os.environ.get('PORT', 8080))


def razor_loop():
    init_razor_db()
    mode = "LIVE" if ENABLE_TRADING else "PAPER"
    logger.info(f"RAZOR starting [{mode}] -- buy ${BUY_MIN}-${BUY_MAX}, side={SIDE_STRATEGY}, {ROUND_BUDGET_PCT*100:.0f}% budget, {MAX_BUYS_PER_WINDOW} buys/window")
    logger.info(f"RAZOR Series: {CRYPTO_SERIES}")
    sync_positions()

    while True:
        try:
            run_cycle()
        except Exception as e:
            logger.error(f"RAZOR Cycle error: {e}")
            traceback.print_exc()
        time.sleep(CYCLE_SECONDS)


if __name__ == '__main__':
    from threading import Thread
    bot_thread = Thread(target=razor_loop, daemon=True)
    bot_thread.start()
    app.run(host='0.0.0.0', port=PORT)
