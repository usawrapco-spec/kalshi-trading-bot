"""
Buy cheap crypto contracts, sell when they pop.
$0.03-$0.15, sell at +50%, expiry save >0% @ 1min.
No caps, no filters — just price and bid.
"""

import os, time, logging, re, requests, traceback
from datetime import datetime, timezone
from flask import Flask, render_template_string, jsonify
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
ENABLE_TRADING = os.environ.get('ENABLE_TRADING', 'false').lower() == 'true'

# === SETTINGS ===
BUY_MIN = 0.03
BUY_MAX = 0.15
CYCLE_SECONDS = 10
MAX_CONTRACTS_PER_TRADE = 3
PROFIT_BANK_PCT = 0.20
PAPER_STARTING_BALANCE = 20.00
PAPER_RESET_TIME = '2026-03-23T21:00:00Z'

# === SERIES TO SCAN (crypto hourly brackets only — 19W/0L) ===
ALL_SERIES = ['KXBTCD', 'KXETHD', 'KXBTC', 'KXETH', 'KXSOLD']

# === HARD BLOCKS ===
BLOCKED_PATTERNS = ['KXMVE', '-15M']

# === INIT ===
db = create_client(SUPABASE_URL, SUPABASE_KEY)
auth = KalshiAuth()
app = Flask(__name__)

# === STATE ===
banked_profit = 0.0
current_hot_markets = []


def sf(val):
    try:
        return float(val) if val is not None else 0.0
    except:
        return 0.0


# === EXPIRY PARSING ===

def get_time_to_expiry(ticker, market=None):
    if isinstance(ticker, dict):
        market = ticker
        ticker = ticker.get('ticker', '')
    # Try parsing from ticker: e.g. KXBTCD-26MAR2318-B62 → 26MAR23 day=26 month=MAR year=23, hour=18
    # Match with or without trailing dash
    match = re.search(r'-(\d{2})([A-Z]{3})(\d{2})(\d{2})(?:-|$)', ticker)
    if match:
        g1, mon_str, g3, g4 = match.groups()
        months = {'JAN':1,'FEB':2,'MAR':3,'APR':4,'MAY':5,'JUN':6,
                  'JUL':7,'AUG':8,'SEP':9,'OCT':10,'NOV':11,'DEC':12}
        month = months.get(mon_str)
        if month:
            year = 2000 + int(g1)
            day = int(g3)
            hour = int(g4)
            try:
                expiry = datetime(year, month, day, hour, 59, 59, tzinfo=timezone.utc)
                now = datetime.now(timezone.utc)
                return max(0, (expiry - now).total_seconds())
            except:
                pass
    # Fallback: use market close_time/expiration_time from API
    if market:
        for field in ('close_time', 'expiration_time', 'expected_expiration_time'):
            close_time = market.get(field)
            if close_time:
                try:
                    close_dt = datetime.fromisoformat(close_time.replace('Z', '+00:00'))
                    now = datetime.now(timezone.utc)
                    return max(0, (close_dt - now).total_seconds())
                except:
                    pass
    return None


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


def kalshi_delete(path):
    try:
        url = f"{KALSHI_HOST}/trade-api/v2{path}"
        headers = auth.get_headers("DELETE", f"/trade-api/v2{path}")
        resp = requests.delete(url, headers=headers, timeout=10)
        resp.raise_for_status()
        return resp.json() if resp.text else {}
    except Exception as e:
        logger.error(f"DELETE {path} failed: {e}")
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
        time.sleep(0.5)
        return {'order_id': order_id, 'status': status}
    except Exception as e:
        logger.error(f"ORDER FAILED: {action.upper()} {ticker} -- {e}")
        time.sleep(0.5)
        return None


def get_live_bid(ticker, side):
    try:
        resp = kalshi_get(f"/markets/{ticker}/orderbook?depth=3")
        if side == 'yes':
            bids = resp.get('yes', resp.get('orderbook', {}).get('yes', []))
        else:
            bids = resp.get('no', resp.get('orderbook', {}).get('no', []))
        if isinstance(bids, list) and bids:
            if isinstance(bids[0], list):
                return float(bids[0][0]) / 100.0
            elif isinstance(bids[0], dict):
                prices = [float(k) for k in bids[0].keys()]
                return max(prices) / 100.0 if prices else 0.0
        return 0.0
    except Exception as e:
        logger.warning(f"Orderbook fetch failed for {ticker}: {e}")
        return 0.0


