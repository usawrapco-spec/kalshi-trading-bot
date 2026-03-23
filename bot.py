"""
Ride and protect: track peak by ticker, sell on 10pt reversal from 30%+.
Buy cheap crypto contracts. Let winners run with no ceiling.
Peak gains stored in DB so restarts don't lose tracking.
Sell when: (1) peaked above 30% and dropped 10pts, or (2) expiry save.
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

# === SETTINGS ===
MIN_PRICE = 0.03
MAX_PRICE = 0.20
CYCLE_SECONDS = 5
MAX_CONTRACTS_PER_TRADE = 5
MAX_BUYS_PER_CYCLE = 15
MAX_DEPLOYMENT_PCT = 1.0
MAX_SPEND_PER_TRADE_PCT = 0.15
MAX_SPEND_PER_CYCLE = 25
MAX_OPEN_POSITIONS = 200

# === CRYPTO SERIES — the only thing we scan ===
CRYPTO_SERIES = ['KXBTC', 'KXETH', 'KXSOL', 'KXBTCD', 'KXETHD', 'KXSOLD',
                 'KXBTC15M', 'KXETH15M', 'KXSOL15M']

# === INIT ===
db = create_client(SUPABASE_URL, SUPABASE_KEY)
auth = KalshiAuth()
app = Flask(__name__)


def sf(val):
    try:
        return float(val) if val is not None else 0.0
    except:
        return 0.0


# === EXPIRY PARSING ===

def get_time_to_expiry(ticker):
    """Parse expiry from ticker. Format: KXBTCD-26MAR2305-T68499.99"""
    if isinstance(ticker, dict):
        ticker = ticker.get('ticker', '')
    match = re.search(r'-(\d{2})([A-Z]{3})(\d{2})(\d{2})-', ticker)
    if not match:
        return None
    day_str, mon_str, h1, h2 = match.groups()
    months = {'JAN': 1, 'FEB': 2, 'MAR': 3, 'APR': 4, 'MAY': 5, 'JUN': 6,
              'JUL': 7, 'AUG': 8, 'SEP': 9, 'OCT': 10, 'NOV': 11, 'DEC': 12}
    month = months.get(mon_str)
    if not month:
        return None
    hour = int(h1 + h2)
    extra_days = hour // 24
    hour = hour % 24
    try:
        expiry = datetime(2026, month, int(day_str) + extra_days, hour, 59, 59, tzinfo=timezone.utc)
        return max(0, (expiry - datetime.now(timezone.utc)).total_seconds())
    except:
        return None


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


def kalshi_delete(path):
    url = f"{KALSHI_HOST}/trade-api/v2{path}"
    headers = auth.get_headers("DELETE", f"/trade-api/v2{path}")
    resp = requests.delete(url, headers=headers, timeout=10)
    resp.raise_for_status()
    return resp.json() if resp.text else {}


def get_market(ticker):
    try:
        resp = kalshi_get(f"/markets/{ticker}")
        return resp.get('market', resp)
    except:
        return None


def place_order(ticker, side, action, price, count):
    if action == 'buy':
        count = min(count, MAX_CONTRACTS_PER_TRADE)
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
        time.sleep(0.5)  # Rate limit: avoid Kalshi blocking
        return {'order_id': order_id, 'status': status}
    except Exception as e:
        logger.error(f"ORDER FAILED: {action.upper()} {ticker} — {e}")
        time.sleep(0.5)  # Rate limit even on failure
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

def get_balance():
    try:
        resp = kalshi_get('/portfolio/balance')
        balance_cents = resp.get('balance', 0)
        return float(balance_cents) / 100.0
    except Exception as e:
        logger.error(f"Balance fetch failed: {e}")
        return 0.0


def get_trading_balance():
    balance = get_balance()
    logger.info(f"Balance: ${balance:.2f}")
    return balance


def get_owned():
    result = db.table('trades').select('ticker') \
        .eq('action', 'buy').is_('pnl', 'null').execute()
    return {t['ticker'] for t in (result.data or [])}


# === CLEAR NON-BTCD — sell everything that isn't KXBTCD on startup ===

def clear_non_btcd():
    """Sell any open position that isn't KXBTCD. Mark dead if no bid."""
    try:
        open_buys = db.table('trades').select('*') \
            .eq('action', 'buy').is_('pnl', 'null').execute()
        if not open_buys.data:
            logger.info("Clear non-BTCD: no open positions")
            return

        sold = 0
        dead = 0
        for trade in open_buys.data:
            ticker = trade['ticker']
            if 'KXBTCD' in ticker:
                continue  # keep these

            side = trade['side']
            count = trade.get('count') or 1
            entry_price = sf(trade['price'])

            bid = get_live_bid(ticker, side)
            if bid and bid > 0:
                sell_order_id = place_order(ticker, side, 'sell', bid, count)
                if not sell_order_id:
                    continue

                pnl = round((bid - entry_price) * count, 4)
                gain_pct = ((bid - entry_price) / entry_price) * 100 if entry_price > 0 else 0
                logger.info(f"CLEAR: sold {ticker} {side} x{count} @ ${bid:.2f} | pnl=${pnl:.4f}")

                try:
                    db.table('trades').insert({
                        'ticker': ticker, 'side': side, 'action': 'sell',
                        'price': float(bid), 'count': count,
                        'pnl': float(pnl), 'strategy': 'crypto',
                        'reason': f"CLEAR NON-BTCD {gain_pct:+.0f}%",
                        'sell_gain_pct': float(round(gain_pct, 1)),
                    }).execute()
                except Exception as e:
                    logger.error(f"Clear non-BTCD DB insert failed: {e}")

                try:
                    db.table('trades').update({
                        'pnl': 0.0,
                        'current_bid': float(bid),
                    }).eq('id', trade['id']).execute()
                except:
                    pass
                sold += 1
            else:
                loss = round(-entry_price * count, 4)
                try:
                    db.table('trades').update({
                        'pnl': loss,
                        'current_bid': 0,
                    }).eq('id', trade['id']).execute()
                except:
                    pass
                logger.info(f"CLEAR: marked {ticker} dead (loss=${loss:.4f})")
                dead += 1

        logger.info(f"CLEAR NON-BTCD DONE: sold {sold}, dead {dead}")
    except Exception as e:
        logger.error(f"Clear non-BTCD error: {e}")


