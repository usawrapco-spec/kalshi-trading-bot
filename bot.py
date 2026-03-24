"""
Baseline v5: crypto scalper. Buy cheap, sell at +50%, take loss on expiry.
15M series only, $0.03-$0.15, flat 5 contracts, 10s cycles.
"""

import os, time, logging, traceback, uuid
from datetime import datetime, timezone, timedelta
from flask import Flask, jsonify, redirect, request, render_template_string
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
BUY_MAX = 0.15
SELL_THRESHOLD = 0.50
CONTRACTS = 5
CYCLE_SECONDS = 10
EXPIRY_MINUTES = 20
RESERVE_RATIO = 0.50

CRYPTO_SERIES = ['KXBTC15M', 'KXETH15M', 'KXSOL15M', 'KXXRP15M', 'KXDOGE15M']

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


def get_orderbook_bid(ticker, side):
    """Get best bid from orderbook for the given side. Returns cents (int) or 0."""
    try:
        book = kalshi_get(f"/markets/{ticker}/orderbook?depth=1")
        bids = book.get("orderbook", {}).get(side, [])
        if bids:
            return bids[0][0]  # [price_cents, quantity]
    except Exception as e:
        logger.error(f"Orderbook fetch failed for {ticker}: {e}")
    return 0


def place_order(ticker, side, action, price_cents, count):
    """Place order. price_cents is an integer in cents."""
    price_dollars = price_cents / 100.0

    if not ENABLE_TRADING:
        logger.info(f"PAPER {action.upper()}: {ticker} {side} x{count} @ ${price_dollars:.2f}")
        return {'order_id': 'paper', 'status': 'executed'}

    payload = {
        'ticker': ticker,
        'client_order_id': str(uuid.uuid4()),
        'action': action,
        'side': side,
        'type': 'limit',
        'count': count,
        'yes_price' if side == 'yes' else 'no_price': price_cents,
    }

    logger.info(f"{action.upper()} ORDER PAYLOAD: {payload}")

    try:
        resp = kalshi_post('/portfolio/orders', payload)
        logger.info(f"{action.upper()} RESPONSE: {resp}")
        order = resp.get('order', {})
        return {'order_id': order.get('order_id', ''), 'status': order.get('status', '')}
    except Exception as e:
        logger.error(f"ORDER FAILED: {action.upper()} {ticker} -- {e}")
        if hasattr(e, 'response') and e.response is not None:
            logger.error(f"RESPONSE BODY: {e.response.text}")
        return None


# === BALANCE ===

def get_kalshi_balance():
    """Get real cash balance from Kalshi API (returns dollars)."""
    try:
        resp = kalshi_get('/portfolio/balance')
        balance_cents = resp.get('balance', 0)
        return balance_cents / 100.0
    except Exception as e:
        logger.error(f"Kalshi balance fetch failed: {e}")
        return 0.0


def get_open_positions():
    try:
        result = db.table('trades').select('*').eq('action', 'buy').is_('pnl', 'null').execute()
        return result.data or []
    except Exception as e:
        logger.error(f"get_open_positions failed: {e}")
        return []


def get_dedup_tickers():
    """Get tickers we already own (open) plus any bought in last 20 minutes."""
    tickers = set()
    try:
        # All open positions (pnl is null)
        open_result = db.table('trades').select('ticker').eq('action', 'buy').is_('pnl', 'null').execute()
        for t in (open_result.data or []):
            tickers.add(t['ticker'])

        # All buys in last 20 minutes
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
        recent_result = db.table('trades').select('ticker').eq('action', 'buy').gte('created_at', cutoff).execute()
        for t in (recent_result.data or []):
            tickers.add(t['ticker'])
    except Exception as e:
        logger.error(f"get_dedup_tickers failed: {e}")
    return tickers


# === SELL LOGIC ===