# === BALANCE ===

def get_kalshi_balance():
    try:
        resp = kalshi_get('/portfolio/balance')
        balance_cents = resp.get('balance', 0)
        return float(balance_cents) / 100.0
    except Exception as e:
        logger.error(f"Balance fetch failed: {e}")
        return 0.0


def get_realized_pnl():
    try:
        sells = db.table('trades').select('pnl') \
            .eq('action', 'sell').not_.is_('pnl', 'null') \
            .gte('created_at', PAPER_RESET_TIME).execute()
        return sum(sf(t['pnl']) for t in (sells.data or []))
    except Exception as e:
        logger.error(f"get_realized_pnl failed: {e}")
        return 0.0


def get_open_position_cost():
    try:
        open_buys = db.table('trades').select('price,count') \
            .eq('action', 'buy').is_('pnl', 'null') \
            .gte('created_at', PAPER_RESET_TIME).execute()
        return sum(sf(t['price']) * (t.get('count') or 1) for t in (open_buys.data or []))
    except Exception as e:
        logger.error(f"get_open_position_cost failed: {e}")
        return 0.0


def get_balance():
    if ENABLE_TRADING:
        return get_kalshi_balance()
    else:
        realized = get_realized_pnl()
        open_cost = get_open_position_cost()
        paper = PAPER_STARTING_BALANCE + realized - open_cost
        return max(0, paper)


def get_owned():
    try:
        result = db.table('trades').select('ticker') \
            .eq('action', 'buy').is_('pnl', 'null').execute()
        return {t['ticker'] for t in (result.data or [])}
    except Exception as e:
        logger.error(f"get_owned failed: {e}")
        return set()


# === SELL LOGIC ===

def should_sell(entry_price, current_bid, count, time_to_expiry_seconds):
    if current_bid <= 0 or entry_price <= 0:
        return False, 0, None

    gain_pct = ((current_bid - entry_price) / entry_price) * 100

    # +50% → SELL ALL (fast turnover, recycle cash)
    if gain_pct >= 50:
        return True, count, f"SELL +{gain_pct:.0f}%"

    # Expiry save — 1 min left + any profit → SELL ALL
    if time_to_expiry_seconds is not None and time_to_expiry_seconds < 60:
        if gain_pct > 0:
            return True, count, f"EXPIRY SAVE +{gain_pct:.0f}%"

    return False, 0, None


# === BUY LOGIC ===

def _get_volume(market):
    for key in ('volume', 'volume_24h', 'volume_24h_fp', 'volume_fp'):
        val = market.get(key)
        if val is not None and val != '' and val != 0:
            try:
                return int(float(val))
            except:
                pass
    return 0


def find_buy_candidates(markets):
    if markets:
        sample = markets[0]
        logger.info(f"MARKET FIELDS: {list(sample.keys())}")
        logger.info(f"VOL FIELDS: {[k for k in sample.keys() if 'vol' in k.lower()]} = {[sample.get(k) for k in sample.keys() if 'vol' in k.lower()]}")

    candidates = []
    blocked = wrong_price = no_bid = 0

    for market in markets:
        ticker = market.get('ticker', '')

        if any(pat in ticker for pat in BLOCKED_PATTERNS):
            blocked += 1
            continue

        yes_ask = sf(market.get('yes_ask_dollars', '0'))
        yes_bid = sf(market.get('yes_bid_dollars', '0'))
        no_ask = sf(market.get('no_ask_dollars', '0'))
        no_bid = sf(market.get('no_bid_dollars', '0'))
        volume = _get_volume(market)
        added = False

        # YES side: price in range + bid exists
        if BUY_MIN <= yes_ask <= BUY_MAX and yes_bid > 0:
            candidates.append({
                'ticker': ticker, 'side': 'yes',
                'price': yes_ask, 'bid': yes_bid,
                'volume': volume
            })
            added = True

        # NO side: price in range + bid exists
        if BUY_MIN <= no_ask <= BUY_MAX and no_bid > 0:
            candidates.append({
                'ticker': ticker, 'side': 'no',
                'price': no_ask, 'bid': no_bid,
                'volume': volume
            })
            added = True

        if not added:
            if not ((BUY_MIN <= yes_ask <= BUY_MAX) or (BUY_MIN <= no_ask <= BUY_MAX)):
                wrong_price += 1
            elif yes_bid <= 0 and no_bid <= 0:
                no_bid += 1

    candidates.sort(key=lambda x: x['volume'], reverse=True)
    total = len(markets)
    logger.info(f"FILTER: {total} total | {blocked} blocked | {wrong_price} price out | {no_bid} no bid | {len(candidates)} candidates")

    # Log first 10 candidates
    for c in candidates[:10]:
        logger.info(f"  CANDIDATE: {c['ticker']} {c['side']} ask=${c['price']:.2f} bid=${c['bid']:.2f} vol={c['volume']}")

    return candidates


