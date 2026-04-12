"""
Polymarket Gamma API client — READ ONLY.

Fetches crypto strike-grid events and returns normalized strike books.
No auth required. Does NOT place orders — use Polymarket CLOB API for that.

Docs: https://docs.polymarket.com/developers/gamma-markets-api/overview
"""
import re
import logging
import requests
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "kalshi-trading-bot/arb-discovery"})

# Event slug patterns we care about — "Bitcoin above ___ on April 12", etc.
# These are Polymarket's daily crypto strike events.
CRYPTO_EVENT_SLUG_PATTERNS = [
    re.compile(r"^bitcoin-above-on-"),
    re.compile(r"^ethereum-above-on-"),
    re.compile(r"^solana-above-on-"),
    re.compile(r"^xrp-above-on-"),
]

ASSET_FROM_SLUG = {
    "bitcoin": "BTC", "ethereum": "ETH", "solana": "SOL", "xrp": "XRP",
}

# Match "Will the price of Bitcoin be above $74,000 on April 12?"
STRIKE_RE = re.compile(
    r"price of (\w+).*?\babove \$([\d,]+(?:\.\d+)?)", re.IGNORECASE
)


def _get(path: str, params: Optional[dict] = None):
    r = SESSION.get(f"{GAMMA_BASE}{path}", params=params or {}, timeout=15)
    r.raise_for_status()
    return r.json()


def list_active_events(limit: int = 500) -> list[dict]:
    """Return all active non-closed events ordered by volume."""
    return _get("/events", {
        "active": "true", "closed": "false", "limit": limit,
        "order": "volume24hr", "ascending": "false",
    })


def find_crypto_strike_events() -> list[dict]:
    """Return events whose slug looks like `bitcoin-above-on-...` etc."""
    events = list_active_events(500)
    hits = []
    for ev in events:
        slug = ev.get("slug", "")
        if any(p.match(slug) for p in CRYPTO_EVENT_SLUG_PATTERNS):
            hits.append(ev)
    return hits


def parse_event_to_strikes(event: dict) -> dict:
    """Normalize a Polymarket strike event into:
        {
          'event_ticker': str,
          'asset': 'BTC'/'ETH'/...,
          'end_time': datetime (UTC),
          'strikes': [
             {'strike': float, 'yes_bid': float, 'yes_ask': float,
              'no_bid': float, 'no_ask': float, 'market_id': str}
          ]
        }
    Polymarket binary markets quote only the "Yes" side via bestBid/bestAsk.
    For a YES/NO binary, no_bid = 1 - yes_ask, no_ask = 1 - yes_bid.
    """
    slug = event.get("slug", "")
    title = event.get("title", "")
    asset = None
    for k, v in ASSET_FROM_SLUG.items():
        if slug.startswith(f"{k}-above-on-"):
            asset = v
            break

    end_iso = event.get("endDate", "")
    try:
        end_time = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
    except Exception:
        end_time = None

    strikes = []
    for m in event.get("markets", []):
        q = m.get("question", "")
        mm = STRIKE_RE.search(q)
        if not mm:
            continue
        try:
            strike_val = float(mm.group(2).replace(",", ""))
        except Exception:
            continue

        try:
            yes_ask = float(m.get("bestAsk") or 0)
            yes_bid = float(m.get("bestBid") or 0)
        except Exception:
            yes_ask = yes_bid = 0.0

        # Only accept binary YES/NO markets with live quotes
        if yes_ask <= 0 or yes_bid <= 0:
            continue

        strikes.append({
            "strike": strike_val,
            "yes_bid": yes_bid,
            "yes_ask": yes_ask,
            "no_bid": round(1.0 - yes_ask, 4),
            "no_ask": round(1.0 - yes_bid, 4),
            "market_id": m.get("id") or m.get("conditionId"),
            "question": q,
        })

    strikes.sort(key=lambda s: s["strike"])
    return {
        "event_ticker": event.get("ticker", ""),
        "slug": slug,
        "title": title,
        "asset": asset,
        "end_time": end_time,
        "strikes": strikes,
    }


def fetch_crypto_universe() -> list[dict]:
    """High-level: return a list of normalized strike-grid events across BTC/ETH/etc."""
    events = find_crypto_strike_events()
    out = []
    for ev in events:
        parsed = parse_event_to_strikes(ev)
        if parsed["asset"] and parsed["strikes"]:
            out.append(parsed)
    logger.info(
        "polymarket: %d crypto strike events, %d total strikes",
        len(out), sum(len(e["strikes"]) for e in out),
    )
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    uni = fetch_crypto_universe()
    for ev in uni:
        print(f"\n{ev['asset']} {ev['event_ticker']} ends {ev['end_time']} ({len(ev['strikes'])} strikes)")
        for s in ev["strikes"]:
            print(
                f"  ${s['strike']:>10,.0f}  "
                f"yes={s['yes_bid']:.3f}/{s['yes_ask']:.3f}  "
                f"no={s['no_bid']:.3f}/{s['no_ask']:.3f}"
            )
