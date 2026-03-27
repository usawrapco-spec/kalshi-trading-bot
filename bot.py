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
ENABLE_TRADING = True  # LIVE TRADING ENABLED

# === STRATEGY ===
BUY_MIN = 0.01
BUY_MAX = 0.40
SELL_THRESHOLD = None         # no individual take profit — ride to settlement
TAKER_FEE_RATE = 0.07
MAX_MINS_TO_EXPIRY = 15
MIN_MINS_TO_EXPIRY = 10       # only buy 10-15 min window (early buys win 60%)
CYCLE_SECONDS = 2
STARTING_BALANCE = 20.00
CASH_RESERVE = 0.50
SAVINGS_RATE = 0.00
MAX_BUYS_PER_CYCLE = 5
CONTRACTS = 1
MAX_POSITIONS = 25
PORTFOLIO_TAKE_PROFIT = None  # disabled — ride to settlement for max payout

CRYPTO_SERIES = ['KXBTC15M', 'KXETH15M', 'KXSOL15M', 'KXXRP15M', 'KXDOGE15M', 'KXBTC1H']

# === CRYPTO PRICE TRACKING ===

# Map series tickers to coin symbols
SERIES_TO_COIN = {
    'KXBTC15M': 'BTC', 'KXETH15M': 'ETH', 'KXSOL15M': 'SOL',
    'KXXRP15M': 'XRP', 'KXDOGE15M': 'DOGE', 'KXBTC1H': 'BTC',
}
COIN_TO_GECKO = {
    'BTC': 'bitcoin', 'ETH': 'ethereum', 'SOL': 'solana',
    'XRP': 'ripple', 'DOGE': 'dogecoin',
}
MAX_PRICE_HISTORY = 300

price_history = {coin: [] for coin in COIN_TO_GECKO}

# Hourly contracts need longer expiry window
SERIES_MAX_EXPIRY = {
    'KXBTC1H': 60,
}
DEFAULT_MAX_EXPIRY = MAX_MINS_TO_EXPIRY

def fetch_crypto_prices():
    """Fetch real-time prices from CoinGecko for all tracked coins."""
    now = time.time()
    ids = ','.join(COIN_TO_GECKO.values())
    try:
        resp = requests.get(f"https://api.coingecko.com/api/v3/simple/price?ids={ids}&vs_currencies=usd", timeout=5)
        resp.raise_for_status()
        data = resp.json()
        for coin, gecko_id in COIN_TO_GECKO.items():
            if gecko_id in data and 'usd' in data[gecko_id]:
                price = float(data[gecko_id]['usd'])
                price_history[coin].append({'time': now, 'price': price})
                if len(price_history[coin]) > MAX_PRICE_HISTORY:
                    price_history[coin] = price_history[coin][-MAX_PRICE_HISTORY:]
    except Exception as e:
        logger.warning(f"CoinGecko price fetch failed: {e}")


