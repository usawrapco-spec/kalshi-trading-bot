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
    """Bot status, uptime, current cycle, last run time."""
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


@app.route('/api/balance')
def api_balance():
    """Paper balance, starting balance, total P&L, daily P&L, ROI %."""
    if not db.client:
        return jsonify({'error': 'Database not available'})

    try:
        # Get latest status
        result = db.client.table('kalshi_bot_status').select('*').order('last_check', desc=True).limit(1).execute()
        latest = result.data[0] if result.data else {}

        # Get all trades for total P&L calculation
        result = db.client.table('kalshi_trades').select('*').execute()
        trades = result.data or []

        # Calculate metrics
        starting_balance = 100.0  # Paper trading start
        current_balance = latest.get('balance', starting_balance)
        daily_pnl = latest.get('daily_pnl', 0)

        # Calculate total P&L from all settlements
        settlements = [t for t in trades if t.get('action') == 'settle']
        total_realized_pnl = 0.0
        for t in settlements:
            if t.get('confidence', 0) == 100:  # WIN
                total_realized_pnl += (t.get('count') or 1) * (1.0 - (t.get('price') or 0))
            else:  # LOSS
                total_realized_pnl -= (t.get('count') or 1) * (t.get('price') or 0)

        roi_pct = (total_realized_pnl / starting_balance) * 100 if starting_balance > 0 else 0

        return jsonify({
            'starting_balance': starting_balance,
            'current_balance': round(current_balance, 2),
            'daily_pnl': round(daily_pnl, 2),
            'total_realized_pnl': round(total_realized_pnl, 2),
            'roi_percentage': round(roi_pct, 2),
            'total_trades': len([t for t in trades if t.get('action') == 'buy']),
            'open_positions': latest.get('active_positions', 0),
        })
    except Exception as e:
        logger.error(f"Balance API error: {e}")
        return jsonify({'error': str(e)})


@app.route('/api/trades')
def api_trades():
    """All paper trades with filters: strategy, date range, resolved/open."""
    if not db.client:
        return jsonify({'error': 'Database not available'})

    try:
        result = db.client.table('kalshi_trades').select('*').order('timestamp', desc=True).limit(500).execute()
        trades = result.data or []

        # Add computed fields
        for trade in trades:
            trade['pnl'] = None
            if trade.get('action') == 'settle':
                if trade.get('confidence', 0) == 100:  # WIN
                    trade['pnl'] = (trade.get('count') or 1) * (1.0 - (trade.get('price') or 0))
                else:  # LOSS
                    trade['pnl'] = -(trade.get('count') or 1) * (trade.get('price') or 0)

        return jsonify({'trades': trades})
    except Exception as e:
        logger.error(f"Trades API error: {e}")
        return jsonify({'error': str(e)})


@app.route('/api/trades/recent')
def api_trades_recent():
    """Last 20 trades."""
    if not db.client:
        return jsonify({'error': 'Database not available'})

    try:
        result = db.client.table('kalshi_trades').select('*').order('timestamp', desc=True).limit(20).execute()
        trades = result.data or []
        return jsonify({'trades': trades})
    except Exception as e:
        logger.error(f"Recent trades API error: {e}")
        return jsonify({'error': str(e)})


@app.route('/api/positions')
def api_positions():
    """All open positions with current market prices and unrealized P&L."""
    # For now, return mock data since we don't have real-time market prices
    # In production, this would fetch current prices from Kalshi API
    return jsonify({
        'positions': [],
        'note': 'Real-time position tracking requires market price integration'
    })


@app.route('/api/strategies')
def api_strategies():
    """Per-strategy stats: trades count, win rate, total P&L, avg edge, avg confidence."""
    if not db.client:
        return jsonify({'error': 'Database not available'})

    try:
        result = db.client.table('kalshi_trades').select('*').execute()
        trades = result.data or []

        # Group by strategy
        strategy_stats = {}
        for trade in trades:
            strategy = trade.get('strategy', 'unknown')
            if strategy not in strategy_stats:
                strategy_stats[strategy] = {
                    'trades': 0,
                    'wins': 0,
                    'total_pnl': 0.0,
                    'total_confidence': 0,
                    'settlements': 0
                }

            stats = strategy_stats[strategy]
            stats['trades'] += 1
            stats['total_confidence'] += trade.get('confidence', 0)

            if trade.get('action') == 'settle':
                stats['settlements'] += 1
                if trade.get('confidence', 0) == 100:  # WIN
                    stats['wins'] += 1
                    pnl = (trade.get('count') or 1) * (1.0 - (trade.get('price') or 0))
                    stats['total_pnl'] += pnl
                else:  # LOSS
                    pnl = -(trade.get('count') or 1) * (trade.get('price') or 0)
                    stats['total_pnl'] += pnl

        # Calculate derived metrics
        strategies = []
        for name, stats in strategy_stats.items():
            win_rate = (stats['wins'] / stats['settlements'] * 100) if stats['settlements'] > 0 else 0
            avg_confidence = stats['total_confidence'] / stats['trades'] if stats['trades'] > 0 else 0

            strategies.append({
                'name': name,
                'trades': stats['trades'],
                'settlements': stats['settlements'],
                'wins': stats['wins'],
                'win_rate': round(win_rate, 1),
                'total_pnl': round(stats['total_pnl'], 2),
                'avg_confidence': round(avg_confidence, 1),
                'avg_edge': 0.0,  # Would need signal log data
            })

        return jsonify({'strategies': strategies})
    except Exception as e:
        logger.error(f"Strategies API error: {e}")
        return jsonify({'error': str(e)})


