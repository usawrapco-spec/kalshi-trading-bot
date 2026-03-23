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
MAX_SPEND_PER_TRADE_PCT = 0.10  # Max 10% of trading_balance per trade
MAX_SPEND_PER_CYCLE = 25
MAX_TRADES_PER_CYCLE = 10
MAX_OPEN_POSITIONS = 200

# === SELL THRESHOLDS ===
BASE_SELL_PCT = 25              # Sell at 25% gain — fast turnover beats big holds
EXPIRY_SELL_SECONDS = 300       # Sell anything profitable with <5 min to expiry

# === PROFIT COMPOUNDING ===
PROFIT_SAVE_PCT = 0.20          # 20% of every win gets banked permanently
PROFIT_REINVEST_PCT = 0.80      # 80% of every win goes back to trading

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

        # Resolve any non-supported positions (trending/weather leftovers)
        all_open = db.table('trades').select('id,ticker,strategy') \
            .eq('action', 'buy').is_('pnl', 'null').execute()
        if all_open.data:
            valid_strategies = {'crypto', 'mm_scalp'}
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
    # HARD SAFETY: cap buy orders at 5 contracts max (sells can be any size)
    if action == 'buy':
        count = min(count, 5)
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
    # BTC first — the proven winners (hourly only, 15M dropped — data says they lose)
    'KXBTCD', 'KXBTC', 'KXBTC1H',
    # ETH — brackets work well
    'KXETHD', 'KXETH', 'KXETH1H',
    # SOL — weakest but keep for diversification
    'KXSOLD', 'KXSOL', 'KXSOL1H',
]

# === MARCH MADNESS ===

MARCH_MADNESS_SERIES = [
    'KXNCAAMBGAME',  # Individual NCAA men's basketball games — THE MAIN ONE (178+ markets)
    'KXMARMAD',      # March Madness championship futures
    'KXNCAAMB',      # NCAA men's basketball general
]

BASKETBALL_KEYWORDS = [
    'NCAA', 'March Madness', 'championship', 'tournament',
    'Sweet 16', 'Elite Eight', 'Final Four',
    'Duke', 'Michigan', 'Arizona', 'Florida', 'Auburn',
    'Houston', 'Iowa State', 'UConn', 'Tennessee', 'Alabama',
    'Purdue', 'Kansas', 'Kentucky', 'Arkansas',
    'Gonzaga', "St. John", 'Illinois',
]


def fetch_all_crypto():
    """Fetch all crypto markets — hourly only, 15M dropped."""
    markets = []
    for series in CRYPTO_SERIES:
        try:
            resp = kalshi_get(f"/markets?series_ticker={series}&status=open&limit=100")
            for m in resp.get('markets', []):
                ticker = m.get('ticker', '')
                if '15M' in ticker:
                    continue  # 15-min contracts lose money — data proven
                markets.append(m)
        except:
            pass
    logger.info(f"Fetched {len(markets)} crypto markets (hourly only)")
    return markets


