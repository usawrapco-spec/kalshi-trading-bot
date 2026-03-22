"""
KALSHI ALPHA — Neural Cortex Dashboard
Sci-fi terminal-style trading dashboard with real-time Supabase data.
"""

import os
import json
import threading
from flask import Flask, jsonify

app = Flask(__name__)

# --- Supabase connection ---
_db = None
def get_db():
    global _db
    if _db is None:
        try:
            from utils.supabase_db import SupabaseDB
            _db = SupabaseDB()
        except Exception as e:
            print(f"Dashboard DB init failed: {e}")
    return _db


# ============================================================
#  API ENDPOINTS — Real Supabase data
# ============================================================

@app.route('/')
def health():
    return "OK"

@app.route('/api/status')
def api_status():
    try:
        db = get_db()

        # Fetch ALL trades once, split by live vs paper
        all_trades_result = db.client.table('kalshi_trades').select('*').execute()
        all_trades = all_trades_result.data or []

        live_trades = [t for t in all_trades if t.get('order_id') not in (None, 'paper', 'forced_paper')]
        paper_trades = [t for t in all_trades if t.get('order_id') in (None, 'paper', 'forced_paper')]

        # --- LIVE stats using resolved/pnl columns ---
        live_open = [t for t in live_trades if not t.get('resolved')]
        live_settled = [t for t in live_trades if t.get('resolved')]
        live_open_cost = sum(t.get('price', 0) * t.get('count', 0) for t in live_open)
        live_realized_pnl = sum(t.get('pnl', 0) or 0 for t in live_settled)
        live_wins = sum(1 for t in live_settled if (t.get('pnl') or 0) > 0)
        live_losses = sum(1 for t in live_settled if (t.get('pnl') or 0) <= 0)

        # --- PAPER stats using resolved/pnl columns ---
        paper_open = [t for t in paper_trades if not t.get('resolved')]
        paper_settled = [t for t in paper_trades if t.get('resolved')]
        paper_open_cost = sum(t.get('price', 0) * t.get('count', 0) for t in paper_open)
        paper_realized_pnl = sum(t.get('pnl', 0) or 0 for t in paper_settled)
        paper_balance = 100.0 - paper_open_cost + paper_realized_pnl
        paper_wins = sum(1 for t in paper_settled if (t.get('pnl') or 0) > 0)
        paper_losses = sum(1 for t in paper_settled if (t.get('pnl') or 0) <= 0)

        # Get latest bot status
        status_result = db.client.table('kalshi_bot_status').select('*').order('id', desc=True).limit(1).execute()
        r = status_result.data[0] if status_result.data else {}

        # FETCH REAL KALSHI CASH BALANCE FROM API
        real_cash = None
        try:
            from utils.kalshi_client import KalshiAPIClient
            kalshi_client = KalshiAPIClient()
            balance_response = kalshi_client.get_balance()
            if balance_response and 'balance' in balance_response:
                real_cash = balance_response['balance'] / 100.0
        except Exception as e:
            print(f"Failed to fetch Kalshi balance: {e}")
            real_cash = r.get('real_balance')

        # Portfolio value = cash + open position cost basis
        portfolio_value = None
        live_daily_pnl = None
        starting_balance = 10.00
        if real_cash is not None:
            portfolio_value = real_cash + live_open_cost
            live_daily_pnl = portfolio_value - starting_balance

        return jsonify({
            'is_running': r.get('is_running', False),
            'last_check': r.get('last_check'),
            'paper': {
                'balance': round(paper_balance, 2),
                'daily_pnl': round(paper_realized_pnl, 2),
                'positions': len(paper_open),
                'roi_percent': round(((paper_balance - 100) / 100) * 100, 2),
                'trades_today': len(paper_trades),
                'wins': paper_wins,
                'losses': paper_losses,
            },
            'live': {
                'balance': portfolio_value,
                'cash': real_cash,
                'positions_value': round(live_open_cost, 2),
                'daily_pnl': live_daily_pnl,
                'realized_pnl': round(live_realized_pnl, 2),
                'positions': len(live_open),
                'wins': live_wins,
                'losses': live_losses,
                'total_exposure': round(live_open_cost, 2),
                'max_exposure': 5.00,
            },
        })
    except Exception as e:
        return jsonify({
            'is_running': False, 'last_check': None, 'error': str(e),
            'paper': {'balance': 100, 'daily_pnl': 0, 'positions': 0, 'roi_percent': 0, 'trades_today': 0, 'wins': 0, 'losses': 0},
            'live': {'balance': None, 'daily_pnl': None, 'positions': 0, 'wins': 0, 'losses': 0, 'total_exposure': 0, 'max_exposure': 5.00},
        })

@app.route('/api/trades')
def api_trades():
    try:
        db = get_db()
        result = db.client.table('kalshi_trades').select('*').order('id', desc=True).limit(30).execute()
        trades = []
        for t in (result.data or []):
            trades.append({
                'timestamp': t.get('timestamp') or t.get('created_at'),
                'ticker': t.get('ticker', ''),
                'side': t.get('side', ''),
                'price': t.get('price', 0),
                'count': t.get('count', 0),
                'strategy': t.get('strategy', ''),
                'is_live': t.get('order_id') not in (None, 'paper', 'forced_paper'),
                'order_id': t.get('order_id', ''),
                'confidence': t.get('confidence', 0),
                'reason': (t.get('reason') or '')[:100],
            })
        return jsonify(trades)
    except:
        return jsonify([])

