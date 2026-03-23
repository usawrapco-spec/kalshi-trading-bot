import os, time, logging, math, requests, traceback
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

MIN_PRICE = 0.02
MAX_PRICE = 0.50
CYCLE_SECONDS = 30

# === FAST TURNOVER DEPLOYMENT — sell fast, recycle capital, repeat ===
MAX_DEPLOYMENT_PCT = 0.75       # Deploy up to 75% of balance
MIN_CASH_RESERVE_PCT = 0.25     # 25% protected (saved profits live here)
MAX_CONTRACTS_PER_TRADE = 5     # Max 5 contracts (was 3 — sells work now)
MIN_CONTRACTS_PER_TRADE = 2     # Minimum 2 contracts per trade
MAX_SPEND_PER_TRADE_PCT = 0.15  # Max 15% of trading_balance per trade (fewer bigger bets)
MAX_SPEND_PER_CYCLE = 25
MAX_TRADES_PER_CYCLE = 10       # High volume: buy everything that qualifies
MAX_OPEN_POSITIONS = 200

# === SELL THRESHOLDS ===
# Tiered: 100%+ = instant sell, <100% near expiry + profitable = save, otherwise HOLD
# See decide_sell() for full logic

# === PROFIT COMPOUNDING ===
PROFIT_SAVE_PCT = 0.20          # 20% of every win gets banked permanently
PROFIT_REINVEST_PCT = 0.80      # 80% of every win goes back to trading

# === INIT ===
db = create_client(SUPABASE_URL, SUPABASE_KEY)
auth = KalshiAuth()
app = Flask(__name__)

# Scanner stats — updated each cycle for dashboard
last_scan = {'total': 0, 'categories': {}, 'timestamp': None}


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

        # Resolve any non-supported positions (trending/weather leftovers)
        all_open = db.table('trades').select('id,ticker,strategy') \
            .eq('action', 'buy').is_('pnl', 'null').execute()
        if all_open.data:
            # All strategies from categorize_market are valid now
            valid_strategies = {
                'crypto', 'mm_scalp',  # legacy
                'crypto direction', 'crypto bracket', 'NCAA basketball',
                'march madness futures', 'weather', 'oil', 'sports',
                'elections', 'tennis', 'economics', 'other',
            }
            stale = [t for t in all_open.data if t.get('strategy') not in valid_strategies]
            for t in stale:
                db.table('trades').update({
                    'pnl': 0.0,
                    'reason': 'RESOLVED — unsupported strategy reset',
                }).eq('id', t['id']).execute()
            if stale:
                logger.info(f"Resolved {len(stale)} non-supported positions")

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


def get_saved_balance():
    """Saved balance = 20% of all winning sells. Computed from DB, survives restarts.
    This money is PROTECTED — never traded with."""
    sells = db.table('trades').select('pnl') \
        .eq('action', 'sell').not_.is_('pnl', 'null').execute()
    total_wins = sum(max(0.0, sf(t['pnl'])) for t in (sells.data or []))
    return round(total_wins * PROFIT_SAVE_PCT, 4)


def get_trading_balance():
    """Trading balance = Kalshi balance minus saved (protected) balance.
    Position sizing uses ONLY this, never the saved portion."""
    total = get_balance()
    saved = get_saved_balance()
    trading = max(0.0, total - saved)
    logger.info(f"Balance split: ${total:.2f} total | ${trading:.2f} trading | ${saved:.2f} SAVED (protected)")
    return total, trading, saved


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
    # HARD SAFETY: block 15M contracts at the gate
    if '15M' in ticker:
        logger.warning(f"BLOCKED at order gate: {ticker} — 15-min contracts disabled")
        return None
    # HARD SAFETY: cap buy orders at 15 contracts max (sells can be any size)
    if action == 'buy':
        count = min(count, 15)
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


# === UNIVERSAL MARKET SCANNER ===

def categorize_market(ticker):
    """Categorize any Kalshi market by ticker pattern."""
    if '15M' in ticker: return '15-min crypto (BLOCKED)'
    elif any(x in ticker for x in ['KXBTCD', 'KXETHD', 'KXSOLD']): return 'crypto direction'
    elif any(x in ticker for x in ['KXBTC-', 'KXETH-', 'KXSOL-']): return 'crypto bracket'
    elif 'KXNCAAMBGAME' in ticker or 'KXNCAAMB' in ticker or 'KXCBB' in ticker: return 'NCAA basketball'
    elif 'KXMARMAD' in ticker or 'KXMM' in ticker: return 'march madness futures'
    elif 'KXHIGH' in ticker or 'KXLOWT' in ticker: return 'weather'
    elif 'KXMVE' in ticker: return 'multivariate (SKIP)'
    elif any(x in ticker for x in ['OIL', 'WTI', 'CRUDE', 'BRENT']): return 'oil'
    elif any(x in ticker for x in ['KXNFL', 'KXNBA', 'KXMLB', 'KXNHL', 'KXSOCCER', 'KXMLS', 'KXEPL', 'KXUCL']): return 'sports'
    elif any(x in ticker for x in ['KXELECT', 'KXPRES', 'KXGOV', 'KXSEN', 'KXREFERENDUM']): return 'elections'
    elif any(x in ticker for x in ['KXTENNIS', 'KXATP', 'KXWTA']): return 'tennis'
    elif any(x in ticker for x in ['KXFED', 'KXCPI', 'KXGDP', 'KXJOBS', 'KXINFL']): return 'economics'
    else: return 'other'


CRYPTO_SERIES = ['KXBTC', 'KXETH', 'KXSOL', 'KXBTCD', 'KXETHD', 'KXSOLD']


# ============================================
# 10% SKIMMER — Separate from crypto moonshot
# Uses skimmer_trades table, own logic, own limits
# ============================================

SKIMMER_SELL_TARGET = 0.10      # Sell at +10%
SKIMMER_STOP_LOSS = -0.15       # Cut loss at -15%
SKIMMER_MAX_CONTRACTS = 5       # Per trade
SKIMMER_MAX_OPEN = 20           # Max simultaneous skimmer positions
SKIMMER_MAX_PER_CYCLE = 5       # Max new buys per cycle
SKIMMER_MIN_VOLUME = 100        # Minimum 24h volume
SKIMMER_MAX_SPREAD = 0.08       # Max 8 cent spread (tight enough to exit)
SKIMMER_PRICE_RANGE = (0.15, 0.50)  # Sweet spot — enough room to move 10% either way

# Skimmer stats — updated each cycle for dashboard
last_skimmer_scan = {'total': 0, 'candidates': 0, 'bought': 0, 'timestamp': None}


def fetch_all_markets():
    """Fetch ALL open Kalshi markets (every category)."""
    all_markets = []
    cursor = None

    while True:
        params = 'status=open&limit=1000'
        if cursor:
            params += f'&cursor={cursor}'

        try:
            resp = kalshi_get(f'/markets?{params}')
        except Exception as e:
            logger.error(f"All-market fetch failed: {e}")
            break

        markets = resp.get('markets', [])
        all_markets.extend(markets)

        cursor = resp.get('cursor')
        if not cursor or not markets:
            break

    logger.info(f"SKIMMER SCAN: fetched {len(all_markets)} total markets from Kalshi")
    return all_markets


