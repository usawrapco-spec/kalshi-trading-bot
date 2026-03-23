import os, time, logging, math, requests, traceback
from datetime import datetime, timezone
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
MAX_PRICE = 0.50
CYCLE_SECONDS = 30

# === AGGRESSIVE DEPLOYMENT ===
MAX_DEPLOYMENT_PCT = 0.80       # Deploy up to 80% of balance
MIN_CASH_RESERVE_PCT = 0.20     # Keep 20% cash
MAX_CONTRACTS_PER_TRADE = 10
MIN_CONTRACTS_PER_TRADE = 3
MAX_SPEND_PER_TRADE_PCT = 0.10  # Max 10% of balance on single trade
MAX_SPEND_PER_CYCLE = 20        # $ max spend per cycle
MAX_TRADES_PER_CYCLE = 10
MAX_OPEN_POSITIONS = 200

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
    """Get real Kalshi balance via API."""
    try:
        resp = kalshi_get('/portfolio/balance')
        balance_cents = resp.get('balance', 0)
        return float(balance_cents) / 100.0
    except Exception as e:
        logger.error(f"Balance fetch failed: {e}")
        return 0.0


def get_realized_pnl():
    """P&L from sell records ONLY — single source of truth."""
    sells = db.table('trades').select('pnl') \
        .eq('action', 'sell').not_.is_('pnl', 'null').execute()
    return sum(sf(t['pnl']) for t in (sells.data or []))


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


def place_order(ticker, side, action, price, count):
    """Place a real Kalshi order. Returns order_id or None."""
    price_cents = int(round(price * 100))
    try:
        logger.info(f"ORDER: {action.upper()} {ticker} {side} x{count} @ ${price:.2f} ({price_cents}c)")
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
        logger.info(f"ORDER PLACED: {order_id} status={status}")
        return order_id
    except Exception as e:
        logger.error(f"ORDER FAILED: {action.upper()} {ticker} — {e}")
        return None


# === CRYPTO ONLY ===

