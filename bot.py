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

MIN_PRICE = 0.03
MAX_PRICE = 0.15
TAKE_PROFIT_PCT = 30
BUY_COUNT = 2
MAX_BUYS_PER_CYCLE = 50
CYCLE_SECONDS = 30
STARTING_BALANCE = 10.00

# === INIT ===
db = create_client(SUPABASE_URL, SUPABASE_KEY)
auth = KalshiAuth()
app = Flask(__name__)


def sf(val):
    try:
        return float(val) if val is not None else 0.0
    except:
        return 0.0


# === BALANCE ===

def get_balance():
    """Returns (trading_balance, saved_amount)."""
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

CRYPTO_SERIES = ["KXBTC15M", "KXETH15M", "KXSOL15M"]
WEATHER_SERIES = [
    "KXHIGHNY", "KXHIGHCHI", "KXHIGHMIA", "KXHIGHLAX", "KXHIGHDEN",
    "KXHIGHAUS", "KXHIGHTPHX", "KXHIGHTSFO", "KXHIGHTATL", "KXHIGHPHIL",
    "KXHIGHTDC", "KXHIGHTSEA", "KXHIGHTHOU", "KXHIGHTMIN", "KXHIGHTBOS",
    "KXHIGHTLV", "KXHIGHTOKC",
    "KXLOWTNYC", "KXLOWTCHI", "KXLOWTMIA", "KXLOWTLAX", "KXLOWTDEN",
    "KXLOWTAUS", "KXLOWTPHIL"
]
TRENDING_SERIES = [
    "KXNCAAMB", "KXNBA", "KXNHL", "KXMLB",
    "KXPRES", "KXTRUMPPARDONS", "KXSCOTUS", "KXSCOURT",
    "KXNEXTUKPM", "KXPRESPERSON", "KXPERFORMBONDSONG",
    "KXROASTSUBJECT", "KXBOND", "KXTERMLIMITS",
    "POWER", "EUEXIT", "KXAGICO", "KXGTAPRICE",
    "KXFULLTERMSKPRES", "KXDEBTGROWTH", "KXWITHDRAW",
    "SENATECT", "KXBRUVSEAT", "KXNEWSCOTUSCONF",
    "KXALBERTAREFYES", "KXTRILLIONAIRE",
]


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
        resp = kalshi_get(f"/markets?series_ticker={series_ticker}&status=open&limit=100")
        return resp.get('markets', [])
    except Exception as e:
        logger.error(f"get_series_markets({series_ticker}) failed: {e}")
        return []


# === SCAN ALL MARKETS ===

def categorize(ticker):
    t = ticker.upper()
    if 'KXBTC' in t or 'KXETH' in t or 'KXSOL' in t:
        return 'crypto'
    if 'KXHIGH' in t or 'KXLOWT' in t:
        return 'weather'
    if any(x in t for x in ['NCAA', 'NBA', 'NFL', 'NHL', 'MLB', 'SPORT']):
        return 'sports'
    if any(x in t for x in ['TRUMP', 'BIDEN', 'SENATE', 'HOUSE', 'ELECT', 'PRES', 'GOV']):
        return 'politics'
    return 'trending'