def skimmer_check_sells():
    """Check all open skimmer positions — sell at +10% or cut at -15%"""
    open_positions = db.table('skimmer_trades').select('*').eq('action', 'buy').is_('pnl', 'null').execute()

    sold = 0
    cut = 0

    for pos in (open_positions.data or []):
        ticker = pos['ticker']
        side = pos['side']
        entry = float(pos['price'])
        count = pos['count'] or 1

        # Get LIVE bid from Kalshi
        bid = get_live_bid(ticker, side)
        if bid is None or bid <= 0:
            continue

        gain_pct = ((bid - entry) / entry) * 100

        # Update current_bid in DB
        try:
            db.table('skimmer_trades').update({
                'current_bid': float(bid),
                'sell_gain_pct': round(gain_pct, 1)
            }).eq('id', pos['id']).execute()
        except:
            pass

        # TARGET HIT — sell at +10%
        if gain_pct >= 10:
            order_id = place_order(ticker, side, 'sell', bid, count)
            if not order_id:
                continue
            profit = (bid - entry) * count
            try:
                db.table('skimmer_trades').insert({
                    'ticker': ticker, 'side': side, 'action': 'sell',
                    'price': float(bid), 'count': count,
                    'pnl': round(profit, 4),
                    'sell_gain_pct': round(gain_pct, 1),
                    'category': pos.get('category', 'unknown'),
                    'reason': f'TARGET +{gain_pct:.0f}%'
                }).execute()
                # Mark the buy as resolved
                db.table('skimmer_trades').update({
                    'pnl': round(profit, 4),
                    'sell_gain_pct': round(gain_pct, 1)
                }).eq('id', pos['id']).execute()
            except Exception as e:
                logger.error(f"Skimmer sell DB error: {e}")
            sold += 1
            logger.info(f"SKIM SOLD: {ticker} {side} x{count} | +{gain_pct:.0f}% | profit=${profit:.4f}")

        # STOP LOSS — cut at -15%
        elif gain_pct <= -15:
            order_id = place_order(ticker, side, 'sell', bid, count)
            if not order_id:
                continue
            loss = (bid - entry) * count
            try:
                db.table('skimmer_trades').insert({
                    'ticker': ticker, 'side': side, 'action': 'sell',
                    'price': float(bid), 'count': count,
                    'pnl': round(loss, 4),
                    'sell_gain_pct': round(gain_pct, 1),
                    'category': pos.get('category', 'unknown'),
                    'reason': f'STOP LOSS {gain_pct:.0f}%'
                }).execute()
                db.table('skimmer_trades').update({
                    'pnl': round(loss, 4),
                    'sell_gain_pct': round(gain_pct, 1)
                }).eq('id', pos['id']).execute()
            except Exception as e:
                logger.error(f"Skimmer stop loss DB error: {e}")
            cut += 1
            logger.info(f"SKIM CUT: {ticker} {side} x{count} | {gain_pct:.0f}% | loss=${loss:.4f}")

        # Also check for settlement (market closed)
        else:
            try:
                market = get_market(ticker)
                if market:
                    status = market.get('status', '')
                    result_val = market.get('result', '')
                    if status in ('closed', 'settled', 'finalized') or result_val:
                        if result_val == side:
                            pnl = round((1.0 - entry) * count, 4)
                            reason = f"WIN settled (entry ${entry:.2f})"
                            settle_price = 1.0
                        elif result_val:
                            pnl = round(-entry * count, 4)
                            reason = f"LOSS expired (entry ${entry:.2f})"
                            settle_price = 0.0
                        else:
                            continue
                        db.table('skimmer_trades').insert({
                            'ticker': ticker, 'side': side, 'action': 'sell',
                            'price': float(settle_price), 'count': count,
                            'pnl': float(pnl),
                            'sell_gain_pct': round(((settle_price - entry) / entry) * 100, 1),
                            'category': pos.get('category', 'unknown'),
                            'reason': reason
                        }).execute()
                        db.table('skimmer_trades').update({
                            'pnl': float(pnl),
                            'sell_gain_pct': round(((settle_price - entry) / entry) * 100, 1)
                        }).eq('id', pos['id']).execute()
                        logger.info(f"SKIM SETTLED: {ticker} {side} | {reason} | pnl=${pnl:.4f}")
            except:
                pass

    if sold > 0 or cut > 0:
        logger.info(f"SKIMMER SELLS: {sold} targets hit, {cut} stops cut")


def skimmer_scan_and_buy(trading_balance):
    """Scan ALL live markets, buy the most liquid ones with tight spreads"""
    global last_skimmer_scan

    # Check how many skimmer positions are already open
    open_rows = db.table('skimmer_trades').select('id,ticker').eq('action', 'buy').is_('pnl', 'null').execute()
    open_count = len(open_rows.data or [])
    if open_count >= SKIMMER_MAX_OPEN:
        logger.info(f"SKIMMER: {open_count} positions open, max {SKIMMER_MAX_OPEN} — skipping buys")
        return

    # Fetch ALL open markets
    all_markets = fetch_all_markets()

    candidates = []
    for market in all_markets:
        ticker = market.get('ticker', '')

        # Skip stuff we don't want
        if '15M' in ticker: continue     # 15-min crypto = bad
        if 'KXMVE' in ticker: continue   # multivariate parlays

        yes_bid = sf(market.get('yes_bid_dollars', '0'))
        yes_ask = sf(market.get('yes_ask_dollars', '0'))
        no_bid = sf(market.get('no_bid_dollars', '0'))
        no_ask = sf(market.get('no_ask_dollars', '0'))
        volume = sf(market.get('volume', 0)) or sf(market.get('volume_24h', 0))

        if volume < SKIMMER_MIN_VOLUME:
            continue

        # Check YES side
        if SKIMMER_PRICE_RANGE[0] <= yes_ask <= SKIMMER_PRICE_RANGE[1] and yes_bid > 0:
            spread = yes_ask - yes_bid
            if 0 < spread <= SKIMMER_MAX_SPREAD:
                candidates.append({
                    'ticker': ticker,
                    'side': 'yes',
                    'price': yes_ask,
                    'bid': yes_bid,
                    'spread': spread,
                    'volume': volume,
                    'category': categorize_market(ticker)
                })

        # Check NO side
        if SKIMMER_PRICE_RANGE[0] <= no_ask <= SKIMMER_PRICE_RANGE[1] and no_bid > 0:
            spread = no_ask - no_bid
            if 0 < spread <= SKIMMER_MAX_SPREAD:
                candidates.append({
                    'ticker': ticker,
                    'side': 'no',
                    'price': no_ask,
                    'bid': no_bid,
                    'spread': spread,
                    'volume': volume,
                    'category': categorize_market(ticker)
                })

    # Sort by volume (most liquid = easiest to sell)
    candidates.sort(key=lambda x: x['volume'], reverse=True)

    # Don't buy tickers we already have open in skimmer
    open_tickers = set(t['ticker'] for t in (open_rows.data or []))
    candidates = [c for c in candidates if c['ticker'] not in open_tickers]

    # Buy top candidates
    buys = 0
    spots_left = SKIMMER_MAX_OPEN - open_count

    for c in candidates:
        if buys >= min(SKIMMER_MAX_PER_CYCLE, spots_left):
            break

        contracts = min(SKIMMER_MAX_CONTRACTS, max(1, int((trading_balance * 0.05) / c['price'])))

        order_id = place_order(c['ticker'], c['side'], 'buy', c['price'], contracts)
        if not order_id:
            continue

        try:
            db.table('skimmer_trades').insert({
                'ticker': c['ticker'], 'side': c['side'], 'action': 'buy',
                'price': c['price'], 'count': contracts,
                'category': c['category'],
                'reason': f"vol={c['volume']:.0f} spread=${c['spread']:.2f}"
            }).execute()
        except Exception as e:
            logger.error(f"Skimmer buy DB insert failed: {e}")
            continue

        buys += 1
        logger.info(f"SKIM BUY: {c['ticker']} {c['side']} x{contracts} @ ${c['price']:.2f} | "
                    f"vol={c['volume']:.0f} | spread=${c['spread']:.2f} | {c['category']}")

    last_skimmer_scan = {
        'total': len(all_markets),
        'candidates': len(candidates),
        'bought': buys,
        'timestamp': datetime.now(timezone.utc).isoformat()
    }
    logger.info(f"SKIMMER: scanned {len(all_markets)} markets, {len(candidates)} candidates, bought {buys}")


def fetch_crypto_markets():
    """Fetch crypto markets only — direction + bracket series. Skip 15M."""
    all_markets = []
    cursor = None

    while True:
        params = 'status=open&limit=1000'
        if cursor:
            params += f'&cursor={cursor}'

        try:
            resp = kalshi_get(f'/markets?{params}')
        except Exception as e:
            logger.error(f"Market fetch failed: {e}")
            break

        markets = resp.get('markets', [])
        # Filter to crypto only, skip 15M
        for m in markets:
            ticker = m.get('ticker', '')
            if '15M' in ticker:
                continue
            if any(ticker.startswith(series) or series in ticker for series in CRYPTO_SERIES):
                all_markets.append(m)

        cursor = resp.get('cursor')
        if not cursor or not markets:
            break

    logger.info(f"CRYPTO MARKETS: {len(all_markets)} (filtered from full Kalshi catalog)")

    # Categorize for dashboard
    categories = {}
    for m in all_markets:
        ticker = m.get('ticker', '')
        cat = categorize_market(ticker)
        categories[cat] = categories.get(cat, 0) + 1

    for cat, count in sorted(categories.items(), key=lambda x: -x[1]):
        logger.info(f"  {cat}: {count} markets")

    last_scan['total'] = len(all_markets)
    last_scan['categories'] = categories
    last_scan['timestamp'] = datetime.now(timezone.utc).isoformat()

    return all_markets


# === BUY LOGIC ===

def calculate_position_size(contract_price, available_balance, volume=0, strategy='crypto'):
    """Default 3 contracts, max 5. Fast turnover — moderate size, quick exits."""
    if contract_price <= 0:
        return 3
    target_spend = available_balance * 0.06  # 6% of balance
    calculated = int(target_spend / contract_price)
    result = max(MIN_CONTRACTS_PER_TRADE, min(calculated, MAX_CONTRACTS_PER_TRADE))
    # HARD SAFETY: absolutely never exceed 5
    return min(result, 5)



