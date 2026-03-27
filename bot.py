"""
Cheap contract scraper. Buy the cheapest available crypto contract.
At 5 minutes after buy: cut anything 50%+ red. Rest rides to settlement.
Dashboard pulls directly from Kalshi API — matches Kalshi exactly.
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
MIN_MINS_TO_BUY = 6          # stop buying when < 6 min left
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


# === DATABASE (just for tracking buy times for cut-loss logic) ===

def get_db():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    return conn


def init_db():
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS scraper_buys (
                    id SERIAL PRIMARY KEY,
                    ticker TEXT NOT NULL,
                    side TEXT NOT NULL,
                    price NUMERIC NOT NULL,
                    count INTEGER DEFAULT 1,
                    bought_at TIMESTAMPTZ DEFAULT NOW(),
                    cut BOOLEAN DEFAULT FALSE
                )
            """)
    finally:
        conn.close()
    logger.info("Database initialized")


def record_buy(ticker, side, price, count):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO scraper_buys (ticker, side, price, count) VALUES (%s, %s, %s, %s)",
                (ticker, side, float(price), count)
            )
    finally:
        conn.close()


def get_buy_time(ticker):
    """Get earliest uncut buy time for a ticker."""
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT bought_at FROM scraper_buys WHERE ticker=%s AND cut=FALSE ORDER BY bought_at ASC LIMIT 1",
                (ticker,)
            )
            row = cur.fetchone()
            return row['bought_at'] if row else None
    finally:
        conn.close()


def mark_cut(ticker):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE scraper_buys SET cut=TRUE WHERE ticker=%s AND cut=FALSE", (ticker,))
    finally:
        conn.close()


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


def get_kalshi_balance():
    """Get balance + portfolio_value from Kalshi API."""
    try:
        resp = kalshi_get('/portfolio/balance')
        return {
            'balance': resp.get('balance', 0) / 100.0,
            'portfolio_value': resp.get('portfolio_value', 0) / 100.0,
        }
    except:
        return {'balance': 0.0, 'portfolio_value': 0.0}


def get_kalshi_positions():
    """Get all positions from Kalshi API."""
    all_positions = []
    cursor = None
    try:
        while True:
            url = '/portfolio/positions?limit=200&count_filter=position'
            if cursor:
                url += f'&cursor={cursor}'
            resp = kalshi_get(url)
            batch = resp.get('market_positions', [])
            all_positions.extend(batch)
            cursor = resp.get('cursor')
            if not cursor or not batch:
                break
    except Exception as e:
        logger.error(f"Get positions failed: {e}")
    return all_positions


def get_kalshi_fills(limit=50):
    """Get recent fills from Kalshi API."""
    try:
        resp = kalshi_get(f'/portfolio/fills?limit={limit}')
        return resp.get('fills', [])
    except Exception as e:
        logger.error(f"Get fills failed: {e}")
        return []


def get_kalshi_settlements(limit=50):
    """Get recent settlements from Kalshi API."""
    try:
        resp = kalshi_get(f'/portfolio/settlements?limit={limit}')
        return resp.get('settlements', [])
    except Exception as e:
        logger.error(f"Get settlements failed: {e}")
        return []