def scan_all_markets():
    """Fetch ALL open markets from Kalshi, return cheap ones sorted by volume."""
    all_markets = []
    cursor = None

    for page in range(10):
        url = '/markets?status=open&limit=200'
        if cursor:
            url += f'&cursor={cursor}'
        try:
            resp = kalshi_get(url)
            markets = resp.get('markets', [])
            cursor = resp.get('cursor', None)
            all_markets.extend(markets)
            if not cursor or not markets:
                break
        except Exception as e:
            logger.error(f"Market fetch page {page}: {e}")
            break

    logger.info(f"Fetched {len(all_markets)} total markets from generic endpoint")

    # Log first 3 markets raw prices to diagnose field names
    for i, m in enumerate(all_markets[:3]):
        logger.info(f"  Generic market[{i}]: {m.get('ticker','?')} yes_ask_dollars={m.get('yes_ask_dollars', 'MISSING')!r} no_ask_dollars={m.get('no_ask_dollars', 'MISSING')!r}")

    cheap = []
    for m in all_markets:
        ticker = m.get('ticker', '')
        if 'KXMVE' in ticker:
            continue

        yes_ask = float(m.get('yes_ask_dollars', '0') or '0')
        no_ask = float(m.get('no_ask_dollars', '0') or '0')
        volume = float(m.get('volume_24h_fp', '0') or '0')
        title = m.get('title', '')
        strat = categorize(ticker)

        if MIN_PRICE <= yes_ask <= MAX_PRICE:
            cheap.append({
                'ticker': ticker, 'side': 'yes', 'price': yes_ask,
                'volume': volume, 'strategy': strat,
                'reason': f"{strat}: {title[:40]} YES @ ${yes_ask:.2f}",
            })
        if MIN_PRICE <= no_ask <= MAX_PRICE:
            cheap.append({
                'ticker': ticker, 'side': 'no', 'price': no_ask,
                'volume': volume, 'strategy': strat,
                'reason': f"{strat}: {title[:40]} NO @ ${no_ask:.2f}",
            })

    cheap.sort(key=lambda x: x['volume'], reverse=True)
    if cheap:
        logger.info(f"  Generic scan: first 3 cheap: {[(c['ticker'], c['side'], c['price']) for c in cheap[:3]]}")

    counts = {}
    for c in cheap:
        counts[c['strategy']] = counts.get(c['strategy'], 0) + 1
    breakdown = ', '.join(f"{k}: {v}" for k, v in sorted(counts.items()))
    logger.info(f"Generic scan: {len(cheap)} cheap ({breakdown})")
    return cheap


def _scan_series(series_list, strategy):
    """Scan a list of series tickers and return cheap contracts."""
    cheap = []
    for series in series_list:
        markets = get_series_markets(series)
        for m in markets:
            ticker = m.get('ticker', '')
            if 'KXMVE' in ticker:
                continue
            yes_ask = float(m.get('yes_ask_dollars', '0') or '0')
            no_ask = float(m.get('no_ask_dollars', '0') or '0')
            if MIN_PRICE <= yes_ask <= MAX_PRICE:
                cheap.append({'ticker': ticker, 'side': 'yes', 'price': yes_ask,
                              'volume': 0, 'strategy': strategy,
                              'reason': f"{strategy}: YES @ ${yes_ask:.2f}"})
            if MIN_PRICE <= no_ask <= MAX_PRICE:
                cheap.append({'ticker': ticker, 'side': 'no', 'price': no_ask,
                              'volume': 0, 'strategy': strategy,
                              'reason': f"{strategy}: NO @ ${no_ask:.2f}"})
    return cheap


def scan_series_markets():
    """Fetch crypto + weather + trending series individually."""
    crypto = _scan_series(CRYPTO_SERIES, 'crypto')
    weather = _scan_series(WEATHER_SERIES, 'weather')
    trending = _scan_series(TRENDING_SERIES, 'trending')
    cheap = crypto + weather + trending
    logger.info(f"Series scan: {len(cheap)} cheap (crypto:{len(crypto)} weather:{len(weather)} trending:{len(trending)})")
    return cheap


# === PAPER TRADING ===

def buy(ticker, side, price, strategy, reason):
    cost = price * BUY_COUNT
    logger.info(f"BUY: {ticker} {side} x{BUY_COUNT} @ ${price:.2f} = ${cost:.2f} | {reason}")
    try:
        db.table('trades').insert({
            'ticker': ticker, 'side': side, 'action': 'buy',
            'price': float(price), 'count': BUY_COUNT,
            'strategy': strategy, 'reason': reason,
            'last_seen_bid': float(price),
        }).execute()
        return True
    except Exception as e:
        logger.error(f"DB insert failed: {e}")
        return False


