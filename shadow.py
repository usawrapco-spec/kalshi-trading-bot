"""
SHADOW — Paper trading bot. Mirrors RAZOR logic but never places real orders.
Buys ALL price ranges ($0.01-$0.99), no duplicate ticker restriction.
Tracks everything in shadow_trades table. Flask Blueprint.
"""

import os, time, logging, traceback, math
from datetime import datetime, timezone
from flask import Blueprint, jsonify
import psycopg2
from psycopg2.extras import RealDictCursor
from kalshi_auth import KalshiAuth
import requests

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger('shadow')

# === CONFIG ===
KALSHI_HOST = os.environ.get('KALSHI_API_HOST', 'https://api.elections.kalshi.com')
DATABASE_URL = os.environ.get('DATABASE_URL', 'postgresql://kalshi:kalshi@localhost:5432/kalshi')
ENABLE_TRADING = False  # PAPER MODE — never places real orders

# === STRATEGY ===
BUY_MIN = 0.01
BUY_MAX = 0.99
TAKER_FEE_RATE = 0.07
MAX_MINS_TO_EXPIRY = 15
MIN_MINS_TO_BUY = 10          # only buy when 10-15 min left (first 5 min of window)
CYCLE_SECONDS = 2
CONTRACTS = 1
MAX_POSITIONS = 10
CUT_LOSS_AFTER_MINS = 5
CUT_LOSS_THRESHOLD = -0.70

CRYPTO_SERIES = ['KXBTC15M', 'KXETH15M', 'KXSOL15M', 'KXXRP15M', 'KXDOGE15M']

# === INIT ===
auth = KalshiAuth()
shadow_bp = Blueprint('shadow', __name__)

# Shadow's own market data cache
_shadow_cache = {
    'markets': {},          # ticker -> market data
}


def sf(val):
    try:
        return float(val) if val is not None else 0.0
    except:
        return 0.0


def kalshi_fee(price, count):
    return min(math.ceil(TAKER_FEE_RATE * count * price * (1 - price) * 100) / 100, 0.02 * count)


# === DATABASE ===

def get_db():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    return conn


