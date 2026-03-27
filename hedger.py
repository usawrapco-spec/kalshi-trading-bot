"""
Hedger — Pool-based hedging strategy for Kalshi crypto 15-minute contracts.
Buy groups of 3 contracts spread across different coins (cheapest side).
When the pool is net +5%, sell everything and lock in profit.
"""

import os, time, logging, traceback, math, random
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
PORT = int(os.environ.get('HEDGER_PORT', 8081))
ENABLE_TRADING = os.environ.get('ENABLE_TRADING', 'false').lower() == 'true'

# === STRATEGY ===
STARTING_BALANCE = 20.00
BUY_MIN = 0.01
BUY_MAX = 0.55
CONTRACTS_PER_BUY = 3           # buy 3 contracts per pool per cycle
POOL_SIZE = 3                    # positions per pool
MAX_POOLS = 10                   # run up to 10 pools simultaneously
POOL_TAKE_PROFIT = 0.30          # sell pool when +30%
TAKER_FEE_RATE = 0.07
MAX_MINS_TO_EXPIRY = 15
MIN_MINS_TO_EXPIRY = 10
CYCLE_SECONDS = 2
CASH_RESERVE = 0.50
CRYPTO_SERIES = ['KXBTC15M', 'KXETH15M', 'KXSOL15M', 'KXXRP15M', 'KXDOGE15M']

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
                CREATE TABLE IF NOT EXISTS hedger_trades (
                    id SERIAL PRIMARY KEY,
                    ticker TEXT,
                    side TEXT,
                    action TEXT,
                    price NUMERIC,
                    count INTEGER DEFAULT 1,
                    current_bid NUMERIC,
                    pnl NUMERIC,
                    series TEXT,
                    mins_to_expiry NUMERIC,
                    round_id INTEGER,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            # Add columns if they don't exist
            for col, typ in [('series', 'TEXT'), ('mins_to_expiry', 'NUMERIC'), ('round_id', 'INTEGER'), ('current_bid', 'NUMERIC')]:
                try:
                    cur.execute(f"ALTER TABLE hedger_trades ADD COLUMN {col} {typ}")
                except:
                    pass
            cur.execute("""
                CREATE TABLE IF NOT EXISTS hedger_rounds (
                    id SERIAL PRIMARY KEY,
                    started_at TIMESTAMPTZ DEFAULT NOW(),
                    ended_at TIMESTAMPTZ,
                    positions INTEGER,
                    total_cost NUMERIC,
                    total_value NUMERIC,
                    pnl NUMERIC,
                    pnl_pct NUMERIC,
                    exit_reason TEXT
                )
            """)
    finally:
        conn.close()


# === INIT ===
init_db()
auth = KalshiAuth()
app = Flask(__name__)

_pool_sold_flag = False      # set True momentarily when pool sells for confetti


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
            cur.execute("SELECT price, count FROM hedger_trades WHERE action = 'buy' AND pnl IS NULL")
            buys = cur.fetchall()
            buy_cost = sum(sf(t['price']) * (t.get('count') or 1) for t in buys)
            cur.execute("SELECT pnl FROM hedger_trades WHERE pnl IS NOT NULL")
            pnl_data = cur.fetchall()
            total_pnl = sum(sf(t['pnl']) for t in pnl_data)
            return max(0, STARTING_BALANCE - buy_cost + total_pnl)
    except Exception as e:
        logger.error(f"Balance calc failed: {e}")
        return 0.0
    finally:
        conn.close()


def get_open_positions():
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM hedger_trades WHERE action = 'buy' AND pnl IS NULL")
            return cur.fetchall()
    except Exception as e:
        logger.error(f"get_open_positions failed: {e}")
        return []
    finally:
        conn.close()


# === ROUND/POOL MANAGEMENT ===

def get_open_pools():
    """Get all open pool (round) IDs."""
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT id FROM hedger_rounds WHERE ended_at IS NULL ORDER BY id")
            return [row['id'] for row in cur.fetchall()]
    finally:
        conn.close()


def create_new_pool():
    """Create a new pool (round) and return its ID."""
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("INSERT INTO hedger_rounds (started_at) VALUES (NOW()) RETURNING id")
            return cur.fetchone()['id']
    finally:
        conn.close()


def get_pool_positions(round_id):
    """Get open positions for a specific pool."""
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM hedger_trades WHERE round_id = %s AND pnl IS NULL", (round_id,))
            return cur.fetchall()
    finally:
        conn.close()


def close_round(round_id, positions, total_cost, total_value, pnl, pnl_pct, reason):
    """Close a specific pool/round."""
    global _pool_sold_flag
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE hedger_rounds SET ended_at = NOW(), positions = %s, total_cost = %s, total_value = %s, pnl = %s, pnl_pct = %s, exit_reason = %s WHERE id = %s",
                (positions, float(total_cost), float(total_value), float(pnl), float(pnl_pct), reason, round_id)
            )
        _pool_sold_flag = True
    except Exception as e:
        logger.error(f"Close round failed: {e}")
    finally:
        conn.close()


