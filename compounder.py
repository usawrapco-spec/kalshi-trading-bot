"""
COMPOUNDER — Kalshi resting-order market-maker + VIP rebate collector.

Strategy:
  1. Scan all multi-strike crypto events (hourly + daily, 800+ strikes)
  2. Find markets with spreads >= 2c where we can improve the book
  3. Place resting BUY YES at (best_bid + 1c) AND BUY NO at (best_no_bid + 1c)
  4. When one side fills, we own a position 1c better than the market
  5. If BOTH sides fill: we hold a bundle costing < $1.00 → guaranteed profit
  6. Uncompleted singles ride to settlement (50/50 EV by definition at mid)
  7. Every fill also earns $0.005 VIP cashback (free, on top of spread capture)

Risk:
  - Adverse selection: we get filled on the losing side more often than the winning
  - Mitigation: only post on liquid crypto markets with active two-sided flow
  - Position cap: never more than MAX_OPEN positions outstanding
  - Single-trade cap: 1 contract per market (small size = small risk)

Paper mode: logs all order activity, computes theoretical fills from live books.
Live mode: places real resting limit orders via Kalshi REST API.
"""

import os, time, logging, traceback, math
from datetime import datetime, timezone
from threading import Thread
from flask import Flask, jsonify, render_template_string
import psycopg2
from psycopg2.extras import RealDictCursor
from kalshi_auth import KalshiAuth
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# === CONFIG ===
KALSHI_HOST = os.environ.get("KALSHI_API_HOST", "https://api.elections.kalshi.com")
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://kalshi:kalshi@localhost:5432/kalshi")
PORT = int(os.environ.get("COMPOUNDER_PORT", 8084))
ENABLE_TRADING = os.environ.get("ENABLE_TRADING", "").lower() in ("1", "true", "yes")

# === STRATEGY ===
STARTING_BALANCE = 100.00
MIN_SPREAD = 0.02          # Only post on markets with >= 2c spread
CONTRACTS = 1              # 1 contract per order (small size)
MAX_OPEN_ORDERS = 20       # Max simultaneous resting orders
MAX_POSITIONS = 30         # Max positions held (filled, awaiting settlement)
CYCLE_SECONDS = 15         # How often to scan + refresh orders
CANCEL_IF_FILLED = True    # Cancel the other side if one side fills (don't go naked)
TAKER_FEE_RATE = 0.07
FEE_CAP = 0.02
MAKER_FEE = 0.00           # Makers pay $0 on Kalshi
VIP_REBATE = 0.005         # $0.005 per contract from Volume Incentive Program
MIN_PRICE = 0.05           # Don't post below 5c (too risky, low VIP eligibility)
MAX_PRICE = 0.95           # Don't post above 95c
MIN_MINS_TO_EXPIRY = 10    # Don't post on markets about to close
MAX_MINS_TO_EXPIRY = 1440  # Up to 24h out

# Which crypto series to scan
CRYPTO_SERIES = ["KXBTC", "KXETH", "KXSOL", "KXXRP", "KXDOGE",
                 "KXBTCD", "KXETHD", "KXSOLD", "KXXRPD", "KXDOGED"]

# === INIT ===
auth = KalshiAuth()
app = Flask(__name__)

def _make_session():
    s = requests.Session()
    retries = Retry(total=3, backoff_factor=1, status_forcelist=[500, 502, 503, 504])
    s.mount("https://", HTTPAdapter(max_retries=retries))
    return s

session = _make_session()

# Track our resting orders and positions
_state = {
    "resting_orders": {},   # order_id -> {ticker, side, price, placed_at}
    "positions": [],        # filled positions awaiting settlement
    "cycles": 0,
    "last_cycle": None,
    "fills": 0,
    "pairs_completed": 0,
    "total_pnl": 0.0,
    "errors": [],
}


def sf(val):
    try:
        return float(val) if val is not None else 0.0
    except:
        return 0.0