def init_shadow_db():
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS shadow_trades (
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
                cur.execute("ALTER TABLE shadow_trades ADD COLUMN fees NUMERIC DEFAULT 0")
            except:
                pass
    finally:
        conn.close()
    logger.info("Shadow database initialized")


# === KALSHI API ===

def kalshi_get(path):
    url = f"{KALSHI_HOST}/trade-api/v2{path}"
    headers = auth.get_headers("GET", f"/trade-api/v2{path}")
    resp = requests.get(url, headers=headers, timeout=10)
    resp.raise_for_status()
    return resp.json()


def get_market(ticker):
    try:
        resp = kalshi_get(f"/markets/{ticker}")
        return resp.get('market', resp)
    except:
        return None


# === CORE LOGIC ===

def get_open_positions():
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM shadow_trades WHERE status = 'open' ORDER BY bought_at")
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
            logger.error(f"Shadow fetch {series} failed: {e}")
    return all_markets


def find_candidates(markets):
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

        if BUY_MIN <= yes_ask <= BUY_MAX:
            candidates.append({'ticker': ticker, 'side': 'yes', 'price': yes_ask, 'mins_left': mins_left})
        if BUY_MIN <= no_ask <= BUY_MAX:
            candidates.append({'ticker': ticker, 'side': 'no', 'price': no_ask, 'mins_left': mins_left})

    candidates.sort(key=lambda x: x['price'])
    return candidates


def check_sells():
    """Update bids, handle settlements, cut -70% losers at 5min."""
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
                logger.info(f"SHADOW SETTLED: {ticker} {side} ${entry:.2f} -> {reason} pnl=${pnl:.4f}")
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE shadow_trades SET pnl=%s, fees=%s, status='closed', closed_at=NOW(), close_reason=%s, current_bid=%s WHERE id=%s",
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
                cur.execute("UPDATE shadow_trades SET current_bid=%s WHERE id=%s",
                            (float(current_bid), trade_id))

            if current_bid <= 0:
                continue

            # === 5-MINUTE CUT LOSS CHECK ===
            bought_at = trade['bought_at']
            if bought_at.tzinfo is None:
                bought_at = bought_at.replace(tzinfo=timezone.utc)
            mins_held = (now - bought_at).total_seconds() / 60

            if mins_held >= CUT_LOSS_AFTER_MINS:
                gain = (current_bid - entry) / entry
                if gain <= CUT_LOSS_THRESHOLD:
                    buy_fee = kalshi_fee(entry, count)
                    sell_fee = kalshi_fee(current_bid, count)
                    total_fees = buy_fee + sell_fee
                    pnl = round((current_bid - entry) * count - total_fees, 4)
                    logger.info(f"SHADOW CUT LOSS: {ticker} {side} ${entry:.2f} -> ${current_bid:.2f} ({gain*100:+.0f}%) pnl=${pnl:.4f}")
                    # Paper mode: just update DB, no real order
                    with conn.cursor() as cur:
                        cur.execute(
                            "UPDATE shadow_trades SET pnl=%s, fees=%s, status='closed', closed_at=NOW(), close_reason='cut_loss', current_bid=%s WHERE id=%s",
                            (float(pnl), float(total_fees), float(current_bid), trade_id)
                        )
    finally:
        conn.close()


def buy_cheapest(markets):
    open_positions = get_open_positions()

    # No position cap check (paper money) — just check MAX_POSITIONS count
    if len(open_positions) >= MAX_POSITIONS:
        logger.info(f"Shadow max positions ({MAX_POSITIONS}) reached")
        return

    # No held_tickers filter — allow buying same ticker multiple times
    candidates = find_candidates(markets)

    if not candidates:
        logger.info("Shadow: no buy candidates")
        return

    best = candidates[0]

    logger.info(f"SHADOW BUY: {best['ticker']} {best['side']} @ ${best['price']:.2f} ({best['mins_left']:.1f}min left)")

    # Paper mode: just record in DB, no real order
    buy_fee = kalshi_fee(best['price'], CONTRACTS)
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO shadow_trades (ticker, side, price, count, current_bid, fees) VALUES (%s, %s, %s, %s, %s, %s)",
                (best['ticker'], best['side'], float(best['price']), CONTRACTS, float(best['price']), float(buy_fee))
            )
        logger.info(f"SHADOW BOUGHT: {best['ticker']} {best['side']} x{CONTRACTS} @ ${best['price']:.2f} fee=${buy_fee:.4f}")
    finally:
        conn.close()


# === MAIN CYCLE ===

def run_cycle():
    open_pos = get_open_positions()
    total_cost = sum(sf(t['price']) * (t.get('count') or 1) for t in open_pos)
    total_value = sum(sf(t.get('current_bid', 0)) * (t.get('count') or 1) for t in open_pos)
    logger.info(f"=== SHADOW CYCLE === {len(open_pos)} positions | cost=${total_cost:.2f} | value=${total_value:.2f}")
    check_sells()
    markets = fetch_all_markets()
    # Cache all market data for dashboard
    for m in markets:
        _shadow_cache['markets'][m.get('ticker', '')] = m
    logger.info(f"Shadow fetched {len(markets)} markets")
    buy_cheapest(markets)


# === DASHBOARD API ===

@shadow_bp.route('/api/shadow/status')
def api_shadow_status():
    try:
        wins = 0
        losses = 0
        cuts = 0
        avg_win = 0
        avg_loss = 0
        bot_pnl = 0
        bot_fees = 0
        open_count = 0
        total_cost = 0
        total_value = 0

        conn = get_db()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                # Open positions
                cur.execute("SELECT * FROM shadow_trades WHERE status='open'")
                open_trades = cur.fetchall()
                open_count = len(open_trades)
                total_cost = sum(sf(t['price']) * (t.get('count') or 1) for t in open_trades)
                total_value = sum(sf(t.get('current_bid', 0)) * (t.get('count') or 1) for t in open_trades)

                # Closed trades
                cur.execute("SELECT * FROM shadow_trades WHERE status='closed'")
                closed = cur.fetchall()
        finally:
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

        return jsonify({
            'total_cost': round(total_cost, 4),
            'total_value': round(total_value, 4),
            'unrealized': round(total_value - total_cost, 4),
            'open_count': open_count,
            'realized_pnl': bot_pnl,
            'total_fees': round(bot_fees, 4),
            'wins': wins,
            'losses': losses,
            'cuts': cuts,
            'avg_win': avg_win,
            'avg_loss': avg_loss,
        })
    except Exception as e:
        logger.error(f"Shadow API status error: {e}")
        return jsonify({'error': str(e)})


