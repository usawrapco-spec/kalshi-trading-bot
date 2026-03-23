"""
Clean slate scalper. Buy cheap crypto, sell at 30%, take the loss on expiry.
$0.03-$0.20, 5 contracts, 30s cycles, paper mode.
"""

import os, time, logging, traceback
from datetime import datetime, timezone
from flask import Flask, render_template_string, jsonify
from threading import Thread
from supabase import create_client
from kalshi_auth import KalshiAuth
import requests

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# === CONFIG ===
KALSHI_HOST = os.environ.get('KALSHI_API_HOST', 'https://api.elections.kalshi.com')
SUPABASE_URL = os.environ.get('SUPABASE_URL')
SUPABASE_KEY = os.environ.get('SUPABASE_KEY')
PORT = int(os.environ.get('PORT', 8080))
ENABLE_TRADING = os.environ.get('ENABLE_TRADING', 'false').lower() == 'true'

# === SETTINGS ===
BUY_MIN = 0.03
BUY_MAX = 0.20
CYCLE_SECONDS = 30
CONTRACTS_PER_TRADE = 5
SELL_THRESHOLD = 0.30  # 30% gain = sell
PAPER_STARTING_BALANCE = 50.00
MAX_BUYS_PER_CYCLE = 10

CRYPTO_SERIES = ['KXBTC', 'KXETH', 'KXSOL', 'KXBTCD', 'KXETHD', 'KXSOLD']

# === INIT ===
db = create_client(SUPABASE_URL, SUPABASE_KEY)
auth = KalshiAuth()
app = Flask(__name__)

# === STATE ===
current_hot_markets = []


def sf(val):
    try:
        return float(val) if val is not None else 0.0
    except:
        return 0.0


# === KALSHI API ===

def kalshi_get(path):
    try:
        url = f"{KALSHI_HOST}/trade-api/v2{path}"
        headers = auth.get_headers("GET", f"/trade-api/v2{path}")
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error(f"GET {path} failed: {e}")
        raise


