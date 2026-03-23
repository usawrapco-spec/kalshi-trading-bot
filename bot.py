import os, time, logging, math, requests, traceback
from flask import Flask, render_template_string
from threading import Thread
from supabase import create_client
from kalshi_auth import KalshiAuth

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# === CONFIG ===
KALSHI_HOST = os.environ.get('KALSHI_API_HOST', 'https://api.elections.kalshi.com')
SUPABASE_URL = os.environ.get('SUPABASE_URL')
SUPABASE_KEY = os.environ.get('SUPABASE_KEY')
PORT = int(os.environ.get('PORT', 8080))

MIN_PRICE = 0.02
MAX_PRICE = 0.95
MAX_BUYS_PER_CYCLE = 50
CYCLE_SECONDS = 30
STARTING_BALANCE = 50.00
RESERVE_BALANCE = 3.00

# === LIVE TRADING CONFIG ===
ENABLE_TRADING = os.environ.get('ENABLE_TRADING', 'false').lower() == 'true'
LIVE_STRATEGIES = [s.strip() for s in os.environ.get('LIVE_STRATEGIES', '').split(',') if s.strip()]
LIVE_MAX_PRICE = 0.50         # Max $0.50 per contract for live
LIVE_MAX_COUNT = 1            # Max 1 contract per live trade
LIVE_MAX_EXPOSURE = 45.00     # Max $45 total live exposure
LIVE_MAX_PER_CYCLE = 5        # Max 5 live trades per cycle
LIVE_RESERVE = 5.00           # Never go below $5 real balance

# === INIT ===
db = create_client(SUPABASE_URL, SUPABASE_KEY)
auth = KalshiAuth()
app = Flask(__name__)


def sf(val):
    try:
        return float(val) if val is not None else 0.0
    except:
        return 0.0


# === STARTUP ===

def close_all_old_positions():
    """Resolve old positions + fix both-sides. Run ONCE at startup."""
    try:
        for reason in ['CLOSED — nuclear reset', 'RESOLVED — fresh start',
                       'RESOLVED — fresh start v2', 'RESOLVED — activity reset',
                       'RESOLVED — velocity reset']:
            db.table('trades').delete().eq('reason', reason).execute()

        # Fix both-sides: keep cheapest, resolve rest
        open_buys = db.table('trades').select('id,ticker,side,price') \
            .eq('action', 'buy').is_('pnl', 'null').execute()
        if open_buys.data:
            ticker_sides = {}
            for t in open_buys.data:
                tk = t['ticker']
                if tk not in ticker_sides:
                    ticker_sides[tk] = []
                ticker_sides[tk].append(t)

            resolved = 0
            for tk, trades in ticker_sides.items():
                if len(trades) >= 2:
                    trades.sort(key=lambda x: sf(x.get('price')))
                    for t in trades[1:]:
                        db.table('trades').update({
                            'pnl': 0.0,
                            'reason': 'RESOLVED — both-sides fix',
                        }).eq('id', t['id']).execute()
                        resolved += 1
            if resolved:
                logger.info(f"Fixed both-sides: resolved {resolved} duplicates")

        # Resolve any non-crypto positions (trending/weather leftovers)
        all_open = db.table('trades').select('id,ticker,strategy') \
            .eq('action', 'buy').is_('pnl', 'null').execute()
        if all_open.data:
            non_crypto = [t for t in all_open.data if t.get('strategy') != 'crypto']
            for t in non_crypto:
                db.table('trades').update({
                    'pnl': 0.0,
                    'reason': 'RESOLVED — crypto-only reset',
                }).eq('id', t['id']).execute()
            if non_crypto:
                logger.info(f"Resolved {len(non_crypto)} non-crypto positions")

        logger.info("Startup cleanup complete")
    except Exception as e:
        logger.info(f"Startup cleanup: {e}")


# === BALANCE ===

def get_balance():
    open_buys = db.table('trades').select('price,count') \
        .eq('action', 'buy').is_('pnl', 'null').execute()
    open_cost = sum(sf(t['price']) * (t['count'] or 1) for t in (open_buys.data or []))

    sells = db.table('trades').select('pnl') \
        .eq('action', 'sell').not_.is_('pnl', 'null').execute()
    sell_pnls = [sf(t['pnl']) for t in (sells.data or [])]

    settled = db.table('trades').select('pnl') \
        .eq('action', 'buy').not_.is_('pnl', 'null').execute()
    settled_pnls = [sf(t['pnl']) for t in (settled.data or [])]

    all_pnls = sell_pnls + settled_pnls
    total_profit = sum(p for p in all_pnls if p > 0)
    total_loss = abs(sum(p for p in all_pnls if p < 0))

    saved = round(total_profit * 0.25, 4)
    trading = round(STARTING_BALANCE - open_cost + total_profit * 0.75 - total_loss, 2)
    return trading, saved


