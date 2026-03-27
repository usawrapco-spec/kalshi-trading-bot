"""
Pair-matching arbitrage bot for Kalshi crypto 15-minute contracts.
Buy BOTH yes AND no sides under $0.40 to lock in guaranteed profit at settlement.
Each matched pair pays $1.00 - (yes_cost + no_cost) - fees.
"""

import os, time, logging, traceback, math, json
from datetime import datetime, timezone
from flask import Flask, jsonify, request, render_template_string
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
PORT = int(os.environ.get('MATCHER_PORT', 8082))
ENABLE_TRADING = os.environ.get('ENABLE_TRADING', '').lower() in ('1', 'true', 'yes')

# === STRATEGY ===
STARTING_BALANCE = 10.00
BUY_MAX = 0.40
CONTRACTS = 1
TAKER_FEE_RATE = 0.07
FEE_CAP = 0.02
CYCLE_SECONDS = 2
MIN_MINS_TO_EXPIRY = 10
MAX_MINS_TO_EXPIRY = 15
CASH_RESERVE = 0.30

CRYPTO_SERIES = ['KXBTC15M', 'KXETH15M', 'KXSOL15M', 'KXXRP15M', 'KXDOGE15M']
SERIES_TO_COIN = {
    'KXBTC15M': 'BTC', 'KXETH15M': 'ETH', 'KXSOL15M': 'SOL',
    'KXXRP15M': 'XRP', 'KXDOGE15M': 'DOGE',
}

# === INIT ===
auth = KalshiAuth()
app = Flask(__name__)

# Bot state
bot_status = {'running': False, 'cycles': 0, 'last_cycle': None, 'errors': []}


def sf(val):
    try:
        return float(val) if val is not None else 0.0
    except:
        return 0.0


def kalshi_fee(price, count=1):
    """Kalshi taker fee: 7% of P*(1-P) per contract, max $0.02/contract."""
    return min(math.ceil(TAKER_FEE_RATE * count * price * (1 - price) * 100) / 100, FEE_CAP * count)


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


