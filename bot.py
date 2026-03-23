import os, time, logging, json, requests
from datetime import datetime, timezone
from flask import Flask, jsonify, render_template_string
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

# Strategy settings
MIN_PRICE = 0.03        # Only buy 3c and above
MAX_PRICE = 0.20        # Only buy 20c and below
TAKE_PROFIT_PCT = 50    # Sell when up 50%
STOP_LOSS_PCT = -50     # Sell when down 50%
MAX_LIVE_SPEND = 3.00   # Max $3 per cycle on live trades
MAX_POSITIONS = 30      # Max open positions total
CYCLE_SECONDS = 60      # Check every 60 seconds
PAPER_STARTING_BALANCE = 10.00

# Weather series to scan
WEATHER_SERIES = [
    "KXHIGHNY", "KXHIGHCHI", "KXHIGHMIA", "KXHIGHLAX", "KXHIGHDEN",
    "KXHIGHAUS", "KXHIGHTPHX", "KXHIGHTSFO", "KXHIGHTATL", "KXHIGHPHIL",
    "KXHIGHTDC", "KXHIGHTSEA", "KXHIGHTHOU", "KXHIGHTMIN", "KXHIGHTBOS",
    "KXHIGHTLV", "KXHIGHTOKC",
    "KXLOWTNYC", "KXLOWTCHI", "KXLOWTMIA", "KXLOWTLAX", "KXLOWTDEN",
    "KXLOWTAUS", "KXLOWTPHIL"
]

# NWS station coordinates (for GFS ensemble forecasts)
STATIONS = {
    "KXHIGHNY":    (40.7789, -73.9692),
    "KXHIGHCHI":   (41.7868, -87.7522),
    "KXHIGHMIA":   (25.7959, -80.2870),
    "KXHIGHLAX":   (33.9425, -118.4081),
    "KXHIGHDEN":   (39.8561, -104.6737),
    "KXHIGHAUS":   (30.1944, -97.6700),
    "KXHIGHTPHX":  (33.4373, -112.0078),
    "KXHIGHTSFO":  (37.6213, -122.3790),
    "KXHIGHTATL":  (33.6407, -84.4277),
    "KXHIGHPHIL":  (39.8744, -75.2424),
    "KXHIGHTDC":   (38.8512, -77.0402),
    "KXHIGHTSEA":  (47.4502, -122.3088),
    "KXHIGHTHOU":  (29.6454, -95.2789),
    "KXHIGHTMIN":  (44.8848, -93.2223),
    "KXHIGHTBOS":  (42.3656, -71.0096),
    "KXHIGHTLV":   (36.0840, -115.1537),
    "KXHIGHTOKC":  (35.3931, -97.6007),
    "KXLOWTNYC":   (40.7789, -73.9692),
    "KXLOWTCHI":   (41.7868, -87.7522),
    "KXLOWTMIA":   (25.7959, -80.2870),
    "KXLOWTLAX":   (33.9425, -118.4081),
    "KXLOWTDEN":   (39.8561, -104.6737),
    "KXLOWTAUS":   (30.1944, -97.6700),
    "KXLOWTPHIL":  (39.8744, -75.2424),
}

# === INIT ===
db = create_client(SUPABASE_URL, SUPABASE_KEY)
auth = KalshiAuth()
app = Flask(__name__)


def sf(val):
    """Safe float conversion for Supabase string numerics"""
    try:
        return float(val) if val is not None else 0
    except:
        return 0

def get_paper_balance():
    """Paper balance = $10 - cost of open buys + P&L from sells"""
    open_buys = db.table('trades').select('price,count') \
        .eq('action', 'buy').eq('is_live', False).is_('pnl', 'null').execute()
    open_cost = sum(sf(t['price']) * t['count'] for t in (open_buys.data or []))

    sells = db.table('trades').select('pnl') \
        .eq('action', 'sell').eq('is_live', False).not_.is_('pnl', 'null').execute()
    realized_pnl = sum(sf(t['pnl']) for t in (sells.data or []))

    return round(PAPER_STARTING_BALANCE - open_cost + realized_pnl, 2)

def recently_stop_lossed(ticker, hours=2):
    """Check if we stop-lossed this ticker recently"""
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    recent = db.table('trades').select('id') \
        .eq('ticker', ticker).eq('action', 'sell') \
        .like('reason', '%STOP LOSS%') \
        .gte('created_at', cutoff) \
        .limit(1).execute()
    return bool(recent.data)