@shadow_bp.route('/api/shadow/positions')
def api_shadow_positions():
    """Open positions from shadow_trades DB with cached market prices."""
    try:
        now = datetime.now(timezone.utc)
        conn = get_db()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT * FROM shadow_trades WHERE status='open' ORDER BY bought_at")
                open_trades = cur.fetchall()
        finally:
            conn.close()

        results = []
        for trade in open_trades:
            ticker = trade['ticker']
            side = trade['side']
            entry = sf(trade['price'])
            count = trade.get('count') or 1
            current_bid = sf(trade.get('current_bid', 0))

            # Try to get fresher bid from cache
            market = _shadow_cache['markets'].get(ticker, {})
            if market:
                if side == 'yes':
                    cached_bid = sf(market.get('yes_bid_dollars', '0'))
                else:
                    cached_bid = sf(market.get('no_bid_dollars', '0'))
                if cached_bid > 0:
                    current_bid = cached_bid

            bought_at = trade['bought_at']
            if bought_at.tzinfo is None:
                bought_at = bought_at.replace(tzinfo=timezone.utc)
            mins_held = (now - bought_at).total_seconds() / 60

            cost = entry * count
            current_value = current_bid * count if current_bid > 0 else 0
            unrealized = round(current_value - cost, 4) if current_bid > 0 else 0
            gain_pct = ((current_value - cost) / cost * 100) if cost > 0 and current_bid > 0 else 0

            results.append({
                'ticker': ticker,
                'side': side,
                'count': count,
                'cost': round(cost, 4),
                'entry_per': round(entry, 4),
                'current_bid': round(current_bid, 2),
                'current_value': round(current_value, 4),
                'unrealized': unrealized,
                'gain_pct': round(gain_pct, 1),
                'mins_held': round(mins_held, 1),
                'cut_eligible': mins_held >= CUT_LOSS_AFTER_MINS,
            })

        results.sort(key=lambda x: x['gain_pct'], reverse=True)
        return jsonify(results)
    except Exception as e:
        logger.error(f"Shadow API positions error: {e}")
        return jsonify([])


@shadow_bp.route('/api/shadow/closed')
def api_shadow_closed():
    try:
        conn = get_db()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT * FROM shadow_trades WHERE status='closed' ORDER BY closed_at DESC LIMIT 50")
                trades = cur.fetchall()
        finally:
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
        logger.error(f"Shadow API closed error: {e}")
        return jsonify([])


@shadow_bp.route('/shadow')
def shadow_dashboard():
    return SHADOW_DASHBOARD_HTML