def get_kalshi_balance():
    try:
        resp = kalshi_get('/portfolio/balance')
        return resp.get('balance', 0) / 100.0
    except Exception as e:
        logger.error(f"Kalshi balance fetch failed: {e}")
        return None


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
                CREATE TABLE IF NOT EXISTS matcher_trades (
                    id SERIAL PRIMARY KEY,
                    pair_id INTEGER,
                    ticker TEXT,
                    side TEXT,
                    price NUMERIC,
                    count INTEGER DEFAULT 1,
                    current_bid NUMERIC,
                    pnl NUMERIC,
                    order_id TEXT,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS matcher_pairs (
                    id SERIAL PRIMARY KEY,
                    ticker TEXT,
                    coin TEXT,
                    status TEXT DEFAULT 'open',
                    yes_trade_id INTEGER,
                    no_trade_id INTEGER,
                    yes_price NUMERIC,
                    no_price NUMERIC,
                    guaranteed_profit NUMERIC,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    settled_at TIMESTAMPTZ
                )
            """)
    finally:
        conn.close()


# === BALANCE ===

def get_balance():
    if ENABLE_TRADING:
        real = get_kalshi_balance()
        if real is not None:
            return real
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT price, count FROM matcher_trades WHERE pnl IS NULL")
            open_trades = cur.fetchall()
            open_cost = sum(sf(t['price']) * (t.get('count') or 1) + kalshi_fee(sf(t['price']), t.get('count') or 1) for t in open_trades)
            cur.execute("SELECT pnl FROM matcher_trades WHERE pnl IS NOT NULL")
            resolved = cur.fetchall()
            total_pnl = sum(sf(t['pnl']) for t in resolved)
            return max(0, STARTING_BALANCE - open_cost + total_pnl)
    except Exception as e:
        logger.error(f"Balance calc failed: {e}")
        return 0.0
    finally:
        conn.close()


# === MARKET SCANNING ===

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


def get_ticker_coin(ticker):
    """Determine which coin a ticker belongs to."""
    for series, coin in SERIES_TO_COIN.items():
        if ticker.startswith(series):
            return coin
    return None


# === PAIR MATCHING ENGINE ===

def run_matcher_cycle():
    """One cycle: scan markets, find cheap sides, buy to build/complete pairs."""
    conn = get_db()
    try:
        balance = get_balance()
        deployable = balance - CASH_RESERVE
        logger.info(f"MATCHER CYCLE | Balance: ${balance:.2f} | Deployable: ${deployable:.2f}")

        if deployable < 0.05:
            logger.info("Insufficient deployable cash — skipping buys")
            update_open_trades(conn)
            check_settlements(conn)
            return

        markets = fetch_all_markets()
        now = datetime.now(timezone.utc)

        # Get open (unmatched) pairs
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM matcher_pairs WHERE status = 'open'")
            open_pairs = cur.fetchall()

        # Index open pairs by ticker
        pairs_by_ticker = {}
        for p in open_pairs:
            pairs_by_ticker[p['ticker']] = p

        for market in markets:
            ticker = market.get('ticker', '')
            coin = get_ticker_coin(ticker)
            if not coin:
                continue

            # Check expiry window
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

            yes_ask = sf(market.get('yes_ask', 0)) / 100 if sf(market.get('yes_ask', 0)) > 1 else sf(market.get('yes_ask', 0))
            no_ask = sf(market.get('no_ask', 0)) / 100 if sf(market.get('no_ask', 0)) > 1 else sf(market.get('no_ask', 0))

            # Normalize — Kalshi returns cents
            if yes_ask > 1:
                yes_ask = yes_ask / 100
            if no_ask > 1:
                no_ask = no_ask / 100

            existing_pair = pairs_by_ticker.get(ticker)

            if existing_pair:
                # Already have a pair for this ticker — try to fill missing side
                if existing_pair['yes_trade_id'] and not existing_pair['no_trade_id']:
                    # Need NO side
                    if 0 < no_ask <= BUY_MAX:
                        cost = no_ask * CONTRACTS + kalshi_fee(no_ask, CONTRACTS)
                        if cost <= deployable:
                            trade_id = execute_buy(conn, ticker, 'no', no_ask, CONTRACTS, existing_pair['id'])
                            if trade_id:
                                complete_pair(conn, existing_pair['id'], 'no', trade_id, no_ask)
                                deployable -= cost
                elif existing_pair['no_trade_id'] and not existing_pair['yes_trade_id']:
                    # Need YES side
                    if 0 < yes_ask <= BUY_MAX:
                        cost = yes_ask * CONTRACTS + kalshi_fee(yes_ask, CONTRACTS)
                        if cost <= deployable:
                            trade_id = execute_buy(conn, ticker, 'yes', yes_ask, CONTRACTS, existing_pair['id'])
                            if trade_id:
                                complete_pair(conn, existing_pair['id'], 'yes', trade_id, yes_ask)
                                deployable -= cost
            else:
                # No pair for this ticker yet — start one if either side is cheap
                # Prefer to start with the cheapest side
                sides_to_try = []
                if 0 < yes_ask <= BUY_MAX:
                    sides_to_try.append(('yes', yes_ask))
                if 0 < no_ask <= BUY_MAX:
                    sides_to_try.append(('no', no_ask))

                # If both sides are cheap enough, buy both immediately
                if len(sides_to_try) == 2:
                    total_cost = sum(p * CONTRACTS + kalshi_fee(p, CONTRACTS) for _, p in sides_to_try)
                    combined_price = sides_to_try[0][1] + sides_to_try[1][1]
                    # Only buy both if there's guaranteed profit
                    yes_fee = kalshi_fee(sides_to_try[0][1] if sides_to_try[0][0] == 'yes' else sides_to_try[1][1], CONTRACTS)
                    no_fee = kalshi_fee(sides_to_try[1][1] if sides_to_try[1][0] == 'no' else sides_to_try[0][1], CONTRACTS)
                    profit = 1.00 * CONTRACTS - combined_price * CONTRACTS - yes_fee - no_fee
                    if profit > 0 and total_cost <= deployable:
                        pair_id = create_pair(conn, ticker, coin)
                        for side, price in sides_to_try:
                            trade_id = execute_buy(conn, ticker, side, price, CONTRACTS, pair_id)
                            if trade_id:
                                complete_pair(conn, pair_id, side, trade_id, price)
                                deployable -= price * CONTRACTS + kalshi_fee(price, CONTRACTS)

                elif len(sides_to_try) == 1:
                    side, price = sides_to_try[0]
                    cost = price * CONTRACTS + kalshi_fee(price, CONTRACTS)
                    # Only start a pair if the other side could still be profitable
                    max_other = 1.00 - price  # theoretical max for other side
                    if max_other > BUY_MAX:
                        # Other side would need to be under BUY_MAX for profit, which means
                        # total < price + BUY_MAX, guaranteed profit > 1 - price - BUY_MAX - fees
                        potential = 1.00 - price - BUY_MAX - kalshi_fee(price) - kalshi_fee(BUY_MAX)
                        if potential > 0 and cost <= deployable:
                            pair_id = create_pair(conn, ticker, coin)
                            trade_id = execute_buy(conn, ticker, side, price, CONTRACTS, pair_id)
                            if trade_id:
                                complete_pair(conn, pair_id, side, trade_id, price)
                                deployable -= cost

        update_open_trades(conn)
        check_settlements(conn)

    except Exception as e:
        logger.error(f"Matcher cycle error: {traceback.format_exc()}")
        bot_status['errors'].append(str(e))
        if len(bot_status['errors']) > 20:
            bot_status['errors'] = bot_status['errors'][-20:]
    finally:
        conn.close()


def create_pair(conn, ticker, coin):
    """Create a new open pair, return its ID."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO matcher_pairs (ticker, coin, status) VALUES (%s, %s, 'open') RETURNING id",
            (ticker, coin)
        )
        return cur.fetchone()[0]