# ============================================================
# KALSHI API HELPERS
# ============================================================

def kalshi_get(path):
    """GET request to Kalshi API with auth"""
    url = f"{KALSHI_HOST}/trade-api/v2{path}"
    headers = auth.get_headers("GET", f"/trade-api/v2{path}")
    resp = requests.get(url, headers=headers, timeout=10)
    resp.raise_for_status()
    return resp.json()

def kalshi_post(path, data):
    """POST request to Kalshi API with auth"""
    url = f"{KALSHI_HOST}/trade-api/v2{path}"
    headers = auth.get_headers("POST", f"/trade-api/v2{path}")
    headers["Content-Type"] = "application/json"
    logger.info(f"KALSHI POST: {path} | PAYLOAD: {json.dumps(data)}")
    resp = requests.post(url, headers=headers, json=data, timeout=10)
    if resp.status_code != 200:
        logger.error(f"KALSHI ERROR {resp.status_code}: {resp.text}")
    resp.raise_for_status()
    return resp.json()

def kalshi_delete(path):
    """DELETE request to Kalshi API with auth"""
    url = f"{KALSHI_HOST}/trade-api/v2{path}"
    headers = auth.get_headers("DELETE", f"/trade-api/v2{path}")
    resp = requests.delete(url, headers=headers, timeout=10)
    resp.raise_for_status()
    return resp.json()

def get_balance():
    """Get Kalshi balance in dollars"""
    try:
        resp = kalshi_get("/portfolio/balance")
        return resp.get('balance', 0) / 100
    except:
        return 0

def get_market(ticker):
    """Get single market data"""
    try:
        resp = kalshi_get(f"/markets/{ticker}")
        return resp.get('market', resp)
    except:
        return None

def get_series_markets(series_ticker):
    """Get all open markets for a series"""
    try:
        resp = kalshi_get(f"/markets?series_ticker={series_ticker}&status=open")
        return resp.get('markets', [])
    except:
        return []


# ============================================================
# BUY ORDER
# ============================================================

def buy(ticker, side, count, price_dollars, is_live, strategy, reason):
    """Buy contracts. Returns order_id or 'paper'."""
    price_cents = int(price_dollars * 100)
    cost = price_dollars * count

    if is_live:
        try:
            order = {
                "ticker": ticker,
                "action": "buy",
                "side": side,
                "count": count,
                "type": "limit",
            }
            if side == "yes":
                order["yes_price"] = price_cents
            else:
                order["no_price"] = price_cents

            resp = kalshi_post("/portfolio/orders", order)
            order_id = resp.get('order', {}).get('order_id', 'live-unknown')
            logger.info(f"LIVE BUY: {ticker} {side} x{count} @ {price_cents}c = ${cost:.2f}")
        except Exception as e:
            logger.error(f"Live buy failed: {ticker} -- {e}")
            order_id = 'paper'
            is_live = False
    else:
        order_id = 'paper'
        logger.info(f"PAPER BUY: {ticker} {side} x{count} @ {price_cents}c = ${cost:.2f}")

    # Log to Supabase
    try:
        db.table('trades').insert({
            'ticker': ticker, 'side': side, 'action': 'buy',
            'price': float(price_dollars), 'count': count, 'cost': float(cost),
            'is_live': is_live, 'order_id': order_id,
            'strategy': strategy, 'reason': reason,
        }).execute()
    except Exception as e:
        logger.error(f"DB log failed: {e}")

    return order_id


# ============================================================
# SELL ORDER
# ============================================================