def kalshi_post(path, data):
    try:
        url = f"{KALSHI_HOST}/trade-api/v2{path}"
        headers = auth.get_headers("POST", f"/trade-api/v2{path}")
        headers['Content-Type'] = 'application/json'
        resp = requests.post(url, headers=headers, json=data, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error(f"POST {path} failed: {e}")
        raise


def get_market(ticker):
    try:
        resp = kalshi_get(f"/markets/{ticker}")
        return resp.get('market', resp)
    except:
        return None


def place_order(ticker, side, action, price, count):
    if not ENABLE_TRADING:
        logger.info(f"PAPER {action.upper()}: {ticker} {side} x{count} @ ${price:.2f}")
        return {'order_id': 'paper', 'status': 'executed'}

    price_cents = int(round(price * 100))
    try:
        resp = kalshi_post('/portfolio/orders', {
            'ticker': ticker,
            'action': action,
            'side': side,
            'type': 'limit',
            'count': count,
            'yes_price' if side == 'yes' else 'no_price': price_cents,
        })
        order = resp.get('order', {})
        return {'order_id': order.get('order_id', ''), 'status': order.get('status', '')}
    except Exception as e:
        logger.error(f"ORDER FAILED: {action.upper()} {ticker} -- {e}")
        return None


# === BALANCE ===

def get_paper_balance():
    try:
        # Sum all buy costs
        buys = db.table('trades').select('price,count').eq('action', 'buy').execute()
        buy_cost = sum(sf(t['price']) * (t.get('count') or 1) for t in (buys.data or []))

        # Sum all sell revenue
        sells = db.table('trades').select('price,count').eq('action', 'sell').execute()
        sell_revenue = sum(sf(t['price']) * (t.get('count') or 1) for t in (sells.data or []))

        return max(0, PAPER_STARTING_BALANCE - buy_cost + sell_revenue)
    except Exception as e:
        logger.error(f"Balance calc failed: {e}")
        return 0.0


def get_open_positions():
    try:
        result = db.table('trades').select('*').eq('action', 'buy').is_('pnl', 'null').execute()
        return result.data or []
    except Exception as e:
        logger.error(f"get_open_positions failed: {e}")
        return []


def get_owned_tickers():
    return {t['ticker'] for t in get_open_positions()}


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

        # Check if expired/settled
        if status in ('closed', 'settled', 'finalized') or result_val:
            if result_val == side:
                pnl = round((1.0 - entry_price) * count, 4)
                sell_price = 1.0
                reason = f"WIN settled @$1.00"
            elif result_val:
                pnl = round(-entry_price * count, 4)
                sell_price = 0.0
                reason = f"LOSS expired"
            else:
                continue

            logger.info(f"EXPIRED/SETTLED: {ticker} {side} | {reason} | pnl=${pnl:.4f}")
            try:
                db.table('trades').insert({
                    'ticker': ticker, 'side': side, 'action': 'sell',
                    'price': float(sell_price), 'count': count,
                    'pnl': float(pnl),
                }).execute()
                db.table('trades').update({'pnl': 0.0}).eq('id', trade['id']).execute()
            except Exception as e:
                logger.error(f"Settle DB error: {e}")
            expired += 1
            continue

        # Check if contract close_time is past
        close_time = market.get('close_time') or market.get('expected_expiration_time')
        if close_time:
            try:
                close_dt = datetime.fromisoformat(close_time.replace('Z', '+00:00'))
                if close_dt < datetime.now(timezone.utc):
                    pnl = round(-entry_price * count, 4)
                    logger.info(f"EXPIRED (time): {ticker} pnl=${pnl:.4f}")
                    try:
                        db.table('trades').update({'pnl': pnl}).eq('id', trade['id']).execute()
                    except:
                        pass
                    expired += 1
                    continue
            except:
                pass

        # Get current bid
        if side == 'yes':
            current_bid = sf(market.get('yes_bid_dollars', '0'))
        else:
            current_bid = sf(market.get('no_bid_dollars', '0'))

        if current_bid <= 0:
            continue

        gain = (current_bid - entry_price) / entry_price
        gain_pct = gain * 100
        logger.info(f"  POS: {ticker} {side} entry=${entry_price:.2f} bid=${current_bid:.2f} {gain_pct:+.0f}%")

        # Update current_bid in DB for dashboard
        try:
            db.table('trades').update({'current_bid': float(current_bid)}).eq('id', trade['id']).execute()
        except:
            pass

        # 30% gain = SELL
        if gain >= SELL_THRESHOLD:
            pnl = round((current_bid - entry_price) * count, 4)
            result = place_order(ticker, side, 'sell', current_bid, count)
            if not result:
                continue

            logger.info(f"SELL: {ticker} {side} x{count} @ ${current_bid:.2f} | +{gain_pct:.0f}% | pnl=${pnl:.4f}")
            try:
                db.table('trades').insert({
                    'ticker': ticker, 'side': side, 'action': 'sell',
                    'price': float(current_bid), 'count': count,
                    'pnl': float(pnl),
                    'sell_gain_pct': float(round(gain_pct, 1)),
                }).execute()
                db.table('trades').update({'pnl': 0.0, 'current_bid': float(current_bid)}).eq('id', trade['id']).execute()
            except Exception as e:
                logger.error(f"Sell DB error: {e}")
            sold += 1

    logger.info(f"SELL SUMMARY: sold={sold} expired={expired}")


# === BUY LOGIC ===

def fetch_all_markets():
    all_markets = []
    for series in CRYPTO_SERIES:
        try:
            resp = kalshi_get(f'/markets?series_ticker={series}&status=open&limit=200')
            batch = resp.get('markets', [])
            all_markets.extend(batch)
        except Exception as e:
            logger.error(f"Fetch {series} failed: {e}")
    logger.info(f"Fetched {len(all_markets)} markets from {len(CRYPTO_SERIES)} series")
    return all_markets


def buy_candidates(markets):
    balance = get_paper_balance()
    owned = get_owned_tickers()
    logger.info(f"Balance: ${balance:.2f} | {len(owned)} positions open")

    if balance <= 5.0:
        logger.info("Balance too low, skipping buys")
        return

    candidates = []
    now = datetime.now(timezone.utc)

    for market in markets:
        ticker = market.get('ticker', '')

        # Skip if already owned
        if ticker in owned:
            continue

        # Must expire in more than 30 minutes
        close_time = market.get('close_time') or market.get('expected_expiration_time')
        if close_time:
            try:
                close_dt = datetime.fromisoformat(close_time.replace('Z', '+00:00'))
                if (close_dt - now).total_seconds() < 1800:
                    continue
            except:
                pass

        yes_ask = sf(market.get('yes_ask_dollars', '0'))
        yes_bid = sf(market.get('yes_bid_dollars', '0'))
        no_ask = sf(market.get('no_ask_dollars', '0'))
        no_bid = sf(market.get('no_bid_dollars', '0'))

        # YES side: price in range + bid exists
        if BUY_MIN <= yes_ask <= BUY_MAX and yes_bid > 0:
            candidates.append({'ticker': ticker, 'side': 'yes', 'price': yes_ask, 'bid': yes_bid})

        # NO side: price in range + bid exists
        if BUY_MIN <= no_ask <= BUY_MAX and no_bid > 0:
            candidates.append({'ticker': ticker, 'side': 'no', 'price': no_ask, 'bid': no_bid})

    # Sort by price ascending (cheapest first)
    candidates.sort(key=lambda x: x['price'])
    logger.info(f"Found {len(candidates)} buy candidates")

    for c in candidates[:10]:
        logger.info(f"  CANDIDATE: {c['ticker']} {c['side']} ask=${c['price']:.2f} bid=${c['bid']:.2f}")

    bought = 0
    for c in candidates:
        if bought >= MAX_BUYS_PER_CYCLE:
            break

        cost = c['price'] * CONTRACTS_PER_TRADE
        if cost > balance:
            logger.info(f"OUT OF CASH: need ${cost:.2f}, have ${balance:.2f}")
            break

        result = place_order(c['ticker'], c['side'], 'buy', c['price'], CONTRACTS_PER_TRADE)
        if not result:
            continue

        logger.info(f"BUY: {c['ticker']} {c['side']} x{CONTRACTS_PER_TRADE} @ ${c['price']:.2f}")
        try:
            db.table('trades').insert({
                'ticker': c['ticker'], 'side': c['side'], 'action': 'buy',
                'price': float(c['price']), 'count': CONTRACTS_PER_TRADE,
                'current_bid': float(c['bid']),
            }).execute()
            owned.add(c['ticker'])
            balance -= cost
            bought += 1
        except Exception as e:
            logger.error(f"Buy DB insert failed: {e}")

    logger.info(f"Bought {bought} positions")


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
    owned = get_owned_tickers()
    by_vol = sorted(markets, key=lambda m: _get_volume(m), reverse=True)[:20]
    current_hot_markets = [
        {
            'ticker': m.get('ticker', ''),
            'title': m.get('title', m.get('subtitle', '')),
            'yes_ask': sf(m.get('yes_ask_dollars', '0')),
            'no_ask': sf(m.get('no_ask_dollars', '0')),
            'yes_bid': sf(m.get('yes_bid_dollars', '0')),
            'no_bid': sf(m.get('no_bid_dollars', '0')),
            'volume': _get_volume(m),
            'owned': m.get('ticker', '') in owned,
        }
        for m in by_vol
    ]


# === MAIN CYCLE ===

def run_cycle():
    balance = get_paper_balance()
    mode = "PAPER" if not ENABLE_TRADING else "LIVE"
    logger.info(f"=== CYCLE START [{mode}] === Balance: ${balance:.2f}")

    check_sells()

    markets = fetch_all_markets()
    update_hot_markets(markets)

    if balance > 5.0:
        buy_candidates(markets)

    balance = get_paper_balance()
    logger.info(f"=== CYCLE END [{mode}] === Balance: ${balance:.2f}")


# === DASHBOARD API ===

@app.route('/')
def health():
    return 'OK'


@app.route('/api/status')
def api_status():
    try:
        balance = get_paper_balance()

        sells = db.table('trades').select('pnl').eq('action', 'sell').not_.is_('pnl', 'null').execute()
        sell_data = sells.data or []
        net_pnl = sum(sf(t['pnl']) for t in sell_data)
        wins = sum(1 for t in sell_data if sf(t['pnl']) > 0)
        losses = sum(1 for t in sell_data if sf(t['pnl']) < 0)

        open_positions = get_open_positions()
        open_count = len(open_positions)

        mode = "PAPER" if not ENABLE_TRADING else "LIVE"

        return jsonify({
            'balance': round(balance, 2),
            'net_pnl': round(net_pnl, 4),
            'wins': wins,
            'losses': losses,
            'open_count': open_count,
            'mode': mode,
        })
    except Exception as e:
        logger.error(f"API status error: {e}")
        return jsonify({'balance': 0, 'net_pnl': 0, 'wins': 0, 'losses': 0, 'open_count': 0, 'mode': 'PAPER', 'error': str(e)})


@app.route('/api/trades')
def api_trades():
    try:
        result = db.table('trades').select('*').order('created_at', desc=True).limit(50).execute()
        return jsonify(result.data or [])
    except Exception as e:
        logger.error(f"API trades error: {e}")
        return jsonify([])


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
                'count': count,
                'entry': price,
                'current_bid': current,
                'unrealized': unrealized,
                'gain_pct': gain_pct,
                'created_at': t.get('created_at', ''),
            })
        positions.sort(key=lambda x: x['gain_pct'], reverse=True)
        return jsonify(positions)
    except Exception as e:
        logger.error(f"API open error: {e}")
        return jsonify([])