def execute_buy(conn, ticker, side, price, count, pair_id):
    """Place a buy order and record the trade. Returns trade ID or None."""
    result = place_order(ticker, side, 'buy', price, count)
    if not result:
        return None

    order_id, filled = result
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO matcher_trades (pair_id, ticker, side, price, count, order_id)
               VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
            (pair_id, ticker, side, price, filled, str(order_id))
        )
        trade_id = cur.fetchone()[0]
    logger.info(f"BOUGHT {side.upper()} {ticker} x{filled} @ ${price:.2f} | trade_id={trade_id} pair_id={pair_id}")
    return trade_id


def complete_pair(conn, pair_id, side, trade_id, price):
    """Update pair with the newly bought side."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        if side == 'yes':
            cur.execute(
                "UPDATE matcher_pairs SET yes_trade_id = %s, yes_price = %s WHERE id = %s",
                (trade_id, price, pair_id)
            )
        else:
            cur.execute(
                "UPDATE matcher_pairs SET no_trade_id = %s, no_price = %s WHERE id = %s",
                (trade_id, price, pair_id)
            )

        # Check if pair is now matched (both sides filled)
        cur.execute("SELECT * FROM matcher_pairs WHERE id = %s", (pair_id,))
        pair = cur.fetchone()
        if pair['yes_trade_id'] and pair['no_trade_id']:
            yp = sf(pair['yes_price'])
            np = sf(pair['no_price'])
            yes_fee = kalshi_fee(yp, CONTRACTS)
            no_fee = kalshi_fee(np, CONTRACTS)
            gp = 1.00 * CONTRACTS - (yp + np) * CONTRACTS - yes_fee - no_fee
            cur.execute(
                "UPDATE matcher_pairs SET status = 'matched', guaranteed_profit = %s WHERE id = %s",
                (round(gp, 4), pair_id)
            )
            logger.info(f"PAIR MATCHED! #{pair_id} {pair['ticker']} | yes=${yp:.2f} + no=${np:.2f} = guaranteed ${gp:.4f}")


def update_open_trades(conn):
    """Update current_bid for all open trades."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM matcher_trades WHERE pnl IS NULL")
        open_trades = cur.fetchall()

    for trade in open_trades:
        ticker = trade['ticker']
        side = trade['side']
        try:
            market = get_market(ticker)
            if not market:
                continue
            if side == 'yes':
                bid = sf(market.get('yes_bid', 0))
            else:
                bid = sf(market.get('no_bid', 0))
            if bid > 1:
                bid = bid / 100
            with conn.cursor() as cur:
                cur.execute("UPDATE matcher_trades SET current_bid = %s WHERE id = %s", (bid, trade['id']))
        except Exception as e:
            logger.warning(f"Update bid for {ticker} failed: {e}")