# === SETTLEMENT CHECK ===

def check_settlements():
    """Check open positions for settlement (expired contracts)."""
    open_positions = get_open_positions()
    if not open_positions:
        return

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
                    cur.execute("UPDATE hedger_trades SET pnl = %s WHERE id = %s", (float(pnl), trade['id']))
            finally:
                conn.close()
            continue

        # === CLOSED but no result yet ===
        if status in ('closed', 'settled', 'finalized'):
            continue

        # Update current bid for live tracking
        if side == 'yes':
            current_bid = sf(market.get('yes_bid_dollars', '0'))
        else:
            current_bid = sf(market.get('no_bid_dollars', '0'))

        conn = get_db()
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE hedger_trades SET current_bid = %s WHERE id = %s",
                            (float(current_bid), trade['id']))
        except:
            pass
        finally:
            conn.close()


# === POOL SELL CHECK ===

def check_pool_sell():
    """Check each open pool — if any is net +30%, sell it."""
    open_pools = get_open_pools()
    any_sold = False

    for pool_id in open_pools:
        positions = get_pool_positions(pool_id)
        if not positions:
            # Empty pool (all settled) — close it
            close_round(pool_id, 0, 0, 0, 0, 0, 'all_settled')
            continue

        total_cost = sum(sf(t.get('price')) * (t.get('count') or 1) for t in positions)
        total_value = sum(sf(t.get('current_bid')) * (t.get('count') or 1) for t in positions)

        if total_cost <= 0:
            continue

        pool_pct = (total_value - total_cost) / total_cost
        logger.info(f"POOL #{pool_id}: cost=${total_cost:.4f} value=${total_value:.4f} {pool_pct*100:+.1f}% ({len(positions)} pos)")

        if pool_pct >= POOL_TAKE_PROFIT:
            logger.info(f"POOL #{pool_id} TAKE PROFIT: +{pool_pct*100:.1f}% -- selling all {len(positions)} positions")
            total_pnl = 0
            sold = 0
            for trade in positions:
                entry = sf(trade['price'])
                bid = sf(trade.get('current_bid'))
                count = trade.get('count') or 1
                if bid <= 0:
                    bid = 0.001
                buy_fee = kalshi_fee(entry, count)
                sell_fee = kalshi_fee(bid, count)
                pnl = round((bid - entry) * count - buy_fee - sell_fee, 4)

                place_order(trade['ticker'], trade['side'], 'sell', bid, count)
                conn = get_db()
                try:
                    with conn.cursor() as cur:
                        cur.execute("UPDATE hedger_trades SET pnl = %s, current_bid = %s WHERE id = %s",
                                    (float(pnl), float(bid), trade['id']))
                finally:
                    conn.close()
                total_pnl += pnl
                sold += 1

            logger.info(f"POOL #{pool_id} SOLD: {sold} positions, P&L=${total_pnl:.4f}")
            close_round(pool_id, sold, total_cost, total_value, round(total_pnl, 4), round(pool_pct * 100, 1), 'pool_take_profit')
            any_sold = True

    return any_sold


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