# === SELL LOGIC ===
# Ride and protect: track peak by ticker in DB, sell on 10pt reversal from 30%+

def get_peak_gain(ticker, side):
    """Get stored peak gain from DB."""
    key = f"{ticker}_{side}"
    try:
        result = db.table('peak_gains').select('peak_pct').eq('key', key).execute()
        if result.data:
            return sf(result.data[0].get('peak_pct'))
    except:
        pass
    return None

def set_peak_gain(ticker, side, peak_pct):
    """Store peak gain in DB (survives restarts)."""
    key = f"{ticker}_{side}"
    try:
        existing = db.table('peak_gains').select('key').eq('key', key).execute()
        if existing.data:
            db.table('peak_gains').update({'peak_pct': float(peak_pct)}).eq('key', key).execute()
        else:
            db.table('peak_gains').insert({'key': key, 'peak_pct': float(peak_pct)}).execute()
    except Exception as e:
        logger.warning(f"Peak gain DB write failed for {key}: {e}")

def clear_peak_gain(ticker, side):
    """Remove peak gain after selling."""
    key = f"{ticker}_{side}"
    try:
        db.table('peak_gains').delete().eq('key', key).execute()
    except:
        pass

def should_sell(entry_price, current_bid, count, time_to_expiry_seconds, ticker='', side=''):
    if current_bid <= 0 or entry_price <= 0:
        return False, 0, None
    gain_pct = ((current_bid - entry_price) / entry_price) * 100

    # Track peak in DB (survives restarts)
    stored_peak = get_peak_gain(ticker, side)
    if stored_peak is None or gain_pct > stored_peak:
        set_peak_gain(ticker, side, gain_pct)
        peak = gain_pct
    else:
        peak = stored_peak

    drop = peak - gain_pct

    # Hit 30%+ and dropped 10 points from peak — the run is over, sell
    if peak >= 30 and drop >= 10:
        clear_peak_gain(ticker, side)
        return True, count, f"SELL +{gain_pct:.0f}% (peak +{peak:.0f}%)"

    # 5 min before expiry — sell EVERYTHING, green or red
    # A -20% sell recovers 80%. Expiry at $0 recovers nothing.
    if time_to_expiry_seconds is not None and time_to_expiry_seconds < 300:
        if current_bid > 0:
            clear_peak_gain(ticker, side)
            return True, count, f"EXPIRY EXIT {gain_pct:+.0f}%"

    return False, 0, None