def sell(trade, current_bid_dollars, reason):
    """Sell a position. Updates the original buy record with P&L."""
    ticker = trade['ticker']
    side = trade['side']
    count = trade['count']
    entry_price = float(trade['price'])
    is_live = trade['is_live']
    bid_cents = int(current_bid_dollars * 100)
    pnl = (current_bid_dollars - entry_price) * count

    if bid_cents < 1:
        logger.warning(f"Bid is 0 for {ticker}, can't sell")
        return False

    if is_live:
        try:
            order = {
                "ticker": ticker,
                "action": "sell",
                "side": side,
                "count": count,
                "type": "limit",
            }
            if side == "yes":
                order["yes_price"] = bid_cents
            else:
                order["no_price"] = bid_cents

            logger.info(f"LIVE SELL: {ticker} {side} x{count} @ {bid_cents}c | P&L: ${pnl:.2f} | {reason}")
            resp = kalshi_post("/portfolio/orders", order)
            sell_order_id = resp.get('order', {}).get('order_id', 'live-sell')
        except Exception as e:
            logger.error(f"Live sell FAILED: {ticker} -- {e}")
            return False
    else:
        logger.info(f"PAPER SELL: {ticker} {side} x{count} @ {bid_cents}c | P&L: ${pnl:.2f} | {reason}")
        sell_order_id = 'paper'

    # Log sell and update original buy with P&L
    try:
        db.table('trades').insert({
            'ticker': ticker, 'side': side, 'action': 'sell',
            'price': float(current_bid_dollars), 'count': count,
            'cost': float(current_bid_dollars * count),
            'is_live': is_live, 'order_id': sell_order_id,
            'strategy': trade.get('strategy', ''), 'reason': reason,
            'pnl': float(round(pnl, 4)),
        }).execute()

        db.table('trades').update({
            'pnl': float(round(pnl, 4)),
        }).eq('id', trade['id']).execute()
    except Exception as e:
        logger.error(f"DB sell log failed: {e}")

    return True


# ============================================================
# POSITION MONITOR
# ============================================================

def check_positions():
    """Check all open buys, sell winners and losers"""
    open_buys = db.table('trades').select('*') \
        .eq('action', 'buy') \
        .is_('pnl', 'null') \
        .execute()

    if not open_buys.data:
        return

    for trade in open_buys.data:
        ticker = trade['ticker']
        side = trade['side']
        entry_price = float(trade['price'])

        if entry_price <= 0:
            continue

        market = get_market(ticker)
        if not market:
            continue

        # Check if market settled
        status = market.get('status', '')
        if status in ['closed', 'settled', 'finalized']:
            result = market.get('result', '')
            if result == side:
                pnl = (1.0 - entry_price) * trade['count']
                sell(trade, 1.0, f"SETTLED WIN +${pnl:.2f}")
            elif result:
                sell(trade, 0.0, f"SETTLED LOSS -${entry_price * trade['count']:.2f}")
            continue

        # Get current bid (what we could sell for)
        if side == 'yes':
            current_bid = float(market.get('yes_bid_dollars', '0') or '0')
        else:
            current_bid = float(market.get('no_bid_dollars', '0') or '0')

        if current_bid <= 0:
            continue

        # Calculate gain/loss percentage
        pct = ((current_bid - entry_price) / entry_price) * 100

        if pct >= TAKE_PROFIT_PCT:
            sell(trade, current_bid, f"TAKE PROFIT +{pct:.0f}% ({entry_price:.2f}->{current_bid:.2f})")
        elif pct <= STOP_LOSS_PCT:
            sell(trade, current_bid, f"STOP LOSS {pct:.0f}% ({entry_price:.2f}->{current_bid:.2f})")


# ============================================================
# STRATEGY 1: WEATHER EDGE (live trades)
# ============================================================

def get_gfs_ensemble(lat, lon, is_high=True):
    """Get GFS ensemble temperature forecast"""
    try:
        variable = "temperature_2m_max" if is_high else "temperature_2m_min"
        url = "https://ensemble-api.open-meteo.com/v1/ensemble"
        params = {
            "latitude": lat, "longitude": lon,
            "daily": variable,
            "models": "gfs_seamless",
            "forecast_days": 3,
            "temperature_unit": "fahrenheit",
        }
        resp = requests.get(url, params=params, timeout=10)
        data = resp.json()

        results = {}
        daily = data.get('daily', {})
        dates = daily.get('time', [])

        for i, date in enumerate(dates):
            members = []
            for key in daily:
                if variable in key and key != variable:
                    vals = daily[key]
                    if i < len(vals) and vals[i] is not None:
                        members.append(vals[i])
            if members:
                results[date] = members

        return results
    except Exception as e:
        logger.error(f"GFS fetch failed: {e}")
        return {}

