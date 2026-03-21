"""
Kalshi Trading Bot Dashboard
Serves a web interface for monitoring and controlling the bot.
"""

import os
import json
import asyncio
from datetime import datetime, timedelta
from collections import defaultdict
from flask import Flask, render_template_string, request, jsonify
from flask_cors import CORS

from bot import KalshiBot
from utils.supabase_db import SupabaseDB
from utils.logger import setup_logger

logger = setup_logger('dashboard')

app = Flask(__name__)
CORS(app)

# Global bot instance
bot_instance = None

# HTML template with embedded CSS/JS (adapted from Polymarket dashboard)
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Kalshi Trading Bot Dashboard</title>
    <style>
        /* ─── Reset & Base ─────────────────────────────────────────────── */
        *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

        :root {
            --bg: #080a12;
            --bg-card: rgba(22, 25, 38, 0.75);
            --bg-panel: rgba(18, 21, 32, 0.8);
            --bg-table-row: rgba(26, 29, 42, 0.6);
            --bg-hover: rgba(40, 44, 64, 0.6);
            --bg-input: rgba(26, 29, 42, 0.8);
            --border: rgba(255, 255, 255, 0.06);
            --border-focus: #4c8dff;
            --text: #e8eaf4;
            --text-dim: #9499b3;
            --text-muted: #5a5f78;
            --accent-blue: #4c8dff;
            --accent-green: #00e68a;
            --accent-red: #ff4d6a;
            --accent-orange: #ff9f43;
            --accent-purple: #a855f7;
            --accent-teal: #14b8a6;
            --accent-pink: #f472b6;
            --accent-yellow: #fbbf24;
            --glow-blue: rgba(76, 141, 255, 0.15);
            --glow-green: rgba(0, 230, 138, 0.15);
            --glow-purple: rgba(168, 85, 247, 0.15);
            --glass: rgba(255, 255, 255, 0.03);
            --glass-border: rgba(255, 255, 255, 0.08);
            --font: 'Inter', 'SF Pro Display', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            --font-mono: 'JetBrains Mono', 'SF Mono', 'Fira Code', 'Consolas', monospace;
            --radius: 16px;
            --radius-sm: 10px;
            --shadow-sm: 0 2px 8px rgba(0,0,0,0.2);
            --shadow-md: 0 8px 32px rgba(0,0,0,0.3);
            --shadow-lg: 0 16px 48px rgba(0,0,0,0.4);
            --shadow-glow-blue: 0 0 20px rgba(76,141,255,0.2);
            --shadow-glow-green: 0 0 20px rgba(0,230,138,0.2);
        }

        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap');

        body {
            font-family: var(--font);
            background: var(--bg);
            background-image:
                radial-gradient(ellipse 80% 50% at 50% -20%, rgba(76,141,255,0.08), transparent),
                radial-gradient(ellipse 60% 40% at 80% 100%, rgba(168,85,247,0.05), transparent);
            color: var(--text);
            line-height: 1.6;
            min-height: 100vh;
            padding: 0 24px 40px;
            max-width: 1600px;
            margin: 0 auto;
            -webkit-font-smoothing: antialiased;
            -moz-osx-font-smoothing: grayscale;
        }

        /* ─── Scrollbar ────────────────────────────────────────────────── */
        ::-webkit-scrollbar { width: 6px; height: 6px; }
        ::-webkit-scrollbar-track { background: transparent; }
        ::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.08); border-radius: 3px; }
        ::-webkit-scrollbar-thumb:hover { background: rgba(255,255,255,0.15); }

        /* ─── Header ───────────────────────────────────────────────────── */
        header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 16px 0;
            border-bottom: 1px solid var(--border);
            margin-bottom: 24px;
            position: sticky;
            top: 0;
            background: rgba(8, 10, 18, 0.85);
            backdrop-filter: blur(20px);
            -webkit-backdrop-filter: blur(20px);
            z-index: 100;
        }
        header h1 { font-size: 1.4rem; font-weight: 800; letter-spacing: -0.03em; background: linear-gradient(135deg, #fff 0%, #94a3d0 100%); -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text; }
        .subtitle { color: var(--text-muted); font-size: 0.8rem; margin-left: 12px; font-weight: 500; letter-spacing: 0.02em; }
        .header-right { display: flex; align-items: center; gap: 10px; }
        .last-updated { font-size: 0.72rem; color: var(--text-muted); font-weight: 500; }

        /* ─── Tab Navigation ───────────────────────────────────────────── */
        .tab-nav {
            display: flex;
            gap: 3px;
            padding: 3px;
            background: var(--bg-card);
            backdrop-filter: blur(12px);
            -webkit-backdrop-filter: blur(12px);
            border-radius: var(--radius);
            margin-bottom: 24px;
            border: 1px solid var(--glass-border);
            position: sticky;
            top: 62px;
            z-index: 99;
            overflow-x: auto;
            -webkit-overflow-scrolling: touch;
            box-shadow: var(--shadow-sm);
        }

        .tab-btn {
            display: flex;
            align-items: center;
            gap: 7px;
            padding: 10px 18px;
            border: none;
            border-radius: var(--radius-sm);
            background: transparent;
            color: var(--text-muted);
            font-family: var(--font);
            font-size: 0.82rem;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.25s cubic-bezier(0.4, 0, 0.2, 1);
            white-space: nowrap;
            flex: 1;
            justify-content: center;
            position: relative;
        }

        .tab-btn:hover {
            background: var(--bg-hover);
            color: var(--text);
        }

        .tab-btn.active {
            background: linear-gradient(135deg, var(--accent-blue), #3b6fdf);
            color: #fff;
            box-shadow: 0 2px 12px rgba(76, 141, 255, 0.35), inset 0 1px 0 rgba(255,255,255,0.1);
            text-shadow: 0 1px 2px rgba(0,0,0,0.2);
        }

        .tab-icon { font-size: 0.95rem; }
        .tab-label { letter-spacing: -0.01em; }

        /* Tab content visibility */
        .tab-content {
            display: none;
            animation: tabFadeIn 0.35s cubic-bezier(0.4, 0, 0.2, 1);
        }

        .tab-content.active {
            display: block;
        }

        @keyframes tabFadeIn {
            from { opacity: 0; transform: translateY(8px) scale(0.998); }
            to { opacity: 1; transform: translateY(0) scale(1); }
        }

        .badge {
            font-size: 0.65rem;
            font-weight: 700;
            letter-spacing: 0.06em;
            padding: 4px 10px;
            border-radius: 6px;
            text-transform: uppercase;
            backdrop-filter: blur(4px);
        }
        .badge-paper { background: linear-gradient(135deg, var(--accent-orange), #e68a30); color: #000; }
        .badge-live { background: linear-gradient(135deg, var(--accent-red), #cc3050); color: #fff; animation: pulse-live 2s infinite; box-shadow: 0 0 12px rgba(255,77,106,0.4); }
        .badge-ok { background: linear-gradient(135deg, var(--accent-green), #00b875); color: #000; }
        .badge-danger { background: linear-gradient(135deg, var(--accent-red), #cc3050); color: #fff; }

        @keyframes pulse-live {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.6; }
        }

        /* ─── Card Grid ────────────────────────────────────────────────── */
        .card-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 16px;
            margin-bottom: 24px;
        }

        .card {
            background: var(--glass);
            backdrop-filter: blur(12px);
            -webkit-backdrop-filter: blur(12px);
            border-radius: var(--radius);
            padding: 20px;
            border: 1px solid var(--glass-border);
            border-left: 4px solid var(--border);
            transition: var(--transition-normal);
            position: relative;
            overflow: hidden;
        }
        .card::before {
            content: '';
            position: absolute;
            top: 0;
            left: 0;
            right: 0;
            height: 1px;
            background: linear-gradient(90deg, transparent, rgba(255,255,255,0.08), transparent);
        }
        .card:hover {
            transform: translateY(-3px);
            box-shadow: var(--shadow-lg);
            border-color: rgba(255,255,255,0.1);
        }

        .card.accent-blue   { border-left-color: var(--accent-blue); }
        .card.accent-green  { border-left-color: var(--accent-green); }
        .card.accent-purple { border-left-color: var(--accent-purple); }
        .card.accent-orange { border-left-color: var(--accent-orange); }
        .card.accent-teal   { border-left-color: var(--accent-teal); }
        .card.accent-pink   { border-left-color: var(--accent-pink); }

        .card.accent-blue:hover   { box-shadow: 0 8px 32px rgba(76,141,255,0.15); }
        .card.accent-green:hover  { box-shadow: 0 8px 32px rgba(0,214,143,0.15); }
        .card.accent-purple:hover { box-shadow: 0 8px 32px rgba(168,85,247,0.15); }
        .card.accent-orange:hover { box-shadow: 0 4px 20px rgba(255,159,67,0.15); }

        .card-label { font-size: 0.72rem; color: var(--text-dim); text-transform: uppercase; letter-spacing: 0.08em; font-weight: 600; }
        .card-value { font-size: 1.8rem; font-weight: 700; margin: 6px 0; font-family: var(--font-mono); letter-spacing: -0.02em; background: linear-gradient(135deg, var(--text), var(--text-dim)); -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text; }
        .card-sub { font-size: 0.78rem; color: var(--text-muted); }

        /* ─── Panel ────────────────────────────────────────────────────── */
        .panel {
            background: var(--glass);
            backdrop-filter: blur(12px);
            -webkit-backdrop-filter: blur(12px);
            border-radius: var(--radius);
            padding: 24px;
            margin-bottom: 20px;
            border: 1px solid var(--glass-border);
            transition: var(--transition-normal);
        }
        .panel:hover {
            border-color: rgba(255,255,255,0.08);
        }
        .panel h2 { font-size: 1.1rem; font-weight: 700; margin-bottom: 16px; }

        .panel-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 16px;
            flex-wrap: wrap;
            gap: 12px;
        }
        .panel-header h2 { margin-bottom: 0; }
        .panel-tools {
            display: flex;
            align-items: center;
            gap: 8px;
        }

        /* ─── Tables ───────────────────────────────────────────────────── */
        .table-wrap { overflow-x: auto; border-radius: var(--radius-sm); }

        table {
            width: 100%;
            border-collapse: collapse;
            font-size: 0.82rem;
        }
        thead th {
            text-align: left;
            font-size: 0.7rem;
            text-transform: uppercase;
            letter-spacing: 0.06em;
            color: var(--text-dim);
            padding: 12px 14px;
            border-bottom: 1px solid var(--glass-border);
            font-weight: 700;
            white-space: nowrap;
            background: rgba(15,17,23,0.3);
        }
        tbody td {
            padding: 11px 14px;
            border-bottom: 1px solid rgba(255,255,255,0.03);
            vertical-align: middle;
        }
        tbody tr {
            transition: var(--transition-fast);
        }
        tbody tr:hover {
            background: rgba(76,141,255,0.04);
        }

        .empty-state {
            text-align: center;
            color: var(--text-muted);
            padding: 40px 12px !important;
            font-style: italic;
            font-size: 0.85rem;
        }

        /* ─── Decision/Status Pills ────────────────────────────────────── */
        .pill {
            display: inline-block;
            font-size: 0.68rem;
            font-weight: 700;
            padding: 3px 8px;
            border-radius: 4px;
            text-transform: uppercase;
            letter-spacing: 0.04em;
        }
        .pill-trade  { background: rgba(0,214,143,0.15); color: var(--accent-green); }
        .pill-no-trade { background: rgba(255,77,106,0.12); color: var(--accent-red); }
        .pill-filled { background: rgba(76,141,255,0.15); color: var(--accent-blue); }
        .pill-dry    { background: rgba(255,159,67,0.15); color: var(--accent-orange); }
        .pill-buy    { background: rgba(0,214,143,0.15); color: var(--accent-green); }
        .pill-sell   { background: rgba(255,77,106,0.12); color: var(--accent-red); }

        .pnl-positive { color: var(--accent-green); }
        .pnl-negative { color: var(--accent-red); }
        .pnl-zero    { color: var(--text-muted); }

        /* ─── Buttons ──────────────────────────────────────────────────── */
        .btn {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            gap: 6px;
            font-size: 0.72rem;
            font-weight: 700;
            padding: 8px 16px;
            border-radius: 8px;
            border: none;
            cursor: pointer;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            transition: var(--transition-normal);
            position: relative;
            overflow: hidden;
        }
        .btn::after {
            content: '';
            position: absolute;
            inset: 0;
            background: linear-gradient(135deg, rgba(255,255,255,0.1), transparent);
            opacity: 0;
            transition: opacity 0.2s;
        }
        .btn:hover::after { opacity: 1; }
        .btn:hover { transform: translateY(-1px); box-shadow: var(--shadow-md); }
        .btn:active { transform: translateY(0); box-shadow: none; }
        .btn-danger { background: linear-gradient(135deg, var(--accent-red), #cc3050); color: #fff; }
        .btn-danger:hover { box-shadow: 0 4px 20px rgba(255,77,106,0.3); }
        .btn-ok     { background: linear-gradient(135deg, var(--accent-green), #00b875); color: #000; }
        .btn-ok:hover { box-shadow: 0 4px 20px rgba(0,214,143,0.3); }
        .btn-save   { background: linear-gradient(135deg, var(--accent-blue), #3570d9); color: #fff; }
        .btn-save:hover { box-shadow: 0 4px 20px rgba(76,141,255,0.3); }
        .btn-muted  { background: var(--glass); color: var(--text-dim); border: 1px solid var(--glass-border); backdrop-filter: blur(8px); }
        .btn-muted:hover { border-color: rgba(255,255,255,0.15); color: var(--text); }
        .btn-export { background: var(--glass); color: var(--accent-teal); border: 1px solid var(--glass-border); font-size: 0.7rem; padding: 6px 12px; backdrop-filter: blur(8px); }
        .btn-export:hover { border-color: var(--accent-teal); box-shadow: 0 4px 16px rgba(0,210,211,0.15); }
        .btn-sm { font-size: 0.68rem; padding: 5px 12px; }

        /* ─── Toast Notifications ──────────────────────────────────────── */
        #toast-container {
            position: fixed;
            top: 20px;
            right: 20px;
            z-index: 10000;
            display: flex;
            flex-direction: column;
            gap: 10px;
            pointer-events: none;
        }

        .toast {
            display: flex;
            align-items: center;
            gap: 12px;
            padding: 14px 22px;
            border-radius: var(--radius);
            font-size: 0.82rem;
            font-weight: 600;
            color: var(--text);
            background: var(--glass);
            backdrop-filter: blur(20px);
            -webkit-backdrop-filter: blur(20px);
            border: 1px solid var(--glass-border);
            box-shadow: var(--shadow-lg);
            pointer-events: auto;
            opacity: 0;
            transform: translateX(100%) scale(0.95);
            transition: opacity 0.35s cubic-bezier(0.4, 0, 0.2, 1), transform 0.35s cubic-bezier(0.4, 0, 0.2, 1);
            max-width: 400px;
        }
        .toast-show {
            opacity: 1;
            transform: translateX(0) scale(1);
        }
        .toast-hide {
            opacity: 0;
            transform: translateX(100%) scale(0.95);
        }
        .toast-icon { font-size: 1.2rem; }
        .toast-msg { flex: 1; }

        .toast-success { border-left: 4px solid var(--accent-green); box-shadow: var(--shadow-lg), inset 0 0 20px rgba(0,214,143,0.03); }
        .toast-error   { border-left: 4px solid var(--accent-red); box-shadow: var(--shadow-lg), inset 0 0 20px rgba(255,51,51,0.03); }
        .toast-info    { border-left: 4px solid var(--accent-blue); box-shadow: var(--shadow-lg), inset 0 0 20px rgba(76,141,255,0.03); }
        .toast-warning { border-left: 4px solid var(--accent-orange); box-shadow: var(--shadow-lg), inset 0 0 20px rgba(255,159,67,0.03); }

        /* ─── Confirmation Modal ──────────────────────────────────────── */
        .modal-overlay {
            position: fixed;
            inset: 0;
            background: rgba(0,0,0,0.7);
            z-index: 9999;
            display: flex;
            align-items: center;
            justify-content: center;
            backdrop-filter: blur(8px);
            -webkit-backdrop-filter: blur(8px);
            animation: modalOverlayIn 0.25s ease-out;
        }
        @keyframes modalOverlayIn {
            from { opacity: 0; }
            to { opacity: 1; }
        }
        .modal-box {
            background: var(--glass);
            backdrop-filter: blur(24px);
            -webkit-backdrop-filter: blur(24px);
            border: 1px solid var(--glass-border);
            border-radius: 16px;
            padding: 32px;
            max-width: 440px;
            width: 90%;
            box-shadow: 0 24px 64px rgba(0,0,0,0.5), 0 0 0 1px rgba(255,255,255,0.05);
            animation: modalBoxIn 0.3s cubic-bezier(0.34, 1.56, 0.64, 1);
        }
        @keyframes modalBoxIn {
            from { opacity: 0; transform: scale(0.9) translateY(20px); }
            to { opacity: 1; transform: scale(1) translateY(0); }
        }
        .modal-box h3 {
            font-size: 1.1rem;
            font-weight: 700;
            margin-bottom: 12px;
        }
        .modal-box p {
            color: var(--text-dim);
            font-size: 0.88rem;
            margin-bottom: 24px;
            line-height: 1.5;
        }
        .modal-actions {
            display: flex;
            justify-content: flex-end;
            gap: 10px;
        }

        /* ─── Footer ───────────────────────────────────────────────────── */
        footer {
            display: flex;
            justify-content: space-between;
            padding: 20px 0;
            border-top: 1px solid var(--glass-border);
            margin-top: 20px;
            font-size: 0.75rem;
            color: var(--text-muted);
        }

        /* ─── Responsive ── */
        @media (max-width: 768px) {
            body { padding: 0 12px 24px; }
            .card-grid { grid-template-columns: repeat(2, 1fr); }
            .panel-header { flex-direction: column; align-items: flex-start; }
            .panel-tools { width: 100%; }
            .tab-nav { top: 0; }
            .tab-btn { padding: 8px 12px; font-size: 0.75rem; }
        }
    </style>
</head>
<body>
    <header>
        <div>
            <h1>Kalshi Trading Bot</h1>
            <span class="subtitle">AI-Powered Market Prediction</span>
        </div>
        <div class="header-right">
            <span class="badge" id="mode-badge">LOADING</span>
            <span class="last-updated" id="last-updated">Loading...</span>
        </div>
    </header>

    <nav class="tab-nav">
        <button class="tab-btn active" data-tab="overview" onclick="switchTab('overview')">
            <span class="tab-icon">📊</span>
            <span class="tab-label">Overview</span>
        </button>
        <button class="tab-btn" data-tab="trading" onclick="switchTab('trading')">
            <span class="tab-icon">📈</span>
            <span class="tab-label">Trading</span>
        </button>
        <button class="tab-btn" data-tab="learning" onclick="switchTab('learning')">
            <span class="tab-icon">🧠</span>
            <span class="tab-label">Learning Lab</span>
        </button>
        <button class="tab-btn" data-tab="admin" onclick="switchTab('admin')">
            <span class="tab-icon">⚙️</span>
            <span class="tab-label">Admin</span>
        </button>
    </nav>

    <!-- Overview Tab -->
    <div class="tab-content active" data-tab="overview">
        <div class="card-grid">
            <div class="card accent-blue">
                <div class="card-label">Bankroll</div>
                <div class="card-value" id="bankroll">$0.00</div>
                <div class="card-sub" id="available-capital">Available: $0.00</div>
            </div>
            <div class="card accent-green">
                <div class="card-label">Total P&L</div>
                <div class="card-value" id="total-pnl">$0.00</div>
                <div class="card-sub" id="unrealized-pnl">Realized: $0.00 | Unrealized: $0.00</div>
            </div>
            <div class="card accent-purple">
                <div class="card-label">Open Positions</div>
                <div class="card-value" id="open-positions">0</div>
                <div class="card-sub" id="total-invested">Invested: $0.00</div>
            </div>
            <div class="card accent-teal">
                <div class="card-label">Total Trades</div>
                <div class="card-value" id="total-trades">0</div>
                <div class="card-sub" id="trade-breakdown">Live: 0 | Paper: 0</div>
            </div>
        </div>

        <div class="card-grid">
            <div class="card accent-orange">
                <div class="card-label">Avg Edge</div>
                <div class="card-value" id="avg-edge">0.0%</div>
                <div class="card-sub" id="avg-evidence-quality">Avg EQ: 0.000</div>
            </div>
            <div class="card accent-pink">
                <div class="card-label">Today</div>
                <div class="card-value" id="today-trades">0 trades</div>
                <div class="card-sub" id="daily-volume">Volume: $0.00</div>
            </div>
            <div class="card accent-yellow">
                <div class="card-label">Engine Status</div>
                <div class="card-value" id="engine-status">UNKNOWN</div>
                <div class="card-sub" id="engine-cycles">Cycles: 0</div>
            </div>
        </div>

        <div class="panel">
            <div class="panel-header">
                <h2>Recent Activity</h2>
                <div class="panel-tools">
                    <button class="btn btn-muted btn-sm" onclick="refreshData()">🔄 Refresh</button>
                </div>
            </div>
            <div class="table-wrap">
                <table>
                    <thead>
                        <tr>
                            <th>Time</th>
                            <th>Action</th>
                            <th>Market</th>
                            <th>Details</th>
                            <th>Status</th>
                        </tr>
                    </thead>
                    <tbody id="activity-body">
                        <tr><td colspan="5" class="empty-state">Loading activity...</td></tr>
                    </tbody>
                </table>
            </div>
        </div>
    </div>

    <!-- Trading Tab -->
    <div class="tab-content" data-tab="trading">
        <div class="panel">
            <div class="panel-header">
                <h2>Open Positions</h2>
                <div class="panel-tools">
                    <button class="btn btn-export btn-sm" onclick="exportData('positions')">📊 Export</button>
                </div>
            </div>
            <div class="table-wrap">
                <table id="positions-table">
                    <thead>
                        <tr>
                            <th>Market</th>
                            <th>Direction</th>
                            <th>Entry Price</th>
                            <th>Current Price</th>
                            <th>Size</th>
                            <th>P&L</th>
                            <th>P&L %</th>
                            <th>Status</th>
                            <th>Held</th>
                        </tr>
                    </thead>
                    <tbody id="positions-body">
                        <tr><td colspan="9" class="empty-state">No active positions</td></tr>
                    </tbody>
                </table>
            </div>
        </div>

        <div class="panel">
            <div class="panel-header">
                <h2>Recent Trades</h2>
                <div class="panel-tools">
                    <button class="btn btn-export btn-sm" onclick="exportData('trades')">📊 Export</button>
                </div>
            </div>
            <div class="table-wrap">
                <table id="trades-table">
                    <thead>
                        <tr>
                            <th>Market</th>
                            <th>Direction</th>
                            <th>Entry</th>
                            <th>Exit</th>
                            <th>P&L</th>
                            <th>P&L %</th>
                            <th>Status</th>
                            <th>Held</th>
                            <th>Mode</th>
                        </tr>
                    </thead>
                    <tbody id="trades-body">
                        <tr><td colspan="9" class="empty-state">No trades yet</td></tr>
                    </tbody>
                </table>
            </div>
        </div>
    </div>

    <!-- Learning Lab Tab -->
    <div class="tab-content" data-tab="learning">
        <div class="panel">
            <div class="panel-header">
                <h2>🤖 Self-Improvement Analysis</h2>
                <div class="panel-tools">
                    <button class="btn btn-save btn-sm" onclick="runSelfAnalysis()">🔬 Run Analysis</button>
                    <button class="btn btn-muted btn-sm" onclick="loadLatestAnalysis()">📊 Load Latest</button>
                </div>
            </div>
            <div id="analysis-results">
                <div class="empty-state">Click "Run Analysis" to start self-improvement analysis</div>
            </div>
        </div>

        <div class="panel">
            <div class="panel-header">
                <h2>📈 Learning Progress</h2>
            </div>
            <div id="learning-progress">
                <div class="empty-state">No learning data available yet</div>
            </div>
        </div>
    </div>

    <!-- Admin Tab -->
    <div class="tab-content" data-tab="admin">
        <div class="card-grid">
            <div class="card accent-blue">
                <div class="card-label">Engine Control</div>
                <div class="card-value" id="admin-engine-status">STOPPED</div>
                <div class="card-sub" id="admin-engine-mode">Mode: Unknown</div>
            </div>
            <div class="card accent-green">
                <div class="card-label">Kill Switch</div>
                <div class="card-value" id="kill-switch-status">OFF</div>
                <div class="card-sub">Emergency stop</div>
            </div>
        </div>

        <div class="panel">
            <div class="panel-header">
                <h2>Bot Controls</h2>
            </div>
            <div style="display: flex; gap: 12px; flex-wrap: wrap;">
                <button class="btn btn-ok" id="btn-start" onclick="startBot()">▶ Start Bot</button>
                <button class="btn btn-danger" id="btn-stop" onclick="stopBot()">⏹ Stop Bot</button>
                <button class="btn btn-danger" onclick="killSwitch()">🚨 Kill Switch</button>
                <button class="btn btn-muted" onclick="refreshData()">🔄 Refresh</button>
            </div>
        </div>

        <div class="panel">
            <div class="panel-header">
                <h2>System Status</h2>
            </div>
            <div class="table-wrap">
                <table>
                    <tbody id="system-status-body">
                        <tr><td colspan="2" class="empty-state">Loading system status...</td></tr>
                    </tbody>
                </table>
            </div>
        </div>
    </div>

    <!-- Toast Container -->
    <div id="toast-container"></div>

    <!-- Modal Overlays -->
    <div id="modal-overlay" class="modal-overlay" style="display: none;">
        <div class="modal-box">
            <h3 id="modal-title">Confirm Action</h3>
            <p id="modal-message">Are you sure?</p>
            <div class="modal-actions">
                <button class="btn btn-muted" onclick="closeModal()">Cancel</button>
                <button class="btn btn-danger" id="modal-confirm">Confirm</button>
            </div>
        </div>
    </div>

    <footer>
        <div>Kalshi Trading Bot v1.0</div>
        <div id="footer-status">Status: Loading...</div>
    </footer>

    <script>
        // ─── State ──────────────────────────────────────────────────────
        let _activeTab = 'overview';
        let _modalConfirmCb = null;

        // ─── Tab Navigation ─────────────────────────────────────────────
        function switchTab(tabName) {
            _activeTab = tabName;
            document.querySelectorAll('.tab-btn').forEach(btn => {
                btn.classList.toggle('active', btn.dataset.tab === tabName);
            });
            document.querySelectorAll('.tab-content').forEach(content => {
                content.classList.toggle('active', content.dataset.tab === tabName);
            });
            window.scrollTo({ top: 0, behavior: 'smooth' });
        }

        // ─── Helpers ────────────────────────────────────────────────────
        const fmt = (v, d=2) => Number(v||0).toFixed(d);
        const fmtD = (v) => `$${fmt(v)}`;
        const fmtP = (v) => `${fmt(v)}%`;
        const pnlClass = (v) => v > 0.001 ? 'pnl-positive' : v < -0.001 ? 'pnl-negative' : 'pnl-zero';
        const shortDate = (iso) => {
            if (!iso) return '—';
            const d = new Date(iso);
            return d.toLocaleString('en-US', {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});
        };

        // ─── Toast Notifications ────────────────────────────────────────
        function showToast(message, type='info') {
            const container = document.getElementById('toast-container');
            if (!container) return;
            const toast = document.createElement('div');
            toast.className = `toast toast-${type}`;
            const icons = {success:'✅',error:'❌',info:'ℹ️',warning:'⚠️'};
            toast.innerHTML = `<span class="toast-icon">${icons[type]||'ℹ️'}</span><span class="toast-msg">${message}</span>`;
            container.appendChild(toast);
            requestAnimationFrame(() => toast.classList.add('toast-show'));
            setTimeout(() => {
                toast.classList.remove('toast-show');
                toast.classList.add('toast-hide');
                setTimeout(() => toast.remove(), 400);
            }, 3500);
        }

        // ─── Modal Functions ────────────────────────────────────────────
        function showConfirmModal(title, message, onConfirm) {
            document.getElementById('modal-title').textContent = title;
            document.getElementById('modal-message').textContent = message;
            _modalConfirmCb = onConfirm;
            document.getElementById('modal-overlay').style.display = 'flex';
        }

        function closeModal() {
            document.getElementById('modal-overlay').style.display = 'none';
            _modalConfirmCb = null;
        }

        // ─── API Functions ──────────────────────────────────────────────
        async function apiFetch(url, opts = {}) {
            try {
                const res = await fetch(url, opts);
                if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
                return await res.json();
            } catch (e) {
                console.error(`API ${url}:`, e);
                return null;
            }
        }

        // ─── Data Export ────────────────────────────────────────────────
        async function exportData(table) {
            showToast(`Exporting ${table}…`, 'info');
            const data = await apiFetch(`/api/export/${table}`);
            if (!data || !data.rows) { showToast('Export failed', 'error'); return; }
            if (data.rows.length === 0) { showToast('No data to export', 'warning'); return; }

            const keys = Object.keys(data.rows[0]);
            const csvRows = [keys.join(',')];
            for (const row of data.rows) {
                csvRows.push(keys.map(k => {
                    let v = row[k] ?? '';
                    if (typeof v === 'string' && (v.includes(',') || v.includes('"') || v.includes('\\n'))) {
                        v = `"${v.replace(/"/g, '""')}"`;
                    }
                    return v;
                }).join(','));
            }
            const blob = new Blob([csvRows.join('\\n')], {type:'text/csv'});
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url; a.download = `${table}_export.csv`; a.click();
            URL.revokeObjectURL(url);
            showToast(`Exported ${data.rows.length} rows`, 'success');
        }

        // ─── Bot Controls ───────────────────────────────────────────────
        async function startBot() {
            const response = await fetch('/api/start', { method: 'POST' });
            const result = await response.json();
            if (result.message) {
                showToast(result.message, 'success');
            } else {
                showToast('Failed to start bot', 'error');
            }
            refreshData();
        }

        async function stopBot() {
            const response = await fetch('/api/stop', { method: 'POST' });
            const result = await response.json();
            if (result.message) {
                showToast(result.message, 'success');
            } else {
                showToast('Failed to stop bot', 'error');
            }
            refreshData();
        }

        async function killSwitch() {
            showConfirmModal('Activate Kill Switch',
                'This will immediately halt all trading. Are you sure?',
                async () => {
                    const response = await fetch('/api/kill-switch', { method: 'POST' });
                    const result = await response.json();
                    if (result.message) {
                        showToast(result.message, 'warning');
                    } else {
                        showToast('Kill switch failed', 'error');
                    }
                    refreshData();
                });
        }

        // ─── Learning Lab Functions ─────────────────────────────────────
        async function runSelfAnalysis() {
            showToast('Running self-improvement analysis...', 'info');
            const result = await apiFetch('/api/learning/run-analysis', { method: 'POST' });
            if (result && result.success) {
                showToast('Analysis complete!', 'success');
                loadLatestAnalysis();
            } else {
                showToast('Analysis failed: ' + (result?.error || 'Unknown error'), 'error');
            }
        }

        async function loadLatestAnalysis() {
            const data = await apiFetch('/api/learning/latest-analysis');
            const container = document.getElementById('analysis-results');
            if (!data) {
                container.innerHTML = '<div class="empty-state">No analysis data available</div>';
                return;
            }

            let html = '<div style="font-family: monospace; font-size: 0.8rem; white-space: pre-wrap;">';
            if (data.strategy_performance) {
                html += '🎯 STRATEGY PERFORMANCE SUMMARY:\\n';
                for (const [strategy, stats] of Object.entries(data.strategy_performance)) {
                    const exp = stats.expectancy || 0;
                    const wr = stats.win_rate || 0;
                    const trades = stats.trades || 0;
                    html += `  ${strategy}: Exp: ${exp.toFixed(2)}, WR: ${(wr*100).toFixed(1)}%, Trades: ${trades}\\n`;
                }
                html += '\\n';
            }

            if (data.new_parameters) {
                html += '🔧 NEW PARAMETERS GENERATED:\\n';
                html += JSON.stringify(data.new_parameters, null, 2);
            }

            html += '</div>';
            container.innerHTML = html;
        }

        // ─── Main Data Refresh ──────────────────────────────────────────
        async function refreshData() {
            try {
                // Update portfolio
                const portfolio = await apiFetch('/api/portfolio');
                if (portfolio) {
                    document.getElementById('bankroll').textContent = fmtD(portfolio.bankroll);
                    document.getElementById('available-capital').textContent = `Available: ${fmtD(portfolio.available_capital)}`;
                    document.getElementById('total-pnl').textContent = fmtD(portfolio.total_pnl);
                    document.getElementById('total-pnl').className = `card-value ${pnlClass(portfolio.total_pnl)}`;
                    document.getElementById('unrealized-pnl').textContent = portfolio.open_positions > 0
                        ? `Realized: ${fmtD(portfolio.realized_pnl || 0)} | Unrealized: ${fmtD(portfolio.unrealized_pnl)}`
                        : `Realized: ${fmtD(portfolio.realized_pnl || 0)}`;
                    document.getElementById('open-positions').textContent = portfolio.open_positions;
                    document.getElementById('total-invested').textContent = `Invested: ${fmtD(portfolio.total_invested)}`;
                    document.getElementById('total-trades').textContent = portfolio.total_trades;
                    document.getElementById('trade-breakdown').textContent = `Live: ${portfolio.live_trades} | Paper: ${portfolio.paper_trades}`;
                    document.getElementById('avg-edge').textContent = fmtP(portfolio.avg_edge * 100);
                    document.getElementById('avg-evidence-quality').textContent = `Avg EQ: ${fmt(portfolio.avg_evidence_quality, 3)}`;
                    document.getElementById('today-trades').textContent = `${portfolio.today_trades} trades`;
                    document.getElementById('daily-volume').textContent = `Volume: ${fmtD(portfolio.daily_volume)}`;

                    // Mode badge
                    const modeBadge = document.getElementById('mode-badge');
                    if (portfolio.live_trading_enabled && !portfolio.dry_run) {
                        modeBadge.textContent = 'LIVE';
                        modeBadge.className = 'badge badge-live';
                    } else {
                        modeBadge.textContent = 'PAPER MODE';
                        modeBadge.className = 'badge badge-paper';
                    }
                }

                // Update engine status
                const status = await apiFetch('/api/engine-status');
                if (status) {
                    document.getElementById('engine-status').textContent = status.running ? 'RUNNING' : 'STOPPED';
                    document.getElementById('engine-status').className = status.running ? 'card-value pnl-positive' : 'card-value pnl-zero';
                    document.getElementById('engine-cycles').textContent = `Cycles: ${status.cycles || 0}`;
                    document.getElementById('admin-engine-status').textContent = status.running ? 'RUNNING' : 'STOPPED';
                    document.getElementById('admin-engine-mode').textContent = status.live_trading ? '🔴 LIVE' : (status.paper_mode ? '📝 Paper' : '⚠️ Dry Run');
                }

                // Update kill switch
                const killStatus = await apiFetch('/api/kill-switch-status');
                if (killStatus) {
                    document.getElementById('kill-switch-status').textContent = killStatus.active ? '🛑 ACTIVE' : 'OFF';
                    document.getElementById('kill-switch-status').className = `card-value ${killStatus.active ? 'pnl-negative' : 'pnl-zero'}`;
                }

                // Update positions
                const positions = await apiFetch('/api/positions');
                if (positions && positions.positions) {
                    const tbody = document.getElementById('positions-body');
                    if (positions.positions.length === 0) {
                        tbody.innerHTML = '<tr><td colspan="9" class="empty-state">No active positions</td></tr>';
                    } else {
                        tbody.innerHTML = positions.positions.map(p => {
                            const pnl = p.pnl || 0;
                            const pnlPct = p.pnl_pct || 0;
                            const priceChange = p.price_change || 0;
                            const priceChangePct = p.price_change_pct || 0;
                            const arrow = priceChange > 0.001 ? '▲' : priceChange < -0.001 ? '▼' : '─';
                            const arrowClass = priceChange > 0.001 ? 'pnl-positive' : priceChange < -0.001 ? 'pnl-negative' : 'pnl-zero';
                            const hoursHeld = p.hours_held || 0;
                            const timeLabel = hoursHeld >= 24 ? `${(hoursHeld/24).toFixed(1)}d` : `${hoursHeld.toFixed(1)}h`;

                            return `<tr>
                                <td title="${p.market_id}">${(p.question||p.market_id||'').substring(0,50)}</td>
                                <td><span class="pill ${p.direction==='BUY_YES'||p.direction==='BUY'?'pill-buy':'pill-sell'}">${p.direction||'—'}</span></td>
                                <td>${fmt(p.entry_price,3)}</td>
                                <td>
                                    <span class="live-price">${fmt(p.current_price,3)}</span>
                                    <span class="price-arrow ${arrowClass}">${arrow}</span>
                                </td>
                                <td>${fmt(p.size,1)}</td>
                                <td class="${pnlClass(pnl)}">${fmtD(pnl)}</td>
                                <td class="${pnlClass(pnlPct)}">${fmtP(pnlPct)}</td>
                                <td>${p.status || 'Active'}</td>
                                <td title="${p.opened_at||''}">${timeLabel}</td>
                            </tr>`;
                        }).join('');
                    }
                }

                // Update trades
                const trades = await apiFetch('/api/trades?limit=20');
                if (trades && trades.trades) {
                    const tbody = document.getElementById('trades-body');
                    if (trades.trades.length === 0) {
                        tbody.innerHTML = '<tr><td colspan="9" class="empty-state">No trades yet</td></tr>';
                    } else {
                        tbody.innerHTML = trades.trades.map(t => {
                            const pnl = t.pnl != null ? t.pnl : null;
                            const pnlCls = pnl > 0 ? 'pnl-positive' : pnl < 0 ? 'pnl-negative' : 'pnl-zero';
                            const pnlStr = pnl != null ? (pnl >= 0 ? '+' : '') + fmtD(pnl) : '—';
                            const pnlPctStr = t.pnl_pct != null ? (t.pnl_pct >= 0 ? '+' : '') + t.pnl_pct.toFixed(2) + '%' : '—';
                            const entry = t.entry_price != null ? fmt(t.entry_price, 3) : '—';
                            const exitVal = t.trade_status === 'ACTIVE'
                                ? `<span style="color:var(--accent-blue)">${fmt(t.current_price||0,3)}</span>`
                                : (t.exit_price != null ? fmt(t.exit_price, 3) : '—');
                            const dirCls = (t.direction||'').toUpperCase() === 'YES' ? 'pill-buy' : 'pill-sell';
                            const reasonLabel = t.close_reason_label || (t.trade_status === 'ACTIVE' ? '—' : '—');
                            const isActive = t.trade_status === 'ACTIVE';
                            const hoursHeld = t.hours_held || 0;
                            const timeLabel = hoursHeld >= 24 ? `${(hoursHeld/24).toFixed(1)}d` : `${hoursHeld.toFixed(1)}h`;

                            return `<tr>
                                <td title="${t.question||''}">${(t.question||t.market_id||'').substring(0,55)}${(t.question||'').length>55?'…':''}</td>
                                <td><span class="pill ${dirCls}">${t.direction||'—'}</span></td>
                                <td style="font-family:var(--font-mono)">${entry}</td>
                                <td style="font-family:var(--font-mono)">${exitVal}</td>
                                <td style="font-family:var(--font-mono)" class="${pnlCls}">${pnlStr}</td>
                                <td class="${pnlCls}">${pnlPctStr}</td>
                                <td><span class="pill ${isActive ? 'pill-filled' : 'pill-dry'}">${t.trade_status}</span></td>
                                <td>${reasonLabel}</td>
                                <td>${timeLabel}</td>
                                <td>${t.is_paper ? '🧪 Paper' : '💰 Live'}</td>
                            </tr>`;
                        }).join('');
                    }
                }

                // Update activity feed
                const activity = await apiFetch('/api/activity?limit=10');
                if (activity && activity.entries) {
                    const tbody = document.getElementById('activity-body');
                    if (activity.entries.length === 0) {
                        tbody.innerHTML = '<tr><td colspan="5" class="empty-state">No recent activity</td></tr>';
                    } else {
                        tbody.innerHTML = activity.entries.map(e => `
                            <tr>
                                <td>${shortDate(e.timestamp)}</td>
                                <td>${e.action}</td>
                                <td>${(e.market_id || '').substring(0, 30)}</td>
                                <td>${e.details || ''}</td>
                                <td><span class="pill pill-${e.status === 'success' ? 'trade' : 'no-trade'}">${e.status}</span></td>
                            </tr>
                        }).join('');
                    }
                }

                // Update system status
                const sysStatus = await apiFetch('/api/system-status');
                if (sysStatus) {
                    const tbody = document.getElementById('system-status-body');
                    tbody.innerHTML = Object.entries(sysStatus).map(([key, value]) => `
                        <tr>
                            <td>${key.replace(/_/g, ' ').replace(/\\b\\w/g, c => c.toUpperCase())}</td>
                            <td>${value}</td>
                        </tr>
                    `).join('');
                }

                document.getElementById('last-updated').textContent = 'Last updated: ' + new Date().toLocaleString();
                document.getElementById('footer-status').textContent = 'Status: Connected';
            } catch (error) {
                console.error('Refresh error:', error);
                document.getElementById('footer-status').textContent = 'Status: Error';
                showToast('Failed to refresh data', 'error');
            }
        }

        // Auto-refresh every 15 seconds
        setInterval(refreshData, 15000);

        // Initial load
        refreshData();
    </script>
</body>
</html>
"""

@app.route('/')
def dashboard():
    return render_template_string(HTML_TEMPLATE)

@app.route('/api/start', methods=['POST'])
def start_bot():
    global bot_instance
    try:
        if not bot_instance:
            bot_instance = KalshiBot()
        asyncio.create_task(bot_instance.start())
        return jsonify({'message': 'Bot started successfully'})
    except Exception as e:
        logger.error(f"Failed to start bot: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/stop', methods=['POST'])
def stop_bot():
    global bot_instance
    try:
        if bot_instance:
            asyncio.create_task(bot_instance.stop())
        return jsonify({'message': 'Bot stop signal sent'})
    except Exception as e:
        logger.error(f"Failed to stop bot: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/kill-switch', methods=['POST'])
def kill_switch():
    try:
        db = SupabaseDB()
        db.client.table('kill_switch').insert({'active': True, 'timestamp': datetime.utcnow().isoformat()}).execute()
        return jsonify({'message': 'Kill switch activated'})
    except Exception as e:
        logger.error(f"Failed to activate kill switch: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/kill-switch-status')
def kill_switch_status():
    try:
        db = SupabaseDB()
        result = db.client.table('kill_switch').select('*').eq('active', True).order('timestamp', desc=True).limit(1).execute()
        active = len(result.data) > 0
        return jsonify({'active': active})
    except Exception as e:
        logger.error(f"Failed to get kill switch status: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/portfolio')
def get_portfolio():
    try:
        db = SupabaseDB()

        # Get positions
        positions = db.client.table('positions').select('*').eq('status', 'open').execute()
        open_positions = len(positions.data) if positions.data else 0

        # Calculate P&L
        total_pnl = sum(p.get('pnl', 0) for p in (positions.data or []))
        total_invested = sum(p.get('stake_usd', 0) for p in (positions.data or []))
        realized_pnl = sum(p.get('realized_pnl', 0) for p in (positions.data or []))
        unrealized_pnl = total_pnl - realized_pnl

        # Get trade count
        trades = db.client.table('trades').select('id, is_paper').execute()
        total_trades = len(trades.data) if trades.data else 0
        live_trades = sum(1 for t in (trades.data or []) if not t.get('is_paper', False))
        paper_trades = total_trades - live_trades

        # Get today's trades
        today = datetime.utcnow().date()
        today_trades = db.client.table('trades').select('stake_usd').gte('opened_at', today.isoformat()).execute()
        today_trades_count = len(today_trades.data) if today_trades.data else 0
        daily_volume = sum(t.get('stake_usd', 0) for t in (today_trades.data or []))

        # Get average edge and evidence quality from recent signal evaluations
        recent_signals = db.client.table('signal_evaluations').select('edge, evidence_quality').gte('timestamp', (datetime.utcnow() - timedelta(days=7)).isoformat()).execute()
        if recent_signals.data:
            avg_edge = sum(s.get('edge', 0) for s in recent_signals.data) / len(recent_signals.data)
            avg_evidence_quality = sum(s.get('evidence_quality', 0) for s in recent_signals.data) / len(recent_signals.data)
        else:
            avg_edge = 0
            avg_evidence_quality = 0

        # Check trading mode
        live_trading = os.getenv('ENABLE_LIVE_TRADING', 'false').lower() == 'true'
        dry_run = os.getenv('DRY_RUN', 'true').lower() == 'true'

        return jsonify({
            'bankroll': 10000.0,  # Placeholder - should come from config
            'available_capital': 10000.0 - total_invested,  # Placeholder
            'total_pnl': total_pnl,
            'realized_pnl': realized_pnl,
            'unrealized_pnl': unrealized_pnl,
            'open_positions': open_positions,
            'total_invested': total_invested,
            'total_trades': total_trades,
            'live_trades': live_trades,
            'paper_trades': paper_trades,
            'avg_edge': avg_edge,
            'avg_evidence_quality': avg_evidence_quality,
            'today_trades': today_trades_count,
            'daily_volume': daily_volume,
            'live_trading_enabled': live_trading,
            'dry_run': dry_run
        })
    except Exception as e:
        logger.error(f"Failed to get portfolio: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/engine-status')
def get_engine_status():
    try:
        # Simplified - in real implementation, check if bot process is running
        running = True  # Placeholder
        live_trading = os.getenv('ENABLE_LIVE_TRADING', 'false').lower() == 'true'
        paper_mode = os.getenv('DRY_RUN', 'true').lower() == 'true'
        return jsonify({
            'running': running,
            'live_trading': live_trading,
            'paper_mode': paper_mode,
            'cycles': 0  # Placeholder
        })
    except Exception as e:
        logger.error(f"Failed to get engine status: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/positions')
def get_positions():
    try:
        db = SupabaseDB()
        positions = db.client.table('positions').select('*').eq('status', 'open').execute()

        # Enhance with current market data (simplified)
        for pos in positions.data or []:
            # Calculate P&L based on current price (placeholder)
            current_price = pos.get('current_price', pos.get('entry_price', 0))
            entry_price = pos.get('entry_price', 0)
            pos['price_change'] = current_price - entry_price
            pos['price_change_pct'] = (pos['price_change'] / entry_price * 100) if entry_price > 0 else 0
            pos['pnl'] = pos.get('pnl', 0)  # Would be calculated properly
            pos['pnl_pct'] = pos.get('pnl_pct', 0)

        return jsonify({
            'positions': positions.data or []
        })
    except Exception as e:
        logger.error(f"Failed to get positions: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/trades')
def get_trades():
    try:
        limit = int(request.args.get('limit', 50))
        db = SupabaseDB()
        trades = db.client.table('trades').select('*').order('opened_at', desc=True).limit(limit).execute()
        return jsonify({
            'trades': trades.data or []
        })
    except Exception as e:
        logger.error(f"Failed to get trades: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/activity')
def get_activity():
    try:
        limit = int(request.args.get('limit', 20))
        db = SupabaseDB()

        # Get recent trades and positions as activity
        trades = db.client.table('trades').select('*').order('opened_at', desc=True).limit(limit//2).execute()
        positions = db.client.table('positions').select('*').order('opened_at', desc=True).limit(limit//2).execute()

        entries = []

        # Add trade entries
        for trade in trades.data or []:
            entries.append({
                'timestamp': trade.get('opened_at'),
                'action': 'Trade Executed',
                'market_id': trade.get('market_id'),
                'details': f"{trade.get('direction')} {trade.get('size', 0)} shares at ${trade.get('entry_price', 0):.3f}",
                'status': 'success'
            })

        # Add position entries
        for pos in positions.data or []:
            entries.append({
                'timestamp': pos.get('opened_at'),
                'action': 'Position Opened',
                'market_id': pos.get('market_id'),
                'details': f"{pos.get('direction')} {pos.get('size', 0)} shares",
                'status': 'success'
            })

        # Sort by timestamp
        entries.sort(key=lambda x: x['timestamp'] or '', reverse=True)
        entries = entries[:limit]

        return jsonify({
            'entries': entries
        })
    except Exception as e:
        logger.error(f"Failed to get activity: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/system-status')
def get_system_status():
    try:
        import psutil
        import platform

        return jsonify({
            'hostname': platform.node(),
            'platform': platform.system(),
            'python_version': platform.python_version(),
            'cpu_count': psutil.cpu_count(),
            'memory_usage': f"{psutil.virtual_memory().percent:.1f}%",
            'disk_usage': f"{psutil.disk_usage('/').percent:.1f}%" if platform.system() != 'Windows' else 'N/A',
            'process_uptime': 'N/A',  # Would need to track process start time
        })
    except Exception as e:
        logger.error(f"Failed to get system status: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/export/<table>')
def export_table(table):
    try:
        db = SupabaseDB()
        if table == 'positions':
            data = db.client.table('positions').select('*').execute()
        elif table == 'trades':
            data = db.client.table('trades').select('*').execute()
        else:
            return jsonify({'error': 'Invalid table'}), 400

        return jsonify({
            'rows': data.data or []
        })
    except Exception as e:
        logger.error(f"Failed to export {table}: {e}")
        return jsonify({'error': str(e)}), 500

# Learning Lab endpoints
@app.route('/api/learning/run-analysis', methods=['POST'])
def run_self_analysis():
    try:
        from self_improver import SelfImprover
        improver = SelfImprover()
        results = improver.run_full_analysis(lookback_days=7)
        return jsonify({'success': True, 'results': results})
    except Exception as e:
        logger.error(f"Failed to run analysis: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/learning/latest-analysis')
def get_latest_analysis():
    try:
        db = SupabaseDB()
        result = db.client.table('improvement_logs').select('*').order('timestamp', desc=True).limit(1).execute()
        if result.data and len(result.data) > 0:
            return jsonify(result.data[0].get('analysis_json', {}))
        return jsonify({})
    except Exception as e:
        logger.error(f"Failed to get latest analysis: {e}")
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
+++++++ REPLACE</diff>
</replace_in_file>