def fetch_march_madness():
    """Find all March Madness markets via series tickers, keyword search, and events."""
    mm_markets = []

    # Method 1: Try known series tickers
    for series in MARCH_MADNESS_SERIES:
        try:
            resp = kalshi_get(f"/markets?series_ticker={series}&status=open&limit=200")
            found = resp.get('markets', [])
            mm_markets.extend(found)
            if found:
                logger.info(f"Found {len(found)} markets in series {series}")
        except:
            continue

    # Method 2: Keyword search across all open markets
    if not mm_markets:
        try:
            resp = kalshi_get('/markets?status=open&limit=1000')
            for m in resp.get('markets', []):
                title = (m.get('title', '') + ' ' + m.get('subtitle', '')).lower()
                if any(kw.lower() in title for kw in BASKETBALL_KEYWORDS):
                    mm_markets.append(m)
            if mm_markets:
                logger.info(f"Found {len(mm_markets)} March Madness markets via keyword search")
        except:
            pass

    # Method 3: Try events API
    if not mm_markets:
        try:
            resp = kalshi_get('/events?status=open&with_nested_markets=true&limit=100')
            for event in resp.get('events', []):
                title = event.get('title', '').lower()
                if any(kw.lower() in title for kw in ['ncaa', 'march madness', 'basketball', 'tournament', 'champion']):
                    for m in event.get('markets', []):
                        mm_markets.append(m)
            if mm_markets:
                logger.info(f"Found {len(mm_markets)} March Madness markets via events")
        except:
            pass

    # Deduplicate by ticker
    seen = set()
    unique = []
    for m in mm_markets:
        t = m.get('ticker', '')
        if t not in seen:
            seen.add(t)
            unique.append(m)
    mm_markets = unique

    # Log what we found for debugging
    if mm_markets:
        series_found = set(m.get('series_ticker', 'unknown') for m in mm_markets)
        logger.info(f"MM series tickers: {series_found}")
        logger.info(f"MM sample tickers: {[m['ticker'] for m in mm_markets[:10]]}")
    else:
        logger.info("No March Madness markets found")

    return mm_markets


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


def buy_priority(ticker, strategy='crypto'):
    """Lower = buy first. BTC daily is the money maker, MM is secondary."""
    if strategy == 'mm_scalp':
        return 8  # After all crypto
    if 'KXBTCD' in ticker: return 0
    if ticker.startswith('KXBTC-'): return 1
    if ticker.startswith('KXETH-'): return 2
    if 'KXETHD' in ticker: return 3
    if 'KXSOL' in ticker: return 4
    return 5