def buy_pool_contracts(markets):
    """Fill existing pools and create new ones up to MAX_POOLS."""
    balance = get_balance()
    open_pools = get_open_pools()
    all_open = get_open_positions()
    logger.info(f"Balance: ${balance:.2f} | {len(all_open)} positions across {len(open_pools)} pools")

    # Find a pool that needs filling, or create a new one
    target_pool = None
    pool_positions = []

    for pool_id in open_pools:
        positions = get_pool_positions(pool_id)
        if len(positions) < POOL_SIZE:
            target_pool = pool_id
            pool_positions = positions
            break

    need_new_pool = target_pool is None
    if need_new_pool:
        if len(open_pools) >= MAX_POOLS:
            logger.info(f"MAX POOLS ({MAX_POOLS}) reached -- waiting for sells or settlements")
            return
        # Don't create yet — wait until we confirm there are candidates

    slots = min(CONTRACTS_PER_BUY, POOL_SIZE - len(pool_positions))
    if slots <= 0:
        return

    deployable = balance * (1.0 - CASH_RESERVE)
    if deployable <= 0.10:
        logger.info(f"Balance ${balance:.2f}, deployable ${deployable:.2f} too low -- skipping buys")
        return

    now = datetime.now(timezone.utc)
    open_tickers = set(t.get('ticker', '') for t in all_open)
    open_sides = [t.get('side', '') for t in pool_positions]
    yes_count = sum(1 for s in open_sides if s == 'yes')
    no_count = sum(1 for s in open_sides if s == 'no')

    # Build candidates per series (coin), with both sides available
    candidates_by_series = {}
    for market in markets:
        ticker = market.get('ticker', '')

        # Skip if we already hold this exact ticker
        if ticker in open_tickers:
            continue

        # Figure out which series
        market_series = None
        for s in CRYPTO_SERIES:
            if ticker.startswith(s):
                market_series = s
                break
        if not market_series:
            continue

        # Expiry filter
        close_time = market.get('close_time') or market.get('expected_expiration_time')
        if not close_time:
            continue
        try:
            close_dt = datetime.fromisoformat(close_time.replace('Z', '+00:00'))
            mins_left = (close_dt - now).total_seconds() / 60
            if mins_left > MAX_MINS_TO_EXPIRY or mins_left < MIN_MINS_TO_EXPIRY:
                continue
        except:
            continue

        yes_ask = float(market.get('yes_ask_dollars') or '999')
        no_ask = float(market.get('no_ask_dollars') or '999')

        # Determine which side to buy based on pool balance
        # Force mixed sides: if pool already has more YES, buy NO (and vice versa)
        if yes_count > no_count:
            # Need more NO — only consider NO side
            if BUY_MIN <= no_ask <= BUY_MAX:
                side = 'no'
                price = no_ask
            else:
                continue
        elif no_count > yes_count:
            # Need more YES — only consider YES side
            if BUY_MIN <= yes_ask <= BUY_MAX:
                side = 'yes'
                price = yes_ask
            else:
                continue
        else:
            # Pool is balanced — pick cheapest side
            if yes_ask <= no_ask and BUY_MIN <= yes_ask <= BUY_MAX:
                side = 'yes'
                price = yes_ask
            elif no_ask < yes_ask and BUY_MIN <= no_ask <= BUY_MAX:
                side = 'no'
                price = no_ask
            else:
                continue

        if market_series not in candidates_by_series:
            candidates_by_series[market_series] = []
        candidates_by_series[market_series].append({
            'ticker': ticker,
            'side': side,
            'price': price,
            'series': market_series,
            'mins_left': mins_left,
        })

    if not candidates_by_series:
        logger.info("No buy candidates found")
        return

    # Sort each series by price (cheapest first)
    for s in candidates_by_series:
        candidates_by_series[s].sort(key=lambda c: c['price'])

    # Round-robin across different series to spread risk
    series_list = list(candidates_by_series.keys())
    random.shuffle(series_list)

    # Create pool now that we have candidates
    if need_new_pool:
        target_pool = create_new_pool()
        logger.info(f"Created new pool #{target_pool}")
    round_id = target_pool
    bought = 0

    series_idx = 0
    attempts = 0
    max_attempts = len(series_list) * 3  # avoid infinite loop

    while bought < slots and attempts < max_attempts:
        series = series_list[series_idx % len(series_list)]
        series_idx += 1
        attempts += 1

        cands = candidates_by_series.get(series, [])
        if not cands:
            continue

        cand = cands.pop(0)
        price = cand['price']
        cost = price + kalshi_fee(price, 1)

        if cost > deployable:
            logger.info(f"  SKIP: {cand['ticker']} costs ${cost:.2f}, only ${deployable:.2f} deployable")
            continue

        result = place_order(cand['ticker'], cand['side'], 'buy', price, 1)
        if not result:
            continue

        order_id, filled = result
        if filled <= 0:
            continue

        fee = kalshi_fee(price, filled)
        conn = get_db()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO hedger_trades (ticker, side, action, price, count, series, mins_to_expiry, round_id, current_bid) VALUES (%s, %s, 'buy', %s, %s, %s, %s, %s, %s)",
                    (cand['ticker'], cand['side'], float(price), filled, cand['series'], round(cand['mins_left'], 1), round_id, float(price))
                )
        finally:
            conn.close()

        deployable -= cost
        bought += 1
        # Update side counts for mixed-side balancing
        if cand['side'] == 'yes':
            yes_count += 1
        else:
            no_count += 1
        logger.info(f"  BOUGHT: {cand['ticker']} {cand['side']} x{filled} @ ${price:.2f} (fee ${fee:.4f}) [{cand['series']}]")

    logger.info(f"BUY SUMMARY: bought {bought}/{slots} contracts across {len(set(c['series'] for c in [] ))} series")


