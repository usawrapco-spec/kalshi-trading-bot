"""
Cheap contract scraper. Buy the cheapest available crypto contract.
At 5 minutes after buy: cut anything 50%+ red. Rest rides to settlement.
Tracks everything in local DB. Dashboard shows real positions + market prices.
"""

import os, time, logging, traceback, math
from datetime import datetime, timezone
from flask import Flask, jsonify
from threading import Thread
import psycopg2
from psycopg2.extras import RealDictCursor
from kalshi_auth import KalshiAuth
import requests

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# === CONFIG ===
KALSHI_HOST = os.environ.get('KALSHI_API_HOST', 'https://api.elections.kalshi.com')
DATABASE_URL = os.environ.get('DATABASE_URL', 'postgresql://kalshi:kalshi@localhost:5432/kalshi')
PORT = int(os.environ.get('PORT', 8080))
ENABLE_TRADING = True

# === STRATEGY ===
BUY_MIN = 0.01
BUY_MAX = 0.99
TAKER_FEE_RATE = 0.07
MAX_MINS_TO_EXPIRY = 15
MIN_MINS_TO_BUY = 6
CYCLE_SECONDS = 2
CONTRACTS = 1
MAX_POSITIONS = 15
CUT_LOSS_AFTER_MINS = 5
CUT_LOSS_THRESHOLD = -0.50

CRYPTO_SERIES = ['KXBTC15M', 'KXETH15M', 'KXSOL15M', 'KXXRP15M', 'KXDOGE15M']

# === INIT ===
auth = KalshiAuth()
app = Flask(__name__)


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


def init_db():
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
    logger.info("Database initialized")


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
        logger.info(f"PAPER {action}: {ticker} {side} x{count} @ ${price:.2f}")
        return ('paper', count)

    price_cents = int(round(price * 100))
    try:
        resp = kalshi_post('/portfolio/orders', {
            'ticker': ticker, 'action': action, 'side': side,
            'type': 'limit', 'count': count,
            'yes_price' if side == 'yes' else 'no_price': price_cents,
        })
        order = resp.get('order', {})
        filled = order.get('place_count', 0) - order.get('remaining_count', 0)
        if filled <= 0:
            filled = count if order.get('status') in ('executed', 'filled') else 0
        return (order.get('order_id', ''), filled) if filled > 0 else None
    except Exception as e:
        logger.error(f"ORDER FAILED: {action} {ticker} -- {e}")
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
            logger.error(f"Fetch {series} failed: {e}")
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

        if BUY_MIN <= yes_ask <= BUY_MAX:
            candidates.append({'ticker': ticker, 'side': 'yes', 'price': yes_ask, 'mins_left': mins_left})
        if BUY_MIN <= no_ask <= BUY_MAX:
            candidates.append({'ticker': ticker, 'side': 'no', 'price': no_ask, 'mins_left': mins_left})

    candidates.sort(key=lambda x: x['price'])
    return candidates


def check_sells():
    """Update bids from Kalshi, handle settlements, cut 50%+ losers at 5min."""
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
                logger.info(f"SETTLED: {ticker} {side} ${entry:.2f} -> {reason} pnl=${pnl:.4f}")
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
                    logger.info(f"CUT LOSS: {ticker} {side} ${entry:.2f} -> ${current_bid:.2f} ({gain*100:+.0f}%) pnl=${pnl:.4f}")
                    result = place_order(ticker, side, 'sell', current_bid, count)
                    if result:
                        with conn.cursor() as cur:
                            cur.execute(
                                "UPDATE scraper_trades SET pnl=%s, fees=%s, status='closed', closed_at=NOW(), close_reason='cut_loss', current_bid=%s WHERE id=%s",
                                (float(pnl), float(total_fees), float(current_bid), trade_id)
                            )
    finally:
        conn.close()


