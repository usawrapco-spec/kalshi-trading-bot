"""
Kalshi Trading Bot Dashboard - BADASS EDITION
Ultimate professional trading dashboard inspired by TradingView, Binance Pro, and Material Dashboard 3
"""

import os
import json
import asyncio
from datetime import datetime, timedelta
from collections import defaultdict
from flask import Flask, render_template_string, request, jsonify
from flask_cors import CORS

from utils.supabase_db import SupabaseDB
from utils.logger import setup_logger

logger = setup_logger('dashboard')

app = Flask(__name__)
CORS(app)

# Global bot instance
bot_instance = None

# ULTIMATE BADASS DASHBOARD TEMPLATE
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>🚀 Kalshi Pro Dashboard</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800;900&family=JetBrains+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css" rel="stylesheet">
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/luxon@3.3.0/build/global/luxon.min.js"></script>
    <style>
        :root {
            /* Advanced Color System */
            --bg-primary: #0a0b0f;
            --bg-secondary: #111318;
            --bg-tertiary: #1a1d23;
            --bg-card: rgba(26, 29, 39, 0.95);
            --bg-overlay: rgba(10, 11, 15, 0.98);

            /* Professional Gradients */
            --gradient-primary: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            --gradient-success: linear-gradient(135deg, #4facfe 0%, #00f2fe 100%);
            --gradient-danger: linear-gradient(135deg, #ff6b6b 0%, #ee5a24 100%);
            --gradient-warning: linear-gradient(135deg, #f093fb 0%, #f5576c 100%);
            --gradient-info: linear-gradient(135deg, #4facfe 0%, #00f2fe 100%);
            --gradient-purple: linear-gradient(135deg, #a8edea 0%, #fed6e3 100%);

            /* Typography */
            --font-primary: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
            --font-mono: 'JetBrains Mono', 'Fira Code', monospace;

            /* Spacing & Layout */
            --radius-sm: 8px;
            --radius-md: 12px;
            --radius-lg: 16px;
            --radius-xl: 24px;

            --shadow-sm: 0 2px 8px rgba(0,0,0,0.1);
            --shadow-md: 0 8px 24px rgba(0,0,0,0.15);
            --shadow-lg: 0 16px 48px rgba(0,0,0,0.25);
            --shadow-xl: 0 32px 64px rgba(0,0,0,0.3);

            /* Colors */
            --text-primary: #ffffff;
            --text-secondary: #a1a1aa;
            --text-muted: #71717a;
            --text-accent: #3b82f6;

            --border-primary: rgba(255,255,255,0.1);
            --border-secondary: rgba(255,255,255,0.05);

            /* Status Colors */
            --success: #10b981;
            --danger: #ef4444;
            --warning: #f59e0b;
            --info: #3b82f6;
            --purple: #8b5cf6;
            --pink: #ec4899;
            --teal: #14b8a6;
        }

        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }

        body {
            font-family: var(--font-primary);
            background: var(--bg-primary);
            color: var(--text-primary);
            line-height: 1.6;
            overflow-x: hidden;
            position: relative;
        }

        /* Animated Background */
        body::before {
            content: '';
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background:
                radial-gradient(circle at 20% 80%, rgba(120, 119, 198, 0.3) 0%, transparent 50%),
                radial-gradient(circle at 80% 20%, rgba(255, 119, 198, 0.3) 0%, transparent 50%),
                radial-gradient(circle at 40% 40%, rgba(120, 219, 226, 0.2) 0%, transparent 50%);
            animation: backgroundShift 20s ease-in-out infinite alternate;
            z-index: -1;
        }

        @keyframes backgroundShift {
            0% { transform: scale(1) rotate(0deg); }
            100% { transform: scale(1.1) rotate(1deg); }
        }

        /* Header */
        .header {
            background: var(--bg-overlay);
            backdrop-filter: blur(20px);
            border-bottom: 1px solid var(--border-primary);
            padding: 1rem 2rem;
            position: sticky;
            top: 0;
            z-index: 1000;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }

        .header h1 {
            font-size: 1.5rem;
            font-weight: 800;
            background: var(--gradient-primary);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
            margin: 0;
        }

        .header .subtitle {
            font-size: 0.875rem;
            color: var(--text-muted);
            margin-left: 1rem;
        }

        .header-right {
            display: flex;
            align-items: center;
            gap: 1rem;
        }

        .status-badge {
            padding: 0.5rem 1rem;
            border-radius: var(--radius-lg);
            font-size: 0.75rem;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            position: relative;
            overflow: hidden;
        }

        .status-badge.live {
            background: var(--gradient-danger);
            color: white;
            animation: pulse 2s infinite;
            box-shadow: 0 0 20px rgba(239, 68, 68, 0.3);
        }

        .status-badge.paper {
            background: var(--gradient-warning);
            color: #1f2937;
        }

        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.7; }
        }

        /* Navigation */
        .nav {
            background: var(--bg-secondary);
            border-bottom: 1px solid var(--border-primary);
            padding: 0 2rem;
            position: sticky;
            top: 73px;
            z-index: 999;
        }

        .nav-tabs {
            display: flex;
            gap: 0.25rem;
            overflow-x: auto;
        }

        .nav-tab {
            display: flex;
            align-items: center;
            gap: 0.5rem;
            padding: 0.75rem 1.5rem;
            background: transparent;
            border: none;
            color: var(--text-muted);
            font-size: 0.875rem;
            font-weight: 600;
            cursor: pointer;
            border-radius: var(--radius-md);
            transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
            position: relative;
            white-space: nowrap;
        }

        .nav-tab:hover {
            background: var(--bg-tertiary);
            color: var(--text-primary);
        }

        .nav-tab.active {
            background: var(--gradient-primary);
            color: white;
            box-shadow: 0 4px 20px rgba(102, 126, 234, 0.3);
        }

        .nav-tab.active::after {
            content: '';
            position: absolute;
            bottom: -1px;
            left: 50%;
            transform: translateX(-50%);
            width: 80%;
            height: 2px;
            background: white;
            border-radius: 1px;
        }

        /* Main Content */
        .main {
            padding: 2rem;
            max-width: 1600px;
            margin: 0 auto;
        }

        /* Stats Grid */
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
            gap: 1.5rem;
            margin-bottom: 2rem;
        }

        .stat-card {
            background: var(--bg-card);
            backdrop-filter: blur(20px);
            border: 1px solid var(--border-primary);
            border-radius: var(--radius-xl);
            padding: 1.5rem;
            position: relative;
            overflow: hidden;
            transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
            cursor: pointer;
        }

        .stat-card::before {
            content: '';
            position: absolute;
            top: 0;
            left: 0;
            right: 0;
            height: 3px;
            background: var(--gradient-primary);
            transform: scaleX(0);
            transition: transform 0.3s ease;
        }

        .stat-card:hover {
            transform: translateY(-4px);
            box-shadow: var(--shadow-xl);
            border-color: rgba(255,255,255,0.2);
        }

        .stat-card:hover::before {
            transform: scaleX(1);
        }

        .stat-card.success { --gradient-primary: var(--gradient-success); }
        .stat-card.danger { --gradient-primary: var(--gradient-danger); }
        .stat-card.warning { --gradient-primary: var(--gradient-warning); }
        .stat-card.info { --gradient-primary: var(--gradient-info); }
        .stat-card.purple { --gradient-primary: var(--gradient-purple); }

        .stat-header {
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            margin-bottom: 1rem;
        }

        .stat-title {
            font-size: 0.875rem;
            font-weight: 600;
            color: var(--text-muted);
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }

        .stat-icon {
            width: 2.5rem;
            height: 2.5rem;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            background: var(--gradient-primary);
            color: white;
            font-size: 1rem;
        }

        .stat-value {
            font-size: 2rem;
            font-weight: 800;
            font-family: var(--font-mono);
            margin-bottom: 0.25rem;
            background: linear-gradient(135deg, var(--text-primary), var(--text-secondary));
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
        }

        .stat-subtitle {
            font-size: 0.875rem;
            color: var(--text-muted);
        }

        /* Charts */
        .chart-container {
            background: var(--bg-card);
            backdrop-filter: blur(20px);
            border: 1px solid var(--border-primary);
            border-radius: var(--radius-xl);
            padding: 1.5rem;
            margin-bottom: 2rem;
            position: relative;
            overflow: hidden;
        }

        .chart-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 1rem;
        }

        .chart-title {
            font-size: 1.25rem;
            font-weight: 700;
            margin: 0;
        }

        .chart-subtitle {
            font-size: 0.875rem;
            color: var(--text-muted);
            margin: 0;
        }

        /* Tables */
        .table-container {
            background: var(--bg-card);
            backdrop-filter: blur(20px);
            border: 1px solid var(--border-primary);
            border-radius: var(--radius-xl);
            overflow: hidden;
            margin-bottom: 2rem;
        }

        .table-header {
            padding: 1.5rem;
            border-bottom: 1px solid var(--border-primary);
            display: flex;
            justify-content: space-between;
            align-items: center;
        }

        .table-title {
            font-size: 1.25rem;
            font-weight: 700;
            margin: 0;
        }

        .table-actions {
            display: flex;
            gap: 0.5rem;
        }

        .table-wrapper {
            overflow-x: auto;
        }

        table {
            width: 100%;
            border-collapse: collapse;
        }

        thead th {
            padding: 1rem;
            text-align: left;
            font-size: 0.75rem;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            color: var(--text-muted);
            background: var(--bg-tertiary);
            border-bottom: 1px solid var(--border-primary);
            white-space: nowrap;
        }

        tbody td {
            padding: 1rem;
            border-bottom: 1px solid var(--border-secondary);
            vertical-align: middle;
            font-size: 0.875rem;
        }

        tbody tr {
            transition: background-color 0.2s ease;
        }

        tbody tr:hover {
            background: var(--bg-tertiary);
        }

        /* Status Pills */
        .pill {
            display: inline-flex;
            align-items: center;
            gap: 0.25rem;
            padding: 0.25rem 0.75rem;
            border-radius: 9999px;
            font-size: 0.75rem;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }

        .pill.success {
            background: rgba(16, 185, 129, 0.1);
            color: var(--success);
            border: 1px solid rgba(16, 185, 129, 0.2);
        }

        .pill.danger {
            background: rgba(239, 68, 68, 0.1);
            color: var(--danger);
            border: 1px solid rgba(239, 68, 68, 0.2);
        }

        .pill.warning {
            background: rgba(245, 158, 11, 0.1);
            color: var(--warning);
            border: 1px solid rgba(245, 158, 11, 0.2);
        }

        .pill.info {
            background: rgba(59, 130, 246, 0.1);
            color: var(--info);
            border: 1px solid rgba(59, 130, 246, 0.2);
        }

        /* Buttons */
        .btn {
            display: inline-flex;
            align-items: center;
            gap: 0.5rem;
            padding: 0.5rem 1rem;
            border-radius: var(--radius-md);
            font-size: 0.875rem;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            border: none;
            cursor: pointer;
            transition: all 0.2s ease;
            position: relative;
            overflow: hidden;
        }

        .btn::before {
            content: '';
            position: absolute;
            inset: 0;
            background: linear-gradient(135deg, rgba(255,255,255,0.1), transparent);
            opacity: 0;
            transition: opacity 0.2s ease;
        }

        .btn:hover::before {
            opacity: 1;
        }

        .btn:hover {
            transform: translateY(-1px);
        }

        .btn-primary {
            background: var(--gradient-primary);
            color: white;
            box-shadow: 0 4px 16px rgba(102, 126, 234, 0.3);
        }

        .btn-success {
            background: var(--gradient-success);
            color: #1f2937;
        }

        .btn-danger {
            background: var(--gradient-danger);
            color: white;
        }

        .btn-ghost {
            background: transparent;
            color: var(--text-secondary);
            border: 1px solid var(--border-primary);
        }

        .btn-ghost:hover {
            background: var(--bg-tertiary);
            border-color: var(--border-secondary);
        }

        /* Empty State */
        .empty-state {
            text-align: center;
            padding: 3rem 1rem;
            color: var(--text-muted);
        }

        .empty-state-icon {
            font-size: 3rem;
            margin-bottom: 1rem;
            opacity: 0.5;
        }

        .empty-state-title {
            font-size: 1.125rem;
            font-weight: 600;
            margin-bottom: 0.5rem;
        }

        .empty-state-description {
            font-size: 0.875rem;
        }

        /* Responsive */
        @media (max-width: 768px) {
            .main {
                padding: 1rem;
            }

            .stats-grid {
                grid-template-columns: 1fr;
                gap: 1rem;
            }

            .header {
                padding: 1rem;
                flex-direction: column;
                gap: 1rem;
                text-align: center;
            }

            .nav {
                padding: 0 1rem;
                top: auto;
            }

            .nav-tabs {
                justify-content: center;
            }
        }

        /* Animations */
        @keyframes fadeInUp {
            from {
                opacity: 0;
                transform: translateY(20px);
            }
            to {
                opacity: 1;
                transform: translateY(0);
            }
        }

        .fade-in-up {
            animation: fadeInUp 0.6s ease-out;
        }

        /* Loading */
        .loading {
            display: inline-block;
            width: 1rem;
            height: 1rem;
            border: 2px solid rgba(255,255,255,0.1);
            border-radius: 50%;
            border-top-color: #667eea;
            animation: spin 1s ease-in-out infinite;
        }

        @keyframes spin {
            to { transform: rotate(360deg); }
        }
    </style>
