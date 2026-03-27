"""
Cheap contract scraper. Buy the cheapest available crypto contract.
At 5 minutes after buy: cut anything 50%+ red. Rest rides to settlement.
"""

import os, time, logging, traceback, math
from datetime import datetime, timezone, timedelta
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
BUY_MIN = 0.01              # minimum price
BUY_MAX = 0.99              # buy anything up to $0.99
TAKER_FEE_RATE = 0.07
MAX_MINS_TO_EXPIRY = 15     # full 15-min window
CYCLE_SECONDS = 2            # 2-second cycles
CONTRACTS = 1
MAX_POSITIONS = 15           # max open at once
CUT_LOSS_AFTER_MINS = 5     # check at 5 min mark
CUT_LOSS_THRESHOLD = -0.50  # sell if 50%+ red

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
                    status TEXT DEFAULT 'open',
                    bought_at TIMESTAMPTZ DEFAULT NOW(),
                    closed_at TIMESTAMPTZ,
                    close_reason TEXT
                )
            """)
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


def get_kalshi_balance():
    try:
        resp = kalshi_get('/portfolio/balance')
        return resp.get('balance', 0) / 100.0
    except:
        return 0.0


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
    """Find the single cheapest contract across all markets within expiry window."""
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
            if mins_left < 10 or mins_left > MAX_MINS_TO_EXPIRY:
                continue
        except:
            continue

        yes_ask = sf(market.get('yes_ask_dollars', '999'))
        no_ask = sf(market.get('no_ask_dollars', '999'))

        # Check yes side
        if BUY_MIN <= yes_ask <= BUY_MAX:
            candidates.append({
                'ticker': ticker, 'side': 'yes', 'price': yes_ask,
                'mins_left': mins_left
            })
        # Check no side
        if BUY_MIN <= no_ask <= BUY_MAX:
            candidates.append({
                'ticker': ticker, 'side': 'no', 'price': no_ask,
                'mins_left': mins_left
            })

    candidates.sort(key=lambda x: x['price'])
    return candidates


def check_sells():
    """At 5 min after buy: cut 50%+ losers. Also handle settlements."""
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
                        "UPDATE scraper_trades SET pnl=%s, status='closed', closed_at=NOW(), close_reason=%s WHERE id=%s",
                        (float(pnl), reason, trade_id)
                    )
                continue

            if status in ('closed', 'settled', 'finalized'):
                continue

            # Get current bid
            if side == 'yes':
                current_bid = sf(market.get('yes_bid_dollars', '0'))
            else:
                current_bid = sf(market.get('no_bid_dollars', '0'))

            # Update bid in DB
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
                    pnl = round((current_bid - entry) * count - buy_fee - sell_fee, 4)
                    logger.info(f"CUT LOSS: {ticker} {side} ${entry:.2f} -> ${current_bid:.2f} ({gain*100:+.0f}%) pnl=${pnl:.4f}")
                    result = place_order(ticker, side, 'sell', current_bid, count)
                    if result:
                        with conn.cursor() as cur:
                            cur.execute(
                                "UPDATE scraper_trades SET pnl=%s, status='closed', closed_at=NOW(), close_reason='cut_loss', current_bid=%s WHERE id=%s",
                                (float(pnl), float(current_bid), trade_id)
                            )
    finally:
        conn.close()


def buy_cheapest(markets):
    """Buy the single cheapest available contract."""
    open_positions = get_open_positions()
    if len(open_positions) >= MAX_POSITIONS:
        logger.info(f"Max positions ({MAX_POSITIONS}) reached, skipping buys")
        return

    # Don't buy tickers we already hold
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

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO scraper_trades (ticker, side, price, count, current_bid) VALUES (%s, %s, %s, %s, %s)",
                (best['ticker'], best['side'], float(best['price']), filled, float(best['price']))
            )
        logger.info(f"BOUGHT: {best['ticker']} {best['side']} x{filled} @ ${best['price']:.2f}")
    finally:
        conn.close()


# === MAIN CYCLE ===

def run_cycle():
    balance = get_kalshi_balance()
    open_pos = get_open_positions()
    logger.info(f"=== CYCLE === Balance: ${balance:.2f} | {len(open_pos)} open positions")
    check_sells()
    markets = fetch_all_markets()
    logger.info(f"Fetched {len(markets)} markets")
    buy_cheapest(markets)


# === DASHBOARD ===

@app.route('/')
def health():
    return 'OK'


@app.route('/api/status')
def api_status():
    try:
        balance = get_kalshi_balance()
        conn = get_db()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM scraper_trades WHERE status='open'")
            open_trades = cur.fetchall()
            cur.execute("SELECT * FROM scraper_trades WHERE status='closed' ORDER BY closed_at DESC LIMIT 100")
            closed_trades = cur.fetchall()
        conn.close()

        positions_value = sum(sf(t.get('current_bid', 0)) * (t.get('count') or 1) for t in open_trades)
        total_pnl = sum(sf(t['pnl']) for t in closed_trades if t.get('pnl') is not None)
        wins = sum(1 for t in closed_trades if sf(t.get('pnl', 0)) > 0)
        losses = sum(1 for t in closed_trades if sf(t.get('pnl', 0)) <= 0 and t.get('pnl') is not None)
        cuts = sum(1 for t in closed_trades if t.get('close_reason') == 'cut_loss')

        return jsonify({
            'balance': round(balance, 2),
            'positions_value': round(positions_value, 2),
            'portfolio': round(balance + positions_value, 2),
            'open_count': len(open_trades),
            'total_pnl': round(total_pnl, 4),
            'wins': wins,
            'losses': losses,
            'cuts': cuts,
            'total_trades': len(closed_trades),
        })
    except Exception as e:
        logger.error(f"API status error: {e}")
        return jsonify({'error': str(e)})


@app.route('/api/open')
def api_open():
    try:
        conn = get_db()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM scraper_trades WHERE status='open' ORDER BY bought_at DESC")
            trades = cur.fetchall()
        conn.close()

        now = datetime.now(timezone.utc)
        positions = []
        for t in trades:
            entry = sf(t['price'])
            bid = sf(t.get('current_bid', 0))
            count = t.get('count') or 1
            bought_at = t['bought_at']
            if bought_at.tzinfo is None:
                bought_at = bought_at.replace(tzinfo=timezone.utc)
            mins_held = (now - bought_at).total_seconds() / 60
            gain_pct = ((bid - entry) / entry * 100) if entry > 0 and bid > 0 else 0
            unrealized = round((bid - entry) * count, 4) if bid > 0 else 0

            positions.append({
                'ticker': t['ticker'],
                'side': t['side'],
                'entry': entry,
                'bid': bid,
                'count': count,
                'gain_pct': round(gain_pct, 1),
                'unrealized': unrealized,
                'mins_held': round(mins_held, 1),
                'cut_eligible': mins_held >= CUT_LOSS_AFTER_MINS,
            })
        return jsonify(positions)
    except Exception as e:
        logger.error(f"API open error: {e}")
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
            results.append({
                'ticker': t['ticker'],
                'side': t['side'],
                'entry': entry,
                'pnl': sf(t.get('pnl', 0)),
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
.stats{background:#111;border:1px solid #1a1a1a;border-radius:6px;padding:16px 20px;margin-bottom:14px;text-align:center}
.stats .big{font-size:22px;font-weight:700;color:#fff}
.stats .row{display:flex;justify-content:center;gap:28px;margin-top:8px;font-size:12px;color:#888}
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
.cut-bar{height:3px;background:#222;border-radius:2px;margin-top:3px;overflow:hidden}
.cut-fill{height:100%;background:#ffaa00;border-radius:2px;transition:width .3s}
.footer{text-align:center;color:#333;font-size:9px;margin-top:8px}
</style>
</head>
<body>

<div class="header">
  <span class="live-dot"></span>
  SCRAPER BOT &mdash; buy $0.01-$0.99, cut 50%+ losers at 5min, ride rest to settlement
  &mdash; <span id="last-update">--</span>
</div>

<div class="stats">
  <div class="big">$<span id="portfolio">--</span></div>
  <div class="row">
    <span>Cash: $<span id="balance">--</span></span>
    <span>Positions: $<span id="pos-value">--</span></span>
    <span>Open: <span id="open-count">--</span></span>
  </div>
  <div class="row" style="margin-top:6px">
    <span>P&L: <span id="pnl">--</span></span>
    <span class="green"><span id="wins">0</span>W</span>
    <span class="red"><span id="losses">0</span>L</span>
    <span class="orange"><span id="cuts">0</span> cuts</span>
  </div>
</div>

<div class="panel">
  <div class="panel-header"><h2>Open Positions</h2><div class="count" id="open-label"></div></div>
  <div class="panel-body"><table><thead><tr>
    <th>Ticker</th><th>Side</th><th>Entry</th><th>Bid</th><th>P&L</th><th>Gain</th><th>Held</th><th>Cut Timer</th>
  </tr></thead><tbody id="open-body"><tr><td colspan="8" class="gray" style="text-align:center;padding:20px">Loading...</td></tr></tbody></table></div>
</div>

<div class="panel">
  <div class="panel-header"><h2>Closed Trades</h2><div class="count" id="closed-label"></div></div>
  <div class="panel-body"><table><thead><tr>
    <th>Ticker</th><th>Side</th><th>Entry</th><th>P&L</th><th>Result</th><th>When</th>
  </tr></thead><tbody id="closed-body"><tr><td colspan="6" class="gray" style="text-align:center;padding:20px">Loading...</td></tr></tbody></table></div>
</div>

<div class="footer">Scraper Bot &mdash; auto-refresh 2s &mdash; cycle 2s &mdash; buy $0.01-$0.99</div>

<script>
function $(id){return document.getElementById(id)}
function cls(v){return v>0?'green':v<0?'red':'gray'}
function esc(s){var d=document.createElement('div');d.textContent=s;return d.innerHTML}
function timeAgo(s){
  if(!s||s==='None')return '--';
  var diff=Math.floor((Date.now()-new Date(s).getTime())/1000);
  if(diff<60)return diff+'s ago';if(diff<3600)return Math.floor(diff/60)+'m ago';
  return Math.floor(diff/3600)+'h ago';
}

async function refresh(){
  try{
    var [status,open,closed]=await Promise.all([
      fetch('/api/status').then(r=>r.json()),
      fetch('/api/open').then(r=>r.json()),
      fetch('/api/closed').then(r=>r.json())
    ]);

    if(status&&!status.error){
      $('portfolio').textContent=(status.portfolio||0).toFixed(2);
      $('balance').textContent=(status.balance||0).toFixed(2);
      $('pos-value').textContent=(status.positions_value||0).toFixed(2);
      $('open-count').textContent=status.open_count||0;
      var p=status.total_pnl||0;
      $('pnl').innerHTML='<span class="'+cls(p)+'">'+(p>=0?'+':'')+p.toFixed(4)+'</span>';
      $('wins').textContent=status.wins||0;
      $('losses').textContent=status.losses||0;
      $('cuts').textContent=status.cuts||0;
    }

    if(open){
      $('open-label').textContent=open.length+' positions';
      var h='';
      open.forEach(function(p){
        var gc=cls(p.gain_pct);
        var cutPct=Math.min(p.mins_held/5*100,100);
        var cutColor=cutPct>=100?(p.gain_pct<=-50?'#ff4444':'#00d673'):'#ffaa00';
        h+='<tr>';
        h+='<td style="font-size:10px">'+esc(p.ticker)+'</td>';
        h+='<td>'+esc(p.side)+'</td>';
        h+='<td>$'+p.entry.toFixed(2)+'</td>';
        h+='<td>'+(p.bid>0?'$'+p.bid.toFixed(2):'--')+'</td>';
        h+='<td class="'+gc+'">'+(p.unrealized>=0?'+':'')+p.unrealized.toFixed(4)+'</td>';
        h+='<td class="'+gc+'">'+(p.gain_pct>=0?'+':'')+p.gain_pct.toFixed(0)+'%</td>';
        h+='<td>'+p.mins_held.toFixed(1)+'m</td>';
        h+='<td><div class="cut-bar"><div class="cut-fill" style="width:'+cutPct+'%;background:'+cutColor+'"></div></div>';
        h+=cutPct>=100?(p.gain_pct<=-50?'<span class="red" style="font-size:9px">CUTTING</span>':'<span class="green" style="font-size:9px">SAFE</span>'):'<span class="gray" style="font-size:9px">'+p.mins_held.toFixed(1)+'/5m</span>';
        h+='</td></tr>';
      });
      $('open-body').innerHTML=h||'<tr><td colspan="8" class="gray" style="text-align:center;padding:20px">No open positions</td></tr>';
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
        h+='<td>'+esc(t.side)+'</td>';
        h+='<td>$'+t.entry.toFixed(2)+'</td>';
        h+='<td class="'+pc+'">'+(t.pnl>=0?'+':'')+t.pnl.toFixed(4)+'</td>';
        h+='<td><span class="tag '+tag+'">'+label+'</span></td>';
        h+='<td>'+timeAgo(t.closed_at)+'</td>';
        h+='</tr>';
      });
      $('closed-body').innerHTML=h||'<tr><td colspan="6" class="gray" style="text-align:center;padding:20px">No trades yet</td></tr>';
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
    logger.info(f"Scraper bot starting -- buy cheapest <= ${BUY_MAX}, cut 50%+ red at 5min")
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
