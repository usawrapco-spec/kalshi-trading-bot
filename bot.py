import os, time, logging, requests
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

MIN_PRICE = 0.03
MAX_PRICE = 0.15
TAKE_PROFIT_PCT = 35
CYCLE_SECONDS = 60
STARTING_BALANCE = 10.00

WEATHER_SERIES = [
    "KXHIGHNY", "KXHIGHCHI", "KXHIGHMIA", "KXHIGHLAX", "KXHIGHDEN",
    "KXHIGHAUS", "KXHIGHTPHX", "KXHIGHTSFO", "KXHIGHTATL", "KXHIGHPHIL",
    "KXHIGHTDC", "KXHIGHTSEA", "KXHIGHTHOU", "KXHIGHTMIN", "KXHIGHTBOS",
    "KXHIGHTLV", "KXHIGHTOKC",
    "KXLOWTNYC", "KXLOWTCHI", "KXLOWTMIA", "KXLOWTLAX", "KXLOWTDEN",
    "KXLOWTAUS", "KXLOWTPHIL"
]

CRYPTO_SERIES = ["KXBTC15M", "KXETH15M", "KXSOL15M"]

# === INIT ===
db = create_client(SUPABASE_URL, SUPABASE_KEY)
auth = KalshiAuth()
app = Flask(__name__)


def sf(val):
    """Safe float conversion for Supabase string numerics."""
    try:
        return float(val) if val is not None else 0.0
    except:
        return 0.0


# === BALANCE ===

def get_balance():
    """Returns (trading_balance, saved_amount).

    saved = 25% of all positive sell pnl (never reinvested)
    trading = $10 - open_cost + 75% of positive sell pnl + all negative pnl
    """
    open_buys = db.table('trades').select('price,count') \
        .eq('action', 'buy').is_('pnl', 'null').execute()
    open_cost = sum(sf(t['price']) * (t['count'] or 1) for t in (open_buys.data or []))

    # Sells with pnl
    sells = db.table('trades').select('pnl') \
        .eq('action', 'sell').not_.is_('pnl', 'null').execute()
    sell_pnls = [sf(t['pnl']) for t in (sells.data or [])]

    # Settled/expired buys (pnl set by settlement)
    settled = db.table('trades').select('pnl') \
        .eq('action', 'buy').not_.is_('pnl', 'null').execute()
    settled_pnls = [sf(t['pnl']) for t in (settled.data or [])]

    all_pnls = sell_pnls + settled_pnls
    positive_pnl = sum(p for p in all_pnls if p > 0)
    negative_pnl = sum(p for p in all_pnls if p < 0)

    saved = round(0.25 * positive_pnl, 4)
    trading = round(STARTING_BALANCE - open_cost + 0.75 * positive_pnl + negative_pnl, 2)

    return trading, saved


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


def get_series_markets(series_ticker):
    try:
        resp = kalshi_get(f"/markets?series_ticker={series_ticker}&status=open")
        return resp.get('markets', [])
    except Exception as e:
        logger.error(f"get_series_markets({series_ticker}) failed: {e}")
        return []


# === PAPER TRADING ===

def buy(ticker, side, price, strategy, reason):
    """Paper buy 1 contract. Returns True on success."""
    logger.info(f"BUY: {ticker} {side} @ ${price:.2f} | {reason}")
    try:
        db.table('trades').insert({
            'ticker': ticker, 'side': side, 'action': 'buy',
            'price': float(price), 'count': 1,
            'strategy': strategy, 'reason': reason,
        }).execute()
        return True
    except Exception as e:
        logger.error(f"DB insert failed: {e}")
        return False


def sell(trade, current_bid, reason):
    """Paper sell a position. Updates original buy row with P&L."""
    entry_price = sf(trade['price'])
    pnl = round((current_bid - entry_price) * (trade['count'] or 1), 4)

    logger.info(f"SELL: {trade['ticker']} {trade['side']} @ ${current_bid:.2f} | P&L: ${pnl:.4f} | {reason}")
    try:
        db.table('trades').insert({
            'ticker': trade['ticker'], 'side': trade['side'], 'action': 'sell',
            'price': float(current_bid), 'count': trade['count'] or 1,
            'pnl': float(pnl), 'strategy': trade.get('strategy', ''),
            'reason': reason,
        }).execute()
        db.table('trades').update({'pnl': float(pnl)}).eq('id', trade['id']).execute()
        return True
    except Exception as e:
        logger.error(f"DB sell failed: {e}")
        return False