# === FETCH ALL MARKETS ===

def fetch_all_markets():
    all_markets = []
    for series in ALL_SERIES:
        try:
            resp = kalshi_get(f'/markets?series_ticker={series}&status=open&limit=200')
            batch = resp.get('markets', [])
            all_markets.extend(batch)
        except Exception as e:
            logger.error(f"Fetch {series} failed: {e}")
    logger.info(f"Fetched {len(all_markets)} markets from {len(ALL_SERIES)} series")
    return all_markets


def _update_hot_markets(markets):
    global current_hot_markets
    owned = get_owned()
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
    for h in by_vol[:10]:
        vol = _get_volume(h)
        ticker = h.get('ticker', '')
        yes = h.get('yes_ask_dollars', '?')
        no = h.get('no_ask_dollars', '?')
        logger.info(f"HOT: {ticker} vol={vol} yes=${yes} no=${no}")


# === CANCEL ALL RESTING ORDERS ===

def cancel_all_resting():
    if not ENABLE_TRADING:
        return
    try:
        resp = kalshi_get('/portfolio/orders?status=resting')
        orders = resp.get('orders', [])
        cancelled = 0
        for order in orders:
            try:
                kalshi_delete(f"/portfolio/orders/{order['order_id']}")
                logger.info(f"CANCELLED: {order.get('ticker', 'unknown')} {order.get('side', '')} {order.get('action', '')}")
                cancelled += 1
            except Exception as e:
                logger.error(f"Cancel order failed: {e}")
        logger.info(f"Cancelled {cancelled} resting orders")
    except Exception as e:
        logger.error(f"Cancel all resting error: {e}")


# === SYNC DB WITH KALSHI ===

def sync_with_kalshi():
    try:
        open_buys = db.table('trades').select('*') \
            .eq('action', 'buy').is_('pnl', 'null').execute()
        if not open_buys.data:
            logger.info("Sync: no open positions in DB")
            return

        try:
            resp = kalshi_get('/portfolio/positions?limit=1000')
            kalshi_positions = resp.get('market_positions', [])
        except Exception as e:
            logger.error(f"Sync: failed to fetch Kalshi positions: {e}")
            return

        owned_on_kalshi = set()
        for pos in kalshi_positions:
            ticker = pos.get('ticker', '')
            if pos.get('total_traded', 0) > 0:
                owned_on_kalshi.add(ticker)

        cleared = 0
        for trade in open_buys.data:
            ticker = trade['ticker']
            entry_price = sf(trade['price'])
            count = trade.get('count') or 1

            if ticker in owned_on_kalshi:
                continue

            loss = round(-entry_price * count, 4)
            try:
                db.table('trades').update({
                    'pnl': loss,
                    'current_bid': 0,
                }).eq('id', trade['id']).execute()
                cleared += 1
                logger.info(f"SYNC: {ticker} not on Kalshi, cleared (pnl=${loss:.4f})")
            except Exception as e:
                logger.error(f"Sync update failed for {ticker}: {e}")

        logger.info(f"SYNC COMPLETE: cleared {cleared} ghost positions from DB")
    except Exception as e:
        logger.error(f"Sync error: {e}")


# === CHECK SELLS ===