def weather_edge_strategy():
    """Find mispriced weather markets using GFS ensemble"""
    signals = []

    for series in WEATHER_SERIES:
        markets = get_series_markets(series)
        if not markets:
            continue

        coords = STATIONS.get(series)
        if not coords:
            continue

        is_high = "HIGH" in series
        ensemble_data = get_gfs_ensemble(coords[0], coords[1], is_high)
        if not ensemble_data:
            continue

        for market in markets:
            ticker = market.get('ticker', '')
            if 'KXMVE' in ticker:
                continue

            yes_ask = float(market.get('yes_ask_dollars', '0') or '0')
            no_ask = float(market.get('no_ask_dollars', '0') or '0')

            # Only cheap contracts
            if yes_ask < MIN_PRICE and no_ask < MIN_PRICE:
                continue
            if yes_ask > MAX_PRICE and no_ask > MAX_PRICE:
                continue

            # Extract threshold from ticker
            # Format: KXHIGHCHI-26MAR23-B44.5 or T44.5
            try:
                parts = ticker.split('-')
                bracket = parts[-1]  # e.g. "B44.5" or "T76"
                threshold = float(bracket[1:])
                is_above = bracket.startswith('B')
            except:
                continue

            # Match to ensemble date
            for date_str, members in ensemble_data.items():
                if not members:
                    continue

                if is_high:
                    if is_above:
                        prob = sum(1 for m in members if m >= threshold) / len(members)
                    else:
                        prob = sum(1 for m in members if m <= threshold) / len(members)
                else:
                    if is_above:
                        prob = sum(1 for m in members if m >= threshold) / len(members)
                    else:
                        prob = sum(1 for m in members if m <= threshold) / len(members)

                # Determine trade side and edge
                if prob > 0.6 and yes_ask >= MIN_PRICE and yes_ask <= MAX_PRICE:
                    edge = prob - yes_ask
                    if edge >= 0.20:
                        signals.append({
                            'ticker': ticker, 'side': 'yes', 'price': yes_ask,
                            'edge': edge, 'prob': prob,
                            'reason': f"GFS {prob:.0%} vs market {yes_ask:.0%}, edge {edge:.0%}"
                        })
                elif prob < 0.4 and no_ask >= MIN_PRICE and no_ask <= MAX_PRICE:
                    edge = (1 - prob) - no_ask
                    if edge >= 0.20:
                        signals.append({
                            'ticker': ticker, 'side': 'no', 'price': no_ask,
                            'edge': edge, 'prob': 1 - prob,
                            'reason': f"GFS {1-prob:.0%} NO vs market {no_ask:.0%}, edge {edge:.0%}"
                        })
                break  # Only use first matching date

    return signals


# ============================================================
# STRATEGY 2: VOLATILITY SCALP (paper first)
# ============================================================

def scalp_strategy():
    """Buy cheap contracts on volatile markets, sell the pump"""
    signals = []

    try:
        resp = kalshi_get("/markets?status=open&limit=200")
        markets = resp.get('markets', [])
    except:
        return signals

    for market in markets:
        ticker = market.get('ticker', '')
        if 'KXMVE' in ticker:
            continue

        yes_ask = float(market.get('yes_ask_dollars', '0') or '0')
        no_ask = float(market.get('no_ask_dollars', '0') or '0')
        volume = float(market.get('volume_24h_fp', '0') or '0')

        # Only volatile markets with volume
        if volume < 50:
            continue

        if MIN_PRICE <= yes_ask <= MAX_PRICE:
            signals.append({
                'ticker': ticker, 'side': 'yes', 'price': yes_ask,
                'reason': f"Scalp: YES @ {yes_ask:.2f}, vol={volume:.0f}"
            })

        if MIN_PRICE <= no_ask <= MAX_PRICE:
            signals.append({
                'ticker': ticker, 'side': 'no', 'price': no_ask,
                'reason': f"Scalp: NO @ {no_ask:.2f}, vol={volume:.0f}"
            })

    return signals


# ============================================================
# MAIN BOT CYCLE
# ============================================================