CRYPTO_SERIES = [
    # BTC first — the proven winners
    'KXBTCD', 'KXBTC', 'KXBTC1H', 'KXBTC15M',
    # ETH — brackets work well
    'KXETHD', 'KXETH', 'KXETH1H', 'KXETH15M',
    # SOL — weakest but keep for diversification
    'KXSOLD', 'KXSOL', 'KXSOL1H', 'KXSOL15M',
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

def calculate_position_size(contract_price, available_balance):
    """Deploy more capital per trade. Target 8% of balance per trade.
    Cheap contracts = more contracts. Floor 3, cap 10."""
    target_spend = available_balance * 0.08
    if contract_price <= 0:
        return MIN_CONTRACTS_PER_TRADE
    max_contracts = int(target_spend / contract_price)
    return max(MIN_CONTRACTS_PER_TRADE, min(max_contracts, MAX_CONTRACTS_PER_TRADE))


def buy_priority(ticker):
    """Lower = buy first. BTC daily is the money maker."""
    if 'KXBTCD' in ticker: return 0
    if ticker.startswith('KXBTC-'): return 1
    if ticker.startswith('KXETH-'): return 2
    if 'KXBTC15M' in ticker: return 3
    if 'KXETHD' in ticker: return 4
    if 'KXETH15M' in ticker: return 5
    if 'KXSOL' in ticker: return 6
    return 7


def run_buys(markets):
    balance = get_balance()
    owned = get_owned()
    num_open = len(owned)
    logger.info(f"Own {num_open} tickers, balance ${balance:.2f}")

    if num_open >= MAX_OPEN_POSITIONS:
        logger.info(f"At max open positions ({MAX_OPEN_POSITIONS}), skipping buys")
        return

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
        count = calculate_position_size(price, balance)

        buys.append({
            'ticker': ticker, 'side': side, 'price': price,
            'bid': bid, 'spread': spread, 'count': count,
        })

    # Sort by priority (BTC daily first) then tightest spread
    buys.sort(key=lambda x: (buy_priority(x['ticker']), x['spread']))

    # Aggressive deployment: 80% of balance, 10% per trade
    max_exposure = balance * MAX_DEPLOYMENT_PCT
    max_per_trade = balance * MAX_SPEND_PER_TRADE_PCT
    reserve = balance * MIN_CASH_RESERVE_PCT

    # Get current deployed
    open_buys = db.table('trades').select('price,count') \
        .eq('action', 'buy').is_('pnl', 'null').execute()
    current_deployed = sum(sf(t['price']) * (t['count'] or 1) for t in (open_buys.data or []))

    bought = 0
    cycle_spent = 0.0
    for b in buys:
        if bought >= MAX_TRADES_PER_CYCLE:
            break
        if cycle_spent >= MAX_SPEND_PER_CYCLE:
            break
        if num_open + bought >= MAX_OPEN_POSITIONS:
            break
        cost = b['price'] * b['count']
        if cost > max_per_trade:
            # Try fewer contracts instead of skipping entirely
            affordable = int(max_per_trade / b['price'])
            if affordable < MIN_CONTRACTS_PER_TRADE:
                continue
            b['count'] = affordable
            cost = b['price'] * b['count']
        if current_deployed + cost > max_exposure:
            continue
        if cost > balance - reserve:
            continue

        # Place real Kalshi order
        order_id = place_order(b['ticker'], b['side'], 'buy', b['price'], b['count'])
        if not order_id:
            continue

        logger.info(f"BUY: {b['ticker']} {b['side']} x{b['count']} @ ${b['price']:.2f} (bid=${b['bid']:.2f} spread=${b['spread']:.2f})")
        try:
            db.table('trades').insert({
                'ticker': b['ticker'], 'side': b['side'], 'action': 'buy',
                'price': float(b['price']), 'count': b['count'],
                'strategy': 'crypto',
                'reason': f"crypto: {b['side'].upper()} @ ${b['price']:.2f} bid=${b['bid']:.2f}",
                'last_seen_bid': float(b['bid']),
                'current_bid': float(b['bid']),
            }).execute()
            owned.add(b['ticker'])
            balance -= cost
            current_deployed += cost
            cycle_spent += cost
            bought += 1
        except Exception as e:
            logger.error(f"Buy DB insert failed: {e}")

    logger.info(f"Bought {bought}, spent ${cycle_spent:.2f}, balance ${balance:.2f}, deployed ${current_deployed:.2f}/{max_exposure:.2f}")


# === SELL LOGIC — ADAPTIVE THRESHOLD, HANDLE SETTLEMENTS ===

sell_history = []  # Rolling last 20 sell gain percentages
peak_bids = {}     # trade_id -> highest bid seen, for trailing stop

TRAILING_STOP_PCT = 0.50  # Sell if price drops to 50% of peak gain
TRAILING_STOP_ACTIVATE = 50  # Only activate trailing stop after 50% gain


def get_time_to_expiry(market):
    """Returns seconds until market closes, or None if unknown."""
    close_time_str = market.get('close_time') or market.get('expiration_time')
    if not close_time_str:
        return None
    try:
        # Kalshi returns ISO 8601 timestamps
        close_time_str = close_time_str.replace('Z', '+00:00')
        close_time = datetime.fromisoformat(close_time_str)
        now = datetime.now(timezone.utc)
        return max(0, (close_time - now).total_seconds())
    except:
        return None


def decide_sell(entry_price, current_bid, count, time_to_expiry, trade_id):
    """Tiered profit-taking + trailing stop + emergency exit.
    Returns (should_sell, sell_qty, reason)."""
    gain_pct = ((current_bid - entry_price) / entry_price) * 100

    # Track peak bid for trailing stop
    prev_peak = peak_bids.get(trade_id, current_bid)
    if current_bid > prev_peak:
        peak_bids[trade_id] = current_bid
    peak = peak_bids.get(trade_id, current_bid)
    peak_gain_pct = ((peak - entry_price) / entry_price) * 100

    # === EMERGENCY EXIT: < 60 seconds to expiry, dump everything ===
    if time_to_expiry is not None and time_to_expiry < 60 and gain_pct > 0:
        return True, count, f"EMERGENCY EXIT <60s, locking +{gain_pct:.0f}%"

    # === EXPIRY APPROACHING: < 120 seconds, sell all if profitable ===
    if time_to_expiry is not None and time_to_expiry < 120 and gain_pct > 0:
        return True, count, f"EXPIRY <2min, locking +{gain_pct:.0f}%"

    # === TRAILING STOP: once up 50%+, sell if dropped to 50% of peak ===
    if peak_gain_pct >= TRAILING_STOP_ACTIVATE and gain_pct > 0:
        trailing_threshold = peak_gain_pct * TRAILING_STOP_PCT
        if gain_pct <= trailing_threshold:
            return True, count, f"TRAILING STOP: peak +{peak_gain_pct:.0f}% -> now +{gain_pct:.0f}%"

    # === NEVER sell at a loss ===
    if gain_pct <= 0 or current_bid <= entry_price:
        return False, 0, None

    # === SINGLE CONTRACT: hold for 150% minimum ===
    if count == 1:
        if gain_pct >= 150:
            return True, 1, f"PROFIT +{gain_pct:.0f}% (single contract, 150% target)"
        return False, 0, None

    # === MULTI-CONTRACT: tiered exits ===
    if gain_pct >= 300:
        return True, count, f"MOONSHOT +{gain_pct:.0f}% — selling all {count}"
    if gain_pct >= 200:
        sell_qty = max(1, count // 3)
        return True, sell_qty, f"TIER 2: +{gain_pct:.0f}% — partial {sell_qty}/{count}"
    if gain_pct >= 100:
        sell_qty = max(1, count // 3)
        return True, sell_qty, f"TIER 1: +{gain_pct:.0f}% — partial {sell_qty}/{count}"

    return False, 0, None


def execute_sell(trade, ticker, side, entry_price, current_bid, sell_qty, total_count, gain_pct, reason):
    """Execute a sell order and update DB. Returns True on success."""
    pnl = round((current_bid - entry_price) * sell_qty, 4)

    sell_order_id = place_order(ticker, side, 'sell', current_bid, sell_qty)
    if not sell_order_id:
        logger.error(f"SELL ORDER FAILED — skipping {ticker}")
        return False

    logger.info(f"SELL: {ticker} {side} x{sell_qty} +{gain_pct:.0f}% pnl=${pnl:.4f} | {reason}")
    try:
        db.table('trades').insert({
            'ticker': ticker, 'side': side, 'action': 'sell',
            'price': float(current_bid), 'count': sell_qty,
            'pnl': float(pnl), 'strategy': 'crypto',
            'reason': reason,
            'sell_gain_pct': float(round(gain_pct, 1)),
        }).execute()
    except Exception as e:
        logger.error(f"SELL INSERT FAILED: {e}")
        logger.error(f"SELL traceback: {traceback.format_exc()}")

    remaining = total_count - sell_qty
    try:
        if remaining <= 0:
            # Fully sold — resolve the buy record
            db.table('trades').update({
                'pnl': 0.0,
                'current_bid': float(current_bid),
                'sell_gain_pct': float(round(gain_pct, 1)),
            }).eq('id', trade['id']).execute()
            # Clean up peak tracking
            peak_bids.pop(trade['id'], None)
            logger.info(f"BUY RESOLVED: id={trade['id']}")
        else:
            # Partial sell — update remaining count on buy record
            db.table('trades').update({
                'count': remaining,
                'current_bid': float(current_bid),
            }).eq('id', trade['id']).execute()
            logger.info(f"PARTIAL SELL: {sell_qty} sold, {remaining} remaining for id={trade['id']}")
    except Exception as e:
        logger.error(f"BUY UPDATE FAILED: {e}")

    return True


def check_sells():
    """Tiered exits, trailing stop, emergency dump. Never sell at a loss."""
    global sell_history
    logger.info("check_sells() called — tiered exits + trailing stop")

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
                db.table('trades').insert({
                    'ticker': ticker, 'side': side, 'action': 'sell',
                    'price': float(settle_price), 'count': count,
                    'pnl': float(pnl), 'strategy': 'crypto',
                    'reason': reason,
                    'sell_gain_pct': float(round(((settle_price - entry_price) / entry_price) * 100, 1)),
                }).execute()
            except Exception as e:
                logger.error(f"SETTLE INSERT FAILED: {e}")

            try:
                db.table('trades').update({
                    'pnl': 0.0,
                    'current_bid': float(settle_price),
                    'reason': f"{trade.get('reason', '')} | {reason}",
                }).eq('id', trade['id']).execute()
            except:
                pass
            peak_bids.pop(trade.get('id'), None)
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

        # === DECIDE SELL ===
        time_to_expiry = get_time_to_expiry(market)
        should_sell, sell_qty, reason = decide_sell(
            entry_price, current_bid, count, time_to_expiry, trade.get('id')
        )

        if should_sell and sell_qty > 0:
            success = execute_sell(
                trade, ticker, side, entry_price, current_bid,
                sell_qty, count, gain_pct, reason
            )
            if success:
                sold += 1
                sell_history.append(gain_pct)
                if len(sell_history) > 20:
                    sell_history = sell_history[-20:]

    avg_win = (sum(sell_history) / len(sell_history)) if sell_history else 0
    logger.info(f"Checked {len(open_buys.data)} positions | sold={sold} settled={settled} | avg_win={avg_win:.0f}%")


# === MAIN CYCLE ===

def run_cycle():
    balance = get_balance()
    logger.info(f"=== CYCLE START === Balance: ${balance:.2f}")

    try:
        check_sells()
    except Exception as e:
        logger.error(f"Sell check error: {e}")

    try:
        markets = fetch_all_crypto()
        run_buys(markets)
    except Exception as e:
        logger.error(f"Buy error: {e}")

    balance = get_balance()
    logger.info(f"=== CYCLE END === Balance: ${balance:.2f}")


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
        <div class="subtitle">LIVE Trading — 30s cycles — BTC/ETH/SOL — Tiered exits + trailing stop</div>
    </div>
    <div class="stats">
        <div class="stat-box"><div class="stat-label">Balance</div><div class="stat-value green">${{balance}}</div></div>
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
        balance = get_balance()
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
                # Sell records are the ONLY source of truth for P&L
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
            # Resolved buys (pnl set) — skip, sell record shows the result

        return render_template_string(DASHBOARD_HTML,
            balance=f"{balance:.2f}",
            rpnl=rpnl, rpnl_fmt=f"{rpnl:.4f}",
            total_open=total_open, deployed=f"{deployed:.2f}",
            wins=wins, losses=losses, trades=display)
    except Exception as e:
        logger.error(f"Dashboard error: {e}")
        return f"Dashboard error: {e}<br><pre>{traceback.format_exc()}</pre>"


# === MAIN ===

def bot_loop():
    logger.info("Bot starting — LIVE TRADING — crypto only — tiered exits + trailing stop")
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
