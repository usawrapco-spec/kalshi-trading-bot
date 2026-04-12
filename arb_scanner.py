"""
Cross-venue arb DISCOVERY scanner — Kalshi ↔ Polymarket.

READ-ONLY. Pulls both catalogs every cycle, attempts to pair events by
asset / settlement-time proximity / nearest strike, and logs:
  - True arbs (combined yes+no across venues < $1.00 - MIN_EDGE, same event)
  - Near-arbs (combined within 3¢)
  - Large spreads (potential spread bets, not risk-free)

The goal of this scanner is evidence: does a cross-venue arb opportunity
actually exist in live markets, and if so, how often and on which strikes?
Once we have that data we decide whether to build the execution layer.

Run: python arb_scanner.py
Env:
  DATABASE_URL     - Postgres
  KALSHI_API_HOST  - https://api.elections.kalshi.com
  KALSHI_API_KEY_ID + KALSHI_PRIVATE_KEY via kalshi_auth
"""
import os, time, logging, traceback
from datetime import datetime, timezone, timedelta
from threading import Thread
from flask import Flask, jsonify
import psycopg2
from psycopg2.extras import RealDictCursor
import requests

from kalshi_auth import KalshiAuth
from polymarket_client import fetch_crypto_universe

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# === CONFIG ===
KALSHI_HOST = os.environ.get("KALSHI_API_HOST", "https://api.elections.kalshi.com")
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://kalshi:kalshi@localhost:5432/kalshi")
PORT = int(os.environ.get("ARB_PORT", 8083))

CYCLE_SECONDS = 30
SETTLEMENT_WINDOW_MINUTES = 60   # Pair events whose close_time is within this window
STRIKE_TOLERANCE_PCT = 0.5       # "same strike" if within 0.5% of each other
MIN_EDGE = 0.02                  # 2¢ minimum profit for "arb hit" (after fees)
NEAR_EDGE = 0.00                 # Log combined <= 1.00 as "near arb"

# Kalshi crypto daily series (multi-strike events)
KALSHI_SERIES = ["KXBTCD", "KXETHD", "KXSOLD", "KXXRPD"]
SERIES_TO_ASSET = {"KXBTCD": "BTC", "KXETHD": "ETH", "KXSOLD": "SOL", "KXXRPD": "XRP"}

# Kalshi taker fee cap
FEE_CAP = 0.02  # $0.02/contract worst case — use for conservative arb gate
# Polymarket fees are built into the spread (no explicit taker fee on CLOB market orders)

auth = KalshiAuth()
app = Flask(__name__)

_kalshi_session = requests.Session()

# === Database ===

def get_db():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    return conn