def log_settlement(trade, pnl, label):
    """Record a settled/expired contract result on the original buy."""
    try:
        db.table('trades').update({
            'pnl': float(round(pnl, 4)),
            'reason': f"{trade.get('reason', '')} | {label}",
        }).eq('id', trade['id']).execute()
        logger.info(f"SETTLED: {trade['ticker']} {trade['side']} | {label} | pnl=${pnl:.4f}")
    except Exception as e:
        logger.error(f"Settlement log failed: {e}")


# === POSITION MONITOR ===

def check_positions():
    """Check all open buys — sell winners at 35%+, hold losers, record settlements."""
    open_buys = db.table('trades').select('*') \
        .eq('action', 'buy').is_('pnl', 'null').execute()

    if not open_buys.data:
        return

    for trade in open_buys.data:
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

        # Take profit check
        if side == 'yes':
            current_bid = float(market.get('yes_bid_dollars', '0') or '0')
        else:
            current_bid = float(market.get('no_bid_dollars', '0') or '0')

        if current_bid <= 0:
            continue

        pct = ((current_bid - entry_price) / entry_price) * 100
        if pct >= TAKE_PROFIT_PCT:
            sell(trade, current_bid, f"TAKE PROFIT +{pct:.0f}% ({entry_price:.2f}->{current_bid:.2f})")
        # No stop loss — hold until pump or expiry


# === SCANNER ===

def scan_and_buy():
    """Scan weather + crypto markets, buy 1 of every cheap contract we don't own."""
    trading_bal, _ = get_balance()
    logger.info(f"Paper balance: ${trading_bal:.2f}")

    # Get all open positions to avoid duplicates
    open_positions = db.table('trades').select('ticker,side') \
        .eq('action', 'buy').is_('pnl', 'null').execute()
    owned = {(t['ticker'], t['side']) for t in (open_positions.data or [])}

    bought = 0

    # Weather markets
    for series in WEATHER_SERIES:
        markets = get_series_markets(series)
        for market in markets:
            ticker = market.get('ticker', '')
            if 'KXMVE' in ticker or market.get('status') != 'open':
                continue

            for side, field in [('yes', 'yes_ask_dollars'), ('no', 'no_ask_dollars')]:
                ask = float(market.get(field, '0') or '0')
                if MIN_PRICE <= ask <= MAX_PRICE and (ticker, side) not in owned:
                    if ask > trading_bal:
                        continue
                    if buy(ticker, side, ask, 'weather', f"Weather: {side.upper()} @ ${ask:.2f}"):
                        owned.add((ticker, side))
                        trading_bal -= ask
                        bought += 1

    # Crypto 15-min markets
    for series in CRYPTO_SERIES:
        markets = get_series_markets(series)
        for market in markets:
            ticker = market.get('ticker', '')
            if 'KXMVE' in ticker or market.get('status') != 'open':
                continue

            for side, field in [('yes', 'yes_ask_dollars'), ('no', 'no_ask_dollars')]:
                ask = float(market.get(field, '0') or '0')
                if MIN_PRICE <= ask <= MAX_PRICE and (ticker, side) not in owned:
                    if ask > trading_bal:
                        continue
                    if buy(ticker, side, ask, 'crypto', f"Crypto: {side.upper()} @ ${ask:.2f}"):
                        owned.add((ticker, side))
                        trading_bal -= ask
                        bought += 1

    logger.info(f"Scan complete: bought {bought} contracts, balance now ${trading_bal:.2f}")


# === MAIN CYCLE ===