# === MAIN CYCLE ===

def run_cycle():
    mode = "PAPER" if not ENABLE_TRADING else "LIVE"
    balance = get_balance()
    logger.info(f"=== HEDGER CYCLE [{mode}] === Balance: ${balance:.2f}")

    # 1. Check settlements
    check_settlements()

    # 2. Check each pool for take profit
    check_pool_sell()

    # 3. Buy more contracts — fill existing pools or create new ones
    markets = fetch_all_markets()
    buy_pool_contracts(markets)

    balance = get_balance()
    logger.info(f"=== HEDGER CYCLE END [{mode}] === Balance: ${balance:.2f}")


# === DASHBOARD API ===

@app.route('/')
def health():
    return jsonify({'status': 'ok', 'bot': 'hedger', 'mode': 'PAPER' if not ENABLE_TRADING else 'LIVE'})


@app.route('/api/status')
def api_status():
    try:
        cash = get_balance()
        open_positions = get_open_positions()
        positions_value = sum(sf(t.get('current_bid', 0)) * (t.get('count') or 1) for t in open_positions)
        portfolio = cash + positions_value

        total_cost = sum(sf(t.get('price')) * (t.get('count') or 1) for t in open_positions)
        pool_pnl = round(positions_value - total_cost, 4) if total_cost > 0 else 0
        pool_pct = round((pool_pnl / total_cost * 100), 2) if total_cost > 0 else 0

        conn = get_db()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT pnl FROM hedger_trades WHERE pnl IS NOT NULL")
                resolved = cur.fetchall()
                cur.execute("SELECT COALESCE(SUM(pnl), 0) as total, COUNT(*) as cnt FROM hedger_rounds WHERE ended_at IS NOT NULL")
                rounds_summary = cur.fetchone()
        finally:
            conn.close()

        total_resolved_pnl = sum(sf(t['pnl']) for t in resolved)
        wins = sum(1 for t in resolved if sf(t['pnl']) > 0)
        losses = sum(1 for t in resolved if sf(t['pnl']) <= 0)
        overall_pnl = round((cash + positions_value) - STARTING_BALANCE, 4)

        global _pool_sold_flag
        confetti = _pool_sold_flag
        _pool_sold_flag = False

        return jsonify({
            'portfolio': round(portfolio, 2),
            'cash': round(cash, 2),
            'positions_value': round(positions_value, 4),
            'overall_pnl': round(overall_pnl, 4),
            'pool_positions': len(open_positions),
            'pool_cost': round(total_cost, 4),
            'pool_value': round(positions_value, 4),
            'pool_pnl': pool_pnl,
            'pool_pct': pool_pct,
            'pool_target': POOL_TAKE_PROFIT * 100,
            'max_pool': POOL_SIZE,
            'max_pools': MAX_POOLS,
            'active_pools': len([r for r in get_open_pools()]),
            'resolved_pnl': round(total_resolved_pnl, 4),
            'wins': wins,
            'losses': losses,
            'rounds_pnl': round(float(rounds_summary['total']), 4),
            'rounds_count': rounds_summary['cnt'],
            'mode': 'PAPER' if not ENABLE_TRADING else 'LIVE',
            'confetti': confetti,
        })
    except Exception as e:
        logger.error(f"API status error: {e}")
        return jsonify({'portfolio': 0, 'cash': 0, 'overall_pnl': 0, 'pool_positions': 0, 'pool_pct': 0, 'mode': 'PAPER'})


@app.route('/api/pool')
def api_pool():
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
                'id': t['id'],
                'ticker': t.get('ticker', ''),
                'side': t.get('side', ''),
                'price': float(price),
                'current_bid': float(current),
                'count': count,
                'series': t.get('series', ''),
                'unrealized': unrealized,
                'gain_pct': gain_pct,
                'mins_to_expiry': float(t.get('mins_to_expiry') or 0),
                'created_at': str(t.get('created_at', '')),
            })
        positions.sort(key=lambda p: p['gain_pct'], reverse=True)
        return jsonify(positions)
    except Exception as e:
        logger.error(f"API pool error: {e}")
        return jsonify([])