def run_cycle():
    """One complete bot cycle"""
    balance = get_balance()
    logger.info(f"=== CYCLE START === Balance: ${balance:.2f}")

    open_count_resp = db.table('trades').select('id').eq('action', 'buy').is_('pnl', 'null').execute()
    open_count = len(open_count_resp.data) if open_count_resp.data else 0

    # 1. CHECK POSITIONS FIRST (sell winners/losers)
    try:
        check_positions()
    except Exception as e:
        logger.error(f"Position check error: {e}")

    # 2. WEATHER EDGE (live trades)
    if open_count < MAX_POSITIONS:
        try:
            weather_signals = weather_edge_strategy()
            live_spent = 0
            available = balance * 0.75  # Keep 25% reserve
            for signal in weather_signals:
                if live_spent >= MAX_LIVE_SPEND:
                    break
                if open_count >= MAX_POSITIONS:
                    break

                if recently_stop_lossed(signal['ticker']):
                    logger.info(f"  Skip {signal['ticker']}: recently stop-lossed, cooling down")
                    continue

                existing = db.table('trades').select('id') \
                    .eq('ticker', signal['ticker']).eq('side', signal['side']) \
                    .eq('action', 'buy').is_('pnl', 'null').execute()
                if existing.data:
                    continue

                count = max(1, int(1.0 / signal['price']))
                count = min(count, 10)
                cost = signal['price'] * count

                # Check if we can afford it
                if cost > available:
                    count = max(1, int(available / signal['price']))
                    cost = signal['price'] * count
                if cost > available or count < 1:
                    logger.info(f"  Skip {signal['ticker']}: cost ${cost:.2f} > available ${available:.2f}")
                    continue

                buy(signal['ticker'], signal['side'], count, signal['price'],
                    is_live=True, strategy='weather_edge', reason=signal['reason'])
                live_spent += cost
                available -= cost
                open_count += 1
                logger.info(f"  Weather: {signal['ticker']} {signal['side']} x{count} @ {signal['price']:.2f} | {signal['reason']}")
        except Exception as e:
            logger.error(f"Weather strategy error: {e}")

    # 3. SCALP STRATEGY (paper for now)
    if open_count < MAX_POSITIONS:
        try:
            scalp_signals = scalp_strategy()
            paper_count = 0
            paper_bal = get_paper_balance()
            for signal in scalp_signals[:10]:
                if open_count >= MAX_POSITIONS:
                    break
                if paper_count >= 5:
                    break

                if recently_stop_lossed(signal['ticker']):
                    logger.info(f"  Skip {signal['ticker']}: recently stop-lossed, cooling down")
                    continue

                existing = db.table('trades').select('id') \
                    .eq('ticker', signal['ticker']).eq('side', signal['side']) \
                    .eq('action', 'buy').is_('pnl', 'null').execute()
                if existing.data:
                    continue

                count = max(1, int(0.50 / signal['price']))
                count = min(count, 5)
                cost = signal['price'] * count

                # Check paper balance
                if cost > paper_bal * 0.20:
                    count = max(1, int((paper_bal * 0.20) / signal['price']))
                    cost = signal['price'] * count
                if cost > paper_bal or count < 1:
                    logger.info(f"  Skip {signal['ticker']}: paper balance too low (${paper_bal:.2f})")
                    continue

                buy(signal['ticker'], signal['side'], count, signal['price'],
                    is_live=False, strategy='scalp', reason=signal['reason'])
                paper_bal -= cost
                paper_count += 1
                open_count += 1
        except Exception as e:
            logger.error(f"Scalp strategy error: {e}")

    logger.info(f"=== CYCLE END === Open positions: {open_count}")


# ============================================================
# FLASK DASHBOARD
# ============================================================

