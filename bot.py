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
MAX_PRICE = 0.60
MAX_BUYS_PER_CYCLE = 50
CYCLE_SECONDS = 30
STARTING_BALANCE = 50.00
RESERVE_BALANCE = 3.00

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
    """Resolve all open positions. Delete fake records. Run ONCE at startup."""
    try:
        db.table('trades').delete().eq('reason', 'CLOSED — nuclear reset').execute()
        db.table('trades').delete().eq('reason', 'RESOLVED — fresh start').execute()
        db.table('trades').delete().eq('reason', 'RESOLVED — fresh start v2').execute()
        result = db.table('trades').update({
            'pnl': 0.0,
            'reason': 'RESOLVED — activity reset',
        }).eq('action', 'buy').is_('pnl', 'null').execute()
        closed = len(result.data) if result.data else 0
        logger.info(f"Resolved {closed} old positions — fresh $50 start")
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
    result = db.table('trades').select('ticker,side') \
        .eq('action', 'buy').is_('pnl', 'null').execute()
    return {(t['ticker'], t['side']) for t in (result.data or [])}


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


# === TWO DATA SOURCES ===

CRYPTO_SERIES = [
    'KXBTC15M', 'KXETH15M', 'KXSOL15M',
    'KXBTC1H', 'KXETH1H', 'KXSOL1H',
    'KXBTCD', 'KXETHD', 'KXSOLD',
]


def categorize(ticker):
    t = ticker.upper()
    if any(x in t for x in ['BTC', 'ETH', 'SOL']):
        return 'crypto'
    return 'trending'


def fetch_top_activity():
    """Source 1: ONE API call — top 1000 markets, sort by volume, take top 100."""
    try:
        resp = kalshi_get('/markets?status=open&limit=1000')
        markets = resp.get('markets', [])
        markets.sort(key=lambda m: float(m.get('volume_24h', '0') or m.get('volume_24h_fp', '0') or '0'), reverse=True)
        top = markets[:100]
        logger.info(f"Top activity: {len(markets)} fetched, top 100 selected")
        return top
    except Exception as e:
        logger.warning(f"Top activity fetch: {e}")
        return []


def fetch_series_markets():
    """Source 2: 6 API calls — crypto series only."""
    markets = []
    for series in CRYPTO_SERIES:
        try:
            resp = kalshi_get(f"/markets?series_ticker={series}&status=open&limit=50")
            series_markets = resp.get('markets', [])
            markets.extend(series_markets)
        except:
            pass
    logger.info(f"Crypto: {len(markets)} markets from {len(CRYPTO_SERIES)} series")
    return markets


# === PAPER TRADING ===

def kalshi_fee(price):
    return math.ceil(0.07 * price * (1 - price) * 100) / 100


def buy(ticker, side, price, strategy, reason):
    logger.info(f"BUY: {ticker} {side} x1 @ ${price:.2f} | {reason}")
    try:
        db.table('trades').insert({
            'ticker': ticker, 'side': side, 'action': 'buy',
            'price': float(price), 'count': 1,
            'strategy': strategy, 'reason': reason,
            'last_seen_bid': float(price),
        }).execute()
        return True
    except Exception as e:
        logger.error(f"DB insert failed: {e}")
        return False


def sell(trade, current_bid, reason):
    entry_price = sf(trade['price'])
    count = trade['count'] or 1
    buy_fee = kalshi_fee(entry_price)
    sell_fee = kalshi_fee(current_bid)
    pnl = round((current_bid - entry_price - buy_fee - sell_fee) * count, 4)
    gain_pct = round(((current_bid - entry_price) / entry_price) * 100, 2) if entry_price > 0 else 0

    logger.info(f"SELL: {trade['ticker']} {trade['side']} @ ${current_bid:.2f} | fees ${buy_fee:.2f}+${sell_fee:.2f} | P&L ${pnl:.4f} ({gain_pct:+.0f}%) | {reason}")
    try:
        db.table('trades').insert({
            'ticker': trade['ticker'], 'side': trade['side'], 'action': 'sell',
            'price': float(current_bid), 'count': count,
            'pnl': float(pnl), 'strategy': trade.get('strategy', ''),
            'reason': reason, 'sell_gain_pct': float(gain_pct),
        }).execute()
        db.table('trades').update({'pnl': float(pnl)}).eq('id', trade['id']).execute()
        return True
    except Exception as e:
        logger.error(f"DB sell failed: {e}")
        return False