# === CORE LOGIC ===

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
    """At 5 min after buy: cut 50%+ losers. Uses Kalshi positions API."""
    positions = get_kalshi_positions()
    if not positions:
        return

    now = datetime.now(timezone.utc)

    for pos in positions:
        ticker = pos.get('ticker', '')
        position = sf(pos.get('position_fp', '0'))
        if position == 0:
            continue

        # Get buy time from our DB
        bought_at = get_buy_time(ticker)
        if not bought_at:
            continue
        if bought_at.tzinfo is None:
            bought_at = bought_at.replace(tzinfo=timezone.utc)

        mins_held = (now - bought_at).total_seconds() / 60
        if mins_held < CUT_LOSS_AFTER_MINS:
            continue

        # Check market for current price
        market = get_market(ticker)
        if not market:
            continue

        # Skip settled
        if market.get('result', '') or market.get('status', '') in ('closed', 'settled', 'finalized'):
            continue

        # Determine our side and prices
        exposure = sf(pos.get('market_exposure_dollars', '0'))
        total_cost = sf(pos.get('total_traded_dollars', '0'))

        if position > 0:
            side = 'yes'
            current_bid = sf(market.get('yes_bid_dollars', '0'))
            count = int(abs(position))
        else:
            side = 'no'
            current_bid = sf(market.get('no_bid_dollars', '0'))
            count = int(abs(position))

        if current_bid <= 0 or total_cost <= 0:
            continue

        # Calculate gain based on exposure vs cost
        # exposure is current value, negative means underwater
        gain = exposure / abs(total_cost) if total_cost != 0 else 0

        if gain <= CUT_LOSS_THRESHOLD:
            logger.info(f"CUT LOSS: {ticker} {side} x{count} exposure=${exposure:.2f} cost=${total_cost:.2f} ({gain*100:+.0f}%)")
            result = place_order(ticker, side, 'sell', current_bid, count)
            if result:
                mark_cut(ticker)


def buy_cheapest(markets):
    positions = get_kalshi_positions()
    held_tickers = {p.get('ticker', '') for p in positions if sf(p.get('position_fp', '0')) != 0}

    if len(held_tickers) >= MAX_POSITIONS:
        logger.info(f"Max positions ({MAX_POSITIONS}) reached")
        return

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
    if filled > 0:
        record_buy(best['ticker'], best['side'], best['price'], filled)
        logger.info(f"BOUGHT: {best['ticker']} {best['side']} x{filled} @ ${best['price']:.2f}")


# === MAIN CYCLE ===

def run_cycle():
    bal = get_kalshi_balance()
    positions = get_kalshi_positions()
    active = [p for p in positions if sf(p.get('position_fp', '0')) != 0]
    logger.info(f"=== CYCLE === Balance: ${bal['balance']:.2f} | Portfolio: ${bal['portfolio_value']:.2f} | {len(active)} positions")
    check_sells()
    markets = fetch_all_markets()
    logger.info(f"Fetched {len(markets)} markets")
    buy_cheapest(markets)


# === DASHBOARD API (all from Kalshi) ===

@app.route('/')
def health():
    return 'OK'


@app.route('/api/status')
def api_status():
    try:
        bal = get_kalshi_balance()
        positions = get_kalshi_positions()
        active = [p for p in positions if sf(p.get('position_fp', '0')) != 0]

        total_exposure = sum(sf(p.get('market_exposure_dollars', '0')) for p in active)
        total_realized = sum(sf(p.get('realized_pnl_dollars', '0')) for p in positions)
        total_fees = sum(sf(p.get('fees_paid_dollars', '0')) for p in positions)

        settlements = get_kalshi_settlements(limit=200)
        wins = sum(1 for s in settlements if s.get('revenue', 0) > 0)
        losses = sum(1 for s in settlements if s.get('revenue', 0) == 0 and sf(s.get('yes_count_fp', '0')) + sf(s.get('no_count_fp', '0')) > 0)

        # Count cuts from our DB
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM scraper_buys WHERE cut=TRUE")
            cuts = cur.fetchone()[0]
        conn.close()

        return jsonify({
            'balance': round(bal['balance'], 2),
            'portfolio_value': round(bal['portfolio_value'], 2),
            'total': round(bal['balance'] + bal['portfolio_value'], 2),
            'open_count': len(active),
            'exposure': round(total_exposure, 4),
            'realized_pnl': round(total_realized, 4),
            'total_fees': round(total_fees, 4),
            'wins': wins,
            'losses': losses,
            'cuts': cuts,
        })
    except Exception as e:
        logger.error(f"API status error: {e}")
        return jsonify({'error': str(e)})