def check_settlements(conn):
    """Check if any matched pairs have settled."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM matcher_pairs WHERE status = 'matched'")
        matched_pairs = cur.fetchall()

    for pair in matched_pairs:
        ticker = pair['ticker']
        try:
            market = get_market(ticker)
            if not market:
                continue
            status = market.get('status', '')
            result = market.get('result', '')

            if status in ('settled', 'closed', 'finalized') or result:
                yp = sf(pair['yes_price'])
                np = sf(pair['no_price'])
                yes_fee = kalshi_fee(yp, CONTRACTS)
                no_fee = kalshi_fee(np, CONTRACTS)
                gp = 1.00 * CONTRACTS - (yp + np) * CONTRACTS - yes_fee - no_fee

                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE matcher_pairs SET status = 'settled', settled_at = NOW(), guaranteed_profit = %s WHERE id = %s",
                        (round(gp, 4), pair['id'])
                    )
                    # Mark trades as resolved
                    # The winning side gets $1 payout, losing side gets $0
                    if result == 'yes':
                        yes_pnl = 1.00 - yp - yes_fee
                        no_pnl = 0 - np - no_fee
                    elif result == 'no':
                        yes_pnl = 0 - yp - yes_fee
                        no_pnl = 1.00 - np - no_fee
                    else:
                        # Unknown result, use guaranteed profit split
                        yes_pnl = gp / 2
                        no_pnl = gp / 2

                    if pair['yes_trade_id']:
                        cur.execute("UPDATE matcher_trades SET pnl = %s WHERE id = %s", (round(yes_pnl, 4), pair['yes_trade_id']))
                    if pair['no_trade_id']:
                        cur.execute("UPDATE matcher_trades SET pnl = %s WHERE id = %s", (round(no_pnl, 4), pair['no_trade_id']))

                logger.info(f"PAIR SETTLED #{pair['id']} {ticker} result={result} profit=${gp:.4f}")
        except Exception as e:
            logger.warning(f"Settlement check for {ticker} failed: {e}")

    # Also check open (unmatched) pairs for settlement — if market settles before matched, record loss
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM matcher_pairs WHERE status = 'open'")
        open_pairs = cur.fetchall()

    for pair in open_pairs:
        ticker = pair['ticker']
        try:
            market = get_market(ticker)
            if not market:
                continue
            status = market.get('status', '')
            result = market.get('result', '')
            if status in ('settled', 'closed', 'finalized') or result:
                # Unmatched pair settled — only one side was bought
                with conn.cursor() as cur:
                    cur.execute("UPDATE matcher_pairs SET status = 'settled', settled_at = NOW() WHERE id = %s", (pair['id'],))
                    if pair['yes_trade_id']:
                        yp = sf(pair['yes_price'])
                        pnl = (1.00 - yp - kalshi_fee(yp)) if result == 'yes' else (0 - yp - kalshi_fee(yp))
                        cur.execute("UPDATE matcher_trades SET pnl = %s WHERE id = %s", (round(pnl, 4), pair['yes_trade_id']))
                    if pair['no_trade_id']:
                        np = sf(pair['no_price'])
                        pnl = (1.00 - np - kalshi_fee(np)) if result == 'no' else (0 - np - kalshi_fee(np))
                        cur.execute("UPDATE matcher_trades SET pnl = %s WHERE id = %s", (round(pnl, 4), pair['no_trade_id']))
                logger.info(f"UNMATCHED PAIR SETTLED #{pair['id']} {ticker} result={result}")
        except Exception as e:
            logger.warning(f"Open pair settlement check for {ticker} failed: {e}")


# === BOT LOOP ===

def bot_loop():
    bot_status['running'] = True
    logger.info("Matcher bot started")
    while True:
        try:
            run_matcher_cycle()
            bot_status['cycles'] += 1
            bot_status['last_cycle'] = datetime.now(timezone.utc).isoformat()
        except Exception as e:
            logger.error(f"Bot loop error: {traceback.format_exc()}")
        time.sleep(CYCLE_SECONDS)


# === SELL API ===

@app.route('/api/sell', methods=['POST'])
def api_sell():
    """Sell a position by trade ID."""
    try:
        data = request.get_json()
        trade_id = data.get('trade_id')
        if not trade_id:
            return jsonify({'success': False, 'error': 'Missing trade_id'}), 400

        conn = get_db()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT * FROM matcher_trades WHERE id = %s AND pnl IS NULL", (trade_id,))
                trade = cur.fetchone()
                if not trade:
                    return jsonify({'success': False, 'error': 'Trade not found or already resolved'}), 404

            ticker = trade['ticker']
            side = trade['side']
            count = trade.get('count') or 1
            buy_price = sf(trade['price'])

            # Get current bid from Kalshi
            market = get_market(ticker)
            if not market:
                return jsonify({'success': False, 'error': 'Could not fetch market data'}), 500

            if side == 'yes':
                bid = sf(market.get('yes_bid', 0))
            else:
                bid = sf(market.get('no_bid', 0))
            if bid > 1:
                bid = bid / 100

            if bid <= 0:
                return jsonify({'success': False, 'error': f'No bid available for {side} side (bid=0)'}), 400

            # Place sell order
            result = place_order(ticker, side, 'sell', bid, count)
            if not result:
                return jsonify({'success': False, 'error': 'Sell order failed'}), 500

            order_id, filled = result
            sell_fee = kalshi_fee(bid, filled)
            buy_fee = kalshi_fee(buy_price, filled)
            pnl = (bid - buy_price) * filled - buy_fee - sell_fee

            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE matcher_trades SET pnl = %s, current_bid = %s WHERE id = %s",
                    (round(pnl, 4), bid, trade_id)
                )
                # If this trade belongs to a pair, check if we should update pair status
                if trade.get('pair_id'):
                    cur.execute("SELECT * FROM matcher_pairs WHERE id = %s", (trade['pair_id'],))
                    pair_row = cur.fetchone()
                    if pair_row:
                        # Check if both sides are now resolved
                        cur.execute(
                            "SELECT COUNT(*) FROM matcher_trades WHERE pair_id = %s AND pnl IS NULL",
                            (trade['pair_id'],)
                        )
                        remaining = cur.fetchone()[0]
                        if remaining == 0:
                            cur.execute(
                                "UPDATE matcher_pairs SET status = 'settled', settled_at = NOW() WHERE id = %s",
                                (trade['pair_id'],)
                            )

            logger.info(f"SOLD trade #{trade_id} {ticker} {side} @ ${bid:.2f} | pnl=${pnl:.4f}")
            return jsonify({
                'success': True,
                'trade_id': trade_id,
                'sell_price': bid,
                'pnl': round(pnl, 4),
                'order_id': str(order_id),
            })

        finally:
            conn.close()

    except Exception as e:
        logger.error(f"Sell API error: {traceback.format_exc()}")
        return jsonify({'success': False, 'error': str(e)}), 500


# === DASHBOARD DATA API ===

@app.route('/api/data')
def api_data():
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # All pairs
            cur.execute("SELECT * FROM matcher_pairs ORDER BY created_at DESC")
            pairs = cur.fetchall()

            # All trades
            cur.execute("SELECT * FROM matcher_trades ORDER BY created_at DESC")
            trades = cur.fetchall()

        # Stats
        total_matched = sum(1 for p in pairs if p['status'] in ('matched', 'settled') and p['yes_trade_id'] and p['no_trade_id'])
        total_gp = sum(sf(p['guaranteed_profit']) for p in pairs if p['status'] in ('matched', 'settled') and p['guaranteed_profit'])
        settled_pairs = [p for p in pairs if p['status'] == 'settled']
        settled_profit = sum(sf(p['guaranteed_profit']) for p in settled_pairs if p['guaranteed_profit'])

        balance = get_balance()

        # Build pair cards data
        pair_cards = []
        for p in pairs:
            yes_trade = None
            no_trade = None
            for t in trades:
                if t['id'] == p.get('yes_trade_id'):
                    yes_trade = t
                if t['id'] == p.get('no_trade_id'):
                    no_trade = t

            pair_cards.append({
                'id': p['id'],
                'ticker': p['ticker'],
                'coin': p['coin'],
                'status': p['status'],
                'yes_price': float(sf(p['yes_price'])) if p['yes_price'] else None,
                'no_price': float(sf(p['no_price'])) if p['no_price'] else None,
                'guaranteed_profit': float(sf(p['guaranteed_profit'])) if p['guaranteed_profit'] else None,
                'yes_trade': {
                    'id': yes_trade['id'],
                    'price': float(sf(yes_trade['price'])),
                    'current_bid': float(sf(yes_trade['current_bid'])) if yes_trade['current_bid'] else None,
                    'pnl': float(sf(yes_trade['pnl'])) if yes_trade['pnl'] is not None else None,
                } if yes_trade else None,
                'no_trade': {
                    'id': no_trade['id'],
                    'price': float(sf(no_trade['price'])),
                    'current_bid': float(sf(no_trade['current_bid'])) if no_trade['current_bid'] else None,
                    'pnl': float(sf(no_trade['pnl'])) if no_trade['pnl'] is not None else None,
                } if no_trade else None,
                'created_at': p['created_at'].isoformat() if p['created_at'] else None,
                'settled_at': p['settled_at'].isoformat() if p['settled_at'] else None,
            })

        return jsonify({
            'balance': round(balance, 2),
            'total_matched': total_matched,
            'total_guaranteed_profit': round(total_gp, 4),
            'settled_profit': round(settled_profit, 4),
            'pairs': pair_cards,
            'bot': bot_status,
            'trading_enabled': ENABLE_TRADING,
        })
    finally:
        conn.close()


# === DASHBOARD ===

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Kalshi Pair Matcher</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;500;600;700&display=swap');
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
    background: #06080d; color: #e0e0e0; font-family: 'JetBrains Mono', monospace;
    min-height: 100vh; padding: 20px;
}
.header {
    text-align: center; margin-bottom: 24px; padding: 20px;
    background: linear-gradient(135deg, #0a0f1a, #111827);
    border: 1px solid #1e293b; border-radius: 12px;
}
.header h1 {
    font-size: 28px; font-weight: 700;
    background: linear-gradient(90deg, #60a5fa, #a78bfa, #f472b6);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent;
    margin-bottom: 8px;
}
.header .subtitle { color: #64748b; font-size: 13px; }
.mode-badge {
    display: inline-block; padding: 4px 12px; border-radius: 20px;
    font-size: 11px; font-weight: 600; margin-top: 8px;
}
.mode-paper { background: #1e1b4b; color: #818cf8; border: 1px solid #4338ca; }
.mode-live { background: #14532d; color: #4ade80; border: 1px solid #16a34a; animation: pulse 2s infinite; }
@keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.7; } }

.stats-row {
    display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 24px;
}
.stat-card {
    background: #0d1117; border: 1px solid #1e293b; border-radius: 10px;
    padding: 16px; text-align: center;
}
.stat-card .label { color: #64748b; font-size: 11px; text-transform: uppercase; margin-bottom: 6px; }
.stat-card .value { font-size: 24px; font-weight: 700; }
.stat-card .value.green { color: #4ade80; }
.stat-card .value.blue { color: #60a5fa; }
.stat-card .value.purple { color: #a78bfa; }
.stat-card .value.yellow { color: #fbbf24; }

.section-title {
    font-size: 16px; font-weight: 600; color: #94a3b8; margin: 24px 0 12px;
    display: flex; align-items: center; gap: 8px;
}

.pairs-grid {
    display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); gap: 14px;
    margin-bottom: 24px;
}

.pair-card {
    background: #0d1117; border: 1px solid #1e293b; border-radius: 12px;
    padding: 16px; transition: all 0.3s;
}
.pair-card:hover { border-color: #334155; transform: translateY(-1px); }
.pair-card.matched { border-color: #16a34a; box-shadow: 0 0 20px rgba(74, 222, 128, 0.08); }
.pair-card.settled { border-color: #64748b; opacity: 0.7; }

.pair-header {
    display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px;
}
.pair-coin {
    font-size: 18px; font-weight: 700; color: #f8fafc;
    display: flex; align-items: center; gap: 8px;
}
.pair-ticker { font-size: 10px; color: #475569; font-weight: 400; display: block; }
.pair-status {
    padding: 3px 10px; border-radius: 12px; font-size: 10px; font-weight: 600; text-transform: uppercase;
}
.pair-status.open { background: #7f1d1d; color: #fca5a5; }
.pair-status.matched { background: #14532d; color: #4ade80; }
.pair-status.settled { background: #1e293b; color: #94a3b8; }

.side-row {
    display: flex; justify-content: space-between; align-items: center;
    padding: 8px 10px; border-radius: 8px; margin-bottom: 6px;
}
.side-yes { background: rgba(74, 222, 128, 0.06); }
.side-no { background: rgba(248, 113, 113, 0.06); }

.side-label { font-size: 12px; font-weight: 600; width: 30px; }
.side-label.yes-label { color: #4ade80; }
.side-label.no-label { color: #f87171; }

.side-info { font-size: 12px; color: #94a3b8; flex: 1; margin-left: 8px; }
.side-info .price { color: #e2e8f0; font-weight: 500; }
.side-info .bid { color: #64748b; font-size: 11px; }

.dot {
    width: 8px; height: 8px; border-radius: 50%; display: inline-block; margin-right: 6px;
}
.dot.red { background: #ef4444; animation: blink 1.5s infinite; }
.dot.green { background: #4ade80; }
@keyframes blink { 0%, 100% { opacity: 1; } 50% { opacity: 0.3; } }

.profit-row {
    margin-top: 10px; padding: 8px 10px; background: rgba(74, 222, 128, 0.08);
    border-radius: 8px; text-align: center; font-size: 13px;
}
.profit-row .profit-val { color: #4ade80; font-weight: 700; font-size: 16px; }

.sell-btn {
    background: #7f1d1d; color: #fca5a5; border: 1px solid #991b1b; border-radius: 6px;
    padding: 3px 10px; font-size: 10px; font-family: 'JetBrains Mono', monospace;
    cursor: pointer; font-weight: 600; transition: all 0.2s;
}
.sell-btn:hover { background: #991b1b; color: #fee2e2; }
.sell-btn:disabled { opacity: 0.4; cursor: not-allowed; }

.history-table {
    width: 100%; border-collapse: collapse; font-size: 12px;
}
.history-table th {
    text-align: left; padding: 8px 10px; color: #64748b; font-weight: 500;
    border-bottom: 1px solid #1e293b; font-size: 11px; text-transform: uppercase;
}
.history-table td {
    padding: 8px 10px; border-bottom: 1px solid #0f172a; color: #cbd5e1;
}
.history-table tr:hover td { background: #111827; }

.pnl-pos { color: #4ade80; }
.pnl-neg { color: #f87171; }

#confetti-canvas {
    position: fixed; top: 0; left: 0; width: 100%; height: 100%;
    pointer-events: none; z-index: 9999;
}
.bot-info {
    display: flex; gap: 20px; justify-content: center; margin-top: 8px;
    font-size: 11px; color: #475569;
}
</style>
</head>
<body>
<canvas id="confetti-canvas"></canvas>

<div class="header">
    <h1>PAIR MATCHER</h1>
    <div class="subtitle">Buy YES + NO under $0.40 &mdash; Lock guaranteed profit at settlement</div>
    <div id="mode-badge" class="mode-badge mode-paper">PAPER MODE</div>
    <div class="bot-info">
        <span>Cycle: <span id="cycle-count">0</span></span>
        <span>Last: <span id="last-cycle">--</span></span>
    </div>
</div>

<div class="stats-row">
    <div class="stat-card">
        <div class="label">Cash Balance</div>
        <div class="value blue" id="balance">$0.00</div>
    </div>
    <div class="stat-card">
        <div class="label">Pairs Matched</div>
        <div class="value purple" id="pairs-matched">0</div>
    </div>
    <div class="stat-card">
        <div class="label">Guaranteed Profit</div>
        <div class="value green" id="guaranteed-profit">$0.0000</div>
    </div>
    <div class="stat-card">
        <div class="label">Settled Profit</div>
        <div class="value yellow" id="settled-profit">$0.0000</div>
    </div>
</div>

<div class="section-title">ACTIVE PAIRS</div>
<div class="pairs-grid" id="active-pairs"></div>

<div class="section-title">HISTORY</div>
<div style="background:#0d1117; border:1px solid #1e293b; border-radius:12px; padding:16px; overflow-x:auto;">
<table class="history-table">
<thead><tr><th>Pair</th><th>Coin</th><th>Ticker</th><th>Yes$</th><th>No$</th><th>Profit</th><th>Settled</th></tr></thead>
<tbody id="history-body"></tbody>
</table>
</div>

<script>
let previousMatchedIds = new Set();

function sellPosition(tradeId) {
    const btn = document.querySelector(`[data-trade-id="${tradeId}"]`);
    if (btn) { btn.disabled = true; btn.textContent = 'SELLING...'; }

    fetch('/api/sell', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({trade_id: tradeId})
    })
    .then(r => r.json())
    .then(data => {
        if (data.success) {
            if (btn) { btn.textContent = 'SOLD'; btn.style.background = '#14532d'; btn.style.color = '#4ade80'; }
            setTimeout(refresh, 500);
        } else {
            alert('Sell failed: ' + (data.error || 'unknown error'));
            if (btn) { btn.disabled = false; btn.textContent = 'SELL'; }
        }
    })
    .catch(err => {
        alert('Sell error: ' + err);
        if (btn) { btn.disabled = false; btn.textContent = 'SELL'; }
    });
}

function renderSide(label, trade, isMatched) {
    const labelClass = label === 'YES' ? 'yes-label' : 'no-label';
    const rowClass = label === 'YES' ? 'side-yes' : 'side-no';

    if (!trade) {
        return `<div class="side-row ${rowClass}">
            <span class="side-label ${labelClass}">${label}</span>
            <span class="side-info"><span class="dot red"></span> waiting...</span>
        </div>`;
    }

    const price = trade.price.toFixed(2);
    const bid = trade.current_bid !== null ? `bid $${trade.current_bid.toFixed(2)}` : '';
    const dotClass = isMatched ? 'green' : 'red';
    const sellBtn = trade.pnl === null
        ? `<button class="sell-btn" data-trade-id="${trade.id}" onclick="sellPosition(${trade.id})">SELL</button>`
        : '';

    return `<div class="side-row ${rowClass}">
        <span class="side-label ${labelClass}">${label}</span>
        <span class="side-info"><span class="dot ${dotClass}"></span><span class="price">$${price}</span> <span class="bid">${bid}</span></span>
        ${sellBtn}
    </div>`;
}

function renderPairCard(pair) {
    const statusClass = pair.status;
    const isMatched = pair.status === 'matched' || (pair.status === 'settled' && pair.yes_trade && pair.no_trade);
    const cardClass = pair.status === 'matched' ? 'matched' : pair.status === 'settled' ? 'settled' : '';

    let profitHtml = '';
    if (isMatched && pair.guaranteed_profit !== null) {
        profitHtml = `<div class="profit-row">
            Guaranteed: <span class="profit-val">+$${pair.guaranteed_profit.toFixed(4)}</span>
        </div>`;
    }

    return `<div class="pair-card ${cardClass}">
        <div class="pair-header">
            <div class="pair-coin">${pair.coin} <span class="pair-ticker">${pair.ticker}</span></div>
            <span class="pair-status ${statusClass}">${pair.status.toUpperCase()}</span>
        </div>
        ${renderSide('YES', pair.yes_trade, isMatched)}
        ${renderSide('NO', pair.no_trade, isMatched)}
        ${profitHtml}
    </div>`;
}

function fireConfetti() {
    const canvas = document.getElementById('confetti-canvas');
    const ctx = canvas.getContext('2d');
    canvas.width = window.innerWidth;
    canvas.height = window.innerHeight;
    const pieces = [];
    const colors = ['#4ade80','#60a5fa','#a78bfa','#f472b6','#fbbf24','#f87171'];
    for (let i = 0; i < 120; i++) {
        pieces.push({
            x: Math.random() * canvas.width,
            y: -20 - Math.random() * 200,
            w: 6 + Math.random() * 6,
            h: 4 + Math.random() * 4,
            color: colors[Math.floor(Math.random() * colors.length)],
            vx: (Math.random() - 0.5) * 6,
            vy: 2 + Math.random() * 4,
            rot: Math.random() * 360,
            rotV: (Math.random() - 0.5) * 10,
            life: 1
        });
    }
    let frame = 0;
    function animate() {
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        let alive = false;
        for (const p of pieces) {
            if (p.life <= 0) continue;
            alive = true;
            p.x += p.vx;
            p.y += p.vy;
            p.vy += 0.1;
            p.rot += p.rotV;
            p.life -= 0.008;
            ctx.save();
            ctx.translate(p.x, p.y);
            ctx.rotate(p.rot * Math.PI / 180);
            ctx.globalAlpha = Math.max(0, p.life);
            ctx.fillStyle = p.color;
            ctx.fillRect(-p.w/2, -p.h/2, p.w, p.h);
            ctx.restore();
        }
        if (alive && frame < 300) { frame++; requestAnimationFrame(animate); }
        else { ctx.clearRect(0, 0, canvas.width, canvas.height); }
    }
    animate();
}

function refresh() {
    fetch('/api/data')
    .then(r => r.json())
    .then(data => {
        // Mode badge
        const badge = document.getElementById('mode-badge');
        if (data.trading_enabled) {
            badge.className = 'mode-badge mode-live';
            badge.textContent = 'LIVE TRADING';
        } else {
            badge.className = 'mode-badge mode-paper';
            badge.textContent = 'PAPER MODE';
        }

        // Stats
        document.getElementById('balance').textContent = '$' + data.balance.toFixed(2);
        document.getElementById('pairs-matched').textContent = data.total_matched;
        document.getElementById('guaranteed-profit').textContent = '$' + data.total_guaranteed_profit.toFixed(4);
        document.getElementById('settled-profit').textContent = '$' + data.settled_profit.toFixed(4);

        // Bot info
        document.getElementById('cycle-count').textContent = data.bot.cycles || 0;
        document.getElementById('last-cycle').textContent = data.bot.last_cycle
            ? new Date(data.bot.last_cycle).toLocaleTimeString() : '--';

        // Active pairs (open + matched)
        const activePairs = data.pairs.filter(p => p.status !== 'settled');
        const activeEl = document.getElementById('active-pairs');
        if (activePairs.length === 0) {
            activeEl.innerHTML = '<div style="color:#475569;text-align:center;padding:40px;font-size:13px;">No active pairs — waiting for cheap contracts...</div>';
        } else {
            activeEl.innerHTML = activePairs.map(renderPairCard).join('');
        }

        // Confetti on new matches
        const currentMatchedIds = new Set();
        data.pairs.forEach(p => {
            if (p.status === 'matched') currentMatchedIds.add(p.id);
        });
        for (const id of currentMatchedIds) {
            if (!previousMatchedIds.has(id)) {
                fireConfetti();
                break;
            }
        }
        previousMatchedIds = currentMatchedIds;

        // History (settled pairs)
        const settled = data.pairs.filter(p => p.status === 'settled');
        const histBody = document.getElementById('history-body');
        if (settled.length === 0) {
            histBody.innerHTML = '<tr><td colspan="7" style="text-align:center;color:#475569;">No settled pairs yet</td></tr>';
        } else {
            histBody.innerHTML = settled.map(p => {
                const yp = p.yes_price !== null ? '$' + p.yes_price.toFixed(2) : '--';
                const np = p.no_price !== null ? '$' + p.no_price.toFixed(2) : '--';
                const gp = p.guaranteed_profit !== null ? p.guaranteed_profit : 0;
                const gpClass = gp >= 0 ? 'pnl-pos' : 'pnl-neg';
                const gpStr = gp >= 0 ? '+$' + gp.toFixed(4) : '-$' + Math.abs(gp).toFixed(4);
                const settled = p.settled_at ? new Date(p.settled_at).toLocaleString() : '--';
                return `<tr>
                    <td>#${p.id}</td>
                    <td>${p.coin}</td>
                    <td style="font-size:10px">${p.ticker}</td>
                    <td>${yp}</td>
                    <td>${np}</td>
                    <td class="${gpClass}">${gpStr}</td>
                    <td style="font-size:10px">${settled}</td>
                </tr>`;
            }).join('');
        }
    })
    .catch(err => console.error('Refresh failed:', err));
}

refresh();
setInterval(refresh, 3000);
</script>
</body>
</html>"""


@app.route('/')
def dashboard():
    return render_template_string(DASHBOARD_HTML)


# === MAIN ===

if __name__ == '__main__':
    init_db()
    logger.info(f"Matcher starting | Trading={'LIVE' if ENABLE_TRADING else 'PAPER'} | Port={PORT}")
    logger.info(f"Config: BUY_MAX=${BUY_MAX} | CONTRACTS={CONTRACTS} | Window={MIN_MINS_TO_EXPIRY}-{MAX_MINS_TO_EXPIRY}min")

    Thread(target=bot_loop, daemon=True).start()
    app.run(host='0.0.0.0', port=PORT, debug=False)