def run_cycle():
    logger.info("=== CYCLE START ===")
    try:
        check_positions()
    except Exception as e:
        logger.error(f"Position check error: {e}")
    try:
        scan_and_buy()
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
        body { background: #0a0a0f; color: #e8e8e8; font-family: monospace; padding: 20px; max-width: 900px; margin: 0 auto; }
        .panel { background: #1a1a2e; border-radius: 8px; padding: 20px; margin: 10px 0; }
        .green { color: #2ecc71; }
        .red { color: #e74c3c; }
        h1 { color: #e63946; }
        h2 { color: #457b9d; margin: 0 0 10px 0; }
        table { width: 100%; border-collapse: collapse; margin: 10px 0; }
        th, td { padding: 6px 12px; text-align: left; border-bottom: 1px solid #333; }
        th { color: #888; }
        .stat { font-size: 24px; font-weight: bold; }
        .row { display: flex; gap: 20px; flex-wrap: wrap; }
        .col { flex: 1; min-width: 200px; }
    </style>
</head>
<body>
    <h1>KALSHI SCALP BOT (paper)</h1>
    <div class="row">
        <div class="col panel">
            <h2>BALANCE</h2>
            <div>Trading: <span class="stat {{ 'green' if trading_bal >= 10 else 'red' }}">${{ trading_bal }}</span></div>
            <div>Saved: <span class="green">${{ saved }}</span></div>
        </div>
        <div class="col panel">
            <h2>STATS</h2>
            <div>Open: <span class="stat">{{ open_count }}</span></div>
            <div>Wins: <span class="green">{{ wins }}</span> | Losses: <span class="red">{{ losses }}</span> | Win rate: {{ win_rate }}%</div>
            <div>Total P&L: <span class="{{ 'green' if total_pnl >= 0 else 'red' }}">${{ total_pnl }}</span></div>
        </div>
    </div>
    <div class="panel">
        <h2>RECENT TRADES</h2>
        <table>
            <tr><th>Time</th><th>Action</th><th>Ticker</th><th>Side</th><th>Price</th><th>P&L</th><th>Reason</th></tr>
            {% for t in trades %}
            <tr>
                <td>{{ t.time }}</td>
                <td>{{ t.action }}</td>
                <td>{{ t.ticker }}</td>
                <td>{{ t.side }}</td>
                <td>${{ t.price }}</td>
                <td class="{{ 'green' if t.pnl and t.pnl > 0 else 'red' if t.pnl and t.pnl < 0 else '' }}">{{ ("$%.4f"|format(t.pnl)) if t.pnl else "---" }}</td>
                <td>{{ t.reason }}</td>
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

        all_trades = db.table('trades').select('*').order('created_at', desc=True).limit(50).execute()
        trades = all_trades.data or []

        # Closed trades = sells + settled buys (buys with pnl set)
        sells = [t for t in trades if t['action'] == 'sell' and t.get('pnl') is not None]
        settled_buys = [t for t in trades if t['action'] == 'buy' and t.get('pnl') is not None]
        closed = sells + settled_buys

        wins = sum(1 for t in closed if sf(t['pnl']) > 0)
        losses = sum(1 for t in closed if sf(t['pnl']) < 0)
        total_pnl = sum(sf(t['pnl']) for t in closed)
        open_count = sum(1 for t in trades if t['action'] == 'buy' and t.get('pnl') is None)
        win_rate = round(wins / (wins + losses) * 100) if (wins + losses) > 0 else 0

        trades_display = []
        for t in trades[:30]:
            trades_display.append({
                'time': (t.get('created_at') or '')[:19],
                'action': t['action'].upper(),
                'ticker': t['ticker'],
                'side': t['side'],
                'price': f"{sf(t['price']):.2f}",
                'pnl': sf(t['pnl']) if t.get('pnl') is not None else None,
                'reason': (t.get('reason') or '')[:60],
            })

        return render_template_string(DASHBOARD_HTML,
            trading_bal=f"{trading_bal:.2f}",
            saved=f"{saved:.2f}",
            open_count=open_count,
            wins=wins,
            losses=losses,
            win_rate=win_rate,
            total_pnl=f"{total_pnl:.2f}",
            trades=trades_display,
        )
    except Exception as e:
        return f"Dashboard error: {e}"


# === MAIN ===

def bot_loop():
    logger.info("Bot starting — paper trading mode")
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