def kalshi_fee(price):
    """Taker fee per contract: ceil(0.07 * price * (1-price) * 100) / 100"""
    return math.ceil(0.07 * price * (1 - price) * 100) / 100


def sell(trade, current_bid, reason):
    entry_price = sf(trade['price'])
    count = trade['count'] or 1
    buy_fee = kalshi_fee(entry_price)
    sell_fee = kalshi_fee(current_bid)
    pnl = round((current_bid - entry_price - buy_fee - sell_fee) * count, 4)

    logger.info(f"SELL: {trade['ticker']} {trade['side']} @ ${current_bid:.2f} | fees: ${buy_fee:.2f}+${sell_fee:.2f} | P&L: ${pnl:.4f} | {reason}")
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
    """Check open buys — sell at 30%+ when momentum fades, record settlements."""
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

        # Get current bid
        if side == 'yes':
            current_bid = float(market.get('yes_bid_dollars', '0') or '0')
        else:
            current_bid = float(market.get('no_bid_dollars', '0') or '0')

        if current_bid <= 0:
            continue

        pct = ((current_bid - entry_price) / entry_price) * 100
        last_seen = sf(trade.get('last_seen_bid'))

        # Update price tracking: prev_bid = old last_seen, then update both
        db.table('trades').update({
            'prev_bid': float(last_seen) if last_seen > 0 else None,
            'last_seen_bid': float(current_bid),
            'current_bid': float(current_bid),
        }).eq('id', trade['id']).execute()

        if pct >= TAKE_PROFIT_PCT:
            if current_bid > last_seen and last_seen > 0:
                logger.info(f"HOLD: {ticker} {side} +{pct:.0f}% — still climbing ({last_seen:.2f}->{current_bid:.2f})")
            else:
                sell(trade, current_bid, f"TAKE PROFIT +{pct:.0f}% peaked ({entry_price:.2f}->{current_bid:.2f})")
        # No stop loss — hold until pump or expiry


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
        # Combine both scans: series (proven working) + generic (more markets)
        series_cheap = scan_series_markets()
        generic_cheap = scan_all_markets()

        # Merge: series first, then generic (dedup by ticker+side)
        seen = set()
        cheap = []
        for s in series_cheap:
            key = (s['ticker'], s['side'])
            if key not in seen:
                seen.add(key)
                cheap.append(s)
        for s in generic_cheap:
            key = (s['ticker'], s['side'])
            if key not in seen:
                seen.add(key)
                cheap.append(s)

        logger.info(f"Combined: {len(cheap)} unique cheap contracts")

        bought = 0
        for signal in cheap:
            cost = signal['price'] * BUY_COUNT
            if cost > trading_bal:
                continue
            if bought >= MAX_BUYS_PER_CYCLE:
                break
            if (signal['ticker'], signal['side']) in owned:
                continue
            if buy(signal['ticker'], signal['side'], signal['price'],
                   signal['strategy'], signal['reason']):
                owned.add((signal['ticker'], signal['side']))
                trading_bal -= cost
                bought += 1
        logger.info(f"Bought {bought} contracts, balance now ${trading_bal:.2f}")
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
        .stats { display: flex; gap: 15px; margin-bottom: 20px; flex-wrap: wrap; }
        .stat-box { background: #12121a; border: 1px solid #222; border-radius: 8px; padding: 15px; flex: 1; min-width: 120px; text-align: center; }
        .stat-label { color: #666; font-size: 11px; text-transform: uppercase; }
        .stat-value { font-size: 22px; font-weight: bold; margin-top: 4px; }
        .green { color: #00ff88; }
        .red { color: #ff4444; }
        .yellow { color: #ffaa00; }
        .gray { color: #666; }
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
        .badge-weather { background: #002233; color: #44aaff; }
        .badge-crypto { background: #332200; color: #ffaa00; }
        .badge-sports { background: #220033; color: #aa44ff; }
        .badge-politics { background: #003322; color: #44ffaa; }
        .badge-trending { background: #333300; color: #ffff44; }
    </style>
</head>
<body>
    <div class="header">
        <h1>KALSHI SCALP BOT</h1>
        <div class="subtitle">Paper Trading — Started $10.00 — 30s cycles — All markets</div>
    </div>

    <div class="stats">
        <div class="stat-box">
            <div class="stat-label">Trading Balance</div>
            <div class="stat-value green">${{balance}}</div>
        </div>
        <div class="stat-box">
            <div class="stat-label">Saved (25%)</div>
            <div class="stat-value yellow">${{saved}}</div>
        </div>
        <div class="stat-box">
            <div class="stat-label">Total P&L</div>
            <div class="stat-value {{'green' if total_pnl_positive else 'red'}}">${{total_pnl}}</div>
        </div>
        <div class="stat-box">
            <div class="stat-label">Open Positions</div>
            <div class="stat-value">{{total_open}}</div>
        </div>
        <div class="stat-box">
            <div class="stat-label">Win Rate</div>
            <div class="stat-value">{{win_rate}}%</div>
        </div>
        <div class="stat-box">
            <div class="stat-label">Record</div>
            <div class="stat-value"><span class="green">{{total_wins}}W</span> / <span class="red">{{total_losses}}L</span></div>
        </div>
    </div>

    <div class="section">
        <h2>Strategy Breakdown</h2>
        <table>
            <tr><th>Strategy</th><th>Open</th><th>Sold</th><th>Wins</th><th>Losses</th><th>Win Rate</th><th>P&L</th></tr>
            {% for name, s in strats.items() %}
            <tr>
                <td><span class="badge badge-{{name}}">{{name}}</span></td>
                <td>{{s.open}}</td>
                <td>{{s.sells}}</td>
                <td class="green">{{s.wins}}</td>
                <td class="red">{{s.losses}}</td>
                <td>{{"%d"|format(s.wins / (s.wins + s.losses) * 100) if (s.wins + s.losses) > 0 else "—"}}%</td>
                <td class="{{'green' if s.pnl >= 0 else 'red'}}">${{"%.4f"|format(s.pnl)}}</td>
            </tr>
            {% endfor %}
        </table>
    </div>

    <div class="section">
        <h2>Positions & Trades</h2>
        <table>
            <tr><th>Time</th><th>Action</th><th>Ticker</th><th>Side</th><th>Entry</th><th>Current</th><th>Gain</th><th>Gain%</th><th>Chg</th><th>Strategy</th></tr>
            {% for t in trades %}
            <tr>
                <td>{{t.time}}</td>
                <td><span class="badge badge-{{t.action_class}}">{{t.action}}</span></td>
                <td style="font-size:10px">{{t.ticker}}</td>
                <td>{{t.side}}</td>
                <td>${{"%.2f"|format(t.entry)}}</td>
                <td>{% if t.current > 0 %}${{"%.2f"|format(t.current)}}{% else %}—{% endif %}</td>
                <td class="{{'green' if t.gain > 0 else 'red' if t.gain < 0 else 'gray'}}">
                    {% if t.gain != 0 %}{{"$%.4f"|format(t.gain)}}{% else %}—{% endif %}
                </td>
                <td class="{{'green' if t.gain_pct > 0 else 'red' if t.gain_pct < 0 else 'gray'}}">
                    {% if t.gain_pct != 0 %}{{"%.0f"|format(t.gain_pct)}}%{% else %}—{% endif %}
                </td>
                <td>
                    {% if t.change > 0 %}<span class="green">+{{"%.0f"|format(t.change * 100)}}¢</span>
                    {% elif t.change < 0 %}<span class="red">{{"%.0f"|format(t.change * 100)}}¢</span>
                    {% else %}—{% endif %}
                </td>
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

        all_trades = db.table('trades').select('*').order('created_at', desc=True).limit(500).execute()
        trades = all_trades.data or []

        # Strategy breakdown from ALL trades
        strats = {}
        for t in trades:
            s = t.get('strategy') or 'unknown'
            if s not in strats:
                strats[s] = {'open': 0, 'sells': 0, 'wins': 0, 'losses': 0, 'pnl': 0.0}
            if t['action'] == 'buy' and t.get('pnl') is None:
                strats[s]['open'] += 1
            if t['action'] == 'sell':
                strats[s]['sells'] += 1
                p = sf(t.get('pnl'))
                strats[s]['pnl'] += p
                if p > 0:
                    strats[s]['wins'] += 1
                elif p < 0:
                    strats[s]['losses'] += 1
            # Count settled buys too
            if t['action'] == 'buy' and t.get('pnl') is not None:
                p = sf(t.get('pnl'))
                strats[s]['pnl'] += p
                if p > 0:
                    strats[s]['wins'] += 1
                elif p < 0:
                    strats[s]['losses'] += 1

        total_open = sum(s['open'] for s in strats.values())
        total_wins = sum(s['wins'] for s in strats.values())
        total_losses = sum(s['losses'] for s in strats.values())
        total_pnl = sum(s['pnl'] for s in strats.values())

        # Build display trades
        display_trades = []
        for t in trades[:50]:
            action = t['action']
            price = sf(t.get('price'))
            pnl = sf(t.get('pnl')) if t.get('pnl') is not None else 0
            count = int(t.get('count') or 1)
            current = sf(t.get('current_bid')) or sf(t.get('last_seen_bid'))
            prev = sf(t.get('prev_bid'))

            if action == 'sell':
                # Sell: price=exit, derive entry from pnl
                exit_price = price
                entry = exit_price - (pnl / count) if count else exit_price
                gain = pnl
                gain_pct = ((exit_price - entry) / entry * 100) if entry > 0 else 0
                change = 0
                action_class = 'sell'
                display_action = 'SELL'
            elif action == 'buy' and t.get('pnl') is None:
                # Open position
                entry = price
                gain = (current - entry) * count if current > 0 else 0
                gain_pct = ((current - entry) / entry * 100) if entry > 0 and current > 0 else 0
                change = current - prev if current > 0 and prev > 0 else 0
                action_class = 'buy'
                display_action = 'BUY'
            elif action == 'buy' and t.get('pnl') is not None:
                # Settled
                entry = price
                gain = pnl
                gain_pct = (pnl / (entry * count) * 100) if entry > 0 and count > 0 else 0
                current = 1.0 if pnl > 0 else 0.0
                change = 0
                action_class = 'settled'
                display_action = 'WIN' if pnl > 0 else 'LOSS'
            else:
                continue

            display_trades.append({
                'time': (t.get('created_at') or '')[-8:],  # just HH:MM:SS
                'action': display_action,
                'action_class': action_class,
                'ticker': t.get('ticker', ''),
                'side': t.get('side', ''),
                'entry': entry,
                'current': current,
                'gain': gain,
                'gain_pct': gain_pct,
                'change': change,
                'strategy': t.get('strategy') or 'unknown',
            })

        return render_template_string(DASHBOARD_HTML,
            balance=f"{trading_bal:.2f}",
            saved=f"{saved:.2f}",
            total_pnl=f"{total_pnl:.4f}",
            total_pnl_positive=total_pnl >= 0,
            total_open=total_open,
            total_wins=total_wins,
            total_losses=total_losses,
            win_rate=f"{(total_wins/(total_wins+total_losses)*100):.0f}" if (total_wins + total_losses) > 0 else "—",
            strats=strats,
            trades=display_trades,
        )
    except Exception as e:
        logger.error(f"Dashboard error: {e}")
        return f"Dashboard error: {e}<br><pre>{traceback.format_exc()}</pre>"


# === MAIN ===

def bot_loop():
    logger.info("Bot starting — paper trading, 30s cycles, scan ALL markets")
    try:
        db.table('trades').delete().eq('strategy', 'test').execute()
        logger.info("Cleaned up test trades")
    except:
        pass
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
