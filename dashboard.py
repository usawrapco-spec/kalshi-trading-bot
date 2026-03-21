"""Flask dashboard for monitoring the Kalshi trading bot."""

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
    """Return full bot status as JSON."""
    # Latest bot status
    status_rows = []
    trades = []
    positions = []

    if db.client:
        try:
            result = db.client.table('kalshi_bot_status').select('*').order(
                'last_check', desc=True
            ).limit(1).execute()
            status_rows = result.data or []
        except Exception as e:
            logger.error(f"Failed to fetch status: {e}")

        try:
            result = db.client.table('kalshi_trades').select('*').order(
                'timestamp', desc=True
            ).limit(200).execute()
            trades = result.data or []
        except Exception as e:
            logger.error(f"Failed to fetch trades: {e}")

        try:
            result = db.client.table('kalshi_positions').select('*').execute()
            positions = result.data or []
        except Exception as e:
            logger.error(f"Failed to fetch positions: {e}")

    latest_status = status_rows[0] if status_rows else {}

    # Calculate stats
    total_trades = len(trades)
    wins = sum(1 for t in trades if (t.get('price') or 0) > 0)
    win_rate = (wins / total_trades * 100) if total_trades > 0 else 0
    total_return = sum((t.get('price') or 0) * (t.get('count') or 0) for t in trades) / 100

    return jsonify({
        'bot': {
            'is_running': latest_status.get('is_running', False),
            'last_check': latest_status.get('last_check', 'never'),
            'balance': latest_status.get('balance', 0),
            'daily_pnl': latest_status.get('daily_pnl', 0),
            'trades_today': latest_status.get('trades_today', 0),
            'active_positions': latest_status.get('active_positions', 0),
        },
        'stats': {
            'total_trades': total_trades,
            'win_rate': round(win_rate, 1),
            'total_return': round(total_return, 2),
        },
        'trades': trades[:50],
        'positions': [p for p in positions if p.get('position')],
    })


def start_dashboard():
    """Start the Flask dashboard on a separate thread."""
    port = int(os.environ.get('PORT', 5000))
    thread = threading.Thread(
        target=lambda: app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False),
        daemon=True,
    )
    thread.start()
    logger.info(f"Dashboard running on port {port}")
    return thread


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Kalshi Trading Bot</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0d1117; color: #c9d1d9; padding: 20px; }
  h1 { color: #58a6ff; margin-bottom: 20px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin-bottom: 24px; }
  .card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px; }
  .card .label { color: #8b949e; font-size: 12px; text-transform: uppercase; }
  .card .value { font-size: 28px; font-weight: 600; margin-top: 4px; }
  .green { color: #3fb950; } .red { color: #f85149; } .blue { color: #58a6ff; }
  table { width: 100%; border-collapse: collapse; background: #161b22; border: 1px solid #30363d; border-radius: 8px; overflow: hidden; }
  th { background: #21262d; color: #8b949e; font-size: 12px; text-transform: uppercase; padding: 10px 12px; text-align: left; }
  td { padding: 8px 12px; border-top: 1px solid #30363d; font-size: 14px; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 11px; font-weight: 600; }
  .badge-green { background: #1b4332; color: #3fb950; }
  .badge-red { background: #3d1f1f; color: #f85149; }
  .badge-blue { background: #1a3a5c; color: #58a6ff; }
  #status-dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 6px; }
  .loading { color: #8b949e; padding: 40px; text-align: center; }
</style>
</head>
<body>
<h1>Kalshi Trading Bot</h1>
<div id="app"><div class="loading">Loading...</div></div>
<script>
async function load() {
  try {
    const r = await fetch('/api/status');
    const d = await r.json();
    const b = d.bot, s = d.stats;
    const running = b.is_running;
    const pnlClass = b.daily_pnl >= 0 ? 'green' : 'red';
    const pnlSign = b.daily_pnl >= 0 ? '+' : '';
    let html = `
      <div class="grid">
        <div class="card"><div class="label">Status</div><div class="value"><span id="status-dot" style="background:${running ? '#3fb950' : '#f85149'}"></span>${running ? 'Running' : 'Stopped'}</div></div>
        <div class="card"><div class="label">Balance</div><div class="value blue">$${b.balance.toFixed(2)}</div></div>
        <div class="card"><div class="label">Daily P&amp;L</div><div class="value ${pnlClass}">${pnlSign}$${b.daily_pnl.toFixed(2)}</div></div>
        <div class="card"><div class="label">Trades Today</div><div class="value">${b.trades_today}</div></div>
        <div class="card"><div class="label">Open Positions</div><div class="value">${b.active_positions}</div></div>
        <div class="card"><div class="label">Win Rate</div><div class="value ${s.win_rate >= 50 ? 'green' : 'red'}">${s.win_rate}%</div></div>
        <div class="card"><div class="label">Total Return</div><div class="value ${s.total_return >= 0 ? 'green' : 'red'}">$${s.total_return.toFixed(2)}</div></div>
        <div class="card"><div class="label">Last Check</div><div class="value" style="font-size:14px">${b.last_check === 'never' ? 'Never' : new Date(b.last_check).toLocaleString()}</div></div>
      </div>
      <h2 style="color:#58a6ff;margin-bottom:12px">Recent Trades</h2>`;
    if (d.trades.length === 0) {
      html += '<div class="loading">No trades yet</div>';
    } else {
      html += '<table><tr><th>Time</th><th>Ticker</th><th>Action</th><th>Side</th><th>Qty</th><th>Strategy</th><th>Confidence</th><th>Reason</th></tr>';
      for (const t of d.trades.slice(0, 30)) {
        const time = t.timestamp ? new Date(t.timestamp).toLocaleString() : '-';
        const confVal = t.confidence || 0;
        const confClass = confVal >= 80 ? 'badge-green' : confVal >= 70 ? 'badge-blue' : 'badge-red';
        html += '<tr>' +
          '<td>' + time + '</td>' +
          '<td><strong>' + (t.ticker || '-') + '</strong></td>' +
          '<td>' + (t.action || '-').toUpperCase() + '</td>' +
          '<td>' + (t.side || '-').toUpperCase() + '</td>' +
          '<td>' + (t.count || '-') + '</td>' +
          '<td>' + (t.strategy || '-') + '</td>' +
          '<td><span class="badge ' + confClass + '">' + Math.round(confVal) + '</span></td>' +
          '<td style="max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + (t.reason || '-') + '</td>' +
          '</tr>';
      }
      html += '</table>';
    }
    document.getElementById('app').innerHTML = html;
  } catch(e) {
    document.getElementById('app').innerHTML = '<div class="loading">Error loading data: ' + e.message + '</div>';
  }
}
load();
setInterval(load, 30000);
</script>
</body>
</html>"""
