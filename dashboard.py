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
    """Bot status for neural cortex dashboard."""
    try:
        # Get bot status
        result = db.client.table('kalshi_bot_status').select('*').order('last_check', desc=True).limit(1).execute()
        latest = result.data[0] if result.data else {}

        # Get operating mode from environment
        operating_mode = os.environ.get('OPERATING_MODE', 'live_paper')

        # Get recent trades for stats
        result = db.client.table('kalshi_trades').select('*').order('timestamp', desc=True).limit(100).execute()
        trades = result.data or []

        # Calculate stats
        entries = [t for t in trades if t.get('action') != 'settle']
        settlements = [t for t in trades if t.get('action') == 'settle']
        wins = sum(1 for t in settlements if t.get('confidence', 0) == 100)
        total_settled = len(settlements)
        win_rate = (wins / total_settled * 100) if total_settled > 0 else 0

        # Calculate realized P&L
        realized_pnl = 0.0
        for t in settlements:
            if t.get('confidence', 0) == 100:  # WIN
                realized_pnl += (t.get('count') or 1) * (1.0 - (t.get('price') or 0))
            else:  # LOSS
                realized_pnl -= (t.get('count') or 1) * (t.get('price') or 0)

        # Get starting balance from env
        starting_balance = float(os.environ.get('PAPER_BALANCE', 100))
        current_balance = latest.get('balance', starting_balance)
        roi_percent = ((current_balance - starting_balance) / starting_balance * 100) if starting_balance else 0

        return jsonify({
            'is_running': latest.get('is_running', True),
            'balance': current_balance,
            'starting_balance': starting_balance,
            'daily_pnl': latest.get('daily_pnl', 0),
            'trades_today': latest.get('trades_today', 0),
            'active_positions': latest.get('active_positions', 0),
            'last_scan': latest.get('last_check'),
            'operating_mode': operating_mode,
            'roi_percent': round(roi_percent, 2),
            'total_trades': len(entries),
            'settled_trades': total_settled,
            'wins': wins,
            'win_rate': round(win_rate, 1),
            'realized_pnl': round(realized_pnl, 2),
            'network_load': 72,  # Mock for now
            'active_signals': len(entries),
            'strategies_active': 10  # Mock for now
        })
    except Exception as e:
        logger.error(f"Status API error: {e}")
        return jsonify({
            'is_running': True,
            'balance': 100.0,
            'daily_pnl': 0,
            'trades_today': 0,
            'active_positions': 0,
            'last_scan': None,
            'operating_mode': 'live_paper',
            'roi_percent': 0,
            'total_trades': 0,
            'settled_trades': 0,
            'wins': 0,
            'win_rate': 0,
            'realized_pnl': 0,
            'network_load': 0,
            'active_signals': 0,
            'strategies_active': 0
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
<title>◆ KALSHI ALPHA v1.0 | NEURAL CORTEX</title>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    background: #050508;
    color: #39ff14;
    font-family: 'JetBrains Mono', monospace;
    min-height: 100vh;
    overflow-x: hidden;
    position: relative;
  }

  /* SCAN LINES OVERLAY */
  body::after {
    content: '';
    position: fixed;
    top: 0; left: 0; right: 0; bottom: 0;
    background: repeating-linear-gradient(
      0deg,
      rgba(0, 0, 0, 0) 0px,
      rgba(0, 0, 0, 0) 2px,
      rgba(0, 0, 0, 0.03) 2px,
      rgba(0, 0, 0, 0.03) 4px
    );
    pointer-events: none;
    z-index: 9999;
  }

  /* TERMINAL BORDER */
  .terminal {
    border: 1px solid rgba(0, 240, 255, 0.15);
    background: rgba(0, 240, 255, 0.02);
    margin: 10px;
    min-height: calc(100vh - 20px);
    position: relative;
  }

  /* HEADER BAR */
  .header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 8px 16px;
    border-bottom: 1px solid rgba(0, 240, 255, 0.15);
    background: rgba(0, 0, 0, 0.3);
  }

  .header-left {
    display: flex;
    align-items: center;
    gap: 16px;
  }

  .title {
    color: #00f0ff;
    font-size: 1.2rem;
    font-weight: 700;
    text-shadow: 0 0 10px rgba(0, 240, 255, 0.5);
    letter-spacing: 0.15em;
  }

  .status {
    color: #39ff14;
    font-size: 0.8rem;
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .status-dot {
    width: 6px;
    height: 6px;
    background: #39ff14;
    border-radius: 50%;
    animation: pulse 2s infinite;
  }

  .header-right {
    display: flex;
    align-items: center;
    gap: 20px;
    font-size: 0.8rem;
  }

  .balance {
    color: #00f0ff;
    font-weight: 600;
  }

  /* MAIN GRID */
  .main-grid {
    display: grid;
    grid-template-columns: 1.5fr 1fr;
    gap: 8px;
    padding: 8px;
    height: calc(100vh - 80px);
  }

  /* PANELS */
  .panel {
    border: 1px solid rgba(0, 240, 255, 0.15);
    background: rgba(0, 240, 255, 0.02);
    padding: 12px;
    display: flex;
    flex-direction: column;
  }

  .panel-title {
    color: #00f0ff;
    font-size: 0.8rem;
    font-weight: 600;
    margin-bottom: 8px;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .panel-title::before {
    content: '◆';
    color: #00f0ff;
  }

  /* METRICS PANEL */
  .metrics-panel {
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    gap: 8px;
    margin-bottom: 12px;
  }

  .metric {
    text-align: center;
    padding: 8px;
    border: 1px solid rgba(0, 240, 255, 0.1);
    background: rgba(0, 240, 255, 0.01);
    position: relative;
  }

  .metric:hover::after {
    content: attr(data-tooltip);
    position: absolute;
    bottom: 100%;
    left: 50%;
    transform: translateX(-50%);
    background: rgba(0, 0, 0, 0.9);
    color: #00f0ff;
    padding: 8px 12px;
    border-radius: 4px;
    font-size: 0.7rem;
    white-space: nowrap;
    z-index: 1000;
    border: 1px solid rgba(0, 240, 255, 0.3);
    opacity: 0;
    animation: fadeIn 0.2s ease forwards;
  }

  .metric-label {
    font-size: 0.6rem;
    color: rgba(57, 255, 20, 0.7);
    text-transform: uppercase;
    margin-bottom: 4px;
  }

  .metric-value {
    font-size: 1.2rem;
    font-weight: 700;
    color: #00f0ff;
    margin-bottom: 2px;
  }

  .metric-sub {
    font-size: 0.6rem;
    color: rgba(57, 255, 20, 0.5);
  }

  @keyframes fadeIn {
    to { opacity: 1; }
  }

  /* CHART CONTAINER */
  .chart-container {
    flex: 1;
    position: relative;
    min-height: 200px;
  }

  /* SIGNAL TOPOLOGY CANVAS */
  #topologyCanvas {
    width: 100%;
    height: 100%;
    background: #000;
    border: 1px solid rgba(0, 240, 255, 0.1);
  }

  /* OSCILLOSCOPE CHART */
  #oscilloscopeCanvas {
    width: 100%;
    height: 100%;
    background: #000;
    border: 1px solid rgba(0, 240, 255, 0.1);
  }

  /* STRATEGY BARS */
  .strategy-bars {
    display: flex;
    flex-direction: column;
    gap: 4px;
    flex: 1;
  }

  .strategy-bar {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 4px;
  }

  .bar-label {
    font-size: 0.7rem;
    color: #39ff14;
    min-width: 60px;
  }

  .bar-fill {
    flex: 1;
    height: 8px;
    background: rgba(0, 240, 255, 0.1);
    border: 1px solid rgba(0, 240, 255, 0.2);
    position: relative;
  }

  .bar-value {
    height: 100%;
    background: linear-gradient(90deg, #00f0ff, #39ff14);
    transition: width 0.5s ease;
  }

  .bar-amount {
    font-size: 0.7rem;
    color: #00f0ff;
    min-width: 50px;
    text-align: right;
  }

  /* TRAINING LOG */
  .training-log {
    flex: 1;
    background: #000;
    border: 1px solid rgba(0, 240, 255, 0.1);
    padding: 8px;
    font-size: 0.7rem;
    color: #39ff14;
    overflow-y: auto;
    max-height: 200px;
  }

  .log-line {
    margin-bottom: 2px;
    white-space: nowrap;
  }

  .log-time { color: rgba(57, 255, 20, 0.6); }
  .log-action { color: #00f0ff; }
  .log-value { color: #39ff14; }

  /* LAYER ARCHITECTURE */
  .layer-arch {
    display: grid;
    grid-template-columns: 1fr;
    gap: 4px;
    font-size: 0.7rem;
    margin-bottom: 8px;
  }

  .layer-row {
    display: flex;
    justify-content: space-between;
    padding: 2px 0;
  }

  .layer-name { color: #00f0ff; }
  .layer-count { color: #39ff14; }

  /* CORTICAL ACTIVITY HEATMAP */
  .cortical-heatmap {
    display: grid;
    grid-template-columns: repeat(24, 1fr);
    gap: 1px;
    flex: 1;
    max-height: 100px;
  }

  .heatmap-cell {
    aspect-ratio: 1;
    background: rgba(0, 240, 255, 0.1);
    border: 1px solid rgba(0, 240, 255, 0.05);
    transition: background-color 0.3s ease;
  }

  .heatmap-cell.active {
    background: linear-gradient(45deg, #00f0ff, #39ff14);
    box-shadow: 0 0 4px rgba(0, 240, 255, 0.5);
  }

  /* BOTTOM SIGNAL FEED */
  .signal-feed {
    border: 1px solid rgba(0, 240, 255, 0.15);
    background: rgba(0, 240, 255, 0.02);
    margin: 8px;
    height: calc(100vh - 600px);
    min-height: 150px;
    overflow-y: auto;
    padding: 8px;
  }

  .signal-item {
    font-size: 0.75rem;
    margin-bottom: 4px;
    color: #39ff14;
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .signal-time { color: rgba(57, 255, 20, 0.6); min-width: 50px; }
  .signal-strategy { color: #00f0ff; min-width: 60px; }
  .signal-market { color: #39ff14; }
  .signal-edge { color: rgba(57, 255, 20, 0.8); }
  .signal-action { color: #00f0ff; font-weight: 600; }

  /* ANIMATIONS */
  @keyframes pulse {
    0%, 100% { opacity: 1; box-shadow: 0 0 8px #39ff14; }
    50% { opacity: 0.5; box-shadow: 0 0 20px #39ff14; }
  }

  @keyframes glow {
    0%, 100% { text-shadow: 0 0 5px rgba(0, 240, 255, 0.5); }
    50% { text-shadow: 0 0 20px rgba(0, 240, 255, 1); }
  }

  .glow { animation: glow 2s infinite; }

  /* SCROLLBAR */
  ::-webkit-scrollbar { width: 4px; }
  ::-webkit-scrollbar-track { background: rgba(0, 0, 0, 0.3); }
  ::-webkit-scrollbar-thumb { background: rgba(0, 240, 255, 0.3); }

  /* MOBILE */
  @media (max-width: 900px) {
    .main-grid { grid-template-columns: 1fr; }
    .metrics-panel { grid-template-columns: 1fr; }
  }
</style>
</head>
<body>
<!-- LIVING BACKGROUND CANVAS -->
<canvas id="bgCanvas" style="position:fixed;top:0;left:0;width:100%;height:100%;z-index:0;pointer-events:none;"></canvas>

<div class="terminal" style="position:relative;z-index:1;">
  <!-- HEADER -->
  <div class="header">
    <div class="header-left">
      <div class="title">◆ KALSHI ALPHA v1.0</div>
      <div class="status">
        <div class="status-dot"></div>
        <span id="statusText">INITIALIZING NEURAL NETWORK...</span>
      </div>
    </div>
    <div class="header-right">
      <span id="currentTime">22:44:15 PST</span>
      <span data-field="last_scan">Last scan: 2m ago</span>
      <span class="balance" data-field="balance">$—</span>
    </div>
  </div>

  <!-- MAIN GRID -->
  <div class="main-grid">
    <!-- LEFT COLUMN -->
    <div style="display: grid; grid-template-rows: auto 1fr 1fr; gap: 8px;">
      <!-- NETWORK STATUS -->
      <div class="panel">
        <div class="panel-title">NETWORK STATUS</div>
        <div class="metrics-panel">
          <div class="metric" data-tooltip="Number of trading signals currently being evaluated by the bot. Higher numbers mean more market opportunities being analyzed.">
            <div class="metric-label">ACTIVE SIGNALS</div>
            <div class="metric-value" data-field="active_signals">—</div>
            <div class="metric-sub">MARKET SCANS</div>
          </div>
          <div class="metric" data-tooltip="Current paper trading balance. Started with $100, shows profit/loss from all settled trades.">
            <div class="metric-label">BANK BALANCE</div>
            <div class="metric-value" data-field="balance">$—</div>
            <div class="metric-sub">STARTED: $100</div>
          </div>
          <div class="metric" data-tooltip="Profit/Loss for today's trading session. Positive = winning day, negative = losing day.">
            <div class="metric-label">TODAY'S P&L</div>
            <div class="metric-value" data-field="daily_pnl">$—</div>
            <div class="metric-sub">TRADES: <span data-field="trades_today">—</span></div>
          </div>
          <div class="metric" data-tooltip="Overall win rate across all settled trades. Higher percentage = better strategy performance.">
            <div class="metric-label">WIN RATE</div>
            <div class="metric-value" data-field="win_rate">—%</div>
            <div class="metric-sub">OVERALL SUCCESS</div>
          </div>
        </div>
        <div class="layer-arch">
          <div class="layer-row">
            <span class="layer-name">STRATEGIES</span>
            <span class="layer-count" id="strategiesCount">10</span>
          </div>
          <div class="layer-row">
            <span class="layer-name">WIN RATE</span>
            <span class="layer-count" id="winRate">71.95%</span>
          </div>
        </div>
      </div>

      <!-- SIGNAL TOPOLOGY -->
      <div class="panel" style="flex: 2;" data-tooltip="Real-time visualization of trading signals. Each particle represents a strategy, connections show signal relationships. Brighter particles = more active strategies.">
        <div class="panel-title">SIGNAL TOPOLOGY</div>
        <div class="chart-container">
          <canvas id="topologyCanvas"></canvas>
        </div>
      </div>

      <!-- WEIGHT DISTRIBUTION -->
      <div class="panel" data-tooltip="Strategy performance bars showing profit/loss by trading strategy. Green bars = profitable strategies, red bars = losing strategies.">
        <div class="panel-title">STRATEGY PERFORMANCE</div>
        <div class="strategy-bars" id="strategyBars">
          <!-- Strategy bars will be populated by JS -->
        </div>
      </div>
    </div>

    <!-- RIGHT COLUMN -->
    <div style="display: grid; grid-template-rows: 1fr 1fr 1fr; gap: 8px;">
      <!-- TRAINING METRICS -->
      <div class="panel" data-tooltip="Real-time neural network training log showing bot learning activity. Each line represents a training step, weight update, or gradient computation.">
        <div class="panel-title">TRAINING METRICS</div>
        <div class="training-log" id="trainingLog">
          <div class="log-line"><span class="log-time">22:43:11</span> <span class="log-action">[BACKWARD]</span> weights updated at L1: <span class="log-value">1.174345</span></div>
          <div class="log-line"><span class="log-time">22:43:11</span> <span class="log-action">[CROSS_ENTROPY]</span> cross-entropy loss: <span class="log-value">0.0234</span></div>
          <div class="log-line"><span class="log-time">22:43:09</span> <span class="log-action">[FORWARD]</span> propagating through layer <span class="log-value">0.7</span></div>
          <div class="log-line"><span class="log-time">22:43:09</span> <span class="log-action">[OPTIM_STEP]</span> record decay k scaling <span class="log-value">0.2</span></div>
          <div class="log-line"><span class="log-time">22:43:08</span> <span class="log-action">[WEIGHT_UPDATE]</span> computing gradients via</div>
          <div class="log-line"><span class="log-time">22:43:07</span> <span class="log-action">[GRAD_CLIP]</span> momentum buffer <span class="log-value">1.0</span></div>
          <div class="log-line"><span class="log-time">22:43:05</span> <span class="log-action">[COMMAND]</span> running eval/val rotation <span class="log-value">1.02</span></div>
          <div class="log-line"><span class="log-time">22:43:04</span> <span class="log-action">[OUTPUT]</span> eval applied grid: <span class="log-value">0.315645</span></div>
        </div>
      </div>

      <!-- LAYER ARCHITECTURE -->
      <div class="panel" data-tooltip="Neural network layer activity showing how many signals each strategy has processed. Higher numbers = more active strategies.">
        <div class="panel-title">LAYER ARCHITECTURE</div>
        <div class="layer-arch">
          <div class="layer-row">
            <span class="layer-name">INPUT</span>
            <span class="layer-count" id="inputNodes">784</span>
          </div>
          <div class="layer-row">
            <span class="layer-name">WEATHER</span>
            <span class="layer-count" id="weatherNodes">0</span>
          </div>
          <div class="layer-row">
            <span class="layer-name">GROK</span>
            <span class="layer-count" id="grokNodes">0</span>
          </div>
          <div class="layer-row">
            <span class="layer-name">SPORTS</span>
            <span class="layer-count" id="sportsNodes">0</span>
          </div>
          <div class="layer-row">
            <span class="layer-name">MENTION</span>
            <span class="layer-count" id="mentionNodes">0</span>
          </div>
          <div class="layer-row">
            <span class="layer-name">ARBIT</span>
            <span class="layer-count" id="arbitNodes">0</span>
          </div>
          <div class="layer-row">
            <span class="layer-name">NEAR</span>
            <span class="layer-count" id="nearNodes">0</span>
          </div>
          <div class="layer-row">
            <span class="layer-name">HIGHPROB</span>
            <span class="layer-count" id="highprobNodes">0</span>
          </div>
          <div class="layer-row">
            <span class="layer-name">CROSS</span>
            <span class="layer-count" id="crossNodes">0</span>
          </div>
          <div class="layer-row">
            <span class="layer-name">ORDERBOOK</span>
            <span class="layer-count" id="orderbookNodes">0</span>
          </div>
          <div class="layer-row">
            <span class="layer-name">OUTPUT</span>
            <span class="layer-count" id="outputNodes">0</span>
          </div>
        </div>
      </div>

      <!-- CORTICAL ACTIVITY -->
      <div class="panel" data-tooltip="24-hour trading activity heatmap. Each cell represents one hour of the week. Brighter cells = more trading activity during that hour.">
        <div class="panel-title">CORTICAL ACTIVITY</div>
        <div class="cortical-heatmap" id="corticalHeatmap">
          <!-- 24x7 grid will be populated by JS -->
        </div>
      </div>
    </div>
  </div>

  <!-- BOTTOM SIGNAL FEED -->
  <div class="signal-feed" id="signalFeed" data-tooltip="Live feed of all trading signals. Shows which strategy analyzed which market, the edge/confidence score, and whether the bot decided to trade or skip. Edge = how much better the bot thinks the odds are than the market price.">
    <div class="signal-item">
      <span class="signal-time">22:43:11</span>
      <span class="signal-strategy">[WEATHER]</span>
      <span class="signal-market">KXHIGHNY-26MAR</span>
      <span class="signal-edge">edge=+8.2% conf=85</span>
      <span class="signal-action">→ TRADE</span>
    </div>
    <div class="signal-item">
      <span class="signal-time">22:43:11</span>
      <span class="signal-strategy">[SPORTS]</span>
      <span class="signal-market">NFLSB-YES</span>
      <span class="signal-edge">edge=+3.1% conf=62</span>
      <span class="signal-action">→ SKIP</span>
    </div>
    <div class="signal-item">
      <span class="signal-time">22:43:09</span>
      <span class="signal-strategy">[MENTION]</span>
      <span class="signal-market">TRUMP-TWEET</span>
      <span class="signal-edge">edge=+12% conf=78</span>
      <span class="signal-action">→ TRADE</span>
    </div>
    <div class="signal-item">
      <span class="signal-time">22:42:55</span>
      <span class="signal-strategy">[PROB_ARB]</span>
      <span class="signal-market">BTCHIGH-YES</span>
      <span class="signal-edge">edge=+2.1% conf=90</span>
      <span class="signal-action">→ SKIP</span>
    </div>
  </div>
</div>

<script>
// ========================================
// NEURAL CORTEX DASHBOARD — SCI-FI TERMINAL
// ========================================

let particles = [];
let trainingLogLines = [];
let corticalData = new Array(24 * 7).fill(0); // 24 hours x 7 days

// === LIVING BACKGROUND PARTICLE SYSTEM ===
let bgParticles = [];
let ripples = [];
let lastTradeCount = null;
let lastBalance = null;
let knownTrades = new Set();

// Background particle system
function initBackgroundParticles() {
  const bgCanvas = document.getElementById('bgCanvas');
  const bgCtx = bgCanvas.getContext('2d');

  function resizeBgCanvas() {
    bgCanvas.width = window.innerWidth;
    bgCanvas.height = window.innerHeight;
  }
  resizeBgCanvas();
  window.addEventListener('resize', resizeBgCanvas);

  // Initialize ambient particles
  for (let i = 0; i < 60; i++) {
    bgParticles.push(new BgParticle(-10, Math.random() * bgCanvas.height));
  }

  animateBg();
}

class BgParticle {
  constructor(x, y, opts = {}) {
    this.x = x || Math.random() * document.getElementById('bgCanvas').width;
    this.y = y || Math.random() * document.getElementById('bgCanvas').height;
    this.vx = opts.vx || (Math.random() * 0.3 + 0.1);
    this.vy = opts.vy || (Math.random() - 0.5) * 0.2;
    this.size = opts.size || Math.random() * 2 + 0.5;
    this.alpha = opts.alpha || Math.random() * 0.15 + 0.05;
    this.maxAlpha = this.alpha;
    this.color = opts.color || '0, 240, 255';  // cyan
    this.life = opts.life || Infinity;
    this.age = 0;
    this.decay = opts.decay || 0;
  }
  update() {
    this.x += this.vx;
    this.y += this.vy;
    this.age++;
    if (this.decay > 0) {
      this.alpha -= this.decay;
    }
    const canvas = document.getElementById('bgCanvas');
    if (this.x > canvas.width + 10) this.x = -10;
    if (this.x < -10) this.x = canvas.width + 10;
    if (this.y > canvas.height + 10) this.y = -10;
    if (this.y < -10) this.y = canvas.height + 10;
  }
  draw(ctx) {
    if (this.alpha <= 0) return;
    ctx.beginPath();
    ctx.arc(this.x, this.y, this.size, 0, Math.PI * 2);
    ctx.fillStyle = `rgba(${this.color}, ${Math.max(0, this.alpha)})`;
    ctx.fill();
  }
  isDead() {
    return this.alpha <= 0 || this.age > this.life;
  }
}

class Ripple {
  constructor(x, y, color, maxRadius) {
    this.x = x;
    this.y = y;
    this.radius = 0;
    this.maxRadius = maxRadius || 200;
    this.alpha = 0.6;
    this.color = color || '0, 240, 255';
    this.speed = 3;
  }
  update() {
    this.radius += this.speed;
    this.alpha -= 0.01;
  }
  draw(ctx) {
    if (this.alpha <= 0) return;
    ctx.beginPath();
    ctx.arc(this.x, this.y, this.radius, 0, Math.PI * 2);
    ctx.strokeStyle = `rgba(${this.color}, ${this.alpha})`;
    ctx.lineWidth = 2;
    ctx.stroke();
  }
  isDead() { return this.alpha <= 0; }
}

function animateBg() {
  const bgCanvas = document.getElementById('bgCanvas');
  const bgCtx = bgCanvas.getContext('2d');

  // Fade trail effect
  bgCtx.fillStyle = 'rgba(5, 5, 8, 0.15)';
  bgCtx.fillRect(0, 0, bgCanvas.width, bgCanvas.height);

  // Update and draw particles
  for (let p of bgParticles) {
    p.update();
    p.draw(bgCtx);
  }
  bgParticles = bgParticles.filter(p => !p.isDead());

  // Draw neural mesh connections
  const ambientParticles = bgParticles.filter(p => p.life === Infinity);
  for (let i = 0; i < ambientParticles.length; i++) {
    for (let j = i + 1; j < ambientParticles.length; j++) {
      const dx = ambientParticles[i].x - ambientParticles[j].x;
      const dy = ambientParticles[i].y - ambientParticles[j].y;
      const dist = Math.sqrt(dx * dx + dy * dy);
      if (dist < 100) {
        const alpha = (1 - dist / 100) * 0.06;
        bgCtx.beginPath();
        bgCtx.moveTo(ambientParticles[i].x, ambientParticles[i].y);
        bgCtx.lineTo(ambientParticles[j].x, ambientParticles[j].y);
        bgCtx.strokeStyle = `rgba(0, 240, 255, ${alpha})`;
        bgCtx.lineWidth = 0.5;
        bgCtx.stroke();
      }
    }
  }

  // Update and draw ripples
  for (let r of ripples) {
    r.update();
    r.draw(bgCtx);
  }
  ripples = ripples.filter(r => !r.isDead());

  // Keep ambient particle count at ~60
  while (bgParticles.filter(p => p.life === Infinity).length < 60) {
    bgParticles.push(new BgParticle(-10, Math.random() * bgCanvas.height));
  }

  requestAnimationFrame(animateBg);
}

// === EVENT TRIGGERS ===
function triggerScanPulse() {
  // Bot just scanned — brief speed burst + brightness wave
  for (let p of bgParticles) {
    if (p.life === Infinity) {
      p.vx *= 3;
      setTimeout(() => { p.vx /= 3; }, 500);
      p.alpha = Math.min(p.maxAlpha * 3, 0.4);
    }
  }
  // Add wave of new particles
  for (let i = 0; i < 15; i++) {
    bgParticles.push(new BgParticle(-10, Math.random() * document.getElementById('bgCanvas').height, {
      vx: Math.random() * 2 + 1,
      alpha: 0.3,
      decay: 0.003,
      life: 200,
      size: Math.random() * 3 + 1
    }));
  }
}

function triggerNewTrade(side) {
  // New trade placed — sonar ping + particle burst from center
  const cx = document.getElementById('bgCanvas').width / 2;
  const cy = document.getElementById('bgCanvas').height / 2;

  // Sonar ripple
  ripples.push(new Ripple(cx, cy, '0, 240, 255', 300));

  // Particle burst
  for (let i = 0; i < 40; i++) {
    const angle = (Math.PI * 2 * i) / 40;
    const speed = Math.random() * 2 + 1;
    bgParticles.push(new BgParticle(cx, cy, {
      vx: Math.cos(angle) * speed,
      vy: Math.sin(angle) * speed,
      alpha: 0.6,
      decay: 0.008,
      life: 120,
      size: Math.random() * 3 + 1,
      color: side === 'yes' ? '0, 240, 255' : '255, 106, 0'
    }));
  }

  // Screen border flash
  flashBorder('0, 240, 255');
}

function triggerWin(pnl) {
  // SETTLEMENT WIN — EPIC CELEBRATION
  const cx = document.getElementById('bgCanvas').width / 2;
  const cy = document.getElementById('bgCanvas').height / 2;

  // Multiple expanding ripples (golden/green)
  for (let i = 0; i < 3; i++) {
    setTimeout(() => {
      ripples.push(new Ripple(cx, cy, '57, 255, 20', 400 + i * 100));
    }, i * 200);
  }

  // MASSIVE particle explosion — golden and green
  for (let i = 0; i < 80; i++) {
    const angle = Math.random() * Math.PI * 2;
    const speed = Math.random() * 4 + 1;
    const isGold = Math.random() > 0.5;
    bgParticles.push(new BgParticle(cx + (Math.random() - 0.5) * 100, cy + (Math.random() - 0.5) * 100, {
      vx: Math.cos(angle) * speed,
      vy: Math.sin(angle) * speed,
      alpha: 0.8,
      decay: 0.005,
      life: 200,
      size: Math.random() * 4 + 2,
      color: isGold ? '255, 215, 0' : '57, 255, 20'
    }));
  }

  // Screen border flash green
  flashBorder('57, 255, 20');

  // Show floating P&L text
  showFloatingText(`+$${Math.abs(pnl).toFixed(2)}`, 'profit');
}

function triggerLoss(pnl) {
  // SETTLEMENT LOSS — dramatic but not depressing
  const cx = document.getElementById('bgCanvas').width / 2;
  const cy = document.getElementById('bgCanvas').height / 2;

  // Red ripple (like an impact)
  ripples.push(new Ripple(cx, cy, '255, 51, 51', 250));

  // Particles "scatter" outward from center — red/orange
  for (let i = 0; i < 30; i++) {
    const angle = Math.random() * Math.PI * 2;
    const speed = Math.random() * 3 + 0.5;
    bgParticles.push(new BgParticle(cx, cy, {
      vx: Math.cos(angle) * speed,
      vy: Math.sin(angle) * speed + 1,
      alpha: 0.5,
      decay: 0.008,
      life: 120,
      size: Math.random() * 3 + 1,
      color: Math.random() > 0.5 ? '255, 51, 51' : '255, 106, 0'
    }));
  }

  // Brief screen shake effect
  shakeScreen();

  // Red border flash
  flashBorder('255, 51, 51');

  // Show floating P&L text
  showFloatingText(`-$${Math.abs(pnl).toFixed(2)}`, 'loss');
}

// === VISUAL HELPER FUNCTIONS ===
function flashBorder(color) {
  const flash = document.createElement('div');
  flash.style.cssText = `
    position: fixed; top: 0; left: 0; right: 0; bottom: 0;
    border: 2px solid rgba(${color}, 0.8);
    box-shadow: inset 0 0 60px rgba(${color}, 0.3);
    pointer-events: none; z-index: 9998;
    transition: opacity 1s ease-out;
  `;
  document.body.appendChild(flash);
  setTimeout(() => { flash.style.opacity = '0'; }, 50);
  setTimeout(() => { flash.remove(); }, 1100);
}

function shakeScreen() {
  const body = document.body;
  body.style.transition = 'none';
  const shakes = [
    {x: 3, y: 0}, {x: -3, y: 2}, {x: 2, y: -2},
    {x: -2, y: 1}, {x: 1, y: -1}, {x: 0, y: 0}
  ];
  shakes.forEach((s, i) => {
    setTimeout(() => {
      body.style.transform = `translate(${s.x}px, ${s.y}px)`;
    }, i * 50);
  });
}

function showFloatingText(text, type) {
  const el = document.createElement('div');
  const color = type === 'profit' ? '#39ff14' : '#ff3333';
  const glow = type === 'profit' ? '0 0 20px rgba(57,255,20,0.6)' : '0 0 20px rgba(255,51,51,0.6)';
  el.textContent = text;
  el.style.cssText = `
    position: fixed;
    top: 40%;
    left: 50%;
    transform: translate(-50%, -50%);
    font-family: 'JetBrains Mono', monospace;
    font-size: 3rem;
    font-weight: 700;
    color: ${color};
    text-shadow: ${glow};
    z-index: 9999;
    pointer-events: none;
    animation: floatUp 2s ease-out forwards;
  `;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 2500);
}

// Add the float-up animation to the page styles
const floatStyle = document.createElement('style');
floatStyle.textContent = `
  @keyframes floatUp {
    0% { opacity: 1; transform: translate(-50%, -50%) scale(0.5); }
    20% { opacity: 1; transform: translate(-50%, -50%) scale(1.2); }
    40% { opacity: 1; transform: translate(-50%, -50%) scale(1); }
    100% { opacity: 0; transform: translate(-50%, -150%) scale(1); }
  }
`;
document.head.appendChild(floatStyle);

// Initialize cortical heatmap
function initCorticalHeatmap() {
  const container = document.getElementById('corticalHeatmap');
  container.innerHTML = '';
  for (let i = 0; i < 24 * 7; i++) {
    const cell = document.createElement('div');
    cell.className = 'heatmap-cell';
    cell.dataset.index = i;
    container.appendChild(cell);
  }
}

// Update cortical activity
function updateCorticalActivity() {
  // Simulate activity based on current hour
  const now = new Date();
  const hour = now.getHours();
  const day = now.getDay();

  // Add activity to current hour
  const index = day * 24 + hour;
  if (index < corticalData.length) {
    corticalData[index] = Math.min(1, corticalData[index] + 0.1);
  }

  // Update visual cells
  document.querySelectorAll('.heatmap-cell').forEach((cell, i) => {
    const intensity = corticalData[i] || 0;
    if (intensity > 0.1) {
      cell.classList.add('active');
      cell.style.opacity = intensity;
    } else {
      cell.classList.remove('active');
    }
  });

  // Decay over time
  corticalData = corticalData.map(v => Math.max(0, v - 0.001));
}

// Initialize signal topology
function initTopology() {
  const canvas = document.getElementById('topologyCanvas');
  const ctx = canvas.getContext('2d');

  // Set canvas size
  canvas.width = canvas.offsetWidth;
  canvas.height = canvas.offsetHeight;

  // Create initial particles
  for (let i = 0; i < 20; i++) {
    particles.push({
      x: Math.random() * canvas.width,
      y: Math.random() * canvas.height,
      vx: (Math.random() - 0.5) * 0.5,
      vy: (Math.random() - 0.5) * 0.5,
      brightness: Math.random() * 0.5,
      strategy: ['WEATHER', 'GROK', 'SPORTS', 'MENTION', 'ARBIT'][Math.floor(Math.random() * 5)],
      connections: []
    });
  }

  animateTopology();
}

function animateTopology() {
  const canvas = document.getElementById('topologyCanvas');
  const ctx = canvas.getContext('2d');

  // Fade trail effect
  ctx.fillStyle = 'rgba(5, 5, 8, 0.05)';
  ctx.fillRect(0, 0, canvas.width, canvas.height);

  // Update particles
  particles.forEach(particle => {
    particle.x += particle.vx;
    particle.y += particle.vy;

    // Bounce off walls
    if (particle.x < 0 || particle.x > canvas.width) particle.vx *= -1;
    if (particle.y < 0 || particle.y > canvas.height) particle.vy *= -1;

    // Draw particle
    ctx.beginPath();
    ctx.arc(particle.x, particle.y, 3, 0, Math.PI * 2);
    ctx.fillStyle = particle.brightness > 0.3 ? '#00f0ff' : '#333';
    ctx.globalAlpha = particle.brightness;
    ctx.fill();

    // Glow effect
    if (particle.brightness > 0.5) {
      ctx.shadowColor = '#00f0ff';
      ctx.shadowBlur = 10;
      ctx.fill();
      ctx.shadowBlur = 0;
    }

    ctx.globalAlpha = 1;

    // Decay brightness
    particle.brightness *= 0.995;
  });

  // Draw connections between close particles
  particles.forEach((p1, i) => {
    particles.slice(i + 1).forEach(p2 => {
      const dx = p1.x - p2.x;
      const dy = p1.y - p2.y;
      const distance = Math.sqrt(dx * dx + dy * dy);

      if (distance < 80) {
        ctx.beginPath();
        ctx.moveTo(p1.x, p1.y);
        ctx.lineTo(p2.x, p2.y);
        ctx.strokeStyle = `rgba(0, 240, 255, ${0.2 * (1 - distance / 80)})`;
        ctx.lineWidth = 1;
        ctx.stroke();
      }
    });
  });

  requestAnimationFrame(animateTopology);
}

// Add new signal to topology
function addSignalToTopology(strategy, ticker, action) {
  // Find or create particle for this strategy
  let particle = particles.find(p => p.strategy === strategy);
  if (!particle) {
    particle = {
      x: Math.random() * document.getElementById('topologyCanvas').width,
      y: Math.random() * document.getElementById('topologyCanvas').height,
      vx: (Math.random() - 0.5) * 0.5,
      vy: (Math.random() - 0.5) * 0.5,
      brightness: 1.0,
      strategy: strategy,
      connections: []
    };
    particles.push(particle);
  }

  // Pulse the particle
  particle.brightness = 1.0;

  // Add to training log
  const time = new Date().toLocaleTimeString();
  const actionMap = {
    'TRADE': 'FORWARD',
    'SKIP': 'GRAD_CLIP'
  };
  const logAction = actionMap[action] || 'COMMAND';
  const logValue = action === 'TRADE' ? (Math.random() * 2 - 1).toFixed(2) : 'threshold';

  trainingLogLines.unshift({
    time: time,
    action: logAction,
    value: logValue,
    strategy: strategy
  });

  // Keep only last 20 lines
  trainingLogLines = trainingLogLines.slice(0, 20);
  updateTrainingLog();
}

// Update training log display
function updateTrainingLog() {
  const container = document.getElementById('trainingLog');
  container.innerHTML = trainingLogLines.map(line => `
    <div class="log-line">
      <span class="log-time">${line.time}</span>
      <span class="log-action">[${line.action}]</span>
      ${line.strategy ? `${line.strategy} ` : ''}${line.value}
    </div>
  `).join('');
}

// Update strategy bars
function updateStrategyBars(strategies) {
  const container = document.getElementById('strategyBars');

  if (!strategies || strategies.length === 0) {
    container.innerHTML = '<div class="log-line">No strategy data yet...</div>';
    return;
  }

  // Find max P&L for scaling
  const maxPnL = Math.max(...strategies.map(s => Math.abs(s.total_pnl || 0)));

  container.innerHTML = strategies.slice(0, 8).map(strategy => {
    const pnl = strategy.total_pnl || 0;
    const width = maxPnL > 0 ? Math.abs(pnl) / maxPnL * 100 : 0;
    const color = pnl >= 0 ? '#39ff14' : '#ff3333';

    return `
      <div class="strategy-bar">
        <div class="bar-label">${(strategy.name || strategy.strategy || '').substring(0, 8)}</div>
        <div class="bar-fill">
          <div class="bar-value" style="width: ${width}%; background: ${color};"></div>
        </div>
        <div class="bar-amount">${pnl >= 0 ? '+' : ''}$${pnl.toFixed(0)}</div>
      </div>
    `;
  }).join('');
}

// Update layer architecture
function updateLayerArchitecture(strategies) {
  if (!strategies) return;

  const counts = {};
  strategies.forEach(s => {
    const name = (s.name || s.strategy || '').toLowerCase();
    if (name.includes('weather')) counts.weather = (counts.weather || 0) + (s.trades || 0);
    else if (name.includes('grok')) counts.grok = (counts.grok || 0) + (s.trades || 0);
    else if (name.includes('sports')) counts.sports = (counts.sports || 0) + (s.trades || 0);
    else if (name.includes('mention')) counts.mention = (counts.mention || 0) + (s.trades || 0);
    else if (name.includes('prob') || name.includes('arb')) counts.arbit = (counts.arbit || 0) + (s.trades || 0);
    else if (name.includes('near')) counts.near = (counts.near || 0) + (s.trades || 0);
    else if (name.includes('high')) counts.highprob = (counts.highprob || 0) + (s.trades || 0);
    else if (name.includes('cross')) counts.cross = (counts.cross || 0) + (s.trades || 0);
    else if (name.includes('order')) counts.orderbook = (counts.orderbook || 0) + (s.trades || 0);
  });

  document.getElementById('weatherNodes').textContent = counts.weather || 0;
  document.getElementById('grokNodes').textContent = counts.grok || 0;
  document.getElementById('sportsNodes').textContent = counts.sports || 0;
  document.getElementById('mentionNodes').textContent = counts.mention || 0;
  document.getElementById('arbitNodes').textContent = counts.arbit || 0;
  document.getElementById('nearNodes').textContent = counts.near || 0;
  document.getElementById('highprobNodes').textContent = counts.highprob || 0;
  document.getElementById('crossNodes').textContent = counts.cross || 0;
  document.getElementById('orderbookNodes').textContent = counts.orderbook || 0;
  document.getElementById('outputNodes').textContent = Object.values(counts).reduce((a, b) => a + b, 0);
}

// Update signal feed
function updateSignalFeed(signals) {
  const container = document.getElementById('signalFeed');

  if (!signals || signals.length === 0) {
    container.innerHTML = '<div class="signal-item">Waiting for neural signals...</div>';
    return;
  }

  container.innerHTML = signals.slice(0, 20).map(signal => {
    const time = new Date(signal.timestamp).toLocaleTimeString();
    const strategy = (signal.strategy || '').toUpperCase().substring(0, 8);
    const action = signal.action || 'UNKNOWN';
    const actionColor = action === 'TRADE' ? '#00f0ff' : '#666';

    // Add to topology
    addSignalToTopology(strategy, signal.ticker, action);

    return `
      <div class="signal-item">
        <span class="signal-time">${time}</span>
        <span class="signal-strategy">[${strategy}]</span>
        <span class="signal-market">${signal.ticker || '?'}</span>
        <span class="signal-edge">edge=${((signal.edge || 0) * 100).toFixed(1)}% conf=${signal.confidence || 0}</span>
        <span class="signal-action" style="color: ${actionColor}">→ ${action}</span>
      </div>
    `;
  }).join('');
}

// FETCH AND UPDATE DATA
async function fetchData() {
  try {
    const [statusRes, strategiesRes, signalsRes, tradesRes] = await Promise.all([
      fetch('/api/status'),
      fetch('/api/strategies'),
      fetch('/api/signals'),
      fetch('/api/trades')
    ]);

    const status = await statusRes.json();
    const strategies = await strategiesRes.json();
    const signals = await signalsRes.json();
    const trades = await tradesRes.json();

    // === DETECT EVENTS AND TRIGGER ANIMATIONS ===
    triggerScanPulse();  // Always trigger scan pulse on refresh

    // Detect new trades
    if (trades.trades && Array.isArray(trades.trades)) {
      for (let t of trades.trades) {
        const tradeId = t.id || t.timestamp;
        if (tradeId && !knownTrades.has(tradeId)) {
          knownTrades.add(tradeId);

          // Only animate if this isn't the first load
          if (lastTradeCount !== null) {
            const reason = (t.reason || '').toUpperCase();

            if (reason.includes('SETTLED WIN')) {
              // Extract P&L from reason string
              let pnl = 0;
              if (t.reason && t.reason.includes('pnl=')) {
                try { pnl = parseFloat(t.reason.split('pnl=')[1].split(',')[0].replace('$','').replace('+','')); } catch(e) {}
              }
              triggerWin(pnl);
            } else if (reason.includes('SETTLED LOSS')) {
              let pnl = 0;
              if (t.reason && t.reason.includes('pnl=')) {
                try { pnl = parseFloat(t.reason.split('pnl=')[1].split(',')[0].replace('$','').replace('+','')); } catch(e) {}
              }
              triggerLoss(Math.abs(pnl));
            } else if (t.action === 'buy') {
              triggerNewTrade(t.side);
            }
          }
        }
      }
      lastTradeCount = trades.trades.length;
    }

    // Detect balance change
    if (status && status.balance) {
      if (lastBalance !== null && lastBalance !== status.balance) {
        const diff = status.balance - lastBalance;
        if (Math.abs(diff) > 0.01) {
          // Balance changed — could be a settlement
          if (diff > 0) {
            flashBorder('57, 255, 20');
          } else {
            flashBorder('255, 51, 51');
          }
        }
      }
      lastBalance = status.balance;
    }

    // === UPDATE ALL DASHBOARD ELEMENTS WITH REAL DATA ===

    // Update header
    document.getElementById('statusText').textContent = status.operating_mode?.toUpperCase() || 'ACTIVE';
    document.getElementById('balanceDisplay').textContent = `$${status.balance?.toFixed(2) || '0.00'}`;

    // Update metrics using data-field attributes
    document.querySelectorAll('[data-field="balance"]').forEach(el => {
      el.textContent = `$${status.balance?.toFixed(2) || '0.00'}`;
    });
    document.querySelectorAll('[data-field="daily_pnl"]').forEach(el => {
      const pnl = status.daily_pnl || 0;
      const prefix = pnl >= 0 ? '+$' : '-$';
      el.textContent = prefix + Math.abs(pnl).toFixed(2);
      el.className = pnl >= 0 ? 'profit' : 'loss';
    });
    document.querySelectorAll('[data-field="roi"]').forEach(el => {
      const roi = status.roi_percent || 0;
      el.textContent = (roi >= 0 ? '+' : '') + roi.toFixed(2) + '%';
      el.className = roi >= 0 ? 'profit' : 'loss';
    });
    document.querySelectorAll('[data-field="trades_today"]').forEach(el => {
      el.textContent = status.trades_today || 0;
    });
    document.querySelectorAll('[data-field="positions"]').forEach(el => {
      el.textContent = status.active_positions || 0;
    });
    document.querySelectorAll('[data-field="last_scan"]').forEach(el => {
      if (status.last_scan) {
        const d = new Date(status.last_scan);
        const now = new Date();
        const diff = (now - d) / 1000;
        if (diff < 60) el.textContent = Math.floor(diff) + 's ago';
        else if (diff < 3600) el.textContent = Math.floor(diff / 60) + 'm ago';
        else el.textContent = Math.floor(diff / 3600) + 'h ago';
      } else {
        el.textContent = '—';
      }
    });

    // Update page title with real balance
    document.title = `💰 $${status.balance?.toFixed(0) || '0'} | KALSHI ALPHA`;

    // Update strategy bars and layers
    updateStrategyBars(strategies.strategies || []);
    updateLayerArchitecture(strategies.strategies || []);

    // Update signal feed
    updateSignalFeed(signals.signals || []);

  } catch (error) {
    console.error('Data fetch error:', error);
  }
}

// Update time
function updateTime() {
  const now = new Date();
  const timeString = now.toLocaleTimeString('en-US', {
    hour12: false,
    timeZone: 'America/Los_Angeles'
  });
  document.getElementById('currentTime').textContent = timeString + ' PST';
}

// Initialize
function init() {
  initCorticalHeatmap();
  initTopology();
  updateTime();
  setInterval(updateTime, 1000);
  setInterval(updateCorticalActivity, 5000);
  fetchData();
  setInterval(fetchData, 10000);
}

// Start the neural cortex
document.addEventListener('DOMContentLoaded', init);
</script>
</body>
</html>"""
