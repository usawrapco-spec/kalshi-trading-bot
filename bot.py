"""
Crypto scalper. Buy cheap 15M contracts settling within 20min.
Sell at +45% (beats fees), stop loss at -25%. Keep it simple.
"""

import os, time, logging, traceback
from datetime import datetime, timezone
from flask import Flask, jsonify, render_template_string
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

# === STRATEGY ===
BUY_MIN = 0.03
BUY_MAX = 0.50
SELL_THRESHOLD = 0.50       # +50% take profit (beats fees)
TAKER_FEE_RATE = 0.07      # Kalshi taker fee: ceil(0.07 * contracts * P * (1-P)), max $0.02/contract
STOP_LOSS = -0.25           # -25% stop loss
MAX_MINS_TO_EXPIRY = 20     # only buy contracts settling within 20 min
CYCLE_SECONDS = 10
STARTING_BALANCE = 100.00
CASH_RESERVE = 0.50         # keep 50% cash
MAX_BUYS_PER_CYCLE = 10
CONTRACTS_DEFAULT = 5
CONTRACTS_HOT = 20
HOT_STREAK_WINS = 3         # 3 of last 5 wins = hot streak

CRYPTO_SERIES = ['KXBTC15M', 'KXETH15M', 'KXSOL15M', 'KXXRP15M', 'KXDOGE15M']

# === INIT ===
db = create_client(SUPABASE_URL, SUPABASE_KEY)
auth = KalshiAuth()
app = Flask(__name__)

current_hot_markets = []


def sf(val):
    try:
        return float(val) if val is not None else 0.0
    except:
        return 0.0


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
    """Place order and return (order_id, filled_count) or None on failure."""
    if not ENABLE_TRADING:
        logger.info(f"PAPER {action.upper()}: {ticker} {side} x{count} @ ${price:.2f}")
        return ('paper', count)

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
        order_id = order.get('order_id', '')
        status = order.get('status', '')

        # Only count actually filled contracts
        filled = order.get('place_count', 0) - order.get('remaining_count', 0)
        if filled <= 0:
            filled = count if status in ('executed', 'filled') else 0

        logger.info(f"ORDER {action.upper()}: {ticker} status={status} filled={filled}/{count} id={order_id}")
        return (order_id, filled) if filled > 0 else None
    except Exception as e:
        logger.error(f"ORDER FAILED: {action.upper()} {ticker} -- {e}")
        if hasattr(e, 'response') and e.response is not None:
            logger.error(f"ERROR BODY: {e.response.text}")
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
    try:
        buys = db.table('trades').select('price,count').eq('action', 'buy').execute()
        buy_cost = sum(sf(t['price']) * (t.get('count') or 1) for t in (buys.data or []))
        pnls = db.table('trades').select('pnl').not_.is_('pnl', 'null').execute()
        total_pnl = sum(sf(t['pnl']) for t in (pnls.data or []))
        return max(0, STARTING_BALANCE - buy_cost + total_pnl)
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
    try:
        result = db.table('trades').select('ticker').eq('action', 'buy').is_('pnl', 'null').execute()
        return {t['ticker'] for t in (result.data or [])}
    except Exception as e:
        logger.error(f"get_owned_tickers failed: {e}")
        return set()