@app.route('/api/rounds')
def api_rounds():
    try:
        conn = get_db()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT * FROM hedger_rounds WHERE ended_at IS NOT NULL ORDER BY ended_at DESC LIMIT 50")
                rows = cur.fetchall()
        finally:
            conn.close()
        result = []
        for r in rows:
            result.append({
                'id': r['id'],
                'ended_at': str(r.get('ended_at', '')),
                'positions': r.get('positions', 0),
                'cost': round(float(r.get('total_cost') or 0), 4),
                'value': round(float(r.get('total_value') or 0), 4),
                'pnl': round(float(r.get('pnl') or 0), 4),
                'pnl_pct': round(float(r.get('pnl_pct') or 0), 1),
                'exit_reason': r.get('exit_reason', ''),
            })
        return jsonify(result)
    except Exception as e:
        logger.error(f"API rounds error: {e}")
        return jsonify([])


@app.route('/api/history')
def api_history():
    """All resolved trades."""
    try:
        conn = get_db()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT * FROM hedger_trades WHERE pnl IS NOT NULL ORDER BY created_at DESC LIMIT 100")
                rows = cur.fetchall()
        finally:
            conn.close()
        result = []
        for t in rows:
            result.append({
                'id': t['id'],
                'ticker': t.get('ticker', ''),
                'side': t.get('side', ''),
                'price': float(t.get('price') or 0),
                'pnl': float(t.get('pnl') or 0),
                'series': t.get('series', ''),
                'created_at': str(t.get('created_at', '')),
            })
        return jsonify(result)
    except Exception as e:
        logger.error(f"API history error: {e}")
        return jsonify([])


@app.route('/dashboard')
def dashboard():
    return DASHBOARD_HTML


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Hedger - Pool Trading Terminal</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;500;600;700;800&display=swap');
*{margin:0;padding:0;box-sizing:border-box}
:root{
  --bg:#06080d;--bg1:#0c1017;--bg2:#111820;--bg3:#1a2130;
  --border:#1a2235;--border2:#243050;
  --text:#c8d0e0;--text2:#6a7490;--text3:#3a4260;
  --green:#00e68a;--green2:#00cc7a;--green-bg:rgba(0,230,138,.06);--green-bg2:rgba(0,230,138,.12);
  --red:#ff4466;--red2:#ee3355;--red-bg:rgba(255,68,102,.06);--red-bg2:rgba(255,68,102,.12);
  --gold:#f0b040;--gold2:#e0a030;--gold-bg:rgba(240,176,64,.08);
  --blue:#4488ff;--cyan:#40d0e0;--purple:#a855f7;
}
body{background:var(--bg);color:var(--text);font-family:'JetBrains Mono',monospace;font-size:12px;line-height:1.5;min-height:100vh;display:flex;flex-direction:column}

@keyframes pulse{0%,100%{opacity:1}50%{opacity:.25}}
@keyframes fadeIn{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:translateY(0)}}
@keyframes confetti-fall{0%{transform:translateY(-100vh) rotate(0deg);opacity:1}100%{transform:translateY(100vh) rotate(720deg);opacity:0}}

.live-dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;animation:pulse 1.8s ease-in-out infinite;vertical-align:middle}
.dot-paper{background:var(--gold);box-shadow:0 0 8px var(--gold)}
.dot-live{background:var(--green);box-shadow:0 0 8px var(--green)}

.header-bar{background:var(--bg1);border-bottom:1px solid var(--border);padding:10px 24px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px}
.header-left{display:flex;align-items:center;gap:16px}
.brand{font-size:13px;font-weight:700;color:var(--purple);letter-spacing:2px;text-transform:uppercase}
.mode-badge{font-size:10px;padding:3px 10px;border-radius:3px;font-weight:600;letter-spacing:1px}
.mode-paper{background:rgba(240,176,64,.15);color:var(--gold);border:1px solid rgba(240,176,64,.3)}
.mode-live{background:rgba(0,230,138,.15);color:var(--green);border:1px solid rgba(0,230,138,.3)}
.header-right{display:flex;align-items:center;gap:16px;font-size:10px;color:var(--text2)}

.main{padding:20px 24px;flex:1;display:flex;flex-direction:column;gap:16px;max-width:1400px;margin:0 auto;width:100%}