def sell_position(trade):
    """Attempt to sell a single position. Returns True if sold/expired."""
    ticker = trade['ticker']
    side = trade['side']
    entry_price = sf(trade['price'])
    count = trade.get('count') or 1
    trade_id = trade['id']

    if entry_price <= 0:
        return False

    market = get_market(ticker)
    if not market:
        logger.warning(f"SELL CHECK: {ticker} -- market data unavailable")
        return False

    status = market.get('status', '')
    result_val = market.get('result', '')

    # Settled / expired by Kalshi
    if status in ('closed', 'settled', 'finalized') or result_val:
        if result_val == side:
            pnl = round((1.0 - entry_price) * count, 4)
            reason = "WIN settled @$1.00"
        elif result_val:
            pnl = round(-entry_price * count, 4)
            reason = "LOSS expired"
        else:
            return False

        logger.info(f"EXPIRED/SETTLED: {ticker} {side} | {reason} | pnl=${pnl:.4f}")
        try:
            db.table('trades').update({'pnl': float(pnl)}).eq('id', trade_id).execute()
        except Exception as e:
            logger.error(f"Settle DB error: {e}")
        return True

    # Check if close_time has passed
    close_time = market.get('close_time') or market.get('expected_expiration_time')
    if close_time:
        try:
            close_dt = datetime.fromisoformat(close_time.replace('Z', '+00:00'))
            if close_dt < datetime.now(timezone.utc):
                pnl = round(-entry_price * count, 4)
                logger.info(f"EXPIRED (time past): {ticker} pnl=${pnl:.4f}")
                try:
                    db.table('trades').update({'pnl': float(pnl)}).eq('id', trade_id).execute()
                except:
                    pass
                return True
        except:
            pass

    # Get current bid from orderbook (cents)
    bid_cents = get_orderbook_bid(ticker, side)
    current_bid = bid_cents / 100.0

    # Update current_bid in DB for dashboard
    try:
        db.table('trades').update({'current_bid': float(current_bid)}).eq('id', trade_id).execute()
    except:
        pass

    if bid_cents <= 0:
        pnl = round(-entry_price * count, 4)
        logger.info(f"EXPIRED: {ticker} {side} | bid=$0 (no orderbook bids) | pnl=${pnl:.4f}")
        try:
            db.table('trades').update({'pnl': float(pnl)}).eq('id', trade_id).execute()
        except Exception as e:
            logger.error(f"Expire DB error: {e}")
        return True

    gain = (current_bid - entry_price) / entry_price
    gain_pct = gain * 100
    logger.info(f"  POS: {ticker} {side} entry=${entry_price:.2f} bid=${current_bid:.2f} {gain_pct:+.0f}%")

    # Take profit at +50%
    if gain >= SELL_THRESHOLD:
        logger.info(f"SELL ATTEMPT: ticker={ticker} side={side} count={count} bid=${current_bid:.2f} gain=+{gain_pct:.0f}%")

        result = place_order(ticker, side, 'sell', bid_cents, count)

        if not result:
            logger.error(f"SELL FAILED: ticker={ticker} error=order returned None")
            return False

        pnl = round((current_bid - entry_price) * count, 4)
        logger.info(f"SELL EXECUTED: {ticker} {side} x{count} @ ${current_bid:.2f} | pnl=${pnl:.4f} | +{gain_pct:.0f}% PROFIT")
        try:
            db.table('trades').update({
                'pnl': float(pnl),
                'current_bid': float(current_bid),
            }).eq('id', trade_id).execute()
        except Exception as e:
            logger.error(f"Sell DB error: {e}")
        return True

    return False


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
        result = sell_position(trade)
        if result:
            # Check if it was a sell or expiry by looking at the gain
            entry = sf(trade['price'])
            bid = sf(trade.get('current_bid', 0))
            if bid > 0 and entry > 0 and (bid - entry) / entry >= SELL_THRESHOLD:
                sold += 1
            else:
                expired += 1

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
    cash = get_kalshi_balance()
    owned = get_dedup_tickers()
    logger.info(f"Cash: ${cash:.2f} | {len(owned)} deduped tickers")

    # Check deployed cost against reserve
    open_positions = get_open_positions()
    deployed = sum(sf(t.get('price', 0)) * (t.get('count') or 1) for t in open_positions)

    if deployed >= cash * RESERVE_RATIO:
        logger.info(f"Reserve hit: deployed ${deployed:.2f} >= {RESERVE_RATIO*100:.0f}% of cash ${cash:.2f} -- skipping buys")
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
                if mins_left > EXPIRY_MINUTES or mins_left <= 0:
                    continue
            except:
                continue
        else:
            continue

        yes_ask = float(market.get('yes_ask_dollars') or '999')
        no_ask = float(market.get('no_ask_dollars') or '999')

        # Pick whichever side has the lower ask, within price cap
        if yes_ask <= no_ask and BUY_MIN <= yes_ask <= BUY_MAX:
            side, price = 'yes', yes_ask
        elif BUY_MIN <= no_ask <= BUY_MAX:
            side, price = 'no', no_ask
        elif BUY_MIN <= yes_ask <= BUY_MAX:
            side, price = 'yes', yes_ask
        else:
            continue

        candidates.append({'ticker': ticker, 'side': side, 'price': price})

    # Sort cheapest first
    candidates.sort(key=lambda x: x['price'])
    logger.info(f"Found {len(candidates)} buy candidates")

    bought = 0
    for c in candidates:
        cost = c['price'] * CONTRACTS

        # Re-check reserve with updated deployed amount
        if deployed + cost >= cash * RESERVE_RATIO:
            logger.info(f"Reserve would be hit: deployed ${deployed:.2f} + cost ${cost:.2f} >= limit ${cash * RESERVE_RATIO:.2f}")
            break

        price_cents = int(round(c['price'] * 100))
        result = place_order(c['ticker'], c['side'], 'buy', price_cents, CONTRACTS)
        if not result:
            continue

        logger.info(f"BUY: {c['ticker']} {c['side']} x{CONTRACTS} @ ${c['price']:.2f}")
        try:
            db.table('trades').insert({
                'ticker': c['ticker'], 'side': c['side'], 'action': 'buy',
                'price': float(c['price']), 'count': CONTRACTS,
                'current_bid': float(c['price']),
            }).execute()
            owned.add(c['ticker'])
            deployed += cost
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
    by_vol = sorted(markets, key=lambda m: _get_volume(m), reverse=True)[:10]
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
    cash = get_kalshi_balance()
    logger.info(f"=== CYCLE START [{mode}] === Cash: ${cash:.2f}")

    check_sells()

    markets = fetch_all_markets()
    update_hot_markets(markets)
    buy_candidates(markets)

    cash = get_kalshi_balance()
    logger.info(f"=== CYCLE END [{mode}] === Cash: ${cash:.2f}")