def log_settlement(trade, pnl, label):
    try:
        db.table('trades').update({
            'pnl': float(round(pnl, 4)),
            'reason': f"{trade.get('reason', '')} | {label}",
        }).eq('id', trade['id']).execute()
        logger.info(f"SETTLED: {trade['ticker']} {trade['side']} | {label} | pnl=${pnl:.4f}")
    except Exception as e:
        logger.error(f"Settlement log failed: {e}")


# === POSITION MONITOR — PROFIT ONLY ===

def check_positions():
    """Check positions. Sell ONLY for profit. Never sell at a loss. Max 30 per cycle."""
    open_buys = db.table('trades').select('*') \
        .eq('action', 'buy').is_('pnl', 'null').execute()

    if not open_buys.data:
        return

    total = len(open_buys.data)
    checking = min(total, 30)
    logger.info(f"Checking {checking}/{total} positions for profit...")

    for trade in open_buys.data[:30]:
        ticker = trade['ticker']
        side = trade['side']
        entry_price = sf(trade['price'])
        if entry_price <= 0:
            continue

        market = get_market(ticker)
        if not market:
            continue

        # Settlement check
        status = market.get('status', '')
        if status in ('closed', 'settled', 'finalized'):
            result = market.get('result', '')
            if result == side:
                pnl = (1.0 - entry_price) * (trade['count'] or 1)
                log_settlement(trade, pnl, "WIN — settled $1.00")
            elif result:
                pnl = -entry_price * (trade['count'] or 1)
                log_settlement(trade, pnl, "LOSS — expired worthless")
            continue

        # Get current BID (what we'd receive if selling)
        if side == 'yes':
            current_bid = float(market.get('yes_bid_dollars', '0') or '0')
        else:
            current_bid = float(market.get('no_bid_dollars', '0') or '0')

        if current_bid <= 0:
            continue

        gain_pct = ((current_bid - entry_price) / entry_price) * 100
        count = trade['count'] or 1
        last_seen = sf(trade.get('last_seen_bid'))

        # Update price tracking
        db.table('trades').update({
            'prev_bid': float(last_seen) if last_seen > 0 else None,
            'last_seen_bid': float(current_bid),
            'current_bid': float(current_bid),
        }).eq('id', trade['id']).execute()

        # Net P&L after fees
        buy_fee = kalshi_fee(entry_price)
        sell_fee = kalshi_fee(current_bid)
        net_pnl = (current_bid - entry_price - buy_fee - sell_fee) * count

        # === SELL RULES — PROFIT ONLY, TIERED BY ENTRY PRICE ===
        if gain_pct > 0 and net_pnl > 0:
            should_sell = False
            reason = ""

            if gain_pct >= 50:
                should_sell = True
                reason = f"BIG WIN +{gain_pct:.0f}%"
            elif entry_price <= 0.10 and gain_pct >= 20:
                should_sell = True
                reason = f"SCALP +{gain_pct:.0f}%"
            elif entry_price <= 0.20 and gain_pct >= 15:
                should_sell = True
                reason = f"PROFIT +{gain_pct:.0f}%"
            elif gain_pct >= 12:
                should_sell = True
                reason = f"SWING +{gain_pct:.0f}%"

            if should_sell:
                sell(trade, current_bid, f"{reason} net=${net_pnl:.4f}")
        elif gain_pct > 0 and net_pnl <= 0:
            logger.info(f"SKIP: {ticker} {side} +{gain_pct:.0f}% but net=${net_pnl:.4f} (fees eat profit)")
        # Never sell at a loss. Hold forever.


# === MAIN CYCLE ===

