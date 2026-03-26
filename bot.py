"""
Crypto scalper. Buy cheap 15M contracts settling within 20min.
Sell at +30%. No stop loss — ride to settlement. Keep it simple.
"""

import os, time, logging, traceback, math
from datetime import datetime, timezone
from flask import Flask, jsonify, render_template_string
from threading import Thread
import psycopg2
from psycopg2.extras import RealDictCursor
from kalshi_auth import KalshiAuth
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# === CONFIG ===
KALSHI_HOST = os.environ.get('KALSHI_API_HOST', 'https://api.elections.kalshi.com')
DATABASE_URL = os.environ.get('DATABASE_URL', 'postgresql://kalshi:kalshi@localhost:5432/kalshi')
PORT = int(os.environ.get('PORT', 8080))
ENABLE_TRADING = os.environ.get('ENABLE_TRADING', 'false').lower() == 'true'

# === STRATEGY ===
BUY_MIN = 0.01
BUY_MAX = 0.99
SELL_THRESHOLD = None        # No profit take — ride everything to settlement
TAKER_FEE_RATE = 0.07
MAX_MINS_TO_EXPIRY = 20
CYCLE_SECONDS = 2
STARTING_BALANCE = 100000.00
CASH_RESERVE = 0.50
SAVINGS_RATE = 0.25
MAX_BUYS_PER_CYCLE = 1000
CONTRACTS = 1
MAX_POSITIONS_PER_SERIES = 99999  # unlimited positions per series for data collection
MAX_ADDS = 5                  # can add to a winning position up to 5 times
ADD_CONTRACTS = 20            # double down with more contracts on momentum plays
ADD_MAX_AGE_MINS = 5          # only double down if position is < 5 min old

CRYPTO_SERIES = ['KXBTC15M', 'KXETH15M', 'KXSOL15M', 'KXXRP15M', 'KXDOGE15M', 'KXBTC1H']

# Hourly contracts need longer expiry window
SERIES_MAX_EXPIRY = {
    'KXBTC1H': 60,
}
DEFAULT_MAX_EXPIRY = MAX_MINS_TO_EXPIRY  # 20 min for 15M contracts

# === DATABASE ===

def get_db():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    return conn