/* Stats row */
.stats-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}
.stat-card{background:var(--bg1);border:1px solid var(--border);border-radius:8px;padding:14px 16px}
.stat-label{font-size:10px;color:var(--text2);text-transform:uppercase;letter-spacing:1px;margin-bottom:4px}
.stat-value{font-size:20px;font-weight:700}
.stat-sub{font-size:10px;color:var(--text2);margin-top:2px}
.val-green{color:var(--green)}
.val-red{color:var(--red)}
.val-gold{color:var(--gold)}
.val-purple{color:var(--purple)}

/* Pool progress */
.pool-progress-wrap{background:var(--bg1);border:1px solid var(--border);border-radius:8px;padding:18px 20px}
.pool-progress-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}
.pool-progress-title{font-size:13px;font-weight:700;color:var(--text);text-transform:uppercase;letter-spacing:1px}
.pool-progress-pct{font-size:22px;font-weight:800}
.progress-bar-bg{background:var(--bg3);border-radius:6px;height:20px;overflow:hidden;position:relative}
.progress-bar-fill{height:100%;border-radius:6px;transition:width .5s ease;min-width:2px}
.progress-bar-fill.positive{background:linear-gradient(90deg,var(--green2),var(--green))}
.progress-bar-fill.negative{background:linear-gradient(90deg,var(--red2),var(--red))}
.progress-target{position:absolute;right:8px;top:50%;transform:translateY(-50%);font-size:9px;color:var(--text2);font-weight:600}

/* Tables */
.section{background:var(--bg1);border:1px solid var(--border);border-radius:8px;overflow:hidden}
.section-header{padding:12px 16px;border-bottom:1px solid var(--border);font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:1px;color:var(--text2)}
table{width:100%;border-collapse:collapse}
th{padding:8px 12px;text-align:left;font-size:10px;color:var(--text3);text-transform:uppercase;letter-spacing:.5px;border-bottom:1px solid var(--border);font-weight:600}
td{padding:7px 12px;font-size:11px;border-bottom:1px solid rgba(26,34,53,.4)}
tr:hover{background:rgba(255,255,255,.015)}
.pnl-pos{color:var(--green)}
.pnl-neg{color:var(--red)}

/* Confetti */
.confetti-container{position:fixed;top:0;left:0;width:100%;height:100%;pointer-events:none;z-index:9999;overflow:hidden}
.confetti-piece{position:absolute;width:10px;height:10px;top:-20px;animation:confetti-fall 3s ease-in forwards}

/* Two-col layout */
.two-col{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:900px){.two-col{grid-template-columns:1fr}}
</style>
</head>
<body>

<div class="header-bar">
  <div class="header-left">
    <span class="brand">HEDGER POOL</span>
    <span class="mode-badge" id="modeBadge">PAPER</span>
  </div>
  <div class="header-right">
    <span><span class="live-dot dot-paper" id="liveDot"></span><span id="modeText">PAPER MODE</span></span>
    <span id="lastUpdate">--</span>
  </div>
</div>
<div style="background:var(--bg1);border-bottom:1px solid var(--border);padding:10px 24px;font-size:11px;color:var(--text2);line-height:1.6">
  <strong style="color:#b060ff">STRATEGY:</strong> Run up to 10 pools simultaneously, each with 3 mixed YES/NO positions across BTC, ETH, SOL, XRP, DOGE (15-min contracts). Every 2 seconds, check each pool — if any is +30%, sell it and lock in profit. Multiple pools = more volume, more chances to hit +30%. Mixed sides create real hedging.
</div>

