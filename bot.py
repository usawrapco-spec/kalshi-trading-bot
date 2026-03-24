"""
Crypto scalper. Buy cheap 15M contracts settling within 20min.
Sell at +30% (beats fees), stop loss at -40%, trailing stop -15% from peak.
Directional filter: only buy with momentum. $100 balance, 20% reserve.
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

# === STRATEGY: ALL 15M CRYPTO MARKETS ===
BUY_MIN = 0.03
BUY_MAX = 0.15              # cheap contracts only — high risk/reward
SELL_THRESHOLD = 0.30       # +30%: sell ALL contracts (must stay >= 30% to beat Kalshi fees)
STOP_LOSS_PCT = -0.30       # -30%: cut losses before total wipeout
TRAIL_DROP_PCT = 0.15       # sell if price drops 15% from peak (lock in gains)
MOMENTUM_THRESHOLD = 0.02   # 2% price change = momentum signal (was 5%, too strict)
MAX_ADDS = 2                # can add to a GREEN position twice (max 15 contracts)
TAKER_FEE_RATE = 0.07
MAX_MINS_TO_EXPIRY = 20
CYCLE_SECONDS = 10          # 10 sec cycles — don't overtrade
STARTING_BALANCE = 1000.00
CASH_RESERVE = 0.20         # keep 20% cash reserve ($20 buffer)
MAX_BUYS_PER_CYCLE = 2      # max 2 buys per cycle — don't flood
CONTRACTS = 3
MAX_DAILY_LOSS = float(os.environ.get('MAX_DAILY_LOSS', '10.00'))  # stop buying after $10 daily loss

CRYPTO_SERIES = ['KXBTC15M', 'KXETH15M', 'KXSOL15M', 'KXXRP15M', 'KXDOGE15M']

# === INIT ===
db = create_client(SUPABASE_URL, SUPABASE_KEY)
auth = KalshiAuth()
app = Flask(__name__)

current_hot_markets = []
last_cycle_prices = {}   # ticker -> yes_ask, for momentum detection
peak_bids = {}           # trade_id -> highest bid seen
daily_loss = 0.0         # cumulative realized losses today
daily_loss_date = None   # reset daily


def kalshi_fee(price, count):
    """Calculate Kalshi taker fee for a trade. Fee applies on both buy and sell."""
    p = float(price)
    c = int(count)
    return min(TAKER_FEE_RATE * c * p * (1 - p), 0.02 * c)


def net_pnl(entry_price, exit_price, count):
    """Calculate PnL after Kalshi fees (buy + sell side)."""
    gross = (exit_price - entry_price) * count
    buy_fee = kalshi_fee(entry_price, count)
    sell_fee = kalshi_fee(exit_price, count)
    return round(gross - buy_fee - sell_fee, 4)


def net_gain_pct(entry_price, exit_price, count):
    """Net gain % after fees, relative to total cost (entry + buy fee)."""
    total_cost = entry_price * count + kalshi_fee(entry_price, count)
    if total_cost <= 0:
        return 0
    return net_pnl(entry_price, exit_price, count) / total_cost


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


# === SELL LOGIC ===

def check_sells():
    global daily_loss
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
            settle_price = 1.0 if result_val == side else 0.0
            gross = round((settle_price - entry_price) * count, 4)
            b_fee = kalshi_fee(entry_price, count)
            s_fee = 0.0  # no sell fee on settlement
            net = round(gross - b_fee, 4)
            reason = f"{'WIN' if result_val == side else 'LOSS'} settled @${settle_price:.2f}"
            logger.info(f"SETTLED: {ticker} {side} | {reason} | gross=${gross:.4f} buy_fee=${b_fee:.4f} net=${net:.4f}")
            if net < 0:
                daily_loss += abs(net)
            try:
                db.table('trades').update({
                    'pnl': float(net),
                    'gross_pnl': float(gross),
                    'net_pnl': float(net),
                    'buy_fee': float(b_fee),
                    'sell_fee': float(s_fee),
                    'current_bid': float(settle_price),
                }).eq('id', trade['id']).execute()
            except Exception as e:
                logger.error(f"Settle DB error: {e}")
            peak_bids.pop(trade['id'], None)
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

        # If bid is $0 and market is still open — force stop-loss, don't wait
        if current_bid <= 0:
            if status == 'open':
                # Market is open but bid is $0 — this IS a total loss, book it now
                pnl = round(-entry_price * count, 4)
                logger.info(f"STOP LOSS ($0 bid): {ticker} {side} x{count} entry=${entry_price:.2f} | pnl=${pnl:.4f}")
                if pnl < 0:
                    daily_loss += abs(pnl)
                try:
                    db.table('trades').update({'pnl': float(pnl), 'current_bid': 0.0}).eq('id', trade['id']).execute()
                except Exception as e:
                    logger.error(f"$0 stop DB error: {e}")
                peak_bids.pop(trade['id'], None)
                expired += 1
            else:
                # Market closed/settled — wait for Kalshi to finalize
                logger.info(f"WAITING: {ticker} bid=$0, status={status}, waiting for settlement")
            continue

        # Calculate net gain AFTER Kalshi fees
        raw_gain = (current_bid - entry_price) / entry_price
        raw_gain_pct = raw_gain * 100
        net = net_gain_pct(entry_price, current_bid, count)
        net_pct = net * 100
        net_profit = net_pnl(entry_price, current_bid, count)
        fees = kalshi_fee(entry_price, count) + kalshi_fee(current_bid, count)

        # Track peak bid
        trade_id = trade['id']
        if trade_id not in peak_bids or current_bid > peak_bids[trade_id]:
            peak_bids[trade_id] = current_bid
        peak = peak_bids[trade_id]

        logger.info(f"  POS: {ticker} {side} entry=${entry_price:.2f} bid=${current_bid:.2f} raw={raw_gain_pct:+.0f}% NET={net_pct:+.1f}% fees=${fees:.3f} x{count}")

        should_sell = False
        reason = ''

        # Instant sell at +100% net (doubled after fees)
        if net >= 1.00:
            should_sell = True
            reason = f"NET +{net_pct:.0f}% DOUBLED — INSTANT SELL"

        # Take profit at +10% net (after fees — the real baseline)
        elif net >= 0.10:
            should_sell = True
            reason = f"NET +{net_pct:.1f}% PROFIT (${net_profit:.3f} after ${fees:.3f} fees)"

        # Smart stop loss — check momentum before cutting
        elif raw_gain <= STOP_LOSS_PCT:
            # Check how close to settlement
            close_time = market.get('close_time') or market.get('expected_expiration_time')
            mins_to_settle = 999
            if close_time:
                try:
                    close_dt = datetime.fromisoformat(close_time.replace('Z', '+00:00'))
                    mins_to_settle = (close_dt - datetime.now(timezone.utc)).total_seconds() / 60
                except:
                    pass

            # Near 2-min mark: check if momentum is shifting our way
            if mins_to_settle <= 2:
                prev_bid = peak_bids.get(f"{trade_id}_prev", current_bid)
                if current_bid > prev_bid:
                    # Price recovering — hold, momentum shifting our way
                    logger.info(f"  HOLD: {ticker} net {net_pct:.0f}% but recovering (${prev_bid:.2f} -> ${current_bid:.2f}), {mins_to_settle:.1f}min left")
                else:
                    # Still dropping near expiry — cut it
                    should_sell = True
                    reason = f"NET {net_pct:.0f}% STOP LOSS (not recovering, {mins_to_settle:.1f}min left)"
            else:
                # Not near settlement yet — hard stop loss
                should_sell = True
                reason = f"NET {net_pct:.0f}% STOP LOSS"

            # Track previous bid for momentum detection
            peak_bids[f"{trade_id}_prev"] = current_bid

        if not should_sell:
            continue

        # === SELL ALL CONTRACTS ===
        pnl = net_pnl(entry_price, current_bid, count)

        logger.info(f"SELL ATTEMPT: {ticker} {side} x{count} entry=${entry_price:.2f} bid=${current_bid:.2f} net={net_pct:+.1f}% reason={reason}")

        result = place_order(ticker, side, 'sell', current_bid, count)
        if not result:
            logger.error(f"SELL FAILED: {ticker} -- order not filled")
            continue

        order_id, filled = result
        if filled < count:
            logger.warning(f"PARTIAL SELL: {ticker} filled {filled}/{count}")
            pnl = net_pnl(entry_price, current_bid, filled)

        s_fee = kalshi_fee(current_bid, filled)
        b_fee = kalshi_fee(entry_price, filled)
        gross = round((current_bid - entry_price) * filled, 4)

        logger.info(f"SOLD ({reason}): {ticker} {side} x{filled} @ ${current_bid:.2f} | gross=${gross:.4f} buy_fee=${b_fee:.4f} sell_fee=${s_fee:.4f} net=${pnl:.4f}")
        if pnl < 0:
            daily_loss += abs(pnl)
            logger.info(f"Daily loss now: ${daily_loss:.2f} (limit: ${MAX_DAILY_LOSS:.2f})")
        try:
            if filled >= count:
                db.table('trades').update({
                    'pnl': float(pnl),
                    'gross_pnl': float(gross),
                    'net_pnl': float(pnl),
                    'buy_fee': float(b_fee),
                    'sell_fee': float(s_fee),
                    'current_bid': float(current_bid),
                }).eq('id', trade['id']).execute()
            else:
                db.table('trades').update({
                    'count': count - filled,
                    'current_bid': float(current_bid),
                }).eq('id', trade['id']).execute()
                db.table('trades').insert({
                    'ticker': ticker, 'side': side, 'action': 'buy',
                    'price': float(entry_price), 'count': filled,
                    'current_bid': float(current_bid),
                    'pnl': float(pnl),
                    'gross_pnl': float(gross),
                    'net_pnl': float(pnl),
                    'buy_fee': float(b_fee),
                    'sell_fee': float(s_fee),
                }).execute()
        except Exception as e:
            logger.error(f"Sell DB error: {e}")
        sold += 1

    logger.info(f"SELL SUMMARY: sold={sold} expired={expired}")


# === BUY LOGIC ===

def fetch_all_markets():
    all_markets = []
    for series in CRYPTO_SERIES:
        cursor = None
        pages = 0
        try:
            while True:
                url = f'/markets?series_ticker={series}&status=open&limit=200'
                if cursor:
                    url += f'&cursor={cursor}'
                resp = kalshi_get(url)
                batch = resp.get('markets', [])
                all_markets.extend(batch)
                pages += 1
                cursor = resp.get('cursor')
                if not cursor or not batch:
                    break
            if pages > 1:
                logger.info(f"  {series}: {pages} pages fetched")
        except Exception as e:
            logger.error(f"Fetch {series} failed: {e}")
    logger.info(f"Fetched {len(all_markets)} markets from {len(CRYPTO_SERIES)} series")
    return all_markets


def detect_momentum(markets):
    """Compare current prices to last cycle. Returns True if any market moved significantly."""
    global last_cycle_prices
    momentum = False
    for market in markets:
        ticker = market.get('ticker', '')
        yes_ask = float(market.get('yes_ask_dollars') or '0')
        if yes_ask <= 0 or yes_ask >= 0.99:
            continue
        if ticker in last_cycle_prices:
            old_price = last_cycle_prices[ticker]
            if old_price > 0:
                change = abs(yes_ask - old_price) / old_price
                if change >= MOMENTUM_THRESHOLD:
                    logger.info(f"MOMENTUM: {ticker} moved {change*100:.1f}% ({old_price:.2f} -> {yes_ask:.2f})")
                    momentum = True
        last_cycle_prices[ticker] = yes_ask
    return momentum


def get_recent_losers():
    """Get tickers that lost in the last 3 resolved trades — don't re-buy losers."""
    try:
        recent = db.table('trades').select('ticker,pnl').eq('action', 'buy').not_.is_('pnl', 'null').order('created_at', desc=True).limit(3).execute()
        return {t['ticker'] for t in (recent.data or []) if sf(t['pnl']) < 0}
    except:
        return set()