def get_owned():
    """Returns set of TICKER STRINGS — one side per market only."""
    result = db.table('trades').select('ticker') \
        .eq('action', 'buy').is_('pnl', 'null').execute()
    return {t['ticker'] for t in (result.data or [])}


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


def get_kalshi_balance():
    """Get real Kalshi account balance."""
    try:
        resp = kalshi_get('/portfolio/balance')
        return float(resp.get('balance', 0)) / 100  # Kalshi returns cents
    except Exception as e:
        logger.error(f"Balance fetch failed: {e}")
        return 0.0


def get_live_exposure():
    """Total $ deployed in live trades."""
    result = db.table('trades').select('price,count') \
        .eq('action', 'buy').is_('pnl', 'null').eq('strategy', 'crypto_live').execute()
    return sum(sf(t['price']) * (t['count'] or 1) for t in (result.data or []))


def place_live_order(ticker, side, price, count):
    """Place a real Kalshi order. Returns order_id or None."""
    # Convert price to cents for Kalshi API
    price_cents = int(round(price * 100))
    try:
        logger.info(f"LIVE ORDER: {ticker} {side} x{count} @ ${price:.2f} ({price_cents}c)")
        resp = kalshi_post('/portfolio/orders', {
            'ticker': ticker,
            'action': 'buy',
            'side': side,
            'type': 'limit',
            'count': count,
            'yes_price' if side == 'yes' else 'no_price': price_cents,
        })
        order = resp.get('order', {})
        order_id = order.get('order_id', '')
        status = order.get('status', '')
        logger.info(f"LIVE ORDER PLACED: {order_id} status={status}")
        return order_id
    except Exception as e:
        logger.error(f"LIVE ORDER FAILED: {e}")
        return None


def place_live_sell(ticker, side, price, count):
    """Place a real Kalshi sell order."""
    price_cents = int(round(price * 100))
    try:
        logger.info(f"LIVE SELL ORDER: {ticker} {side} x{count} @ ${price:.2f}")
        resp = kalshi_post('/portfolio/orders', {
            'ticker': ticker,
            'action': 'sell',
            'side': side,
            'type': 'limit',
            'count': count,
            'yes_price' if side == 'yes' else 'no_price': price_cents,
        })
        order = resp.get('order', {})
        order_id = order.get('order_id', '')
        logger.info(f"LIVE SELL PLACED: {order_id}")
        return order_id
    except Exception as e:
        logger.error(f"LIVE SELL FAILED: {e}")
        return None


def is_live_enabled():
    return ENABLE_TRADING and 'crypto' in LIVE_STRATEGIES


# === CRYPTO ONLY ===

CRYPTO_SERIES = [
    'KXBTC15M', 'KXETH15M', 'KXSOL15M',
    'KXBTC1H', 'KXETH1H', 'KXSOL1H',
    'KXBTCD', 'KXETHD', 'KXSOLD',
    'KXBTC', 'KXETH', 'KXSOL',
]


def fetch_all_crypto():
    """Fetch all crypto markets — ~12 API calls, one per series."""
    markets = []
    for series in CRYPTO_SERIES:
        try:
            resp = kalshi_get(f"/markets?series_ticker={series}&status=open&limit=100")
            markets.extend(resp.get('markets', []))
        except:
            pass
    logger.info(f"Fetched {len(markets)} crypto markets from {len(CRYPTO_SERIES)} series")
    return markets


# === BUY LOGIC ===

def get_buy_count(ticker):
    """1 contract on 15-min, 2 on bracket, 1 on daily/hourly."""
    if '15M' in ticker:
        return 1
    if ticker.startswith('KXBTC-') or ticker.startswith('KXETH-') or ticker.startswith('KXSOL-'):
        return 2
    return 1