<div class="main">
  <!-- Stats Row -->
  <div class="stats-row">
    <div class="stat-card">
      <div class="stat-label">Overall P&L</div>
      <div class="stat-value" id="overallPnl">$0.00</div>
      <div class="stat-sub">cash + positions - starting</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Portfolio</div>
      <div class="stat-value val-gold" id="portfolio">$0.00</div>
      <div class="stat-sub" id="cashDetail">Cash: $0 | Pos: $0</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Pool Size</div>
      <div class="stat-value val-purple" id="poolSize">0 / 15</div>
      <div class="stat-sub" id="poolCostValue">Cost: $0 | Value: $0</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Rounds Completed</div>
      <div class="stat-value val-gold" id="roundsCount">0</div>
      <div class="stat-sub" id="roundsPnl">Total rounds P&L: $0</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Win / Loss</div>
      <div class="stat-value" id="winLoss">0 / 0</div>
      <div class="stat-sub" id="winRate">--</div>
    </div>
  </div>

  <!-- Pool Progress Bar -->
  <div class="pool-progress-wrap">
    <div class="pool-progress-header">
      <span class="pool-progress-title">Pool P&L Progress</span>
      <span class="pool-progress-pct" id="poolPctDisplay">+0.0% / 5.0%</span>
    </div>
    <div class="progress-bar-bg">
      <div class="progress-bar-fill positive" id="progressFill" style="width:0%"></div>
      <span class="progress-target">TARGET: +5%</span>
    </div>
  </div>

  <div class="two-col">
    <!-- Pool Positions Table -->
    <div class="section">
      <div class="section-header">Pool Positions</div>
      <table>
        <thead><tr><th>Ticker</th><th>Side</th><th>Entry</th><th>Bid</th><th>P&L</th><th>%</th></tr></thead>
        <tbody id="poolTable"><tr><td colspan="6" style="color:var(--text3);text-align:center;padding:20px">No positions</td></tr></tbody>
      </table>
    </div>

    <!-- Rounds Table -->
    <div class="section">
      <div class="section-header">Completed Rounds</div>
      <table>
        <thead><tr><th>#</th><th>Time</th><th>Pos</th><th>Cost</th><th>Value</th><th>P&L</th><th>%</th></tr></thead>
        <tbody id="roundsTable"><tr><td colspan="7" style="color:var(--text3);text-align:center;padding:20px">No rounds yet</td></tr></tbody>
      </table>
    </div>
  </div>

  <!-- Recent Trade History -->
  <div class="section">
    <div class="section-header">Recent Resolved Trades</div>
    <table>
      <thead><tr><th>Ticker</th><th>Side</th><th>Entry</th><th>P&L</th><th>Series</th><th>Time</th></tr></thead>
      <tbody id="historyTable"><tr><td colspan="6" style="color:var(--text3);text-align:center;padding:20px">No trades yet</td></tr></tbody>
    </table>
  </div>
</div>

<div class="confetti-container" id="confettiContainer"></div>

<script>
const COLORS = ['#00e68a','#ff4466','#f0b040','#4488ff','#a855f7','#40d0e0','#ff8855','#55ff88'];

function fireConfetti(){
  const c = document.getElementById('confettiContainer');
  for(let i=0;i<80;i++){
    const p = document.createElement('div');
    p.className = 'confetti-piece';
    p.style.left = Math.random()*100+'%';
    p.style.background = COLORS[Math.floor(Math.random()*COLORS.length)];
    p.style.animationDelay = Math.random()*1.5+'s';
    p.style.animationDuration = (2+Math.random()*2)+'s';
    p.style.width = (6+Math.random()*8)+'px';
    p.style.height = (6+Math.random()*8)+'px';
    p.style.borderRadius = Math.random()>.5?'50%':'2px';
    c.appendChild(p);
  }
  setTimeout(()=>{c.innerHTML=''},5000);
}

function pnlClass(v){return v>=0?'pnl-pos':'pnl-neg'}
function pnlSign(v){return v>=0?'+'+v.toFixed(4):v.toFixed(4)}