def buy_candidates(markets):
    global daily_loss, daily_loss_date

    # Reset daily loss counter at midnight UTC
    today = datetime.now(timezone.utc).date()
    if daily_loss_date != today:
        daily_loss = 0.0
        daily_loss_date = today

    # Check daily loss limit
    if daily_loss >= MAX_DAILY_LOSS:
        logger.info(f"DAILY LOSS LIMIT HIT: ${daily_loss:.2f} >= ${MAX_DAILY_LOSS:.2f} -- no new buys")
        return

    balance = get_balance()
    open_positions = get_open_positions()
    logger.info(f"Balance: ${balance:.2f} | {len(open_positions)} positions open | Daily loss: ${daily_loss:.2f}")

    deployable = balance * (1.0 - CASH_RESERVE)
    if deployable <= 1.0:
        logger.info(f"Balance ${balance:.2f}, deployable ${deployable:.2f} too low -- skipping buys")
        return

    candidates = []
    now = datetime.now(timezone.utc)

    for market in markets:
        ticker = market.get('ticker', '')

        # Skip if more than 20 min to settlement (not a 15M contract window)
        close_time = market.get('close_time') or market.get('expected_expiration_time')
        if close_time:
            try:
                close_dt = datetime.fromisoformat(close_time.replace('Z', '+00:00'))
                mins_left = (close_dt - now).total_seconds() / 60
                if mins_left > MAX_MINS_TO_EXPIRY:
                    continue
            except:
                pass

        yes_ask = float(market.get('yes_ask_dollars') or '999')
        yes_bid = float(market.get('yes_bid_dollars') or '0')
        no_ask = float(market.get('no_ask_dollars') or '999')
        no_bid = float(market.get('no_bid_dollars') or '0')

        logger.info(f"  MARKET: {ticker} yes=${yes_ask:.2f} no=${no_ask:.2f}")

        # Buy the CHEAPEST side — stop-loss & trailing stop provide protection
        if yes_ask <= no_ask and yes_ask < 0.99 and yes_bid > 0:
            side, price, bid = 'yes', yes_ask, yes_bid
            strategy = 'yes'
        elif no_ask < 0.99 and no_bid > 0:
            side, price, bid = 'no', no_ask, no_bid
            strategy = 'no'
        elif yes_ask < 0.99 and yes_bid > 0:
            side, price, bid = 'yes', yes_ask, yes_bid
            strategy = 'yes'
        else:
            continue

        # Dedup: check if we already own this ticker
        ticker_positions = [t for t in open_positions if t['ticker'] == ticker]
        if ticker_positions:
            # Only double down if position is GREEN using LIVE bid
            pos = ticker_positions[0]
            pos_entry = sf(pos.get('price'))
            pos_side = pos.get('side')
            live_bid = yes_bid if pos_side == 'yes' else no_bid
            if live_bid <= pos_entry or len(ticker_positions) > MAX_ADDS:
                continue  # red or maxed out, skip
            logger.info(f"  DOUBLE DOWN: {ticker} {pos_side} entry=${pos_entry:.2f} live_bid=${live_bid:.2f} GREEN, adding")

        candidates.append({'ticker': ticker, 'side': side, 'price': price, 'bid': bid, 'strategy': strategy})

    candidates.sort(key=lambda x: x['price'])
    candidates = candidates[:MAX_BUYS_PER_CYCLE]
    logger.info(f"Found {len(candidates)} buy candidates")

    bought = 0
    for c in candidates:
        if bought >= MAX_BUYS_PER_CYCLE:
            break

        contracts = CONTRACTS
        cost = c['price'] * contracts
        if cost > deployable:
            logger.info(f"OUT OF CASH: need ${cost:.2f}, deployable ${deployable:.2f}")
            continue

        result = place_order(c['ticker'], c['side'], 'buy', c['price'], contracts)
        if not result:
            continue

        order_id, filled = result
        if filled <= 0:
            continue

        actual_cost = c['price'] * filled
        b_fee = kalshi_fee(c['price'], filled)
        logger.info(f"BUY: {c['ticker']} {c['side']} x{filled} @ ${c['price']:.2f} | buy_fee=${b_fee:.4f}")
        try:
            db.table('trades').insert({
                'ticker': c['ticker'], 'side': c['side'], 'action': 'buy',
                'price': float(c['price']), 'count': filled,
                'current_bid': float(c['bid']),
                'strategy': c.get('strategy'),
                'buy_fee': float(b_fee),
            }).execute()
            open_positions.append({'ticker': c['ticker'], 'price': c['price']})
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

        # Fee tracking from DB columns
        all_trades = db.table('trades').select('count,buy_fee,sell_fee,gross_pnl,net_pnl').eq('action', 'buy').execute()
        total_contracts = sum((t.get('count') or 1) for t in (all_trades.data or []))
        total_buy_fees = sum(sf(t.get('buy_fee')) for t in (all_trades.data or []))
        total_sell_fees = sum(sf(t.get('sell_fee')) for t in (all_trades.data or []))
        total_fees = round(total_buy_fees + total_sell_fees, 4)
        total_gross = sum(sf(t.get('gross_pnl')) for t in (all_trades.data or []) if t.get('gross_pnl') is not None)
        total_net = sum(sf(t.get('net_pnl')) for t in (all_trades.data or []) if t.get('net_pnl') is not None)

        mode = "PAPER" if not ENABLE_TRADING else "LIVE"

        return jsonify({
            'portfolio': round(portfolio, 2),
            'cash': round(cash, 2),
            'positions_value': round(positions_value, 2),
            'gross_pnl': round(total_gross, 4),
            'buy_fees': round(total_buy_fees, 4),
            'sell_fees': round(total_sell_fees, 4),
            'total_fees': total_fees,
            'net_pnl': round(total_net, 4),
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
                'strategy': (t.get('strategy') or 'fav').upper(),
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
            count = t.get('count', 1)
            gross = sf(t.get('gross_pnl'))
            b_fee = sf(t.get('buy_fee'))
            s_fee = sf(t.get('sell_fee'))
            net = sf(t.get('net_pnl')) or sf(t.get('pnl'))
            net_pct = round(net_gain_pct(entry, exit_price, count) * 100, 1) if entry > 0 else 0
            trades.append({
                'created_at': t.get('created_at', ''),
                'ticker': t.get('ticker', ''),
                'side': t.get('side', ''),
                'count': count,
                'entry': entry,
                'exit': exit_price,
                'gross_pnl': gross,
                'buy_fee': b_fee,
                'sell_fee': s_fee,
                'net_pnl': net,
                'gain_pct': net_pct,
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
  <span id="mode-label">PAPER MODE</span> &mdash; all 15M crypto &mdash; take +10% net / stop -30% / +100% instant
  &mdash; NEXT SETTLEMENT: <span id="countdown" style="color:#ffaa00;font-weight:700">--:--</span>
</div>

<div class="top-bar" style="flex-direction:column;gap:6px">
  <div style="font-size:16px">PORTFOLIO: <span id="tb-portfolio">...</span></div>
  <div style="font-size:12px;color:#888">Positions: <span id="tb-positions">...</span> &nbsp;&nbsp; Cash: <span id="tb-cash">...</span></div>
  <div style="font-size:12px">Gross: <span id="tb-gross">...</span> &nbsp;&nbsp; Buy Fees: <span id="tb-bfees" class="red">...</span> &nbsp;&nbsp; Sell Fees: <span id="tb-sfees" class="red">...</span> &nbsp;&nbsp; Net: <span id="tb-net">...</span></div>
  <div style="font-size:12px">RECORD: <span id="tb-record">...</span></div>
</div>

<div class="panel">
  <div class="panel-header"><h2>Open Positions</h2><div class="count" id="open-count"></div></div>
  <div class="panel-body"><table><thead><tr>
    <th>Type</th><th>Ticker</th><th>Side</th><th>Qty</th><th>Entry</th><th>Cost</th><th>Bid</th><th>Value</th><th>P&amp;L</th><th>Gain%</th>
  </tr></thead><tbody id="open-body"><tr><td colspan="10" class="loading">Loading...</td></tr></tbody></table></div>
</div>

<div class="panel">
  <div class="panel-header"><h2>Recent Trades</h2><div class="count" id="trades-count"></div></div>
  <div class="panel-body"><table><thead><tr>
    <th>Time</th><th>Ticker</th><th>Side</th><th>Qty</th><th>Entry</th><th>Exit</th><th>Gross</th><th>Buy Fee</th><th>Sell Fee</th><th>Net P&amp;L</th><th>Net%</th>
  </tr></thead><tbody id="trades-body"><tr><td colspan="11" class="loading">Loading...</td></tr></tbody></table></div>
</div>

<div class="panel">
  <div class="panel-header"><h2>Hot Markets</h2><div class="count" id="hot-count"></div></div>
  <div class="panel-body"><table><thead><tr>
    <th>Ticker</th><th>Yes Price</th><th>No Price</th><th>Volume</th>
  </tr></thead><tbody id="hot-body"><tr><td colspan="4" class="loading">Loading...</td></tr></tbody></table></div>
</div>

<div class="status-bar">
  <span>All 15M crypto | 2% momentum gate | Sell +30% | Stop -30% | Trail -15%</span>
  <span>20min max | 3 contracts | 20% reserve | $100 balance</span>
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

    var gross=status.gross_pnl||0;
    $('tb-gross').innerHTML='<span class="'+cls(gross)+'">'+(gross>=0?'+$':'-$')+Math.abs(gross).toFixed(4)+'</span>';
    var bf=status.buy_fees||0;
    $('tb-bfees').textContent='-$'+bf.toFixed(4)+' ('+(status.total_contracts||0)+'c)';
    var sf2=status.sell_fees||0;
    $('tb-sfees').textContent='-$'+sf2.toFixed(4);
    var net=status.net_pnl||0;
    $('tb-net').innerHTML='<span class="'+cls(net)+'" style="font-weight:700">'+(net>=0?'+$':'-$')+Math.abs(net).toFixed(4)+'</span>';
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
      var strat=p.strategy||'FAV';
      var sc=strat==='LONG'?'color:#ffaa00':'color:#00d673';
      h+='<tr class="'+rc+'">';
      h+='<td style="'+sc+';font-weight:700;font-size:9px">'+strat+'</td>';
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
    $('open-body').innerHTML=h||'<tr><td colspan="10" class="gray" style="text-align:center">No open positions</td></tr>';
  }

  if(trades){
    $('trades-count').textContent=trades.length+' trades';
    var h='';
    trades.forEach(function(t){
      var net=t.net_pnl||0;
      var pc=cls(net);
      var rc=net>0?'row-green':net<0?'row-red':'';
      h+='<tr class="'+rc+'">';
      h+='<td>'+timeAgo(t.created_at)+'</td>';
      h+='<td style="font-size:10px">'+esc(t.ticker||'')+'</td>';
      h+='<td>'+esc(t.side||'')+'</td>';
      h+='<td>'+(t.count||1)+'</td>';
      h+='<td>$'+(t.entry||0).toFixed(2)+'</td>';
      h+='<td>$'+(t.exit||0).toFixed(2)+'</td>';
      h+='<td class="'+cls(t.gross_pnl||0)+'">'+(t.gross_pnl>=0?'+':'')+( t.gross_pnl||0).toFixed(4)+'</td>';
      h+='<td class="red">-'+(t.buy_fee||0).toFixed(4)+'</td>';
      h+='<td class="red">-'+(t.sell_fee||0).toFixed(4)+'</td>';
      h+='<td class="'+pc+'" style="font-weight:700">'+(net>=0?'+':'')+net.toFixed(4)+'</td>';
      var gc2=cls(t.gain_pct||0);
      h+='<td class="'+gc2+'" style="font-weight:700">'+(t.gain_pct>=0?'+':'')+(t.gain_pct||0).toFixed(1)+'%</td>';
      h+='</tr>';
    });
    $('trades-body').innerHTML=h||'<tr><td colspan="11" class="gray" style="text-align:center">No trades yet</td></tr>';
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
  var next=new Date(now.getTime());
  if(nextQuarter>=60){
    next.setHours(now.getHours()+1,0,0,0);
  } else {
    next.setMinutes(nextQuarter,0,0);
  }
  return next;
}
function updateCountdown(){
  var secs=Math.max(0,Math.floor((getNextSettlement()-new Date())/1000));
  if(secs>900)secs=secs%900;
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
    logger.info(f"Bot starting [{mode}] -- buy cheap ${BUY_MIN}-${BUY_MAX}, sell +{SELL_THRESHOLD*100:.0f}%, {CONTRACTS} contracts, {CASH_RESERVE*100:.0f}% reserve")
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