</head>
<body>
    <header class="header">
        <div>
            <h1>🚀 Kalshi Pro</h1>
            <span class="subtitle">AI-Powered Trading Intelligence</span>
        </div>
        <div class="header-right">
            <div class="status-badge paper" id="mode-badge">PAPER MODE</div>
            <div class="last-updated" id="last-updated">Loading...</div>
        </div>
    </header>

    <nav class="nav">
        <div class="nav-tabs">
            <button class="nav-tab active" data-tab="overview">
                <i class="fas fa-chart-line"></i>
                <span>Overview</span>
            </button>
            <button class="nav-tab" data-tab="trading">
                <i class="fas fa-exchange-alt"></i>
                <span>Trading</span>
            </button>
            <button class="nav-tab" data-tab="analytics">
                <i class="fas fa-brain"></i>
                <span>Analytics</span>
            </button>
            <button class="nav-tab" data-tab="settings">
                <i class="fas fa-cog"></i>
                <span>Settings</span>
            </button>
        </div>
    </nav>

    <main class="main">
        <!-- Overview Tab -->
        <div class="tab-content active" data-tab="overview">
            <div class="stats-grid">
                <div class="stat-card success">
                    <div class="stat-header">
                        <div>
                            <div class="stat-title">Portfolio Value</div>
                            <div class="stat-value" id="portfolio-value">$0.00</div>
                            <div class="stat-subtitle" id="portfolio-change">+0.00 (0.00%)</div>
                        </div>
                        <div class="stat-icon">
                            <i class="fas fa-wallet"></i>
                        </div>
                    </div>
                </div>

                <div class="stat-card info">
                    <div class="stat-header">
                        <div>
                            <div class="stat-title">Total P&L</div>
                            <div class="stat-value" id="total-pnl">$0.00</div>
                            <div class="stat-subtitle" id="pnl-breakdown">Realized: $0.00 | Unrealized: $0.00</div>
                        </div>
                        <div class="stat-icon">
                            <i class="fas fa-chart-bar"></i>
                        </div>
                    </div>
                </div>

                <div class="stat-card warning">
                    <div class="stat-header">
                        <div>
                            <div class="stat-title">Active Positions</div>
                            <div class="stat-value" id="active-positions">0</div>
                            <div class="stat-subtitle" id="positions-value">Value: $0.00</div>
                        </div>
                        <div class="stat-icon">
                            <i class="fas fa-briefcase"></i>
                        </div>
                    </div>
                </div>

                <div class="stat-card purple">
                    <div class="stat-header">
                        <div>
                            <div class="stat-title">Today's Trades</div>
                            <div class="stat-value" id="today-trades">0</div>
                            <div class="stat-subtitle" id="trade-success">Success Rate: 0%</div>
                        </div>
                        <div class="stat-icon">
                            <i class="fas fa-bolt"></i>
                        </div>
                    </div>
                </div>
            </div>

            <div class="chart-container">
                <div class="chart-header">
                    <div>
                        <h3 class="chart-title">Portfolio Performance</h3>
                        <p class="chart-subtitle">7-day equity curve</p>
                    </div>
                    <div class="chart-actions">
                        <button class="btn btn-ghost">
                            <i class="fas fa-download"></i>
                            Export
                        </button>
                    </div>
                </div>
                <canvas id="equity-chart" width="400" height="200"></canvas>
            </div>

            <div class="table-container">
                <div class="table-header">
                    <h3 class="table-title">Recent Activity</h3>
                    <div class="table-actions">
                        <button class="btn btn-ghost" onclick="refreshData()">
                            <i class="fas fa-sync-alt"></i>
                            Refresh
                        </button>
                        <button class="btn btn-primary">
                            <i class="fas fa-plus"></i>
                            New Trade
                        </button>
                    </div>
                </div>
                <div class="table-wrapper">
                    <table>
                        <thead>
                            <tr>
                                <th>Time</th>
                                <th>Action</th>
                                <th>Market</th>
                                <th>Strategy</th>
                                <th>P&L</th>
                                <th>Status</th>
                            </tr>
                        </thead>
                        <tbody id="activity-body">
                            <tr>
                                <td colspan="6" class="empty-state">
                                    <div class="empty-state-icon">📊</div>
                                    <div class="empty-state-title">No Recent Activity</div>
                                    <div class="empty-state-description">Trade activity will appear here</div>
                                </td>
                            </tr>
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- Trading Tab -->
        <div class="tab-content" data-tab="trading">
            <div class="table-container">
                <div class="table-header">
                    <h3 class="table-title">Open Positions</h3>
                    <div class="table-actions">
                        <button class="btn btn-ghost">
                            <i class="fas fa-filter"></i>
                            Filter
                        </button>
                        <button class="btn btn-primary">
                            <i class="fas fa-plus"></i>
                            Add Position
                        </button>
                    </div>
                </div>
                <div class="table-wrapper">
                    <table>
                        <thead>
                            <tr>
                                <th>Market</th>
                                <th>Direction</th>
                                <th>Entry Price</th>
                                <th>Current Price</th>
                                <th>Size</th>
                                <th>P&L</th>
                                <th>P&L %</th>
                                <th>Duration</th>
                                <th>Actions</th>
                            </tr>
                        </thead>
                        <tbody id="positions-body">
                            <tr>
                                <td colspan="9" class="empty-state">
                                    <div class="empty-state-icon">📈</div>
                                    <div class="empty-state-title">No Open Positions</div>
                                    <div class="empty-state-description">Active positions will appear here</div>
                                </td>
                            </tr>
                        </tbody>
                    </table>
                </div>
            </div>

            <div class="table-container">
                <div class="table-header">
                    <h3 class="table-title">Trade History</h3>
                    <div class="table-actions">
                        <button class="btn btn-ghost">
                            <i class="fas fa-calendar"></i>
                            Date Range
                        </button>
                        <button class="btn btn-ghost">
                            <i class="fas fa-download"></i>
                            Export
                        </button>
                    </div>
                </div>
                <div class="table-wrapper">
                    <table>
                        <thead>
                            <tr>
                                <th>Market</th>
                                <th>Direction</th>
                                <th>Entry</th>
                                <th>Exit</th>
                                <th>P&L</th>
                                <th>Duration</th>
                                <th>Strategy</th>
                                <th>Status</th>
                            </tr>
                        </thead>
                        <tbody id="trades-body">
                            <tr>
                                <td colspan="8" class="empty-state">
                                    <div class="empty-state-icon">📋</div>
                                    <div class="empty-state-title">No Trade History</div>
                                    <div class="empty-state-description">Completed trades will appear here</div>
                                </td>
                            </tr>
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- Analytics Tab -->
        <div class="tab-content" data-tab="analytics">
            <div class="chart-container">
                <div class="chart-header">
                    <h3 class="chart-title">Strategy Performance</h3>
                    <p class="chart-subtitle">Win rates and profitability by strategy</p>
                </div>
                <canvas id="strategy-chart" width="400" height="200"></canvas>
            </div>

            <div class="stats-grid">
                <div class="stat-card success">
                    <div class="stat-header">
                        <div>
                            <div class="stat-title">Best Strategy</div>
                            <div class="stat-value" id="best-strategy">N/A</div>
                            <div class="stat-subtitle" id="best-strategy-stats">0% win rate</div>
                        </div>
                        <div class="stat-icon">
                            <i class="fas fa-trophy"></i>
                        </div>
                    </div>
                </div>

                <div class="stat-card danger">
                    <div class="stat-header">
                        <div>
                            <div class="stat-title">Worst Strategy</div>
                            <div class="stat-value" id="worst-strategy">N/A</div>
                            <div class="stat-subtitle" id="worst-strategy-stats">0% win rate</div>
                        </div>
                        <div class="stat-icon">
                            <i class="fas fa-exclamation-triangle"></i>
                        </div>
                    </div>
                </div>
            </div>
        </div>

        <!-- Settings Tab -->
        <div class="tab-content" data-tab="settings">
            <div class="table-container">
                <div class="table-header">
                    <h3 class="table-title">Bot Controls</h3>
                </div>
                <div style="padding: 2rem; display: flex; gap: 1rem; flex-wrap: wrap;">
                    <button class="btn btn-success" id="btn-start" onclick="startBot()">
                        <i class="fas fa-play"></i>
                        Start Bot
                    </button>
                    <button class="btn btn-danger" id="btn-stop" onclick="stopBot()">
                        <i class="fas fa-stop"></i>
                        Stop Bot
                    </button>
                    <button class="btn btn-danger" onclick="killSwitch()">
                        <i class="fas fa-exclamation-triangle"></i>
                        Emergency Stop
                    </button>
                    <button class="btn btn-ghost" onclick="refreshData()">
                        <i class="fas fa-sync-alt"></i>
                        Refresh Data
                    </button>
                </div>
            </div>

            <div class="table-container">
                <div class="table-header">
                    <h3 class="table-title">System Status</h3>
                </div>
                <div class="table-wrapper">
                    <table>
                        <tbody id="system-status-body">
                            <tr>
                                <td>Status</td>
                                <td><span class="pill info">Loading...</span></td>
                            </tr>
                        </tbody>
                    </table>
                </div>
            </div>
        </div>
    </main>

    <script>
        // Tab Navigation
        document.querySelectorAll('.nav-tab').forEach(tab => {
            tab.addEventListener('click', () => {
                const tabName = tab.dataset.tab;

                // Update tab states
                document.querySelectorAll('.nav-tab').forEach(t => t.classList.remove('active'));
                document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));

                tab.classList.add('active');
                document.querySelector(`.tab-content[data-tab="${tabName}"]`).classList.add('active');
            });
        });

        // Chart.js initialization
        let equityChart, strategyChart;

        function initCharts() {
            // Equity Chart
            const equityCtx = document.getElementById('equity-chart').getContext('2d');
            equityChart = new Chart(equityCtx, {
                type: 'line',
                data: {
                    labels: [],
                    datasets: [{
                        label: 'Portfolio Value',
                        data: [],
                        borderColor: '#667eea',
                        backgroundColor: 'rgba(102, 126, 234, 0.1)',
                        borderWidth: 2,
                        fill: true,
                        tension: 0.4
                    }]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        legend: {
                            display: false
                        }
                    },
                    scales: {
                        x: {
                            grid: {
                                color: 'rgba(255,255,255,0.1)'
                            },
                            ticks: {
                                color: '#a1a1aa'
                            }
                        },
                        y: {
                            grid: {
                                color: 'rgba(255,255,255,0.1)'
                            },
                            ticks: {
                                color: '#a1a1aa',
                                callback: function(value) {
                                    return '$' + value.toFixed(2);
                                }
                            }
                        }
                    }
                }
            });

            // Strategy Chart
            const strategyCtx = document.getElementById('strategy-chart').getContext('2d');
            strategyChart = new Chart(strategyCtx, {
                type: 'bar',
                data: {
                    labels: [],
                    datasets: [{
                        label: 'Win Rate %',
                        data: [],
                        backgroundColor: 'rgba(102, 126, 234, 0.8)',
                        borderColor: '#667eea',
                        borderWidth: 1
                    }]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        legend: {
                            display: false
                        }
                    },
                    scales: {
                        y: {
                            beginAtZero: true,
                            max: 100,
                            grid: {
                                color: 'rgba(255,255,255,0.1)'
                            },
                            ticks: {
                                color: '#a1a1aa',
                                callback: function(value) {
                                    return value + '%';
                                }
                            }
                        },
                        x: {
                            grid: {
                                color: 'rgba(255,255,255,0.1)'
                            },
                            ticks: {
                                color: '#a1a1aa'
                            }
                        }
                    }
                }
            });
        }

        // API Functions
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

        // Data Refresh
        async function refreshData() {
            try {
                // Get portfolio data
                const portfolio = await apiFetch('/api/portfolio');
                if (portfolio) {
                    document.getElementById('portfolio-value').textContent = `$${portfolio.bankroll?.toFixed(2) || '0.00'}`;
                    document.getElementById('total-pnl').textContent = `$${portfolio.total_pnl?.toFixed(2) || '0.00'}`;
                    document.getElementById('active-positions').textContent = portfolio.open_positions || 0;
                    document.getElementById('today-trades').textContent = portfolio.today_trades || 0;

                    // Update mode badge
                    const modeBadge = document.getElementById('mode-badge');
                    if (portfolio.live_trading_enabled) {
                        modeBadge.textContent = 'LIVE TRADING';
                        modeBadge.className = 'status-badge live';
                    } else {
                        modeBadge.textContent = 'PAPER MODE';
                        modeBadge.className = 'status-badge paper';
                    }
                }

                // Get positions
                const positions = await apiFetch('/api/positions');
                if (positions && positions.positions) {
                    const tbody = document.getElementById('positions-body');
                    if (positions.positions.length === 0) {
                        tbody.innerHTML = '<tr><td colspan="9" class="empty-state"><div class="empty-state-icon">📈</div><div class="empty-state-title">No Open Positions</div><div class="empty-state-description">Active positions will appear here</div></td></tr>';
                    } else {
                        tbody.innerHTML = positions.positions.map(p => `
                            <tr>
                                <td>${p.question?.substring(0, 30) || p.market_id || 'Unknown'}</td>
                                <td><span class="pill ${p.direction?.toLowerCase().includes('yes') ? 'success' : 'danger'}">${p.direction || 'Unknown'}</span></td>
                                <td>${p.entry_price?.toFixed(3) || '0.000'}</td>
                                <td>${p.current_price?.toFixed(3) || '0.000'}</td>
                                <td>${p.size || 0}</td>
                                <td class="${(p.pnl || 0) >= 0 ? 'text-green-400' : 'text-red-400'}">$${(p.pnl || 0).toFixed(2)}</td>
                                <td class="${(p.pnl_pct || 0) >= 0 ? 'text-green-400' : 'text-red-400'}">${(p.pnl_pct || 0).toFixed(2)}%</td>
                                <td>${p.hours_held ? Math.floor(p.hours_held / 24) + 'd ' + (p.hours_held % 24).toFixed(1) + 'h' : '—'}</td>
                                <td><button class="btn btn-ghost btn-sm">Close</button></td>
                            </tr>
                        `).join('');
                    }
                }

                // Get trades
                const trades = await apiFetch('/api/trades?limit=20');
                if (trades && trades.trades) {
                    const tbody = document.getElementById('trades-body');
                    if (trades.trades.length === 0) {
                        tbody.innerHTML = '<tr><td colspan="8" class="empty-state"><div class="empty-state-icon">📋</div><div class="empty-state-title">No Trade History</div><div class="empty-state-description">Completed trades will appear here</div></td></tr>';
                    } else {
                        tbody.innerHTML = trades.trades.map(t => `
                            <tr>
                                <td>${t.question?.substring(0, 30) || t.market_id || 'Unknown'}</td>
                                <td><span class="pill ${t.direction?.toLowerCase().includes('yes') ? 'success' : 'danger'}">${t.direction || 'Unknown'}</span></td>
                                <td>${t.entry_price?.toFixed(3) || '—'}</td>
                                <td>${t.exit_price?.toFixed(3) || '—'}</td>
                                <td class="${(t.pnl || 0) >= 0 ? 'text-green-400' : 'text-red-400'}">$${(t.pnl || 0).toFixed(2)}</td>
                                <td>${t.hours_held ? Math.floor(t.hours_held / 24) + 'd ' + (t.hours_held % 24).toFixed(1) + 'h' : '—'}</td>
                                <td>${t.strategy || 'Unknown'}</td>
                                <td><span class="pill ${t.trade_status === 'CLOSED' ? 'success' : 'info'}">${t.trade_status || 'Unknown'}</span></td>
                            </tr>
                        `).join('');
                    }
                }

                // Get activity
                const activity = await apiFetch('/api/activity?limit=10');
                if (activity && activity.entries) {
                    const tbody = document.getElementById('activity-body');
                    if (activity.entries.length === 0) {
                        tbody.innerHTML = '<tr><td colspan="6" class="empty-state"><div class="empty-state-icon">📊</div><div class="empty-state-title">No Recent Activity</div><div class="empty-state-description">Trade activity will appear here</div></td></tr>';
                    } else {
                        tbody.innerHTML = activity.entries.map(e => `
                            <tr>
                                <td>${new Date(e.timestamp).toLocaleTimeString()}</td>
                                <td>${e.action || 'Unknown'}</td>
                                <td>${e.market_id?.substring(0, 20) || 'Unknown'}</td>
                                <td>${e.strategy || 'Unknown'}</td>
                                <td class="${e.pnl >= 0 ? 'text-green-400' : 'text-red-400'}">$${e.pnl?.toFixed(2) || '0.00'}</td>
                                <td><span class="pill ${e.status === 'success' ? 'success' : 'info'}">${e.status || 'Unknown'}</span></td>
                            </tr>
                        `).join('');
                    }
                }

                // Update equity chart
                const equity = await apiFetch('/api/equity');
                if (equity && equity.length > 0) {
                    const labels = equity.map(e => new Date(e.timestamp).toLocaleDateString());
                    const data = equity.map(e => e.balance || 100);
                    equityChart.data.labels = labels;
                    equityChart.data.datasets[0].data = data;
                    equityChart.update();
                }

                // Get strategies for analytics
                const strategies = await apiFetch('/api/strategies');
                if (strategies && strategies.length > 0) {
                    const labels = strategies.map(s => s.strategy);
                    const data = strategies.map(s => s.win_rate);
                    strategyChart.data.labels = labels;
                    strategyChart.data.datasets[0].data = data;
                    strategyChart.update();

                    // Update best/worst strategy
                    const best = strategies.reduce((a, b) => a.win_rate > b.win_rate ? a : b);
                    const worst = strategies.reduce((a, b) => a.win_rate < b.win_rate ? a : b);

                    document.getElementById('best-strategy').textContent = best.strategy;
                    document.getElementById('best-strategy-stats').textContent = `${best.win_rate.toFixed(1)}% win rate`;
                    document.getElementById('worst-strategy').textContent = worst.strategy;
                    document.getElementById('worst-strategy-stats').textContent = `${worst.win_rate.toFixed(1)}% win rate`;
                }

                document.getElementById('last-updated').textContent = 'Updated ' + new Date().toLocaleTimeString();
            } catch (error) {
                console.error('Refresh error:', error);
            }
        }

        // Bot Controls
        async function startBot() {
            const response = await fetch('/api/start', { method: 'POST' });
            const result = await response.json();
            if (result.message) {
                alert('✅ ' + result.message);
            } else {
                alert('❌ Failed to start bot');
            }
            refreshData();
        }

        async function stopBot() {
            const response = await fetch('/api/stop', { method: 'POST' });
            const result = await response.json();
            if (result.message) {
                alert('✅ ' + result.message);
            } else {
                alert('❌ Failed to stop bot');
            }
            refreshData();
        }

        async function killSwitch() {
            if (confirm('🚨 Activate Emergency Kill Switch? This will halt all trading immediately.')) {
                const response = await fetch('/api/kill-switch', { method: 'POST' });
                const result = await response.json();
                if (result.message) {
                    alert('✅ ' + result.message);
                } else {
                    alert('❌ Kill switch failed');
                }
                refreshData();
            }
        }

        // Initialize
        document.addEventListener('DOMContentLoaded', () => {
            initCharts();
            refreshData();

            // Auto-refresh every 30 seconds
            setInterval(refreshData, 30000);
        });
    </script>