@app.route('/api/positions')
def api_positions():
    """Live positions straight from Kalshi."""
    try:
        positions = get_kalshi_positions()
        now = datetime.now(timezone.utc)
        results = []

        for pos in positions:
            ticker = pos.get('ticker', '')
            position_fp = sf(pos.get('position_fp', '0'))
            if position_fp == 0:
                continue

            side = 'yes' if position_fp > 0 else 'no'
            count = int(abs(position_fp))
            exposure = sf(pos.get('market_exposure_dollars', '0'))
            cost = sf(pos.get('total_traded_dollars', '0'))
            realized = sf(pos.get('realized_pnl_dollars', '0'))
            fees = sf(pos.get('fees_paid_dollars', '0'))

            # Get current market price
            market = get_market(ticker)
            current_bid = 0
            if market:
                if side == 'yes':
                    current_bid = sf(market.get('yes_bid_dollars', '0'))
                else:
                    current_bid = sf(market.get('no_bid_dollars', '0'))

            # Get buy time from our DB
            bought_at = get_buy_time(ticker)
            mins_held = 0
            if bought_at:
                if bought_at.tzinfo is None:
                    bought_at = bought_at.replace(tzinfo=timezone.utc)
                mins_held = (now - bought_at).total_seconds() / 60

            gain_pct = (exposure / abs(cost) * 100) if cost != 0 else 0

            results.append({
                'ticker': ticker,
                'side': side,
                'count': count,
                'cost': round(abs(cost), 4),
                'exposure': round(exposure, 4),
                'current_bid': round(current_bid, 2),
                'realized_pnl': round(realized, 4),
                'fees': round(fees, 4),
                'gain_pct': round(gain_pct, 1),
                'mins_held': round(mins_held, 1),
                'cut_eligible': mins_held >= CUT_LOSS_AFTER_MINS,
            })

        results.sort(key=lambda x: x['gain_pct'], reverse=True)
        return jsonify(results)
    except Exception as e:
        logger.error(f"API positions error: {e}")
        return jsonify([])


@app.route('/api/fills')
def api_fills():
    """Recent fills straight from Kalshi."""
    try:
        fills = get_kalshi_fills(limit=50)
        results = []
        for f in fills:
            results.append({
                'ticker': f.get('ticker', ''),
                'side': f.get('side', ''),
                'action': f.get('action', ''),
                'count': sf(f.get('count_fp', '0')),
                'yes_price': sf(f.get('yes_price_dollars', '0')),
                'no_price': sf(f.get('no_price_dollars', '0')),
                'fee': sf(f.get('fee_cost', '0')),
                'is_taker': f.get('is_taker', False),
                'time': f.get('created_time', ''),
            })
        return jsonify(results)
    except Exception as e:
        logger.error(f"API fills error: {e}")
        return jsonify([])