def run_buys(markets):
    trading_bal, _ = get_balance()
    owned = get_owned()
    logger.info(f"Own {len(owned)} tickers, balance ${trading_bal:.2f}")

    buys = []
    for m in markets:
        ticker = m.get('ticker', '')
        if ticker in owned:
            continue
        if 'KXMVE' in ticker:
            continue

        yes_bid = float(m.get('yes_bid_dollars', '0') or '0')
        yes_ask = float(m.get('yes_ask_dollars', '0') or '0')
        no_bid = float(m.get('no_bid_dollars', '0') or '0')
        no_ask = float(m.get('no_ask_dollars', '0') or '0')

        # Collect liquid sides
        candidates = []
        if yes_bid > 0 and yes_ask > 0 and MIN_PRICE <= yes_ask <= MAX_PRICE:
            candidates.append(('yes', yes_ask, yes_bid, yes_ask - yes_bid))
        if no_bid > 0 and no_ask > 0 and MIN_PRICE <= no_ask <= MAX_PRICE:
            candidates.append(('no', no_ask, no_bid, no_ask - no_bid))

        if not candidates:
            continue

        # Pick side with tightest spread
        candidates.sort(key=lambda x: x[3])
        side, price, bid, spread = candidates[0]
        count = get_buy_count(ticker)

        buys.append({
            'ticker': ticker, 'side': side, 'price': price,
            'bid': bid, 'spread': spread, 'count': count,
        })

    # Sort by tightest spread
    buys.sort(key=lambda x: x['spread'])

    # Live trading state
    live_enabled = is_live_enabled()
    live_bought = 0
    live_exposure = get_live_exposure() if live_enabled else 0

    if live_enabled:
        logger.info(f"LIVE TRADING ENABLED — exposure=${live_exposure:.2f}/{LIVE_MAX_EXPOSURE}")

    bought = 0
    for b in buys:
        if bought >= MAX_BUYS_PER_CYCLE:
            break
        if trading_bal < RESERVE_BALANCE:
            break
        cost = b['price'] * b['count']
        if cost > trading_bal - RESERVE_BALANCE:
            continue

        # Try live order if enabled and within limits
        is_live = False
        order_id = None
        if (live_enabled
            and live_bought < LIVE_MAX_PER_CYCLE
            and b['price'] <= LIVE_MAX_PRICE
            and live_exposure + b['price'] <= LIVE_MAX_EXPOSURE):
            order_id = place_live_order(b['ticker'], b['side'], b['price'], 1)
            if order_id:
                is_live = True
                live_bought += 1
                live_exposure += b['price']

        strategy = 'crypto_live' if is_live else 'crypto'
        label = 'LIVE BUY' if is_live else 'BUY'
        logger.info(f"{label}: {b['ticker']} {b['side']} x{b['count']} @ ${b['price']:.2f} (bid=${b['bid']:.2f} spread=${b['spread']:.2f})")

        try:
            db.table('trades').insert({
                'ticker': b['ticker'], 'side': b['side'], 'action': 'buy',
                'price': float(b['price']), 'count': 1 if is_live else b['count'],
                'strategy': strategy,
                'reason': f"{strategy}: {b['side'].upper()} @ ${b['price']:.2f} bid=${b['bid']:.2f}",
                'last_seen_bid': float(b['bid']),
                'current_bid': float(b['bid']),
            }).execute()
            owned.add(b['ticker'])
            trading_bal -= cost
            bought += 1
        except Exception as e:
            logger.error(f"Buy insert failed: {e}")

    logger.info(f"Bought {bought} (live={live_bought}), balance ${trading_bal:.2f}")


# === SELL LOGIC — ADAPTIVE THRESHOLD, HANDLE SETTLEMENTS ===

sell_history = []  # Rolling last 20 sell gain percentages