def get_contracts(price):
    recent = db.table('trades').select('pnl').eq('action', 'buy').not_.is_('pnl', 'null').order('created_at', desc=True).limit(5).execute()
    recent_wins = sum(1 for t in recent.data if t['pnl'] > 0)
    if recent_wins >= HOT_STREAK_WINS and price <= BUY_MAX:
        return CONTRACTS_HOT
    return CONTRACTS_DEFAULT


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

        # === SETTLED: Kalshi has a final result ===
        if result_val:
            if result_val == side:
                pnl = round((1.0 - entry_price) * count, 4)
                reason = "WIN settled @$1.00"
            else:
                pnl = round(-entry_price * count, 4)
                reason = "LOSS settled"
            logger.info(f"SETTLED: {ticker} {side} | {reason} | pnl=${pnl:.4f}")
            try:
                db.table('trades').update({'pnl': float(pnl)}).eq('id', trade['id']).execute()
            except Exception as e:
                logger.error(f"Settle DB error: {e}")
            expired += 1
            continue

        # === CLOSED but no result yet: wait for Kalshi to settle ===
        if status in ('closed', 'settled', 'finalized'):
            logger.info(f"WAITING: {ticker} status={status} but no result yet, skipping")
            continue

        # Get current bid
        if side == 'yes':
            current_bid = sf(market.get('yes_bid_dollars', '0'))
        else:
            current_bid = sf(market.get('no_bid_dollars', '0'))

        # Update current_bid in DB for dashboard
        try:
            db.table('trades').update({'current_bid': float(current_bid)}).eq('id', trade['id']).execute()
        except:
            pass

        # If bid is $0, don't book a loss -- wait for settlement
        if current_bid <= 0:
            close_time = market.get('close_time') or market.get('expected_expiration_time')
            if close_time:
                try:
                    close_dt = datetime.fromisoformat(close_time.replace('Z', '+00:00'))
                    if close_dt < datetime.now(timezone.utc):
                        logger.info(f"LIKELY EXPIRED: {ticker} bid=$0, past close_time, waiting for settlement")
                        continue
                except:
                    pass
            logger.info(f"SKIP: {ticker} bid=$0, waiting")
            continue

        gain = (current_bid - entry_price) / entry_price
        gain_pct = gain * 100
        logger.info(f"  POS: {ticker} {side} entry=${entry_price:.2f} bid=${current_bid:.2f} {gain_pct:+.0f}%")

        should_sell = False
        reason = ''

        # Take profit at +45%
        if gain >= SELL_THRESHOLD:
            should_sell = True
            reason = f"+{gain_pct:.0f}% PROFIT"

        # Stop loss at -25%
        if gain <= STOP_LOSS:
            should_sell = True
            reason = f"{gain_pct:+.0f}% STOP LOSS"

        if not should_sell:
            continue

        # === SELL ORDER ===
        pnl = round((current_bid - entry_price) * count, 4)

        logger.info(f"SELL ATTEMPT: {ticker} {side} x{count} entry=${entry_price:.2f} bid=${current_bid:.2f} {gain_pct:+.0f}% reason={reason}")

        result = place_order(ticker, side, 'sell', current_bid, count)
        if not result:
            logger.error(f"SELL FAILED: {ticker} -- order not filled")
            continue

        order_id, filled = result

        if filled < count:
            logger.warning(f"PARTIAL SELL: {ticker} filled {filled}/{count}")
            pnl = round((current_bid - entry_price) * filled, 4)

        logger.info(f"SOLD ({reason}): {ticker} {side} x{filled} @ ${current_bid:.2f} | pnl=${pnl:.4f}")
        try:
            if filled >= count:
                db.table('trades').update({
                    'pnl': float(pnl),
                    'current_bid': float(current_bid),
                }).eq('id', trade['id']).execute()
            else:
                # Partial: reduce count, record sold portion separately
                db.table('trades').update({
                    'count': count - filled,
                    'current_bid': float(current_bid),
                }).eq('id', trade['id']).execute()
                db.table('trades').insert({
                    'ticker': ticker, 'side': side, 'action': 'buy',
                    'price': float(entry_price), 'count': filled,
                    'current_bid': float(current_bid), 'pnl': float(pnl),
                }).execute()
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
    balance = get_balance()
    owned = get_owned_tickers()
    logger.info(f"Balance: ${balance:.2f} | {len(owned)} positions open")

    deployable = balance * (1.0 - CASH_RESERVE)
    if deployable <= 1.0:
        logger.info(f"Balance ${balance:.2f}, deployable ${deployable:.2f} too low -- skipping buys")
        return

    candidates = []
    now = datetime.now(timezone.utc)

    for market in markets:
        ticker = market.get('ticker', '')

        if ticker in owned:
            continue

        # Only buy contracts settling within 20 minutes
        close_time = market.get('close_time') or market.get('expected_expiration_time')
        if close_time:
            try:
                close_dt = datetime.fromisoformat(close_time.replace('Z', '+00:00'))
                mins_left = (close_dt - now).total_seconds() / 60
                if mins_left > MAX_MINS_TO_EXPIRY or mins_left < 1:
                    continue
            except:
                continue
        else:
            continue

        yes_ask = float(market.get('yes_ask_dollars') or '999')
        yes_bid = float(market.get('yes_bid_dollars') or '0')
        no_ask = float(market.get('no_ask_dollars') or '999')
        no_bid = float(market.get('no_bid_dollars') or '0')

        # Pick whichever side is CHEAPER
        if yes_ask <= no_ask and BUY_MIN <= yes_ask <= BUY_MAX and yes_bid > 0:
            side, price, bid = 'yes', yes_ask, yes_bid
        elif BUY_MIN <= no_ask <= BUY_MAX and no_bid > 0:
            side, price, bid = 'no', no_ask, no_bid
        elif BUY_MIN <= yes_ask <= BUY_MAX and yes_bid > 0:
            side, price, bid = 'yes', yes_ask, yes_bid
        else:
            continue

        candidates.append({'ticker': ticker, 'side': side, 'price': price, 'bid': bid})

    candidates.sort(key=lambda x: x['price'])
    candidates = candidates[:MAX_BUYS_PER_CYCLE]
    logger.info(f"Found {len(candidates)} buy candidates")

    bought = 0
    for c in candidates:
        if bought >= MAX_BUYS_PER_CYCLE:
            break

        contracts = get_contracts(c['price'])
        cost = c['price'] * contracts
        if cost > deployable:
            logger.info(f"OUT OF CASH: need ${cost:.2f}, deployable ${deployable:.2f}")
            break

        result = place_order(c['ticker'], c['side'], 'buy', c['price'], contracts)
        if not result:
            continue

        order_id, filled = result
        if filled <= 0:
            continue

        actual_cost = c['price'] * filled
        logger.info(f"BUY: {c['ticker']} {c['side']} x{filled} @ ${c['price']:.2f} (ordered {contracts})")
        try:
            db.table('trades').insert({
                'ticker': c['ticker'], 'side': c['side'], 'action': 'buy',
                'price': float(c['price']), 'count': filled,
                'current_bid': float(c['bid']),
            }).execute()
            owned.add(c['ticker'])
            deployable -= actual_cost
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
    # Filter out settled markets ($1.00 yes = already decided)
    active = [m for m in markets if sf(m.get('yes_ask_dollars', '0')) < 0.99]
    by_vol = sorted(active, key=lambda m: _get_volume(m), reverse=True)[:10]
    current_hot_markets = [
        {
            'ticker': m.get('ticker', ''),
            'yes_ask': sf(m.get('yes_ask_dollars', '0')),
            'no_ask': sf(m.get('no_ask_dollars', '0')),
            'volume': _get_volume(m),
        }
        for m in by_vol
    ]


