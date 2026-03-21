"""Flask dashboard for monitoring the Kalshi paper trading bot."""

import os
import threading
from flask import Flask, jsonify
from utils.supabase_db import SupabaseDB
from utils.logger import setup_logger

logger = setup_logger('dashboard')

app = Flask(__name__)
db = SupabaseDB()


@app.route('/')
def health():
    return jsonify({'status': 'ok'})


@app.route('/dashboard')
def dashboard():
    return DASHBOARD_HTML


@app.route('/api/status')
def api_status():
    status_rows, trades, positions = [], [], []

    if db.client:
        try:
            result = db.client.table('kalshi_bot_status').select('*').order('last_check', desc=True).limit(1).execute()
            status_rows = result.data or []
        except Exception as e:
            logger.error(f"Status fetch failed: {e}")

        try:
            result = db.client.table('kalshi_trades').select('*').order('timestamp', desc=True).limit(200).execute()
            trades = result.data or []
        except Exception as e:
            logger.error(f"Trades fetch failed: {e}")

    latest = status_rows[0] if status_rows else {}

    # Separate entries from settlements
    entries = [t for t in trades if t.get('action') != 'settle']
    settlements = [t for t in trades if t.get('action') == 'settle']
    wins = sum(1 for t in settlements if t.get('confidence', 0) == 100)  # We log conf=100 for wins
    total_settled = len(settlements)
    win_rate = (wins / total_settled * 100) if total_settled > 0 else 0

    # Calculate realized P&L from settlements
    realized_pnl = 0.0
    for t in settlements:
        if t.get('confidence', 0) == 100:  # WIN
            realized_pnl += (t.get('count') or 1) * (1.0 - (t.get('price') or 0))
        else:  # LOSS
            realized_pnl -= (t.get('count') or 1) * (t.get('price') or 0)

    # Per-strategy breakdown
    strat_stats = {}
    for t in entries:
        s = t.get('strategy', 'unknown')
        if s not in strat_stats:
            strat_stats[s] = {'trades': 0, 'total_conf': 0}
        strat_stats[s]['trades'] += 1
        strat_stats[s]['total_conf'] += t.get('confidence') or 0

    strategy_breakdown = []
    for s, st in sorted(strat_stats.items(), key=lambda x: -x[1]['trades']):
        avg_conf = st['total_conf'] / st['trades'] if st['trades'] > 0 else 0
        strategy_breakdown.append({'name': s, 'trades': st['trades'], 'avg_confidence': round(avg_conf, 1)})

    return jsonify({
        'bot': {
            'is_running': latest.get('is_running', False),
            'last_check': latest.get('last_check', 'never'),
            'balance': latest.get('balance', 0),
            'daily_pnl': latest.get('daily_pnl', 0),
            'trades_today': latest.get('trades_today', 0),
            'active_positions': latest.get('active_positions', 0),
        },
        'stats': {
            'total_trades': len(entries),
            'settled': total_settled,
            'wins': wins,
            'win_rate': round(win_rate, 1),
            'realized_pnl': round(realized_pnl, 2),
        },
        'strategies': strategy_breakdown,
        'trades': trades[:50],
    })