def check_sells():
    """Check ALL positions. Adaptive threshold (30% floor). Never sell at a loss."""
    global sell_history
    logger.info("check_sells() called")

    # Adaptive threshold: 50% of average win, minimum 30%
    if len(sell_history) >= 10:
        avg_win = sum(sell_history) / len(sell_history)
        threshold = max(30, avg_win * 0.5)
    else:
        threshold = 30
        avg_win = 0
    logger.info(f"Sell threshold: {threshold:.0f}% (avg win: {avg_win:.0f}%, {len(sell_history)} sells tracked)")

    open_buys = db.table('trades').select('*') \
        .eq('action', 'buy').is_('pnl', 'null').execute()

    if not open_buys.data:
        logger.info("No open positions")
        return

    sold = 0
    settled = 0

    for trade in open_buys.data:
        ticker = trade['ticker']
        side = trade['side']
        entry_price = sf(trade['price'])
        count = trade['count'] or 1
        if entry_price <= 0:
            continue

        try:
            market = get_market(ticker)
        except:
            continue
        if not market:
            continue

        status = market.get('status', '')
        result_val = market.get('result', '')

        # === SETTLEMENT CHECK ===
        if status in ('closed', 'settled', 'finalized') or result_val:
            if result_val == side:
                pnl = round((1.0 - entry_price) * count, 4)
                reason = f"WIN — settled $1.00 (entry ${entry_price:.2f})"
                settle_price = 1.0
            elif result_val:
                pnl = round(-entry_price * count, 4)
                reason = f"LOSS — expired (entry ${entry_price:.2f})"
                settle_price = 0.0
            else:
                continue

            logger.info(f"SETTLED: {ticker} {side} | {reason} | pnl=${pnl:.4f}")
            try:
                logger.info(f"INSERTING SETTLE: {ticker}")
                db.table('trades').insert({
                    'ticker': ticker, 'side': side, 'action': 'sell',
                    'price': float(settle_price), 'count': count,
                    'pnl': float(pnl), 'strategy': 'crypto',
                    'reason': reason,
                    'sell_gain_pct': float(round(((settle_price - entry_price) / entry_price) * 100, 1)),
                }).execute()
                logger.info(f"SETTLE SAVED: {ticker}")
            except Exception as e:
                logger.error(f"SETTLE INSERT FAILED: {e}")

            try:
                db.table('trades').update({
                    'pnl': float(pnl),
                    'current_bid': float(settle_price),
                }).eq('id', trade['id']).execute()
            except:
                pass
            settled += 1
            continue

        # === PRICE CHECK ===
        if side == 'yes':
            current_bid = float(market.get('yes_bid_dollars', '0') or '0')
        else:
            current_bid = float(market.get('no_bid_dollars', '0') or '0')

        if current_bid <= 0:
            continue

        gain_pct = ((current_bid - entry_price) / entry_price) * 100

        # Update current price for dashboard
        try:
            db.table('trades').update({
                'current_bid': float(current_bid),
                'last_seen_bid': float(current_bid),
            }).eq('id', trade['id']).execute()
        except:
            pass

        # NEVER sell at a loss
        if gain_pct <= 0 or current_bid <= entry_price:
            continue

        # Adaptive threshold (30% floor)
        should_sell = False
        reason = ""
        if gain_pct >= threshold:
            should_sell = True
            reason = f"PROFIT +{gain_pct:.0f}% (thresh={threshold:.0f}%)"

        if should_sell:
            pnl = round((current_bid - entry_price) * count, 4)
            strategy = trade.get('strategy', 'crypto')

            # Live sell for live positions
            if strategy == 'crypto_live' and is_live_enabled():
                sell_order_id = place_live_sell(ticker, side, current_bid, count)
                if not sell_order_id:
                    logger.error(f"LIVE SELL FAILED — skipping {ticker}")
                    continue
                label = 'LIVE SELL'
            else:
                label = 'SELL'

            logger.info(f"INSERTING {label}: {ticker} {side} +{gain_pct:.0f}% pnl=${pnl:.4f}")
            try:
                sell_result = db.table('trades').insert({
                    'ticker': ticker, 'side': side, 'action': 'sell',
                    'price': float(current_bid), 'count': count,
                    'pnl': float(pnl), 'strategy': strategy,
                    'reason': reason,
                    'sell_gain_pct': float(round(gain_pct, 1)),
                }).execute()
                logger.info(f"{label} SAVED: {len(sell_result.data) if sell_result.data else 0} rows")
            except Exception as e:
                logger.error(f"{label} INSERT FAILED: {e}")
                logger.error(f"{label} traceback: {traceback.format_exc()}")

            try:
                db.table('trades').update({
                    'pnl': float(pnl),
                    'current_bid': float(current_bid),
                    'sell_gain_pct': float(round(gain_pct, 1)),
                }).eq('id', trade['id']).execute()
                logger.info(f"BUY UPDATED: id={trade['id']} pnl={pnl}")
            except Exception as e:
                logger.error(f"BUY UPDATE FAILED: {e}")
            sold += 1

            # Track for adaptive threshold
            sell_history.append(gain_pct)
            if len(sell_history) > 20:
                sell_history = sell_history[-20:]

    logger.info(f"Checked {len(open_buys.data)} positions | sold={sold} settled={settled}")


# === MAIN CYCLE ===

def run_cycle():
    trading_bal, _ = get_balance()
    logger.info(f"=== CYCLE START === Balance: ${trading_bal:.2f}")

    try:
        check_sells()
    except Exception as e:
        logger.error(f"Sell check error: {e}")

    try:
        markets = fetch_all_crypto()
        run_buys(markets)
    except Exception as e:
        logger.error(f"Buy error: {e}")

    trading_bal, _ = get_balance()
    logger.info(f"=== CYCLE END === Balance: ${trading_bal:.2f}")