def buy_cheapest(markets):
    open_positions = get_open_positions()
    if len(open_positions) >= MAX_POSITIONS:
        logger.info(f"Max positions ({MAX_POSITIONS}) reached")
        return

    held_tickers = {t['ticker'] for t in open_positions}
    candidates = find_cheapest(markets)
    candidates = [c for c in candidates if c['ticker'] not in held_tickers]

    if not candidates:
        logger.info("No buy candidates")
        return

    best = candidates[0]
    logger.info(f"CHEAPEST: {best['ticker']} {best['side']} @ ${best['price']:.2f} ({best['mins_left']:.1f}min left)")

    result = place_order(best['ticker'], best['side'], 'buy', best['price'], CONTRACTS)
    if not result:
        return

    order_id, filled = result
    if filled <= 0:
        return

    buy_fee = kalshi_fee(best['price'], filled)
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO scraper_trades (ticker, side, price, count, current_bid, fees) VALUES (%s, %s, %s, %s, %s, %s)",
                (best['ticker'], best['side'], float(best['price']), filled, float(best['price']), float(buy_fee))
            )
        logger.info(f"BOUGHT: {best['ticker']} {best['side']} x{filled} @ ${best['price']:.2f} fee=${buy_fee:.4f}")
    finally:
        conn.close()


# === MAIN CYCLE ===

def run_cycle():
    open_pos = get_open_positions()
    total_cost = sum(sf(t['price']) * (t.get('count') or 1) for t in open_pos)
    total_value = sum(sf(t.get('current_bid', 0)) * (t.get('count') or 1) for t in open_pos)
    logger.info(f"=== CYCLE === {len(open_pos)} positions | cost=${total_cost:.2f} | value=${total_value:.2f}")
    check_sells()
    markets = fetch_all_markets()
    logger.info(f"Fetched {len(markets)} markets")
    buy_cheapest(markets)


# === DASHBOARD API ===

@app.route('/')
def health():
    return 'OK'


@app.route('/api/status')
def api_status():
    try:
        conn = get_db()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Open positions
            cur.execute("SELECT * FROM scraper_trades WHERE status='open'")
            open_trades = cur.fetchall()
            # All closed
            cur.execute("SELECT * FROM scraper_trades WHERE status='closed'")
            closed_trades = cur.fetchall()
        conn.close()

        open_cost = sum(sf(t['price']) * (t.get('count') or 1) for t in open_trades)
        open_value = sum(sf(t.get('current_bid', 0)) * (t.get('count') or 1) for t in open_trades)
        unrealized = round(open_value - open_cost, 4)

        total_pnl = sum(sf(t['pnl']) for t in closed_trades if t.get('pnl') is not None)
        total_fees = sum(sf(t.get('fees', 0)) for t in closed_trades)
        wins = sum(1 for t in closed_trades if t.get('close_reason') == 'win')
        losses = sum(1 for t in closed_trades if t.get('close_reason') == 'loss')
        cuts = sum(1 for t in closed_trades if t.get('close_reason') == 'cut_loss')
        win_pnl = sum(sf(t['pnl']) for t in closed_trades if sf(t.get('pnl', 0)) > 0)
        loss_pnl = sum(sf(t['pnl']) for t in closed_trades if sf(t.get('pnl', 0)) < 0)
        avg_win = round(win_pnl / wins, 4) if wins > 0 else 0
        avg_loss = round(loss_pnl / losses, 4) if losses > 0 else 0

        return jsonify({
            'open_count': len(open_trades),
            'open_cost': round(open_cost, 4),
            'open_value': round(open_value, 4),
            'unrealized': unrealized,
            'realized_pnl': round(total_pnl, 4),
            'total_fees': round(total_fees, 4),
            'overall_pnl': round(total_pnl + unrealized, 4),
            'wins': wins,
            'losses': losses,
            'cuts': cuts,
            'total_trades': len(closed_trades),
            'avg_win': avg_win,
            'avg_loss': avg_loss,
        })
    except Exception as e:
        logger.error(f"API status error: {e}")
        return jsonify({'error': str(e)})