def run_buys(markets):
    """Crypto buy logic — buy any qualifying contract. Simple filters, high volume."""
    total_balance, trading_balance, saved_balance = get_trading_balance()
    owned = get_owned()
    num_open = len(owned)
    already_owned = 0
    logger.info(f"[CRYPTO] Own {num_open} tickers | trading=${trading_balance:.2f} saved=${saved_balance:.2f} (PROTECTED, never traded)")

    if num_open >= MAX_OPEN_POSITIONS:
        logger.info(f"At max open positions ({MAX_OPEN_POSITIONS}), skipping buys")
        return

    buys = []
    skipped_15m = 0
    skipped_mve = 0
    no_price = 0
    no_bid = 0
    price_ok = 0

    for m in markets:
        ticker = m.get('ticker', '')

        # HARD BLOCK: no 15-minute contracts ever
        if '15M' in ticker:
            skipped_15m += 1
            continue

        # Skip multivariate
        if 'KXMVE' in ticker:
            skipped_mve += 1
            continue

        if ticker in owned:
            already_owned += 1
            continue

        category = categorize_market(ticker)
        volume = sf(m.get('volume', 0)) or sf(m.get('volume_24h', 0))

        yes_ask = sf(m.get('yes_ask_dollars', '0'))
        yes_bid = sf(m.get('yes_bid_dollars', '0'))
        no_ask = sf(m.get('no_ask_dollars', '0'))
        no_bid = sf(m.get('no_bid_dollars', '0'))

        # Also try non-dollar fields as fallback (cents)
        if yes_ask == 0 and yes_bid == 0 and no_ask == 0 and no_bid == 0:
            yes_ask = sf(m.get('yes_ask', 0)) / 100.0 if sf(m.get('yes_ask', 0)) > 1 else sf(m.get('yes_ask', 0))
            yes_bid = sf(m.get('yes_bid', 0)) / 100.0 if sf(m.get('yes_bid', 0)) > 1 else sf(m.get('yes_bid', 0))
            no_ask = sf(m.get('no_ask', 0)) / 100.0 if sf(m.get('no_ask', 0)) > 1 else sf(m.get('no_ask', 0))
            no_bid = sf(m.get('no_bid', 0)) / 100.0 if sf(m.get('no_bid', 0)) > 1 else sf(m.get('no_bid', 0))

        # Collect qualifying sides: price 5-45 cents, has ANY bid
        candidates = []
        if 0.05 <= yes_ask <= 0.45 and yes_bid > 0:
            candidates.append(('yes', yes_ask, yes_bid))
        if 0.05 <= no_ask <= 0.45 and no_bid > 0:
            candidates.append(('no', no_ask, no_bid))

        if not candidates:
            # Track why it failed for debug
            has_any_ask = yes_ask > 0 or no_ask > 0
            has_any_bid = yes_bid > 0 or no_bid > 0
            if not has_any_ask:
                no_price += 1
            elif not has_any_bid:
                no_bid += 1
            else:
                price_ok += 1  # Has price but outside 5-45c range
            continue

        # Pick cheapest side (more room to grow)
        candidates.sort(key=lambda x: x[1])
        side, price, bid = candidates[0]

        buys.append({
            'ticker': ticker, 'side': side, 'price': price,
            'bid': bid, 'spread': price - bid, 'count': MAX_CONTRACTS_PER_TRADE,
            'volume': volume, 'strategy': category,
        })

    logger.info(f"FILTER DEBUG: total={len(markets)} | 15m_blocked={skipped_15m} | mve_skip={skipped_mve} | "
                f"owned={already_owned} | no_price={no_price} | no_bid={no_bid} | "
                f"out_of_range={price_ok} | final_candidates={len(buys)}")

    # Sort by cheapest first (most upside potential)
    buys.sort(key=lambda x: x['price'])

    # Limits based on trading_balance (excludes saved/protected money)
    max_exposure = trading_balance * MAX_DEPLOYMENT_PCT
    max_per_trade = trading_balance * MAX_SPEND_PER_TRADE_PCT

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
            affordable = int(max_per_trade / b['price'])
            if affordable < MIN_CONTRACTS_PER_TRADE:
                continue
            b['count'] = affordable
            cost = b['price'] * b['count']
        if current_deployed + cost > max_exposure:
            continue
        if cost > trading_balance - current_deployed:
            continue

        # FINAL SAFETY: hard cap at 5 contracts before ANY order
        b['count'] = min(b['count'], 5)
        cost = b['price'] * b['count']

        # Place real Kalshi order
        order_id = place_order(b['ticker'], b['side'], 'buy', b['price'], b['count'])
        if not order_id:
            continue

        strat_label = b['strategy'].upper()
        logger.info(f"BUY [{strat_label}]: {b['ticker']} {b['side']} x{b['count']} @ ${b['price']:.2f} (bid=${b['bid']:.2f} spread=${b['spread']:.2f} vol={b['volume']:.0f})")
        try:
            db.table('trades').insert({
                'ticker': b['ticker'], 'side': b['side'], 'action': 'buy',
                'price': float(b['price']), 'count': b['count'],
                'strategy': b['strategy'],
                'reason': f"{strat_label}: {b['side'].upper()} @ ${b['price']:.2f} bid=${b['bid']:.2f}",
                'last_seen_bid': float(b['bid']),
                'current_bid': float(b['bid']),
            }).execute()
            owned.add(b['ticker'])
            trading_balance -= cost
            current_deployed += cost
            cycle_spent += cost
            bought += 1
        except Exception as e:
            logger.error(f"Buy DB insert failed: {e}")

    logger.info(f"[CRYPTO] Bought {bought}, spent ${cycle_spent:.2f}, trading=${trading_balance:.2f}, deployed ${current_deployed:.2f}/{max_exposure:.2f}")

    # Log category breakdown of buys
    if bought > 0:
        buy_cats = {}
        for b in buys[:bought]:
            buy_cats[b['strategy']] = buy_cats.get(b['strategy'], 0) + 1
        for cat, cnt in sorted(buy_cats.items(), key=lambda x: -x[1]):
            logger.info(f"  Bought {cnt} from {cat}")


# === SELL LOGIC — SMART TIERED: 100% instant sell, save profits before expiry, let winners ride ===

sell_history = []  # Rolling last 20 sell gain percentages

EXPIRY_WINDOW_SECONDS = 60  # 1 minute — sell anything green before close


def get_time_to_expiry(market):
    """Returns seconds until market closes, or None if unknown."""
    close_time_str = market.get('close_time') or market.get('expiration_time')
    if not close_time_str:
        return None
    try:
        close_time_str = close_time_str.replace('Z', '+00:00')
        close_time = datetime.fromisoformat(close_time_str)
        now = datetime.now(timezone.utc)
        return max(0, (close_time - now).total_seconds())
    except:
        return None