@app.route('/api/hot')
def api_hot():
    return jsonify(current_hot_markets)


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
.dot-paper{background:#ffaa00}
.dot-live{background:#00d673}

.portfolio{text-align:center;margin-bottom:20px;padding:20px 0 16px;border-bottom:1px solid #1a1a1a}
.portfolio .sub{color:#555;font-size:11px;margin-bottom:12px}
.portfolio-value{font-size:48px;font-weight:700;color:#fff;margin-bottom:4px}
.portfolio-pnl{font-size:18px;font-weight:700;margin-bottom:14px}
.portfolio-breakdown{display:flex;justify-content:center;gap:32px;flex-wrap:wrap}
.portfolio-breakdown .item{text-align:center}
.portfolio-breakdown .item .label{color:#666;font-size:9px;text-transform:uppercase;letter-spacing:1px;margin-bottom:4px}
.portfolio-breakdown .item .val{font-size:18px;font-weight:700}

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

.equity-section{background:#111;border:1px solid #1a1a1a;border-radius:6px;padding:14px;margin-bottom:14px}
.equity-section h2{color:#ffaa00;font-size:12px;text-transform:uppercase;letter-spacing:1px;margin-bottom:10px}
#equity-chart{width:100%;height:120px}

.status-bar{background:#111;border:1px solid #1a1a1a;border-radius:6px;padding:10px 16px;display:flex;justify-content:space-between;flex-wrap:wrap;gap:8px;font-size:10px;color:#555}
.status-bar .status-item{display:flex;align-items:center;gap:4px}
.footer{text-align:center;color:#333;font-size:9px;margin-top:8px}
.loading{color:#555;text-align:center;padding:20px}
.panel-body::-webkit-scrollbar{width:4px}
.panel-body::-webkit-scrollbar-track{background:#111}
.panel-body::-webkit-scrollbar-thumb{background:#333;border-radius:2px}

@media(max-width:900px){
.portfolio-value{font-size:36px}
.portfolio-breakdown{gap:16px}
}
</style>
</head>
<body>

<div class="portfolio">
  <div class="sub"><span class="live-dot dot-paper" id="mode-dot"></span><span id="mode-label">PAPER MODE</span> &mdash; simple scalper &mdash; crypto 3-20c &mdash; sell at 30%</div>
  <div class="portfolio-value" id="p-total">...</div>
  <div class="portfolio-pnl" id="p-pnl">...</div>
  <div class="portfolio-breakdown">
    <div class="item"><div class="label">Open</div><div class="val" id="p-positions">...</div></div>
    <div class="item"><div class="label">Balance</div><div class="val" id="p-cash">...</div></div>
    <div class="item"><div class="label">Record</div><div class="val" id="p-record">...</div></div>
  </div>
</div>

<div class="panel">
  <div class="panel-header"><h2>Open Positions</h2><div class="count" id="open-count"></div></div>
  <div class="panel-body"><table><thead><tr>
    <th>Ticker</th><th>Side</th><th>Qty</th><th>Entry</th><th>Bid</th><th>P&amp;L</th><th>Gain</th>
  </tr></thead><tbody id="open-body"><tr><td colspan="7" class="loading">Loading...</td></tr></tbody></table></div>
</div>

<div class="panel">
  <div class="panel-header"><h2>Hot Markets</h2><div class="count" id="hot-count"></div></div>
  <div class="panel-body"><table><thead><tr>
    <th>Ticker</th><th>Title</th><th>Volume</th><th>Yes</th><th>No</th><th></th>
  </tr></thead><tbody id="hot-body"><tr><td colspan="6" class="loading">Loading...</td></tr></tbody></table></div>
</div>

<div class="panel">
  <div class="panel-header"><h2>Recent Trades</h2><div class="count" id="trades-count"></div></div>
  <div class="panel-body"><table><thead><tr>
    <th>Time</th><th>Ticker</th><th>Action</th><th>Side</th><th>Qty</th><th>Price</th><th>P&amp;L</th>
  </tr></thead><tbody id="trades-body"><tr><td colspan="7" class="loading">Loading...</td></tr></tbody></table></div>
</div>

<div class="equity-section">
  <h2>Equity Curve</h2>
  <canvas id="equity-chart"></canvas>
</div>

<div class="status-bar">
  <div class="status-item"><span class="live-dot dot-paper" id="status-dot"></span> <span id="status-mode">PAPER</span></div>
  <div class="status-item">Buy: 3-20c, cheapest first</div>
  <div class="status-item">Sell: 30% gain</div>
  <div class="status-item">5 contracts, 30s cycles</div>
  <div class="status-item">Series: KXBTC/ETH/SOL + daily</div>
  <div class="status-item">Last: <span id="last-update">&mdash;</span></div>
</div>
<div class="footer">Simple Scalper &mdash; auto-refresh 15s</div>

<script>
function $(id){return document.getElementById(id)}
function cls(v){return v>0?'green':v<0?'red':'gray'}
function esc(s){var d=document.createElement('div');d.textContent=s;return d.innerHTML}

async function fetchJSON(url){
  try{var r=await fetch(url);return await r.json()}
  catch(e){console.error(url,e);return null}
}

function fmtVol(v){if(v>=1e6)return(v/1e6).toFixed(1)+'M';if(v>=1e3)return(v/1e3).toFixed(1)+'K';return v.toString()}

async function refresh(){
  var [status,open,trades,hot]=await Promise.all([
    fetchJSON('/api/status'),
    fetchJSON('/api/open'),
    fetchJSON('/api/trades'),
    fetchJSON('/api/hot')
  ]);

  if(status&&!status.error){
    $('p-total').textContent='$'+(status.balance||0).toFixed(2);

    var pnl=status.net_pnl||0;
    var arrow=pnl>=0?'\\u25B2':'\\u25BC';
    $('p-pnl').innerHTML='<span class="'+cls(pnl)+'">'+arrow+' '+(pnl>=0?'+':'')+pnl.toFixed(2)+' realized</span>';

    $('p-positions').textContent=status.open_count||0;
    $('p-cash').textContent='$'+(status.balance||0).toFixed(2);
    $('p-record').innerHTML='<span class="green">'+status.wins+'W</span> <span class="gray">/</span> <span class="red">'+status.losses+'L</span>';

    var mode=status.mode||'PAPER';
    var isLive=mode==='LIVE';
    $('mode-label').textContent=isLive?'LIVE TRADING':'PAPER MODE';
    $('mode-dot').className='live-dot '+(isLive?'dot-live':'dot-paper');
    $('status-mode').textContent=mode;
    $('status-dot').className='live-dot '+(isLive?'dot-live':'dot-paper');
  }

  if(open&&!open.error){
    $('open-count').textContent=open.length+' positions';
    var h='';
    open.forEach(function(p){
      var rc=p.gain_pct>2?'row-green':p.gain_pct<-2?'row-red':'';
      var gc=cls(p.gain_pct);
      h+='<tr class="'+rc+'">';
      h+='<td style="font-size:10px">'+esc(p.ticker)+'</td>';
      h+='<td>'+esc(p.side)+'</td>';
      h+='<td>'+p.count+'</td>';
      h+='<td>$'+p.entry.toFixed(2)+'</td>';
      h+='<td>$'+(p.current_bid||0).toFixed(2)+'</td>';
      h+='<td class="'+gc+'">'+(p.unrealized>=0?'+':'')+p.unrealized.toFixed(4)+'</td>';
      h+='<td class="'+gc+'">'+(p.gain_pct>=0?'+':'')+p.gain_pct.toFixed(0)+'%</td>';
      h+='</tr>';
    });
    $('open-body').innerHTML=h||'<tr><td colspan="7" class="gray" style="text-align:center">No open positions</td></tr>';
  }

  if(trades&&!trades.error){
    $('trades-count').textContent=trades.length+' trades';
    var h='';
    trades.slice(0,50).forEach(function(t){
      var p=t.pnl;
      var hasPnl=p!==null&&p!==undefined;
      var pc=hasPnl?cls(p):'gray';
      var rc=hasPnl?(p>0?'row-green':p<0?'row-red':''):'';
      var time=(t.created_at||'').replace('T',' ').substring(5,19);
      h+='<tr class="'+rc+'">';
      h+='<td>'+esc(time)+'</td>';
      h+='<td style="font-size:10px">'+esc(t.ticker||'')+'</td>';
      h+='<td>'+esc(t.action||'')+'</td>';
      h+='<td>'+esc(t.side||'')+'</td>';
      h+='<td>'+(t.count||1)+'</td>';
      h+='<td>$'+(t.price||0).toFixed(2)+'</td>';
      h+='<td class="'+pc+'">'+(hasPnl?((p>=0?'+':'')+p.toFixed(4)):'--')+'</td>';
      h+='</tr>';
    });
    $('trades-body').innerHTML=h||'<tr><td colspan="7" class="gray" style="text-align:center">No trades yet</td></tr>';

    var completed=trades.filter(function(t){return t.action==='sell'&&t.pnl!==null&&t.pnl!==0});
    drawEquity(completed);
  }

  if(hot&&!hot.error){
    $('hot-count').textContent='Top '+hot.length+' by volume';
    var h='';
    hot.forEach(function(m){
      var owned=m.owned?'row-green':'';
      var title=(m.title||'').substring(0,40);
      h+='<tr class="'+owned+'">';
      h+='<td style="font-size:10px">'+esc(m.ticker)+'</td>';
      h+='<td style="font-size:10px;color:#888">'+esc(title)+'</td>';
      h+='<td style="color:#ffaa00;font-weight:700">'+fmtVol(m.volume)+'</td>';
      h+='<td>$'+(m.yes_ask||0).toFixed(2)+'<span class="gray">/</span>$'+(m.yes_bid||0).toFixed(2)+'</td>';
      h+='<td>$'+(m.no_ask||0).toFixed(2)+'<span class="gray">/</span>$'+(m.no_bid||0).toFixed(2)+'</td>';
      h+='<td>'+(m.owned?'<span class="green">OWNED</span>':'')+'</td>';
      h+='</tr>';
    });
    $('hot-body').innerHTML=h||'<tr><td colspan="6" class="gray" style="text-align:center">No data yet</td></tr>';
  }

  $('last-update').textContent=new Date().toLocaleTimeString();
}

function drawEquity(trades){
  var canvas=$('equity-chart');
  if(!canvas)return;
  var ctx=canvas.getContext('2d');
  var W=canvas.parentElement.clientWidth-28;
  var H=120;
  canvas.width=W;canvas.height=H;
  ctx.clearRect(0,0,W,H);

  var sorted=trades.slice().reverse();
  var cumulative=[0];
  var running=0;
  sorted.forEach(function(t){running+=(t.pnl||0);cumulative.push(running)});

  if(cumulative.length<2)return;

  var min=Math.min.apply(null,cumulative);
  var max=Math.max.apply(null,cumulative);
  var range=max-min||1;
  var pad=10;

  var zeroY=H-pad-((0-min)/range)*(H-2*pad);
  ctx.strokeStyle='#222';ctx.lineWidth=1;
  ctx.beginPath();ctx.moveTo(0,zeroY);ctx.lineTo(W,zeroY);ctx.stroke();

  ctx.strokeStyle=running>=0?'#00d673':'#ff4444';
  ctx.lineWidth=1.5;
  ctx.beginPath();
  for(var i=0;i<cumulative.length;i++){
    var x=(i/(cumulative.length-1))*W;
    var y=H-pad-((cumulative[i]-min)/range)*(H-2*pad);
    if(i===0)ctx.moveTo(x,y);else ctx.lineTo(x,y);
  }
  ctx.stroke();

  ctx.lineTo(W,H);ctx.lineTo(0,H);ctx.closePath();
  ctx.fillStyle=running>=0?'rgba(0,214,115,.06)':'rgba(255,68,68,.06)';
  ctx.fill();

  ctx.fillStyle='#555';ctx.font='9px JetBrains Mono,monospace';
  ctx.fillText('$'+max.toFixed(2),4,pad+6);
  ctx.fillText('$'+min.toFixed(2),4,H-4);
  ctx.fillText('$'+running.toFixed(2)+' net',W-80,pad+6);
}

refresh();
setInterval(refresh,15000);
</script>
</body>
</html>"""


# === MAIN ===

def bot_loop():
    mode = "PAPER" if not ENABLE_TRADING else "LIVE"
    logger.info(f"Bot starting [{mode}] -- simple scalper: ${BUY_MIN}-${BUY_MAX}, sell at {SELL_THRESHOLD*100:.0f}%, {CYCLE_SECONDS}s cycles")
    logger.info(f"Series: {CRYPTO_SERIES}")
    logger.info(f"Sizing: {CONTRACTS_PER_TRADE} contracts per trade")

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