# === BUY LOGIC ===

def find_buy_candidates(markets):
    candidates = []
    for market in markets:
        ticker = market.get('ticker', '')
        if 'KXMVE' in ticker:
            continue

        yes_ask = float(market.get('yes_ask_dollars', '0') or '0')
        yes_bid = float(market.get('yes_bid_dollars', '0') or '0')
        no_ask = float(market.get('no_ask_dollars', '0') or '0')
        no_bid = float(market.get('no_bid_dollars', '0') or '0')

        # Tight spread = active contract. Bid must be 80%+ of ask.
        if MIN_PRICE <= yes_ask <= MAX_PRICE and yes_bid >= yes_ask * 0.70:
            candidates.append({'ticker': ticker, 'side': 'yes', 'price': yes_ask, 'bid': yes_bid})
        if MIN_PRICE <= no_ask <= MAX_PRICE and no_bid >= no_ask * 0.70:
            candidates.append({'ticker': ticker, 'side': 'no', 'price': no_ask, 'bid': no_bid})

    logger.info(f"Filter: {len(markets)} markets -> {len(candidates)} candidates (3-20c, 70% spread)")
    return candidates


# === FETCH CRYPTO MARKETS ===

def fetch_crypto_markets():
    all_markets = []
    for series in CRYPTO_SERIES:
        try:
            resp = kalshi_get(f'/markets?series_ticker={series}&status=open&limit=200')
            all_markets.extend(resp.get('markets', []))
        except Exception as e:
            logger.error(f"Fetch {series} failed: {e}")
    logger.info(f"Fetched {len(all_markets)} crypto markets")
    return all_markets


# === SELL EVERYTHING — dump all Kalshi positions on startup ===

def sell_everything():
    """Cancel all pending orders, then sell every position on Kalshi."""
    # Cancel pending orders first
    cancel_all_resting()

    # Get all positions from Kalshi and sell
    try:
        resp = kalshi_get('/portfolio/positions?limit=1000')
        positions = resp.get('market_positions', [])
        sold = 0
        no_bid = 0
        for pos in positions:
            ticker = pos.get('ticker', '')
            qty = abs(pos.get('total_traded', 0))
            if qty <= 0:
                continue
            side = 'yes' if pos.get('market_exposure', 0) > 0 else 'no'
            bid = get_live_bid(ticker, side)
            if bid and bid > 0:
                place_order(ticker, side, 'sell', bid, qty)
                logger.info(f"SOLD: {ticker} {side} x{qty} @ ${bid:.2f}")
                sold += 1
            else:
                logger.info(f"NO BID: {ticker} {side} x{qty}")
                no_bid += 1
        logger.info(f"SELL EVERYTHING DONE: sold {sold}, no bid {no_bid}")
    except Exception as e:
        logger.error(f"SELL EVERYTHING ERROR: {e}")

    # Mark all DB positions as closed
    try:
        open_buys = db.table('trades').select('*') \
            .eq('action', 'buy').is_('pnl', 'null').execute()
        for trade in (open_buys.data or []):
            entry_price = sf(trade['price'])
            count = trade.get('count') or 1
            loss = round(-entry_price * count, 4)
            db.table('trades').update({
                'pnl': loss,
                'current_bid': 0,
            }).eq('id', trade['id']).execute()
        logger.info(f"Cleared {len(open_buys.data or [])} DB positions")
    except Exception as e:
        logger.error(f"DB clear error: {e}")


# === CANCEL ALL RESTING ORDERS — free locked cash ===

