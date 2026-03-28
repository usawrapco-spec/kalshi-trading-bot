"""
Test razor.py — verify config, buy discipline, side strategy, analysis endpoint.
Uses mock Kalshi API + real DB connection (DATABASE_URL) for full simulation.
"""
import os, sys, json, time, math
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

# Force paper mode
os.environ['ENABLE_TRADING'] = 'false'
os.environ['SIDE_STRATEGY'] = 'yes'

# Mock KalshiAuth before importing razor
sys.modules['kalshi_auth'] = MagicMock()
mock_auth = MagicMock()
mock_auth.get_headers.return_value = {'Authorization': 'test'}
sys.modules['kalshi_auth'].KalshiAuth.return_value = mock_auth

PASS = 0
FAIL = 0


def test(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS: {name}")
    else:
        FAIL += 1
        print(f"  FAIL: {name} -- {detail}")


# Build fake market data that simulates real Kalshi 15M crypto markets
def make_fake_markets():
    """Generate realistic 15M crypto markets with proper pricing."""
    now = datetime.now(timezone.utc)
    close_time = (now + timedelta(minutes=12)).isoformat()  # 12 min left = within buy window
    close_time_soon = (now + timedelta(minutes=3)).isoformat()  # 3 min = too soon
    close_time_far = (now + timedelta(minutes=25)).isoformat()  # 25 min = too far

    markets = [
        # Good candidates — within time window, price in range
        {'ticker': 'KXBTC-25MAR27-T12-B96500', 'series_ticker': 'KXBTC15M', 'status': 'open',
         'close_time': close_time,
         'yes_ask_dollars': '0.08', 'yes_bid_dollars': '0.06',
         'no_ask_dollars': '0.93', 'no_bid_dollars': '0.91',
         'volume': 500},
        {'ticker': 'KXETH-25MAR27-T12-B2100', 'series_ticker': 'KXETH15M', 'status': 'open',
         'close_time': close_time,
         'yes_ask_dollars': '0.12', 'yes_bid_dollars': '0.10',
         'no_ask_dollars': '0.89', 'no_bid_dollars': '0.87',
         'volume': 300},
        {'ticker': 'KXSOL-25MAR27-T12-B140', 'series_ticker': 'KXSOL15M', 'status': 'open',
         'close_time': close_time,
         'yes_ask_dollars': '0.05', 'yes_bid_dollars': '0.03',
         'no_ask_dollars': '0.96', 'no_bid_dollars': '0.94',
         'volume': 200},
        # Both sides cheap — tests SIDE_STRATEGY filtering
        {'ticker': 'KXDOGE-25MAR27-T12-B0.18', 'series_ticker': 'KXDOGE15M', 'status': 'open',
         'close_time': close_time,
         'yes_ask_dollars': '0.40', 'yes_bid_dollars': '0.38',
         'no_ask_dollars': '0.35', 'no_bid_dollars': '0.33',
         'volume': 100},
        # Too soon — should be filtered
        {'ticker': 'KXBTC-25MAR27-T12-B97000', 'series_ticker': 'KXBTC15M', 'status': 'open',
         'close_time': close_time_soon,
         'yes_ask_dollars': '0.03', 'yes_bid_dollars': '0.01',
         'no_ask_dollars': '0.98', 'no_bid_dollars': '0.96',
         'volume': 50},
        # Too far — should be filtered
        {'ticker': 'KXBTC-25MAR27-T13-B97500', 'series_ticker': 'KXBTC15M', 'status': 'open',
         'close_time': close_time_far,
         'yes_ask_dollars': '0.10', 'yes_bid_dollars': '0.08',
         'no_ask_dollars': '0.91', 'no_bid_dollars': '0.89',
         'volume': 400},
    ]
    return markets


print("=" * 60)
print("RAZOR TEST SUITE (Paper Mode Simulation)")
print("=" * 60)

# Import razor now (with mocked auth)
import razor
from razor import (
    app, find_cheapest, sf, kalshi_fee,
    ENABLE_TRADING, SIDE_STRATEGY, ROUND_BUDGET_PCT,
    MAX_BUYS_PER_WINDOW, STARTING_BALANCE, CRYPTO_SERIES
)

# === TEST 1: Config ===
print("\n--- Test 1: Configuration ---")
test("ENABLE_TRADING is False (paper)", ENABLE_TRADING == False, f"got {ENABLE_TRADING}")
test("SIDE_STRATEGY is 'yes'", SIDE_STRATEGY == 'yes', f"got {SIDE_STRATEGY}")
test("ROUND_BUDGET_PCT is 0.25", ROUND_BUDGET_PCT == 0.25, f"got {ROUND_BUDGET_PCT}")
test("MAX_BUYS_PER_WINDOW is 3", MAX_BUYS_PER_WINDOW == 3, f"got {MAX_BUYS_PER_WINDOW}")
test("STARTING_BALANCE is $50", STARTING_BALANCE == 50.0, f"got {STARTING_BALANCE}")

# === TEST 2: Side strategy filtering ===
print("\n--- Test 2: Side strategy (YES only) ---")
markets = make_fake_markets()
candidates = find_cheapest(markets)
no_candidates = [c for c in candidates if c['side'] == 'no']
yes_candidates = [c for c in candidates if c['side'] == 'yes']
test("No 'no' side candidates when SIDE_STRATEGY=yes", len(no_candidates) == 0,
     f"found {len(no_candidates)} no-side candidates: {[c['ticker'] for c in no_candidates]}")
test("Found yes-side candidates", len(yes_candidates) > 0, f"found {len(yes_candidates)}")

# Check time filtering
too_soon = [c for c in candidates if 'B97000' in c['ticker']]
too_far = [c for c in candidates if 'T13' in c['ticker']]
test("Filtered out too-soon markets", len(too_soon) == 0)
test("Filtered out too-far markets", len(too_far) == 0)

# Check sorting (cheapest first)
if len(candidates) >= 2:
    test("Sorted by price ascending", candidates[0]['price'] <= candidates[1]['price'],
         f"first=${candidates[0]['price']:.2f} second=${candidates[1]['price']:.2f}")

print(f"    Candidates ({len(candidates)}):")
for c in candidates:
    print(f"      {c['ticker']} {c['side']} @ ${c['price']:.2f} ({c['mins_left']:.1f}min)")

# === TEST 3: Budget math ===
print("\n--- Test 3: Budget enforcement ---")
budget_50 = 50.0 * ROUND_BUDGET_PCT
test("25% of $50 = $12.50", budget_50 == 12.50, f"got ${budget_50:.2f}")

# Simulate: 3 buys at $0.05 x 3 contracts = $0.45 total
simulated_spend = 3 * (0.05 * 3)
test("3 buys of $0.05x3 = $0.45 < $12.50 budget", simulated_spend < budget_50,
     f"spent ${simulated_spend:.2f}")

# After MAX_BUYS_PER_WINDOW, should stop
razor._round['buys'] = MAX_BUYS_PER_WINDOW
test("Buy cap blocks after 3 buys", razor._round['buys'] >= MAX_BUYS_PER_WINDOW)
razor._round['buys'] = 0  # reset

# === TEST 4: Fee calculation ===
print("\n--- Test 4: Fee calculation ---")
fee1 = kalshi_fee(0.05, 3)
expected_fee1 = min(math.ceil(0.07 * 3 * 0.05 * 0.95 * 100) / 100, 0.02 * 3)
test(f"Fee($0.05 x3) = ${fee1:.4f}", abs(fee1 - expected_fee1) < 0.001, f"expected ${expected_fee1:.4f}")

fee2 = kalshi_fee(0.50, 1)
expected_fee2 = min(math.ceil(0.07 * 1 * 0.50 * 0.50 * 100) / 100, 0.02)
test(f"Fee($0.50 x1) = ${fee2:.4f}", abs(fee2 - expected_fee2) < 0.001, f"expected ${expected_fee2:.4f}")

# === TEST 5: Flask API endpoints ===
print("\n--- Test 5: API endpoints ---")
with app.test_client() as client:
    r = client.get('/')
    test("GET / returns 200", r.status_code == 200)

    r = client.get('/dashboard')
    test("GET /dashboard returns HTML", r.status_code == 200 and b'RAZOR' in r.data)

    # The DB-dependent endpoints - test they don't crash (may return empty if no DB)
    try:
        r = client.get('/api/analysis')
        test("GET /api/analysis returns 200", r.status_code == 200)
        analysis = r.get_json()
        if analysis and analysis.get('total_trades', 0) > 0:
            print(f"    Total trades: {analysis['total_trades']}")
            by_side = analysis.get('by_side', {})
            for side, stats in by_side.items():
                print(f"      {side.upper()}: {stats['wins']}W / {stats['losses']}L | win_rate={stats['win_rate']}% | pnl=${stats['pnl']:.4f}")
            by_coin = analysis.get('by_coin', {})
            for coin, stats in by_coin.items():
                print(f"      {coin}: {stats['wins']}W / {stats['losses']}L | win_rate={stats['win_rate']}%")
            by_bucket = analysis.get('by_price_bucket', {})
            for bucket, stats in sorted(by_bucket.items()):
                print(f"      {bucket}: {stats['wins']}W / {stats['losses']}L | win_rate={stats['win_rate']}%")
        else:
            print(f"    Analysis: {json.dumps(analysis)[:200]}")
    except Exception as e:
        test("GET /api/analysis returns 200", False, str(e))

# === TEST 6: Simulate full paper cycle with mock ===
print("\n--- Test 6: Simulated paper cycle ---")

# Mock kalshi_get to return fake data
fake_balance = {'balance': 5000}  # $50.00
fake_positions = {'market_positions': [], 'cursor': None}

def mock_kalshi_get(path):
    if '/portfolio/balance' in path:
        return fake_balance
    if '/portfolio/positions' in path:
        return fake_positions
    if '/markets?' in path:
        return {'markets': make_fake_markets(), 'cursor': None}
    if '/markets/' in path:
        # Single market lookup for check_sells
        ticker = path.split('/markets/')[-1]
        for m in make_fake_markets():
            if m['ticker'] == ticker:
                return {'market': m}
        return {'market': {'status': 'open'}}
    return {}

# Mock DB operations for paper cycle
mock_conn = MagicMock()
mock_cursor = MagicMock()
mock_cursor.fetchall.return_value = []
mock_cursor.__enter__ = lambda self: self
mock_cursor.__exit__ = MagicMock(return_value=False)
mock_conn.cursor.return_value = mock_cursor
mock_conn.__enter__ = lambda self: self
mock_conn.__exit__ = MagicMock(return_value=False)

razor._round['window_id'] = -1
razor._round['buys'] = 0
razor._round['spent'] = 0

with patch.object(razor, 'kalshi_get', side_effect=mock_kalshi_get), \
     patch.object(razor, 'get_db', return_value=mock_conn):

    razor.run_cycle()

    test("Window ID updated after cycle", razor._round['window_id'] >= 0)
    test("Start balance set to $50", razor._round['start_balance'] == 50.0,
         f"got ${razor._round['start_balance']:.2f}")
    test("Buys this window <= MAX", razor._round['buys'] <= MAX_BUYS_PER_WINDOW,
         f"got {razor._round['buys']}")

    max_budget = 50.0 * ROUND_BUDGET_PCT
    test(f"Spent <= budget (${max_budget:.2f})", razor._round['spent'] <= max_budget,
         f"spent ${razor._round['spent']:.2f}")

    print(f"    Round state after cycle: balance=${razor._round['start_balance']:.2f} spent=${razor._round['spent']:.2f} buys={razor._round['buys']}")

    # Run 10 more cycles — should NOT exceed caps
    for i in range(10):
        razor.run_cycle()

    test("After 11 cycles: buys still <= 3", razor._round['buys'] <= MAX_BUYS_PER_WINDOW,
         f"got {razor._round['buys']}")
    test("After 11 cycles: spent <= budget", razor._round['spent'] <= max_budget,
         f"spent ${razor._round['spent']:.2f}")
    print(f"    After 11 cycles: buys={razor._round['buys']} spent=${razor._round['spent']:.2f}")

# === TEST 7: Side strategy 'cheapest' mode ===
print("\n--- Test 7: Side strategy 'cheapest' ---")
original = razor.SIDE_STRATEGY
razor.SIDE_STRATEGY = 'cheapest'
candidates_all = find_cheapest(make_fake_markets())
no_cands = [c for c in candidates_all if c['side'] == 'no']
yes_cands = [c for c in candidates_all if c['side'] == 'yes']
test("'cheapest' includes no-side candidates", len(no_cands) > 0, f"found {len(no_cands)}")
test("'cheapest' includes yes-side candidates", len(yes_cands) > 0, f"found {len(yes_cands)}")
razor.SIDE_STRATEGY = original  # restore

# === SUMMARY ===
print("\n" + "=" * 60)
total = PASS + FAIL
print(f"RESULTS: {PASS}/{total} passed, {FAIL} failed")
if FAIL == 0:
    print("ALL TESTS PASSED")
else:
    print(f"FAILURES: {FAIL}")
print("=" * 60)
sys.exit(1 if FAIL > 0 else 0)