def init_db():
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id SERIAL PRIMARY KEY,
                    ticker TEXT,
                    side TEXT,
                    action TEXT,
                    price NUMERIC,
                    count INTEGER,
                    current_bid NUMERIC,
                    pnl NUMERIC,
                    series TEXT,
                    mins_to_expiry NUMERIC,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            # Add columns if they don't exist (for existing tables)
            for col, typ in [('series', 'TEXT'), ('mins_to_expiry', 'NUMERIC'), ('batch_id', 'INTEGER')]:
                try:
                    cur.execute(f"ALTER TABLE trades ADD COLUMN {col} {typ}")
                except:
                    pass
            # Rounds tracking table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS rounds (
                    id SERIAL PRIMARY KEY,
                    started_at TIMESTAMPTZ DEFAULT NOW(),
                    ended_at TIMESTAMPTZ,
                    positions INTEGER,
                    total_cost NUMERIC,
                    total_value NUMERIC,
                    pnl NUMERIC,
                    pnl_pct NUMERIC,
                    peak_pnl NUMERIC,
                    exit_reason TEXT,
                    hold_pnl NUMERIC,
                    hold_pnl_pct NUMERIC
                )
            """)
            # Add hold columns if they don't exist
            for col, typ in [('hold_pnl', 'NUMERIC'), ('hold_pnl_pct', 'NUMERIC')]:
                try:
                    cur.execute(f"ALTER TABLE rounds ADD COLUMN {col} {typ}")
                except:
                    pass
            # Batches table — each cycle's buys are one batch
            cur.execute("""
                CREATE TABLE IF NOT EXISTS batches (
                    id SERIAL PRIMARY KEY,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    num_trades INTEGER DEFAULT 0,
                    total_cost NUMERIC DEFAULT 0,
                    peak_pnl NUMERIC DEFAULT 0,
                    status TEXT DEFAULT 'open',
                    closed_pnl NUMERIC,
                    closed_at TIMESTAMPTZ
                )
            """)
            # Shadow trades table — copies of trades that never get sold
            cur.execute("""
                CREATE TABLE IF NOT EXISTS shadow_trades (
                    id SERIAL PRIMARY KEY,
                    trade_id INTEGER,
                    round_id INTEGER,
                    ticker TEXT,
                    side TEXT,
                    price NUMERIC,
                    count INTEGER,
                    settled_pnl NUMERIC,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
    finally:
        conn.close()


# === LEARNING ENGINE ===

MIN_HISTORY = 20          # need at least 20 resolved trades before learning kicks in
MIN_WIN_RATE = 0.15       # only skip truly terrible combos (15%+ win rate to pass)

def get_win_rates():
    """Analyze last 1000 resolved trades and return win rates by price bucket, side, and series."""
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT price, side, series, mins_to_expiry, pnl
                FROM trades
                WHERE action = 'buy' AND pnl IS NOT NULL
                ORDER BY created_at DESC
                LIMIT 1000
            """)
            trades = cur.fetchall()
    finally:
        conn.close()

    if len(trades) < MIN_HISTORY:
        return None  # not enough data yet, skip learning

    stats = {
        'price_bucket': {},   # '0.00-0.10' -> {wins, total}
        'side': {},           # 'yes' -> {wins, total}
        'series': {},         # 'KXBTC15M' -> {wins, total}
        'time_bucket': {},    # 'early'/'mid'/'late' -> {wins, total}
    }

    for t in trades:
        price = float(t['price'] or 0)
        side = t['side'] or ''
        series = t['series'] or ''
        mins = float(t['mins_to_expiry'] or 10)
        won = float(t['pnl'] or 0) > 0

        # Price bucket
        if price < 0.10:
            pb = '0.00-0.10'
        elif price < 0.20:
            pb = '0.10-0.20'
        elif price < 0.30:
            pb = '0.20-0.30'
        elif price < 0.50:
            pb = '0.30-0.50'
        else:
            pb = '0.50-1.00'

        # Time bucket
        if mins > 10:
            tb = 'early'
        elif mins > 5:
            tb = 'mid'
        else:
            tb = 'late'

        for key, val in [('price_bucket', pb), ('side', side), ('series', series), ('time_bucket', tb)]:
            if val not in stats[key]:
                stats[key][val] = {'wins': 0, 'total': 0}
            stats[key][val]['total'] += 1
            if won:
                stats[key][val]['wins'] += 1

    # Convert to win rates
    rates = {}
    for category, buckets in stats.items():
        rates[category] = {}
        for bucket, data in buckets.items():
            rates[category][bucket] = data['wins'] / data['total'] if data['total'] > 0 else 0.5

    return rates


def score_candidate(price, side, series, mins_left, win_rates):
    """Score a candidate based on historical win rates. Returns average win rate across all factors."""
    if win_rates is None:
        return 1.0  # no history, allow everything

    scores = []

    # Price bucket score
    if price < 0.10:
        pb = '0.00-0.10'
    elif price < 0.20:
        pb = '0.10-0.20'
    elif price < 0.30:
        pb = '0.20-0.30'
    elif price < 0.50:
        pb = '0.30-0.50'
    else:
        pb = '0.50-1.00'
    scores.append(win_rates.get('price_bucket', {}).get(pb, 0.5))

    # Side score
    scores.append(win_rates.get('side', {}).get(side, 0.5))

    # Series score
    scores.append(win_rates.get('series', {}).get(series, 0.5))

    # Time bucket score
    if mins_left > 10:
        tb = 'early'
    elif mins_left > 5:
        tb = 'mid'
    else:
        tb = 'late'
    scores.append(win_rates.get('time_bucket', {}).get(tb, 0.5))

    return sum(scores) / len(scores)


# === INIT ===
init_db()
auth = KalshiAuth()
app = Flask(__name__)

current_hot_markets = []
current_win_rates = None


def sf(val):
    try:
        return float(val) if val is not None else 0.0
    except:
        return 0.0


def kalshi_fee(price, count):
    """Kalshi taker fee: 7% of P*(1-P) per contract, max $0.02/contract."""
    return min(math.ceil(TAKER_FEE_RATE * count * price * (1 - price) * 100) / 100, 0.02 * count)


# === KALSHI API (with retry) ===

def _make_session():
    s = requests.Session()
    retries = Retry(total=3, backoff_factor=1, status_forcelist=[500, 502, 503, 504])
    s.mount('https://', HTTPAdapter(max_retries=retries))
    return s

session = _make_session()


def kalshi_get(path):
    url = f"{KALSHI_HOST}/trade-api/v2{path}"
    headers = auth.get_headers("GET", f"/trade-api/v2{path}")
    resp = session.get(url, headers=headers, timeout=15)
    resp.raise_for_status()
    return resp.json()


def kalshi_post(path, data):
    url = f"{KALSHI_HOST}/trade-api/v2{path}"
    headers = auth.get_headers("POST", f"/trade-api/v2{path}")
    headers['Content-Type'] = 'application/json'
    resp = session.post(url, headers=headers, json=data, timeout=15)
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
        logger.info(f"PAPER {action.upper()}: {ticker} {side} x{count} @ ${price:.2f}")
        return ('paper', count)

    price_cents = int(round(price * 100))
    try:
        resp = kalshi_post('/portfolio/orders', {
            'ticker': ticker, 'action': action, 'side': side,
            'type': 'limit', 'count': count,
            'yes_price' if side == 'yes' else 'no_price': price_cents,
        })
        order = resp.get('order', {})
        order_id = order.get('order_id', '')
        status = order.get('status', '')
        filled = order.get('place_count', 0) - order.get('remaining_count', 0)
        if filled <= 0:
            filled = count if status in ('executed', 'filled') else 0
        logger.info(f"ORDER {action.upper()}: {ticker} status={status} filled={filled}/{count} id={order_id}")
        return (order_id, filled) if filled > 0 else None
    except Exception as e:
        logger.error(f"ORDER FAILED: {action.upper()} {ticker} -- {e}")
        return None


# === BALANCE ===

def get_kalshi_balance():
    try:
        resp = kalshi_get('/portfolio/balance')
        return resp.get('balance', 0) / 100.0
    except Exception as e:
        logger.error(f"Kalshi balance fetch failed: {e}")
        return None


def get_balance():
    if ENABLE_TRADING:
        real = get_kalshi_balance()
        if real is not None:
            return real
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT price, count FROM trades WHERE action = 'buy'")
            buys = cur.fetchall()
            buy_cost = sum(sf(t['price']) * (t.get('count') or 1) for t in buys)
            cur.execute("SELECT pnl FROM trades WHERE pnl IS NOT NULL")
            pnl_data = cur.fetchall()
            wins = sum(sf(t['pnl']) for t in pnl_data if sf(t['pnl']) > 0)
            losses = sum(sf(t['pnl']) for t in pnl_data if sf(t['pnl']) <= 0)
            deployable_pnl = wins * (1 - SAVINGS_RATE) + losses
            return max(0, STARTING_BALANCE - buy_cost + deployable_pnl)
    except Exception as e:
        logger.error(f"Balance calc failed: {e}")
        return 0.0
    finally:
        conn.close()


def get_open_positions():
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM trades WHERE action = 'buy' AND pnl IS NULL")
            return cur.fetchall()
    except Exception as e:
        logger.error(f"get_open_positions failed: {e}")
        return []
    finally:
        conn.close()


# === SELL LOGIC ===

def check_sells():
    logger.info("--- SELL CHECK ---")
    open_positions = get_open_positions()

    if not open_positions:
        logger.info("No open positions")
        return

    logger.info(f"Checking {len(open_positions)} open positions")
    sold = 0
    expired = 0

    for trade in open_positions:
        ticker = trade['ticker']
        side = trade['side']
        entry_price = sf(trade['price'])
        count = trade.get('count') or 1

        if entry_price <= 0:
            continue

        market = get_market(ticker)
        if not market:
            continue

        status = market.get('status', '')
        result_val = market.get('result', '')

        # === SETTLED ===
        if result_val:
            buy_fee = kalshi_fee(entry_price, count)
            if result_val == side:
                pnl = round((1.0 - entry_price) * count - buy_fee, 4)
                reason = "WIN settled @$1.00"
            else:
                pnl = round(-entry_price * count - buy_fee, 4)
                reason = "LOSS settled"
            logger.info(f"SETTLED: {ticker} {side} | {reason} | pnl=${pnl:.4f}")
            conn = get_db()
            try:
                with conn.cursor() as cur:
                    cur.execute("UPDATE trades SET pnl = %s WHERE id = %s", (float(pnl), trade['id']))
            except Exception as e:
                logger.error(f"Settle DB error: {e}")
            finally:
                conn.close()
            expired += 1
            continue

        # === CLOSED but no result yet ===
        if status in ('closed', 'settled', 'finalized'):
            logger.info(f"WAITING: {ticker} status={status}, no result yet")
            continue

        # Get current bid
        if side == 'yes':
            current_bid = sf(market.get('yes_bid_dollars', '0'))
        else:
            current_bid = sf(market.get('no_bid_dollars', '0'))

        # Update bid in DB for dashboard
        conn = get_db()
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE trades SET current_bid = %s WHERE id = %s", (float(current_bid), trade['id']))
        except:
            pass
        finally:
            conn.close()

        if current_bid <= 0:
            logger.info(f"SKIP: {ticker} bid=$0, waiting for settlement")
            continue

        gain = (current_bid - entry_price) / entry_price
        gain_pct = gain * 100

        logger.info(f"  POS: {ticker} {side} entry=${entry_price:.2f} bid=${current_bid:.2f} {gain_pct:+.0f}% x{count}")

        # Take profit (disabled if SELL_THRESHOLD is None)
        if SELL_THRESHOLD is not None and gain >= SELL_THRESHOLD:
            buy_fee = kalshi_fee(entry_price, count)
            sell_fee = kalshi_fee(current_bid, count)
            gross = round((current_bid - entry_price) * count, 4)
            pnl = round(gross - buy_fee - sell_fee, 4)

            logger.info(f"SELL: {ticker} {side} x{count} @ ${current_bid:.2f} | gross=${gross:.4f} fees=${buy_fee+sell_fee:.4f} net=${pnl:.4f}")

            result = place_order(ticker, side, 'sell', current_bid, count)
            if not result:
                logger.error(f"SELL FAILED: {ticker}")
                continue

            order_id, filled = result
            if filled < count:
                pnl = round((current_bid - entry_price) * filled - kalshi_fee(entry_price, filled) - kalshi_fee(current_bid, filled), 4)

            conn = get_db()
            try:
                with conn.cursor() as cur:
                    cur.execute("UPDATE trades SET pnl = %s, current_bid = %s WHERE id = %s",
                                (float(pnl), float(current_bid), trade['id']))
            except Exception as e:
                logger.error(f"Sell DB error: {e}")
            finally:
                conn.close()
            sold += 1

    logger.info(f"SELL SUMMARY: sold={sold} expired={expired}")


# === BUY LOGIC ===

def fetch_all_markets():
    all_markets = []
    for series in CRYPTO_SERIES:
        cursor = None
        try:
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
            logger.error(f"Fetch {series} failed: {e}")
    logger.info(f"Fetched {len(all_markets)} markets from {len(CRYPTO_SERIES)} series")
    return all_markets


def buy_candidates(markets):
    balance = get_balance()
    open_positions = get_open_positions()
    logger.info(f"Balance: ${balance:.2f} | {len(open_positions)} positions open")

    deployable = balance * (1.0 - CASH_RESERVE)
    if deployable <= 1.0:
        logger.info(f"Balance ${balance:.2f}, deployable ${deployable:.2f} too low -- skipping buys")
        return

    candidates = []
    now = datetime.now(timezone.utc)

    # Count open positions per series for caps
    series_position_counts = {}
    for t in open_positions:
        for s in CRYPTO_SERIES:
            if t.get('ticker', '').startswith(s.replace('15M', '').replace('1H', '')):
                series_position_counts[s] = series_position_counts.get(s, 0) + 1
                break

    for market in markets:
        ticker = market.get('ticker', '')

        # Determine which series this market belongs to
        market_series = None
        for s in CRYPTO_SERIES:
            if ticker.startswith(s):
                market_series = s
                break

        # Per-series position cap
        if market_series and series_position_counts.get(market_series, 0) >= MAX_POSITIONS_PER_SERIES:
            continue

        # Expiry filter — hourly gets longer window
        max_expiry = SERIES_MAX_EXPIRY.get(market_series, DEFAULT_MAX_EXPIRY) if market_series else DEFAULT_MAX_EXPIRY
        close_time = market.get('close_time') or market.get('expected_expiration_time')
        if close_time:
            try:
                close_dt = datetime.fromisoformat(close_time.replace('Z', '+00:00'))
                mins_left = (close_dt - now).total_seconds() / 60
                if mins_left > max_expiry or mins_left < 0:
                    continue
            except:
                continue
        else:
            continue

        yes_ask = float(market.get('yes_ask_dollars') or '999')
        yes_bid = float(market.get('yes_bid_dollars') or '0')
        no_ask = float(market.get('no_ask_dollars') or '999')
        no_bid = float(market.get('no_bid_dollars') or '0')

        logger.info(f"  MARKET: {ticker} yes=${yes_ask:.2f} no=${no_ask:.2f} mins_left={mins_left:.1f}")

        # Allow duplicate buys on same ticker

        # Buy cheapest side in range
        if yes_ask <= no_ask and BUY_MIN <= yes_ask <= BUY_MAX and yes_bid > 0:
            side, price, bid = 'yes', yes_ask, yes_bid
        elif BUY_MIN <= no_ask <= BUY_MAX and no_bid > 0:
            side, price, bid = 'no', no_ask, no_bid
        elif BUY_MIN <= yes_ask <= BUY_MAX and yes_bid > 0:
            side, price, bid = 'yes', yes_ask, yes_bid
        else:
            continue

        # Score candidate (for tracking only — does not block buys)
        win_score = score_candidate(price, side, market_series or '', mins_left, current_win_rates)

        candidates.append({'ticker': ticker, 'side': side, 'price': price, 'bid': bid, 'series': market_series or '', 'mins_left': mins_left, 'score': win_score})

    candidates.sort(key=lambda x: x['price'])
    candidates = candidates[:MAX_BUYS_PER_CYCLE]
    logger.info(f"Found {len(candidates)} buy candidates")

    bought = 0
    batch_id = None
    batch_cost = 0

    for c in candidates:
        if bought >= MAX_BUYS_PER_CYCLE:
            break

        cost = c['price'] * CONTRACTS
        if cost > deployable:
            logger.info(f"OUT OF CASH: need ${cost:.2f}, deployable ${deployable:.2f}")
            continue

        result = place_order(c['ticker'], c['side'], 'buy', c['price'], CONTRACTS)
        if not result:
            continue

        order_id, filled = result
        if filled <= 0:
            continue

        # Create batch on first buy of this cycle
        if batch_id is None:
            conn = get_db()
            try:
                with conn.cursor() as cur:
                    cur.execute("INSERT INTO batches (num_trades, total_cost) VALUES (0, 0) RETURNING id")
                    batch_id = cur.fetchone()[0]
            finally:
                conn.close()

        logger.info(f"BUY: {c['ticker']} {c['side']} x{filled} @ ${c['price']:.2f} batch={batch_id}")
        conn = get_db()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO trades (ticker, side, action, price, count, current_bid, series, mins_to_expiry, batch_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (c['ticker'], c['side'], 'buy', float(c['price']), filled, float(c['bid']), c.get('series', ''), round(c.get('mins_left', 0), 1), batch_id)
                )
            open_positions.append({'ticker': c['ticker'], 'price': c['price']})
            deployable -= cost
            batch_cost += cost
            bought += 1
        except Exception as e:
            logger.error(f"Buy DB insert failed: {e}")
        finally:
            conn.close()

    # Update batch totals
    if batch_id and bought > 0:
        conn = get_db()
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE batches SET num_trades = %s, total_cost = %s WHERE id = %s",
                            (bought, float(batch_cost), batch_id))
        finally:
            conn.close()

    logger.info(f"Bought {bought} positions" + (f" batch={batch_id}" if batch_id else ""))


def _get_volume(market):
    for key in ('volume', 'volume_24h'):
        val = market.get(key)
        if val is not None and val != '' and val != 0:
            try:
                return int(float(val))
            except:
                pass
    return 0


def update_hot_markets(markets):
    global current_hot_markets
    active = [m for m in markets if sf(m.get('yes_ask_dollars', '0')) < 0.99]
    by_vol = sorted(active, key=lambda m: _get_volume(m), reverse=True)[:10]
    current_hot_markets = [
        {'ticker': m.get('ticker', ''), 'yes_ask': sf(m.get('yes_ask_dollars', '0')),
         'no_ask': sf(m.get('no_ask_dollars', '0')), 'volume': _get_volume(m)}
        for m in by_vol
    ]


# === SMART LIQUIDATION ===

LIQUIDATE_CHECK_INTERVAL = 1   # check every cycle
LIQUIDATE_MIN_POSITIONS = 5    # need at least 5 positions before considering liquidation
LIQUIDATE_MIN_PROFIT_PCT = 10  # portfolio must be at least +10% unrealized to consider selling
LIQUIDATE_DROP_TRIGGER = 0.30  # sell if P&L drops 30% from peak (trailing stop)

_pnl_history = []              # track unrealized P&L over time
_peak_pnl = 0                  # highest unrealized P&L seen
_was_profitable = False        # whether we've been profitable this window
_green_streak = 0              # consecutive checks where P&L is positive

def check_batch_liquidations():
    """Check each open batch independently for liquidation."""
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Get all open batches
            cur.execute("SELECT * FROM batches WHERE status = 'open'")
            open_batches = cur.fetchall()
    finally:
        conn.close()

    for batch in open_batches:
        batch_id = batch['id']

        # Get trades for this batch
        conn = get_db()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT * FROM trades WHERE batch_id = %s AND action = 'buy' AND pnl IS NULL", (batch_id,))
                batch_trades = cur.fetchall()
        finally:
            conn.close()

        if not batch_trades:
            # All trades settled, close the batch
            conn = get_db()
            try:
                with conn.cursor() as cur:
                    cur.execute("UPDATE batches SET status = 'settled' WHERE id = %s", (batch_id,))
            finally:
                conn.close()
            continue

        if len(batch_trades) < 2:
            continue

        # Calculate batch P&L
        batch_cost = sum(sf(t.get('price')) * (t.get('count') or 1) for t in batch_trades)
        batch_value = sum(sf(t.get('current_bid')) * (t.get('count') or 1) for t in batch_trades)

        if batch_cost <= 0:
            continue

        batch_pnl = batch_value - batch_cost
        batch_pct = (batch_pnl / batch_cost) * 100
        peak = float(batch.get('peak_pnl') or 0)

        # Update peak
        if batch_pnl > peak:
            peak = batch_pnl
            conn = get_db()
            try:
                with conn.cursor() as cur:
                    cur.execute("UPDATE batches SET peak_pnl = %s WHERE id = %s", (float(peak), batch_id))
            finally:
                conn.close()

        peak_pct = (peak / batch_cost * 100) if batch_cost > 0 else 0

        logger.info(f"BATCH {batch_id}: {len(batch_trades)} trades pnl=${batch_pnl:.2f} ({batch_pct:+.1f}%) peak=${peak:.2f} ({peak_pct:+.1f}%)")

        # Skip if peak hasn't reached 20%
        if peak_pct < 20:
            continue

        should_sell = False
        reason = ''

        # Trailing stop: dropped 30% from peak
        if peak > 0 and batch_pnl < peak * (1 - LIQUIDATE_DROP_TRIGGER):
            should_sell = True
            reason = 'trailing_stop'
            logger.info(f"BATCH {batch_id} TRAILING STOP: pnl=${batch_pnl:.2f} dropped from peak=${peak:.2f}")

        # Profit lock: currently above 10%
        if batch_pct >= LIQUIDATE_MIN_PROFIT_PCT:
            should_sell = True
            reason = 'profit_lock'
            logger.info(f"BATCH {batch_id} PROFIT LOCK: +{batch_pct:.1f}%")

        if should_sell:
            liquidate_batch(batch_id, batch_trades, batch_cost, peak, reason)


def liquidate_batch(batch_id, trades, total_cost, peak, reason):
    """Sell all positions in a specific batch."""
    sold = 0
    total_pnl = 0

    for trade in trades:
        ticker = trade['ticker']
        side = trade['side']
        entry_price = sf(trade['price'])
        count = trade.get('count') or 1
        current_bid = sf(trade.get('current_bid'))

        if current_bid <= 0:
            current_bid = 0.001

        buy_fee = kalshi_fee(entry_price, count)
        sell_fee = kalshi_fee(current_bid, count)
        pnl = round((current_bid - entry_price) * count - buy_fee - sell_fee, 4)

        result = place_order(ticker, side, 'sell', current_bid, count)
        if result:
            order_id, filled = result
            if filled < count:
                pnl = round((current_bid - entry_price) * filled - kalshi_fee(entry_price, filled) - kalshi_fee(current_bid, filled), 4)

        conn = get_db()
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE trades SET pnl = %s, current_bid = %s WHERE id = %s",
                            (float(pnl), float(current_bid), trade['id']))
        except Exception as e:
            logger.error(f"Liquidate DB error: {e}")
        finally:
            conn.close()

        total_pnl += pnl
        sold += 1

    # Close the batch
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE batches SET status = 'closed', closed_pnl = %s, closed_at = NOW() WHERE id = %s",
                        (float(total_pnl), batch_id))
    finally:
        conn.close()

    total_value = total_cost + total_pnl
    pnl_pct = round((total_pnl / total_cost * 100), 1) if total_cost > 0 else 0
    logger.info(f"BATCH {batch_id} LIQUIDATED: {sold} trades, P&L=${total_pnl:.2f} ({pnl_pct:+.1f}%)")

    # Save to rounds
    round_id = _save_round(sold, total_cost, total_value, total_pnl, pnl_pct, peak, reason)
    if round_id:
        _save_shadow_trades(trades, round_id)


def _save_round(positions, cost, value, pnl, pnl_pct, peak, reason):
    """Save completed round to database. Returns round ID."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO rounds (ended_at, positions, total_cost, total_value, pnl, pnl_pct, peak_pnl, exit_reason) VALUES (NOW(), %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (positions, float(cost), float(value), float(pnl), float(pnl_pct), float(peak), reason)
            )
            return cur.fetchone()[0]
    except Exception as e:
        logger.error(f"Save round failed: {e}")
        return None
    finally:
        conn.close()