def cancel_all_resting():
    """Cancel all resting (unfilled) orders on Kalshi to free cash."""
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


# === CLEAR DEAD — mark low-bid positions as dead on startup ===

def clear_dead():
    """Mark all positions with current_bid <= $0.02 as dead losses."""
    try:
        open_buys = db.table('trades').select('*') \
            .eq('action', 'buy').is_('pnl', 'null').execute()
        if not open_buys.data:
            return
        dead = 0
        for pos in open_buys.data:
            bid = sf(pos.get('current_bid'))
            if bid <= 0.02:
                entry_price = sf(pos['price'])
                count = pos.get('count') or 1
                loss = round(-entry_price * count, 4)
                db.table('trades').update({
                    'pnl': loss,
                    'current_bid': 0,
                }).eq('id', pos['id']).execute()
                logger.info(f"DEAD: {pos['ticker']} bid=${bid:.2f}")
                dead += 1
        logger.info(f"Clear dead: marked {dead} positions")
    except Exception as e:
        logger.error(f"Clear dead error: {e}")


# === STARTUP PURGE ===

def startup_purge():
    """Mark bid=0 positions as losses."""
    try:
        open_buys = db.table('trades').select('*') \
            .eq('action', 'buy').is_('pnl', 'null').execute()
        if not open_buys.data:
            logger.info("Startup purge: no open positions")
            return

        purged = 0
        for trade in open_buys.data:
            entry_price = sf(trade['price'])
            count = trade.get('count') or 1
            current_bid = sf(trade.get('current_bid'))

            # Ghost position — bid is 0, mark as loss
            if current_bid <= 0:
                loss = round(-entry_price * count, 4)
                db.table('trades').update({
                    'pnl': round(loss, 4),
                    'current_bid': 0,
                }).eq('id', trade['id']).execute()
                purged += 1
                continue

        logger.info(f"Startup purge: {purged} ghosts marked as loss")
    except Exception as e:
        logger.error(f"Startup purge error: {e}")


# === RESYNC — reimport positions from Kalshi into DB ===

def resync_positions():
    """Pull real positions from Kalshi into our DB so the bot can track and sell them."""
    try:
        resp = kalshi_get('/portfolio/positions?limit=1000')
        positions = resp.get('market_positions', [])

        # Get tickers already tracked in DB
        existing = db.table('trades').select('ticker') \
            .eq('action', 'buy').is_('pnl', 'null').execute()
        tracked = {t['ticker'] for t in (existing.data or [])}

        imported = 0
        for pos in positions:
            ticker = pos.get('ticker', '')
            quantity = pos.get('total_traded', 0)
            if quantity <= 0:
                continue
            if ticker in tracked:
                continue

            side = 'yes' if pos.get('market_exposure', 0) > 0 else 'no'
            exposure = abs(pos.get('market_exposure', 0))
            price = exposure / quantity / 100 if quantity > 0 else 0

            try:
                db.table('trades').insert({
                    'ticker': ticker,
                    'side': side,
                    'action': 'buy',
                    'price': round(price, 4),
                    'count': quantity,
                    'strategy': 'resync',
                    'reason': f"RESYNC from Kalshi {side} x{quantity} @ ${price:.4f}",
                }).execute()
                tracked.add(ticker)
                imported += 1
                logger.info(f"RESYNC: {ticker} {side} x{quantity} @ ${price:.4f}")
            except Exception as e:
                logger.error(f"RESYNC insert failed for {ticker}: {e}")

        logger.info(f"RESYNC COMPLETE: imported {imported} positions from Kalshi")
    except Exception as e:
        logger.error(f"RESYNC ERROR: {e}")


# === SYNC DB WITH KALSHI ===