@app.route('/api/equity-curve')
def api_equity_curve():
    """Time series of balance over time for charting."""
    if not db.client:
        return jsonify({'error': 'Database not available'})

    try:
        # Get equity snapshots (we'll add this table)
        result = db.client.table('equity_snapshots').select('*').order('timestamp', asc=True).limit(1000).execute()
        snapshots = result.data or []

        # If no snapshots, create mock data for demo
        if not snapshots:
            snapshots = [
                {'timestamp': '2024-01-01T00:00:00Z', 'balance': 100.0},
                {'timestamp': '2024-01-02T00:00:00Z', 'balance': 102.5},
                {'timestamp': '2024-01-03T00:00:00Z', 'balance': 98.7},
                {'timestamp': '2024-01-04T00:00:00Z', 'balance': 105.2},
            ]

        return jsonify({
            'snapshots': snapshots,
            'note': 'Equity curve data for Chart.js visualization'
        })
    except Exception as e:
        logger.error(f"Equity curve API error: {e}")
        return jsonify({'error': str(e)})


@app.route('/api/signals')
def api_signals():
    """Recent signals INCLUDING skipped ones."""
    if not db.client:
        return jsonify({'error': 'Database not available'})

    try:
        # Get signal log (we'll add this table)
        result = db.client.table('signal_log').select('*').order('timestamp', desc=True).limit(100).execute()
        signals = result.data or []

        return jsonify({
            'signals': signals,
            'note': 'Shows all signals including skipped ones for transparency'
        })
    except Exception as e:
        logger.error(f"Signals API error: {e}")
        return jsonify({'error': str(e)})


@app.route('/api/debates')
def api_debates():
    """Grok vs Claude debate history."""
    if not db.client:
        return jsonify({'error': 'Database not available'})

    try:
        # Get debate log (we'll add this table)
        result = db.client.table('debate_log').select('*').order('timestamp', desc=True).limit(50).execute()
        debates = result.data or []

        return jsonify({
            'debates': debates,
            'note': 'Grok vs Claude decision history'
        })
    except Exception as e:
        logger.error(f"Debates API error: {e}")
        return jsonify({'error': str(e)})


@app.route('/api/risk')
def api_risk():
    """Current risk metrics: Kelly fraction, cash reserve %, exposure by strategy."""
    if not db.client:
        return jsonify({'error': 'Database not available'})

    try:
        # Get latest status
        result = db.client.table('kalshi_bot_status').select('*').order('last_check', desc=True).limit(1).execute()
        latest = result.data[0] if result.data else {}

        # Mock risk metrics for now
        return jsonify({
            'kelly_fraction': 0.1,
            'cash_reserve_percentage': 30.0,
            'daily_loss_limit': -30.0,
            'current_daily_loss': latest.get('daily_pnl', 0),
            'max_trade_size_percentage': 2.0,
            'portfolio_concentration': 0.0,  # Max position as % of balance
            'active_positions': latest.get('active_positions', 0),
            'circuit_breaker_active': False,
        })
    except Exception as e:
        logger.error(f"Risk API error: {e}")
        return jsonify({'error': str(e)})


@app.route('/api/settlements')
def api_settlements():
    """Recently settled markets and our P&L on each."""
    if not db.client:
        return jsonify({'error': 'Database not available'})

    try:
        result = db.client.table('kalshi_trades').select('*').eq('action', 'settle').order('timestamp', desc=True).limit(50).execute()
        settlements = result.data or []

        # Add P&L calculations
        for settlement in settlements:
            if settlement.get('confidence', 0) == 100:  # WIN
                settlement['pnl'] = (settlement.get('count') or 1) * (1.0 - (settlement.get('price') or 0))
                settlement['outcome'] = 'WIN'
            else:  # LOSS
                settlement['pnl'] = -(settlement.get('count') or 1) * (settlement.get('price') or 0)
                settlement['outcome'] = 'LOSS'

        return jsonify({'settlements': settlements})
    except Exception as e:
        logger.error(f"Settlements API error: {e}")
        return jsonify({'error': str(e)})


@app.route('/api/signal-evaluations')
def api_signal_evaluations():
    """Signal evaluations from data collection mode."""
    if not db.client:
        return jsonify({'error': 'Database not available'})

    try:
        # Get recent signal evaluations
        result = db.client.table('signal_evaluations').select('*').order('timestamp', desc=True).limit(100).execute()
        signals = result.data or []

        # Get summary stats
        total_signals = len(signals)
        virtual_trades = sum(1 for s in signals if s.get('action') == 'VIRTUAL_TRADE')
        settled_trades = sum(1 for s in signals if s.get('settled'))
        win_rate = 0
        if settled_trades > 0:
            wins = sum(1 for s in signals if s.get('was_correct'))
            win_rate = wins / settled_trades

        return jsonify({
            'signals': signals,
            'summary': {
                'total_signals': total_signals,
                'virtual_trades': virtual_trades,
                'settled_trades': settled_trades,
                'win_rate': round(win_rate, 3),
                'pending_settlement': virtual_trades - settled_trades
            }
        })
    except Exception as e:
        logger.error(f"Signal evaluations API error: {e}")
        return jsonify({'error': str(e)})