@app.route('/api/positions')
def api_positions():
    """Open positions with live bids from Kalshi."""
    try:
        positions = get_open_positions()
        now = datetime.now(timezone.utc)
        results = []

        for t in positions:
            entry = sf(t['price'])
            bid = sf(t.get('current_bid', 0))
            count = t.get('count') or 1
            bought_at = t['bought_at']
            if bought_at.tzinfo is None:
                bought_at = bought_at.replace(tzinfo=timezone.utc)
            mins_held = (now - bought_at).total_seconds() / 60
            gain_pct = ((bid - entry) / entry * 100) if entry > 0 and bid > 0 else 0
            unrealized = round((bid - entry) * count, 4) if bid > 0 else 0

            results.append({
                'ticker': t['ticker'],
                'side': t['side'],
                'count': count,
                'entry': entry,
                'cost': round(entry * count, 4),
                'bid': bid,
                'value': round(bid * count, 4),
                'unrealized': unrealized,
                'gain_pct': round(gain_pct, 1),
                'mins_held': round(mins_held, 1),
                'cut_eligible': mins_held >= CUT_LOSS_AFTER_MINS,
            })

        results.sort(key=lambda x: x['gain_pct'], reverse=True)
        return jsonify(results)
    except Exception as e:
        logger.error(f"API positions error: {e}")
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
            gain_pct = ((bid - entry) / entry * 100) if entry > 0 and bid > 0 else 0
            results.append({
                'ticker': t['ticker'],
                'side': t['side'],
                'count': t.get('count', 1),
                'entry': entry,
                'exit': bid,
                'pnl': sf(t.get('pnl', 0)),
                'fees': sf(t.get('fees', 0)),
                'gain_pct': round(gain_pct, 1),
                'reason': t.get('close_reason', ''),
                'closed_at': str(t.get('closed_at', '')),
            })
        return jsonify(results)
    except Exception as e:
        logger.error(f"API closed error: {e}")
        return jsonify([])


@app.route('/dashboard')
def dashboard():
    return DASHBOARD_HTML


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Scraper Bot</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&display=swap');
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0a0a0a;color:#e0e0e0;font-family:'JetBrains Mono',monospace;padding:16px 20px;font-size:13px}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
.live-dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:#00d673;margin-right:6px;animation:pulse 2s infinite}
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
  <span class="live-dot"></span>
  SCRAPER BOT &mdash; buy $0.01-$0.99 cheapest, cut 50%+ losers at 5min, ride rest to settlement
  &mdash; <span id="last-update">--</span>
</div>

<div class="pnl-box" id="pnl-box">
  <div class="label">Overall Profit & Loss</div>
  <div class="big" id="overall-pnl">$0.00</div>
</div>

<div class="stat-grid">
  <div class="stat-card">
    <div class="label">Open Positions</div>
    <div class="value orange" id="open-count">0</div>
  </div>
  <div class="stat-card">
    <div class="label">Positions Value</div>
    <div class="value" id="open-value">$0.00</div>
  </div>
  <div class="stat-card">
    <div class="label">Record</div>
    <div class="value"><span class="green" id="wins">0</span>W / <span class="red" id="losses">0</span>L</div>
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
    <th>Ticker</th><th>Side</th><th>Qty</th><th>Entry</th><th>Cost</th><th>Bid</th><th>Value</th><th>P&L</th><th>Gain</th><th>Held</th><th>Cut Timer</th>
  </tr></thead><tbody id="pos-body"><tr><td colspan="11" class="gray" style="text-align:center;padding:20px">Loading...</td></tr></tbody></table></div>
</div>

<div class="panel">
  <div class="panel-header"><h2>Closed Trades</h2><div class="count" id="closed-label"></div></div>
  <div class="panel-body"><table><thead><tr>
    <th>Ticker</th><th>Side</th><th>Qty</th><th>Entry</th><th>Exit</th><th>P&L</th><th>Fees</th><th>Result</th><th>When</th>
  </tr></thead><tbody id="closed-body"><tr><td colspan="9" class="gray" style="text-align:center;padding:20px">Loading...</td></tr></tbody></table></div>