# === MAIN CYCLE ===

def run_cycle():
    mode = "PAPER" if not ENABLE_TRADING else "LIVE"
    balance = get_balance()
    logger.info(f"=== CYCLE START [{mode}] === Balance: ${balance:.2f}")

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

        resolved = db.table('trades').select('pnl,count').eq('action', 'buy').not_.is_('pnl', 'null').execute()
        resolved_data = resolved.data or []
        total_pnl = sum(sf(t['pnl']) for t in resolved_data)
        wins = sum(1 for t in resolved_data if sf(t['pnl']) > 0)
        losses = sum(1 for t in resolved_data if sf(t['pnl']) <= 0)

        # Fee tracking: Kalshi formula ceil(0.07 * count * P * (1-P)), max $0.02/contract
        all_buys = db.table('trades').select('count,price').eq('action', 'buy').execute()
        total_contracts = sum((t.get('count') or 1) for t in (all_buys.data or []))
        total_fees = 0.0
        for t in (all_buys.data or []):
            p = sf(t.get('price'))
            c = t.get('count') or 1
            fee_cents = min(0.07 * c * p * (1 - p), 0.02 * c)
            total_fees += fee_cents
        total_fees = round(total_fees, 2)
        pnl_after_fees = round(total_pnl - total_fees, 4)

        mode = "PAPER" if not ENABLE_TRADING else "LIVE"

        return jsonify({
            'portfolio': round(portfolio, 2),
            'cash': round(cash, 2),
            'positions_value': round(positions_value, 2),
            'net_pnl': round(total_pnl, 4),
            'total_fees': total_fees,
            'pnl_after_fees': pnl_after_fees,
            'total_contracts': total_contracts,
            'wins': wins,
            'losses': losses,
            'open_count': len(open_positions),
            'mode': mode,
        })
    except Exception as e:
        logger.error(f"API status error: {e}")
        return jsonify({'portfolio': 0, 'cash': 0, 'positions_value': 0, 'net_pnl': 0, 'total_fees': 0, 'pnl_after_fees': 0, 'total_contracts': 0, 'wins': 0, 'losses': 0, 'open_count': 0, 'mode': 'PAPER'})


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
        result = db.table('trades').select('*').eq('action', 'buy').not_.is_('pnl', 'null').order('created_at', desc=True).limit(50).execute()
        trades = []
        for t in (result.data or []):
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