def run_cycle():
    trading_bal, _ = get_balance()
    logger.info(f"=== CYCLE START === Balance: ${trading_bal:.2f}")

    try:
        check_positions()
    except Exception as e:
        logger.error(f"Position check error: {e}")

    trading_bal, _ = get_balance()
    owned = get_owned()
    logger.info(f"Own {len(owned)} positions, balance ${trading_bal:.2f}")

    try:
        # Two sources
        top_activity = fetch_top_activity()
        series_markets = fetch_series_markets()

        # Combine and deduplicate by ticker
        seen_tickers = set()
        all_markets = []
        for m in top_activity + series_markets:
            ticker = m.get('ticker', '')
            if ticker not in seen_tickers:
                seen_tickers.add(ticker)
                all_markets.append(m)

        # Find buy opportunities
        buys = []
        for m in all_markets:
            ticker = m.get('ticker', '')
            if 'KXMVE' in ticker:
                continue

            yes = float(m.get('yes_ask_dollars', '0') or '0')
            no = float(m.get('no_ask_dollars', '0') or '0')
            vol = float(m.get('volume_24h', '0') or m.get('volume_24h_fp', '0') or '0')
            title = m.get('title', '')
            strategy = categorize(ticker)

            if MIN_PRICE <= yes <= MAX_PRICE and (ticker, 'yes') not in owned:
                buys.append({'ticker': ticker, 'side': 'yes', 'price': yes,
                             'strategy': strategy, 'volume': vol,
                             'reason': f"{strategy}: {title[:40]} YES@${yes:.2f} vol={vol:.0f}"})
            if MIN_PRICE <= no <= MAX_PRICE and (ticker, 'no') not in owned:
                buys.append({'ticker': ticker, 'side': 'no', 'price': no,
                             'strategy': strategy, 'volume': vol,
                             'reason': f"{strategy}: {title[:40]} NO@${no:.2f} vol={vol:.0f}"})

        # Crypto first (all-in), then trending (only high volume)
        crypto_buys = sorted([b for b in buys if b['strategy'] == 'crypto'], key=lambda x: x['volume'], reverse=True)
        trending_buys = sorted([b for b in buys if b['strategy'] == 'trending' and b['volume'] >= 1000], key=lambda x: x['volume'], reverse=True)
        ordered_buys = crypto_buys + trending_buys

        logger.info(f"Scan: {len(top_activity)} active + {len(series_markets)} crypto | crypto={len(crypto_buys)} trending={len(trending_buys)} (vol>1000)")

        # Buy 1 contract each
        bought = 0
        for signal in ordered_buys:
            if bought >= MAX_BUYS_PER_CYCLE:
                break
            if trading_bal < RESERVE_BALANCE:
                break
            cost = signal['price']  # 1 contract
            if cost > trading_bal - RESERVE_BALANCE:
                continue
            if buy(signal['ticker'], signal['side'], signal['price'],
                   signal['strategy'], signal['reason']):
                owned.add((signal['ticker'], signal['side']))
                trading_bal -= cost
                bought += 1
        logger.info(f"Bought {bought}, balance ${trading_bal:.2f}")
    except Exception as e:
        logger.error(f"Scan error: {e}")

    logger.info("=== CYCLE END ===")


# === DASHBOARD ===