# === MANUAL SELL API ===

def manual_sell_trade(trade):
    """Manually sell a single trade. Returns (success, message)."""
    ticker = trade['ticker']
    side = trade['side']
    entry_price = sf(trade['price'])
    count = trade.get('count') or 1
    trade_id = trade['id']

    bid_cents = get_orderbook_bid(ticker, side)
    if bid_cents <= 0:
        return False, f"No bids for {ticker} ({side})"

    current_bid = bid_cents / 100.0
    logger.info(f"MANUAL SELL ATTEMPT: ticker={ticker} side={side} count={count} bid=${current_bid:.2f}")

    result = place_order(ticker, side, 'sell', bid_cents, count)
    if not result:
        return False, f"Order failed for {ticker}"

    pnl = round((current_bid - entry_price) * count, 4)
    try:
        db.table('trades').update({
            'pnl': float(pnl),
            'current_bid': float(current_bid),
        }).eq('id', trade_id).execute()
    except Exception as e:
        logger.error(f"Manual sell DB error: {e}")

    return True, f"Sold {ticker} @ ${current_bid:.2f} pnl=${pnl:.4f}"


# === DASHBOARD API ===

@app.route('/')
def index():
    return redirect('/dashboard')


@app.route('/api/status')
def api_status():
    try:
        cash = get_kalshi_balance()
        open_positions = get_open_positions()
        positions_value = sum(sf(t.get('current_bid', 0)) * (t.get('count') or 1) for t in open_positions)
        portfolio = cash + positions_value

        resolved = db.table('trades').select('pnl').eq('action', 'buy').not_.is_('pnl', 'null').execute()
        resolved_data = resolved.data or []
        total_pnl = sum(sf(t['pnl']) for t in resolved_data)
        wins = sum(1 for t in resolved_data if sf(t['pnl']) > 0)
        losses = sum(1 for t in resolved_data if sf(t['pnl']) <= 0)
        total = wins + losses
        win_pct = round(wins / total * 100) if total > 0 else 0

        mode = "PAPER" if not ENABLE_TRADING else "LIVE"

        return jsonify({
            'portfolio': round(portfolio, 2),
            'cash': round(cash, 2),
            'positions_value': round(positions_value, 2),
            'net_pnl': round(total_pnl, 4),
            'wins': wins,
            'losses': losses,
            'win_pct': win_pct,
            'open_count': len(open_positions),
            'mode': mode,
        })
    except Exception as e:
        logger.error(f"API status error: {e}")
        return jsonify({'portfolio': 0, 'cash': 0, 'positions_value': 0, 'net_pnl': 0, 'wins': 0, 'losses': 0, 'win_pct': 0, 'open_count': 0, 'mode': 'PAPER'})


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

            # Get time to expiry
            ttl = ''
            market = get_market(t.get('ticker', ''))
            if market:
                close_time = market.get('close_time') or market.get('expected_expiration_time')
                if close_time:
                    try:
                        close_dt = datetime.fromisoformat(close_time.replace('Z', '+00:00'))
                        secs = (close_dt - datetime.now(timezone.utc)).total_seconds()
                        if secs > 0:
                            ttl = f"{int(secs // 60)}m{int(secs % 60):02d}s"
                        else:
                            ttl = "EXPIRED"
                    except:
                        pass

            positions.append({
                'id': t.get('id'),
                'ticker': t.get('ticker', ''),
                'side': t.get('side', ''),
                'count': count,
                'entry': price,
                'current_bid': current,
                'unrealized': unrealized,
                'gain_pct': gain_pct,
                'ttl': ttl,
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


@app.route('/api/sell/<trade_id>', methods=['POST'])
def api_sell_one(trade_id):
    try:
        result = db.table('trades').select('*').eq('id', trade_id).eq('action', 'buy').is_('pnl', 'null').execute()
        if not result.data:
            return jsonify({'error': 'Trade not found or already closed'}), 404
        trade = result.data[0]
        ok, msg = manual_sell_trade(trade)
        return jsonify({'success': ok, 'message': msg}), 200 if ok else 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/sell-all-green', methods=['POST'])
def api_sell_all_green():
    positions = get_open_positions()
    results = []
    for t in positions:
        entry = sf(t.get('price'))
        current = sf(t.get('current_bid'))
        if entry > 0 and current > entry:
            ok, msg = manual_sell_trade(t)
            results.append({'ticker': t['ticker'], 'success': ok, 'message': msg})
    return jsonify({'sold': len([r for r in results if r['success']]), 'results': results})


@app.route('/dashboard')
def dashboard():
    return DASHBOARD_HTML


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Kalshi Scalper - Baseline v5</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&display=swap');
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0a0a0a;color:#e0e0e0;font-family:'JetBrains Mono','SF Mono','Fira Code',monospace;padding:16px 20px;font-size:13px}

@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
.live-dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;animation:pulse 2s infinite}
.dot-paper{background:#ffaa00}
.dot-live{background:#00d673}

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

.btn{cursor:pointer;border:none;border-radius:3px;padding:3px 8px;font-family:inherit;font-size:10px;font-weight:700}
.btn-sell{background:#00d673;color:#0a0a0a}
.btn-sell:hover{background:#00ff8a}
.btn-sell-all{background:#00d673;color:#0a0a0a;padding:6px 16px;font-size:11px}
.btn-sell-all:hover{background:#00ff8a}

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
  <span id="mode-label">PAPER MODE</span> &mdash; baseline v5 &mdash; 15M crypto &mdash; 3-15c &mdash; sell at 50%
  &mdash; NEXT SETTLEMENT: <span id="countdown" style="color:#ffaa00;font-weight:700">--:--</span>
</div>

<div class="top-bar" style="flex-direction:column;gap:6px">
  <div style="font-size:16px">PORTFOLIO: <span id="tb-portfolio">...</span></div>
  <div style="font-size:12px;color:#888">CASH: <span id="tb-cash">...</span> &nbsp;|&nbsp; POSITIONS: <span id="tb-positions">...</span></div>
  <div style="font-size:12px">P&amp;L: <span id="tb-pnl">...</span> &nbsp;|&nbsp; RECORD: <span id="tb-record">...</span></div>
</div>

<div class="panel">
  <div class="panel-header">
    <h2>Open Positions</h2>
    <div style="display:flex;align-items:center;gap:10px">
      <div class="count" id="open-count"></div>
      <button class="btn btn-sell-all" onclick="sellAllGreen()">SELL ALL GREEN</button>
    </div>
  </div>
  <div class="panel-body"><table><thead><tr>
    <th>Ticker</th><th>Side</th><th>Qty</th><th>Entry</th><th>Bid</th><th>P&amp;L</th><th>Gain%</th><th>TTL</th><th></th>
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
  <span>Series: 15M crypto (5 series) | Expiry: 20min | Reserve: 50%</span>
  <span>Buy: 3-15c | Sell: 50% | Flat 5 contracts | 10s cycles</span>
  <span>Last: <span id="last-update">&mdash;</span></span>
</div>
<div class="footer">Baseline v5 &mdash; auto-refresh 15s</div>

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

async function sellOne(tradeId){
  if(!confirm('Sell this position?'))return;
  var r=await fetch('/api/sell/'+tradeId,{method:'POST'});
  var d=await r.json();
  alert(d.message||d.error||'Done');
  refresh();
}

async function sellAllGreen(){
  if(!confirm('Sell ALL green positions?'))return;
  var r=await fetch('/api/sell-all-green',{method:'POST'});
  var d=await r.json();
  alert('Sold '+d.sold+' positions');
  refresh();
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
    var total=status.wins+status.losses;
    var wpct=status.win_pct||0;
    $('tb-record').innerHTML='<span class="green">'+(status.wins||0)+'W</span> <span class="gray">/</span> <span class="red">'+(status.losses||0)+'L</span> <span class="gray">('+wpct+'%)</span>';

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
      h+='<tr class="'+rc+'">';
      h+='<td style="font-size:10px">'+esc(p.ticker)+'</td>';
      h+='<td>'+esc(p.side)+'</td>';
      h+='<td>'+p.count+'</td>';
      h+='<td>$'+p.entry.toFixed(2)+'</td>';
      h+='<td>'+bidText+'</td>';
      h+='<td class="'+gc+'">'+(p.unrealized>=0?'+':'')+p.unrealized.toFixed(4)+'</td>';
      h+='<td class="'+gc+'">'+(p.gain_pct>=0?'+':'')+p.gain_pct.toFixed(0)+'%</td>';
      h+='<td>'+esc(p.ttl||'--')+'</td>';
      h+='<td>'+(p.gain_pct>0?'<button class="btn btn-sell" onclick="sellOne(\''+p.id+'\')">SELL</button>':'')+'</td>';
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
    logger.info(f"Bot starting [{mode}] -- baseline v5")
    logger.info(f"Series: {CRYPTO_SERIES}")
    logger.info(f"Price: ${BUY_MIN}-${BUY_MAX} | Sell: {SELL_THRESHOLD*100:.0f}% | Contracts: {CONTRACTS} | Cycles: {CYCLE_SECONDS}s")
    logger.info(f"Expiry filter: {EXPIRY_MINUTES}min | Reserve: {RESERVE_RATIO*100:.0f}% | Dedup: global+20min")

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