def sync_with_kalshi():
    """Check each DB open position against Kalshi. If we don't own it anymore, mark resolved."""
    try:
        open_buys = db.table('trades').select('*') \
            .eq('action', 'buy').is_('pnl', 'null').execute()
        if not open_buys.data:
            logger.info("Sync: no open positions in DB")
            return

        # Fetch all our positions from Kalshi in one call
        try:
            resp = kalshi_get('/portfolio/positions?limit=1000')
            kalshi_positions = resp.get('market_positions', [])
        except Exception as e:
            logger.error(f"Sync: failed to fetch Kalshi positions: {e}")
            return

        # Build set of tickers we actually own on Kalshi
        owned_on_kalshi = set()
        for pos in kalshi_positions:
            ticker = pos.get('ticker', '')
            yes_qty = pos.get('position', 0)
            no_qty = pos.get('total_traded', 0) - abs(yes_qty) if pos.get('total_traded') else 0
            # If any quantity exists, we own it
            if yes_qty != 0 or pos.get('total_traded', 0) > 0:
                owned_on_kalshi.add(ticker)

        cleared = 0
        for trade in open_buys.data:
            ticker = trade['ticker']
            entry_price = sf(trade['price'])
            count = trade.get('count') or 1

            if ticker in owned_on_kalshi:
                continue

            # Not on Kalshi anymore — mark as resolved
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
    logger.info("check_sells() — ride and protect, 30%+ peak, 10pt drop sells")
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

        # Get current bid
        if side == 'yes':
            current_bid = sf(market.get('yes_bid_dollars', '0'))
        else:
            current_bid = sf(market.get('no_bid_dollars', '0'))

        if current_bid <= 0:
            current_bid = get_live_bid(ticker, side)

        if current_bid <= 0:
            continue

        # Mark dead if bid collapsed
        if current_bid < entry_price * 0.50 and current_bid <= 0.02:
            loss = round(-entry_price * count, 4)
            try:
                db.table('trades').update({
                    'pnl': loss,
                    'current_bid': 0,
                }).eq('id', trade['id']).execute()
            except:
                pass
            logger.info(f"DEAD: {ticker} bid=${current_bid:.2f} too low (entry=${entry_price:.2f})")
            continue

        gain_pct = ((current_bid - entry_price) / entry_price) * 100
        time_to_expiry = get_time_to_expiry(ticker)

        # Update current price in DB
        try:
            db.table('trades').update({
                'current_bid': float(current_bid),
            }).eq('id', trade['id']).execute()
        except:
            pass

        # Decide sell
        do_sell, sell_qty, reason = should_sell(entry_price, current_bid, count, time_to_expiry, ticker=ticker, side=side)

        if do_sell and sell_qty > 0:
            pnl = round((current_bid - entry_price) * sell_qty, 4)
            sell_order_id = place_order(ticker, side, 'sell', current_bid, sell_qty)
            if not sell_order_id:
                continue

            logger.info(f"SELL: {ticker} {side} x{sell_qty} @ ${current_bid:.2f} | {reason} | pnl=${pnl:.4f}")

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
    trading_balance = get_trading_balance()
    owned = get_owned()
    num_open = len(owned)
    logger.info(f"Own {num_open} positions | balance=${trading_balance:.2f}")

    if num_open >= MAX_OPEN_POSITIONS:
        logger.info(f"At max positions ({MAX_OPEN_POSITIONS}), skipping buys")
        return

    candidates = find_buy_candidates(markets)
    # Remove already owned
    candidates = [c for c in candidates if c['ticker'] not in owned]
    # Sort by highest bid (most liquid)
    candidates.sort(key=lambda x: x['bid'], reverse=True)

    logger.info(f"Found {len(candidates)} buy candidates")

    max_exposure = trading_balance * MAX_DEPLOYMENT_PCT
    max_per_trade = trading_balance * MAX_SPEND_PER_TRADE_PCT

    open_buys = db.table('trades').select('price,count') \
        .eq('action', 'buy').is_('pnl', 'null').execute()
    current_deployed = sum(sf(t['price']) * (t['count'] or 1) for t in (open_buys.data or []))

    bought = 0
    cycle_spent = 0.0
    for c in candidates:
        if bought >= MAX_BUYS_PER_CYCLE:
            break
        if cycle_spent >= MAX_SPEND_PER_CYCLE:
            break
        if num_open + bought >= MAX_OPEN_POSITIONS:
            break

        count = MAX_CONTRACTS_PER_TRADE
        cost = c['price'] * count
        if cost > max_per_trade:
            count = int(max_per_trade / c['price'])
            if count < 1:
                continue
            cost = c['price'] * count
        if current_deployed + cost > max_exposure:
            continue
        if cost > trading_balance - current_deployed:
            continue

        result = place_order(c['ticker'], c['side'], 'buy', c['price'], count)
        if not result:
            continue

        if result['status'] == 'resting':
            # Not filled — cancel immediately to free cash
            try:
                kalshi_delete(f"/portfolio/orders/{result['order_id']}")
                logger.info(f"CANCELLED RESTING: {c['ticker']} — no instant fill")
            except Exception as e:
                logger.error(f"Cancel resting failed: {e}")
            continue

        if result['status'] != 'executed':
            logger.info(f"SKIP: {c['ticker']} status={result['status']}, not logging")
            continue

        logger.info(f"BUY: {c['ticker']} {c['side']} x{count} @ ${c['price']:.2f} (bid=${c['bid']:.2f})")
        try:
            db.table('trades').insert({
                'ticker': c['ticker'], 'side': c['side'], 'action': 'buy',
                'price': float(c['price']), 'count': count,
                'strategy': 'crypto',
                'reason': f"BUY {c['side'].upper()} @ ${c['price']:.2f} bid=${c['bid']:.2f}",
                'current_bid': float(c['bid']),
            }).execute()
            owned.add(c['ticker'])
            current_deployed += cost
            cycle_spent += cost
            bought += 1
        except Exception as e:
            logger.error(f"Buy DB insert failed: {e}")

    logger.info(f"Bought {bought}, spent ${cycle_spent:.2f}, deployed ${current_deployed:.2f}/{max_exposure:.2f}")