@app.route('/api/strategies')
def api_strategies():
    try:
        db = get_db()

        # Get all trades
        result = db.client.table('kalshi_trades').select('*').execute()
        all_trades = result.data or []

        # Separate live and paper trades
        live_trades = [t for t in all_trades if t.get('order_id') and t['order_id'] != 'paper' and t['order_id'] != 'forced_paper']
        paper_trades = [t for t in all_trades if t not in live_trades]

        # Count by strategy
        strategies = {}

        def count_trades(trades, trade_type):
            for t in trades:
                s = t.get('strategy', 'unknown')
                if s not in strategies:
                    strategies[s] = {'strategy': s, 'live_trades': 0, 'paper_trades': 0, 'wins': 0, 'losses': 0}

                if trade_type == 'live':
                    strategies[s]['live_trades'] += 1
                else:
                    strategies[s]['paper_trades'] += 1

                reason = (t.get('reason') or '').upper()
                if 'WIN' in reason: strategies[s]['wins'] += 1
                elif 'LOSS' in reason: strategies[s]['losses'] += 1

        count_trades(live_trades, 'live')
        count_trades(paper_trades, 'paper')

        return jsonify(list(strategies.values()))
    except Exception as e:
        return jsonify([])

@app.route('/api/equity')
def api_equity():
    try:
        db = get_db()
        result = db.client.table('equity_snapshots').select('timestamp,balance').order('timestamp', desc=False).limit(500).execute()
        if result.data and len(result.data) > 2:
            return jsonify(result.data)
        result = db.client.table('kalshi_bot_status').select('last_check,balance').order('id', desc=False).execute()
        data = result.data or []
        thinned = data[::20]
        if data and (not thinned or thinned[-1] != data[-1]):
            thinned.append(data[-1])
        return jsonify([{'timestamp': r['last_check'], 'balance': r['balance']} for r in thinned])
    except:
        return jsonify([])

@app.route('/api/signals')
def api_signals():
    try:
        db = get_db()
        result = db.client.table('signal_evaluations').select(
            'timestamp,strategy,ticker,market_title,side,yes_price,no_price,'
            'our_probability,market_probability,edge,confidence,action,skip_reason'
        ).order('timestamp', desc=True).limit(50).execute()
        return jsonify(result.data or [])
    except:
        return jsonify([])

@app.route('/api/debates')
def api_debates():
    try:
        db = get_db()
        result = db.client.table('debate_log').select('*').order('timestamp', desc=True).limit(20).execute()
        return jsonify(result.data or [])
    except:
        return jsonify([])

@app.route('/api/improvements')
def api_improvements():
    try:
        db = get_db()
        result = db.client.table('improvement_logs').select('*').order('timestamp', desc=True).limit(5).execute()
        return jsonify(result.data or [])
    except:
        return jsonify([])

@app.route('/api/live_status')
def api_live_status():
    """Live trading status — real balance, live positions, live trades."""
    try:
        db = get_db()
        # Get latest bot status (includes real_balance and live_positions)
        status = db.client.table('kalshi_bot_status').select('*').order('id', desc=True).limit(1).execute()
        real_balance = None
        live_positions = 0
        if status.data:
            r = status.data[0]
            real_balance = r.get('real_balance')
            live_positions = r.get('live_positions', 0)

        # Get recent live trades
        live_trades = []
        try:
            result = db.client.table('kalshi_trades').select('*').eq('is_live', True).order('id', desc=True).limit(20).execute()
            live_trades = result.data or []
        except Exception:
            pass

        live_strategies_env = os.environ.get('LIVE_STRATEGIES', '')
        return jsonify({
            'real_balance': real_balance,
            'live_positions': live_positions,
            'live_trades': live_trades,
            'live_strategies': [s.strip() for s in live_strategies_env.split(',') if s.strip()],
            'enable_trading': os.environ.get('ENABLE_TRADING', 'false').lower() == 'true',
        })
    except Exception as e:
        return jsonify({'real_balance': None, 'live_positions': 0, 'live_trades': [], 'live_strategies': [], 'error': str(e)})


# ============================================================
#  NEURAL CORTEX DASHBOARD — Full HTML/CSS/JS inline
# ============================================================

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>KALSHI ALPHA | Neural Cortex</title>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;500;600;700&family=Space+Grotesk:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#050508;color:#c0c8d4;font-family:'JetBrains Mono',monospace;min-height:100vh;overflow-x:hidden}
body::after{content:'';position:fixed;top:0;left:0;right:0;bottom:0;background:repeating-linear-gradient(0deg,rgba(0,0,0,0) 0px,rgba(0,0,0,0) 2px,rgba(0,0,0,0.04) 2px,rgba(0,0,0,0.04) 4px);pointer-events:none;z-index:9999}

/* Canvas behind everything */
#bgCanvas{position:fixed;top:0;left:0;width:100%;height:100%;z-index:0;pointer-events:none}
.page{position:relative;z-index:1}