.top-bar{background:#111;border:1px solid #1a1a1a;border-radius:6px;padding:14px 20px;margin-bottom:14px;display:flex;justify-content:center;align-items:center;gap:32px;flex-wrap:wrap;font-size:14px;font-weight:700}
.top-bar .sep{color:#333}

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
  <span id="mode-label">PAPER MODE</span> &mdash; crypto scalper &mdash; 15M only &mdash; 3-50c &mdash; sell +50% / stop -25%
  &mdash; NEXT SETTLEMENT: <span id="countdown" style="color:#ffaa00;font-weight:700">--:--</span>
</div>

<div class="top-bar" style="flex-direction:column;gap:6px">
  <div style="font-size:16px">PORTFOLIO: <span id="tb-portfolio">...</span></div>
  <div style="font-size:12px;color:#888">Positions: <span id="tb-positions">...</span> &nbsp;&nbsp; Cash: <span id="tb-cash">...</span></div>
  <div style="font-size:12px">P&amp;L: <span id="tb-pnl">...</span> &nbsp;&nbsp; Fees: <span id="tb-fees" class="red">...</span> &nbsp;&nbsp; Net: <span id="tb-net">...</span></div>
  <div style="font-size:12px">RECORD: <span id="tb-record">...</span></div>
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
  <div class="panel-header"><h2>Hot Markets</h2><div class="count" id="hot-count"></div></div>
  <div class="panel-body"><table><thead><tr>
    <th>Ticker</th><th>Yes Price</th><th>No Price</th><th>Volume</th>
  </tr></thead><tbody id="hot-body"><tr><td colspan="4" class="loading">Loading...</td></tr></tbody></table></div>
</div>

<div class="status-bar">
  <span>Series: 15M crypto (5 series) | 20min max | Stop -25% | Fees: ~$0.07/contract</span>
  <span>Buy: 3-50c | Sell: +50% | 5/20 contracts | 10s cycles | 50% reserve</span>
  <span>Last: <span id="last-update">&mdash;</span></span>
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

function timeAgo(iso){
  if(!iso)return '--';
  var diff=Math.floor((Date.now()-new Date(iso).getTime())/1000);
  if(diff<60)return diff+'s ago';
  if(diff<3600)return Math.floor(diff/60)+'m ago';
  if(diff<86400)return Math.floor(diff/3600)+'h ago';
  return Math.floor(diff/86400)+'d ago';
}

async function refresh(){
  var [status,open,trades,hot]=await Promise.all([
    fetchJSON('/api/status'),
    fetchJSON('/api/open'),
    fetchJSON('/api/trades'),
    fetchJSON('/api/hot')
  ]);

  if(status){
    $('tb-portfolio').textContent='$'+(status.portfolio||0).toFixed(2);
    $('tb-positions').textContent='$'+(status.positions_value||0).toFixed(2);
    $('tb-cash').textContent='$'+(status.cash||0).toFixed(2);

    var pnl=status.net_pnl||0;
    $('tb-pnl').innerHTML='<span class="'+cls(pnl)+'">'+(pnl>=0?'+$':'-$')+Math.abs(pnl).toFixed(2)+'</span>';
    var fees=status.total_fees||0;
    $('tb-fees').textContent='-$'+fees.toFixed(2)+' ('+( status.total_contracts||0)+'c)';
    var net=status.pnl_after_fees||0;
    $('tb-net').innerHTML='<span class="'+cls(net)+'">'+(net>=0?'+$':'-$')+Math.abs(net).toFixed(2)+'</span>';
    $('tb-record').innerHTML='<span class="green">'+(status.wins||0)+'W</span> <span class="gray">/</span> <span class="red">'+(status.losses||0)+'L</span>';

    var mode=status.mode||'PAPER';
    var isLive=mode==='LIVE';
    $('mode-label').textContent=isLive?'LIVE TRADING':'PAPER MODE';
    $('mode-dot').className='live-dot '+(isLive?'dot-live':'dot-paper');
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
      h+='<td class="'+gc+'">'+(p.unrealized>=0?'+':'')+p.unrealized.toFixed(2)+'</td>';
      h+='<td class="'+gc+'">'+(p.gain_pct>=0?'+':'')+p.gain_pct.toFixed(0)+'%</td>';
      h+='</tr>';
    });
    $('open-body').innerHTML=h||'<tr><td colspan="9" class="gray" style="text-align:center">No open positions</td></tr>';
  }

  if(trades){
    $('trades-count').textContent=trades.length+' trades';
    var h='';
    trades.forEach(function(t){
      var pc=cls(t.pnl);
      var rc=t.pnl>0?'row-green':t.pnl<0?'row-red':'';
      h+='<tr class="'+rc+'">';
      h+='<td>'+timeAgo(t.created_at)+'</td>';
      h+='<td style="font-size:10px">'+esc(t.ticker||'')+'</td>';
      h+='<td>'+esc(t.side||'')+'</td>';
      h+='<td>'+(t.count||1)+'</td>';
      h+='<td>$'+(t.entry||0).toFixed(2)+'</td>';
      h+='<td>$'+(t.exit||0).toFixed(2)+'</td>';
      h+='<td class="'+pc+'">'+(t.pnl>=0?'+':'')+t.pnl.toFixed(4)+'</td>';
      var gc2=cls(t.gain_pct||0);
      h+='<td class="'+gc2+'">'+(t.gain_pct>=0?'+':'')+(t.gain_pct||0).toFixed(0)+'%</td>';
      h+='</tr>';
    });
    $('trades-body').innerHTML=h||'<tr><td colspan="8" class="gray" style="text-align:center">No trades yet</td></tr>';
  }

  if(hot){
    $('hot-count').textContent='Top '+hot.length+' by volume';
    var h='';
    hot.forEach(function(m){
      h+='<tr>';
      h+='<td style="font-size:10px">'+esc(m.ticker)+'</td>';
      h+='<td>$'+(m.yes_ask||0).toFixed(2)+'</td>';
      h+='<td>$'+(m.no_ask||0).toFixed(2)+'</td>';
      h+='<td style="color:#ffaa00;font-weight:700">'+fmtVol(m.volume)+'</td>';
      h+='</tr>';
    });
    $('hot-body').innerHTML=h||'<tr><td colspan="4" class="gray" style="text-align:center">No data yet</td></tr>';
  }

  $('last-update').textContent=new Date().toLocaleTimeString();
}

refresh();
setInterval(refresh,15000);

function getNextSettlement(){
  var now=new Date();
  var mins=now.getMinutes();
  var nextQuarter=Math.ceil((mins+1)/15)*15;
  var next=new Date(now);
  next.setMinutes(nextQuarter,0,0);
  if(nextQuarter>=60){next.setHours(next.getHours()+1);next.setMinutes(0,0,0)}
  return next;
}
function updateCountdown(){
  var secs=Math.floor((getNextSettlement()-new Date())/1000);
  var m=Math.floor(secs/60);
  var s=secs%60;
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
    logger.info(f"Bot starting [{mode}] -- buy ${BUY_MIN}-${BUY_MAX}, sell +{SELL_THRESHOLD*100:.0f}%, stop {STOP_LOSS*100:.0f}%, reserve {CASH_RESERVE*100:.0f}%, max {MAX_MINS_TO_EXPIRY}min")
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