def check_sells():
    global banked_profit
    logger.info("check_sells() -- sell at 50%, expiry save >0% @ 1min")
    try:
        open_buys = db.table('trades').select('*') \
            .eq('action', 'buy').is_('pnl', 'null') \
            .gte('created_at', PAPER_RESET_TIME).execute()
    except Exception as e:
        logger.error(f"check_sells DB query failed: {e}")
        return

    if not open_buys.data:
        logger.info("No open positions")
        return

    logger.info(f"Checking {len(open_buys.data)} open positions:")
    sold = 0
    settled = 0

    for trade in open_buys.data:
        ticker = trade['ticker']
        side = trade['side']
        entry_price = sf(trade['price'])
        count = trade.get('count') or 1
        if entry_price <= 0:
            continue

        # Fetch market
        market = get_market(ticker)
        if not market:
            continue

        status = market.get('status', '')
        result_val = market.get('result', '')

        # Settlement check
        if status in ('closed', 'settled', 'finalized') or result_val:
            if result_val == side:
                pnl = round((1.0 - entry_price) * count, 4)
                reason = f"WIN -- settled $1.00 (entry ${entry_price:.2f})"
                settle_price = 1.0
            elif result_val:
                pnl = round(-entry_price * count, 4)
                reason = f"LOSS -- expired (entry ${entry_price:.2f})"
                settle_price = 0.0
            else:
                continue

            logger.info(f"SETTLED: {ticker} {side} | {reason} | pnl=${pnl:.4f}")

            # Bank profit on wins
            if pnl > 0:
                banked = pnl * PROFIT_BANK_PCT
                banked_profit += banked
                logger.info(f"BANKED ${banked:.4f} (20% of ${pnl:.4f} profit) | Total banked: ${banked_profit:.4f}")

            try:
                db.table('trades').insert({
                    'ticker': ticker, 'side': side, 'action': 'sell',
                    'price': float(settle_price), 'count': count,
                    'pnl': float(pnl), 'strategy': 'crypto',
                    'reason': reason,
                    'sell_gain_pct': float(round(((settle_price - entry_price) / entry_price) * 100, 1)),
                }).execute()
            except Exception as e:
                logger.error(f"Settle insert failed: {e}")

            try:
                db.table('trades').update({
                    'pnl': 0.0,
                    'current_bid': float(settle_price),
                }).eq('id', trade['id']).execute()
            except:
                pass
            settled += 1
            continue

        # Clear expired contracts
        time_to_expiry = get_time_to_expiry(ticker, market=market)
        if time_to_expiry is not None and time_to_expiry == 0:
            loss = round(-entry_price * count, 4)
            try:
                db.table('trades').update({
                    'pnl': loss,
                    'current_bid': 0,
                }).eq('id', trade['id']).execute()
            except:
                pass
            logger.info(f"EXPIRED: {ticker} pnl=${loss:.4f}")
            continue

        # Get current bid
        if side == 'yes':
            current_bid = sf(market.get('yes_bid_dollars', '0'))
        else:
            current_bid = sf(market.get('no_bid_dollars', '0'))

        if current_bid <= 0:
            current_bid = get_live_bid(ticker, side)

        if current_bid <= 0:
            continue

        gain_pct = ((current_bid - entry_price) / entry_price) * 100
        logger.info(f"  POS: {ticker} {side} entry=${entry_price:.2f} bid=${current_bid:.2f} {gain_pct:+.0f}%")

        # Update current price in DB
        try:
            db.table('trades').update({
                'current_bid': float(current_bid),
            }).eq('id', trade['id']).execute()
        except:
            pass

        # Decide sell
        do_sell, sell_qty, reason = should_sell(entry_price, current_bid, count, time_to_expiry)

        if do_sell and sell_qty > 0:
            pnl = round((current_bid - entry_price) * sell_qty, 4)
            sell_order = place_order(ticker, side, 'sell', current_bid, sell_qty)
            if not sell_order:
                continue

            logger.info(f"SELL: {ticker} {side} x{sell_qty} @ ${current_bid:.2f} | {reason} | pnl=${pnl:.4f}")

            # Bank profit
            if pnl > 0:
                banked = pnl * PROFIT_BANK_PCT
                banked_profit += banked
                logger.info(f"BANKED ${banked:.4f} (20% of ${pnl:.4f} profit) | Total banked: ${banked_profit:.4f}")

            try:
                db.table('trades').insert({
                    'ticker': ticker, 'side': side, 'action': 'sell',
                    'price': float(current_bid), 'count': sell_qty,
                    'pnl': float(pnl), 'strategy': 'crypto',
                    'reason': reason,
                    'sell_gain_pct': float(round(gain_pct, 1)),
                }).execute()
            except Exception as e:
                logger.error(f"Sell insert failed: {e}")

            # If partial sell (half), update remaining count on buy record
            remaining = count - sell_qty
            if remaining > 0:
                try:
                    db.table('trades').update({
                        'count': remaining,
                        'current_bid': float(current_bid),
                    }).eq('id', trade['id']).execute()
                except:
                    pass
            else:
                try:
                    db.table('trades').update({
                        'pnl': 0.0,
                        'current_bid': float(current_bid),
                    }).eq('id', trade['id']).execute()
                except:
                    pass
            sold += 1

    logger.info(f"SELL SUMMARY: sold={sold} settled={settled}")