# === MAIN CYCLE ===

_stale_cleaned = False

def run_cycle():
    global _stale_cleaned
    balance = get_trading_balance()
    logger.info(f"=== CYCLE START === Balance: ${balance:.2f}")

    # Force sync on first cycle in case startup missed it
    if not _stale_cleaned:
        try:
            sync_with_kalshi()
            _stale_cleaned = True
        except Exception as e:
            logger.error(f"First cycle sync error: {e}")

    try:
        check_sells()
    except Exception as e:
        logger.error(f"Sell check error: {e}")

    try:
        markets = fetch_crypto_markets()
        run_buys(markets)
    except Exception as e:
        logger.error(f"Buy error: {e}")

    balance = get_trading_balance()
    logger.info(f"=== CYCLE END === Balance: ${balance:.2f}")


# === DASHBOARD ===

@app.route('/')
def health():
    return 'OK'


@app.route('/api/status')
def api_status():
    try:
        balance = get_balance()

        sells = db.table('trades').select('pnl') \
            .eq('action', 'sell').not_.is_('pnl', 'null').execute()
        sell_data = sells.data or []
        net_pnl = sum(sf(t['pnl']) for t in sell_data)
        wins = sum(1 for t in sell_data if sf(t['pnl']) > 0)
        losses = sum(1 for t in sell_data if sf(t['pnl']) < 0)

        open_buys = db.table('trades').select('id,price,count,current_bid') \
            .eq('action', 'buy').is_('pnl', 'null').execute()
        open_data = open_buys.data or []
        live_positions = [t for t in open_data if sf(t.get('current_bid')) > 0]
        open_count = len(live_positions)
        positions_value = round(sum(sf(t.get('current_bid')) * (t.get('count') or 1) for t in live_positions), 2)
        positions_cost = round(sum(sf(t.get('price')) * (t.get('count') or 1) for t in live_positions), 2)

        # Kalshi balance = available cash from API (already excludes locked positions)
        cash = round(balance, 2)

        return jsonify({
            'balance': round(balance + positions_cost, 2),
            'net_pnl': round(net_pnl, 4),
            'wins': wins,
            'losses': losses,
            'open_count': open_count,
            'positions_value': positions_value,
            'positions_cost': positions_cost,
            'cash': cash,
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/trades')
def api_trades():
    try:
        result = db.table('trades').select('*') \
            .order('created_at', desc=True).limit(200).execute()
        return jsonify(result.data or [])
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/open')
def api_open():
    try:
        result = db.table('trades').select('*') \
            .eq('action', 'buy').is_('pnl', 'null').execute()
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
        return jsonify({'error': str(e)}), 500


@app.route('/dashboard')
def dashboard():
    return DASHBOARD_HTML


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Profit Maximizer</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&display=swap');
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0a0a0a;color:#e0e0e0;font-family:'JetBrains Mono','SF Mono','Fira Code',monospace;padding:16px 20px;font-size:13px}

@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
.live-dot{display:inline-block;width:8px;height:8px;background:#00d673;border-radius:50%;margin-right:6px;animation:pulse 2s infinite}

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
.status-bar .dot-live{width:6px;height:6px;background:#00d673;border-radius:50%;animation:pulse 2s infinite}
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
  <div class="sub"><span class="live-dot"></span>PROFIT MAXIMIZER &mdash; 5s cycles &mdash; hold for big gains, protect at 50%+</div>
  <div class="portfolio-value" id="p-total">...</div>
  <div class="portfolio-pnl" id="p-pnl">...</div>
  <div class="portfolio-breakdown">
    <div class="item"><div class="label">Positions</div><div class="val" id="p-positions">...</div></div>
    <div class="item"><div class="label">Cash</div><div class="val" id="p-cash">...</div></div>
    <div class="item"><div class="label">Record</div><div class="val" id="p-record">...</div></div>
  </div>
</div>

<div class="panel">
  <div class="panel-header"><h2>Open Positions</h2><div class="count" id="open-count"></div></div>
  <div class="panel-body"><table><thead><tr>
    <th>Ticker</th><th>Side</th><th>Qty</th><th>Entry</th><th>Bid</th><th>P&amp;L</th><th>Gain</th>
  </tr></thead><tbody id="open-body"><tr><td colspan="7" class="loading">Loading...</td></tr></tbody></table></div>
</div>

<div class="panel">
  <div class="panel-header"><h2>Recent Trades</h2><div class="count" id="trades-count"></div></div>
  <div class="panel-body"><table><thead><tr>
    <th>Time</th><th>Ticker</th><th>Side</th><th>Qty</th><th>P&amp;L</th><th>Gain</th>
  </tr></thead><tbody id="trades-body"><tr><td colspan="6" class="loading">Loading...</td></tr></tbody></table></div>
</div>

<div class="equity-section">
  <h2>Equity Curve</h2>
  <canvas id="equity-chart"></canvas>
</div>

<div class="status-bar">
  <div class="status-item"><span class="dot-live"></span> LIVE</div>
  <div class="status-item">Buy: 3-20c, 70% spread</div>
  <div class="status-item">Strategy: hold, protect 50%+</div>
  <div class="status-item">Expiry save: 2min</div>
  <div class="status-item">Max: 5 contracts</div>
  <div class="status-item">All crypto series</div>
  <div class="status-item">Last: <span id="last-update">&mdash;</span></div>
</div>
<div class="footer">Profit Maximizer &mdash; auto-refresh 15s</div>

<script>
function $(id){return document.getElementById(id)}
function cls(v){return v>0?'green':v<0?'red':'gray'}
function esc(s){var d=document.createElement('div');d.textContent=s;return d.innerHTML}

async function fetchJSON(url){
  try{var r=await fetch(url);return await r.json()}
  catch(e){console.error(url,e);return null}
}

async function refresh(){
  var [status,open,trades]=await Promise.all([
    fetchJSON('/api/status'),
    fetchJSON('/api/open'),
    fetchJSON('/api/trades')
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
    completed.slice(0,50).forEach(function(t){
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
    logger.info("Bot starting — ride and protect, all crypto, 5s cycles, peak gains in DB")
    cancel_all_resting()
    clear_dead()
    sync_with_kalshi()
    cycle_count = 0
    while True:
        try:
            run_cycle()
        except Exception as e:
            logger.error(f"Cycle error: {e}")
        cycle_count += 1
        time.sleep(CYCLE_SECONDS)


if __name__ == '__main__':
    bot_thread = Thread(target=bot_loop, daemon=True)
    bot_thread.start()
    app.run(host='0.0.0.0', port=PORT)