DASHBOARD_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Kalshi Scalp Bot</title>
    <meta http-equiv="refresh" content="30">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { background: #0a0a0f; color: #e0e0e0; font-family: 'Courier New', monospace; padding: 20px; }
        .header { text-align: center; margin-bottom: 20px; }
        .header h1 { color: #00ff88; font-size: 24px; }
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
        .section h2 { color: #00ff88; font-size: 14px; margin-bottom: 10px; text-transform: uppercase; }
        table { width: 100%; border-collapse: collapse; font-size: 12px; }
        th { color: #666; text-align: left; padding: 6px 8px; border-bottom: 1px solid #333; text-transform: uppercase; font-size: 10px; }
        td { padding: 6px 8px; border-bottom: 1px solid #1a1a2e; }
        tr:hover { background: #1a1a2e; }
        .badge { padding: 2px 8px; border-radius: 4px; font-size: 10px; font-weight: bold; }
        .badge-buy { background: #003300; color: #00ff88; }
        .badge-sell { background: #330000; color: #ff4444; }
        .badge-settled { background: #222; color: #888; }
        .badge-crypto { background: #332200; color: #ffaa00; }
        .badge-trending { background: #333300; color: #ffff44; }
        .badge-unknown { background: #222; color: #888; }
    </style>
</head>
<body>
    <div class="header">
        <h1>KALSHI SCALP BOT</h1>
        <div class="subtitle">Paper Trading — $50 — 30s cycles — Trending + Crypto</div>
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
        <h2>Strategy Breakdown</h2>
        <table>
            <tr><th>Strategy</th><th>Open</th><th>Sells</th><th>W</th><th>L</th><th>P&L</th></tr>
            {% for name, s in strats.items() %}
            <tr>
                <td><span class="badge badge-{{name}}">{{name}}</span></td>
                <td>{{s.open}}</td><td>{{s.sells}}</td>
                <td class="green">{{s.wins}}</td><td class="red">{{s.losses}}</td>
                <td class="{{'green' if s.pnl >= 0 else 'red'}}">${{"%.4f"|format(s.pnl)}}</td>
            </tr>
            {% endfor %}
        </table>
    </div>
    <div class="section">
        <h2>Positions & Trades</h2>
        <table>
            <tr><th>Time</th><th>Action</th><th>Ticker</th><th>Side</th><th>Entry</th><th>Now</th><th>P&L</th><th>%</th><th>Strategy</th></tr>
            {% for t in trades %}
            <tr>
                <td>{{t.time}}</td>
                <td><span class="badge badge-{{t.cls}}">{{t.action}}</span></td>
                <td style="font-size:10px">{{t.ticker}}</td>
                <td>{{t.side}}</td>
                <td>${{"%.2f"|format(t.entry)}}</td>
                <td>{% if t.current > 0 %}${{"%.2f"|format(t.current)}}{% else %}—{% endif %}</td>
                <td class="{{t.color}}">{{"$%.4f"|format(t.pnl) if t.pnl != 0 else "—"}}</td>
                <td class="{{t.color}}">{{"%.0f"|format(t.pct) if t.pct != 0 else "—"}}%</td>
                <td><span class="badge badge-{{t.strategy}}">{{t.strategy}}</span></td>
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
        all_trades = db.table('trades').select('*').order('created_at', desc=True).limit(300).execute()
        trades = all_trades.data or []

        strats = {}
        deployed = 0.0
        for t in trades:
            s = t.get('strategy') or 'unknown'
            if s not in strats:
                strats[s] = {'open': 0, 'sells': 0, 'wins': 0, 'losses': 0, 'pnl': 0.0}
            if t['action'] == 'buy' and t.get('pnl') is None:
                strats[s]['open'] += 1
                deployed += sf(t.get('price')) * (t.get('count') or 1)
            elif t['action'] == 'sell':
                strats[s]['sells'] += 1
                p = sf(t.get('pnl'))
                strats[s]['pnl'] += p
                if p > 0: strats[s]['wins'] += 1
                elif p < 0: strats[s]['losses'] += 1
            elif t['action'] == 'buy' and t.get('pnl') is not None:
                p = sf(t.get('pnl'))
                strats[s]['pnl'] += p
                if p > 0: strats[s]['wins'] += 1
                elif p < 0: strats[s]['losses'] += 1

        total_open = sum(s['open'] for s in strats.values())
        wins = sum(s['wins'] for s in strats.values())
        losses = sum(s['losses'] for s in strats.values())
        rpnl = sum(s['pnl'] for s in strats.values())

        display = []
        for t in trades[:50]:
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
                    'entry': entry, 'current': exit_p, 'pnl': pnl_val, 'pct': pct,
                    'color': color, 'strategy': t.get('strategy') or 'unknown'})
            elif action == 'buy' and t.get('pnl') is None:
                pnl_val = (current - price) * count if current > 0 else 0
                pct = ((current - price) / price * 100) if price > 0 and current > 0 else 0
                color = 'green' if pnl_val > 0 else 'red' if pnl_val < 0 else 'gray'
                display.append({'time': (t.get('created_at') or '')[-8:], 'action': 'BUY',
                    'cls': 'buy', 'ticker': t.get('ticker',''), 'side': t.get('side',''),
                    'entry': price, 'current': current, 'pnl': pnl_val, 'pct': pct,
                    'color': color, 'strategy': t.get('strategy') or 'unknown'})
            elif action == 'buy' and t.get('pnl') is not None:
                pct = (pnl_val / (price * count) * 100) if price > 0 and count > 0 else 0
                color = 'green' if pnl_val > 0 else 'red'
                display.append({'time': (t.get('created_at') or '')[-8:],
                    'action': 'WIN' if pnl_val > 0 else 'LOSS',
                    'cls': 'settled', 'ticker': t.get('ticker',''), 'side': t.get('side',''),
                    'entry': price, 'current': 1.0 if pnl_val > 0 else 0.0,
                    'pnl': pnl_val, 'pct': pct, 'color': color,
                    'strategy': t.get('strategy') or 'unknown'})

        return render_template_string(DASHBOARD_HTML,
            balance=f"{trading_bal:.2f}", saved=f"{saved:.2f}",
            rpnl=rpnl, rpnl_fmt=f"{rpnl:.4f}",
            total_open=total_open, deployed=f"{deployed:.2f}",
            wins=wins, losses=losses,
            strats=strats, trades=display)
    except Exception as e:
        logger.error(f"Dashboard error: {e}")
        return f"Dashboard error: {e}<br><pre>{traceback.format_exc()}</pre>"


# === MAIN ===

def bot_loop():
    logger.info("Bot starting — trending + crypto, 1 contract per position, max spread")
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