def _save_shadow_trades(positions, round_id):
    """Save copies of trades for hold-to-settlement comparison."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            for t in positions:
                cur.execute(
                    "INSERT INTO shadow_trades (trade_id, round_id, ticker, side, price, count) VALUES (%s, %s, %s, %s, %s, %s)",
                    (t.get('id'), round_id, t.get('ticker', ''), t.get('side', ''), float(sf(t.get('price'))), t.get('count') or 1)
                )
    except Exception as e:
        logger.error(f"Save shadow trades failed: {e}")
    finally:
        conn.close()


def _check_shadow_settlements():
    """Check if shadow trades have settled and update round hold_pnl."""
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Find rounds that don't have hold_pnl yet
            cur.execute("SELECT DISTINCT round_id FROM shadow_trades WHERE settled_pnl IS NULL")
            pending_rounds = [r['round_id'] for r in cur.fetchall()]

            for round_id in pending_rounds:
                cur.execute("SELECT * FROM shadow_trades WHERE round_id = %s", (round_id,))
                shadows = cur.fetchall()

                all_settled = True
                total_hold_pnl = 0

                for s in shadows:
                    if s['settled_pnl'] is not None:
                        total_hold_pnl += float(s['settled_pnl'])
                        continue

                    # Check if market has settled
                    try:
                        market = get_market(s['ticker'])
                        if not market:
                            all_settled = False
                            continue

                        result_val = market.get('result', '')
                        if not result_val:
                            all_settled = False
                            continue

                        entry = float(s['price'])
                        count = s['count'] or 1
                        buy_fee = kalshi_fee(entry, count)

                        if result_val == s['side']:
                            pnl = round((1.0 - entry) * count - buy_fee, 4)
                        else:
                            pnl = round(-entry * count - buy_fee, 4)

                        cur.execute("UPDATE shadow_trades SET settled_pnl = %s WHERE id = %s", (float(pnl), s['id']))
                        total_hold_pnl += pnl
                    except:
                        all_settled = False

                if all_settled and shadows:
                    total_cost = sum(float(s['price']) * (s['count'] or 1) for s in shadows)
                    hold_pct = round((total_hold_pnl / total_cost * 100), 1) if total_cost > 0 else 0
                    cur.execute("UPDATE rounds SET hold_pnl = %s, hold_pnl_pct = %s WHERE id = %s",
                                (float(total_hold_pnl), float(hold_pct), round_id))
                    logger.info(f"SHADOW SETTLED: round {round_id} hold_pnl=${total_hold_pnl:.2f} ({hold_pct:+.1f}%)")
    except Exception as e:
        logger.error(f"Shadow settlement check failed: {e}")
    finally:
        conn.close()


# === MAIN CYCLE ===

_cycle_count = 0

def run_cycle():
    global current_win_rates, _cycle_count
    _cycle_count += 1

    # Check shadow trade settlements every 10 cycles (~30 sec)
    if _cycle_count % 10 == 0:
        try:
            _check_shadow_settlements()
        except Exception as e:
            logger.error(f"Shadow check failed: {e}")

    # Refresh learning data every 10 cycles (~30 sec)
    if _cycle_count % 10 == 1:
        try:
            current_win_rates = get_win_rates()
            if current_win_rates:
                logger.info(f"LEARNING: updated win rates from history -- sides={current_win_rates.get('side',{})}")
        except Exception as e:
            logger.error(f"Learning update failed: {e}")

    mode = "PAPER" if not ENABLE_TRADING else "LIVE"
    balance = get_balance()
    logger.info(f"=== CYCLE START [{mode}] === Balance: ${balance:.2f}")

    # Per-batch liquidation check
    if _cycle_count % LIQUIDATE_CHECK_INTERVAL == 0:
        try:
            check_batch_liquidations()
        except Exception as e:
            logger.error(f"Batch liquidation check failed: {e}")

    check_sells()
    markets = fetch_all_markets()
    update_hot_markets(markets)
    buy_candidates(markets)
    balance = get_balance()
    logger.info(f"=== CYCLE END [{mode}] === Balance: ${balance:.2f}")


# === DASHBOARD API ===

@app.route('/')
def health():
    return 'OK'


@app.route('/api/status')
def api_status():
    try:
        cash = get_balance()
        open_positions = get_open_positions()
        positions_value = sum(sf(t.get('current_bid', 0)) * (t.get('count') or 1) for t in open_positions)
        portfolio = cash + positions_value

        conn = get_db()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT pnl, count FROM trades WHERE action = 'buy' AND pnl IS NOT NULL")
                resolved_data = cur.fetchall()

                cur.execute("SELECT count, price FROM trades WHERE action = 'buy'")
                all_buys = cur.fetchall()

                cur.execute("SELECT COALESCE(SUM(pnl), 0) as total, COUNT(*) as count FROM rounds")
                rounds_summary = cur.fetchone()
        finally:
            conn.close()

        total_pnl = sum(sf(t['pnl']) for t in resolved_data)
        wins = sum(1 for t in resolved_data if sf(t['pnl']) > 0)
        losses = sum(1 for t in resolved_data if sf(t['pnl']) <= 0)
        win_pnl = sum(sf(t['pnl']) for t in resolved_data if sf(t['pnl']) > 0)
        avg_return = round((total_pnl / len(resolved_data) * 100), 1) if resolved_data else 0
        avg_win = round(sum(sf(t['pnl']) for t in resolved_data if sf(t['pnl']) > 0) / max(wins, 1), 4)
        avg_loss = round(sum(sf(t['pnl']) for t in resolved_data if sf(t['pnl']) <= 0) / max(losses, 1), 4)
        savings = round(win_pnl * SAVINGS_RATE, 4)
        expired = sum(1 for t in open_positions if sf(t.get('current_bid', 0)) <= 0)

        total_contracts = sum((t.get('count') or 1) for t in all_buys)
        total_fees = sum(kalshi_fee(sf(t.get('price')), t.get('count') or 1) for t in all_buys)
        total_fees = round(total_fees, 4)
        pnl_after_fees = round(total_pnl, 4)  # fees already deducted from PnL

        mode = "PAPER" if not ENABLE_TRADING else "LIVE"

        # Live round P&L
        round_cost = sum(sf(t.get('price')) * (t.get('count') or 1) for t in open_positions)
        round_value = positions_value
        round_pnl = round(round_value - round_cost, 4)
        round_pct = round((round_pnl / round_cost * 100), 1) if round_cost > 0 else 0
        round_peak = round(_peak_pnl, 4)

        # Total P&L across all completed rounds
        all_rounds_pnl = round(float(rounds_summary['total']), 2)
        all_rounds_count = rounds_summary['count']

        # Overall = completed rounds + current round unrealized
        overall_pnl = round(all_rounds_pnl + round_pnl, 2)

        return jsonify({
            'portfolio': round(portfolio, 2),
            'cash': round(cash, 2),
            'positions_value': round(positions_value, 2),
            'all_rounds_pnl': all_rounds_pnl,
            'all_rounds_count': all_rounds_count,
            'overall_pnl': overall_pnl,
            'round_pnl': round_pnl,
            'round_pct': round_pct,
            'round_cost': round(round_cost, 2),
            'round_value': round(round_value, 2),
            'round_peak': round_peak,
            'round_positions': len(open_positions),
            'net_pnl': round(total_pnl, 4),
            'total_fees': total_fees,
            'pnl_after_fees': pnl_after_fees,
            'total_contracts': total_contracts,
            'wins': wins,
            'losses': losses,
            'expired': expired,
            'savings': savings,
            'open_count': len(open_positions),
            'mode': mode,
            'avg_return': avg_return,
            'avg_win': avg_win,
            'avg_loss': avg_loss,
        })
    except Exception as e:
        logger.error(f"API status error: {e}")
        return jsonify({'portfolio': 0, 'cash': 0, 'positions_value': 0, 'net_pnl': 0, 'total_fees': 0, 'pnl_after_fees': 0, 'total_contracts': 0, 'wins': 0, 'losses': 0, 'expired': 0, 'savings': 0, 'open_count': 0, 'mode': 'PAPER'})


@app.route('/api/open')
def api_open():
    try:
        positions = []
        for t in get_open_positions():
            price = sf(t.get('price'))
            current = sf(t.get('current_bid'))
            count = int(t.get('count') or 1)
            if price > 0 and current > 0:
                unrealized = round((current - price) * count, 4)
                gain_pct = round(((current - price) / price) * 100, 1)
            else:
                unrealized = 0
                gain_pct = 0
            positions.append({
                'ticker': t.get('ticker', ''),
                'side': t.get('side', ''),
                'strategy': 'FAV',
                'count': count,
                'entry': price,
                'entry_total': round(price * count, 2),
                'current_bid': current,
                'bid_total': round(current * count, 2),
                'unrealized': unrealized,
                'gain_pct': gain_pct,
            })
        positions.sort(key=lambda x: x['gain_pct'], reverse=True)
        return jsonify(positions)
    except Exception as e:
        logger.error(f"API open error: {e}")
        return jsonify([])


@app.route('/api/trades')
def api_trades():
    try:
        conn = get_db()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT * FROM trades WHERE action = 'buy' AND pnl IS NOT NULL ORDER BY created_at DESC LIMIT 50")
                result_data = cur.fetchall()
        finally:
            conn.close()
        trades = []
        for t in result_data:
            entry = sf(t.get('price'))
            exit_price = sf(t.get('current_bid'))
            gain_pct = round(((exit_price - entry) / entry) * 100, 1) if entry > 0 else 0
            trades.append({
                'created_at': t.get('created_at', ''),
                'ticker': t.get('ticker', ''),
                'side': t.get('side', ''),
                'count': t.get('count', 1),
                'entry': entry,
                'exit': exit_price,
                'pnl': sf(t.get('pnl')),
                'gain_pct': gain_pct,
            })
        return jsonify(trades)
    except Exception as e:
        logger.error(f"API trades error: {e}")
        return jsonify([])


@app.route('/api/hot')
def api_hot():
    return jsonify(current_hot_markets)


@app.route('/api/batches')
def api_batches():
    try:
        conn = get_db()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                # Open batches with live P&L
                cur.execute("""
                    SELECT b.id, b.created_at, b.num_trades, b.total_cost, b.peak_pnl, b.status,
                           COALESCE(SUM(CASE WHEN t.pnl IS NULL THEN t.current_bid * t.count ELSE 0 END), 0) as live_value,
                           COALESCE(SUM(CASE WHEN t.pnl IS NULL THEN t.price * t.count ELSE 0 END), 0) as live_cost,
                           COUNT(CASE WHEN t.pnl IS NULL THEN 1 END) as open_trades
                    FROM batches b
                    LEFT JOIN trades t ON t.batch_id = b.id AND t.action = 'buy'
                    WHERE b.status = 'open'
                    GROUP BY b.id
                    ORDER BY b.created_at DESC
                    LIMIT 20
                """)
                open_batches = cur.fetchall()

                cur.execute("SELECT * FROM batches WHERE status != 'open' ORDER BY closed_at DESC LIMIT 20")
                closed_batches = cur.fetchall()
        finally:
            conn.close()

        result = []
        for b in open_batches:
            cost = float(b.get('live_cost') or 0)
            value = float(b.get('live_value') or 0)
            pnl = round(value - cost, 2)
            pct = round((pnl / cost * 100), 1) if cost > 0 else 0
            result.append({
                'id': b['id'], 'status': 'open', 'created_at': str(b.get('created_at', '')),
                'trades': b.get('open_trades', 0), 'cost': round(cost, 2), 'value': round(value, 2),
                'pnl': pnl, 'pnl_pct': pct, 'peak': round(float(b.get('peak_pnl') or 0), 2),
            })
        for b in closed_batches:
            result.append({
                'id': b['id'], 'status': b.get('status', 'closed'), 'created_at': str(b.get('created_at', '')),
                'trades': b.get('num_trades', 0), 'cost': round(float(b.get('total_cost') or 0), 2),
                'pnl': round(float(b.get('closed_pnl') or 0), 2),
                'pnl_pct': round(float(b.get('closed_pnl') or 0) / float(b.get('total_cost') or 1) * 100, 1),
                'peak': round(float(b.get('peak_pnl') or 0), 2),
            })
        return jsonify(result)
    except Exception as e:
        logger.error(f"API batches error: {e}")
        return jsonify([])


@app.route('/api/learning')
def api_learning():
    try:
        rates = get_win_rates()
        if not rates:
            return jsonify({'active': False, 'message': 'Not enough data yet (need 20+ resolved trades)'})

        # Format for dashboard
        result = {'active': True, 'categories': {}}
        for category, buckets in rates.items():
            items = []
            for bucket, rate in sorted(buckets.items()):
                # Get trade count for this bucket
                items.append({
                    'label': bucket,
                    'win_rate': round(rate * 100, 1),
                    'pass': rate >= MIN_WIN_RATE,
                })
            result['categories'][category] = items
        result['min_win_rate'] = MIN_WIN_RATE * 100
        return jsonify(result)
    except Exception as e:
        logger.error(f"API learning error: {e}")
        return jsonify({'active': False, 'message': str(e)})


@app.route('/api/rounds')
def api_rounds():
    try:
        conn = get_db()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT * FROM rounds ORDER BY ended_at DESC LIMIT 50")
                rounds = cur.fetchall()
        finally:
            conn.close()
        result = []
        for r in rounds:
            result.append({
                'ended_at': str(r.get('ended_at', '')),
                'positions': r.get('positions', 0),
                'cost': float(r.get('total_cost') or 0),
                'value': float(r.get('total_value') or 0),
                'pnl': float(r.get('pnl') or 0),
                'pnl_pct': float(r.get('pnl_pct') or 0),
                'peak': float(r.get('peak_pnl') or 0),
                'exit_reason': r.get('exit_reason', ''),
                'hold_pnl': float(r.get('hold_pnl') or 0) if r.get('hold_pnl') is not None else None,
                'hold_pnl_pct': float(r.get('hold_pnl_pct') or 0) if r.get('hold_pnl_pct') is not None else None,
            })
        return jsonify(result)
    except Exception as e:
        logger.error(f"API rounds error: {e}")
        return jsonify([])


@app.route('/dashboard')
def dashboard():
    return DASHBOARD_HTML


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Kalshi Scalper</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&display=swap');
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0a0a0a;color:#e0e0e0;font-family:'JetBrains Mono','SF Mono','Fira Code',monospace;padding:16px 20px;font-size:13px}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
.live-dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;animation:pulse 2s infinite}
.dot-paper{background:#ffaa00}.dot-live{background:#00d673}
.top-bar{background:#111;border:1px solid #1a1a1a;border-radius:6px;padding:14px 20px;margin-bottom:14px;display:flex;justify-content:center;align-items:center;gap:32px;flex-wrap:wrap;font-size:14px;font-weight:700}
.panel{background:#111;border:1px solid #1a1a1a;border-radius:6px;overflow:hidden;margin-bottom:14px}
.panel-header{padding:10px 14px;border-bottom:1px solid #1a1a1a;display:flex;justify-content:space-between;align-items:center}
.panel-header h2{color:#ffaa00;font-size:12px;text-transform:uppercase;letter-spacing:1px}
.panel-header .count{color:#555;font-size:11px}
.panel-body{max-height:400px;overflow-y:auto}
table{width:100%;border-collapse:collapse;font-size:11px}
th{color:#555;text-align:left;padding:6px 8px;border-bottom:1px solid #222;text-transform:uppercase;font-size:9px;letter-spacing:.5px;position:sticky;top:0;background:#111}
td{padding:5px 8px;border-bottom:1px solid #141414}
tr.row-green{background:rgba(0,214,115,.04)}
tr.row-red{background:rgba(255,68,68,.04)}
tr:hover{background:#1a1a1a !important}
.green{color:#00d673}.red{color:#ff4444}.gray{color:#555}
.status-bar{background:#111;border:1px solid #1a1a1a;border-radius:6px;padding:10px 16px;display:flex;justify-content:space-between;flex-wrap:wrap;gap:8px;font-size:10px;color:#555}
.footer{text-align:center;color:#333;font-size:9px;margin-top:8px}
.loading{color:#555;text-align:center;padding:20px}
.panel-body::-webkit-scrollbar{width:4px}
.panel-body::-webkit-scrollbar-track{background:#111}
.panel-body::-webkit-scrollbar-thumb{background:#333;border-radius:2px}
</style>
</head>
<body>

<div style="text-align:center;margin-bottom:10px;color:#555;font-size:11px">
  <span class="live-dot dot-paper" id="mode-dot"></span>
  <span id="mode-label">PAPER MODE</span> &mdash; buy $0.01-$0.99 &mdash; no profit take &mdash; ride everything to settlement
  &mdash; NEXT: <span id="countdown" style="color:#ffaa00;font-weight:700">--:--</span>
</div>

<div class="top-bar" style="flex-direction:column;gap:6px">
  <div style="font-size:20px">OVERALL P&amp;L: <span id="tb-overall">...</span></div>
  <div style="font-size:12px;color:#888">Completed Rounds: <span id="tb-rounds-pnl">...</span> (<span id="tb-rounds-count">0</span> rounds) &nbsp;&nbsp; This Round: <span id="tb-thisround">...</span></div>
  <div style="font-size:12px;color:#888">Positions: <span id="tb-positions">...</span> &nbsp;&nbsp; Cash: <span id="tb-cash">...</span></div>
  <div style="font-size:12px">RECORD: <span id="tb-record">...</span> &nbsp;&nbsp; Fees: <span id="tb-fees" class="red">...</span></div>
  <div style="font-size:12px">AVG RETURN: <span id="tb-avgret">...</span> &nbsp;&nbsp; AVG WIN: <span id="tb-avgwin" class="green">...</span> &nbsp;&nbsp; AVG LOSS: <span id="tb-avgloss" class="red">...</span></div>
</div>

<div class="panel">
  <div class="panel-header"><h2>Live Batches</h2><div class="count" id="batch-count"></div></div>
  <div class="panel-body"><table><thead><tr>
    <th>Batch</th><th>Age</th><th>Trades</th><th>Cost</th><th>Value</th><th>P&amp;L</th><th>Return</th><th>Peak</th><th>Status</th>
  </tr></thead><tbody id="batch-body"><tr><td colspan="9" class="loading">Loading...</td></tr></tbody></table></div>
</div>

<div class="panel">
  <div class="panel-header"><h2>Learning Engine</h2><div class="count" id="learn-status"></div></div>
  <div class="panel-body" id="learn-body" style="padding:12px"><span class="loading">Loading...</span></div>
</div>

<div class="panel">
  <div class="panel-header"><h2>Open Positions</h2><div class="count" id="open-count"></div></div>
  <div class="panel-body"><table><thead><tr>
    <th>Ticker</th><th>Side</th><th>Qty</th><th>Entry</th><th>Cost</th><th>Bid</th><th>Value</th><th>P&amp;L</th><th>Gain%</th>
  </tr></thead><tbody id="open-body"><tr><td colspan="9" class="loading">Loading...</td></tr></tbody></table></div>
</div>

<div class="panel">
  <div class="panel-header"><h2>Recent Trades</h2><div class="count" id="trades-count"></div></div>
  <div class="panel-body"><table><thead><tr>
    <th>Time</th><th>Ticker</th><th>Side</th><th>Qty</th><th>Entry</th><th>Exit</th><th>P&amp;L</th><th>Gain%</th>
  </tr></thead><tbody id="trades-body"><tr><td colspan="8" class="loading">Loading...</td></tr></tbody></table></div>
</div>

<div class="panel">
  <div class="panel-header"><h2>Round History</h2><div class="count" id="rounds-count"></div></div>
  <div class="panel-body"><table><thead><tr>
    <th>Time</th><th>Positions</th><th>Cost</th><th>Sold P&amp;L</th><th>Sold %</th><th>Hold P&amp;L</th><th>Hold %</th><th>Peak</th><th>Exit</th>
  </tr></thead><tbody id="rounds-body"><tr><td colspan="8" class="loading">Loading...</td></tr></tbody></table></div>
</div>

<div class="panel">
  <div class="panel-header"><h2>Hot Markets</h2><div class="count" id="hot-count"></div></div>
  <div class="panel-body"><table><thead><tr>
    <th>Ticker</th><th>Yes Price</th><th>No Price</th><th>Volume</th>
  </tr></thead><tbody id="hot-body"><tr><td colspan="4" class="loading">Loading...</td></tr></tbody></table></div>
</div>

<div class="status-bar">
  <span>Buy $0.03-$0.12 | Sell +30% | No stop loss</span>
  <span>1 contract | 50% reserve | 25% savings</span>
  <span>Last: <span id="last-update">&mdash;</span></span>
</div>
<div class="footer">Kalshi Scalper v7 &mdash; auto-refresh 15s</div>

<script>
function $(id){return document.getElementById(id)}
function cls(v){return v>0?'green':v<0?'red':'gray'}
function esc(s){var d=document.createElement('div');d.textContent=s;return d.innerHTML}
async function fetchJSON(url){try{var r=await fetch(url);return await r.json()}catch(e){return null}}
function fmtVol(v){if(v>=1e6)return(v/1e6).toFixed(1)+'M';if(v>=1e3)return(v/1e3).toFixed(1)+'K';return v.toString()}
function timeAgo(iso){
  if(!iso)return '--';
  var diff=Math.floor((Date.now()-new Date(iso).getTime())/1000);
  if(diff<60)return diff+'s ago';if(diff<3600)return Math.floor(diff/60)+'m ago';
  if(diff<86400)return Math.floor(diff/3600)+'h ago';return Math.floor(diff/86400)+'d ago';
}

async function refresh(){
  var [status,open,trades,hot,rounds,learn,batches]=await Promise.all([
    fetchJSON('/api/status'),fetchJSON('/api/open'),fetchJSON('/api/trades'),fetchJSON('/api/hot'),fetchJSON('/api/rounds'),fetchJSON('/api/learning'),fetchJSON('/api/batches')
  ]);

  if(status){
    var ov=status.overall_pnl||0;
    $('tb-overall').innerHTML='<span class="'+cls(ov)+'" style="font-size:24px">'+(ov>=0?'+$':'-$')+Math.abs(ov).toFixed(2)+'</span>';
    var arp=status.all_rounds_pnl||0;
    $('tb-rounds-pnl').innerHTML='<span class="'+cls(arp)+'">'+(arp>=0?'+$':'-$')+Math.abs(arp).toFixed(2)+'</span>';
    $('tb-rounds-count').textContent=status.all_rounds_count||0;
    var trp=status.round_pnl||0;
    $('tb-thisround').innerHTML='<span class="'+cls(trp)+'">'+(trp>=0?'+$':'-$')+Math.abs(trp).toFixed(2)+'</span>';
    $('tb-positions').textContent='$'+(status.positions_value||0).toFixed(2);
    $('tb-cash').textContent='$'+(status.cash||0).toFixed(2);
    $('tb-fees').textContent='-$'+(status.total_fees||0).toFixed(2)+' ('+(status.total_contracts||0)+'c)';
    $('tb-record').innerHTML='<span class="green">'+(status.wins||0)+'W</span> / <span class="red">'+(status.losses||0)+'L</span>';
    var ar=status.avg_return||0;
    $('tb-avgret').innerHTML='<span class="'+cls(ar)+'">'+(ar>=0?'+':'')+ar.toFixed(1)+'%</span>';
    var rp=status.round_pnl||0;
    // old round fields removed — now using batches
    $('tb-avgwin').textContent='+$'+(status.avg_win||0).toFixed(4);
    $('tb-avgloss').textContent='-$'+Math.abs(status.avg_loss||0).toFixed(4);
    var mode=status.mode||'PAPER';
    $('mode-label').textContent=mode==='LIVE'?'LIVE TRADING':'PAPER MODE';
    $('mode-dot').className='live-dot '+(mode==='LIVE'?'dot-live':'dot-paper');
  }

  if(open){
    $('open-count').textContent=open.length+' positions';
    var h='';
    open.forEach(function(p){
      var rc=p.gain_pct>2?'row-green':p.gain_pct<-2?'row-red':'';
      var gc=cls(p.gain_pct);
      var bidText=p.current_bid<=0?'EXPIRED':'$'+p.current_bid.toFixed(2);
      var valText=p.current_bid<=0?'$0.00':'$'+(p.bid_total||0).toFixed(2);
      h+='<tr class="'+rc+'">';
      h+='<td style="font-size:10px">'+esc(p.ticker)+'</td>';
      h+='<td>'+esc(p.side)+'</td>';
      h+='<td>'+p.count+'</td>';
      h+='<td>$'+p.entry.toFixed(2)+'</td>';
      h+='<td>$'+(p.entry_total||0).toFixed(2)+'</td>';
      h+='<td>'+bidText+'</td>';
      h+='<td class="'+gc+'">'+valText+'</td>';
      h+='<td class="'+gc+'">'+(p.unrealized>=0?'+':'')+p.unrealized.toFixed(4)+'</td>';
      h+='<td class="'+gc+'">'+(p.gain_pct>=0?'+':'')+p.gain_pct.toFixed(0)+'%</td>';
      h+='</tr>';
    });
    $('open-body').innerHTML=h||'<tr><td colspan="9" class="gray" style="text-align:center">No open positions</td></tr>';
  }

  if(trades){
    $('trades-count').textContent=trades.length+' trades';
    var h='';
    trades.forEach(function(t){
      var pc=cls(t.pnl);var rc=t.pnl>0?'row-green':t.pnl<0?'row-red':'';
      h+='<tr class="'+rc+'">';
      h+='<td>'+timeAgo(t.created_at)+'</td>';
      h+='<td style="font-size:10px">'+esc(t.ticker||'')+'</td>';
      h+='<td>'+esc(t.side||'')+'</td>';
      h+='<td>'+(t.count||1)+'</td>';
      h+='<td>$'+(t.entry||0).toFixed(2)+'</td>';
      h+='<td>$'+(t.exit||0).toFixed(2)+'</td>';
      h+='<td class="'+pc+'">'+(t.pnl>=0?'+':'')+t.pnl.toFixed(4)+'</td>';
      h+='<td class="'+cls(t.gain_pct||0)+'">'+(t.gain_pct>=0?'+':'')+(t.gain_pct||0).toFixed(0)+'%</td>';
      h+='</tr>';
    });
    $('trades-body').innerHTML=h||'<tr><td colspan="8" class="gray" style="text-align:center">No trades yet</td></tr>';
  }

  if(hot){
    $('hot-count').textContent='Top '+hot.length+' by volume';
    var h='';
    hot.forEach(function(m){
      h+='<tr><td style="font-size:10px">'+esc(m.ticker)+'</td>';
      h+='<td>$'+(m.yes_ask||0).toFixed(2)+'</td>';
      h+='<td>$'+(m.no_ask||0).toFixed(2)+'</td>';
      h+='<td style="color:#ffaa00;font-weight:700">'+fmtVol(m.volume)+'</td></tr>';
    });
    $('hot-body').innerHTML=h||'<tr><td colspan="4" class="gray" style="text-align:center">No data yet</td></tr>';
  }
  if(batches){
    var openB=batches.filter(function(b){return b.status==='open'});
    var closedB=batches.filter(function(b){return b.status!=='open'});
    $('batch-count').textContent=openB.length+' open / '+closedB.length+' closed';
    var h='';
    batches.forEach(function(b){
      var pc=cls(b.pnl);var rc=b.pnl>0?'row-green':b.pnl<0?'row-red':'';
      var st=b.status==='open'?'<span style="color:#ffaa00">LIVE</span>':'<span class="'+pc+'">'+b.status+'</span>';
      h+='<tr class="'+rc+'">';
      h+='<td>#'+b.id+'</td>';
      h+='<td>'+timeAgo(b.created_at)+'</td>';
      h+='<td>'+b.trades+'</td>';
      h+='<td>$'+(b.cost||0).toFixed(2)+'</td>';
      h+='<td>$'+(b.value||0).toFixed(2)+'</td>';
      h+='<td class="'+pc+'">'+(b.pnl>=0?'+':'')+b.pnl.toFixed(2)+'</td>';
      h+='<td class="'+pc+'">'+(b.pnl_pct>=0?'+':'')+(b.pnl_pct||0).toFixed(1)+'%</td>';
      h+='<td class="green">+$'+(b.peak||0).toFixed(2)+'</td>';
      h+='<td>'+st+'</td>';
      h+='</tr>';
    });
    $('batch-body').innerHTML=h||'<tr><td colspan="9" class="gray" style="text-align:center">No batches yet</td></tr>';
  }
  if(rounds){
    $('rounds-count').textContent=rounds.length+' rounds';
    var h='';
    rounds.forEach(function(r){
      var pc=cls(r.pnl);var rc=r.pnl>0?'row-green':r.pnl<0?'row-red':'';
      var hp=r.hold_pnl!==null?r.hold_pnl:null;
      var hpc=hp!==null?cls(hp):'gray';
      h+='<tr class="'+rc+'">';
      h+='<td>'+timeAgo(r.ended_at)+'</td>';
      h+='<td>'+r.positions+'</td>';
      h+='<td>$'+(r.cost||0).toFixed(2)+'</td>';
      h+='<td class="'+pc+'">'+(r.pnl>=0?'+':'')+r.pnl.toFixed(2)+'</td>';
      h+='<td class="'+pc+'">'+(r.pnl_pct>=0?'+':'')+(r.pnl_pct||0).toFixed(1)+'%</td>';
      h+='<td class="'+hpc+'">'+(hp!==null?(hp>=0?'+':'')+hp.toFixed(2):'pending...')+'</td>';
      h+='<td class="'+hpc+'">'+(r.hold_pnl_pct!==null?(r.hold_pnl_pct>=0?'+':'')+(r.hold_pnl_pct||0).toFixed(1)+'%':'...')+'</td>';
      h+='<td class="green">+$'+(r.peak||0).toFixed(2)+'</td>';
      h+='<td>'+esc(r.exit_reason||'')+'</td>';
      h+='</tr>';
    });
    $('rounds-body').innerHTML=h||'<tr><td colspan="9" class="gray" style="text-align:center">No rounds yet</td></tr>';
  }
  if(learn){
    if(!learn.active){
      $('learn-status').textContent='collecting data...';
      $('learn-body').innerHTML='<span class="gray">'+esc(learn.message||'Waiting for data')+'</span>';
    }else{
      $('learn-status').textContent='ACTIVE (min '+learn.min_win_rate+'% win rate)';
      var h='<div style="display:flex;flex-wrap:wrap;gap:16px">';
      var labels={'price_bucket':'By Price','side':'By Side','series':'By Series','time_bucket':'By Timing'};
      for(var cat in learn.categories){
        h+='<div style="min-width:140px"><div style="color:#ffaa00;font-size:10px;text-transform:uppercase;margin-bottom:6px">'+(labels[cat]||cat)+'</div>';
        learn.categories[cat].forEach(function(item){
          var color=item.pass?'#00d673':'#ff4444';
          var icon=item.pass?'BUY':'SKIP';
          h+='<div style="font-size:11px;margin-bottom:3px"><span style="color:'+color+';font-weight:700;width:35px;display:inline-block">'+icon+'</span> ';
          h+=esc(item.label)+' <span style="color:'+color+'">'+item.win_rate+'%</span></div>';
        });
        h+='</div>';
      }
      h+='</div>';
      $('learn-body').innerHTML=h;
    }
  }
  $('last-update').textContent=new Date().toLocaleTimeString();
}

refresh();
setInterval(refresh,15000);

function updateCountdown(){
  var now=new Date(),mins=now.getMinutes();
  var nq=Math.ceil((mins+1)/15)*15;
  var next=new Date(now);
  if(nq>=60){next.setHours(now.getHours()+1,0,0,0)}else{next.setMinutes(nq,0,0)}
  var secs=Math.max(0,Math.floor((next-now)/1000));
  var m=Math.floor(secs/60),s=secs%60;
  $('countdown').textContent=m+':'+s.toString().padStart(2,'0');
}
updateCountdown();
setInterval(updateCountdown,1000);
</script>
</body>
</html>"""


# === MAIN ===

def bot_loop():
    mode = "PAPER" if not ENABLE_TRADING else "LIVE"
    sell_str = f"+{SELL_THRESHOLD*100:.0f}%" if SELL_THRESHOLD else "settlement"
    logger.info(f"Bot starting [{mode}] -- buy ${BUY_MIN}-${BUY_MAX}, sell {sell_str}, {CONTRACTS} contracts, {CASH_RESERVE*100:.0f}% reserve, {SAVINGS_RATE*100:.0f}% savings")
    logger.info(f"Series: {CRYPTO_SERIES}")

    while True:
        try:
            run_cycle()
        except Exception as e:
            logger.error(f"Cycle error: {e}")
            traceback.print_exc()
        time.sleep(CYCLE_SECONDS)


if __name__ == '__main__':
    bot_thread = Thread(target=bot_loop, daemon=True)
    bot_thread.start()
    app.run(host='0.0.0.0', port=PORT)