def get_momentum(coin):
    """Calculate momentum for a coin. Returns dict with change_1m, change_5m, direction."""
    hist = price_history.get(coin, [])
    if not hist:
        return {'price': None, 'change_1m': 0, 'change_5m': 0, 'direction': 'flat'}

    now = time.time()
    latest = hist[-1]['price']

    # Find price ~1 min ago
    price_1m = None
    for entry in reversed(hist):
        if now - entry['time'] >= 60:
            price_1m = entry['price']
            break

    # Find price ~5 min ago
    price_5m = None
    for entry in reversed(hist):
        if now - entry['time'] >= 300:
            price_5m = entry['price']
            break

    change_1m = ((latest - price_1m) / price_1m * 100) if price_1m else 0
    change_5m = ((latest - price_5m) / price_5m * 100) if price_5m else 0

    # Direction based on 1-min change (primary) and 5-min change (secondary)
    primary = change_1m if price_1m else change_5m
    if abs(primary) < 0.1:
        direction = 'flat'
    elif primary > 0:
        direction = 'up'
    else:
        direction = 'down'

    return {'price': latest, 'change_1m': round(change_1m, 3), 'change_5m': round(change_5m, 3), 'direction': direction}


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
            for col, typ in [('series', 'TEXT'), ('mins_to_expiry', 'NUMERIC'), ('batch_id', 'INTEGER'), ('peak_bid', 'NUMERIC')]:
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
            # Cost of positions still open (money tied up)
            cur.execute("SELECT price, count FROM trades WHERE action = 'buy' AND pnl IS NULL")
            open_buys = cur.fetchall()
            open_cost = sum(sf(t['price']) * (t.get('count') or 1) for t in open_buys)
            # Total P&L from resolved trades (profit + cost recovery already happened)
            cur.execute("SELECT pnl, price, count FROM trades WHERE action = 'buy' AND pnl IS NOT NULL")
            resolved = cur.fetchall()
            total_pnl = sum(sf(t['pnl']) for t in resolved)
            # Cash = starting - money in open positions + net profit from resolved
            return max(0, STARTING_BALANCE - open_cost + total_pnl)
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

        # Update bid and peak in DB for dashboard
        current_peak = sf(trade.get('peak_bid') or 0)
        new_peak = max(current_peak, current_bid)
        conn = get_db()
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE trades SET current_bid = %s, peak_bid = %s WHERE id = %s", (float(current_bid), float(new_peak), trade['id']))
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

    # Stop buying if we have enough positions
    if len(open_positions) >= MAX_POSITIONS:
        logger.info(f"MAX POSITIONS ({MAX_POSITIONS}) reached — waiting for settlements")
        return

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

        # Per-series cap removed — using MAX_POSITIONS instead

        # Expiry filter — hourly gets longer window
        max_expiry = SERIES_MAX_EXPIRY.get(market_series, DEFAULT_MAX_EXPIRY) if market_series else DEFAULT_MAX_EXPIRY
        close_time = market.get('close_time') or market.get('expected_expiration_time')
        if close_time:
            try:
                close_dt = datetime.fromisoformat(close_time.replace('Z', '+00:00'))
                mins_left = (close_dt - now).total_seconds() / 60
                if mins_left > max_expiry or mins_left < MIN_MINS_TO_EXPIRY:
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

        # Check existing positions on this ticker
        ticker_positions = [t for t in open_positions if t.get('ticker') == ticker]
        if ticker_positions:
            # Allow dip buy: position was green at some point (peak > entry + 20%) AND current price is cheaper
            pos = ticker_positions[0]
            pos_entry = sf(pos.get('price'))
            pos_peak = sf(pos.get('peak_bid') or 0)
            if pos_entry <= 0:
                continue
            # Was it ever up 20%+ from entry?
            was_promising = pos_peak >= pos_entry * 1.20
            if not was_promising:
                continue  # never showed promise, don't add
            # Max 3 buys per ticker
            if len(ticker_positions) >= 3:
                continue
            # Dip buy: must be same side, cheaper than original entry
            pos_side = pos.get('side', '')
            if pos_side == 'yes':
                if not (BUY_MIN <= yes_ask <= BUY_MAX and yes_bid > 0 and yes_ask < pos_entry):
                    continue
                side, price, bid = 'yes', yes_ask, yes_bid
            else:
                if not (BUY_MIN <= no_ask <= BUY_MAX and no_bid > 0 and no_ask < pos_entry):
                    continue
                side, price, bid = 'no', no_ask, no_bid
            logger.info(f"  DIP BUY: {ticker} {side} was ${pos_entry:.2f} peaked ${pos_peak:.2f} now ${price:.2f}")
        else:
            # New position — buy cheapest side in range
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
    current_hot_markets = []
    for m in by_vol:
        ya = sf(m.get('yes_ask_dollars', '0'))
        na = sf(m.get('no_ask_dollars', '0'))
        spread = round(ya + na, 3)
        arb = spread < 1.0
        current_hot_markets.append({
            'ticker': m.get('ticker', ''), 'yes_ask': ya, 'no_ask': na,
            'volume': _get_volume(m), 'spread': spread, 'arb': arb
        })


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

    # Clean up settled batches
    try:
        conn = get_db()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE batches SET status = 'settled', closed_at = NOW()
                    WHERE status = 'open' AND id IN (
                        SELECT b.id FROM batches b
                        WHERE b.status = 'open'
                        AND NOT EXISTS (
                            SELECT 1 FROM trades t
                            WHERE t.batch_id = b.id AND t.action = 'buy' AND t.pnl IS NULL
                        )
                        AND EXISTS (
                            SELECT 1 FROM trades t WHERE t.batch_id = b.id
                        )
                    )
                """)
        finally:
            conn.close()
    except:
        pass

    mode = "PAPER" if not ENABLE_TRADING else "LIVE"
    balance = get_balance()
    logger.info(f"=== CYCLE START [{mode}] === Balance: ${balance:.2f}")

    # Portfolio-level take profit: sell all if open positions are +10% combined
    if PORTFOLIO_TAKE_PROFIT:
        try:
            open_pos = get_open_positions()
            if len(open_pos) >= 5:  # need at least 5 positions
                total_cost = sum(sf(t.get('price')) * (t.get('count') or 1) for t in open_pos)
                total_value = sum(sf(t.get('current_bid')) * (t.get('count') or 1) for t in open_pos)
                if total_cost > 0:
                    port_pct = (total_value - total_cost) / total_cost
                    logger.info(f"PORTFOLIO CHECK: cost=${total_cost:.2f} value=${total_value:.2f} {port_pct*100:+.1f}%")
                    if port_pct >= PORTFOLIO_TAKE_PROFIT:
                        logger.info(f"PORTFOLIO TAKE PROFIT: +{port_pct*100:.1f}% — selling all {len(open_pos)} positions")
                        sold = 0
                        total_pnl = 0
                        for trade in open_pos:
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
                                    cur.execute("UPDATE trades SET pnl = %s, current_bid = %s WHERE id = %s",
                                                (float(pnl), float(bid), trade['id']))
                            finally:
                                conn.close()
                            total_pnl += pnl
                            sold += 1
                        logger.info(f"SOLD ALL: {sold} positions, P&L=${total_pnl:.2f}")
                        _save_round(sold, total_cost, total_value, total_pnl, round(port_pct*100, 1), total_pnl, 'portfolio_take_profit')
        except Exception as e:
            logger.error(f"Portfolio take profit check failed: {e}")

    # Fetch real-time crypto prices for momentum tracking
    try:
        fetch_crypto_prices()
    except Exception as e:
        logger.error(f"Crypto price fetch failed: {e}")

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
        # Real P&L = current portfolio value minus starting balance
        overall_pnl = round((cash + positions_value) - STARTING_BALANCE, 2)

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

                closed_batches = []  # don't show closed batches in live panel
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


@app.route('/api/crypto')
def api_crypto():
    try:
        result = []
        for coin in COIN_TO_GECKO:
            m = get_momentum(coin)
            result.append({
                'coin': coin,
                'price': m['price'],
                'change_1m': m['change_1m'],
                'change_5m': m['change_5m'],
                'direction': m['direction'],
            })
        return jsonify(result)
    except Exception as e:
        logger.error(f"API crypto error: {e}")
        return jsonify([])


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
<title>Kalshi Trading Terminal</title>
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
  --blue:#4488ff;--cyan:#40d0e0;
}
body{background:var(--bg);color:var(--text);font-family:'JetBrains Mono',monospace;font-size:12px;line-height:1.5;min-height:100vh;display:flex;flex-direction:column}
a{color:var(--blue);text-decoration:none}

/* Animations */
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.25}}
@keyframes glow-green{0%,100%{box-shadow:0 0 8px rgba(0,230,138,.3)}50%{box-shadow:0 0 20px rgba(0,230,138,.6)}}
@keyframes glow-red{0%,100%{box-shadow:0 0 8px rgba(255,68,102,.3)}50%{box-shadow:0 0 20px rgba(255,68,102,.6)}}
@keyframes fadeIn{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:translateY(0)}}
@keyframes slideIn{from{opacity:0;transform:translateX(-8px)}to{opacity:1;transform:translateX(0)}}
.animate-num{transition:all .4s cubic-bezier(.4,0,.2,1)}

/* Live dot */
.live-dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;animation:pulse 1.8s ease-in-out infinite;vertical-align:middle}
.dot-paper{background:var(--gold);box-shadow:0 0 8px var(--gold)}
.dot-live{background:var(--green);box-shadow:0 0 8px var(--green)}

/* Header bar */
.header-bar{background:var(--bg1);border-bottom:1px solid var(--border);padding:10px 24px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px}
.header-left{display:flex;align-items:center;gap:16px}
.brand{font-size:13px;font-weight:700;color:var(--gold);letter-spacing:2px;text-transform:uppercase}
.mode-badge{font-size:10px;padding:3px 10px;border-radius:3px;font-weight:600;letter-spacing:1px}
.mode-paper{background:rgba(240,176,64,.15);color:var(--gold);border:1px solid rgba(240,176,64,.3)}
.mode-live{background:rgba(0,230,138,.15);color:var(--green);border:1px solid rgba(0,230,138,.3)}
.header-right{display:flex;align-items:center;gap:16px;font-size:10px;color:var(--text2)}
.countdown-box{color:var(--gold);font-weight:700;font-size:12px}

/* Main layout */
.main-wrap{flex:1;padding:16px 20px;max-width:1600px;margin:0 auto;width:100%}
.grid-layout{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:1100px){.grid-layout{grid-template-columns:1fr}}
.full-width{grid-column:1/-1}

/* Hero P&L card */
.hero-card{background:var(--bg1);border:1px solid var(--border);border-radius:8px;padding:24px 32px;text-align:center;position:relative;overflow:hidden}
.hero-card::before{content:'';position:absolute;top:0;left:0;right:0;height:2px;background:linear-gradient(90deg,transparent,var(--gold),transparent)}
.hero-label{font-size:10px;color:var(--text2);text-transform:uppercase;letter-spacing:2px;margin-bottom:6px}
.hero-value{font-size:40px;font-weight:800;letter-spacing:-1px;line-height:1.1}
.hero-sub{display:flex;justify-content:center;gap:32px;margin-top:14px;flex-wrap:wrap}
.hero-sub-item{text-align:center}
.hero-sub-label{font-size:9px;color:var(--text3);text-transform:uppercase;letter-spacing:1px}
.hero-sub-value{font-size:15px;font-weight:600;margin-top:2px}

/* Stats bar */
.stats-bar{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px}
.stat-card{background:var(--bg1);border:1px solid var(--border);border-radius:6px;padding:12px 14px;position:relative;overflow:hidden}
.stat-card::after{content:'';position:absolute;bottom:0;left:0;right:0;height:1px}
.stat-card.accent-green::after{background:var(--green)}
.stat-card.accent-red::after{background:var(--red)}
.stat-card.accent-gold::after{background:var(--gold)}
.stat-card.accent-blue::after{background:var(--blue)}
.stat-label{font-size:9px;color:var(--text3);text-transform:uppercase;letter-spacing:1px;margin-bottom:4px}
.stat-value{font-size:15px;font-weight:700}

/* Panel */
.panel{background:var(--bg1);border:1px solid var(--border);border-radius:8px;overflow:hidden;display:flex;flex-direction:column}
.panel-header{padding:12px 16px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center;background:var(--bg2)}
.panel-header h2{color:var(--gold);font-size:11px;text-transform:uppercase;letter-spacing:1.5px;font-weight:600;display:flex;align-items:center;gap:8px}
.panel-header h2::before{content:'';display:inline-block;width:3px;height:12px;background:var(--gold);border-radius:1px}
.panel-header .count{color:var(--text2);font-size:10px}
.panel-body{max-height:380px;overflow-y:auto;flex:1}
.panel-body::-webkit-scrollbar{width:4px}
.panel-body::-webkit-scrollbar-track{background:var(--bg1)}
.panel-body::-webkit-scrollbar-thumb{background:var(--border2);border-radius:2px}
.panel-body::-webkit-scrollbar-thumb:hover{background:var(--text3)}

/* Tables */
table{width:100%;border-collapse:collapse;font-size:11px}
th{color:var(--text3);text-align:left;padding:8px 10px;border-bottom:1px solid var(--border);text-transform:uppercase;font-size:9px;letter-spacing:.8px;font-weight:600;position:sticky;top:0;background:var(--bg1);z-index:1}
td{padding:7px 10px;border-bottom:1px solid rgba(26,34,53,.5)}
tr{transition:background .15s ease}
tr.row-green{background:var(--green-bg)}
tr.row-red{background:var(--red-bg)}
tr:hover{background:var(--bg3) !important}
.green{color:var(--green)}.red{color:var(--red)}.gray{color:var(--text3)}.gold{color:var(--gold)}

/* Batch progress bars */
.batch-progress{width:100%;height:4px;background:var(--bg);border-radius:2px;overflow:hidden;margin-top:3px}
.batch-progress-fill{height:100%;border-radius:2px;transition:width .4s ease}
.batch-row-gold{border-left:2px solid var(--gold) !important}

/* Learning engine cards */
.learn-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;padding:14px}
.learn-card{background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:12px 14px}
.learn-card-title{font-size:10px;color:var(--gold);text-transform:uppercase;letter-spacing:1.5px;font-weight:600;margin-bottom:10px;padding-bottom:6px;border-bottom:1px solid var(--border)}
.learn-item{display:flex;align-items:center;gap:8px;margin-bottom:6px;font-size:11px}
.learn-bar-wrap{flex:1;height:6px;background:var(--bg2);border-radius:3px;overflow:hidden}
.learn-bar{height:100%;border-radius:3px;transition:width .4s ease}
.learn-badge{font-size:8px;font-weight:700;padding:1px 5px;border-radius:2px;letter-spacing:.5px;min-width:30px;text-align:center}
.learn-badge-buy{background:rgba(0,230,138,.15);color:var(--green)}
.learn-badge-skip{background:rgba(255,68,102,.15);color:var(--red)}

/* Round comparison highlight */
.winner-cell{position:relative}
.winner-cell::after{content:'WIN';position:absolute;top:50%;right:2px;transform:translateY(-50%);font-size:7px;font-weight:700;color:var(--green);background:rgba(0,230,138,.15);padding:1px 3px;border-radius:2px;letter-spacing:.5px}

/* Status bar */
.status-bar{background:var(--bg1);border-top:1px solid var(--border);padding:8px 24px;display:flex;justify-content:space-between;flex-wrap:wrap;gap:8px;font-size:10px;color:var(--text3)}
.status-bar span{display:flex;align-items:center;gap:6px}
.status-sep{color:var(--border2)}

/* Loading */
.loading{color:var(--text3);text-align:center;padding:24px;font-size:11px}

/* Empty state */
.empty-state{color:var(--text3);text-align:center;padding:24px;font-size:11px;font-style:italic}
</style>
</head>
<body>

<!-- Header Bar -->
<div class="header-bar">
  <div class="header-left">
    <span class="brand">Kalshi Scalper</span>
    <span class="live-dot dot-paper" id="mode-dot"></span>
    <span class="mode-badge mode-paper" id="mode-badge">PAPER MODE</span>
  </div>
  <div class="header-right">
    <span>Buy $0.01 - $0.40</span>
    <span class="status-sep">|</span>
    <span>Sell +150% / Portfolio +100%</span>
    <span class="status-sep">|</span>
    <span>Next cycle: <span class="countdown-box" id="countdown">--:--</span></span>
  </div>
</div>
<div style="background:var(--bg1);border-bottom:1px solid var(--border);padding:10px 24px;font-size:11px;color:var(--text2);line-height:1.6">
  <strong style="color:var(--gold)">STRATEGY:</strong> Buy the cheapest side (yes or no) of 15-min crypto contracts in the first 5 minutes of each window (10-15 min before expiry). Price range $0.01-$0.40. One buy per ticker, 10 contracts each. Dip buy if position showed +20% promise then dipped below entry. Individual take profit at +150%. Sell entire portfolio if combined open positions hit +100%. Otherwise ride to settlement. Winners pay $1.00, losers pay $0.
</div>

<!-- Main Content -->
<div class="main-wrap">

<!-- Hero P&L -->
<div class="hero-card full-width" id="hero-card" style="margin-bottom:14px">
  <div class="hero-label">Overall Profit &amp; Loss</div>
  <div class="hero-value animate-num" id="tb-overall">...</div>
  <div class="hero-sub">
    <div class="hero-sub-item">
      <div class="hero-sub-label">Cash</div>
      <div class="hero-sub-value animate-num" id="tb-hero-cash">...</div>
    </div>
    <div class="hero-sub-item">
      <div class="hero-sub-label">Open Positions</div>
      <div class="hero-sub-value animate-num" id="tb-hero-positions">...</div>
    </div>
    <div class="hero-sub-item">
      <div class="hero-sub-label">Fees Paid</div>
      <div class="hero-sub-value animate-num red" id="tb-hero-fees">...</div>
    </div>
    <div class="hero-sub-item">
      <div class="hero-sub-label">Cash Out Value</div>
      <div class="hero-sub-value animate-num" id="tb-cashout" style="font-size:14px;font-weight:700">...</div>
    </div>
  </div>
</div>

<!-- Stats Bar -->
<div class="stats-bar full-width" style="margin-bottom:14px">
  <div class="stat-card accent-blue">
    <div class="stat-label">Cash</div>
    <div class="stat-value" id="tb-cash">...</div>
  </div>
  <div class="stat-card accent-gold">
    <div class="stat-label">Positions Value</div>
    <div class="stat-value" id="tb-positions">...</div>
  </div>
  <div class="stat-card accent-green">
    <div class="stat-label">Record</div>
    <div class="stat-value" id="tb-record">...</div>
  </div>
  <div class="stat-card accent-red">
    <div class="stat-label">Fees Paid</div>
    <div class="stat-value" id="tb-fees">...</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Avg Return</div>
    <div class="stat-value" id="tb-avgret">...</div>
  </div>
  <div class="stat-card accent-green">
    <div class="stat-label">Avg Win</div>
    <div class="stat-value green" id="tb-avgwin">...</div>
  </div>
  <div class="stat-card accent-red">
    <div class="stat-label">Avg Loss</div>
    <div class="stat-value red" id="tb-avgloss">...</div>
  </div>
</div>

<!-- Crypto Prices -->
<div class="panel full-width" style="margin-bottom:14px">
  <div class="panel-header"><h2>Crypto Prices</h2><div class="count" id="crypto-count">Real-time from Binance</div></div>
  <div class="panel-body" style="max-height:200px"><table><thead><tr>
    <th>Coin</th><th>Price</th><th>1m Change</th><th>5m Change</th><th>Direction</th>
  </tr></thead><tbody id="crypto-body"><tr><td colspan="5" class="loading">Loading...</td></tr></tbody></table></div>
</div>

<!-- Grid layout -->
<div class="grid-layout">

<!-- Live Batches -->
<div class="panel full-width">
  <div class="panel-header"><h2>Live Batches</h2><div class="count" id="batch-count"></div></div>
  <div class="panel-body"><table><thead><tr>
    <th>Batch</th><th>Age</th><th>Trades</th><th>Cost</th><th>Value</th><th>P&amp;L</th><th>Return</th><th>Peak</th><th>Progress</th><th>Status</th>
  </tr></thead><tbody id="batch-body"><tr><td colspan="10" class="loading">Loading...</td></tr></tbody></table></div>
</div>

<!-- Learning Engine -->
<div class="panel full-width">
  <div class="panel-header"><h2>Learning Engine</h2><div class="count" id="learn-status"></div></div>
  <div class="panel-body" id="learn-body" style="padding:0"><div class="loading">Loading...</div></div>
</div>

<!-- Open Positions -->
<div class="panel">
  <div class="panel-header"><h2>Open Positions <span id="open-pct" style="font-size:12px;font-weight:700"></span> <span id="open-target" style="font-size:9px;color:var(--text3)">/ 10% to sell</span></h2><div class="count" id="open-count"></div></div>
  <div class="panel-body"><table><thead><tr>
    <th>Ticker</th><th>Side</th><th>Qty</th><th>Entry</th><th>Cost</th><th>Bid</th><th>Value</th><th>P&amp;L</th><th>Gain%</th>
  </tr></thead><tbody id="open-body"><tr><td colspan="9" class="loading">Loading...</td></tr></tbody></table></div>
</div>

<!-- Recent Trades -->
<div class="panel">
  <div class="panel-header"><h2>Recent Trades</h2><div class="count" id="trades-count"></div></div>
  <div class="panel-body"><table><thead><tr>
    <th>Time</th><th>Ticker</th><th>Side</th><th>Qty</th><th>Entry</th><th>Exit</th><th>P&amp;L</th><th>Gain%</th>
  </tr></thead><tbody id="trades-body"><tr><td colspan="8" class="loading">Loading...</td></tr></tbody></table></div>
</div>

<!-- Round History -->
<div class="panel full-width">
  <div class="panel-header"><h2>Round History</h2><div class="count" id="rounds-count"></div></div>
  <div class="panel-body"><table><thead><tr>
    <th>Time</th><th>Positions</th><th>Cost</th><th>Sold P&amp;L</th><th>Sold %</th><th>Hold P&amp;L</th><th>Hold %</th><th>Peak</th><th>Exit</th><th>Better</th>
  </tr></thead><tbody id="rounds-body"><tr><td colspan="10" class="loading">Loading...</td></tr></tbody></table></div>
</div>

<!-- Hot Markets -->
<div class="panel full-width">
  <div class="panel-header"><h2>Hot Markets</h2><div class="count" id="hot-count"></div></div>
  <div class="panel-body" style="max-height:240px"><table><thead><tr>
    <th>Ticker</th><th>Yes Ask</th><th>No Ask</th><th>Spread</th><th>Arb?</th><th>Volume</th>
  </tr></thead><tbody id="hot-body"><tr><td colspan="4" class="loading">Loading...</td></tr></tbody></table></div>
</div>

</div><!-- /grid-layout -->
</div><!-- /main-wrap -->

<!-- Status Bar -->
<div class="status-bar">
  <span id="sb-config">Buy $0.01-$0.40 | Sell +150% / Portfolio +100% | 10-15min window | 1 per ticker + dip buys</span>
  <span class="status-sep">|</span>
  <span id="sb-mode">Mode: <span class="gold" id="mode-label">PAPER</span></span>
  <span class="status-sep">|</span>
  <span>Cycle: <span id="sb-cycle">2s</span></span>
  <span class="status-sep">|</span>
  <span>Savings: <span id="tb-savings" class="gold">$0.00</span></span>
  <span class="status-sep">|</span>
  <span>Updated: <span id="last-update" style="color:var(--text)">&mdash;</span></span>
  <span style="margin-left:auto;color:var(--text3)">Kalshi Terminal v8 &mdash; 5s refresh</span>
</div>

<script>
function $(id){return document.getElementById(id)}
function cls(v){return v>0?'green':v<0?'red':'gray'}
function esc(s){var d=document.createElement('div');d.textContent=s;return d.innerHTML}
async function fetchJSON(url){try{var r=await fetch(url);return await r.json()}catch(e){return null}}
function fmtVol(v){if(v>=1e6)return(v/1e6).toFixed(1)+'M';if(v>=1e3)return(v/1e3).toFixed(1)+'K';return v.toString()}
function fmtPnl(v){return(v>=0?'+$':'-$')+Math.abs(v).toFixed(2)}
function fmtPct(v){return(v>=0?'+':'')+v.toFixed(1)+'%'}
function timeAgo(iso){
  if(!iso)return '--';
  var diff=Math.floor((Date.now()-new Date(iso).getTime())/1000);
  if(diff<60)return diff+'s ago';if(diff<3600)return Math.floor(diff/60)+'m ago';
  if(diff<86400)return Math.floor(diff/3600)+'h ago';return Math.floor(diff/86400)+'d ago';
}

async function refresh(){
  var [status,open,trades,hot,rounds,learn,batches,crypto]=await Promise.all([
    fetchJSON('/api/status'),fetchJSON('/api/open'),fetchJSON('/api/trades'),fetchJSON('/api/hot'),fetchJSON('/api/rounds'),fetchJSON('/api/learning'),fetchJSON('/api/batches'),fetchJSON('/api/crypto')
  ]);

  if(status){
    checkCelebrations(status);
    var ov=status.overall_pnl||0;
    var ovCls=cls(ov);
    $('tb-overall').innerHTML='<span class="'+ovCls+'">'+fmtPnl(ov)+'</span>';
    $('hero-card').style.borderColor=ov>0?'var(--green)':ov<0?'var(--red)':'var(--border)';
    var topGlow=ov>0?'linear-gradient(90deg,transparent,var(--green),transparent)':ov<0?'linear-gradient(90deg,transparent,var(--red),transparent)':'linear-gradient(90deg,transparent,var(--gold),transparent)';
    $('hero-card').querySelector('.hero-label').parentElement.style.setProperty('--top-glow',topGlow);
    document.querySelector('.hero-card::before');
    $('tb-hero-cash').innerHTML='<span style="color:var(--blue)">$'+(status.cash||0).toFixed(2)+'</span>';
    $('tb-hero-positions').innerHTML='<span style="color:var(--gold)">$'+(status.positions_value||0).toFixed(2)+'</span>';
    $('tb-hero-fees').innerHTML='-$'+(status.total_fees||0).toFixed(2);
    var cashout=(status.cash||0)+(status.positions_value||0);
    var cashoutProfit=status.overall_pnl||0;
    $('tb-cashout').innerHTML='$'+cashout.toFixed(2)+' <span style="font-size:11px" class="'+cls(cashoutProfit)+'">('+fmtPnl(cashoutProfit)+')</span>';
    $('tb-positions').innerHTML='<span style="color:var(--gold)">$'+(status.positions_value||0).toFixed(2)+'</span>';
    $('tb-cash').innerHTML='<span style="color:var(--blue)">$'+(status.cash||0).toFixed(2)+'</span>';
    $('tb-fees').innerHTML='<span class="red">-$'+(status.total_fees||0).toFixed(2)+'</span> <span style="font-size:9px;color:var(--text3)">'+(status.total_contracts||0)+'c</span>';
    $('tb-record').innerHTML='<span class="green">'+(status.wins||0)+'W</span> <span style="color:var(--text3)">/</span> <span class="red">'+(status.losses||0)+'L</span>';
    if(status.expired){$('tb-record').innerHTML+=' <span style="color:var(--text3)">/</span> <span class="gray">'+(status.expired||0)+'E</span>';}
    var ar=status.avg_return||0;
    $('tb-avgret').innerHTML='<span class="'+cls(ar)+'">'+fmtPct(ar)+'</span>';
    $('tb-avgwin').textContent='+$'+(status.avg_win||0).toFixed(4);
    $('tb-avgloss').textContent='-$'+Math.abs(status.avg_loss||0).toFixed(4);
    $('tb-savings').textContent='$'+(status.savings||0).toFixed(2);
    var mode=status.mode||'PAPER';
    $('mode-label').textContent=mode;
    $('mode-badge').textContent=mode==='LIVE'?'LIVE TRADING':'PAPER MODE';
    $('mode-badge').className='mode-badge '+(mode==='LIVE'?'mode-live':'mode-paper');
    $('mode-dot').className='live-dot '+(mode==='LIVE'?'dot-live':'dot-paper');
  }

  if(open){
    $('open-count').textContent=open.length+' positions';
    var totalCost=0,totalVal=0;
    open.forEach(function(p){totalCost+=p.entry*(p.count||1);totalVal+=(p.current_bid||0)*(p.count||1)});
    var openPct=totalCost>0?((totalVal-totalCost)/totalCost*100):0;
    $('open-pct').innerHTML='<span class="'+cls(openPct)+'">'+(openPct>=0?'+':'')+openPct.toFixed(1)+'%</span>';
    var h='';
    open.forEach(function(p){
      var rc=p.gain_pct>2?'row-green':p.gain_pct<-2?'row-red':'';
      var gc=cls(p.gain_pct);
      var bidText=p.current_bid<=0?'<span class="red">EXPIRED</span>':'$'+p.current_bid.toFixed(2);
      var valText=p.current_bid<=0?'$0.00':'$'+(p.bid_total||0).toFixed(2);
      h+='<tr class="'+rc+'" style="animation:fadeIn .3s ease">';
      h+='<td style="font-size:10px;max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="'+esc(p.ticker)+'">'+esc(p.ticker)+'</td>';
      h+='<td><span style="color:'+(p.side==='yes'?'var(--green)':'var(--red)')+'">'+esc(p.side).toUpperCase()+'</span></td>';
      h+='<td>'+p.count+'</td>';
      h+='<td>$'+p.entry.toFixed(2)+'</td>';
      h+='<td>$'+(p.entry_total||0).toFixed(2)+'</td>';
      h+='<td>'+bidText+'</td>';
      h+='<td class="'+gc+'">'+valText+'</td>';
      h+='<td class="'+gc+'">'+(p.unrealized>=0?'+':'')+p.unrealized.toFixed(4)+'</td>';
      h+='<td class="'+gc+'" style="font-weight:600">'+(p.gain_pct>=0?'+':'')+p.gain_pct.toFixed(0)+'%</td>';
      h+='</tr>';
    });
    $('open-body').innerHTML=h||'<tr><td colspan="9" class="empty-state">No open positions</td></tr>';
  }

  if(trades){
    $('trades-count').textContent=trades.length+' trades';
    var h='';
    trades.forEach(function(t){
      var pc=cls(t.pnl);var rc=t.pnl>0?'row-green':t.pnl<0?'row-red':'';
      h+='<tr class="'+rc+'">';
      h+='<td style="color:var(--text2)">'+timeAgo(t.created_at)+'</td>';
      h+='<td style="font-size:10px;max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="'+esc(t.ticker||'')+'">'+esc(t.ticker||'')+'</td>';
      h+='<td><span style="color:'+(t.side==='yes'?'var(--green)':'var(--red)')+'">'+esc(t.side||'').toUpperCase()+'</span></td>';
      h+='<td>'+(t.count||1)+'</td>';
      h+='<td>$'+(t.entry||0).toFixed(2)+'</td>';
      h+='<td>$'+(t.exit||0).toFixed(2)+'</td>';
      h+='<td class="'+pc+'" style="font-weight:600">'+(t.pnl>=0?'+':'')+t.pnl.toFixed(4)+'</td>';
      h+='<td class="'+cls(t.gain_pct||0)+'">'+(t.gain_pct>=0?'+':'')+(t.gain_pct||0).toFixed(0)+'%</td>';
      h+='</tr>';
    });
    $('trades-body').innerHTML=h||'<tr><td colspan="8" class="empty-state">No trades yet</td></tr>';
  }

  if(hot){
    $('hot-count').textContent='Top '+hot.length+' by volume';
    var h='';
    hot.forEach(function(m){
      var spread=m.spread||((m.yes_ask||0)+(m.no_ask||0));
      var isArb=spread<1.0;
      h+='<tr'+(isArb?' style="background:rgba(0,230,138,0.15)"':'')+'>';
      h+='<td style="font-size:10px;max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="'+esc(m.ticker)+'">'+esc(m.ticker)+'</td>';
      h+='<td class="green">$'+(m.yes_ask||0).toFixed(2)+'</td>';
      h+='<td class="red">$'+(m.no_ask||0).toFixed(2)+'</td>';
      h+='<td style="color:'+(spread<1.0?'var(--green)':spread<1.02?'var(--gold)':'var(--text2)')+'">$'+spread.toFixed(3)+'</td>';
      h+='<td>'+(isArb?'<span style="color:var(--green);font-weight:700">YES!</span>':'<span style="color:var(--text3)">no</span>')+'</td>';
      h+='<td style="color:var(--gold);font-weight:700">'+fmtVol(m.volume)+'</td>';
      h+='</tr>';
    });
    $('hot-body').innerHTML=h||'<tr><td colspan="6" class="empty-state">No data yet</td></tr>';
  }

  if(batches){
    var openB=batches.filter(function(b){return b.status==='open'});
    var closedB=batches.filter(function(b){return b.status!=='open'});
    $('batch-count').textContent=openB.length+' open / '+closedB.length+' closed';
    var h='';
    batches.forEach(function(b){
      var pc=cls(b.pnl);var rc=b.pnl>0?'row-green':b.pnl<0?'row-red':'';
      var peak=b.peak||0;
      var pnlRatio=peak>0?Math.min(100,Math.max(0,(b.pnl/peak)*100)):0;
      var nearThreshold=b.status==='open'&&b.pnl_pct>=20;
      var rowCls=rc+(nearThreshold?' batch-row-gold':'');
      var barColor=b.pnl>0?'var(--green)':b.pnl<0?'var(--red)':'var(--text3)';
      var st=b.status==='open'?'<span style="color:var(--gold);font-weight:700"><span class="live-dot dot-live" style="width:6px;height:6px"></span>LIVE</span>':'<span class="'+pc+'">'+b.status.toUpperCase()+'</span>';
      h+='<tr class="'+rowCls+'">';
      h+='<td style="font-weight:600">#'+b.id+'</td>';
      h+='<td style="color:var(--text2)">'+timeAgo(b.created_at)+'</td>';
      h+='<td>'+b.trades+'</td>';
      h+='<td>$'+(b.cost||0).toFixed(2)+'</td>';
      h+='<td>$'+(b.value||0).toFixed(2)+'</td>';
      h+='<td class="'+pc+'" style="font-weight:600">'+fmtPnl(b.pnl)+'</td>';
      h+='<td class="'+pc+'">'+(b.pnl_pct>=0?'+':'')+(b.pnl_pct||0).toFixed(1)+'%</td>';
      h+='<td class="green">+$'+peak.toFixed(2)+'</td>';
      h+='<td style="min-width:60px"><div class="batch-progress"><div class="batch-progress-fill" style="width:'+Math.abs(pnlRatio)+'%;background:'+barColor+'"></div></div></td>';
      h+='<td>'+st+'</td>';
      h+='</tr>';
    });
    $('batch-body').innerHTML=h||'<tr><td colspan="10" class="empty-state">No batches yet</td></tr>';
  }

  if(rounds){
    $('rounds-count').textContent=rounds.length+' rounds';
    var h='';
    rounds.forEach(function(r){
      var pc=cls(r.pnl);var rc=r.pnl>0?'row-green':r.pnl<0?'row-red':'';
      var hp=r.hold_pnl!==null?r.hold_pnl:null;
      var hpc=hp!==null?cls(hp):'gray';
      var soldWon=hp!==null&&r.pnl>=hp;
      var holdWon=hp!==null&&hp>r.pnl;
      h+='<tr class="'+rc+'">';
      h+='<td style="color:var(--text2)">'+timeAgo(r.ended_at)+'</td>';
      h+='<td>'+r.positions+'</td>';
      h+='<td>$'+(r.cost||0).toFixed(2)+'</td>';
      h+='<td class="'+pc+'" style="font-weight:'+(soldWon?'700':'400')+'">'+(r.pnl>=0?'+':'')+r.pnl.toFixed(2)+(soldWon?' *':'')+'</td>';
      h+='<td class="'+pc+'">'+(r.pnl_pct>=0?'+':'')+(r.pnl_pct||0).toFixed(1)+'%</td>';
      h+='<td class="'+hpc+'" style="font-weight:'+(holdWon?'700':'400')+'">'+(hp!==null?(hp>=0?'+':'')+hp.toFixed(2)+(holdWon?' *':''):'<span style="color:var(--text3);font-style:italic">pending</span>')+'</td>';
      h+='<td class="'+hpc+'">'+(r.hold_pnl_pct!==null?(r.hold_pnl_pct>=0?'+':'')+(r.hold_pnl_pct||0).toFixed(1)+'%':'<span style="color:var(--text3)">...</span>')+'</td>';
      h+='<td class="green">+$'+(r.peak||0).toFixed(2)+'</td>';
      h+='<td style="font-size:10px;color:var(--text2)">'+esc(r.exit_reason||'')+'</td>';
      var betterLabel='--';
      if(hp!==null){
        if(r.pnl>hp)betterLabel='<span class="green" style="font-weight:700">SOLD</span>';
        else if(hp>r.pnl)betterLabel='<span class="red" style="font-weight:700">HOLD</span>';
        else betterLabel='<span class="gray">TIE</span>';
      }
      h+='<td>'+betterLabel+'</td>';
      h+='</tr>';
    });
    $('rounds-body').innerHTML=h||'<tr><td colspan="10" class="empty-state">No rounds yet</td></tr>';
  }

  if(learn){
    if(!learn.active){
      $('learn-status').textContent='collecting data...';
      $('learn-body').innerHTML='<div style="padding:20px;text-align:center;color:var(--text3)">'+esc(learn.message||'Waiting for sufficient trade data...')+'</div>';
    }else{
      $('learn-status').innerHTML='<span class="green" style="font-weight:600">ACTIVE</span> <span style="color:var(--text3)">(min '+learn.min_win_rate+'% win rate)</span>';
      var h='<div class="learn-grid">';
      var labels={'price_bucket':'Price Range','side':'Trade Side','series':'Series','time_bucket':'Timing'};
      var icons={'price_bucket':'$','side':'S','series':'#','time_bucket':'T'};
      for(var cat in learn.categories){
        h+='<div class="learn-card">';
        h+='<div class="learn-card-title">'+icons[cat]+' '+(labels[cat]||cat)+'</div>';
        learn.categories[cat].forEach(function(item){
          var barColor=item.pass?'var(--green)':'var(--red)';
          var badgeCls=item.pass?'learn-badge-buy':'learn-badge-skip';
          var badgeText=item.pass?'BUY':'SKIP';
          var barWidth=Math.min(100,Math.max(5,item.win_rate));
          h+='<div class="learn-item">';
          h+='<span class="learn-badge '+badgeCls+'">'+badgeText+'</span>';
          h+='<span style="min-width:60px;font-size:10px;color:var(--text)">'+esc(item.label)+'</span>';
          h+='<div class="learn-bar-wrap"><div class="learn-bar" style="width:'+barWidth+'%;background:'+barColor+'"></div></div>';
          h+='<span style="font-size:10px;font-weight:600;color:'+barColor+';min-width:36px;text-align:right">'+item.win_rate+'%</span>';
          h+='</div>';
        });
        h+='</div>';
      }
      h+='</div>';
      $('learn-body').innerHTML=h;
    }
  }
  if(crypto){
    var h='';
    crypto.forEach(function(c){
      var dirArrow='<span class="gray">-</span>';
      if(c.direction==='up')dirArrow='<span class="green" style="font-weight:700;font-size:14px">&#9650;</span>';
      else if(c.direction==='down')dirArrow='<span class="red" style="font-weight:700;font-size:14px">&#9660;</span>';
      var c1=cls(c.change_1m);var c5=cls(c.change_5m);
      var priceStr=c.price!==null?'$'+c.price.toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:c.price<1?6:2}):'--';
      h+='<tr>';
      h+='<td style="font-weight:700;color:var(--gold)">'+esc(c.coin)+'</td>';
      h+='<td style="font-weight:600">'+priceStr+'</td>';
      h+='<td class="'+c1+'" style="font-weight:600">'+(c.change_1m>=0?'+':'')+c.change_1m.toFixed(3)+'%</td>';
      h+='<td class="'+c5+'" style="font-weight:600">'+(c.change_5m>=0?'+':'')+c.change_5m.toFixed(3)+'%</td>';
      h+='<td>'+dirArrow+' <span style="font-size:10px;color:var(--text2)">'+esc(c.direction)+'</span></td>';
      h+='</tr>';
    });
    $('crypto-body').innerHTML=h||'<tr><td colspan="5" class="empty-state">Waiting for price data...</td></tr>';
  }

  $('last-update').textContent=new Date().toLocaleTimeString();
}

var _prevRoundsCount=0;
var _prevOverall=0;

function confetti(){
  var colors=['#00e68a','#f0b040','#4488ff','#40d0e0','#ff4466','#fff'];
  var container=document.createElement('div');
  container.style.cssText='position:fixed;top:0;left:0;width:100%;height:100%;pointer-events:none;z-index:9999;overflow:hidden';
  document.body.appendChild(container);
  for(var i=0;i<80;i++){
    var p=document.createElement('div');
    var c=colors[Math.floor(Math.random()*colors.length)];
    var x=Math.random()*100;
    var d=Math.random()*3+2;
    var r=Math.random()*360;
    p.style.cssText='position:absolute;left:'+x+'%;top:-20px;width:'+Math.random()*8+4+'px;height:'+Math.random()*12+4+'px;background:'+c+';opacity:0.9;border-radius:2px;transform:rotate('+r+'deg);animation:confetti-fall '+d+'s ease-out forwards;animation-delay:'+Math.random()*0.5+'s';
    container.appendChild(p);
  }
  setTimeout(function(){container.remove()},4000);
}

function flashProfit(amount){
  var el=document.createElement('div');
  el.innerHTML='+$'+Math.abs(amount).toFixed(2);
  el.style.cssText='position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);font-size:72px;font-weight:800;color:#00e68a;text-shadow:0 0 40px rgba(0,230,138,0.6),0 0 80px rgba(0,230,138,0.3);z-index:9998;pointer-events:none;animation:profit-flash 2s ease-out forwards;font-family:JetBrains Mono,monospace';
  document.body.appendChild(el);
  setTimeout(function(){el.remove()},2500);
}

// Add confetti + flash animations
var style=document.createElement('style');
style.textContent='@keyframes confetti-fall{0%{top:-20px;opacity:1;transform:rotate(0deg) translateX(0)}100%{top:110vh;opacity:0;transform:rotate(720deg) translateX('+(Math.random()>0.5?'':'-')+'100px)}}@keyframes profit-flash{0%{opacity:0;transform:translate(-50%,-50%) scale(0.5)}20%{opacity:1;transform:translate(-50%,-50%) scale(1.1)}40%{transform:translate(-50%,-50%) scale(1)}100%{opacity:0;transform:translate(-50%,-60%) scale(1)}}';
document.head.appendChild(style);

function checkCelebrations(status){
  var rc=status.all_rounds_count||0;
  if(_prevRoundsCount>0 && rc>_prevRoundsCount){
    // New round completed — check if it was profitable
    var diff=(status.all_rounds_pnl||0)-_prevOverall;
    if(diff>0){
      confetti();
      flashProfit(diff);
    }
  }
  _prevRoundsCount=rc;
  _prevOverall=status.all_rounds_pnl||0;
}

refresh();
setInterval(refresh,5000);

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