# === RUN BUYS ===

def run_buys(markets):
    cash = get_balance()
    owned = get_owned()
    logger.info(f"Balance: ${cash:.2f} | {len(owned)} positions open")

    candidates = find_buy_candidates(markets)
    candidates = [c for c in candidates if c['ticker'] not in owned]
    logger.info(f"Found {len(candidates)} buy candidates after dedup")

    bought = 0
    for c in candidates:
        cost = c['price'] * MAX_CONTRACTS_PER_TRADE

        if cost > cash:
            logger.info(f"OUT OF CASH: need ${cost:.2f}, have ${cash:.2f}")
            break

        count = MAX_CONTRACTS_PER_TRADE
        result = place_order(c['ticker'], c['side'], 'buy', c['price'], count)
        if not result:
            continue

        if ENABLE_TRADING and result.get('status') == 'resting':
            try:
                kalshi_delete(f"/portfolio/orders/{result['order_id']}")
                logger.info(f"CANCELLED RESTING: {c['ticker']} -- no instant fill")
            except Exception as e:
                logger.error(f"Cancel resting failed: {e}")
            continue

        if ENABLE_TRADING and result.get('status') != 'executed':
            logger.info(f"SKIP: {c['ticker']} status={result.get('status')}, not logging")
            continue

        logger.info(f"BUY: {c['ticker']} {c['side']} x{count} @ ${c['price']:.2f} (bid=${c['bid']:.2f}, vol={c['volume']})")
        try:
            db.table('trades').insert({
                'ticker': c['ticker'], 'side': c['side'], 'action': 'buy',
                'price': float(c['price']), 'count': count,
                'strategy': 'crypto',
                'reason': f"BUY {c['side'].upper()} @ ${c['price']:.2f} bid=${c['bid']:.2f} vol={c['volume']}",
                'current_bid': float(c['bid']),
            }).execute()
            owned.add(c['ticker'])
            cash -= cost
            bought += 1
        except Exception as e:
            logger.error(f"Buy DB insert failed: {e}")

    logger.info(f"Bought {bought}")


# === MAIN CYCLE ===

_synced = False

def run_cycle():
    global _synced
    balance = get_balance()
    mode = "PAPER" if not ENABLE_TRADING else "LIVE"
    logger.info(f"=== CYCLE START [{mode}] === Balance: ${balance:.2f}")

    if not _synced:
        try:
            sync_with_kalshi()
            _synced = True
        except Exception as e:
            logger.error(f"First cycle sync error: {e}")

    try:
        check_sells()
    except Exception as e:
        logger.error(f"Sell check error: {e}")

    try:
        markets = fetch_all_markets()
        _update_hot_markets(markets)
        run_buys(markets)
    except Exception as e:
        logger.error(f"Buy error: {e}")

    balance = get_balance()
    logger.info(f"=== CYCLE END [{mode}] === Balance: ${balance:.2f}")


# === DASHBOARD ===

@app.route('/')
def health():
    return 'OK'