@app.route('/api/improvement-logs')
def api_improvement_logs():
    """Self-improvement analysis logs."""
    if not db.client:
        return jsonify({'error': 'Database not available'})

    try:
        result = db.client.table('improvement_logs').select('*').order('timestamp', desc=True).limit(10).execute()
        logs = result.data or []

        return jsonify({'logs': logs})
    except Exception as e:
        logger.error(f"Improvement logs API error: {e}")
        return jsonify({'error': str(e)})


@app.route('/api/active-parameters')
def api_active_parameters():
    """Currently active bot parameters."""
    if not db.client:
        return jsonify({'error': 'Database not available'})

    try:
        result = db.client.table('active_parameters').select('*').eq('id', 'current').execute()
        params = result.data[0] if result.data else {}

        return jsonify({'parameters': params.get('parameters', {})})
    except Exception as e:
        logger.error(f"Active parameters API error: {e}")
        return jsonify({'error': str(e)})


@app.route('/api/learning-lab')
def api_learning_lab():
    """Comprehensive learning lab data for dashboard."""
    if not db.client:
        return jsonify({'error': 'Database not available'})

    try:
        # Get latest improvement log
        improvement_result = db.client.table('improvement_logs').select('*').order('timestamp', desc=True).limit(1).execute()
        latest_analysis = improvement_result.data[0] if improvement_result.data else {}

        # Get signal evaluation stats
        signals_result = db.client.table('signal_evaluations').select('settled, was_correct, virtual_pnl, strategy').execute()
        signals = signals_result.data or []

        # Calculate signal stats
        total_signals = len(signals)
        settled_signals = sum(1 for s in signals if s.get('settled'))
        correct_predictions = sum(1 for s in signals if s.get('was_correct'))
        total_pnl = sum(s.get('virtual_pnl', 0) for s in signals if s.get('settled'))

        # Strategy performance from latest analysis
        strategy_verdicts = latest_analysis.get('strategy_verdicts', {})
        if isinstance(strategy_verdicts, str):
            import json
            try:
                strategy_verdicts = json.loads(strategy_verdicts)
            except:
                strategy_verdicts = {}

        # Active parameters
        params_result = db.client.table('active_parameters').select('parameters').eq('id', 'current').execute()
        active_params = params_result.data[0].get('parameters', {}) if params_result.data else {}

        return jsonify({
            'latest_analysis': latest_analysis,
            'signal_stats': {
                'total_signals': total_signals,
                'settled_signals': settled_signals,
                'correct_predictions': correct_predictions,
                'prediction_accuracy': correct_predictions / max(settled_signals, 1),
                'total_virtual_pnl': total_pnl
            },
            'strategy_verdicts': strategy_verdicts,
            'active_parameters': active_params,
            'next_analysis_eta': 'Auto-runs every 6 hours'
        })
    except Exception as e:
        logger.error(f"Learning lab API error: {e}")
        return jsonify({'error': str(e)})


@app.route('/api/analyze', methods=['POST'])
def api_run_analysis():
    """Manually trigger self-improvement analysis."""
    try:
        from self_improver import SelfImprover
        improver = SelfImprover(db.client)

        # Run full analysis
        results = improver.run_full_analysis(lookback_days=7)

        return jsonify({
            'status': 'success',
            'message': 'Self-improvement analysis completed',
            'results': results
        })
    except Exception as e:
        logger.error(f"Manual analysis trigger failed: {e}")
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500


@app.route('/api/weather')
def api_weather():
    """Weather predictions vs actual outcomes."""
    # Mock data for now
    return jsonify({
        'predictions': [],
        'note': 'Weather forecast vs actual outcome tracking'
    })