def init_db():
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS arb_scan (
                    id SERIAL PRIMARY KEY,
                    scanned_at TIMESTAMPTZ DEFAULT NOW(),
                    asset TEXT,
                    kalshi_event TEXT,
                    kalshi_ticker TEXT,
                    kalshi_strike NUMERIC,
                    kalshi_close_time TIMESTAMPTZ,
                    poly_event TEXT,
                    poly_market_id TEXT,
                    poly_strike NUMERIC,
                    poly_close_time TIMESTAMPTZ,
                    kalshi_yes_ask NUMERIC,
                    kalshi_no_ask NUMERIC,
                    poly_yes_ask NUMERIC,
                    poly_no_ask NUMERIC,
                    best_combined NUMERIC,
                    direction TEXT,
                    is_true_arb BOOLEAN,
                    time_delta_min NUMERIC,
                    strike_delta_pct NUMERIC
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS arb_scan_scanned_at_idx ON arb_scan (scanned_at DESC)")
            cur.execute("CREATE INDEX IF NOT EXISTS arb_scan_is_true_arb_idx ON arb_scan (is_true_arb)")
    finally:
        conn.close()


# === Kalshi fetch ===

def kalshi_get(path):
    url = f"{KALSHI_HOST}/trade-api/v2{path}"
    headers = auth.get_headers("GET", f"/trade-api/v2{path}")
    r = _kalshi_session.get(url, headers=headers, timeout=15)
    r.raise_for_status()
    return r.json()


def parse_kalshi_strike(ticker: str) -> float | None:
    """KXBTCD-26APR1717-T82249.99  →  82249.99
    KXBTCD-26APR1717-B82125         →  82125.0
    """
    tail = ticker.rsplit("-", 1)[-1]
    if tail and tail[0] in ("T", "B"):
        try:
            return float(tail[1:])
        except Exception:
            return None
    return None


def fetch_kalshi_crypto_events() -> list[dict]:
    """Return list of {event_ticker, asset, end_time, strikes[]}."""
    events_out = []
    for series in KALSHI_SERIES:
        try:
            evs = kalshi_get(f"/events?series_ticker={series}&status=open&limit=20").get("events", [])
        except Exception as e:
            logger.error("kalshi events %s failed: %s", series, e)
            continue
        for ev in evs:
            et = ev.get("event_ticker", "")
            if not et:
                continue
            # Get all markets for this event
            cursor = None
            strikes = []
            end_time = None
            while True:
                path = f"/markets?event_ticker={et}&status=open&limit=200"
                if cursor:
                    path += f"&cursor={cursor}"
                try:
                    resp = kalshi_get(path)
                except Exception as e:
                    logger.error("kalshi markets %s failed: %s", et, e)
                    break
                for m in resp.get("markets", []):
                    strike = parse_kalshi_strike(m.get("ticker", ""))
                    if strike is None:
                        continue
                    try:
                        yes_ask = float(m.get("yes_ask_dollars") or 0)
                        no_ask = float(m.get("no_ask_dollars") or 0)
                        yes_bid = float(m.get("yes_bid_dollars") or 0)
                        no_bid = float(m.get("no_bid_dollars") or 0)
                    except Exception:
                        continue
                    if yes_ask <= 0 or no_ask <= 0:
                        continue
                    ct = m.get("close_time")
                    if ct and end_time is None:
                        try:
                            end_time = datetime.fromisoformat(ct.replace("Z", "+00:00"))
                        except Exception:
                            pass
                    strikes.append({
                        "ticker": m.get("ticker"),
                        "strike": strike,
                        "yes_ask": yes_ask,
                        "no_ask": no_ask,
                        "yes_bid": yes_bid,
                        "no_bid": no_bid,
                    })
                cursor = resp.get("cursor")
                if not cursor:
                    break
            strikes.sort(key=lambda s: s["strike"])
            if strikes:
                events_out.append({
                    "event_ticker": et,
                    "asset": SERIES_TO_ASSET.get(series),
                    "end_time": end_time,
                    "strikes": strikes,
                })
    return events_out


# === Pairing + arb check ===

def pair_events(kalshi_events: list[dict], poly_events: list[dict]) -> list[tuple]:
    """Return list of (kalshi_ev, poly_ev, delta_minutes) pairs where:
    - same asset
    - close_time within SETTLEMENT_WINDOW_MINUTES
    """
    pairs = []
    for k in kalshi_events:
        if not k["end_time"]:
            continue
        for p in poly_events:
            if p["asset"] != k["asset"]:
                continue
            if not p["end_time"]:
                continue
            delta = abs((k["end_time"] - p["end_time"]).total_seconds() / 60.0)
            if delta <= SETTLEMENT_WINDOW_MINUTES:
                pairs.append((k, p, delta))
    return pairs


def match_strikes(k_strikes: list[dict], p_strikes: list[dict]) -> list[tuple]:
    """For each Kalshi strike find the nearest Polymarket strike within
    STRIKE_TOLERANCE_PCT. Returns list of (k_strike_dict, p_strike_dict, pct_diff)."""
    matches = []
    for ks in k_strikes:
        k_strike = ks["strike"]
        best = None
        best_diff = None
        for ps in p_strikes:
            diff_pct = abs(ps["strike"] - k_strike) / k_strike * 100
            if best_diff is None or diff_pct < best_diff:
                best_diff = diff_pct
                best = ps
        if best and best_diff <= STRIKE_TOLERANCE_PCT:
            matches.append((ks, best, best_diff))
    return matches


def check_arb(k_strike: dict, p_strike: dict) -> list[dict]:
    """Two possible cross-venue bundle arbs per strike pair:
        A: buy Kalshi YES + Polymarket NO
        B: buy Kalshi NO + Polymarket YES
    Returns list of opportunities (may be 0, 1, or 2).
    """
    opps = []
    # Kalshi taker fee eats up to $0.02/contract; Poly has ~0 explicit fee (cost in spread).
    conservative_fee = FEE_CAP

    # Direction A: Kalshi YES + Poly NO
    cost_a = k_strike["yes_ask"] + p_strike["no_ask"] + conservative_fee
    if cost_a < 1.00 - MIN_EDGE + conservative_fee:  # normalize
        profit_a = 1.00 - (k_strike["yes_ask"] + p_strike["no_ask"]) - conservative_fee
        opps.append({
            "direction": "kalshi_yes+poly_no",
            "combined_before_fees": round(k_strike["yes_ask"] + p_strike["no_ask"], 4),
            "combined_after_fees": round(k_strike["yes_ask"] + p_strike["no_ask"] + conservative_fee, 4),
            "profit": round(profit_a, 4),
            "is_true_arb": profit_a >= MIN_EDGE,
        })

    # Direction B: Kalshi NO + Poly YES
    cost_b = k_strike["no_ask"] + p_strike["yes_ask"] + conservative_fee
    if cost_b < 1.00 - MIN_EDGE + conservative_fee:
        profit_b = 1.00 - (k_strike["no_ask"] + p_strike["yes_ask"]) - conservative_fee
        opps.append({
            "direction": "kalshi_no+poly_yes",
            "combined_before_fees": round(k_strike["no_ask"] + p_strike["yes_ask"], 4),
            "combined_after_fees": round(k_strike["no_ask"] + p_strike["yes_ask"] + conservative_fee, 4),
            "profit": round(profit_b, 4),
            "is_true_arb": profit_b >= MIN_EDGE,
        })

    # Also log "near misses" where combined (no fees) < 1.00
    if not opps:
        for name, combined in [
            ("kalshi_yes+poly_no", k_strike["yes_ask"] + p_strike["no_ask"]),
            ("kalshi_no+poly_yes", k_strike["no_ask"] + p_strike["yes_ask"]),
        ]:
            if combined <= 1.01:  # within 1¢ of par
                opps.append({
                    "direction": name,
                    "combined_before_fees": round(combined, 4),
                    "combined_after_fees": round(combined + conservative_fee, 4),
                    "profit": round(1.00 - combined - conservative_fee, 4),
                    "is_true_arb": False,
                })
    return opps


def log_scan(conn, asset, k_ev, k_strike, p_ev, p_strike, delta_min, strike_diff_pct, opps):
    with conn.cursor() as cur:
        for o in opps:
            cur.execute(
                """
                INSERT INTO arb_scan (
                    asset, kalshi_event, kalshi_ticker, kalshi_strike, kalshi_close_time,
                    poly_event, poly_market_id, poly_strike, poly_close_time,
                    kalshi_yes_ask, kalshi_no_ask, poly_yes_ask, poly_no_ask,
                    best_combined, direction, is_true_arb, time_delta_min, strike_delta_pct
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    asset, k_ev["event_ticker"], k_strike["ticker"], k_strike["strike"], k_ev["end_time"],
                    p_ev["event_ticker"], p_strike["market_id"], p_strike["strike"], p_ev["end_time"],
                    k_strike["yes_ask"], k_strike["no_ask"], p_strike["yes_ask"], p_strike["no_ask"],
                    o["combined_before_fees"], o["direction"], o["is_true_arb"],
                    delta_min, strike_diff_pct,
                ),
            )


# === Main loop ===

def run_cycle():
    conn = get_db()
    try:
        logger.info("cycle: fetching Kalshi + Polymarket...")
        kalshi_events = fetch_kalshi_crypto_events()
        logger.info("Kalshi: %d events, %d strikes",
                    len(kalshi_events), sum(len(e["strikes"]) for e in kalshi_events))

        poly_events = fetch_crypto_universe()
        logger.info("Polymarket: %d events, %d strikes",
                    len(poly_events), sum(len(e["strikes"]) for e in poly_events))

        pairs = pair_events(kalshi_events, poly_events)
        logger.info("Paired events (same asset, <%dm apart): %d",
                    SETTLEMENT_WINDOW_MINUTES, len(pairs))

        total_arb_hits = 0
        total_near = 0
        scanned_strikes = 0
        for k_ev, p_ev, delta_min in pairs:
            matches = match_strikes(k_ev["strikes"], p_ev["strikes"])
            scanned_strikes += len(matches)
            for ks, ps, diff_pct in matches:
                opps = check_arb(ks, ps)
                if opps:
                    log_scan(conn, k_ev["asset"], k_ev, ks, p_ev, ps, delta_min, diff_pct, opps)
                    for o in opps:
                        if o["is_true_arb"]:
                            total_arb_hits += 1
                            logger.warning(
                                "*** TRUE ARB *** %s %s@%s <-> %s@%s  %s  profit=$%.4f",
                                k_ev["asset"], k_ev["event_ticker"], ks["strike"],
                                p_ev["event_ticker"], ps["strike"],
                                o["direction"], o["profit"],
                            )
                        else:
                            total_near += 1

        logger.info(
            "cycle done: %d strike matches scanned, %d TRUE arbs, %d near misses",
            scanned_strikes, total_arb_hits, total_near,
        )
    except Exception as e:
        logger.error("cycle error: %s", traceback.format_exc())
    finally:
        conn.close()


def bot_loop():
    logger.info("ARB SCANNER started (paper-mode, read-only discovery)")
    while True:
        try:
            run_cycle()
        except Exception:
            logger.error(traceback.format_exc())
        time.sleep(CYCLE_SECONDS)


# === Flask health/status ===

@app.route("/")
def index():
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT COUNT(*) AS n FROM arb_scan")
            total = cur.fetchone()["n"]
            cur.execute("SELECT COUNT(*) AS n FROM arb_scan WHERE is_true_arb = true")
            arbs = cur.fetchone()["n"]
            cur.execute(
                "SELECT * FROM arb_scan WHERE is_true_arb = true "
                "ORDER BY scanned_at DESC LIMIT 20"
            )
            recent_arbs = cur.fetchall()
            cur.execute(
                "SELECT asset, COUNT(*) AS n, MIN(best_combined) AS tightest "
                "FROM arb_scan WHERE scanned_at > NOW() - INTERVAL '1 hour' "
                "GROUP BY asset"
            )
            by_asset = cur.fetchall()
    finally:
        conn.close()
    return jsonify({
        "status": "running",
        "total_scans": total,
        "true_arb_hits": arbs,
        "recent_arbs": [{k: str(v) for k, v in row.items()} for row in recent_arbs],
        "last_hour_by_asset": [{k: str(v) for k, v in row.items()} for row in by_asset],
    })


if __name__ == "__main__":
    init_db()
    Thread(target=bot_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT)