# === DASHBOARD ===

DASHBOARD_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Kalshi Crypto Scalp Bot</title>
    <meta http-equiv="refresh" content="30">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { background: #0a0a0f; color: #e0e0e0; font-family: 'Courier New', monospace; padding: 20px; }
        .header { text-align: center; margin-bottom: 20px; }
        .header h1 { color: #ffaa00; font-size: 24px; }
        .header .subtitle { color: #666; font-size: 12px; }
        .stats { display: flex; gap: 12px; margin-bottom: 20px; flex-wrap: wrap; }
        .stat-box { background: #12121a; border: 1px solid #222; border-radius: 8px; padding: 12px; flex: 1; min-width: 100px; text-align: center; }
        .stat-label { color: #666; font-size: 10px; text-transform: uppercase; }
        .stat-value { font-size: 20px; font-weight: bold; margin-top: 4px; }
        .green { color: #00ff88; }
        .red { color: #ff4444; }
        .yellow { color: #ffaa00; }
        .gray { color: #555; }
        .section { background: #12121a; border: 1px solid #222; border-radius: 8px; padding: 15px; margin-bottom: 15px; }
        .section h2 { color: #ffaa00; font-size: 14px; margin-bottom: 10px; text-transform: uppercase; }
        table { width: 100%; border-collapse: collapse; font-size: 12px; }
        th { color: #666; text-align: left; padding: 6px 8px; border-bottom: 1px solid #333; text-transform: uppercase; font-size: 10px; }
        td { padding: 6px 8px; border-bottom: 1px solid #1a1a2e; }
        tr:hover { background: #1a1a2e; }
        .badge { padding: 2px 8px; border-radius: 4px; font-size: 10px; font-weight: bold; }
        .badge-buy { background: #003300; color: #00ff88; }
        .badge-sell { background: #330000; color: #ff4444; }
        .badge-settled { background: #222; color: #888; }
        .badge-crypto { background: #332200; color: #ffaa00; }
        .badge-unknown { background: #222; color: #888; }
    </style>
</head>
<body>
    <div class="header">
        <h1>CRYPTO SCALP BOT</h1>
        <div class="subtitle">Paper Trading — $50 — 30s cycles — BTC/ETH/SOL only — 5% sell threshold</div>
    </div>
    <div class="stats">
        <div class="stat-box"><div class="stat-label">Balance</div><div class="stat-value green">${{balance}}</div></div>
        <div class="stat-box"><div class="stat-label">Saved (25%)</div><div class="stat-value yellow">${{saved}}</div></div>
        <div class="stat-box"><div class="stat-label">Realized P&L</div><div class="stat-value {{'green' if rpnl >= 0 else 'red'}}">${{rpnl_fmt}}</div></div>
        <div class="stat-box"><div class="stat-label">Open</div><div class="stat-value">{{total_open}}</div></div>
        <div class="stat-box"><div class="stat-label">Deployed</div><div class="stat-value yellow">${{deployed}}</div></div>
        <div class="stat-box"><div class="stat-label">Record</div><div class="stat-value"><span class="green">{{wins}}W</span>/<span class="red">{{losses}}L</span></div></div>
    </div>
    <div class="section">
        <h2>Positions & Trades</h2>
        <table>
            <tr><th>Time</th><th>Action</th><th>Ticker</th><th>Side</th><th>Cnt</th><th>Entry</th><th>Bid</th><th>P&L</th><th>%</th></tr>
            {% for t in trades %}
            <tr>
                <td>{{t.time}}</td>
                <td><span class="badge badge-{{t.cls}}">{{t.action}}</span></td>
                <td style="font-size:10px">{{t.ticker}}</td>
                <td>{{t.side}}</td>
                <td>{{t.count}}</td>
                <td>${{"%.2f"|format(t.entry)}}</td>
                <td>{% if t.current > 0 %}${{"%.2f"|format(t.current)}}{% else %}—{% endif %}</td>
                <td class="{{t.color}}">{{"$%.4f"|format(t.pnl) if t.pnl != 0 else "—"}}</td>
                <td class="{{t.color}}">{{"%.0f"|format(t.pct) if t.pct != 0 else "—"}}%</td>
            </tr>
            {% endfor %}
        </table>
    </div>
</body>
</html>
"""


@app.route('/')
def health():
    return 'OK'


@app.route('/dashboard')
def dashboard():
    try:
        trading_bal, saved = get_balance()
        all_trades = db.table('trades').select('*').order('created_at', desc=True).limit(1000).execute()
        trades = all_trades.data or []

        total_open = 0
        deployed = 0.0
        wins = 0
        losses = 0
        rpnl = 0.0

        for t in trades:
            if t['action'] == 'buy' and t.get('pnl') is None:
                total_open += 1
                deployed += sf(t.get('price')) * (t.get('count') or 1)
            elif t['action'] == 'sell':
                p = sf(t.get('pnl'))
                rpnl += p
                if p > 0: wins += 1
                elif p < 0: losses += 1
            elif t['action'] == 'buy' and t.get('pnl') is not None:
                p = sf(t.get('pnl'))
                rpnl += p
                if p > 0: wins += 1
                elif p < 0: losses += 1

        display = []
        for t in trades:
            action = t['action']
            if action not in ('buy', 'sell'):
                continue
            price = sf(t.get('price'))
            pnl_val = sf(t.get('pnl')) if t.get('pnl') is not None else 0
            count = int(t.get('count') or 1)
            current = sf(t.get('current_bid')) or sf(t.get('last_seen_bid'))

            if action == 'sell':
                exit_p = price
                entry = exit_p - (pnl_val / count) if count else exit_p
                pct = sf(t.get('sell_gain_pct')) or ((exit_p - entry) / entry * 100 if entry > 0 else 0)
                color = 'green' if pnl_val > 0 else 'red' if pnl_val < 0 else 'gray'
                display.append({'time': (t.get('created_at') or '')[-8:], 'action': 'SELL',
                    'cls': 'sell', 'ticker': t.get('ticker',''), 'side': t.get('side',''),
                    'count': count, 'entry': entry, 'current': exit_p,
                    'pnl': pnl_val, 'pct': pct, 'color': color})
            elif action == 'buy' and t.get('pnl') is None:
                pnl_val = (current - price) * count if current > 0 else 0
                pct = ((current - price) / price * 100) if price > 0 and current > 0 else 0
                color = 'green' if pnl_val > 0 else 'red' if pnl_val < 0 else 'gray'
                display.append({'time': (t.get('created_at') or '')[-8:], 'action': 'BUY',
                    'cls': 'buy', 'ticker': t.get('ticker',''), 'side': t.get('side',''),
                    'count': count, 'entry': price, 'current': current,
                    'pnl': pnl_val, 'pct': pct, 'color': color})
            elif action == 'buy' and t.get('pnl') is not None:
                pct = (pnl_val / (price * count) * 100) if price > 0 and count > 0 else 0
                color = 'green' if pnl_val > 0 else 'red'
                display.append({'time': (t.get('created_at') or '')[-8:],
                    'action': 'WIN' if pnl_val > 0 else 'LOSS',
                    'cls': 'settled', 'ticker': t.get('ticker',''), 'side': t.get('side',''),
                    'count': count, 'entry': price,
                    'current': 1.0 if pnl_val > 0 else 0.0,
                    'pnl': pnl_val, 'pct': pct, 'color': color})

        return render_template_string(DASHBOARD_HTML,
            balance=f"{trading_bal:.2f}", saved=f"{saved:.2f}",
            rpnl=rpnl, rpnl_fmt=f"{rpnl:.4f}",
            total_open=total_open, deployed=f"{deployed:.2f}",
            wins=wins, losses=losses, trades=display)
    except Exception as e:
        logger.error(f"Dashboard error: {e}")
        return f"Dashboard error: {e}<br><pre>{traceback.format_exc()}</pre>"


# === MAIN ===

def bot_loop():
    logger.info("Bot starting — CRYPTO ONLY — adaptive sell threshold (30% floor)")
    if is_live_enabled():
        logger.info(f"LIVE TRADING ENABLED — max ${LIVE_MAX_PRICE}/contract, {LIVE_MAX_PER_CYCLE}/cycle, ${LIVE_MAX_EXPOSURE} exposure")
    else:
        logger.info("Paper trading only")
    close_all_old_positions()
    while True:
        try:
            run_cycle()
        except Exception as e:
            logger.error(f"Cycle error: {e}")
        time.sleep(CYCLE_SECONDS)


if __name__ == '__main__':
    bot_thread = Thread(target=bot_loop, daemon=True)
    bot_thread.start()
    app.run(host='0.0.0.0', port=PORT)