@app.route('/api/polymarket')
def api_polymarket():
    """Cross-platform price comparisons."""
    # Mock data for now
    return jsonify({
        'comparisons': [],
        'note': 'Kalshi vs Polymarket price comparisons'
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
<title>💰 $--.-- | KALSHI ALPHA</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  :root {
    --bg-primary: #0a0a0f;
    --bg-secondary: #111118;
    --bg-card: rgba(255, 255, 255, 0.03);
    --border: rgba(255, 255, 255, 0.08);
    --border-hover: rgba(0, 240, 255, 0.2);
    --text-primary: #ffffff;
    --text-secondary: #a0a0a0;
    --text-muted: #666666;
    --neon-cyan: #00f0ff;
    --neon-magenta: #ff006e;
    --neon-lime: #39ff14;
    --neon-red: #ff3333;
    --glass-bg: rgba(255, 255, 255, 0.03);
    --glass-border: rgba(255, 255, 255, 0.08);
  }

  body {
    font-family: 'Inter', sans-serif;
    background: var(--bg-primary);
    color: var(--text-primary);
    overflow-x: hidden;
  }

  .command-bar {
    position: sticky;
    top: 0;
    background: var(--bg-secondary);
    border-bottom: 1px solid var(--border);
    padding: 12px 20px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    z-index: 1000;
    backdrop-filter: blur(12px);
    -webkit-backdrop-filter: blur(12px);
  }

  .logo {
    font-size: 18px;
    font-weight: 700;
    color: var(--neon-cyan);
    text-shadow: 0 0 10px rgba(0, 240, 255, 0.5);
    letter-spacing: 1px;
  }

  .status-indicator {
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .status-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--neon-lime);
    animation: pulse 2s infinite;
  }

  .status-text {
    font-size: 12px;
    color: var(--text-secondary);
    font-family: 'JetBrains Mono', monospace;
  }

  .time-info {
    text-align: center;
    font-family: 'JetBrains Mono', monospace;
    font-size: 12px;
    color: var(--text-secondary);
  }

  .balance-display {
    text-align: right;
    font-family: 'JetBrains Mono', monospace;
  }

  .balance-amount {
    font-size: 16px;
    font-weight: 600;
    color: var(--neon-cyan);
  }

  .balance-change {
    font-size: 11px;
    margin-top: 2px;
  }

  .main-grid {
    display: grid;
    grid-template-columns: 1fr 350px;
    gap: 20px;
    padding: 20px;
    max-width: 1800px;
    margin: 0 auto;
  }

  .left-panel {
    display: grid;
    grid-template-rows: auto 1fr;
    gap: 20px;
  }

  .right-panel {
    display: grid;
    grid-template-rows: auto auto auto;
    gap: 20px;
  }

  .card {
    background: var(--glass-bg);
    backdrop-filter: blur(12px);
    -webkit-backdrop-filter: blur(12px);
    border: 1px solid var(--glass-border);
    border-radius: 16px;
    padding: 20px;
    transition: all 0.3s ease;
  }

  .card:hover {
    border-color: var(--border-hover);
    box-shadow: 0 0 30px rgba(0, 240, 255, 0.1);
  }

  .metrics-grid {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 16px;
    margin-bottom: 20px;
  }

  .metric-card {
    background: var(--glass-bg);
    backdrop-filter: blur(12px);
    border: 1px solid var(--glass-border);
    border-radius: 12px;
    padding: 16px;
    text-align: center;
    transition: all 0.3s ease;
  }

  .metric-card:hover {
    border-color: var(--border-hover);
    transform: translateY(-2px);
  }

  .metric-label {
    font-size: 10px;
    text-transform: uppercase;
    color: var(--text-secondary);
    margin-bottom: 8px;
    letter-spacing: 0.5px;
  }

  .metric-value {
    font-size: 24px;
    font-weight: 700;
    font-family: 'JetBrains Mono', monospace;
    margin-bottom: 4px;
  }

  .metric-change {
    font-size: 11px;
    font-family: 'JetBrains Mono', monospace;
  }

  .equity-chart-container {
    background: var(--glass-bg);
    backdrop-filter: blur(12px);
    border: 1px solid var(--glass-border);
    border-radius: 16px;
    padding: 20px;
    height: 300px;
  }

  .chart-title {
    font-size: 14px;
    font-weight: 600;
    color: var(--text-primary);
    margin-bottom: 16px;
    text-transform: uppercase;
    letter-spacing: 1px;
  }

  .trades-table {
    background: var(--glass-bg);
    backdrop-filter: blur(12px);
    border: 1px solid var(--glass-border);
    border-radius: 16px;
    overflow: hidden;
  }

  .table-header {
    background: var(--bg-secondary);
    padding: 12px 20px;
    border-bottom: 1px solid var(--border);
  }

  .table-title {
    font-size: 14px;
    font-weight: 600;
    color: var(--text-primary);
    text-transform: uppercase;
    letter-spacing: 1px;
  }

  table {
    width: 100%;
    border-collapse: collapse;
  }

  th {
    background: var(--bg-secondary);
    color: var(--text-secondary);
    font-size: 10px;
    text-transform: uppercase;
    padding: 12px 16px;
    text-align: left;
    font-weight: 600;
    letter-spacing: 0.5px;
  }

  td {
    padding: 12px 16px;
    border-top: 1px solid var(--border);
    font-size: 12px;
    font-family: 'JetBrains Mono', monospace;
  }

  .strategy-badge {
    display: inline-block;
    padding: 4px 8px;
    border-radius: 8px;
    font-size: 9px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }

  .badge-grok { background: rgba(157, 93, 229, 0.2); color: #9b5de5; }
  .badge-weather { background: rgba(0, 180, 216, 0.2); color: #00b4d8; }
  .badge-arb { background: rgba(245, 37, 133, 0.2); color: #f72585; }
  .badge-sports { background: rgba(255, 109, 0, 0.2); color: #ff6d00; }
  .badge-near { background: rgba(254, 237, 68, 0.2); color: #fee440; }
  .badge-highprob { background: rgba(6, 214, 160, 0.2); color: #06d6a0; }
  .badge-mention { background: rgba(255, 0, 110, 0.2); color: #ff006e; }
  .badge-cross { background: rgba(58, 134, 255, 0.2); color: #3a86ff; }
  .badge-orderbook { background: rgba(131, 56, 236, 0.2); color: #8338ec; }
  .badge-mm { background: rgba(251, 86, 7, 0.2); color: #fb5607; }
  .badge-forced { background: rgba(128, 128, 128, 0.2); color: #808080; }

  .confidence-badge {
    display: inline-block;
    padding: 2px 6px;
    border-radius: 6px;
    font-size: 9px;
    font-weight: 600;
  }

  .conf-high { background: rgba(57, 255, 20, 0.2); color: var(--neon-lime); }
  .conf-med { background: rgba(0, 240, 255, 0.2); color: var(--neon-cyan); }
  .conf-low { background: rgba(255, 51, 51, 0.2); color: var(--neon-red); }

  .pnl-positive { color: var(--neon-lime); }
  .pnl-negative { color: var(--neon-red); }

  .strategy-performance {
    background: var(--glass-bg);
    backdrop-filter: blur(12px);
    border: 1px solid var(--glass-border);
    border-radius: 16px;
    padding: 20px;
  }

  .debate-monitor {
    background: var(--glass-bg);
    backdrop-filter: blur(12px);
    border: 1px solid var(--glass-border);
    border-radius: 16px;
    padding: 20px;
  }

  .debate-item {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 12px;
    background: rgba(255, 255, 255, 0.02);
    border-radius: 8px;
    margin-bottom: 8px;
  }

  .debate-avatars {
    display: flex;
    gap: 8px;
  }

  .avatar {
    width: 24px;
    height: 24px;
    border-radius: 50%;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 10px;
    font-weight: 600;
  }

  .avatar-grok { background: rgba(157, 93, 229, 0.3); color: #9b5de5; }
  .avatar-claude { background: rgba(245, 37, 133, 0.3); color: #f72585; }

  .debate-result {
    flex: 1;
    font-size: 11px;
    font-family: 'JetBrains Mono', monospace;
  }

  .debate-decision {
    font-weight: 600;
    color: var(--neon-cyan);
  }

  .risk-dashboard {
    background: var(--glass-bg);
    backdrop-filter: blur(12px);
    border: 1px solid var(--glass-border);
    border-radius: 16px;
    padding: 20px;
  }

  .risk-metrics {
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    gap: 16px;
  }

  .risk-item {
    text-align: center;
  }

  .risk-label {
    font-size: 10px;
    color: var(--text-secondary);
    text-transform: uppercase;
    margin-bottom: 8px;
  }

  .risk-value {
    font-size: 18px;
    font-weight: 700;
    font-family: 'JetBrains Mono', monospace;
    color: var(--neon-cyan);
  }

  .bottom-panels {
    margin-top: 20px;
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(400px, 1fr));
    gap: 20px;
  }

  .tab-container {
    background: var(--glass-bg);
    backdrop-filter: blur(12px);
    border: 1px solid var(--glass-border);
    border-radius: 16px;
    overflow: hidden;
  }

  .tab-buttons {
    display: flex;
    background: var(--bg-secondary);
    border-bottom: 1px solid var(--border);
  }

  .tab-btn {
    flex: 1;
    padding: 12px 16px;
    background: none;
    border: none;
    color: var(--text-secondary);
    font-size: 12px;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.3s ease;
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }

  .tab-btn.active {
    background: var(--glass-bg);
    color: var(--neon-cyan);
  }

  .tab-content {
    max-height: 300px;
    overflow-y: auto;
    padding: 0;
  }

  .tab-pane {
    display: none;
    padding: 20px;
  }

  .tab-pane.active {
    display: block;
  }

  .signal-item {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 8px 0;
    border-bottom: 1px solid var(--border);
    font-size: 11px;
    font-family: 'JetBrains Mono', monospace;
  }

  .signal-time {
    color: var(--text-secondary);
    min-width: 60px;
  }

  .signal-badge {
    min-width: 80px;
  }

  .signal-content {
    flex: 1;
    color: var(--text-primary);
  }

  .signal-action {
    color: var(--neon-cyan);
    font-weight: 600;
  }

  .loading {
    color: var(--text-secondary);
    padding: 40px;
    text-align: center;
    font-size: 14px;
  }

  @keyframes pulse {
    0%, 100% { opacity: 1; box-shadow: 0 0 8px var(--neon-lime); }
    50% { opacity: 0.5; box-shadow: 0 0 20px var(--neon-lime); }
  }

  @keyframes countUp {
    from { opacity: 0; transform: translateY(10px); }
    to { opacity: 1; transform: translateY(0); }
  }

  .animate-in {
    animation: countUp 0.5s ease-out;
  }

  /* Scrollbar styling */
  ::-webkit-scrollbar {
    width: 6px;
  }

  ::-webkit-scrollbar-track {
    background: var(--bg-primary);
  }

  ::-webkit-scrollbar-thumb {
    background: var(--border);
    border-radius: 3px;
  }

  ::-webkit-scrollbar-thumb:hover {
    background: var(--border-hover);
  }

  /* Mobile responsiveness */
  @media (max-width: 1200px) {
    .main-grid {
      grid-template-columns: 1fr;
      grid-template-rows: auto auto;
    }

    .right-panel {
      order: -1;
    }
  }

  @media (max-width: 768px) {
    .metrics-grid {
      grid-template-columns: repeat(2, 1fr);
    }

    .command-bar {
      flex-direction: column;
      gap: 12px;
      text-align: center;
    }

    .tab-buttons {
      flex-direction: column;
    }
  }
</style>
</head>
<body>
  <!-- Command Bar -->
  <div class="command-bar">
    <div class="logo">KALSHI ALPHA</div>
    <div class="status-indicator">
      <div class="status-dot" id="statusDot"></div>
      <div class="status-text" id="statusText">CONNECTING...</div>
    </div>
    <div class="time-info">
      <div id="currentTime">--:--:-- PST</div>
      <div id="lastScan">Last scan: never</div>
    </div>
    <div class="balance-display">
      <div class="balance-amount" id="balanceAmount">$--.--</div>
      <div class="balance-change" id="balanceChange">--</div>
    </div>
  </div>

  <!-- Main Content -->
  <div class="main-grid">
    <!-- Left Panel -->
    <div class="left-panel">
      <!-- Key Metrics -->
      <div class="metrics-grid" id="metricsGrid">
        <div class="metric-card">
          <div class="metric-label">Balance</div>
          <div class="metric-value" id="balanceValue">$--.--</div>
          <div class="metric-change" id="balanceChangeMetric">--</div>
        </div>
        <div class="metric-card">
          <div class="metric-label">Win Rate</div>
          <div class="metric-value" id="winRateValue">--%</div>
          <div class="metric-change" id="winRateChange">-- wins</div>
        </div>
        <div class="metric-card">
          <div class="metric-label">Active Positions</div>
          <div class="metric-value" id="positionsValue">--</div>
          <div class="metric-change" id="positionsChange">--</div>
        </div>
        <div class="metric-card">
          <div class="metric-label">Daily P&L</div>
          <div class="metric-value" id="dailyPnlValue">--</div>
          <div class="metric-change" id="dailyPnlChange">--</div>
        </div>
      </div>

      <!-- Equity Curve Chart -->
      <div class="equity-chart-container">
        <div class="chart-title">Equity Curve</div>
        <canvas id="equityChart"></canvas>
      </div>

      <!-- Recent Trades Table -->
      <div class="trades-table">
        <div class="table-header">
          <div class="table-title">Recent Trades</div>
        </div>
        <table id="tradesTable">
          <thead>
            <tr>
              <th>Time</th>
              <th>Strategy</th>
              <th>Market</th>
              <th>Side</th>
              <th>Size</th>
              <th>P&L</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody id="tradesTableBody">
            <tr><td colspan="7" class="loading">Loading trades...</td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- Right Panel -->
    <div class="right-panel">
      <!-- Strategy Performance -->
      <div class="strategy-performance">
        <div class="chart-title">Strategy Performance</div>
        <canvas id="strategyChart" height="200"></canvas>
      </div>

      <!-- AI Debate Monitor -->
      <div class="debate-monitor">
        <div class="chart-title">AI Debate Monitor</div>
        <div id="debateContainer">
          <div class="loading">Loading debates...</div>
        </div>
      </div>

      <!-- Risk Dashboard -->
      <div class="risk-dashboard">
        <div class="chart-title">Risk Dashboard</div>
        <div class="risk-metrics">
          <div class="risk-item">
            <div class="risk-label">Cash Reserve</div>
            <div class="risk-value" id="cashReserve">30%</div>
          </div>
          <div class="risk-item">
            <div class="risk-label">Daily Loss Limit</div>
            <div class="risk-value" id="dailyLossLimit">-$30</div>
          </div>
          <div class="risk-item">
            <div class="risk-label">Kelly Fraction</div>
            <div class="risk-value" id="kellyFraction">0.10</div>
          </div>
          <div class="risk-item">
            <div class="risk-label">Circuit Breaker</div>
            <div class="risk-value" id="circuitBreaker">OFF</div>
          </div>
        </div>
      </div>
    </div>
  </div>

  <!-- Bottom Tabbed Panels -->
  <div class="bottom-panels">
    <div class="tab-container">
      <div class="tab-buttons">
        <button class="tab-btn active" onclick="switchTab('signals')">Signal Feed</button>
        <button class="tab-btn" onclick="switchTab('weather')">Weather Station</button>
        <button class="tab-btn" onclick="switchTab('settlements')">Settlements</button>
      </div>
      <div class="tab-content">
        <div class="tab-pane active" id="signalsTab">
          <div id="signalsContainer" class="loading">Loading signals...</div>
        </div>
        <div class="tab-pane" id="weatherTab">
          <div id="weatherContainer" class="loading">Weather tracking coming soon...</div>
        </div>
        <div class="tab-pane" id="settlementsTab">
          <div id="settlementsContainer" class="loading">Loading settlements...</div>
        </div>
      </div>
    </div>
  </div>

  <script>
    // Global state
    let currentData = {};
    let equityChart = null;
    let strategyChart = null;

    // Initialize
    function init() {
      updateTime();
      setInterval(updateTime, 1000);
      loadData();
      setInterval(loadData, 10000); // Update every 10 seconds
    }

    // Update current time
    function updateTime() {
      const now = new Date();
      const timeString = now.toLocaleTimeString('en-US', {
        hour12: false,
        timeZone: 'America/Los_Angeles'
      });
      document.getElementById('currentTime').textContent = timeString + ' PST';
    }

    // Load all dashboard data
    async function loadData() {
      try {
        const [statusRes, balanceRes, tradesRes, strategiesRes, equityRes, signalsRes, debatesRes, riskRes] = await Promise.all([
          fetch('/api/status'),
          fetch('/api/balance'),
          fetch('/api/trades/recent'),
          fetch('/api/strategies'),
          fetch('/api/equity-curve'),
          fetch('/api/signals'),
          fetch('/api/debates'),
          fetch('/api/risk')
        ]);

        const status = await statusRes.json();
        const balance = await balanceRes.json();
        const trades = await tradesRes.json();
        const strategies = await strategiesRes.json();
        const equity = await equityRes.json();
        const signals = await signalsRes.json();
        const debates = await debatesRes.json();
        const risk = await riskRes.json();

        currentData = { status, balance, trades, strategies, equity, signals, debates, risk };
        updateDashboard();

      } catch (error) {
        console.error('Dashboard update error:', error);
        showError('Failed to load dashboard data');
      }
    }

    // Update all dashboard components
    function updateDashboard() {
      updateCommandBar();
      updateMetrics();
      updateEquityChart();
      updateTradesTable();
      updateStrategyChart();
      updateDebateMonitor();
      updateRiskDashboard();
      updateSignalsFeed();
      updateSettlements();

      // Update page title with balance
      const balance = currentData.balance?.current_balance || 0;
      document.title = `💰 $${balance.toFixed(2)} | KALSHI ALPHA`;
    }

    // Update command bar
    function updateCommandBar() {
      const status = currentData.status?.bot || {};
      const balance = currentData.balance?.current_balance || 0;
      const dailyPnl = currentData.balance?.daily_pnl || 0;

      // Status indicator
      const isRunning = status.is_running;
      document.getElementById('statusDot').style.background = isRunning ? 'var(--neon-lime)' : 'var(--neon-red)';
      document.getElementById('statusText').textContent = isRunning ? 'RUNNING' : 'STOPPED';

      // Last scan
      const lastCheck = status.last_check;
      if (lastCheck && lastCheck !== 'never') {
        const lastScanTime = new Date(lastCheck);
        const now = new Date();
        const diffMinutes = Math.floor((now - lastScanTime) / 60000);
        document.getElementById('lastScan').textContent = `Last scan: ${diffMinutes}m ago`;
      }

      // Balance display
      document.getElementById('balanceAmount').textContent = `$${balance.toFixed(2)}`;
      const pnlClass = dailyPnl >= 0 ? 'pnl-positive' : 'pnl-negative';
      const pnlSign = dailyPnl >= 0 ? '+' : '';
      document.getElementById('balanceChange').innerHTML = `<span class="${pnlClass}">${pnlSign}$${dailyPnl.toFixed(2)}</span>`;
    }

    // Update key metrics
    function updateMetrics() {
      const balance = currentData.balance || {};
      const status = currentData.status?.stats || {};

      // Balance
      document.getElementById('balanceValue').textContent = `$${balance.current_balance?.toFixed(2) || '--.--'}`;
      document.getElementById('balanceChangeMetric').textContent = `${balance.roi_percentage?.toFixed(1) || '--'}% ROI`;

      // Win Rate
      const winRate = status.win_rate || 0;
      document.getElementById('winRateValue').textContent = `${winRate.toFixed(1)}%`;
      document.getElementById('winRateChange').textContent = `${status.wins || 0}/${status.settled || 0} wins`;

      // Positions
      document.getElementById('positionsValue').textContent = balance.open_positions || 0;
      document.getElementById('positionsChange').textContent = `${balance.total_trades || 0} total trades`;

      // Daily P&L
      const dailyPnl = balance.daily_pnl || 0;
      const pnlClass = dailyPnl >= 0 ? 'pnl-positive' : 'pnl-negative';
      const pnlSign = dailyPnl >= 0 ? '+' : '';
      document.getElementById('dailyPnlValue').innerHTML = `<span class="${pnlClass}">${pnlSign}$${dailyPnl.toFixed(2)}</span>`;
      document.getElementById('dailyPnlChange').textContent = 'today';
    }

    // Update equity curve chart
    function updateEquityChart() {
      const equity = currentData.equity?.snapshots || [];
      const ctx = document.getElementById('equityChart').getContext('2d');

      const labels = equity.map(s => new Date(s.timestamp).toLocaleDateString());
      const data = equity.map(s => s.balance);

      if (equityChart) {
        equityChart.destroy();
      }

      equityChart = new Chart(ctx, {
        type: 'line',
        data: {
          labels: labels,
          datasets: [{
            label: 'Balance',
            data: data,
            borderColor: 'var(--neon-cyan)',
            backgroundColor: 'rgba(0, 240, 255, 0.1)',
            borderWidth: 2,
            fill: true,
            tension: 0.4
          }]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: {
            legend: { display: false }
          },
          scales: {
            x: {
              grid: { color: 'var(--border)' },
              ticks: { color: 'var(--text-secondary)' }
            },
            y: {
              grid: { color: 'var(--border)' },
              ticks: { color: 'var(--text-secondary)' }
            }
          }
        }
      });
    }

    // Update trades table
    function updateTradesTable() {
      const trades = currentData.trades?.trades || [];
      const tbody = document.getElementById('tradesTableBody');

      if (trades.length === 0) {
        tbody.innerHTML = '<tr><td colspan="7" class="loading">No trades yet</td></tr>';
        return;
      }

      tbody.innerHTML = trades.slice(0, 15).map(trade => {
        const time = new Date(trade.timestamp).toLocaleTimeString();
        const strategy = trade.strategy || 'unknown';
        const strategyClass = `badge-${strategy.toLowerCase().replace('_learning', '')}`;
        const side = (trade.side || '').toUpperCase();
        const count = trade.count || 0;
        const price = trade.price ? `$${trade.price.toFixed(2)}` : '--';
        const pnl = trade.pnl !== undefined ? (trade.pnl >= 0 ? `+$${trade.pnl.toFixed(2)}` : `$${trade.pnl.toFixed(2)}`) : '--';
        const pnlClass = trade.pnl >= 0 ? 'pnl-positive' : 'pnl-negative';
        const status = trade.action === 'settle' ? 'SETTLED' : 'OPEN';

        return `
          <tr>
            <td>${time}</td>
            <td><span class="strategy-badge ${strategyClass}">${strategy.replace('_LEARNING', '')}</span></td>
            <td>${trade.ticker || '--'}</td>
            <td>${side}</td>
            <td>${count}</td>
            <td class="${pnlClass}">${pnl}</td>
            <td>${status}</td>
          </tr>
        `;
      }).join('');
    }

    // Update strategy performance chart
    function updateStrategyChart() {
      const strategies = currentData.strategies?.strategies || [];
      const ctx = document.getElementById('strategyChart').getContext('2d');

      const labels = strategies.map(s => s.name);
      const data = strategies.map(s => s.total_pnl);

      if (strategyChart) {
        strategyChart.destroy();
      }

      strategyChart = new Chart(ctx, {
        type: 'bar',
        data: {
          labels: labels,
          datasets: [{
            label: 'P&L',
            data: data,
            backgroundColor: 'rgba(0, 240, 255, 0.6)',
            borderColor: 'var(--neon-cyan)',
            borderWidth: 1
          }]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: {
            legend: { display: false }
          },
          scales: {
            x: {
              grid: { color: 'var(--border)' },
              ticks: { color: 'var(--text-secondary)' }
            },
            y: {
              grid: { color: 'var(--border)' },
              ticks: { color: 'var(--text-secondary)' }
            }
          }
        }
      });
    }

    // Update AI debate monitor
    function updateDebateMonitor() {
      const debates = currentData.debates?.debates || [];
      const container = document.getElementById('debateContainer');

      if (debates.length === 0) {
        container.innerHTML = '<div class="loading">No debates yet</div>';
        return;
      }

      container.innerHTML = debates.slice(0, 5).map(debate => `
        <div class="debate-item">
          <div class="debate-avatars">
            <div class="avatar avatar-grok">G</div>
            <div class="avatar avatar-claude">C</div>
          </div>
          <div class="debate-result">
            <div>${debate.market_title || debate.ticker}</div>
            <div class="debate-decision">${debate.final_decision || 'TRADE'}</div>
          </div>
        </div>
      `).join('');
    }

    // Update risk dashboard
    function updateRiskDashboard() {
      const risk = currentData.risk || {};

      document.getElementById('cashReserve').textContent = `${risk.cash_reserve_percentage || 30}%`;
      document.getElementById('dailyLossLimit').textContent = `$${risk.current_daily_loss?.toFixed(2) || '0.00'}`;
      document.getElementById('kellyFraction').textContent = `${risk.kelly_fraction?.toFixed(2) || '0.10'}`;
      document.getElementById('circuitBreaker').textContent = risk.circuit_breaker_active ? 'ON' : 'OFF';
    }

    // Update signals feed
    function updateSignalsFeed() {
      const signals = currentData.signals?.signals || [];
      const container = document.getElementById('signalsContainer');

      if (signals.length === 0) {
        container.innerHTML = '<div class="loading">No signals yet</div>';
        return;
      }

      container.innerHTML = signals.slice(0, 20).map(signal => {
        const time = new Date(signal.timestamp).toLocaleTimeString();
        const strategy = signal.strategy || 'unknown';
        const strategyClass = `badge-${strategy.toLowerCase().replace('_learning', '')}`;
        const action = signal.action || 'UNKNOWN';
        const actionClass = action === 'TRADE' ? 'signal-action' : 'signal-action-skip';

        return `
          <div class="signal-item">
            <div class="signal-time">${time}</div>
            <div class="signal-badge">
              <span class="strategy-badge ${strategyClass}">${strategy.replace('_LEARNING', '')}</span>
            </div>
            <div class="signal-content">${signal.ticker} — Edge: ${(signal.edge || 0).toFixed(2)} | Conf: ${signal.confidence || 0}</div>
            <div class="${actionClass}">${action}</div>
          </div>
        `;
      }).join('');
    }

    // Update settlements
    function updateSettlements() {
      // Placeholder for settlements data
      document.getElementById('settlementsContainer').innerHTML = '<div class="loading">Settlements tracking coming soon...</div>';
    }

    // Tab switching
    function switchTab(tabName) {
      // Update tab buttons
      document.querySelectorAll('.tab-btn').forEach(btn => {
        btn.classList.remove('active');
      });
      document.querySelector(`[onclick="switchTab('${tabName}')"]`).classList.add('active');

      // Update tab content
      document.querySelectorAll('.tab-pane').forEach(pane => {
        pane.classList.remove('active');
      });
      document.getElementById(`${tabName}Tab`).classList.add('active');
    }

    // Error handling
    function showError(message) {
      console.error(message);
      // Could show user-friendly error message
    }

    // Initialize on page load
    document.addEventListener('DOMContentLoaded', init);
  </script>
</body>
</html>"""