/* Header */
.hdr{display:flex;justify-content:space-between;align-items:center;padding:12px 20px;border-bottom:1px solid rgba(0,240,255,0.08);background:rgba(5,5,8,0.9);backdrop-filter:blur(10px);position:sticky;top:0;z-index:100}
.hdr-left{display:flex;align-items:center;gap:10px}
.hdr-title{font-size:1rem;font-weight:700;letter-spacing:0.2em;color:#00f0ff;text-shadow:0 0 12px rgba(0,240,255,0.4)}
.dot{width:7px;height:7px;background:#39ff14;border-radius:50%;animation:pulse 2s infinite;display:inline-block}
@keyframes pulse{0%,100%{opacity:1;box-shadow:0 0 6px #39ff14}50%{opacity:.4;box-shadow:0 0 16px #39ff14}}
.hdr-bal{font-size:1.4rem;font-weight:700;color:#00f0ff;text-shadow:0 0 10px rgba(0,240,255,0.3)}
.mode-badge{font-size:.65rem;padding:2px 8px;border:1px solid rgba(0,240,255,0.2);border-radius:2px;color:rgba(0,240,255,0.6);letter-spacing:0.1em}
.hdr-meta{font-size:.7rem;color:rgba(255,255,255,0.25)}

/* Panels */
.panel{border:1px solid rgba(0,240,255,0.1);background:rgba(0,240,255,0.015);margin:0;padding:14px 16px;position:relative}
.panel-title{font-size:.65rem;font-weight:600;letter-spacing:0.15em;text-transform:uppercase;color:rgba(0,240,255,0.5);margin-bottom:10px}
.panel-title::before{content:'◆ ';color:rgba(0,240,255,0.3)}

/* Grid */
.grid-top{display:grid;grid-template-columns:1fr 1fr;border-bottom:1px solid rgba(0,240,255,0.06)}
.grid-live-paper{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px}
.grid-main{display:grid;grid-template-columns:220px 1fr 320px;min-height:420px}
.grid-bottom{border-top:1px solid rgba(0,240,255,0.06)}
@media(max-width:1000px){.grid-top{grid-template-columns:1fr 1fr}.grid-main{grid-template-columns:1fr}}

/* Metric cards */
.metric{text-align:center;padding:16px 12px;border-right:1px solid rgba(0,240,255,0.06)}
.metric:last-child{border-right:none}
.metric-label{font-size:.6rem;letter-spacing:0.12em;text-transform:uppercase;color:rgba(255,255,255,0.25);margin-bottom:6px}
.metric-value{font-size:1.6rem;font-weight:700;color:#00f0ff;text-shadow:0 0 8px rgba(0,240,255,0.25)}
.metric-sub{font-size:.7rem;color:rgba(255,255,255,0.3);margin-top:4px}
.profit{color:#39ff14!important;text-shadow:0 0 8px rgba(57,255,20,0.3)!important}
.loss{color:#ff3333!important;text-shadow:0 0 8px rgba(255,51,51,0.3)!important}

/* Left sidebar */
.sidebar{border-right:1px solid rgba(0,240,255,0.06);padding:0;overflow-y:auto}
.strat-row{display:flex;justify-content:space-between;padding:8px 14px;border-bottom:1px solid rgba(0,240,255,0.03);font-size:.75rem}
.strat-row:hover{background:rgba(0,240,255,0.03)}
.strat-name{color:rgba(0,240,255,0.7);font-size:.65rem}
.strat-count{color:rgba(255,255,255,0.3)}

/* Center */
.center{padding:0;display:flex;flex-direction:column}
.chart-box{flex:1;padding:14px 16px;min-height:200px;position:relative}
.chart-box canvas{width:100%!important}

/* Right sidebar - log */
.logpanel{border-left:1px solid rgba(0,240,255,0.06);overflow-y:auto;font-size:.7rem;max-height:420px}
.log-entry{padding:5px 12px;border-bottom:1px solid rgba(255,255,255,0.02);line-height:1.5}
.log-time{color:rgba(255,255,255,0.15)}
.log-tag{padding:1px 4px;border-radius:1px;font-size:.6rem;margin:0 4px}
.log-tag-fwd{background:rgba(0,240,255,0.1);color:#00f0ff}
.log-tag-bwd{background:rgba(57,255,20,0.1);color:#39ff14}
.log-tag-skip{background:rgba(255,255,255,0.04);color:rgba(255,255,255,0.2)}
.log-tag-reward{background:rgba(255,215,0,0.15);color:#ffd700}
.log-tag-penalty{background:rgba(255,51,51,0.1);color:#ff3333}

/* Bottom tabs */
.tabs{display:flex;border-bottom:1px solid rgba(0,240,255,0.06)}
.tab{padding:8px 18px;font-size:.7rem;letter-spacing:0.08em;cursor:pointer;color:rgba(255,255,255,0.3);border-bottom:2px solid transparent;transition:all .2s}
.tab:hover{color:rgba(255,255,255,0.5)}
.tab.active{color:#00f0ff;border-bottom-color:#00f0ff}
.tab-body{padding:14px 16px;max-height:260px;overflow-y:auto;font-size:.75rem}
.tab-content{display:none}
.tab-content.active{display:block}

/* Trade table */
.ttable{width:100%;font-size:.72rem;border-collapse:collapse}
.ttable th{text-align:left;padding:6px 10px;color:rgba(255,255,255,0.2);font-weight:500;font-size:.6rem;text-transform:uppercase;letter-spacing:.05em;border-bottom:1px solid rgba(0,240,255,0.06)}
.ttable td{padding:6px 10px;border-bottom:1px solid rgba(255,255,255,0.02)}
.ttable tr:hover{background:rgba(0,240,255,0.02)}

/* Floating text animation */
@keyframes floatUp{0%{opacity:1;transform:translate(-50%,-50%) scale(.5)}20%{opacity:1;transform:translate(-50%,-50%) scale(1.2)}40%{opacity:1;transform:translate(-50%,-50%) scale(1)}100%{opacity:0;transform:translate(-50%,-150%) scale(1)}}

/* Scrollbar */
::-webkit-scrollbar{width:4px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:rgba(0,240,255,0.15);border-radius:2px}
</style>
</head>
<body>
<canvas id="bgCanvas"></canvas>
<div class="page">

<!-- HEADER -->
<div class="hdr">
  <div class="hdr-left">
    <span class="dot" id="statusDot"></span>
    <span class="hdr-title">KALSHI ALPHA</span>
    <span class="mode-badge" id="modeBadge">PAPER TRADING</span>
    <span id="liveBadge" style="display:none;font-size:.65rem;padding:2px 8px;border:1px solid rgba(255,60,60,0.5);border-radius:2px;color:#ff3c3c;letter-spacing:0.1em;text-shadow:0 0 6px rgba(255,60,60,0.3)">LIVE</span>
  </div>
  <div style="display:flex;align-items:center;gap:16px">
    <span class="hdr-meta">scan: <span data-field="last_scan">—</span></span>
    <span id="realBalLabel" style="display:none;font-size:.7rem;color:rgba(255,60,60,0.6)">Real:</span>
    <span id="realBal" style="display:none;font-size:1.1rem;font-weight:700;color:#ff3c3c;text-shadow:0 0 8px rgba(255,60,60,0.3)">$—</span>
    <span class="hdr-meta" style="color:rgba(255,255,255,0.15)">|</span>
    <span style="font-size:.7rem;color:rgba(0,240,255,0.4)">Paper:</span>
    <span class="hdr-bal" data-field="balance">$—</span>
  </div>
</div>

<!-- LIVE vs PAPER PANELS -->
<div class="grid-live-paper">
  <!-- LIVE TRADING PANEL -->
  <div class="panel" style="border-color:rgba(255,60,60,0.3);background:rgba(255,60,60,0.02)">
    <div class="panel-title" style="color:#ff3c3c">🔴 LIVE TRADING</div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">
      <div class="metric" style="border-right:1px solid rgba(255,60,60,0.1)">
        <div class="metric-label">Balance</div>
        <div class="metric-value" id="live-balance" style="color:#ff3c3c;text-shadow:0 0 8px rgba(255,60,60,0.3)">$—</div>
        <div class="metric-sub" id="live-exposure">— / $5.00 max</div>
      </div>
      <div class="metric">
        <div class="metric-label">Realized P&L</div>
        <div class="metric-value" id="live-pnl" style="color:#ff3c3c">$—</div>
        <div class="metric-sub" id="live-trades">—</div>
      </div>
      <div class="metric" style="border-right:1px solid rgba(255,60,60,0.1)">
        <div class="metric-label">Positions</div>
        <div class="metric-value" id="live-positions">—</div>
        <div class="metric-sub">open contracts</div>
      </div>
      <div class="metric">
        <div class="metric-label">Win Rate</div>
        <div class="metric-value" id="live-win-rate">—%</div>
        <div class="metric-sub" id="live-wl-record">—W / —L</div>
      </div>
    </div>
  </div>

  <!-- PAPER TRADING PANEL -->
  <div class="panel" style="border-color:rgba(0,240,255,0.3);background:rgba(0,240,255,0.02)">
    <div class="panel-title" style="color:#00f0ff">📝 PAPER TRADING</div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">
      <div class="metric" style="border-right:1px solid rgba(0,240,255,0.1)">
        <div class="metric-label">Balance</div>
        <div class="metric-value" id="paper-balance" style="color:#00f0ff;text-shadow:0 0 8px rgba(0,240,255,0.3)">$—</div>
        <div class="metric-sub" id="paper-roi">—% ROI</div>
      </div>
      <div class="metric">
        <div class="metric-label">Daily P&L</div>
        <div class="metric-value" id="paper-pnl" style="color:#00f0ff">$—</div>
        <div class="metric-sub" id="paper-trades">— trades today</div>
      </div>
      <div class="metric" style="border-right:1px solid rgba(0,240,255,0.1)">
        <div class="metric-label">Positions</div>
        <div class="metric-value" id="paper-positions">—</div>
        <div class="metric-sub">open contracts</div>
      </div>
      <div class="metric">
        <div class="metric-label">Win Rate</div>
        <div class="metric-value" id="paper-win-rate">—%</div>
        <div class="metric-sub" id="paper-wl-record">—W / —L</div>
      </div>
    </div>
  </div>
</div>

<!-- MAIN 3-COLUMN -->
<div class="grid-main">

  <!-- LEFT: Strategy Layers -->
  <div class="sidebar">
    <div class="panel-title" style="padding:14px 14px 8px">Layer Architecture</div>
    <div id="stratLayers">
      <div class="strat-row"><span class="strat-name">Loading...</span></div>
    </div>
    <div class="panel-title" style="padding:14px 14px 8px;margin-top:8px">Risk Status</div>
    <div style="padding:4px 14px;font-size:.72rem">
      <div style="display:flex;justify-content:space-between;padding:4px 0"><span style="color:rgba(255,255,255,0.25)">Kelly Fraction</span><span>0.10</span></div>
      <div style="display:flex;justify-content:space-between;padding:4px 0"><span style="color:rgba(255,255,255,0.25)">Max Entry</span><span>$0.15</span></div>
      <div style="display:flex;justify-content:space-between;padding:4px 0"><span style="color:rgba(255,255,255,0.25)">Min Confidence</span><span>85%</span></div>
      <div style="display:flex;justify-content:space-between;padding:4px 0"><span style="color:rgba(255,255,255,0.25)">Daily Stop</span><span>-$30</span></div>
      <div style="display:flex;justify-content:space-between;padding:4px 0"><span style="color:rgba(255,255,255,0.25)">Cycle Speed</span><span>10s</span></div>
    </div>
  </div>

  <!-- CENTER: Equity Chart -->
  <div class="center">
    <div class="panel-title" style="padding:14px 16px 0">Equity Curve — Balance Over Time</div>
    <div class="chart-box"><canvas id="equityChart"></canvas></div>
  </div>

  <!-- RIGHT: Training Log -->
  <div class="logpanel" id="trainingLog">
    <div class="panel-title" style="padding:12px 12px 8px">Training Log</div>
    <div class="log-entry"><span class="log-time">--:--:--</span> <span class="log-tag log-tag-fwd">INIT</span> Neural Cortex online...</div>
  </div>

</div>

<!-- BOTTOM TABS -->
<div class="grid-bottom">
  <div class="tabs">
    <div class="tab active" onclick="switchTab('trades',this)">RECENT TRADES</div>
    <div class="tab" onclick="switchTab('signals',this)">SIGNAL FEED</div>
    <div class="tab" onclick="switchTab('debates',this)">AI DEBATES</div>
    <div class="tab" onclick="switchTab('learn',this)">LEARNING LAB</div>
  </div>

  <div class="tab-content active" id="tab-trades">
    <div class="tab-body">
      <table class="ttable">
        <thead><tr><th>Time</th><th>Strategy</th><th>Market</th><th>Side</th><th>Price</th><th>Status</th></tr></thead>
        <tbody id="tradesBody"><tr><td colspan="6" style="color:rgba(255,255,255,0.15)">Waiting for trades...</td></tr></tbody>
      </table>
    </div>
  </div>

  <div class="tab-content" id="tab-signals">
    <div class="tab-body" id="signalFeed" style="font-size:.7rem">
      <div style="color:rgba(255,255,255,0.15)">Waiting for signals...</div>
    </div>
  </div>

  <div class="tab-content" id="tab-debates">
    <div class="tab-body" id="debatesFeed">
      <div style="color:rgba(255,255,255,0.15)">No debates yet...</div>
    </div>
  </div>

  <div class="tab-content" id="tab-learn">
    <div class="tab-body" id="learnFeed">
      <div style="color:rgba(255,255,255,0.15)">Self-improvement runs every 6 hours. Waiting for data...</div>
    </div>
  </div>
</div>

</div><!-- /page -->

<script>
// === BACKGROUND PARTICLE SYSTEM ===
const C=document.getElementById('bgCanvas'),X=C.getContext('2d');
let W,H,particles=[];
function resize(){W=C.width=innerWidth;H=C.height=innerHeight}
resize();addEventListener('resize',resize);

class P{
  constructor(x,y,o){
    this.x=x||Math.random()*W;this.y=y||Math.random()*H;
    o=o||{};
    this.vx=o.vx||(Math.random()*.3+.05);
    this.vy=o.vy||((Math.random()-.5)*.15);
    this.s=o.s||(Math.random()*1.5+.3);
    this.a=o.a||(Math.random()*.1+.03);
    this.ma=this.a;
    this.c=o.c||'0,240,255';
    this.life=o.life||Infinity;
    this.age=0;this.d=o.d||0;
  }
  update(){
    this.x+=this.vx;this.y+=this.vy;this.age++;
    if(this.d)this.a-=this.d;
    if(this.x>W+5)this.x=-5;if(this.x<-5)this.x=W+5;
    if(this.y>H+5)this.y=-5;if(this.y<-5)this.y=H+5;
  }
  draw(){
    if(this.a<=0)return;
    X.beginPath();X.arc(this.x,this.y,this.s,0,Math.PI*2);
    X.fillStyle=`rgba(${this.c},${Math.max(0,this.a)})`;X.fill();
  }
  dead(){return this.a<=0||this.age>this.life}
}

// Init ambient particles
for(let i=0;i<50;i++)particles.push(new P());

// Neural mesh lines
function drawMesh(){
  const amb=particles.filter(p=>p.life===Infinity).slice(0,50);
  for(let i=0;i<amb.length;i++){
    for(let j=i+1;j<amb.length;j++){
      const dx=amb[i].x-amb[j].x,dy=amb[i].y-amb[j].y,d=Math.sqrt(dx*dx+dy*dy);
      if(d<120){
        X.beginPath();X.moveTo(amb[i].x,amb[i].y);X.lineTo(amb[j].x,amb[j].y);
        X.strokeStyle=`rgba(0,240,255,${(1-d/120)*.04})`;X.lineWidth=.5;X.stroke();
      }
    }
  }
}

function animBg(){
  X.fillStyle='rgba(5,5,8,.12)';X.fillRect(0,0,W,H);
  particles.forEach(p=>{p.update();p.draw()});
  particles=particles.filter(p=>!p.dead());
  drawMesh();
  while(particles.filter(p=>p.life===Infinity).length<50)
    particles.push(new P(-5,Math.random()*H));
  requestAnimationFrame(animBg);
}
animBg();

// Event triggers
function scanPulse(){
  particles.filter(p=>p.life===Infinity).forEach(p=>{
    p.vx*=2.5;setTimeout(()=>{p.vx/=2.5},400);p.a=Math.min(p.ma*2.5,.3);
  });
  for(let i=0;i<10;i++)
    particles.push(new P(-5,Math.random()*H,{vx:Math.random()*1.5+.8,a:.25,d:.003,life:150,s:Math.random()*2+.5}));
}

function burstTrade(side){
  const cx=W/2,cy=H/2;
  for(let i=0;i<30;i++){
    const a=Math.PI*2*i/30,sp=Math.random()*2+.8;
    particles.push(new P(cx,cy,{vx:Math.cos(a)*sp,vy:Math.sin(a)*sp,a:.5,d:.007,life:100,s:Math.random()*2.5+1,c:side==='yes'?'0,240,255':'255,160,0'}));
  }
}

function burstWin(pnl){
  const cx=W/2,cy=H/2;
  for(let i=0;i<60;i++){
    const a=Math.random()*Math.PI*2,sp=Math.random()*3+1;
    particles.push(new P(cx+(Math.random()-.5)*80,cy+(Math.random()-.5)*80,{
      vx:Math.cos(a)*sp,vy:Math.sin(a)*sp,a:.7,d:.004,life:180,s:Math.random()*3+1.5,
      c:Math.random()>.5?'255,215,0':'57,255,20'
    }));
  }
  showFloat('+$'+Math.abs(pnl).toFixed(2),'#39ff14');
}

function burstLoss(pnl){
  const cx=W/2,cy=H/2;
  for(let i=0;i<25;i++){
    const a=Math.random()*Math.PI*2,sp=Math.random()*2+.5;
    particles.push(new P(cx,cy,{vx:Math.cos(a)*sp,vy:Math.sin(a)*sp+.8,a:.4,d:.008,life:100,s:Math.random()*2+1,c:Math.random()>.5?'255,51,51':'255,106,0'}));
  }
  showFloat('-$'+Math.abs(pnl).toFixed(2),'#ff3333');
}

function showFloat(txt,col){
  const el=document.createElement('div');
  el.textContent=txt;
  el.style.cssText=`position:fixed;top:35%;left:50%;transform:translate(-50%,-50%);font-family:'JetBrains Mono',monospace;font-size:2.5rem;font-weight:700;color:${col};text-shadow:0 0 20px ${col}40;z-index:9998;pointer-events:none;animation:floatUp 2s ease-out forwards`;
  document.body.appendChild(el);setTimeout(()=>el.remove(),2500);
}

// === TAB SWITCHING ===
function switchTab(name,el){
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(t=>t.classList.remove('active'));
  el.classList.add('active');
  document.getElementById('tab-'+name).classList.add('active');
}

// === HELPERS ===
function fmt(ts){
  if(!ts)return'—';const d=new Date(ts),n=new Date(),s=(n-d)/1e3;
  if(s<60)return Math.floor(s)+'s ago';if(s<3600)return Math.floor(s/60)+'m ago';
  if(s<86400)return Math.floor(s/3600)+'h ago';return d.toLocaleDateString();
}
function fmtTime(ts){
  if(!ts)return'--:--:--';return new Date(ts).toLocaleTimeString('en-US',{hour12:false});
}

// === CHARTS ===
let eqChart=null;
function updateEquity(data){
  if(!data||!data.length)return;
  const labels=data.map(d=>fmtTime(d.timestamp));
  const vals=data.map(d=>d.balance);
  const ctx=document.getElementById('equityChart').getContext('2d');
  if(eqChart){
    eqChart.data.labels=labels;eqChart.data.datasets[0].data=vals;eqChart.update('none');
  }else{
    eqChart=new Chart(ctx,{type:'line',data:{labels,datasets:[{data:vals,borderColor:'#00f0ff',borderWidth:1.5,fill:false,tension:.3,pointRadius:0,pointHoverRadius:3,pointHoverBackgroundColor:'#00f0ff'}]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},scales:{x:{grid:{color:'rgba(0,240,255,0.03)'},ticks:{color:'rgba(0,240,255,0.2)',font:{size:9,family:'JetBrains Mono'},maxTicksLimit:6}},y:{grid:{color:'rgba(0,240,255,0.03)'},ticks:{color:'rgba(0,240,255,0.2)',font:{size:9,family:'JetBrains Mono'},callback:v=>'$'+v}}},interaction:{intersect:false,mode:'index'}}});
  }
}

// === DATA REFRESH ===
let prevTradeIds=new Set(),prevBal=null;

async function fetchJ(url){try{const r=await fetch(url);return r.ok?await r.json():null}catch(e){return null}}

async function refreshAll(){
  scanPulse();
  const[status,trades,strats,equity,signals,live,debates,improvements]=await Promise.all([
    fetchJ('/api/status'),fetchJ('/api/trades'),fetchJ('/api/strategies'),fetchJ('/api/equity'),fetchJ('/api/signals'),fetchJ('/api/live_status'),fetchJ('/api/debates'),fetchJ('/api/improvements')
  ]);

  // LIVE STATUS — header shows portfolio value (cash + positions)
  if(live&&live.enable_trading&&live.live_strategies&&live.live_strategies.length){
    document.getElementById('liveBadge').style.display='inline';
    document.getElementById('modeBadge').textContent='HYBRID';
    const hdrBal=status&&status.live&&status.live.balance;
    if(hdrBal!==null&&hdrBal!==undefined){
      document.getElementById('realBalLabel').style.display='inline';
      document.getElementById('realBal').style.display='inline';
      document.getElementById('realBal').textContent='$'+hdrBal.toFixed(2);
    }else if(live.real_balance!==null&&live.real_balance!==undefined){
      document.getElementById('realBalLabel').style.display='inline';
      document.getElementById('realBal').style.display='inline';
      document.getElementById('realBal').textContent='$'+live.real_balance.toFixed(2);
    }
  }

  // STATUS - Now separated into live/paper
  if(status){
    // PAPER TRADING
    const paper=status.paper||{};
    const pb=paper.balance||0,ppnl=paper.daily_pnl||0,proi=paper.roi_percent||0;
    document.getElementById('paper-balance').textContent='$'+pb.toFixed(2);
    document.getElementById('paper-pnl').textContent=(ppnl>=0?'+$':'-$')+Math.abs(ppnl).toFixed(2);
    document.getElementById('paper-pnl').className='metric-value '+(ppnl>=0?'profit':'loss');
    document.getElementById('paper-roi').textContent=(proi>=0?'+':'')+proi.toFixed(1)+'%';
    document.getElementById('paper-roi').className='metric-sub '+(proi>=0?'profit':'loss');
    document.getElementById('paper-trades').textContent=(paper.trades_today||0)+' trades today';
    document.getElementById('paper-positions').textContent=paper.positions||0;
    const pwr=(paper.wins+paper.losses)>0?((paper.wins/(paper.wins+paper.losses))*100).toFixed(0):'—';
    document.getElementById('paper-win-rate').textContent=pwr+'%';
    document.getElementById('paper-wl-record').textContent=(paper.wins||0)+'W / '+(paper.losses||0)+'L';

    // LIVE TRADING — portfolio value = cash + positions
    const live_s=status.live||{};
    const lb=live_s.balance||0;
    const lCash=live_s.cash||0;
    const lPos=live_s.positions_value||0;
    const lpnl=live_s.daily_pnl||0;
    const lRpnl=live_s.realized_pnl||0;
    document.getElementById('live-balance').textContent=lb!==null?'$'+lb.toFixed(2):'—';
    document.getElementById('live-exposure').textContent='Cash: $'+lCash.toFixed(2)+' | Positions: $'+lPos.toFixed(2);
    document.getElementById('live-pnl').textContent=(lRpnl>=0?'+$':'-$')+Math.abs(lRpnl).toFixed(2);
    document.getElementById('live-pnl').className='metric-value '+(lRpnl>=0?'profit':'loss');
    document.getElementById('live-trades').textContent='P&L: '+(lpnl>=0?'+$':'-$')+Math.abs(lpnl).toFixed(2)+' total';
    document.getElementById('live-positions').textContent=live_s.positions||0;
    const lwr=(live_s.wins+live_s.losses)>0?((live_s.wins/(live_s.wins+live_s.losses))*100).toFixed(0):'—';
    document.getElementById('live-win-rate').textContent=lwr+'%';
    document.getElementById('live-wl-record').textContent=(live_s.wins||0)+'W / '+(live_s.losses||0)+'L';

    // Header balance (paper)
    document.querySelectorAll('[data-field="balance"]').forEach(el=>{el.textContent='$'+pb.toFixed(2);el.style.color='#00f0ff'});
    document.querySelectorAll('[data-field="last_scan"]').forEach(el=>{el.textContent=fmt(status.last_check)});
    document.title='$'+pb.toFixed(0)+' | KALSHI ALPHA';

    if(prevBal!==null&&Math.abs(pb-prevBal)>.01){
      if(pb>prevBal)burstWin(pb-prevBal);else burstLoss(prevBal-pb);
    }
    prevBal=pb;
  }

  // TRADES
  if(trades&&trades.length){
    const tbody=document.getElementById('tradesBody');
    // Detect new trades
    trades.forEach(t=>{
      const id=t.id||t.timestamp;
      if(id&&!prevTradeIds.has(id)){prevTradeIds.add(id);if(prevTradeIds.size>1)burstTrade(t.side)}
    });
    // Win rate
    const settled=trades.filter(t=>(t.reason||'').toUpperCase().match(/WIN|LOSS/));
    const wins=settled.filter(t=>(t.reason||'').toUpperCase().includes('WIN')).length;
    const losses=settled.filter(t=>(t.reason||'').toUpperCase().includes('LOSS')).length;
    const wr=(wins+losses)>0?((wins/(wins+losses))*100).toFixed(0):'—';
    document.querySelectorAll('[data-field="win_rate"]').forEach(el=>{el.textContent=wr+'%'});
    document.querySelectorAll('[data-field="wl_record"]').forEach(el=>{el.textContent=wins+'W / '+losses+'L'});

    tbody.innerHTML=trades.slice(0,15).map(t=>{
      const s=(t.strategy||'?').replace(/_/g,' ');
      const side=(t.side||'').toUpperCase();
      const reason=(t.reason||'').toUpperCase();
      const isW=reason.includes('WIN'),isL=reason.includes('LOSS');
      const isLive=t.is_live||reason.includes('[LIVE]');
      const liveBorder=isLive?'border-left:3px solid #ff3c3c;':'';
      const liveTag=isLive?'<span style="color:#ff3c3c;font-size:.6rem;margin-left:4px">LIVE</span>':'';
      return`<tr style="${liveBorder}"><td style="color:rgba(255,255,255,0.2)">${fmt(t.timestamp||t.created_at)}</td><td style="color:${isLive?'#ff3c3c':'#00f0ff'}">${s}${liveTag}</td><td>${(t.ticker||'').substring(0,28)}</td><td style="color:${side==='YES'?'#39ff14':'#ff6d00'}">${side}</td><td>$${(t.price||0).toFixed(2)}</td><td style="color:${isW?'#39ff14':isL?'#ff3333':'rgba(255,255,255,0.2)'}">${isW?'WIN':isL?'LOSS':'OPEN'}</td></tr>`;
    }).join('');
  }

  // STRATEGIES
  if(strats&&strats.length){
    const el=document.getElementById('stratLayers');
    const liveStrats=(live&&live.live_strategies)||[];
    el.innerHTML=strats.map(s=>{
      const raw=s.strategy||'?';
      const name=raw.replace(/_/g,' ');
      const isLiveStrat=liveStrats.some(ls=>raw.toLowerCase().includes(ls.toLowerCase()));
      const badge=isLiveStrat?'<span style="color:#ff3c3c;font-size:.55rem;margin-left:4px;border:1px solid rgba(255,60,60,0.4);padding:0 3px;border-radius:1px">LIVE</span>':'';
      const liveCount=s.live_trades||0;
      const paperCount=s.paper_trades||0;
      const totalCount=liveCount+paperCount;
      const countText=liveCount>0&&paperCount>0?`${liveCount}L / ${paperCount}P`:`${totalCount} trades`;
      return`<div class="strat-row"><span class="strat-name">${name}${badge}</span><span class="strat-count">${countText}</span></div>`;
    }).join('');
  }

  // EQUITY
  if(equity)updateEquity(equity);

  // SIGNALS → Training Log
  if(signals&&signals.length){
    const log=document.getElementById('trainingLog');
    let html='<div class="panel-title" style="padding:12px 12px 8px">Training Log</div>';
    signals.slice(0,30).forEach(s=>{
      const time=fmtTime(s.timestamp);
      const edge=((s.edge||0)*100).toFixed(1);
      const conf=(s.confidence||0).toFixed(0);
      if(s.action==='TRADE'||s.action==='VIRTUAL_TRADE'){
        html+=`<div class="log-entry"><span class="log-time">${time}</span> <span class="log-tag log-tag-bwd">FORWARD</span> ${s.strategy||'?'} → ${s.ticker||'?'} edge=${edge}%</div>`;
      }else{
        html+=`<div class="log-entry"><span class="log-time">${time}</span> <span class="log-tag log-tag-skip">CLIP</span> ${s.ticker||'?'} ${s.skip_reason||'filtered'}</div>`;
      }
    });
    log.innerHTML=html;
  }

  // SIGNALS TAB
  if(signals&&signals.length){
    const el=document.getElementById('signalFeed');
    el.innerHTML=signals.slice(0,40).map(s=>{
      const act=(s.action||'').toUpperCase();
      const isSkip=act==='SKIP';
      const isTrade=act==='TRADE'||act==='VIRTUAL_TRADE';
      const edge=((s.edge||0)*100).toFixed(1);
      const conf=(s.confidence||0).toFixed(0);
      const price=s.yes_price?(s.side==='yes'?s.yes_price:s.no_price||0):0;
      const col=isTrade?'#39ff14':isSkip?'#ff3333':'rgba(255,255,255,0.3)';
      return`<div style="padding:5px 0;border-bottom:1px solid rgba(255,255,255,0.02);${isSkip?'opacity:.4':''}">
        <div><span style="color:rgba(255,255,255,0.15)">${fmtTime(s.timestamp)}</span> <span style="color:#00f0ff;font-weight:600">[${s.strategy||'?'}]</span> ${s.ticker||''}</div>
        <div style="font-size:.65rem;color:rgba(255,255,255,0.3)">Side: ${(s.side||'?').toUpperCase()} ${price?'| Price: $'+price.toFixed(2):''} | Edge: ${edge}% | Conf: ${conf}% → <span style="color:${col}">${act||'?'}</span>${s.skip_reason?' <span style="color:rgba(255,255,255,0.2)">('+s.skip_reason+')</span>':''}</div>
      </div>`;
    }).join('');
  }

  // DEBATES TAB
  if(debates&&debates.length){
    const el=document.getElementById('debatesFeed');
    el.innerHTML=debates.map(d=>{
      const agree=d.agreement;
      return`<div style="padding:8px 0;border-bottom:1px solid rgba(255,255,255,0.03)">
        <div><span style="color:rgba(255,255,255,0.15)">${fmt(d.timestamp)}</span> <span style="color:#00f0ff;font-weight:600">${d.ticker||'?'}</span></div>
        <div style="font-size:.65rem;margin-top:2px">
          <span style="color:#ff6d00">Grok: ${d.grok_probability!==null?((d.grok_probability*100).toFixed(0)+'%'):'—'}</span> → ${d.grok_recommendation||'—'}
          ${d.claude_probability!==null?' | <span style="color:#a855f7">Claude: '+(d.claude_probability*100).toFixed(0)+'%</span> → '+(d.claude_recommendation||'—'):''}
          ${d.gemini_probability!==null?' | <span style="color:#22d3ee">Gemini: '+(d.gemini_probability*100).toFixed(0)+'%</span>':''}
        </div>
        <div style="font-size:.65rem;margin-top:2px">Decision: <span style="color:${d.final_decision==='SKIP'?'#ff3333':'#39ff14'}">${d.final_decision||'?'}</span> | Agreement: ${agree?'✅':'❌'} ${d.votes?'| '+d.votes:''}</div>
      </div>`;
    }).join('');
  }

  // LEARNING LAB TAB
  if(improvements&&improvements.length){
    const el=document.getElementById('learnFeed');
    el.innerHTML=improvements.map(imp=>{
      const verdicts=imp.strategy_verdicts?JSON.parse(imp.strategy_verdicts):{};
      const params=imp.new_parameters?JSON.parse(imp.new_parameters):{};
      return`<div style="padding:8px 0;border-bottom:1px solid rgba(255,255,255,0.03)">
        <div><span style="color:rgba(255,255,255,0.15)">${fmt(imp.timestamp)}</span> <span style="color:#00f0ff;font-weight:600">Self-Improvement Run</span></div>
        <div style="font-size:.65rem;margin-top:4px;color:rgba(255,255,255,0.4)">
          ${Object.entries(verdicts).map(([s,v])=>'<span style="color:'+(v==='PROFITABLE'?'#39ff14':v==='UNPROFITABLE'?'#ff3333':'#ffd700')+'">'+s+': '+v+'</span>').join(' | ')}
        </div>
        <div style="font-size:.65rem;margin-top:2px;color:rgba(255,255,255,0.3)">
          Debate mode: ${params.debate_mode||'—'} | Min volume: ${params.min_volume_filter||'—'}
        </div>
      </div>`;
    }).join('');
  }else{
    const el=document.getElementById('learnFeed');
    const nextRun=new Date();nextRun.setHours(nextRun.getHours()+(6-nextRun.getHours()%6),0,0,0);
    el.innerHTML='<div style="color:rgba(255,255,255,0.15)">Self-improvement analysis runs every 6 hours. Next run: '+nextRun.toLocaleTimeString()+'</div>';
  }
}

// Initial + auto-refresh
refreshAll();
setInterval(refreshAll,12000);
</script>
</body>
</html>"""


@app.route('/dashboard')
def dashboard():
    return DASHBOARD_HTML


# ============================================================
#  Start dashboard in background thread
# ============================================================

def start_dashboard():
    port = int(os.environ.get('PORT', 8080))
    t = threading.Thread(
        target=lambda: app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False),
        daemon=True
    )
    t.start()
    print(f"Dashboard starting on port {port}")