def start_dashboard():
    port = int(os.environ.get('PORT', 5000))
    thread = threading.Thread(
        target=lambda: app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False),
        daemon=True,
    )
    thread.start()
    logger.info(f"Dashboard on 0.0.0.0:{port}")
    return thread


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Kalshi Paper Trading Bot</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0d1117; color: #c9d1d9; padding: 20px; }
  h1 { color: #58a6ff; margin-bottom: 20px; }
  h2 { color: #58a6ff; margin: 20px 0 12px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 14px; margin-bottom: 20px; }
  .card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 14px; }
  .card .label { color: #8b949e; font-size: 11px; text-transform: uppercase; }
  .card .value { font-size: 26px; font-weight: 600; margin-top: 4px; }
  .green { color: #3fb950; } .red { color: #f85149; } .blue { color: #58a6ff; } .yellow { color: #d29922; }
  table { width: 100%; border-collapse: collapse; background: #161b22; border: 1px solid #30363d; border-radius: 8px; overflow: hidden; margin-bottom: 20px; }
  th { background: #21262d; color: #8b949e; font-size: 11px; text-transform: uppercase; padding: 8px 10px; text-align: left; }
  td { padding: 7px 10px; border-top: 1px solid #30363d; font-size: 13px; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 11px; font-weight: 600; }
  .badge-green { background: #1b4332; color: #3fb950; }
  .badge-blue { background: #1a3a5c; color: #58a6ff; }
  .badge-yellow { background: #3d2e00; color: #d29922; }
  .loading { color: #8b949e; padding: 30px; text-align: center; }
  #dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 6px; }
</style>
</head>
<body>
<h1>Kalshi Paper Trading Bot</h1>
<div id="app"><div class="loading">Loading...</div></div>
<script>
async function load() {
  try {
    const r = await fetch('/api/status');
    const d = await r.json();
    const b = d.bot, s = d.stats;
    const pnlC = b.daily_pnl >= 0 ? 'green' : 'red';
    let html = `
      <div class="grid">
        <div class="card"><div class="label">Status</div><div class="value"><span id="dot" style="background:${b.is_running?'#3fb950':'#f85149'}"></span>${b.is_running?'Running':'Stopped'}</div></div>
        <div class="card"><div class="label">Paper Balance</div><div class="value blue">$${(b.balance||0).toFixed(2)}</div></div>
        <div class="card"><div class="label">Daily P&L</div><div class="value ${pnlC}">${b.daily_pnl>=0?'+':''}$${(b.daily_pnl||0).toFixed(2)}</div></div>
        <div class="card"><div class="label">Trades Today</div><div class="value">${b.trades_today||0}</div></div>
        <div class="card"><div class="label">Total Trades</div><div class="value">${s.total_trades}</div></div>
        <div class="card"><div class="label">Settled</div><div class="value">${s.settled||0} (${s.wins||0}W)</div></div>
        <div class="card"><div class="label">Win Rate</div><div class="value ${s.win_rate>=50?'green':'yellow'}">${s.win_rate}%</div></div>
        <div class="card"><div class="label">Realized P&L</div><div class="value ${(s.realized_pnl||0)>=0?'green':'red'}">$${(s.realized_pnl||0).toFixed(2)}</div></div>
        <div class="card"><div class="label">Open Positions</div><div class="value">${b.active_positions||0}</div></div>
        <div class="card"><div class="label">Last Check</div><div class="value" style="font-size:13px">${b.last_check==='never'?'Never':new Date(b.last_check).toLocaleString()}</div></div>
      </div>`;

    // Strategy breakdown
    if (d.strategies && d.strategies.length > 0) {
      html += '<h2>Strategy Breakdown</h2><table><tr><th>Strategy</th><th>Trades</th><th>Avg Confidence</th></tr>';
      for (const st of d.strategies) {
        html += '<tr><td><strong>'+st.name+'</strong></td><td>'+st.trades+'</td><td>'+st.avg_confidence+'</td></tr>';
      }
      html += '</table>';
    }

    // Recent trades
    html += '<h2>Recent Paper Trades</h2>';
    if (d.trades.length === 0) {
      html += '<div class="loading">No trades yet</div>';
    } else {
      html += '<table><tr><th>Time</th><th>Ticker</th><th>Side</th><th>Qty</th><th>Price</th><th>Strategy</th><th>Conf</th><th>Reason</th></tr>';
      for (const t of d.trades.slice(0,30)) {
        const tm = t.timestamp ? new Date(t.timestamp).toLocaleString() : '-';
        const cv = t.confidence||0;
        const cc = cv>=70?'badge-green':cv>=40?'badge-blue':'badge-yellow';
        html += '<tr><td>'+tm+'</td><td><strong>'+(t.ticker||'-')+'</strong></td>'
          +'<td>'+(t.side||'-').toUpperCase()+'</td><td>'+(t.count||'-')+'</td>'
          +'<td>$'+((t.price||0).toFixed?t.price.toFixed(2):'0')+'</td>'
          +'<td>'+(t.strategy||'-')+'</td>'
          +'<td><span class="badge '+cc+'">'+Math.round(cv)+'</span></td>'
          +'<td style="max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'+(t.reason||'-')+'</td></tr>';
      }
      html += '</table>';
    }
    document.getElementById('app').innerHTML = html;
  } catch(e) {
    document.getElementById('app').innerHTML = '<div class="loading">Error: '+e.message+'</div>';
  }
}
load();
setInterval(load, 30000);
</script>
</body>
</html>"""