@app.route('/api/settlements')
def api_settlements():
    """Recent settlements straight from Kalshi."""
    try:
        settlements = get_kalshi_settlements(limit=50)
        results = []
        for s in settlements:
            yes_count = sf(s.get('yes_count_fp', '0'))
            no_count = sf(s.get('no_count_fp', '0'))
            yes_cost = sf(s.get('yes_total_cost_dollars', '0'))
            no_cost = sf(s.get('no_total_cost_dollars', '0'))
            revenue = s.get('revenue', 0) / 100.0
            fee = sf(s.get('fee_cost', '0'))
            result_val = s.get('market_result', '')

            results.append({
                'ticker': s.get('ticker', ''),
                'result': result_val,
                'yes_count': yes_count,
                'no_count': no_count,
                'yes_cost': round(yes_cost, 4),
                'no_cost': round(no_cost, 4),
                'revenue': round(revenue, 4),
                'fee': round(fee, 4),
                'pnl': round(revenue - yes_cost - no_cost, 4),
                'time': s.get('settled_time', ''),
            })
        return jsonify(results)
    except Exception as e:
        logger.error(f"API settlements error: {e}")
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
.stats{background:#111;border:1px solid #1a1a1a;border-radius:6px;padding:16px 20px;margin-bottom:14px;text-align:center}
.stats .big{font-size:24px;font-weight:700;color:#fff}
.stats .sub{font-size:14px;color:#888;margin-top:4px}
.stats .row{display:flex;justify-content:center;gap:28px;margin-top:8px;font-size:12px;color:#888;flex-wrap:wrap}
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
.tag-buy{background:#00d67322;color:#00d673}
.tag-sell{background:#ff444422;color:#ff4444}
.tag-yes{background:#00d67322;color:#00d673}
.tag-no{background:#ff444422;color:#ff4444}
.tag-win{background:#00d67322;color:#00d673}
.tag-loss{background:#ff444422;color:#ff4444}
.tag-void{background:#ffaa0022;color:#ffaa00}
.cut-bar{height:3px;background:#222;border-radius:2px;margin-top:3px;overflow:hidden}
.cut-fill{height:100%;border-radius:2px;transition:width .3s}
.footer{text-align:center;color:#333;font-size:9px;margin-top:8px}
.panel-body::-webkit-scrollbar{width:4px}
.panel-body::-webkit-scrollbar-track{background:#111}
.panel-body::-webkit-scrollbar-thumb{background:#333;border-radius:2px}
</style>
</head>
<body>

<div class="header">
  <span class="live-dot"></span>
  SCRAPER BOT &mdash; buy $0.01-$0.99, cut 50%+ losers at 5min, ride rest to settlement
  &mdash; <span id="last-update">--</span>
</div>

<div class="stats">
  <div class="big">$<span id="total">--</span></div>
  <div class="sub">Balance: $<span id="balance">--</span> + Positions: $<span id="portfolio-value">--</span></div>
  <div class="row">
    <span>Open: <span id="open-count" style="color:#ffaa00;font-weight:700">--</span></span>
    <span>Exposure: <span id="exposure">--</span></span>
    <span>Realized P&L: <span id="realized-pnl">--</span></span>
    <span>Fees: <span id="fees" class="red">--</span></span>
  </div>
  <div class="row" style="margin-top:6px">
    <span class="green"><span id="wins">0</span>W</span>
    <span class="red"><span id="losses">0</span>L</span>
    <span class="orange"><span id="cuts">0</span> cuts</span>
  </div>
</div>

<div class="panel">
  <div class="panel-header"><h2>Live Positions (Kalshi)</h2><div class="count" id="pos-label"></div></div>
  <div class="panel-body"><table><thead><tr>
    <th>Ticker</th><th>Side</th><th>Qty</th><th>Cost</th><th>Bid</th><th>Exposure</th><th>Gain</th><th>Held</th><th>Cut Timer</th>
  </tr></thead><tbody id="pos-body"><tr><td colspan="9" class="gray" style="text-align:center;padding:20px">Loading...</td></tr></tbody></table></div>
</div>

<div class="panel">
  <div class="panel-header"><h2>Recent Fills (Kalshi)</h2><div class="count" id="fills-label"></div></div>
  <div class="panel-body"><table><thead><tr>
    <th>Time</th><th>Ticker</th><th>Action</th><th>Side</th><th>Qty</th><th>Price</th><th>Fee</th>
  </tr></thead><tbody id="fills-body"><tr><td colspan="7" class="gray" style="text-align:center;padding:20px">Loading...</td></tr></tbody></table></div>
</div>

<div class="panel">
  <div class="panel-header"><h2>Settlements (Kalshi)</h2><div class="count" id="sett-label"></div></div>
  <div class="panel-body"><table><thead><tr>
    <th>Time</th><th>Ticker</th><th>Result</th><th>Revenue</th><th>Cost</th><th>P&L</th>
  </tr></thead><tbody id="sett-body"><tr><td colspan="6" class="gray" style="text-align:center;padding:20px">Loading...</td></tr></tbody></table></div>
</div>

<div class="footer">Scraper Bot &mdash; all data from Kalshi API &mdash; auto-refresh 2s</div>

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
    var [status,positions,fills,settlements]=await Promise.all([
      fetch('/api/status').then(r=>r.json()),
      fetch('/api/positions').then(r=>r.json()),
      fetch('/api/fills').then(r=>r.json()),
      fetch('/api/settlements').then(r=>r.json())
    ]);

    if(status&&!status.error){
      $('total').textContent=(status.total||0).toFixed(2);
      $('balance').textContent=(status.balance||0).toFixed(2);
      $('portfolio-value').textContent=(status.portfolio_value||0).toFixed(2);
      $('open-count').textContent=status.open_count||0;
      var exp=status.exposure||0;
      $('exposure').innerHTML='<span class="'+cls(exp)+'">$'+exp.toFixed(4)+'</span>';
      var rpnl=status.realized_pnl||0;
      $('realized-pnl').innerHTML='<span class="'+cls(rpnl)+'">'+(rpnl>=0?'+$':'-$')+Math.abs(rpnl).toFixed(4)+'</span>';
      $('fees').textContent='$'+(status.total_fees||0).toFixed(4);
      $('wins').textContent=status.wins||0;
      $('losses').textContent=status.losses||0;
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
        h+='<td>$'+p.cost.toFixed(4)+'</td>';
        h+='<td>'+(p.current_bid>0?'$'+p.current_bid.toFixed(2):'--')+'</td>';
        h+='<td class="'+cls(p.exposure)+'">$'+p.exposure.toFixed(4)+'</td>';
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
      $('pos-body').innerHTML=h||'<tr><td colspan="9" class="gray" style="text-align:center;padding:20px">No open positions</td></tr>';
    }

    if(fills){
      $('fills-label').textContent=fills.length+' fills';
      var h='';
      fills.forEach(function(f){
        var ac=f.action==='buy'?'tag-buy':'tag-sell';
        var price=f.side==='yes'?f.yes_price:f.no_price;
        h+='<tr>';
        h+='<td>'+timeAgo(f.time)+'</td>';
        h+='<td style="font-size:10px">'+esc(f.ticker)+'</td>';
        h+='<td><span class="tag '+ac+'">'+f.action.toUpperCase()+'</span></td>';
        h+='<td><span class="tag tag-'+f.side+'">'+f.side.toUpperCase()+'</span></td>';
        h+='<td>'+Math.round(f.count)+'</td>';
        h+='<td>$'+price.toFixed(2)+'</td>';
        h+='<td class="red">$'+f.fee.toFixed(4)+'</td>';
        h+='</tr>';
      });
      $('fills-body').innerHTML=h||'<tr><td colspan="7" class="gray" style="text-align:center;padding:20px">No fills yet</td></tr>';
    }

    if(settlements){
      $('sett-label').textContent=settlements.length+' settlements';
      var h='';
      settlements.forEach(function(s){
        var pc=cls(s.pnl);
        var rc=s.result==='yes'?'tag-yes':s.result==='no'?'tag-no':'tag-void';
        var cost=s.yes_cost+s.no_cost;
        h+='<tr>';
        h+='<td>'+timeAgo(s.time)+'</td>';
        h+='<td style="font-size:10px">'+esc(s.ticker)+'</td>';
        h+='<td><span class="tag '+rc+'">'+s.result.toUpperCase()+'</span></td>';
        h+='<td class="green">$'+s.revenue.toFixed(4)+'</td>';
        h+='<td>$'+cost.toFixed(4)+'</td>';
        h+='<td class="'+pc+'">'+(s.pnl>=0?'+':'')+s.pnl.toFixed(4)+'</td>';
        h+='</tr>';
      });
      $('sett-body').innerHTML=h||'<tr><td colspan="6" class="gray" style="text-align:center;padding:20px">No settlements yet</td></tr>';
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