def kalshi_fee(price, count=1):
    """Taker fee. Makers pay $0."""
    return min(math.ceil(TAKER_FEE_RATE * count * price * (1 - price) * 100) / 100, FEE_CAP * count)


# === KALSHI API ===

def kalshi_get(path):
    url = f"{KALSHI_HOST}/trade-api/v2{path}"
    headers = auth.get_headers("GET", f"/trade-api/v2{path}")
    resp = session.get(url, headers=headers, timeout=15)
    resp.raise_for_status()
    return resp.json()


def kalshi_post(path, data):
    url = f"{KALSHI_HOST}/trade-api/v2{path}"
    headers = auth.get_headers("POST", f"/trade-api/v2{path}")
    headers["Content-Type"] = "application/json"
    resp = session.post(url, headers=headers, json=data, timeout=15)
    resp.raise_for_status()
    return resp.json()


def kalshi_delete(path):
    url = f"{KALSHI_HOST}/trade-api/v2{path}"
    headers = auth.get_headers("DELETE", f"/trade-api/v2{path}")
    resp = session.delete(url, headers=headers, timeout=15)
    resp.raise_for_status()
    return resp.json()


def place_resting_order(ticker, side, price, count):
    """Place a resting limit order. Returns order_id or None."""
    price_cents = int(round(price * 100))
    if not ENABLE_TRADING:
        oid = f"paper-{ticker}-{side}-{price_cents}-{int(time.time())}"
        logger.info(f"PAPER ORDER: {side.upper()} {ticker} x{count} @ ${price:.2f} -> {oid}")
        return oid

    try:
        resp = kalshi_post("/portfolio/orders", {
            "ticker": ticker,
            "action": "buy",
            "side": side,
            "type": "limit",
            "count": count,
            "yes_price" if side == "yes" else "no_price": price_cents,
        })
        order = resp.get("order", {})
        order_id = order.get("order_id", "")
        status = order.get("status", "")
        remaining = order.get("remaining_count", count)
        if remaining < count:
            # Partially or fully filled immediately — not resting
            filled = count - remaining
            logger.info(f"IMMEDIATE FILL: {side.upper()} {ticker} filled={filled}/{count} id={order_id}")
            return order_id
        logger.info(f"RESTING: {side.upper()} {ticker} x{count} @ ${price:.2f} status={status} id={order_id}")
        return order_id
    except Exception as e:
        logger.error(f"Order failed: {side.upper()} {ticker} @ ${price:.2f} — {e}")
        return None


def cancel_order(order_id):
    """Cancel a resting order."""
    if not ENABLE_TRADING or order_id.startswith("paper-"):
        logger.info(f"PAPER CANCEL: {order_id}")
        return True
    try:
        kalshi_delete(f"/portfolio/orders/{order_id}")
        logger.info(f"CANCELLED: {order_id}")
        return True
    except Exception as e:
        logger.warning(f"Cancel failed: {order_id} — {e}")
        return False


def get_order_status(order_id):
    """Check if a resting order has been filled."""
    if not ENABLE_TRADING or order_id.startswith("paper-"):
        return "resting"  # Paper orders never fill on their own
    try:
        resp = kalshi_get(f"/portfolio/orders/{order_id}")
        order = resp.get("order", {})
        remaining = order.get("remaining_count", 1)
        if remaining == 0:
            return "filled"
        return order.get("status", "resting")
    except:
        return "unknown"


def get_balance():
    if ENABLE_TRADING:
        try:
            resp = kalshi_get("/portfolio/balance")
            return resp.get("balance", 0) / 100.0
        except:
            pass
    return STARTING_BALANCE


# === DATABASE ===

def get_db():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    return conn