SHADOW_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SHADOW - Paper</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&display=swap');
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0a0a0a;color:#e0e0e0;font-family:'JetBrains Mono',monospace;padding:16px 20px;font-size:13px}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
.live-dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:#ffaa00;margin-right:6px;animation:pulse 2s infinite}
.header{text-align:center;margin-bottom:14px;color:#555;font-size:11px}

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
  <a href="/dashboard" style="color:#555;text-decoration:none;font-size:10px">&larr; RAZOR</a>
  &nbsp;&nbsp;
  <span class="live-dot"></span>
  SHADOW (PAPER) &mdash; buy $0.01-$0.99 all prices, cut -70% losers at 5min, ride rest to settlement
  &mdash; <span id="last-update">--</span>
</div>

<div class="round-timer">
  <div class="round-phases">
    <div class="phase phase-buy" id="phase-buy">
      <div class="phase-label">BUY WINDOW</div>
      <div class="phase-time" id="buy-time">--:--</div>
    </div>
    <div class="phase phase-hold" id="phase-hold">
      <div class="phase-label">HOLD / CUT CHECK</div>
      <div class="phase-time" id="hold-time">--:--</div>
    </div>
    <div class="phase phase-settle" id="phase-settle">
      <div class="phase-label">SETTLEMENT</div>
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
  <div class="label">Paper P&L (Realized)</div>
  <div class="big" id="realized-pnl-big">$0.0000</div>
  <div style="display:flex;justify-content:center;gap:32px;margin-top:8px;font-size:13px">
    <span>Cost: <span style="color:#fff;font-weight:700" id="total-cost">$0.00</span></span>
    <span>Value: <span style="color:#fff;font-weight:700" id="total-value">$0.00</span></span>
    <span>Unrealized: <span style="font-weight:700" id="unrealized">$0.00</span></span>
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
    <div class="label">Fees (Simulated)</div>
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

<div class="footer">SHADOW (PAPER) &mdash; simulated trades &mdash; auto-refresh 2s</div>

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
      fetch('/api/shadow/status').then(r=>r.json()),
      fetch('/api/shadow/positions').then(r=>r.json()),
      fetch('/api/shadow/closed').then(r=>r.json())
    ]);

    if(status&&!status.error){
      var rp=status.realized_pnl||0;
      $('realized-pnl-big').textContent=(rp>=0?'+$':'-$')+Math.abs(rp).toFixed(4);
      $('realized-pnl-big').className='big '+cls(rp);
      var pnlBox=$('pnl-box');
      pnlBox.className='pnl-box'+(rp>0?' positive':rp<0?' negative':'');

      $('total-cost').textContent='$'+(status.total_cost||0).toFixed(4);
      $('total-value').textContent='$'+(status.total_value||0).toFixed(4);
      var ur=status.unrealized||0;
      $('unrealized').textContent=(ur>=0?'+$':'-$')+Math.abs(ur).toFixed(4);
      $('unrealized').className=cls(ur);

      $('open-count').textContent=status.open_count||0;
      $('wins').textContent=status.wins||0;
      $('losses').textContent=status.losses||0;
      $('realized-pnl').textContent=(rp>=0?'+$':'-$')+Math.abs(rp).toFixed(4);
      $('realized-pnl').className='value '+cls(rp);
      $('fees').textContent='-$'+(status.total_fees||0).toFixed(4);
      $('avg-win').textContent='+$'+(status.avg_win||0).toFixed(4);
      $('avg-loss').textContent='-$'+Math.abs(status.avg_loss||0).toFixed(4);
      $('cuts').textContent=status.cuts||0;
    }

    if(positions){
      $('pos-label').textContent=positions.length+' positions';
      var h='';
      positions.forEach(function(p){
        var gc=cls(p.gain_pct);
        var uc=cls(p.unrealized);
        var cutPct=Math.min(p.mins_held/5*100,100);
        var cutColor=cutPct>=100?(p.gain_pct<=-70?'#ff4444':'#00d673'):'#ffaa00';
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
        if(cutPct>=100){
          h+=p.gain_pct<=-70?'<span class="red" style="font-size:9px">CUT</span>':'<span class="green" style="font-size:9px">OK</span>';
        }else{
          h+='<span class="gray" style="font-size:9px">'+p.mins_held.toFixed(1)+'/5m</span>';
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
        var tag=t.reason==='win'?'tag-win':t.reason==='cut_loss'?'tag-cut':'tag-loss';
        var label=t.reason==='win'?'WIN':t.reason==='cut_loss'?'CUT':'LOSS';
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

  /* Phases: Buy 0-5min, Hold/Cut 5-14min, Settle 14-15min */
  var buyEnd=5*60;      /* stop buying at 10min left = 5min in */
  var holdEnd=14*60;    /* last minute is settlement */

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
    $('round-status').innerHTML='<span class="green">BUYING</span> \\u2014 '+fmt(buyLeft)+' left';
  }else if(secsIntoWindow<holdEnd){
    $('round-status').innerHTML='<span class="orange">HOLDING / CUT CHECK</span> \\u2014 '+fmt(holdLeft)+' to settle';
  }else{
    $('round-status').innerHTML='<span class="red">SETTLING</span> \\u2014 '+fmt(settleLeft);
  }
}
updateRoundTimer();
setInterval(updateRoundTimer,1000);
</script>
</body>
</html>"""


# === MAIN ===

def shadow_loop():
    init_shadow_db()
    logger.info(f"SHADOW bot starting -- PAPER MODE -- buy ${BUY_MIN}-${BUY_MAX}, cut -70% at {CUT_LOSS_AFTER_MINS}min")
    logger.info(f"Series: {CRYPTO_SERIES}")

    while True:
        try:
            run_cycle()
        except Exception as e:
            logger.error(f"Shadow cycle error: {e}")
            traceback.print_exc()
        time.sleep(CYCLE_SECONDS)