</body>
</html>
"""


def get_db():
    """Helper to get a SupabaseDB instance."""
    return SupabaseDB()


@app.route('/')
def dashboard():
    return render_template_string(HTML_TEMPLATE)


@app.route('/api/status')
def api_status():
    try:
        db = get_db()
        result = db.client.table('kalshi_bot_status').select('*').order('id', desc=True).limit(1).execute()
        if result.data:
            row = result.data[0]
            bal = row.get('balance', 100)
            return jsonify({
                'is_running': row.get('is_running', False),
                'balance': bal,
                'daily_pnl': row.get('daily_pnl', 0),
                'trades_today': row.get('trades_today', 0),
                'active_positions': row.get('active_positions', 0),
                'last_check': row.get('last_check'),
                'roi_percent': ((bal - 100) / 100 * 100),
            })
        return jsonify({'is_running': False, 'balance': 100, 'daily_pnl': 0, 'trades_today': 0, 'active_positions': 0, 'roi_percent': 0})
    except Exception as e:
        return jsonify({'is_running': False, 'balance': 100, 'daily_pnl': 0, 'error': str(e)})


@app.route('/api/trades')
def get_trades():
    try:
        limit = int(request.args.get('limit', 50))
        db = get_db()
        result = db.client.table('kalshi_trades').select('*').order('id', desc=True).limit(limit).execute()
        return jsonify(result.data or [])
    except Exception as e:
        logger.error(f"Failed to get trades: {e}")
        return jsonify([])


@app.route('/api/strategies')
def get_strategies():
    try:
        db = get_db()
        result = db.client.table('kalshi_trades').select('*').execute()
        trades = result.data or []
        by_strat = defaultdict(lambda: {'trades': 0, 'wins': 0, 'losses': 0})
        for t in trades:
            s = t.get('strategy', 'unknown')
            by_strat[s]['trades'] += 1
            reason = (t.get('reason') or '').upper()
            if 'WIN' in reason:
                by_strat[s]['wins'] += 1
            elif 'LOSS' in reason:
                by_strat[s]['losses'] += 1
        out = []
        for name, stats in by_strat.items():
            total = stats['wins'] + stats['losses']
            out.append({
                'strategy': name,
                'trades': stats['trades'],
                'wins': stats['wins'],
                'losses': stats['losses'],
                'win_rate': (stats['wins'] / total * 100) if total > 0 else 0,
            })
        return jsonify(out)
    except Exception as e:
        logger.error(f"Failed to get strategies: {e}")
        return jsonify([])


@app.route('/api/equity')
def get_equity():
    try:
        db = get_db()
        result = db.client.table('equity_snapshots').select('*').order('id', desc=True).limit(200).execute()
        if result.data:
            return jsonify(result.data)
        # Fallback to bot status balance
        result = db.client.table('kalshi_bot_status').select('balance, last_check').order('id', desc=True).limit(100).execute()
        return jsonify(result.data or [])
    except Exception as e:
        logger.error(f"Failed to get equity: {e}")
        return jsonify([])


@app.route('/api/signals')
def get_signals():
    try:
        db = get_db()
        result = db.client.table('signal_evaluations').select('*').order('id', desc=True).limit(100).execute()
        return jsonify(result.data or [])
    except Exception as e:
        logger.error(f"Failed to get signals: {e}")
        return jsonify([])


@app.route('/api/debates')
def get_debates():
    try:
        db = get_db()
        result = db.client.table('debate_log').select('*').order('id', desc=True).limit(20).execute()
        return jsonify(result.data or [])
    except Exception as e:
        logger.error(f"Failed to get debates: {e}")
        return jsonify([])


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
        db = get_db()
        db.client.table('kill_switch').insert({'active': True, 'timestamp': datetime.utcnow().isoformat()}).execute()
        return jsonify({'message': 'Kill switch activated'})
    except Exception as e:
        logger.error(f"Failed to activate kill switch: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/kill-switch-status')
def kill_switch_status():
    try:
        db = get_db()
        result = db.client.table('kill_switch').select('*').eq('active', True).order('timestamp', desc=True).limit(1).execute()
        active = len(result.data) > 0
        return jsonify({'active': active})
    except Exception as e:
        logger.error(f"Failed to get kill switch status: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/portfolio')
def get_portfolio():
    try:
        db = get_db()
        status = db.client.table('kalshi_bot_status').select('*').order('id', desc=True).limit(1).execute()
        bal = 100.0
        daily_pnl = 0
        trades_today = 0
        active_positions = 0
        if status.data:
            row = status.data[0]
            bal = row.get('balance', 100)
            daily_pnl = row.get('daily_pnl', 0)
            trades_today = row.get('trades_today', 0)
            active_positions = row.get('active_positions', 0)

        trades = db.client.table('kalshi_trades').select('id').execute()
        total_trades = len(trades.data) if trades.data else 0

        return jsonify({
            'bankroll': bal,
            'available_capital': bal,
            'total_pnl': daily_pnl,
            'realized_pnl': daily_pnl,
            'unrealized_pnl': 0,
            'open_positions': active_positions,
            'total_invested': 0,
            'total_trades': total_trades,
            'live_trades': 0,
            'paper_trades': total_trades,
            'avg_edge': 0,
            'avg_evidence_quality': 0,
            'today_trades': trades_today,
            'daily_volume': 0,
            'live_trading_enabled': False,
            'dry_run': True,
            'roi_percent': ((bal - 100) / 100 * 100),
        })
    except Exception as e:
        logger.error(f"Failed to get portfolio: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/engine-status')
def get_engine_status():
    try:
        db = get_db()
        status = db.client.table('kalshi_bot_status').select('is_running').order('id', desc=True).limit(1).execute()
        running = status.data[0].get('is_running', False) if status.data else False
        return jsonify({
            'running': running,
            'live_trading': False,
            'paper_mode': True,
            'cycles': 0,
        })
    except Exception as e:
        logger.error(f"Failed to get engine status: {e}")
        return jsonify({'running': False, 'live_trading': False, 'paper_mode': True, 'cycles': 0})


@app.route('/api/positions')
def get_positions():
    try:
        db = get_db()
        positions = db.client.table('kalshi_positions').select('*').execute()
        return jsonify({'positions': positions.data or []})
    except Exception as e:
        logger.error(f"Failed to get positions: {e}")
        return jsonify({'positions': []})


@app.route('/api/activity')
def get_activity():
    try:
        limit = int(request.args.get('limit', 20))
        db = get_db()
        trades = db.client.table('kalshi_trades').select('*').order('id', desc=True).limit(limit).execute()

        entries = []
        for trade in trades.data or []:
            entries.append({
                'timestamp': trade.get('timestamp'),
                'action': trade.get('action', 'trade'),
                'market_id': trade.get('ticker', ''),
                'strategy': trade.get('strategy', 'unknown'),
                'pnl': trade.get('pnl', 0),
                'status': 'success',
            })

        return jsonify({'entries': entries})
    except Exception as e:
        logger.error(f"Failed to get activity: {e}")
        return jsonify({'entries': []})


@app.route('/api/system-status')
def get_system_status():
    try:
        db = get_db()
        status = db.client.table('kalshi_bot_status').select('*').order('id', desc=True).limit(1).execute()
        if status.data:
            row = status.data[0]
            return jsonify({
                'bot_running': row.get('is_running', False),
                'last_check': row.get('last_check', 'Never'),
                'balance': f"${row.get('balance', 0):.2f}",
                'daily_pnl': f"${row.get('daily_pnl', 0):.2f}",
                'trades_today': row.get('trades_today', 0),
                'active_positions': row.get('active_positions', 0),
            })
        return jsonify({'bot_running': False, 'last_check': 'Never'})
    except Exception as e:
        logger.error(f"Failed to get system status: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/export/<table>')
def export_data(table):
    allowed = ['kalshi_trades', 'signal_evaluations', 'kalshi_bot_status', 'equity_snapshots', 'debate_log']
    if table not in allowed:
        return jsonify({'error': 'Table not allowed'}), 400

    try:
        db = get_db()
        data = db.client.table(table).select('*').order('id', desc=True).limit(1000).execute()
        return jsonify({'rows': data.data or []})
    except Exception as e:
        logger.error(f"Failed to export {table}: {e}")
        return jsonify({'error': str(e)}), 500


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
        db = get_db()
        result = db.client.table('improvement_logs').select('*').order('timestamp', desc=True).limit(1).execute()
        if result.data and len(result.data) > 0:
            return jsonify(result.data[0].get('analysis_json', {}))
        return jsonify({})
    except Exception as e:
        logger.error(f"Failed to get latest analysis: {e}")
        return jsonify({'error': str(e)}), 500


def start_dashboard():
    """Start the dashboard web server (blocks — run bot in background first)."""
    port = int(os.environ.get('PORT', 5000))
    logger.info(f"Dashboard starting on port {port}")
    app.run(host='0.0.0.0', port=port)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)