@app.route('/api/status')
def api_status():
    try:
        balance = get_balance()

        sells = db.table('trades').select('pnl') \
            .eq('action', 'sell').not_.is_('pnl', 'null') \
            .gte('created_at', PAPER_RESET_TIME).execute()
        sell_data = sells.data or []
        net_pnl = sum(sf(t['pnl']) for t in sell_data)
        wins = sum(1 for t in sell_data if sf(t['pnl']) > 0)
        losses = sum(1 for t in sell_data if sf(t['pnl']) < 0)

        open_buys = db.table('trades').select('id,price,count,current_bid') \
            .eq('action', 'buy').is_('pnl', 'null') \
            .gte('created_at', PAPER_RESET_TIME).execute()
        open_data = open_buys.data or []
        live_positions = [t for t in open_data if sf(t.get('current_bid')) > 0]
        open_count = len(live_positions)
        positions_value = round(sum(sf(t.get('current_bid')) * (t.get('count') or 1) for t in live_positions), 2)
        positions_cost = round(sum(sf(t.get('price')) * (t.get('count') or 1) for t in live_positions), 2)

        cash = round(balance, 2)
        mode = "PAPER" if not ENABLE_TRADING else "LIVE"

        return jsonify({
            'balance': round(balance + positions_cost, 2),
            'net_pnl': round(net_pnl, 4),
            'wins': wins,
            'losses': losses,
            'open_count': open_count,
            'positions_value': positions_value,
            'positions_cost': positions_cost,
            'cash': cash,
            'banked_profit': round(banked_profit, 4),
            'mode': mode,
        })
    except Exception as e:
        logger.error(f"API status error: {e}")
        mode = "PAPER" if not ENABLE_TRADING else "LIVE"
        return jsonify({
            'balance': 0, 'net_pnl': 0, 'wins': 0, 'losses': 0,
            'open_count': 0, 'positions_value': 0, 'positions_cost': 0,
            'cash': 0, 'banked_profit': 0, 'mode': mode, 'error': str(e),
        })


@app.route('/api/trades')
def api_trades():
    try:
        result = db.table('trades').select('*') \
            .gte('created_at', PAPER_RESET_TIME) \
            .order('created_at', desc=True).limit(200).execute()
        return jsonify(result.data or [])
    except Exception as e:
        logger.error(f"API trades error: {e}")
        return jsonify([])


@app.route('/api/open')
def api_open():
    try:
        result = db.table('trades').select('*') \
            .eq('action', 'buy').is_('pnl', 'null') \
            .gte('created_at', PAPER_RESET_TIME).execute()
        positions = []
        for t in (result.data or []):
            price = sf(t.get('price'))
            current = sf(t.get('current_bid'))
            count = int(t.get('count') or 1)
            if not current or current <= 0:
                continue
            if price > 0:
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
<title>Kalshi Bot - Clean Reset</title>
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
  <div class="sub"><span class="live-dot dot-paper" id="mode-dot"></span><span id="mode-label">PAPER MODE</span> &mdash; golden hour &mdash; crypto hourly 3-15c &mdash; sell at 50%, expiry save</div>
  <div class="portfolio-value" id="p-total">...</div>
  <div class="portfolio-pnl" id="p-pnl">...</div>
  <div class="portfolio-breakdown">
    <div class="item"><div class="label">Positions</div><div class="val" id="p-positions">...</div></div>
    <div class="item"><div class="label">Cash</div><div class="val" id="p-cash">...</div></div>
    <div class="item"><div class="label">Record</div><div class="val" id="p-record">...</div></div>
    <div class="item"><div class="label">Banked</div><div class="val green" id="p-banked">...</div></div>
  </div>
</div>

<div class="panel">
  <div class="panel-header"><h2>Hot Markets</h2><div class="count" id="hot-count"></div></div>
  <div class="panel-body"><table><thead><tr>
    <th>Ticker</th><th>Title</th><th>Volume</th><th>Yes</th><th>No</th><th></th>
  </tr></thead><tbody id="hot-body"><tr><td colspan="6" class="loading">Loading...</td></tr></tbody></table></div>
</div>

<div class="panel">
  <div class="panel-header"><h2>Open Positions</h2><div class="count" id="open-count"></div></div>
  <div class="panel-body"><table><thead><tr>
    <th>Ticker</th><th>Side</th><th>Qty</th><th>Entry</th><th>Bid</th><th>P&amp;L</th><th>Gain</th>
  </tr></thead><tbody id="open-body"><tr><td colspan="7" class="loading">Loading...</td></tr></tbody></table></div>