def decide_sell(entry_price, current_bid, count, time_to_expiry, trade_id):
    """3 rules: moonshot at 100%, save ALL profit at 1 min, hold everything else.
    Returns (should_sell, sell_qty, reason)."""
    if current_bid <= 0 or entry_price <= 0:
        return False, 0, None

    gain_pct = ((current_bid - entry_price) / entry_price) * 100

    # RULE 1: Half sell at 100%+ (moonshot — lock profit, ride the rest)
    if gain_pct >= 100:
        sell_qty = max(1, count // 2)
        return True, sell_qty, f"HALF SELL +{gain_pct:.0f}% — riding {count - sell_qty} to the moon"

    # RULE 2: 1 minute before expiry — sell EVERYTHING profitable
    # Any profit at all. +1%, +5%, +60%, doesn't matter. Take it.
    if time_to_expiry is not None and time_to_expiry < EXPIRY_WINDOW_SECONDS:
        if gain_pct > 0:
            return True, count, f"LAST MINUTE +{gain_pct:.0f}% — saving profit ({int(time_to_expiry)}s left)"

    # RULE 3: Everything else — HOLD. Let it ride.
    return False, 0, None


def execute_sell(trade, ticker, side, entry_price, current_bid, sell_qty, total_count, gain_pct, reason):
    """Execute a sell order and update DB. Returns True on success."""
    pnl = round((current_bid - entry_price) * sell_qty, 4)

    sell_order_id = place_order(ticker, side, 'sell', current_bid, sell_qty)
    if not sell_order_id:
        logger.error(f"SELL ORDER FAILED — skipping {ticker}")
        return False

    logger.info(f"SELL: {ticker} {side} x{sell_qty} +{gain_pct:.0f}% pnl=${pnl:.4f} | {reason}")
    if pnl > 0:
        banked = pnl * PROFIT_SAVE_PCT
        reinvested = pnl * PROFIT_REINVEST_PCT
        logger.info(f"PROFIT SPLIT: ${pnl:.4f} total | ${banked:.4f} BANKED | ${reinvested:.4f} reinvested")
    try:
        db.table('trades').insert({
            'ticker': ticker, 'side': side, 'action': 'sell',
            'price': float(current_bid), 'count': sell_qty,
            'pnl': float(pnl), 'strategy': trade.get('strategy', 'crypto'),
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


def get_live_bid(ticker, side):
    """Fetch LIVE bid from Kalshi orderbook — don't trust stale market data."""
    try:
        resp = kalshi_get(f"/markets/{ticker}/orderbook?depth=3")
        if side == 'yes':
            bids = resp.get('yes', resp.get('orderbook', {}).get('yes', []))
        else:
            bids = resp.get('no', resp.get('orderbook', {}).get('no', []))
        # Orderbook format varies — try to extract best bid
        if isinstance(bids, list) and bids:
            # Each entry might be [price, qty] or {price: qty}
            if isinstance(bids[0], list):
                return float(bids[0][0]) / 100.0  # cents to dollars
            elif isinstance(bids[0], dict):
                prices = [float(k) for k in bids[0].keys()]
                return max(prices) / 100.0 if prices else 0.0
        return 0.0
    except Exception as e:
        logger.warning(f"Orderbook fetch failed for {ticker}: {e}")
        return 0.0


def cleanup_ghosts():
    """Mark expired positions that Kalshi already settled but bot missed."""
    open_buys = db.table('trades').select('id,ticker,side,price,count,current_bid') \
        .eq('action', 'buy').is_('pnl', 'null').execute()

    cleaned = 0
    for pos in (open_buys.data or []):
        bid = sf(pos.get('current_bid'))
        ticker = pos['ticker']
        entry = sf(pos['price'])
        count = pos.get('count') or 1

        if bid > 0:
            continue

        # Bid is 0 — check if market is settled on Kalshi
        try:
            market = get_market(ticker)
            if not market:
                continue
            status = market.get('status', '')
            result_val = market.get('result', '')

            if status in ('closed', 'settled', 'finalized') or result_val:
                side = pos['side']
                if result_val == side:
                    pnl = round((1.0 - entry) * count, 4)
                    reason = f"GHOST CLEANUP: WIN settled (entry ${entry:.2f})"
                    settle_price = 1.0
                elif result_val:
                    pnl = round(-entry * count, 4)
                    reason = f"GHOST CLEANUP: LOSS expired (entry ${entry:.2f})"
                    settle_price = 0.0
                else:
                    continue

                # Record the sell
                db.table('trades').insert({
                    'ticker': ticker, 'side': side, 'action': 'sell',
                    'price': float(settle_price), 'count': count,
                    'pnl': float(pnl),
                    'strategy': pos.get('strategy', 'crypto') if 'strategy' in pos else 'crypto',
                    'reason': reason,
                    'sell_gain_pct': round(((settle_price - entry) / entry) * 100, 1) if entry > 0 else 0,
                }).execute()
                # Resolve the buy
                db.table('trades').update({
                    'pnl': 0.0,
                    'current_bid': float(settle_price),
                }).eq('id', pos['id']).execute()
                cleaned += 1
        except Exception as e:
            logger.warning(f"Ghost check failed for {ticker}: {e}")

    if cleaned > 0:
        logger.info(f"Cleaned {cleaned} ghost positions (expired contracts)")
    return cleaned


def check_sells():
    """Smart tiered sell: 100% instant, save profits before expiry, let winners ride."""
    global sell_history
    logger.info("check_sells() — smart sell: 100%+ instant, expiry save, let winners ride")

    open_buys = db.table('trades').select('*') \
        .eq('action', 'buy').is_('pnl', 'null').execute()

    if not open_buys.data:
        logger.info("No open positions")
        return

    sold = 0
    settled = 0
    skipped_no_market = 0
    skipped_no_bid = 0
    evaluated = 0

    for trade in open_buys.data:
        ticker = trade['ticker']
        side = trade['side']
        entry_price = sf(trade['price'])
        count = trade['count'] or 1
        if entry_price <= 0:
            continue

        try:
            market = get_market(ticker)
        except Exception as e:
            logger.warning(f"Market fetch FAILED for {ticker}: {e}")
            skipped_no_market += 1
            continue
        if not market:
            logger.warning(f"Market returned None for {ticker}")
            skipped_no_market += 1
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
                    'pnl': float(pnl), 'strategy': trade.get('strategy', 'crypto'),
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
            settled += 1
            continue

        # === PRICE CHECK — try market data first, then live orderbook ===
        if side == 'yes':
            current_bid = float(market.get('yes_bid_dollars', '0') or '0')
        else:
            current_bid = float(market.get('no_bid_dollars', '0') or '0')

        # If market-level bid is 0, fetch live orderbook
        if current_bid <= 0:
            current_bid = get_live_bid(ticker, side)
            if current_bid > 0:
                logger.info(f"Orderbook fallback for {ticker}: got bid=${current_bid:.2f}")

        if current_bid <= 0:
            skipped_no_bid += 1
            logger.info(f"SKIP {ticker} — no bid available (market or orderbook)")
            continue

        gain_pct = ((current_bid - entry_price) / entry_price) * 100
        evaluated += 1

        # Log position evaluation
        time_to_expiry = get_time_to_expiry(market)
        expiry_str = f"{int(time_to_expiry)}s" if time_to_expiry is not None else "unknown"
        action_preview = "SELL" if gain_pct >= 100 or (time_to_expiry is not None and time_to_expiry < EXPIRY_WINDOW_SECONDS and gain_pct > 0) else "HOLD"
        logger.info(f"EVAL: {ticker} {side} x{count} | entry=${entry_price:.2f} bid=${current_bid:.2f} | gain={gain_pct:+.0f}% | expiry={expiry_str} | {action_preview}")

        # Near-expiry alert logging (< 2 min)
        if time_to_expiry is not None and time_to_expiry < 120:
            logger.info(f"NEAR EXPIRY: {ticker} | +{gain_pct:.0f}% | {int(time_to_expiry)}s left | "
                        f"{'SELLING' if gain_pct > 0 else 'letting expire (underwater)'}")

        # Update current price for dashboard
        try:
            db.table('trades').update({
                'current_bid': float(current_bid),
                'last_seen_bid': float(current_bid),
            }).eq('id', trade['id']).execute()
        except:
            pass

        # === DECIDE SELL ===
        should_sell, sell_qty, reason = decide_sell(
            entry_price, current_bid, count, time_to_expiry, trade.get('id')
        )

        if should_sell and sell_qty > 0:
            logger.info(f"SELLING: {ticker} {side} x{sell_qty} at ${current_bid:.2f} | gain={gain_pct:+.0f}% | {reason}")
            success = execute_sell(
                trade, ticker, side, entry_price, current_bid,
                sell_qty, count, gain_pct, reason
            )
            if success:
                sold += 1
                sell_history.append(gain_pct)
                if len(sell_history) > 20:
                    sell_history = sell_history[-20:]
            else:
                logger.error(f"SELL EXECUTION FAILED: {ticker} — order did not go through")
        elif gain_pct >= 50:
            logger.info(f"RIDING: {ticker} +{gain_pct:.0f}% — holding for 100%+ (expiry={expiry_str})")

    avg_win = (sum(sell_history) / len(sell_history)) if sell_history else 0
    logger.info(f"SELL SUMMARY: evaluated={evaluated} sold={sold} settled={settled} skipped_no_market={skipped_no_market} skipped_no_bid={skipped_no_bid} | avg_win={avg_win:.0f}%")


# === DOUBLE DOWN ON WINNERS ===

def double_down_on_winners():
    """Check open positions. If any are up 25%+, buy MORE of the same contract.
    Momentum is confirmed — pile on. By the time it hits 100%, we have 10-15 contracts."""
    total_balance, trading_balance, saved_balance = get_trading_balance()

    open_buys = db.table('trades').select('*') \
        .eq('action', 'buy').is_('pnl', 'null').execute()

    if not open_buys.data:
        return

    # Build lookup: how many total contracts per ticker (across all buy records)
    ticker_contracts = {}
    ticker_doubled = {}
    for t in open_buys.data:
        tk = t['ticker']
        ticker_contracts[tk] = ticker_contracts.get(tk, 0) + (t['count'] or 1)
        reason = t.get('reason', '') or ''
        if 'DOUBLE DOWN' in reason:
            ticker_doubled[tk] = True

    doubled = 0
    for trade in open_buys.data:
        ticker = trade['ticker']
        side = trade['side']
        entry_price = sf(trade['price'])
        count = trade['count'] or 1

        if entry_price <= 0:
            continue

        # Skip if we already doubled down on this ticker
        if ticker in ticker_doubled:
            continue

        # Skip if total contracts already at 10+ (cap at 15)
        total_contracts = ticker_contracts.get(ticker, 0)
        if total_contracts >= 10:
            continue

        # Get live bid to check current gain
        bid = get_live_bid(ticker, side)
        if bid is None or bid <= 0:
            continue

        gain_pct = ((bid - entry_price) / entry_price) * 100

        if gain_pct < 25:
            continue

        # Check expiry — don't double down with < 10 minutes left
        try:
            market = get_market(ticker)
        except:
            continue
        if not market:
            continue

        expiry_seconds = get_time_to_expiry(market)
        if expiry_seconds is not None and expiry_seconds < 600:
            continue

        # Scaling ladder: scale add size with confidence
        if gain_pct >= 50:
            add_contracts = 5   # Strong winner, go big
        elif gain_pct >= 35:
            add_contracts = 3   # Good winner, moderate add
        else:
            add_contracts = 2   # Early winner (+25%), small add

        # Cap total at 15
        add_contracts = min(add_contracts, 15 - total_contracts)
        if add_contracts <= 0:
            continue

        # Affordability check: max 10% of trading balance per double down
        cost = bid * add_contracts
        if cost > trading_balance * 0.10:
            add_contracts = max(1, int((trading_balance * 0.10) / bid))
            cost = bid * add_contracts

        if add_contracts <= 0 or cost > trading_balance:
            continue

        logger.info(f"DOUBLE DOWN: {ticker} {side} | "
                    f"entry=${entry_price:.2f} now=${bid:.2f} +{gain_pct:.0f}% | "
                    f"adding {add_contracts} contracts at ${bid:.2f} | "
                    f"total will be {total_contracts + add_contracts} contracts")

        order_id = place_order(ticker, side, 'buy', bid, add_contracts)
        if not order_id:
            continue

        # Record double-down as a separate buy in DB
        try:
            db.table('trades').insert({
                'ticker': ticker, 'side': side, 'action': 'buy',
                'price': float(bid), 'count': add_contracts,
                'strategy': trade.get('strategy', 'crypto'),
                'reason': f"DOUBLE DOWN +{gain_pct:.0f}%: added {add_contracts} at ${bid:.2f} (original {count} at ${entry_price:.2f})",
                'last_seen_bid': float(bid),
                'current_bid': float(bid),
            }).execute()
            ticker_doubled[ticker] = True
            trading_balance -= cost
            doubled += 1
        except Exception as e:
            logger.error(f"Double down DB insert failed: {e}")

    if doubled:
        logger.info(f"DOUBLE DOWN SUMMARY: added to {doubled} winning positions")
    else:
        logger.info("DOUBLE DOWN: no positions qualified (need +25% confirmed winners)")


# === MAIN CYCLE ===

def run_cycle():
    total, trading, saved = get_trading_balance()
    logger.info(f"=== CYCLE START === Total: ${total:.2f} | Trading: ${trading:.2f} | Saved: ${saved:.2f}")

    # 0. Clean ghost positions (expired contracts Kalshi already settled)
    try:
        cleanup_ghosts()
    except Exception as e:
        logger.error(f"Ghost cleanup error: {e}")

    # 1. Check sells (universal — works for any market type)
    try:
        check_sells()
    except Exception as e:
        logger.error(f"Sell check error: {e}")

    # 2. Double down on existing winners (+25%+ confirmed momentum)
    try:
        double_down_on_winners()
    except Exception as e:
        logger.error(f"Double down error: {e}")

    # 3. Scan crypto markets for new focused trades (best 2 per asset, 5 contracts each)
    try:
        crypto_markets = fetch_crypto_markets()
        run_buys(crypto_markets)
    except Exception as e:
        logger.error(f"Crypto buy error: {e}")

    # === 10% SKIMMER (separate table, separate logic) ===

    # 4. Check skimmer positions for +10% target or -15% stop loss
    try:
        skimmer_check_sells()
    except Exception as e:
        logger.error(f"Skimmer sell check error: {e}")

    # 5. Scan ALL markets for skimmer buys
    try:
        skimmer_scan_and_buy(trading)
    except Exception as e:
        logger.error(f"Skimmer buy error: {e}")

    # Skimmer status summary
    try:
        skim_open = db.table('skimmer_trades').select('id').eq('action', 'buy').is_('pnl', 'null').execute()
        skim_sells = db.table('skimmer_trades').select('pnl').eq('action', 'sell').not_.is_('pnl', 'null').execute()
        skim_open_count = len(skim_open.data or [])
        skim_pnl = sum(sf(t['pnl']) for t in (skim_sells.data or []))
        skim_wins = sum(1 for t in (skim_sells.data or []) if sf(t['pnl']) > 0)
        skim_cuts = sum(1 for t in (skim_sells.data or []) if sf(t['pnl']) < 0)
        logger.info(f"SKIMMER STATUS: open={skim_open_count}/{SKIMMER_MAX_OPEN} | wins={skim_wins} cuts={skim_cuts} | P&L=${skim_pnl:.4f}")
    except:
        pass

    total, trading, saved = get_trading_balance()
    logger.info(f"=== CYCLE END === Total: ${total:.2f} | Trading: ${trading:.2f} | Saved: ${saved:.2f}")


# === DASHBOARD ===

def categorize_for_dashboard(ticker, strategy=None):
    """Categorize for dashboard display. Uses strategy tag if available, else ticker pattern."""
    # Use strategy tag directly if it's from the universal scanner
    if strategy and strategy not in ('crypto', 'mm_scalp', None, ''):
        # Title-case the strategy for display
        return strategy.replace('_', ' ').title()
    # Legacy strategies
    if strategy == 'mm_scalp':
        return 'NCAA Basketball'
    # Fallback to ticker pattern
    if '15M' in ticker:
        return '15-min Crypto'
    elif 'KXBTCD' in ticker or 'KXETHD' in ticker or 'KXSOLD' in ticker:
        return 'Crypto Direction'
    elif any(x in ticker for x in ['KXBTC-', 'KXETH-', 'KXSOL-']):
        return 'Crypto Bracket'
    elif any(x in ticker for x in ['KXNCAAMBGAME', 'KXNCAAMB', 'KXCBB']):
        return 'NCAA Basketball'
    elif any(x in ticker for x in ['KXMARMAD', 'KXMM']):
        return 'March Madness'
    elif 'KXHIGH' in ticker or 'KXLOWT' in ticker:
        return 'Weather'
    elif any(x in ticker for x in ['OIL', 'WTI', 'CRUDE', 'BRENT']):
        return 'Oil'
    elif any(x in ticker for x in ['KXNFL', 'KXNBA', 'KXMLB', 'KXNHL', 'KXSOCCER', 'KXMLS', 'KXEPL', 'KXUCL']):
        return 'Sports'
    elif any(x in ticker for x in ['KXELECT', 'KXPRES', 'KXGOV', 'KXSEN', 'KXREFERENDUM']):
        return 'Elections'
    elif any(x in ticker for x in ['KXTENNIS', 'KXATP', 'KXWTA']):
        return 'Tennis'
    elif any(x in ticker for x in ['KXFED', 'KXCPI', 'KXGDP', 'KXJOBS', 'KXINFL']):
        return 'Economics'
    else:
        return 'Other'


@app.route('/')
def health():
    return 'OK'


@app.route('/api/status')
def api_status():
    try:
        balance = get_balance()
        saved = get_saved_balance()
        trading = max(0.0, balance - saved)

        sells = db.table('trades').select('pnl') \
            .eq('action', 'sell').not_.is_('pnl', 'null').execute()
        sell_data = sells.data or []
        net_pnl = sum(sf(t['pnl']) for t in sell_data)
        wins = sum(1 for t in sell_data if sf(t['pnl']) > 0)
        losses = sum(1 for t in sell_data if sf(t['pnl']) < 0)

        open_buys = db.table('trades').select('id,price,count,current_bid') \
            .eq('action', 'buy').is_('pnl', 'null').execute()
        open_data = open_buys.data or []
        # Split live (bid > 0) vs ghost (bid = 0)
        live_positions = [t for t in open_data if sf(t.get('current_bid')) > 0]
        ghost_count = len(open_data) - len(live_positions)
        open_count = len(live_positions)
        # Positions at market value — only live positions
        positions_value = round(sum(
            sf(t.get('current_bid')) * (t.get('count') or 1)
            for t in live_positions
        ), 2)
        # Cost basis — only live positions
        positions_cost = round(sum(sf(t.get('price')) * (t.get('count') or 1) for t in live_positions), 2)
        cash = round(balance - positions_cost, 2)  # Cash = balance minus what we spent

        # Saved today = 20% of today's winning sells
        today_str = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        today_sells = db.table('trades').select('pnl') \
            .eq('action', 'sell').not_.is_('pnl', 'null') \
            .gte('created_at', today_str).execute()
        saved_today = round(sum(max(0.0, sf(t['pnl'])) for t in (today_sells.data or [])) * PROFIT_SAVE_PCT, 4)

        return jsonify({
            'balance': round(balance, 2),
            'trading': round(trading, 2),
            'saved': round(saved, 4),
            'saved_today': saved_today,
            'net_pnl': round(net_pnl, 4),
            'wins': wins,
            'losses': losses,
            'open_count': open_count,
            'ghost_count': ghost_count,
            'positions_value': positions_value,
            'positions_cost': positions_cost,
            'cash': max(0, cash),
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


@app.route('/api/categories')
def api_categories():
    try:
        sells = db.table('trades').select('ticker,pnl,sell_gain_pct,strategy,created_at') \
            .eq('action', 'sell').not_.is_('pnl', 'null').execute()

        cats = {}
        for t in (sells.data or []):
            cat = categorize_for_dashboard(t.get('ticker', ''), t.get('strategy'))
            if cat not in cats:
                cats[cat] = {'wins': 0, 'losses': 0, 'pnl': 0.0, 'win_pcts': [], 'last_trade': ''}
            p = sf(t['pnl'])
            cats[cat]['pnl'] += p
            ts = t.get('created_at', '')
            if ts > cats[cat]['last_trade']:
                cats[cat]['last_trade'] = ts
            if p > 0:
                cats[cat]['wins'] += 1
                cats[cat]['win_pcts'].append(sf(t.get('sell_gain_pct')))
            elif p < 0:
                cats[cat]['losses'] += 1

        # Also count open positions per category
        open_buys = db.table('trades').select('ticker,strategy') \
            .eq('action', 'buy').is_('pnl', 'null').execute()
        open_cats = {}
        for t in (open_buys.data or []):
            cat = categorize_for_dashboard(t.get('ticker', ''), t.get('strategy'))
            open_cats[cat] = open_cats.get(cat, 0) + 1

        result = []
        all_cat_names = set(cats.keys()) | set(open_cats.keys())
        for name in all_cat_names:
            data = cats.get(name, {'wins': 0, 'losses': 0, 'pnl': 0.0, 'win_pcts': [], 'last_trade': ''})
            avg_win = (sum(data['win_pcts']) / len(data['win_pcts'])) if data['win_pcts'] else 0
            result.append({
                'name': name,
                'wins': data['wins'],
                'losses': data['losses'],
                'pnl': round(data['pnl'], 4),
                'avg_win_pct': round(avg_win, 1),
                'open': open_cats.get(name, 0),
                'last_trade': data['last_trade'],
            })
        result.sort(key=lambda x: x['pnl'], reverse=True)
        return jsonify(result)
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
            current = sf(t.get('current_bid')) or sf(t.get('last_seen_bid'))
            count = int(t.get('count') or 1)

            # Skip ghost positions (bid = 0, expired)
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
                'strategy': t.get('strategy', 'crypto'),
                'category': categorize_for_dashboard(t.get('ticker', ''), t.get('strategy')),
            })
        positions.sort(key=lambda x: x['gain_pct'], reverse=True)
        return jsonify(positions)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/scanner')
def api_scanner():
    return jsonify(last_scan)


@app.route('/api/skimmer')
def api_skimmer():
    try:
        sells = db.table('skimmer_trades').select('pnl,category,sell_gain_pct') \
            .eq('action', 'sell').not_.is_('pnl', 'null').execute()
        sell_data = sells.data or []
        net_pnl = sum(sf(t['pnl']) for t in sell_data)
        wins = sum(1 for t in sell_data if sf(t['pnl']) > 0)
        losses = sum(1 for t in sell_data if sf(t['pnl']) < 0)

        open_buys = db.table('skimmer_trades').select('id,ticker,side,price,count,current_bid,category') \
            .eq('action', 'buy').is_('pnl', 'null').execute()
        open_data = open_buys.data or []
        open_count = len(open_data)

        # Category breakdown of open positions
        cat_counts = {}
        for t in open_data:
            cat = t.get('category', 'unknown')
            cat_counts[cat] = cat_counts.get(cat, 0) + 1

        # Category breakdown of sells
        cat_pnl = {}
        for t in sell_data:
            cat = t.get('category', 'unknown')
            if cat not in cat_pnl:
                cat_pnl[cat] = {'pnl': 0.0, 'wins': 0, 'losses': 0}
            p = sf(t['pnl'])
            cat_pnl[cat]['pnl'] += p
            if p > 0:
                cat_pnl[cat]['wins'] += 1
            elif p < 0:
                cat_pnl[cat]['losses'] += 1

        # Open positions detail
        positions = []
        for t in open_data:
            price = sf(t.get('price'))
            current = sf(t.get('current_bid'))
            count = int(t.get('count') or 1)
            if current > 0 and price > 0:
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
                'category': t.get('category', 'unknown'),
            })
        positions.sort(key=lambda x: x['gain_pct'], reverse=True)

        return jsonify({
            'net_pnl': round(net_pnl, 4),
            'wins': wins,
            'losses': losses,
            'open_count': open_count,
            'max_open': SKIMMER_MAX_OPEN,
            'categories_open': cat_counts,
            'categories_pnl': cat_pnl,
            'positions': positions,
            'scan': last_skimmer_scan,
        })
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
<title>Kalshi Scalp Bot &mdash; Universal Scanner</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&display=swap');
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0a0a0a;color:#e0e0e0;font-family:'JetBrains Mono','SF Mono','Fira Code',monospace;padding:16px 20px;font-size:13px}
a{color:#4488ff;text-decoration:none}

@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
.live-dot{display:inline-block;width:8px;height:8px;background:#00d673;border-radius:50%;margin-right:6px;animation:pulse 2s infinite}

/* Portfolio hero */
.portfolio{text-align:center;margin-bottom:20px;padding:20px 0 16px;border-bottom:1px solid #1a1a1a}
.portfolio .sub{color:#555;font-size:11px;margin-bottom:12px}
.portfolio-value{font-size:48px;font-weight:700;color:#fff;margin-bottom:4px}
.portfolio-pnl{font-size:18px;font-weight:700;margin-bottom:14px}
.portfolio-breakdown{display:flex;justify-content:center;gap:32px;flex-wrap:wrap}
.portfolio-breakdown .item{text-align:center}
.portfolio-breakdown .item .label{color:#666;font-size:9px;text-transform:uppercase;letter-spacing:1px;margin-bottom:4px}
.portfolio-breakdown .item .val{font-size:18px;font-weight:700}

/* Category cards */
.category-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px;margin-bottom:14px}
.cat-card{background:#111;border:1px solid #1a1a1a;border-radius:6px;padding:10px 12px;transition:border-color .2s}
.cat-card:hover{border-color:#333}
.cat-card .cat-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:5px}
.cat-card .cat-name{font-size:11px;font-weight:700;color:#4488ff}
.cat-card .cat-record{font-size:10px;color:#888;margin-bottom:3px}
.cat-card .cat-pnl{font-size:16px;font-weight:700}
.cat-card .cat-detail{font-size:9px;color:#666;margin-top:2px}

/* Status badges */
.status-badge{padding:2px 6px;border-radius:3px;font-size:8px;font-weight:700;letter-spacing:.5px}
.badge-active{background:#002211;color:#00d673}
.badge-disabled{background:#220000;color:#ff4444}
.badge-waiting{background:#221800;color:#ffaa00}
.badge-idle{background:#1a1a1a;color:#555}

/* Panels */
.panels-row{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:14px}
.panel{background:#111;border:1px solid #1a1a1a;border-radius:6px;overflow:hidden}
.panel-header{padding:10px 14px;border-bottom:1px solid #1a1a1a;display:flex;justify-content:space-between;align-items:center}
.panel-header h2{color:#ffaa00;font-size:12px;text-transform:uppercase;letter-spacing:1px}
.panel-header .count{color:#555;font-size:11px}
.panel-body{max-height:400px;overflow-y:auto}
table{width:100%;border-collapse:collapse;font-size:11px}
th{color:#555;text-align:left;padding:6px 8px;border-bottom:1px solid #222;text-transform:uppercase;font-size:9px;letter-spacing:.5px;position:sticky;top:0;background:#111}
td{padding:5px 8px;border-bottom:1px solid #141414}
tr.row-green{background:rgba(0,214,115,.04)}
tr.row-red{background:rgba(255,68,68,.04)}
tr.row-yellow{background:rgba(255,170,0,.04)}
tr:hover{background:#1a1a1a !important}
.green{color:#00d673}.red{color:#ff4444}.yellow{color:#ffaa00}.blue{color:#4488ff}.gray{color:#555}
.badge{padding:2px 6px;border-radius:3px;font-size:9px;font-weight:700}
.badge-win{background:#002211;color:#00d673}
.badge-loss{background:#220000;color:#ff4444}
.badge-expired{background:#221100;color:#ff4444;font-size:9px}
.type-badge{padding:1px 5px;border-radius:3px;font-size:8px;font-weight:700;background:#1a1a2a;color:#7799cc;white-space:nowrap}

/* Equity */
.equity-section{background:#111;border:1px solid #1a1a1a;border-radius:6px;padding:14px;margin-bottom:14px}
.equity-section h2{color:#ffaa00;font-size:12px;text-transform:uppercase;letter-spacing:1px;margin-bottom:10px}
#equity-chart{width:100%;height:120px}

/* Scanner status bar */
.scanner-bar{background:#111;border:1px solid #1a1a1a;border-radius:6px;padding:10px 16px;margin-bottom:14px;font-size:10px;color:#666}
.scanner-bar .scanner-title{color:#ffaa00;font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:1px;margin-bottom:6px}
.scanner-cats{display:flex;flex-wrap:wrap;gap:6px 14px}
.scanner-cats .sc-item{display:flex;align-items:center;gap:3px}
.scanner-cats .sc-dot{width:5px;height:5px;border-radius:50%;background:#00d673}

/* Status bar */
.status-bar{background:#111;border:1px solid #1a1a1a;border-radius:6px;padding:10px 16px;display:flex;justify-content:space-between;flex-wrap:wrap;gap:8px;font-size:10px;color:#555}
.status-bar .status-item{display:flex;align-items:center;gap:4px}
.status-bar .dot-live{width:6px;height:6px;background:#00d673;border-radius:50%;animation:pulse 2s infinite}
.status-bar .dot-blocked{width:6px;height:6px;background:#ff4444;border-radius:50%}
.footer{text-align:center;color:#333;font-size:9px;margin-top:8px}
.loading{color:#555;text-align:center;padding:20px}
.panel-body::-webkit-scrollbar{width:4px}
.panel-body::-webkit-scrollbar-track{background:#111}
.panel-body::-webkit-scrollbar-thumb{background:#333;border-radius:2px}

@media(max-width:900px){
.portfolio-value{font-size:36px}
.portfolio-breakdown{gap:16px}
.panels-row{grid-template-columns:1fr}
}
</style>
</head>
<body>

<!-- Portfolio Hero -->
<div class="portfolio">
  <div class="sub"><span class="live-dot"></span>LIVE TRADING &mdash; 30s cycles &mdash; Universal Scanner &mdash; ALL markets</div>
  <div class="portfolio-value" id="p-total">...</div>
  <div class="portfolio-pnl" id="p-pnl">...</div>
  <div class="portfolio-breakdown">
    <div class="item"><div class="label">Positions</div><div class="val" id="p-positions">...</div></div>
    <div class="item"><div class="label">Cash</div><div class="val" id="p-cash">...</div></div>
    <div class="item"><div class="label">Banked</div><div class="val green" id="p-saved">...</div></div>
    <div class="item"><div class="label">Record</div><div class="val" id="p-record">...</div></div>
  </div>
</div>

<!-- Category Cards (dynamic) -->
<div class="category-row" id="categories">
  <div class="cat-card"><div class="loading">Loading categories...</div></div>
</div>

<!-- 10% Skimmer Section -->
<div class="panel" style="margin-bottom:14px">
  <div class="panel-header"><h2 style="color:#00d673">10% SKIMMER</h2><div class="count" id="skim-summary">Loading...</div></div>
  <div style="padding:10px 14px;display:flex;gap:24px;flex-wrap:wrap;font-size:12px;border-bottom:1px solid #1a1a1a" id="skim-stats"></div>
  <div class="panel-body" style="max-height:250px"><table><thead><tr>
    <th>Cat</th><th>Ticker</th><th>Side</th><th>Qty</th><th>Entry</th><th>Bid</th><th>P&amp;L</th><th>Gain</th>
  </tr></thead><tbody id="skim-body"><tr><td colspan="8" class="loading">Loading...</td></tr></tbody></table></div>
</div>

<!-- Scanner Status -->
<div class="scanner-bar" id="scanner-bar">
  <div class="scanner-title">Universal Scanner</div>
  <div class="scanner-cats" id="scanner-cats">Waiting for first scan...</div>
</div>

<!-- Panels -->
<div class="panels-row">
  <div class="panel">
    <div class="panel-header"><h2>Open Positions</h2><div class="count" id="open-count"></div></div>
    <div class="panel-body"><table><thead><tr>
      <th>Type</th><th>Ticker</th><th>Side</th><th>Qty</th><th>Entry</th><th>Bid</th><th>P&amp;L</th><th>Gain</th>
    </tr></thead><tbody id="open-body"><tr><td colspan="8" class="loading">Loading...</td></tr></tbody></table></div>
  </div>
  <div class="panel">
    <div class="panel-header"><h2>Recent Trades</h2><div class="count" id="trades-count"></div></div>
    <div class="panel-body"><table><thead><tr>
      <th>Time</th><th>Type</th><th>Ticker</th><th>Side</th><th>Qty</th><th>P&amp;L</th><th>Gain</th>
    </tr></thead><tbody id="trades-body"><tr><td colspan="7" class="loading">Loading...</td></tr></tbody></table></div>
  </div>
</div>

<!-- Equity Curve -->
<div class="equity-section">
  <h2>Equity Curve</h2>
  <canvas id="equity-chart"></canvas>
</div>

<!-- Status Bar -->
<div class="status-bar">
  <div class="status-item"><span class="dot-live"></span> Status: LIVE</div>
  <div class="status-item">15M: <span class="dot-blocked"></span> BLOCKED</div>
  <div class="status-item">Max contracts: 5</div>
  <div class="status-item">Sell: 100%+ or expiry save</div>
  <div class="status-item">Saved: <span class="green" id="sb-saved">$0</span> protected</div>
  <div class="status-item">Ghosts: <span class="yellow" id="sb-ghosts">0</span> expired cleaned</div>
  <div class="status-item">Last update: <span id="last-update">&mdash;</span></div>
</div>
<div class="footer">Kalshi Scalp Bot v7 &mdash; Universal Scanner &mdash; auto-refresh 15s</div>

<script>
function $(id){return document.getElementById(id)}
function cls(v){return v>0?'green':v<0?'red':'gray'}
function esc(s){var d=document.createElement('div');d.textContent=s;return d.innerHTML}

function catType(ticker,strategy){
  if(strategy&&strategy!=='crypto'&&strategy!=='mm_scalp'&&strategy!=='')return strategy.replace(/_/g,' ');
  if(strategy==='mm_scalp')return 'NCAA Basketball';
  if(!ticker)return 'Other';
  if(ticker.indexOf('KXBTC')>=0||ticker.indexOf('KXETH')>=0||ticker.indexOf('KXSOL')>=0)return ticker.indexOf('-')>=0?'Crypto Bracket':'Crypto Direction';
  if(ticker.indexOf('KXNCAA')>=0)return 'NCAA Basketball';
  return 'Other';
}

function shortType(name){
  var m={'Crypto Direction':'CRYPTO','Crypto Bracket':'CRYPTO','crypto direction':'CRYPTO','crypto bracket':'CRYPTO',
    'NCAA Basketball':'NCAA','NCAA basketball':'NCAA','march madness futures':'MM','March Madness Futures':'MM',
    'oil':'OIL','Oil':'OIL','sports':'SPORT','Sports':'SPORT','elections':'ELECT','Elections':'ELECT',
    'tennis':'TENNIS','Tennis':'TENNIS','economics':'ECON','Economics':'ECON','weather':'WX','Weather':'WX','other':'OTHER','Other':'OTHER'};
  return m[name]||name.substring(0,6).toUpperCase();
}

async function fetchJSON(url){
  try{var r=await fetch(url);return await r.json()}
  catch(e){console.error(url,e);return null}
}

async function refresh(){
  var [status,cats,open,trades,scanner,skimmer]=await Promise.all([
    fetchJSON('/api/status'),
    fetchJSON('/api/categories'),
    fetchJSON('/api/open'),
    fetchJSON('/api/trades'),
    fetchJSON('/api/scanner'),
    fetchJSON('/api/skimmer')
  ]);

  // Portfolio hero
  if(status&&!status.error){
    var bal=status.balance||0;
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
    var savedToday=status.saved_today||0;
    $('p-saved').innerHTML='$'+(status.saved||0).toFixed(2)
      +(savedToday>0?'<div style="font-size:10px;color:#00d673;margin-top:2px">+$'+savedToday.toFixed(2)+' today</div>':'')
      +'<div style="font-size:9px;color:#555;margin-top:2px">Protected forever</div>';
    $('p-record').innerHTML='<span class="green">'+status.wins+'W</span> <span class="gray">/</span> <span class="red">'+status.losses+'L</span>';
    $('sb-saved').textContent='$'+(status.saved||0).toFixed(2);
    $('sb-ghosts').textContent=(status.ghost_count||0);
  }

  // Dynamic category cards — only show categories that have trades or open positions
  if(cats&&!cats.error){
    var h='';
    cats.forEach(function(c){
      var pc=cls(c.pnl);
      var hasActivity=(c.wins+c.losses)>0;
      var hasOpen=(c.open||0)>0;
      var stLabel,stClass;
      if(c.name==='15-min Crypto'){stLabel='DISABLED';stClass='badge-disabled';}
      else if(hasOpen){stLabel='ACTIVE';stClass='badge-active';}
      else if(hasActivity){stLabel='IDLE';stClass='badge-idle';}
      else{stLabel='SCANNING';stClass='badge-waiting';}
      h+='<div class="cat-card">';
      h+='<div class="cat-header"><span class="cat-name">'+esc(c.name)+'</span><span class="status-badge '+stClass+'">'+stLabel+'</span></div>';
      h+='<div class="cat-record"><span class="green">'+c.wins+'W</span> / <span class="red">'+c.losses+'L</span>';
      if(c.open)h+=' <span class="blue">'+c.open+' open</span>';
      h+='</div>';
      h+='<div class="cat-pnl '+pc+'">'+(c.pnl>=0?'+':'')+c.pnl.toFixed(2)+'</div>';
      if(c.avg_win_pct>0)h+='<div class="cat-detail">Avg win: '+c.avg_win_pct.toFixed(0)+'%</div>';
      h+='</div>';
    });
    $('categories').innerHTML=h||'<div class="cat-card"><div class="loading">No categories yet</div></div>';
  }

  // Scanner status bar
  if(scanner&&scanner.total){
    var scanCats=scanner.categories||{};
    var pairs=Object.keys(scanCats).map(function(k){return{name:k,count:scanCats[k]}}).sort(function(a,b){return b.count-a.count});
    var sh='<span style="color:#e0e0e0;margin-right:8px">Scanning '+scanner.total+' markets:</span>';
    pairs.forEach(function(p){
      if(p.name.indexOf('BLOCKED')>=0||p.name.indexOf('SKIP')>=0)return;
      sh+='<span class="sc-item"><span class="sc-dot"></span>'+esc(p.name)+': '+p.count+'</span>';
    });
    if(scanner.timestamp){
      var ago=Math.round((Date.now()-new Date(scanner.timestamp).getTime())/1000);
      sh+='<span style="margin-left:auto;color:#555">'+ago+'s ago</span>';
    }
    $('scanner-cats').innerHTML=sh;
  }

  // Open positions with TYPE column
  if(open&&!open.error){
    $('open-count').textContent=open.length+' positions';
    var h='';
    open.forEach(function(p){
      var rc=p.gain_pct>2?'row-green':p.gain_pct<-2?'row-red':'row-yellow';
      var gc=cls(p.gain_pct);
      var typ=p.category||catType(p.ticker,p.strategy);
      h+='<tr class="'+rc+'">';
      h+='<td><span class="type-badge">'+esc(shortType(typ))+'</span></td>';
      h+='<td style="font-size:10px">'+esc(p.ticker)+'</td>';
      h+='<td>'+esc(p.side)+'</td>';
      h+='<td>'+p.count+'</td>';
      h+='<td>$'+p.entry.toFixed(2)+'</td>';
      if(p.expired){
        h+='<td><span class="badge badge-expired">EXPIRED</span></td>';
      }else{
        h+='<td>$'+(p.current_bid||0).toFixed(2)+'</td>';
      }
      h+='<td class="'+gc+'">'+(p.unrealized>=0?'+':'')+p.unrealized.toFixed(4)+'</td>';
      h+='<td class="'+gc+'">'+(p.gain_pct>=0?'+':'')+p.gain_pct.toFixed(0)+'%</td>';
      h+='</tr>';
    });
    $('open-body').innerHTML=h||'<tr><td colspan="8" class="gray" style="text-align:center">No open positions</td></tr>';
  }

  // Recent completed trades with TYPE column
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
      var typ=catType(t.ticker,t.strategy);
      h+='<tr class="'+rc+'">';
      h+='<td>'+esc(time)+'</td>';
      h+='<td><span class="type-badge">'+esc(shortType(typ))+'</span></td>';
      h+='<td style="font-size:10px">'+esc(t.ticker||'')+'</td>';
      h+='<td>'+esc(t.side||'')+'</td>';
      h+='<td>'+count+'</td>';
      h+='<td class="'+pc+'">'+(p>=0?'+':'')+p.toFixed(4)+'</td>';
      h+='<td class="'+pc+'">'+(gainPct>=0?'+':'')+gainPct.toFixed(0)+'%</td>';
      h+='</tr>';
    });
    $('trades-body').innerHTML=h||'<tr><td colspan="7" class="gray" style="text-align:center">No completed trades</td></tr>';

    drawEquity(completed);
  }

  // 10% Skimmer section
  if(skimmer&&!skimmer.error){
    var sp=skimmer.net_pnl||0;
    var spc=cls(sp);
    $('skim-summary').innerHTML='<span class="green">'+skimmer.wins+'W</span> / <span class="red">'+skimmer.losses+'L</span> | Open: <span class="blue">'+skimmer.open_count+'/'+skimmer.max_open+'</span> | P&L: <span class="'+spc+'">'+(sp>=0?'+':'')+sp.toFixed(4)+'</span>';

    // Stats row: category breakdown
    var sh='';
    var co=skimmer.categories_open||{};
    var cp=skimmer.categories_pnl||{};
    var allCats=Object.keys(co).concat(Object.keys(cp)).filter(function(v,i,a){return a.indexOf(v)===i});
    allCats.forEach(function(cat){
      var cnt=co[cat]||0;
      var pd=cp[cat]||{pnl:0,wins:0,losses:0};
      var c2=cls(pd.pnl);
      sh+='<span style="margin-right:12px"><span class="blue" style="text-transform:uppercase;font-size:10px;font-weight:700">'+esc(cat)+'</span> ';
      if(cnt>0)sh+='<span class="gray">'+cnt+' open</span> ';
      sh+='<span class="'+c2+'">'+(pd.pnl>=0?'+':'')+pd.pnl.toFixed(2)+'</span>';
      sh+=' <span class="gray">('+pd.wins+'W/'+pd.losses+'L)</span></span>';
    });
    if(skimmer.scan&&skimmer.scan.total){
      sh+='<span style="margin-left:auto;color:#555;font-size:10px">Scanned '+skimmer.scan.total+' mkts, '+skimmer.scan.candidates+' candidates</span>';
    }
    $('skim-stats').innerHTML=sh||'<span class="gray">No skimmer activity yet</span>';

    // Skimmer open positions table
    var positions=skimmer.positions||[];
    var h='';
    positions.forEach(function(p){
      var rc=p.gain_pct>2?'row-green':p.gain_pct<-2?'row-red':'row-yellow';
      var gc=cls(p.gain_pct);
      h+='<tr class="'+rc+'">';
      h+='<td><span class="type-badge">'+esc((p.category||'').substring(0,8).toUpperCase())+'</span></td>';
      h+='<td style="font-size:10px">'+esc(p.ticker)+'</td>';
      h+='<td>'+esc(p.side)+'</td>';
      h+='<td>'+p.count+'</td>';
      h+='<td>$'+p.entry.toFixed(2)+'</td>';
      h+='<td>$'+(p.current_bid||0).toFixed(2)+'</td>';
      h+='<td class="'+gc+'">'+(p.unrealized>=0?'+':'')+p.unrealized.toFixed(4)+'</td>';
      h+='<td class="'+gc+'">'+(p.gain_pct>=0?'+':'')+p.gain_pct.toFixed(0)+'%</td>';
      h+='</tr>';
    });
    $('skim-body').innerHTML=h||'<tr><td colspan="8" class="gray" style="text-align:center">No skimmer positions</td></tr>';
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
    logger.info("Bot starting — LIVE TRADING — ALL MARKETS — universal scanner — max 5 contracts")
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