async function refresh(){
  try{
    const [statusRes, poolRes, roundsRes, histRes] = await Promise.all([
      fetch('/api/status').then(r=>r.json()),
      fetch('/api/pool').then(r=>r.json()),
      fetch('/api/rounds').then(r=>r.json()),
      fetch('/api/history').then(r=>r.json()),
    ]);
    const s = statusRes;

    // Mode
    const isPaper = s.mode === 'PAPER';
    document.getElementById('modeBadge').textContent = s.mode;
    document.getElementById('modeBadge').className = 'mode-badge '+(isPaper?'mode-paper':'mode-live');
    document.getElementById('liveDot').className = 'live-dot '+(isPaper?'dot-paper':'dot-live');
    document.getElementById('modeText').textContent = isPaper?'PAPER MODE':'LIVE TRADING';

    // Stats
    const ov = s.overall_pnl||0;
    const ovEl = document.getElementById('overallPnl');
    ovEl.textContent = (ov>=0?'+':'')+ov.toFixed(2);
    ovEl.className = 'stat-value '+(ov>=0?'val-green':'val-red');

    document.getElementById('portfolio').textContent = '$'+(s.portfolio||0).toFixed(2);
    document.getElementById('cashDetail').textContent = 'Cash: $'+(s.cash||0).toFixed(2)+' | Pos: $'+(s.positions_value||0).toFixed(4);

    document.getElementById('poolSize').textContent = (s.pool_positions||0)+' / '+s.max_pool;
    document.getElementById('poolCostValue').textContent = 'Cost: $'+(s.pool_cost||0).toFixed(4)+' | Value: $'+(s.pool_value||0).toFixed(4);

    document.getElementById('roundsCount').textContent = s.rounds_count||0;
    document.getElementById('roundsPnl').textContent = 'Total rounds P&L: $'+(s.rounds_pnl||0).toFixed(4);

    document.getElementById('winLoss').textContent = (s.wins||0)+' / '+(s.losses||0);
    const total = (s.wins||0)+(s.losses||0);
    document.getElementById('winRate').textContent = total>0?'Win rate: '+(s.wins/total*100).toFixed(1)+'%':'--';

    // Pool progress
    const pct = s.pool_pct||0;
    const target = s.pool_target||5;
    const pctEl = document.getElementById('poolPctDisplay');
    pctEl.textContent = (pct>=0?'+':'')+pct.toFixed(1)+'% / '+target.toFixed(1)+'%';
    pctEl.className = 'pool-progress-pct '+(pct>=0?'val-green':'val-red');

    const fill = document.getElementById('progressFill');
    const fillPct = Math.min(Math.max(Math.abs(pct)/target*100, 0), 100);
    fill.style.width = fillPct+'%';
    fill.className = 'progress-bar-fill '+(pct>=0?'positive':'negative');

    // Pool table
    const pt = document.getElementById('poolTable');
    if(poolRes.length===0){
      pt.innerHTML='<tr><td colspan="6" style="color:var(--text3);text-align:center;padding:20px">No positions in pool</td></tr>';
    } else {
      pt.innerHTML = poolRes.map(p=>{
        const cls = p.gain_pct>=0?'pnl-pos':'pnl-neg';
        return '<tr><td>'+p.ticker+'</td><td>'+p.side.toUpperCase()+'</td><td>$'+p.price.toFixed(2)+'</td><td>$'+p.current_bid.toFixed(2)+'</td><td class="'+cls+'">$'+pnlSign(p.unrealized)+'</td><td class="'+cls+'">'+(p.gain_pct>=0?'+':'')+p.gain_pct.toFixed(1)+'%</td></tr>';
      }).join('');
    }

    // Rounds table
    const rt = document.getElementById('roundsTable');
    if(roundsRes.length===0){
      rt.innerHTML='<tr><td colspan="7" style="color:var(--text3);text-align:center;padding:20px">No rounds yet</td></tr>';
    } else {
      rt.innerHTML = roundsRes.map(r=>{
        const cls = r.pnl>=0?'pnl-pos':'pnl-neg';
        const t = new Date(r.ended_at);
        const ts = t.toLocaleTimeString();
        return '<tr><td>'+r.id+'</td><td>'+ts+'</td><td>'+r.positions+'</td><td>$'+r.cost.toFixed(4)+'</td><td>$'+r.value.toFixed(4)+'</td><td class="'+cls+'">$'+pnlSign(r.pnl)+'</td><td class="'+cls+'">'+(r.pnl_pct>=0?'+':'')+r.pnl_pct.toFixed(1)+'%</td></tr>';
      }).join('');
    }

    // History table
    const ht = document.getElementById('historyTable');
    if(histRes.length===0){
      ht.innerHTML='<tr><td colspan="6" style="color:var(--text3);text-align:center;padding:20px">No trades yet</td></tr>';
    } else {
      ht.innerHTML = histRes.slice(0,30).map(t=>{
        const cls = t.pnl>=0?'pnl-pos':'pnl-neg';
        const dt = new Date(t.created_at);
        const ts = dt.toLocaleTimeString();
        return '<tr><td>'+t.ticker+'</td><td>'+t.side.toUpperCase()+'</td><td>$'+t.price.toFixed(2)+'</td><td class="'+cls+'">$'+pnlSign(t.pnl)+'</td><td>'+t.series+'</td><td>'+ts+'</td></tr>';
      }).join('');
    }

    // Confetti
    if(s.confetti) fireConfetti();

    document.getElementById('lastUpdate').textContent = new Date().toLocaleTimeString();
  }catch(e){
    console.error('Refresh failed:',e);
  }
}

refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>"""


# === BOT LOOP ===

def bot_loop():
    mode = "PAPER" if not ENABLE_TRADING else "LIVE"
    logger.info(f"Hedger starting [{mode}] -- pool target +{POOL_TAKE_PROFIT*100:.0f}%, {MAX_POOLS} pools x {POOL_SIZE}, buy ${BUY_MIN}-${BUY_MAX}")
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