</div>

<div class="footer">Scraper Bot &mdash; live market prices from Kalshi API &mdash; auto-refresh 2s</div>

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
      var op=status.overall_pnl||0;
      $('overall-pnl').textContent=(op>=0?'+$':'-$')+Math.abs(op).toFixed(2);
      $('overall-pnl').className='big '+cls(op);
      $('pnl-box').className='pnl-box '+(op>=0?'positive':'negative');

      $('open-count').textContent=status.open_count||0;
      var ov=status.open_value||0;
      $('open-value').textContent='$'+ov.toFixed(2);
      $('open-value').className='value '+cls(status.unrealized||0);
      $('wins').textContent=status.wins||0;
      $('losses').textContent=status.losses||0;
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
        var cutPct=Math.min(p.mins_held/5*100,100);
        var cutColor=cutPct>=100?(p.gain_pct<=-50?'#ff4444':'#00d673'):'#ffaa00';
        h+='<tr>';
        h+='<td style="font-size:10px">'+esc(p.ticker)+'</td>';
        h+='<td><span class="tag tag-'+p.side+'">'+p.side.toUpperCase()+'</span></td>';
        h+='<td>'+p.count+'</td>';
        h+='<td>$'+p.entry.toFixed(2)+'</td>';
        h+='<td>$'+p.cost.toFixed(2)+'</td>';
        h+='<td>'+(p.bid>0?'$'+p.bid.toFixed(2):'--')+'</td>';
        h+='<td class="'+gc+'">$'+p.value.toFixed(2)+'</td>';
        h+='<td class="'+gc+'">'+(p.unrealized>=0?'+':'')+p.unrealized.toFixed(4)+'</td>';
        h+='<td class="'+gc+'">'+(p.gain_pct>=0?'+':'')+p.gain_pct.toFixed(0)+'%</td>';
        h+='<td>'+p.mins_held.toFixed(1)+'m</td>';
        h+='<td style="min-width:70px"><div class="cut-bar"><div class="cut-fill" style="width:'+cutPct+'%;background:'+cutColor+'"></div></div>';
        if(cutPct>=100){
          h+=p.gain_pct<=-50?'<span class="red" style="font-size:9px">CUTTING</span>':'<span class="green" style="font-size:9px">SAFE</span>';
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
        var tag=t.reason==='win'?'tag-win':t.reason==='cut_loss'?'tag-cut':'tag-loss';
        var label=t.reason==='win'?'WIN':t.reason==='cut_loss'?'CUT':'LOSS';
        h+='<tr>';
        h+='<td style="font-size:10px">'+esc(t.ticker)+'</td>';
        h+='<td><span class="tag tag-'+t.side+'">'+t.side.toUpperCase()+'</span></td>';
        h+='<td>'+(t.count||1)+'</td>';
        h+='<td>$'+t.entry.toFixed(2)+'</td>';
        h+='<td>$'+(t.exit||0).toFixed(2)+'</td>';
        h+='<td class="'+pc+'">'+(t.pnl>=0?'+':'')+t.pnl.toFixed(4)+'</td>';
        h+='<td class="red">$'+(t.fees||0).toFixed(4)+'</td>';
        h+='<td><span class="tag '+tag+'">'+label+'</span></td>';
        h+='<td>'+timeAgo(t.closed_at)+'</td>';
        h+='</tr>';
      });
      $('closed-body').innerHTML=h||'<tr><td colspan="9" class="gray" style="text-align:center;padding:20px">No trades yet</td></tr>';
    }

    $('last-update').textContent=new Date().toLocaleTimeString();
  }catch(e){console.error(e)}
}

refresh();
setInterval(refresh,2000);
</script>
</body>
</html>"""


# === MAIN ===

def bot_loop():
    init_db()
    logger.info(f"Scraper bot starting -- buy ${BUY_MIN}-${BUY_MAX}, cut 50%+ red at {CUT_LOSS_AFTER_MINS}min")
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