DASHBOARD_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Kalshi Bot</title>
    <meta http-equiv="refresh" content="30">
    <style>
        body { background: #0a0a0f; color: #e8e8e8; font-family: monospace; padding: 20px; }
        .panel { background: #1a1a2e; border-radius: 8px; padding: 20px; margin: 10px 0; }
        .live { border-left: 4px solid #e63946; }
        .paper { border-left: 4px solid #457b9d; }
        .green { color: #2ecc71; }
        .red { color: #e74c3c; }
        .yellow { color: #f1c40f; }
        h1 { color: #e63946; }
        h2 { color: #457b9d; margin: 0 0 10px 0; }
        table { width: 100%; border-collapse: collapse; margin: 10px 0; }
        th, td { padding: 6px 12px; text-align: left; border-bottom: 1px solid #333; }
        th { color: #888; }
        .stat { font-size: 24px; font-weight: bold; }
        .row { display: flex; gap: 20px; }
        .col { flex: 1; }
    </style>
</head>
<body>
    <h1>KALSHI SCALP BOT</h1>
    <div class="row">
        <div class="col panel live">
            <h2>LIVE TRADING</h2>
            <div>Balance: <span class="stat green">${{live_balance}}</span></div>
            <div>Open: {{live_open}} | Wins: <span class="green">{{live_wins}}</span> | Losses: <span class="red">{{live_losses}}</span></div>
            <div>P&L: <span class="{{'green' if live_pnl|float >= 0 else 'red'}}">${{live_pnl}}</span></div>
        </div>
        <div class="col panel paper">
            <h2>PAPER TRADING</h2>
            <div>Balance: <span class="stat {{'green' if paper_balance|float >= 10 else 'red'}}">${{paper_balance}}</span> <span style="color:#888">(started at $10)</span></div>
            <div>Open: {{paper_open}} | Wins: <span class="green">{{paper_wins}}</span> | Losses: <span class="red">{{paper_losses}}</span></div>
            <div>P&L: <span class="{{'green' if paper_pnl|float >= 0 else 'red'}}">${{paper_pnl}}</span> <span style="color:#888">({{paper_roi}}%)</span></div>
        </div>
    </div>
    <div class="panel">
        <h2>RECENT TRADES</h2>
        <table>
            <tr><th>Time</th><th>Type</th><th>Ticker</th><th>Side</th><th>Price</th><th>Qty</th><th>P&L</th><th>Reason</th></tr>
            {% for t in trades %}
            <tr>
                <td>{{t.created_at[:19]}}</td>
                <td>{{'LIVE' if t.is_live else 'PAPER'}} {{t.action}}</td>
                <td>{{t.ticker}}</td>
                <td>{{t.side}}</td>
                <td>${{"%.2f"|format(t.price)}}</td>
                <td>{{t.count}}</td>
                <td class="{{'green' if (t.pnl or 0) > 0 else 'red' if (t.pnl or 0) < 0 else ''}}">{{"$%.2f"|format(t.pnl) if t.pnl else "---"}}</td>
                <td>{{(t.reason or '')[:60]}}</td>
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
        all_trades = db.table('trades').select('*').order('created_at', desc=True).limit(100).execute()
        trades = all_trades.data or []

        live_trades = [t for t in trades if t.get('is_live')]
        paper_trades = [t for t in trades if not t.get('is_live')]

        live_sells = [t for t in live_trades if t['action'] == 'sell' and sf(t.get('pnl')) != 0]
        paper_sells = [t for t in paper_trades if t['action'] == 'sell' and sf(t.get('pnl')) != 0]

        live_pnl_total = sum(sf(t['pnl']) for t in live_sells)
        paper_pnl_total = sum(sf(t['pnl']) for t in paper_sells)
        paper_roi = (paper_pnl_total / PAPER_STARTING_BALANCE) * 100

        # Convert numeric fields for Jinja template
        trades_display = []
        for t in trades[:30]:
            t['pnl'] = sf(t.get('pnl')) if t.get('pnl') is not None else None
            t['price'] = sf(t.get('price'))
            t['cost'] = sf(t.get('cost'))
            trades_display.append(t)

        return render_template_string(DASHBOARD_HTML,
            live_balance=f"{get_balance():.2f}",
            live_open=sum(1 for t in live_trades if t['action'] == 'buy' and t.get('pnl') is None),
            live_wins=sum(1 for t in live_sells if sf(t['pnl']) > 0),
            live_losses=sum(1 for t in live_sells if sf(t['pnl']) <= 0),
            live_pnl=f"{live_pnl_total:.2f}",
            paper_balance=f"{get_paper_balance():.2f}",
            paper_open=sum(1 for t in paper_trades if t['action'] == 'buy' and t.get('pnl') is None),
            paper_wins=sum(1 for t in paper_sells if sf(t['pnl']) > 0),
            paper_losses=sum(1 for t in paper_sells if sf(t['pnl']) <= 0),
            paper_pnl=f"{paper_pnl_total:.2f}",
            paper_roi=f"{paper_roi:+.1f}",
            trades=trades_display,
        )
    except Exception as e:
        return f"Dashboard error: {e}"

@app.route('/api/status')
def api_status():
    return jsonify({"status": "running", "balance": get_balance()})


# ============================================================
# MAIN
# ============================================================

def bot_loop():
    """Main bot loop"""
    logger.info("Bot starting...")

    # Cancel any stale resting orders
    try:
        resting = kalshi_get("/portfolio/orders?status=resting")
        for order in resting.get('orders', []):
            oid = order.get('order_id')
            if oid:
                kalshi_delete(f"/portfolio/orders/{oid}")
                logger.info(f"Cancelled stale order: {oid}")
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
