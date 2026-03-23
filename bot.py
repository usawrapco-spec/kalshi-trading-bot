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
MAX_PRICE = 0.85
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
    """Resolve old positions. Delete fake records. Run ONCE at startup."""
    try:
        db.table('trades').delete().eq('reason', 'CLOSED — nuclear reset').execute()
        db.table('trades').delete().eq('reason', 'RESOLVED — fresh start').execute()
        db.table('trades').delete().eq('reason', 'RESOLVED — fresh start v2').execute()
        db.table('trades').delete().eq('reason', 'RESOLVED — activity reset').execute()
        result = db.table('trades').update({
            'pnl': 0.0,
            'reason': 'RESOLVED — velocity reset',
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
    """Returns set of TICKER STRINGS (not tuples) — one side per market only."""
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
CRYPTO_15M = {'KXBTC15M', 'KXETH15M', 'KXSOL15M'}


def is_crypto(ticker):
    t = ticker.upper()
    return any(x in t for x in ['BTC', 'ETH', 'SOL', 'KXBTC', 'KXETH', 'KXSOL'])


def fetch_top_activity():
    """Source 1: ONE API call — top 1000 markets, sort by volume, take top 100."""
    try:
        resp = kalshi_get('/markets?status=open&limit=1000')
        markets = resp.get('markets', [])
        markets.sort(key=lambda m: float(m.get('volume_24h', '0') or m.get('volume_24h_fp', '0') or '0'), reverse=True)
        return markets[:100]
    except Exception as e:
        logger.warning(f"Top activity fetch: {e}")
        return []


def fetch_crypto_series():
    """Source 2: 9 API calls — crypto series."""
    markets = []
    for series in CRYPTO_SERIES:
        try:
            resp = kalshi_get(f"/markets?series_ticker={series}&status=open&limit=50")
            markets.extend(resp.get('markets', []))
        except:
            pass
    return markets


# === BUY LOGIC — LIQUIDITY REQUIRED ===

def get_buy_count(ticker):
    """3 contracts for 15-min crypto, 1 for everything else."""
    return 3 if any(s in ticker for s in CRYPTO_15M) else 1


def buy(ticker, side, price, bid, strategy, reason):
    count = get_buy_count(ticker)
    logger.info(f"BUY: {ticker} {side} x{count} @ ${price:.2f} (bid=${bid:.2f}) | {reason}")
    try:
        db.table('trades').insert({
            'ticker': ticker, 'side': side, 'action': 'buy',
            'price': float(price), 'count': count,
            'strategy': strategy, 'reason': reason,
            'last_seen_bid': float(bid),
            'current_bid': float(bid),
        }).execute()
        return True
    except Exception as e:
        logger.error(f"DB insert failed: {e}")
        return False


# === SELL LOGIC — CHECK ALL POSITIONS, STRATEGY THRESHOLDS ===

def kalshi_fee(price):
    return math.ceil(0.07 * price * (1 - price) * 100) / 100


def check_positions():
    """Check ALL positions. Crypto keeps existing thresholds, trending sells at 3%."""
    logger.info("check_positions() called")
    open_buys = db.table('trades').select('*') \
        .eq('action', 'buy').is_('pnl', 'null').execute()

    if not open_buys.data:
        logger.info("No open positions to check")
        return

    checked = 0
    sold = 0
    best_gain = 0
    best_ticker = ''

    for trade in open_buys.data:  # ALL positions, no limit
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

        checked += 1

        # Settlement check
        status = market.get('status', '')
        if status in ('closed', 'settled', 'finalized'):
            result = market.get('result', '')
            if result == side:
                pnl = (1.0 - entry_price) * count
                try:
                    db.table('trades').update({
                        'pnl': float(round(pnl, 4)),
                        'reason': f"{trade.get('reason', '')} | WIN — settled $1.00",
                    }).eq('id', trade['id']).execute()
                    logger.info(f"SETTLED WIN: {ticker} {side} | pnl=${pnl:.4f}")
                except:
                    pass
            elif result:
                pnl = -entry_price * count
                try:
                    db.table('trades').update({
                        'pnl': float(round(pnl, 4)),
                        'reason': f"{trade.get('reason', '')} | LOSS — expired",
                    }).eq('id', trade['id']).execute()
                    logger.info(f"SETTLED LOSS: {ticker} {side} | pnl=${pnl:.4f}")
                except:
                    pass
            continue

        # Get current BID
        if side == 'yes':
            current_bid = float(market.get('yes_bid_dollars', '0') or '0')
        else:
            current_bid = float(market.get('no_bid_dollars', '0') or '0')

        if current_bid <= 0:
            continue

        gain_pct = ((current_bid - entry_price) / entry_price) * 100

        # Update price tracking
        try:
            db.table('trades').update({
                'current_bid': float(current_bid),
                'last_seen_bid': float(current_bid),
            }).eq('id', trade['id']).execute()
        except:
            pass

        # Track best for logging
        if gain_pct > best_gain:
            best_gain = gain_pct
            best_ticker = ticker

        # NEVER sell at a loss
        if gain_pct <= 0 or current_bid <= entry_price:
            continue

        # Strategy-based sell thresholds
        strategy = trade.get('strategy', '')
        should_sell = False
        reason = ""

        if strategy == 'crypto':
            # Keep existing crypto thresholds
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
        else:
            # Trending: low 3% threshold — farm micro-gains
            if gain_pct >= 50:
                should_sell = True
                reason = f"BIG WIN +{gain_pct:.0f}%"
            elif gain_pct >= 3:
                should_sell = True
                reason = f"SCALP +{gain_pct:.0f}%"

        if should_sell:
            # Compute P&L with fees
            buy_fee = kalshi_fee(entry_price)
            sell_fee = kalshi_fee(current_bid)
            pnl = round((current_bid - entry_price - buy_fee - sell_fee) * count, 4)

            # Only sell if net positive after fees
            if pnl <= 0:
                logger.info(f"SKIP: {ticker} {side} +{gain_pct:.0f}% but net=${pnl:.4f} (fees)")
                continue

            logger.info(f"SELL: {ticker} {side} +{gain_pct:.0f}% | ${pnl:.4f} profit ({strategy})")
            try:
                logger.info(f"INSERTING SELL: {ticker} {side} pnl={pnl} gain={gain_pct:.1f}%")
                sell_result = db.table('trades').insert({
                    'ticker': ticker, 'side': side, 'action': 'sell',
                    'price': float(current_bid), 'count': count,
                    'pnl': float(pnl), 'strategy': strategy,
                    'reason': reason, 'sell_gain_pct': float(round(gain_pct, 1)),
                }).execute()
                logger.info(f"SELL SAVED: {len(sell_result.data) if sell_result.data else 0} rows")
                update_result = db.table('trades').update({
                    'pnl': float(pnl),
                    'sell_gain_pct': float(round(gain_pct, 1)),
                }).eq('id', trade['id']).execute()
                logger.info(f"BUY UPDATED: id={trade['id']} pnl={pnl}")
                sold += 1
            except Exception as e:
                logger.error(f"Sell DB error: {e}")
                logger.error(f"Sell DB traceback: {traceback.format_exc()}")

    total = len(open_buys.data)
    if sold:
        logger.info(f"Checked {checked}/{total} | Sold {sold}")
    elif best_ticker:
        logger.info(f"Checked {checked}/{total} | No sells — best: {best_ticker} +{best_gain:.1f}%")
    else:
        logger.info(f"Checked {checked}/{total} | Nothing profitable yet")


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
        crypto_markets = fetch_crypto_series()

        # Combine and deduplicate
        seen = set()
        all_markets = []
        for m in crypto_markets + top_activity:  # Crypto first for dedup priority
            t = m.get('ticker', '')
            if t not in seen:
                seen.add(t)
                all_markets.append(m)

        # Find buy opportunities — LIQUIDITY REQUIRED (bid > 0)
        crypto_buys = []
        trending_buys = []

        for m in all_markets:
            ticker = m.get('ticker', '')
            if 'KXMVE' in ticker:
                continue
            if ticker in owned:
                continue  # Already own a side of this ticker — skip entirely

            yes_bid = float(m.get('yes_bid_dollars', '0') or '0')
            yes_ask = float(m.get('yes_ask_dollars', '0') or '0')
            no_bid = float(m.get('no_bid_dollars', '0') or '0')
            no_ask = float(m.get('no_ask_dollars', '0') or '0')
            vol = float(m.get('volume_24h', '0') or m.get('volume_24h_fp', '0') or '0')
            title = m.get('title', '')
            crypto = is_crypto(ticker)
            strategy = 'crypto' if crypto else 'trending'
            target = crypto_buys if crypto else trending_buys

            # Pick the CHEAPER side only — never buy both sides
            yes_ok = yes_bid > 0 and yes_ask > 0 and MIN_PRICE <= yes_ask <= MAX_PRICE
            no_ok = no_bid > 0 and no_ask > 0 and MIN_PRICE <= no_ask <= MAX_PRICE

            if yes_ok and no_ok:
                # Both sides liquid — buy the cheaper one
                if yes_ask <= no_ask:
                    spread = yes_ask - yes_bid
                    target.append({'ticker': ticker, 'side': 'yes', 'price': yes_ask,
                                   'bid': yes_bid, 'spread': spread,
                                   'strategy': strategy, 'volume': vol,
                                   'reason': f"{strategy}: {title[:35]} YES@${yes_ask:.2f} bid=${yes_bid:.2f}"})
                else:
                    spread = no_ask - no_bid
                    target.append({'ticker': ticker, 'side': 'no', 'price': no_ask,
                                   'bid': no_bid, 'spread': spread,
                                   'strategy': strategy, 'volume': vol,
                                   'reason': f"{strategy}: {title[:35]} NO@${no_ask:.2f} bid=${no_bid:.2f}"})
            elif yes_ok:
                spread = yes_ask - yes_bid
                target.append({'ticker': ticker, 'side': 'yes', 'price': yes_ask,
                               'bid': yes_bid, 'spread': spread,
                               'strategy': strategy, 'volume': vol,
                               'reason': f"{strategy}: {title[:35]} YES@${yes_ask:.2f} bid=${yes_bid:.2f}"})
            elif no_ok:
                spread = no_ask - no_bid
                target.append({'ticker': ticker, 'side': 'no', 'price': no_ask,
                               'bid': no_bid, 'spread': spread,
                               'strategy': strategy, 'volume': vol,
                               'reason': f"{strategy}: {title[:35]} NO@${no_ask:.2f} bid=${no_bid:.2f}"})

        # Sort each by spread (tightest first = easiest scalp)
        crypto_buys.sort(key=lambda x: (x['spread'], -x['volume']))
        trending_buys.sort(key=lambda x: (x['spread'], -x['volume']))

        # Crypto first, then trending
        ordered = crypto_buys + trending_buys

        logger.info(f"Scan: {len(top_activity)} active + {len(crypto_markets)} crypto | owned={len(owned)} | buyable: crypto={len(crypto_buys)} trending={len(trending_buys)}")

        bought = 0
        for signal in ordered:
            if bought >= MAX_BUYS_PER_CYCLE:
                break
            if trading_bal < RESERVE_BALANCE:
                break
            if signal['ticker'] in owned:
                logger.info(f"SKIP: {signal['ticker']} — already own")
                continue
            cost = signal['price'] * get_buy_count(signal['ticker'])
            if cost > trading_bal - RESERVE_BALANCE:
                continue
            if buy(signal['ticker'], signal['side'], signal['price'], signal['bid'],
                   signal['strategy'], signal['reason']):
                owned.add(signal['ticker'])
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
        <h1>KALSHI VELOCITY SCALP BOT</h1>
        <div class="subtitle">Paper Trading — $50 — 30s cycles — Crypto + Trending</div>
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
            <tr><th>Time</th><th>Action</th><th>Ticker</th><th>Side</th><th>Entry</th><th>Bid</th><th>P&L</th><th>%</th><th>Strategy</th></tr>
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
        all_trades = db.table('trades').select('*').order('created_at', desc=True).limit(1000).execute()
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
    logger.info("Bot starting — velocity scalp: crypto + trending, liquidity required")
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