def init_db():
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS compounder_orders (
                    id SERIAL PRIMARY KEY,
                    ticker TEXT NOT NULL,
                    side TEXT NOT NULL,
                    price NUMERIC NOT NULL,
                    count INTEGER DEFAULT 1,
                    order_id TEXT,
                    pair_key TEXT,
                    status TEXT DEFAULT 'resting',
                    placed_at TIMESTAMPTZ DEFAULT NOW(),
                    filled_at TIMESTAMPTZ,
                    settled_at TIMESTAMPTZ,
                    settle_result TEXT,
                    pnl NUMERIC,
                    vip_rebate NUMERIC DEFAULT 0
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS comp_orders_status_idx ON compounder_orders (status)")
            cur.execute("CREATE INDEX IF NOT EXISTS comp_orders_pair_key_idx ON compounder_orders (pair_key)")
    finally:
        conn.close()


# === MARKET SCANNING ===

def fetch_all_markets():
    """Fetch all multi-strike crypto markets with populated books."""
    all_markets = []
    for series in CRYPTO_SERIES:
        try:
            events = kalshi_get(f"/events?series_ticker={series}&status=open&limit=20").get("events", [])
            for ev in events:
                et = ev.get("event_ticker", "")
                if not et:
                    continue
                cursor = None
                while True:
                    path = f"/markets?event_ticker={et}&status=open&limit=200"
                    if cursor:
                        path += f"&cursor={cursor}"
                    resp = kalshi_get(path)
                    all_markets.extend(resp.get("markets", []))
                    cursor = resp.get("cursor")
                    if not cursor:
                        break
        except Exception as e:
            logger.error(f"Fetch {series} failed: {e}")
    return all_markets


def find_mm_opportunities(markets):
    """Find markets where we can improve the book (spread >= MIN_SPREAD)."""
    now = datetime.now(timezone.utc)
    opportunities = []

    for m in markets:
        ticker = m.get("ticker", "")
        close_time = m.get("close_time") or m.get("expected_expiration_time")
        if not close_time:
            continue
        try:
            close_dt = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
            mins_left = (close_dt - now).total_seconds() / 60
            if mins_left < MIN_MINS_TO_EXPIRY or mins_left > MAX_MINS_TO_EXPIRY:
                continue
        except:
            continue

        yes_ask = sf(m.get("yes_ask_dollars"))
        yes_bid = sf(m.get("yes_bid_dollars"))
        no_ask = sf(m.get("no_ask_dollars"))
        no_bid = sf(m.get("no_bid_dollars"))

        if yes_ask <= 0 or yes_bid <= 0 or no_ask <= 0 or no_bid <= 0:
            continue

        yes_spread = yes_ask - yes_bid
        no_spread = no_ask - no_bid

        # We can post inside the spread if it's wide enough
        if yes_spread >= MIN_SPREAD:
            our_yes_price = round(yes_bid + 0.01, 2)  # Improve best bid by 1c
            our_no_price = round(no_bid + 0.01, 2)    # Improve best no bid by 1c

            # Sanity: our prices must be in range
            if MIN_PRICE <= our_yes_price <= MAX_PRICE and MIN_PRICE <= our_no_price <= MAX_PRICE:
                combined = our_yes_price + our_no_price
                # If both fill: guaranteed profit = $1.00 - combined (no maker fee)
                bundle_profit = 1.00 - combined if combined < 1.00 else 0

                opportunities.append({
                    "ticker": ticker,
                    "yes_bid": yes_bid,
                    "yes_ask": yes_ask,
                    "no_bid": no_bid,
                    "no_ask": no_ask,
                    "our_yes_price": our_yes_price,
                    "our_no_price": our_no_price,
                    "yes_spread": yes_spread,
                    "combined": combined,
                    "bundle_profit": bundle_profit,
                    "mins_left": mins_left,
                    "close_time": close_time,
                })

    # Sort by bundle profit (best first), then by spread (widest first)
    opportunities.sort(key=lambda o: (-o["bundle_profit"], -o["yes_spread"]))
    return opportunities


# === PAPER FILL SIMULATION ===

def simulate_paper_fills(conn):
    """In paper mode, check if our resting orders 'would have filled'
    by comparing our order price to the current market bid/ask.
    If our buy-YES price >= current yes_ask, we got filled (someone sold to us).
    If our buy-NO price >= current no_ask, we got filled."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM compounder_orders WHERE status = 'resting'")
        resting = cur.fetchall()

    if not resting:
        return

    # Batch-fetch markets for all tickers
    tickers = set(r["ticker"] for r in resting)
    market_cache = {}
    for ticker in tickers:
        try:
            resp = kalshi_get(f"/markets/{ticker}")
            market_cache[ticker] = resp.get("market", resp)
        except:
            pass

    for order in resting:
        ticker = order["ticker"]
        market = market_cache.get(ticker)
        if not market:
            continue

        side = order["side"]
        our_price = float(order["price"])

        if side == "yes":
            # Our buy-YES fills if someone is willing to sell YES at our price
            # i.e. the current yes_ask has dropped to or below our bid
            current_ask = sf(market.get("yes_ask_dollars"))
            if current_ask > 0 and our_price >= current_ask:
                _mark_filled(conn, order)
        else:
            current_ask = sf(market.get("no_ask_dollars"))
            if current_ask > 0 and our_price >= current_ask:
                _mark_filled(conn, order)


def _mark_filled(conn, order):
    """Mark a paper order as filled and handle pairing."""
    logger.info(f"FILLED: {order['side'].upper()} {order['ticker']} @ ${float(order['price']):.2f} (order {order['order_id']})")
    _state["fills"] += 1
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE compounder_orders SET status = 'filled', filled_at = NOW() WHERE id = %s",
            (order["id"],),
        )

    # Check if the paired order is also filled → bundle!
    pair_key = order["pair_key"]
    if pair_key:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM compounder_orders WHERE pair_key = %s AND status = 'filled'",
                (pair_key,),
            )
            filled_in_pair = cur.fetchall()
            if len(filled_in_pair) == 2:
                yes_price = sum(float(f["price"]) for f in filled_in_pair if f["side"] == "yes")
                no_price = sum(float(f["price"]) for f in filled_in_pair if f["side"] == "no")
                bundle_profit = 1.00 - yes_price - no_price
                logger.warning(
                    f"*** BUNDLE COMPLETE *** {order['ticker']} yes=${yes_price:.2f}+no=${no_price:.2f}="
                    f"${yes_price+no_price:.2f} → profit=${bundle_profit:.4f}"
                )
                _state["pairs_completed"] += 1

        # Cancel the other resting order in this pair if CANCEL_IF_FILLED
        if CANCEL_IF_FILLED:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    "SELECT * FROM compounder_orders WHERE pair_key = %s AND status = 'resting'",
                    (pair_key,),
                )
                to_cancel = cur.fetchall()
                for tc in to_cancel:
                    cancel_order(tc["order_id"])
                    cur.execute(
                        "UPDATE compounder_orders SET status = 'cancelled' WHERE id = %s",
                        (tc["id"],),
                    )


# === SETTLEMENT ===

def check_settlements(conn):
    """Check filled positions for settlement."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM compounder_orders WHERE status = 'filled'")
        filled = cur.fetchall()

    for order in filled:
        ticker = order["ticker"]
        try:
            resp = kalshi_get(f"/markets/{ticker}")
            market = resp.get("market", resp)
            status = market.get("status", "")
            result = market.get("result", "")

            if status in ("settled", "closed", "finalized") or result:
                price = float(order["price"])
                side = order["side"]
                if result == side:
                    pnl = 1.00 - price  # Won
                else:
                    pnl = -price  # Lost
                # Add VIP rebate
                rebate = VIP_REBATE
                pnl += rebate

                with conn.cursor() as cur2:
                    cur2.execute(
                        "UPDATE compounder_orders SET status='settled', settled_at=NOW(), "
                        "settle_result=%s, pnl=%s, vip_rebate=%s WHERE id=%s",
                        (result, round(pnl, 4), rebate, order["id"]),
                    )
                _state["total_pnl"] += pnl
                logger.info(
                    f"SETTLED: {side.upper()} {ticker} result={result} "
                    f"pnl=${pnl:.4f} (incl ${rebate} VIP)"
                )
        except Exception as e:
            logger.warning(f"Settlement check {ticker}: {e}")