</div>

<div class="panel">
  <div class="panel-header"><h2>Recent Trades (Last 20)</h2><div class="count" id="trades-count"></div></div>
  <div class="panel-body"><table><thead><tr>
    <th>Time</th><th>Ticker</th><th>Side</th><th>Qty</th><th>P&amp;L</th><th>Gain</th>
  </tr></thead><tbody id="trades-body"><tr><td colspan="6" class="loading">Loading...</td></tr></tbody></table></div>
</div>

<div class="equity-section">
  <h2>Equity Curve</h2>
  <canvas id="equity-chart"></canvas>
</div>

<div class="status-bar">
  <div class="status-item"><span class="live-dot dot-paper" id="status-dot"></span> <span id="status-mode">PAPER</span></div>
  <div class="status-item">Buy: 3-15c, bid &gt; 0, no 15M</div>
  <div class="status-item">Sell: 50% all, expiry save &gt;0%@1m</div>
  <div class="status-item">Max: 3 contracts, 10 positions</div>
  <div class="status-item">Series: Crypto hourly only</div>
  <div class="status-item">Last: <span id="last-update">&mdash;</span></div>
</div>
<div class="footer">Clean Reset &mdash; auto-refresh 15s</div>

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
    var posVal=status.positions_value||0;
    var posCost=status.positions_cost||0;
    var cash=status.cash||0;
    var portfolio=cash+posVal;
    var unrealized=posVal-posCost;
    $('p-total').textContent='$'+portfolio.toFixed(2);

    var pnl=status.net_pnl||0;
    var arrow=pnl>=0?'\\u25B2':'\\u25BC';
    $('p-pnl').innerHTML='<span class="'+cls(pnl)+'">'+arrow+' '+(pnl>=0?'+':'')+pnl.toFixed(2)+' realized</span>'
      +(unrealized!==0?' <span class="'+cls(unrealized)+'" style="font-size:14px">'+(unrealized>=0?'+':'')+unrealized.toFixed(2)+' open</span>':'');

    $('p-positions').textContent='$'+posVal.toFixed(2);
    $('p-cash').textContent='$'+cash.toFixed(2);
    $('p-record').innerHTML='<span class="green">'+status.wins+'W</span> <span class="gray">/</span> <span class="red">'+status.losses+'L</span>';
    $('p-banked').textContent='$'+(status.banked_profit||0).toFixed(2);

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
    var completed=trades.filter(function(t){return t.action==='sell'&&t.pnl!==null&&t.pnl!==0});
    $('trades-count').textContent=completed.length+' trades';
    var h='';
    completed.slice(0,20).forEach(function(t){
      var p=t.pnl||0;
      var pc=cls(p);
      var rc=p>0?'row-green':'row-red';
      var time=(t.created_at||'').replace('T',' ').substring(5,19);
      var count=t.count||1;
      var gainPct=t.sell_gain_pct||0;
      h+='<tr class="'+rc+'">';
      h+='<td>'+esc(time)+'</td>';
      h+='<td style="font-size:10px">'+esc(t.ticker||'')+'</td>';
      h+='<td>'+esc(t.side||'')+'</td>';
      h+='<td>'+count+'</td>';
      h+='<td class="'+pc+'">'+(p>=0?'+':'')+p.toFixed(4)+'</td>';
      h+='<td class="'+pc+'">'+(gainPct>=0?'+':'')+gainPct.toFixed(0)+'%</td>';
      h+='</tr>';
    });
    $('trades-body').innerHTML=h||'<tr><td colspan="6" class="gray" style="text-align:center">No completed trades</td></tr>';

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
    logger.info(f"Bot starting [{mode}] -- buy ${BUY_MIN}-${BUY_MAX}, sell +50%, expiry save, {CYCLE_SECONDS}s cycles")
    logger.info(f"Series: {ALL_SERIES}")
    logger.info(f"Sizing: {MAX_CONTRACTS_PER_TRADE} contracts per trade, no position cap")

    cancel_all_resting()
    sync_with_kalshi()

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