def run_buys(markets, strategy='crypto'):
    total_balance, trading_balance, saved_balance = get_trading_balance()
    owned = get_owned()
    num_open = len(owned)
    logger.info(f"[{strategy}] Own {num_open} tickers | trading=${trading_balance:.2f} saved=${saved_balance:.2f}")

    if num_open >= MAX_OPEN_POSITIONS:
        logger.info(f"At max open positions ({MAX_OPEN_POSITIONS}), skipping buys")
        return

    # MM-specific: price range is wider (5-45c) and requires volume filter
    min_price = 0.05 if strategy == 'mm_scalp' else MIN_PRICE
    max_price = 0.45 if strategy == 'mm_scalp' else MAX_PRICE
    max_spread = 0.12 if strategy == 'mm_scalp' else 1.0  # MM needs tight spreads
    min_volume = 50 if strategy == 'mm_scalp' else 0

    buys = []
    for m in markets:
        ticker = m.get('ticker', '')
        # HARD BLOCK: 15-min contracts — disabled, they lose money
        if '15M' in ticker:
            logger.info(f"BLOCKED: {ticker} — 15-min contracts disabled")
            continue
        if ticker in owned:
            continue
        if 'KXMVE' in ticker:
            continue

        # Volume filter for MM
        volume = sf(m.get('volume', 0)) or sf(m.get('volume_24h', 0))
        if strategy == 'mm_scalp' and volume < min_volume:
            continue

        yes_bid = float(m.get('yes_bid_dollars', '0') or '0')
        yes_ask = float(m.get('yes_ask_dollars', '0') or '0')
        no_bid = float(m.get('no_bid_dollars', '0') or '0')
        no_ask = float(m.get('no_ask_dollars', '0') or '0')

        # Collect liquid sides
        candidates = []
        if yes_bid > 0 and yes_ask > 0 and min_price <= yes_ask <= max_price:
            spread = yes_ask - yes_bid
            if spread <= max_spread:
                candidates.append(('yes', yes_ask, yes_bid, spread))
        if no_bid > 0 and no_ask > 0 and min_price <= no_ask <= max_price:
            spread = no_ask - no_bid
            if spread <= max_spread:
                candidates.append(('no', no_ask, no_bid, spread))

        if not candidates:
            continue

        # Pick side with tightest spread
        candidates.sort(key=lambda x: x[3])
        side, price, bid, spread = candidates[0]
        count = calculate_position_size(price, trading_balance, volume, strategy)

        buys.append({
            'ticker': ticker, 'side': side, 'price': price,
            'bid': bid, 'spread': spread, 'count': count,
            'volume': volume, 'strategy': strategy,
        })

    # Sort by priority then tightest spread
    buys.sort(key=lambda x: (buy_priority(x['ticker'], x['strategy']), x['spread']))

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

        strat_label = 'MM' if strategy == 'mm_scalp' else 'CRYPTO'
        logger.info(f"BUY [{strat_label}]: {b['ticker']} {b['side']} x{b['count']} @ ${b['price']:.2f} (bid=${b['bid']:.2f} spread=${b['spread']:.2f})")
        try:
            db.table('trades').insert({
                'ticker': b['ticker'], 'side': b['side'], 'action': 'buy',
                'price': float(b['price']), 'count': b['count'],
                'strategy': strategy,
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

    logger.info(f"[{strategy}] Bought {bought}, spent ${cycle_spent:.2f}, trading=${trading_balance:.2f}, deployed ${current_deployed:.2f}/{max_exposure:.2f}")


# === SELL LOGIC — 50% BASE + MOMENTUM RIDING + 5-MIN EXPIRY SELL ===

sell_history = []  # Rolling last 20 sell gain percentages
peak_bids = {}     # trade_id -> highest bid seen, for trailing stop


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
    """Simple sell logic: 50% base, ride momentum, always exit before expiry.
    Returns (should_sell, sell_qty, reason)."""
    gain_pct = ((current_bid - entry_price) / entry_price) * 100

    # Track peak bid for momentum riding
    prev_peak = peak_bids.get(trade_id, current_bid)
    if current_bid > prev_peak:
        peak_bids[trade_id] = current_bid
    peak = peak_bids.get(trade_id, current_bid)
    peak_gain_pct = ((peak - entry_price) / entry_price) * 100

    # === #1 PRIORITY: EXPIRY SELL — any profit with <5 min left, DUMP IT ===
    if time_to_expiry is not None and time_to_expiry < EXPIRY_SELL_SECONDS and gain_pct > 0:
        return True, count, f"EXPIRY SELL <5min, locking +{gain_pct:.0f}% ({int(time_to_expiry)}s left)"

    # === NEVER sell at a loss ===
    if gain_pct <= 0 or current_bid <= entry_price:
        return False, 0, None

    # === TRAILING STOP: only triggers if price DROPS from a tracked peak ===
    # If peak was tracked from a previous cycle AND price has fallen significantly, sell
    if trade_id in peak_bids and peak_gain_pct >= 100:
        trailing_floor = peak_gain_pct * 0.50
        if gain_pct <= trailing_floor:
            return True, count, f"TRAILING STOP: peak +{peak_gain_pct:.0f}% -> now +{gain_pct:.0f}%"

    # === BASE THRESHOLD: sell at 50%+ gain — ALWAYS ===
    if gain_pct >= BASE_SELL_PCT:
        return True, count, f"PROFIT +{gain_pct:.0f}% (>={BASE_SELL_PCT}% target)"

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


def check_sells():
    """50% base sell + momentum riding + 5-min expiry sell. Never sell at a loss."""
    global sell_history
    logger.info("check_sells() called — 50% base + momentum riding + 5-min expiry")

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
            peak_bids.pop(trade.get('id'), None)
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

        # Log EVERY position evaluation
        logger.info(f"EVAL: {ticker} {side} x{count} | entry=${entry_price:.2f} bid=${current_bid:.2f} | gain={gain_pct:+.0f}% | threshold=50%")

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
        elif gain_pct >= 25:
            logger.warning(f"NOT SELLING despite {gain_pct:+.0f}% gain: {ticker} | should_sell={should_sell} sell_qty={sell_qty} reason={reason}")

    avg_win = (sum(sell_history) / len(sell_history)) if sell_history else 0
    logger.info(f"SELL SUMMARY: evaluated={evaluated} sold={sold} settled={settled} skipped_no_market={skipped_no_market} skipped_no_bid={skipped_no_bid} | avg_win={avg_win:.0f}%")


# === MAIN CYCLE ===

def run_cycle():
    total, trading, saved = get_trading_balance()
    logger.info(f"=== CYCLE START === Total: ${total:.2f} | Trading: ${trading:.2f} | Saved: ${saved:.2f}")

    # 1. Check sells (both crypto and MM positions)
    try:
        check_sells()
    except Exception as e:
        logger.error(f"Sell check error: {e}")

    # 2. Scan crypto (hourly only, 15M dropped)
    crypto_count = 0
    try:
        crypto_markets = fetch_all_crypto()
        crypto_count = len(crypto_markets)
        run_buys(crypto_markets, strategy='crypto')
    except Exception as e:
        logger.error(f"Crypto buy error: {e}")

    # 3. Scan March Madness
    mm_count = 0
    try:
        mm_markets = fetch_march_madness()
        mm_count = len(mm_markets)
        logger.info(f"March Madness markets found: {mm_count} | Sample: {[m.get('ticker','') for m in mm_markets[:5]]}")
        if mm_markets:
            run_buys(mm_markets, strategy='mm_scalp')
    except Exception as e:
        logger.error(f"MM buy error: {e}")

    total, trading, saved = get_trading_balance()
    logger.info(f"=== CYCLE END === crypto={crypto_count} sports={mm_count} | Total: ${total:.2f} | Trading: ${trading:.2f} | Saved: ${saved:.2f}")


# === DASHBOARD ===

def categorize_ticker(ticker):
    if '15M' in ticker:
        return '15-min Crypto'
    elif 'KXBTCD' in ticker or 'KXETHD' in ticker or 'KXSOLD' in ticker:
        return 'Hourly Direction'
    elif any(x in ticker for x in ['KXBTC-', 'KXETH-', 'KXSOL-']):
        return 'Hourly Bracket'
    elif any(x in ticker for x in ['KXNCAAMBGAME', 'NCAA', 'KXMARMAD', 'KXCBB', 'KXMM']):
        return 'March Madness'
    elif 'KXHIGH' in ticker or 'KXLOWT' in ticker:
        return 'Weather'
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

        open_buys = db.table('trades').select('id') \
            .eq('action', 'buy').is_('pnl', 'null').execute()
        open_count = len(open_buys.data or [])

        return jsonify({
            'balance': round(balance, 2),
            'trading': round(trading, 2),
            'saved': round(saved, 4),
            'net_pnl': round(net_pnl, 4),
            'wins': wins,
            'losses': losses,
            'open_count': open_count,
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
        sells = db.table('trades').select('ticker,pnl,sell_gain_pct') \
            .eq('action', 'sell').not_.is_('pnl', 'null').execute()

        cats = {}
        for t in (sells.data or []):
            cat = categorize_ticker(t.get('ticker', ''))
            if cat not in cats:
                cats[cat] = {'wins': 0, 'losses': 0, 'pnl': 0.0, 'win_pcts': []}
            p = sf(t['pnl'])
            cats[cat]['pnl'] += p
            if p > 0:
                cats[cat]['wins'] += 1
                cats[cat]['win_pcts'].append(sf(t.get('sell_gain_pct')))
            elif p < 0:
                cats[cat]['losses'] += 1

        result = []
        for name, data in cats.items():
            avg_win = (sum(data['win_pcts']) / len(data['win_pcts'])) if data['win_pcts'] else 0
            result.append({
                'name': name,
                'wins': data['wins'],
                'losses': data['losses'],
                'pnl': round(data['pnl'], 4),
                'avg_win_pct': round(avg_win, 1),
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
            if current and current > 0 and price > 0:
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
                'expired': current is None or current <= 0,
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
<title>Kalshi Scalp Bot — Command Center</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&display=swap');
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0a0a0a;color:#e0e0e0;font-family:'JetBrains Mono','Fira Code',monospace;padding:16px 20px;font-size:13px}
a{color:#4488ff;text-decoration:none}
.header{text-align:center;margin-bottom:18px;padding:12px 0;border-bottom:1px solid #222}
.header h1{color:#ffaa00;font-size:22px;letter-spacing:3px;margin-bottom:4px}
.header .sub{color:#555;font-size:11px}
.header .live-dot{display:inline-block;width:8px;height:8px;background:#00ff88;border-radius:50%;margin-right:6px;animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
.metrics-row{display:grid;grid-template-columns:repeat(5,1fr);gap:10px;margin-bottom:14px}
.metric-card{background:#111;border:1px solid #1a1a1a;border-radius:6px;padding:14px 10px;text-align:center;transition:border-color .2s}
.metric-card:hover{border-color:#333}
.metric-card .label{color:#666;font-size:9px;text-transform:uppercase;letter-spacing:1px;margin-bottom:6px}
.metric-card .value{font-size:22px;font-weight:700}
.category-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px;margin-bottom:14px}
.cat-card{background:#111;border:1px solid #1a1a1a;border-radius:6px;padding:12px;transition:border-color .2s}
.cat-card:hover{border-color:#333}
.cat-card .cat-name{font-size:11px;font-weight:700;margin-bottom:6px;color:#4488ff}
.cat-card .cat-record{font-size:10px;color:#888;margin-bottom:4px}
.cat-card .cat-pnl{font-size:16px;font-weight:700}
.cat-card .cat-avg{font-size:9px;color:#666;margin-top:2px}
.panels-row{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:14px}
.panel{background:#111;border:1px solid #1a1a1a;border-radius:6px;overflow:hidden}
.panel-header{padding:10px 14px;border-bottom:1px solid #1a1a1a;display:flex;justify-content:space-between;align-items:center}
.panel-header h2{color:#ffaa00;font-size:12px;text-transform:uppercase;letter-spacing:1px}
.panel-header .count{color:#555;font-size:11px}
.panel-body{max-height:400px;overflow-y:auto}
table{width:100%;border-collapse:collapse;font-size:11px}
th{color:#555;text-align:left;padding:6px 8px;border-bottom:1px solid #222;text-transform:uppercase;font-size:9px;letter-spacing:.5px;position:sticky;top:0;background:#111}
td{padding:5px 8px;border-bottom:1px solid #141414}
tr.row-green{background:rgba(0,255,136,.04)}
tr.row-red{background:rgba(255,68,68,.04)}
tr.row-yellow{background:rgba(255,170,0,.04)}
tr:hover{background:#1a1a1a !important}
.green{color:#00ff88}.red{color:#ff4444}.yellow{color:#ffaa00}.blue{color:#4488ff}.gray{color:#555}
.badge{padding:2px 6px;border-radius:3px;font-size:9px;font-weight:700}
.badge-win{background:#002211;color:#00ff88}
.badge-loss{background:#220000;color:#ff4444}
.badge-expired{background:#221100;color:#ff4444;font-size:9px}
.equity-section{background:#111;border:1px solid #1a1a1a;border-radius:6px;padding:14px}
.equity-section h2{color:#ffaa00;font-size:12px;text-transform:uppercase;letter-spacing:1px;margin-bottom:10px}
#equity-chart{width:100%;height:120px}
.footer{text-align:center;color:#333;font-size:9px;margin-top:12px}
.loading{color:#555;text-align:center;padding:20px}
.panel-body::-webkit-scrollbar{width:4px}
.panel-body::-webkit-scrollbar-track{background:#111}
.panel-body::-webkit-scrollbar-thumb{background:#333;border-radius:2px}

@media(max-width:900px){
.metrics-row{grid-template-columns:repeat(3,1fr)}
.panels-row{grid-template-columns:1fr}
}
</style>
</head>
<body>

<div class="header">
  <h1>KALSHI SCALP BOT</h1>
  <div class="sub"><span class="live-dot"></span>LIVE Trading &mdash; 30s cycles &mdash; BTC/ETH/SOL + March Madness &mdash; v4</div>
</div>

<div class="metrics-row" id="metrics">
  <div class="metric-card"><div class="label">Balance</div><div class="value" id="m-balance">...</div></div>
  <div class="metric-card"><div class="label">Net P&amp;L</div><div class="value" id="m-pnl">...</div></div>
  <div class="metric-card"><div class="label">Saved (Banked)</div><div class="value" id="m-saved">...</div></div>
  <div class="metric-card"><div class="label">Open Positions</div><div class="value" id="m-open">...</div></div>
  <div class="metric-card"><div class="label">Record</div><div class="value" id="m-record">...</div></div>
</div>

<div class="category-row" id="categories">
  <div class="cat-card"><div class="loading">Loading categories...</div></div>
</div>

<div class="panels-row">
  <div class="panel">
    <div class="panel-header"><h2>Open Positions</h2><div class="count" id="open-count"></div></div>
    <div class="panel-body"><table><thead><tr>
      <th>Ticker</th><th>Side</th><th>Cnt</th><th>Entry</th><th>Bid</th><th>Unreal P&amp;L</th><th>Gain%</th>
    </tr></thead><tbody id="open-body"><tr><td colspan="7" class="loading">Loading...</td></tr></tbody></table></div>
  </div>
  <div class="panel">
    <div class="panel-header"><h2>Recent Trades</h2><div class="count" id="trades-count"></div></div>
    <div class="panel-body"><table><thead><tr>
      <th>Time</th><th>Ticker</th><th>Side</th><th>Cnt</th><th>Entry</th><th>P&amp;L</th><th>Gain%</th>
    </tr></thead><tbody id="trades-body"><tr><td colspan="7" class="loading">Loading...</td></tr></tbody></table></div>
  </div>
</div>

<div class="equity-section">
  <h2>Equity Curve</h2>
  <canvas id="equity-chart"></canvas>
</div>

<div class="footer">Auto-refresh every 15s &mdash; Last update: <span id="last-update">—</span></div>

<script>
function $(id){return document.getElementById(id)}
function fmt(v,d){return (v>=0?'':'')+'$'+Math.abs(v).toFixed(d||2)}
function cls(v){return v>0?'green':v<0?'red':'gray'}
function esc(s){var d=document.createElement('div');d.textContent=s;return d.innerHTML}

async function fetchJSON(url){
  try{var r=await fetch(url);return await r.json()}
  catch(e){console.error(url,e);return null}
}

async function refresh(){
  var [status,cats,open,trades]=await Promise.all([
    fetchJSON('/api/status'),
    fetchJSON('/api/categories'),
    fetchJSON('/api/open'),
    fetchJSON('/api/trades')
  ]);

  // Metrics
  if(status&&!status.error){
    $('m-balance').textContent='$'+status.balance.toFixed(2);
    $('m-balance').className='value green';

    var pnl=status.net_pnl;
    $('m-pnl').textContent=(pnl>=0?'+':'')+pnl.toFixed(2);
    $('m-pnl').className='value '+cls(pnl);

    $('m-saved').textContent='$'+status.saved.toFixed(2);
    $('m-saved').className='value green';

    $('m-open').textContent=status.open_count;
    $('m-open').className='value yellow';

    $('m-record').innerHTML='<span class="green">'+status.wins+'W</span> / <span class="red">'+status.losses+'L</span>';
  }

  // Categories
  if(cats&&!cats.error){
    var h='';
    cats.forEach(function(c){
      var pc=cls(c.pnl);
      h+='<div class="cat-card">';
      h+='<div class="cat-name">'+esc(c.name)+'</div>';
      h+='<div class="cat-record"><span class="green">'+c.wins+'W</span> / <span class="red">'+c.losses+'L</span></div>';
      h+='<div class="cat-pnl '+pc+'">'+(c.pnl>=0?'+':'')+c.pnl.toFixed(2)+'</div>';
      h+='<div class="cat-avg">Avg win: '+c.avg_win_pct.toFixed(0)+'%</div>';
      h+='</div>';
    });
    $('categories').innerHTML=h||'<div class="cat-card"><div class="loading">No data</div></div>';
  }

  // Open positions
  if(open&&!open.error){
    $('open-count').textContent=open.length+' positions';
    var h='';
    open.forEach(function(p){
      var rc=p.gain_pct>2?'row-green':p.gain_pct<-2?'row-red':'row-yellow';
      var gc=cls(p.gain_pct);
      h+='<tr class="'+rc+'">';
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
    $('open-body').innerHTML=h||'<tr><td colspan="7" class="gray" style="text-align:center">No open positions</td></tr>';
  }

  // Recent completed trades
  if(trades&&!trades.error){
    var completed=trades.filter(function(t){return t.action==='sell'&&t.pnl!==null&&t.pnl!==0});
    $('trades-count').textContent=completed.length+' trades';
    var h='';
    completed.slice(0,50).forEach(function(t){
      var p=t.pnl||0;
      var pc=cls(p);
      var rc=p>0?'row-green':'row-red';
      var time=(t.created_at||'').replace('T',' ').substring(5,19);
      var price=t.price||0;
      var count=t.count||1;
      var entry=price-(p/count);
      var gainPct=t.sell_gain_pct||0;
      h+='<tr class="'+rc+'">';
      h+='<td>'+esc(time)+'</td>';
      h+='<td style="font-size:10px">'+esc(t.ticker||'')+'</td>';
      h+='<td>'+esc(t.side||'')+'</td>';
      h+='<td>'+count+'</td>';
      h+='<td>$'+entry.toFixed(2)+'</td>';
      h+='<td class="'+pc+'">'+(p>=0?'+':'')+p.toFixed(4)+'</td>';
      h+='<td class="'+pc+'">'+(gainPct>=0?'+':'')+gainPct.toFixed(0)+'%</td>';
      h+='</tr>';
    });
    $('trades-body').innerHTML=h||'<tr><td colspan="7" class="gray" style="text-align:center">No completed trades</td></tr>';

    // Equity curve
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

  // Build cumulative P&L (trades are newest-first, reverse)
  var sorted=trades.slice().reverse();
  var cumulative=[0];
  var running=0;
  sorted.forEach(function(t){running+=(t.pnl||0);cumulative.push(running)});

  if(cumulative.length<2)return;

  var min=Math.min.apply(null,cumulative);
  var max=Math.max.apply(null,cumulative);
  var range=max-min||1;
  var pad=10;

  // Zero line
  var zeroY=H-pad-((0-min)/range)*(H-2*pad);
  ctx.strokeStyle='#222';ctx.lineWidth=1;
  ctx.beginPath();ctx.moveTo(0,zeroY);ctx.lineTo(W,zeroY);ctx.stroke();

  // Equity line
  ctx.strokeStyle=running>=0?'#00ff88':'#ff4444';
  ctx.lineWidth=1.5;
  ctx.beginPath();
  for(var i=0;i<cumulative.length;i++){
    var x=(i/(cumulative.length-1))*W;
    var y=H-pad-((cumulative[i]-min)/range)*(H-2*pad);
    if(i===0)ctx.moveTo(x,y);else ctx.lineTo(x,y);
  }
  ctx.stroke();

  // Fill under curve
  ctx.lineTo(W,H);ctx.lineTo(0,H);ctx.closePath();
  ctx.fillStyle=running>=0?'rgba(0,255,136,.06)':'rgba(255,68,68,.06)';
  ctx.fill();

  // Labels
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
    logger.info("Bot starting — LIVE TRADING — crypto + March Madness — 25% fast sell + 5-min expiry exit + max 5 contracts")
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