# === CANCEL STALE ===

def cancel_stale_orders(conn):
    """Cancel resting orders on markets that are about to close."""
    now = datetime.now(timezone.utc)
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM compounder_orders WHERE status = 'resting'")
        resting = cur.fetchall()

    for order in resting:
        ticker = order["ticker"]
        try:
            resp = kalshi_get(f"/markets/{ticker}")
            market = resp.get("market", resp)
            close_time = market.get("close_time")
            status = market.get("status", "")
            if status in ("settled", "closed", "finalized"):
                cancel_order(order["order_id"])
                with conn.cursor() as cur2:
                    cur2.execute(
                        "UPDATE compounder_orders SET status='cancelled' WHERE id=%s",
                        (order["id"],),
                    )
                continue
            if close_time:
                close_dt = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
                mins_left = (close_dt - now).total_seconds() / 60
                if mins_left < 5:  # Cancel if < 5 min to close
                    cancel_order(order["order_id"])
                    with conn.cursor() as cur2:
                        cur2.execute(
                            "UPDATE compounder_orders SET status='cancelled' WHERE id=%s",
                            (order["id"],),
                        )
        except:
            pass


# === MAIN CYCLE ===

def run_cycle():
    conn = get_db()
    try:
        balance = get_balance()

        # Count current state
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT COUNT(*) AS n FROM compounder_orders WHERE status = 'resting'")
            n_resting = cur.fetchone()["n"]
            cur.execute("SELECT COUNT(*) AS n FROM compounder_orders WHERE status = 'filled'")
            n_filled = cur.fetchone()["n"]
            cur.execute(
                "SELECT COALESCE(SUM(pnl), 0) AS total FROM compounder_orders WHERE status = 'settled'"
            )
            total_pnl = float(cur.fetchone()["total"])

        logger.info(
            f"COMPOUNDER [{('LIVE' if ENABLE_TRADING else 'PAPER')}] "
            f"bal=${balance:.2f} | resting={n_resting} filled={n_filled} "
            f"pairs={_state['pairs_completed']} pnl=${total_pnl:.2f}"
        )

        # 1. Check for fills (paper mode simulates, live mode checks API)
        if ENABLE_TRADING:
            check_live_fills(conn)
        else:
            simulate_paper_fills(conn)

        # 2. Check settlements
        check_settlements(conn)

        # 3. Cancel stale orders
        cancel_stale_orders(conn)

        # 4. Recount after cleanup
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT COUNT(*) AS n FROM compounder_orders WHERE status = 'resting'")
            n_resting = cur.fetchone()["n"]
            cur.execute("SELECT COUNT(*) AS n FROM compounder_orders WHERE status = 'filled'")
            n_filled = cur.fetchone()["n"]

        # 5. Place new orders if we have capacity
        slots = MAX_OPEN_ORDERS - n_resting
        if slots <= 0:
            return
        if n_filled >= MAX_POSITIONS:
            logger.info("Max positions reached — not placing new orders")
            return

        markets = fetch_all_markets()
        logger.info(f"Fetched {len(markets)} markets")

        opps = find_mm_opportunities(markets)
        logger.info(f"Found {len(opps)} spread opportunities (>= {MIN_SPREAD*100:.0f}c spread)")

        # Avoid duplicates: get tickers we already have orders on
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT DISTINCT ticker FROM compounder_orders WHERE status IN ('resting', 'filled')"
            )
            existing_tickers = {r["ticker"] for r in cur.fetchall()}

        placed = 0
        for opp in opps:
            if placed >= slots:
                break
            if opp["ticker"] in existing_tickers:
                continue

            ticker = opp["ticker"]
            pair_key = f"{ticker}-{int(time.time())}"

            # Place YES side
            yes_oid = place_resting_order(ticker, "yes", opp["our_yes_price"], CONTRACTS)
            if yes_oid:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO compounder_orders (ticker, side, price, count, order_id, pair_key) "
                        "VALUES (%s, %s, %s, %s, %s, %s)",
                        (ticker, "yes", opp["our_yes_price"], CONTRACTS, yes_oid, pair_key),
                    )

            # Place NO side
            no_oid = place_resting_order(ticker, "no", opp["our_no_price"], CONTRACTS)
            if no_oid:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO compounder_orders (ticker, side, price, count, order_id, pair_key) "
                        "VALUES (%s, %s, %s, %s, %s, %s)",
                        (ticker, "no", opp["our_no_price"], CONTRACTS, no_oid, pair_key),
                    )

            if yes_oid or no_oid:
                placed += 1
                existing_tickers.add(ticker)
                if opp["bundle_profit"] > 0:
                    logger.info(
                        f"POSTED {ticker}: yes@${opp['our_yes_price']:.2f} no@${opp['our_no_price']:.2f} "
                        f"spread={opp['yes_spread']*100:.0f}c bundle_profit=${opp['bundle_profit']:.4f}"
                    )
                else:
                    logger.info(
                        f"POSTED {ticker}: yes@${opp['our_yes_price']:.2f} no@${opp['our_no_price']:.2f} "
                        f"spread={opp['yes_spread']*100:.0f}c"
                    )

        if placed > 0:
            logger.info(f"Placed {placed} new order pairs this cycle")

    except Exception as e:
        logger.error(f"Cycle error: {traceback.format_exc()}")
        _state["errors"].append(str(e))
        if len(_state["errors"]) > 20:
            _state["errors"] = _state["errors"][-20:]
    finally:
        conn.close()

    _state["cycles"] += 1
    _state["last_cycle"] = datetime.now(timezone.utc).isoformat()


def check_live_fills(conn):
    """In live mode, poll order status for fills."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM compounder_orders WHERE status = 'resting'")
        resting = cur.fetchall()

    for order in resting:
        status = get_order_status(order["order_id"])
        if status == "filled":
            _mark_filled(conn, order)


# === BOT LOOP ===

def bot_loop():
    logger.info("COMPOUNDER started — resting-order market maker + VIP rebate collector")
    while True:
        try:
            run_cycle()
        except Exception:
            logger.error(traceback.format_exc())
        time.sleep(CYCLE_SECONDS)


# === FLASK ===

@app.route("/")
def index():
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT status, COUNT(*) AS n,
                       COALESCE(SUM(pnl), 0) AS pnl,
                       COALESCE(SUM(vip_rebate), 0) AS rebates
                FROM compounder_orders GROUP BY status
            """)
            by_status = {r["status"]: r for r in cur.fetchall()}

            cur.execute("""
                SELECT * FROM compounder_orders
                WHERE status IN ('resting', 'filled')
                ORDER BY placed_at DESC LIMIT 30
            """)
            active = cur.fetchall()

            cur.execute("""
                SELECT * FROM compounder_orders
                WHERE status = 'settled'
                ORDER BY settled_at DESC LIMIT 20
            """)
            recent_settled = cur.fetchall()
    finally:
        conn.close()

    return jsonify({
        "mode": "LIVE" if ENABLE_TRADING else "PAPER",
        "by_status": {k: {"count": v["n"], "pnl": float(v["pnl"]), "rebates": float(v["rebates"])}
                      for k, v in by_status.items()},
        "active_orders": [
            {k: str(v) for k, v in r.items()} for r in active
        ],
        "recent_settled": [
            {k: str(v) for k, v in r.items()} for r in recent_settled
        ],
        "state": {
            "cycles": _state["cycles"],
            "fills": _state["fills"],
            "pairs_completed": _state["pairs_completed"],
            "last_cycle": _state["last_cycle"],
        },
    })


if __name__ == "__main__":
    init_db()
    Thread(target=bot_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT)
