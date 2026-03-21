#!/usr/bin/env python3
"""Command-line interface for running Kalshi strategy backtests."""

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Any

# Add the current directory to Python path for imports
sys.path.insert(0, str(Path(__file__).parent))

from utils.backtester import KalshiBacktester, run_strategy_backtest
from strategies.weather_edge import WeatherEdgeStrategy
from strategies.prob_arb import ProbabilityArbStrategy
from strategies.sports_no import SportsNOStrategy
from strategies.grok_news import GrokNewsStrategy
from strategies.near_certainty import NearCertaintyStrategy
from strategies.mention_markets import MentionMarketsStrategy
from strategies.high_prob_lock import HighProbLockStrategy
from strategies.orderbook_edge import OrderBookEdgeStrategy
from strategies.cross_platform import CrossPlatformEdgeStrategy
from strategies.market_making import MarketMakingStrategy

# Strategy registry
STRATEGIES = {
    'weather': WeatherEdgeStrategy,
    'prob_arb': ProbabilityArbStrategy,
    'sports_no': SportsNOStrategy,
    'grok_news': GrokNewsStrategy,
    'near_certainty': NearCertaintyStrategy,
    'mention_markets': MentionMarketsStrategy,
    'high_prob_lock': HighProbLockStrategy,
    'orderbook_edge': OrderBookEdgeStrategy,
    'cross_platform': CrossPlatformEdgeStrategy,
    'market_making': MarketMakingStrategy,
}

def format_metrics(metrics: Dict[str, float]) -> str:
    """Format metrics for display."""
    if not metrics:
        return "No metrics available"

    lines = []
    lines.append("📊 PERFORMANCE METRICS")
    lines.append("=" * 50)

    # Returns
    lines.append("Returns:")
    lines.append(f"  Total Return: {metrics.get('total_return', 0):.2%}")
    lines.append(f"  Annual Return: {metrics.get('annual_return', 0):.2%}")

    # Risk metrics
    lines.append("\nRisk Metrics:")
    lines.append(f"  Volatility: {metrics.get('volatility', 0):.2%}")
    lines.append(f"  Sharpe Ratio: {metrics.get('sharpe_ratio', 0):.2f}")
    lines.append(f"  Max Drawdown: {metrics.get('max_drawdown', 0):.2%}")
    lines.append(f"  Calmar Ratio: {metrics.get('calmar_ratio', 0):.2f}")

    # Trade metrics
    lines.append("\nTrade Metrics:")
    lines.append(f"  Total Trades: {int(metrics.get('num_trades', 0))}")
    lines.append(f"  Win Rate: {metrics.get('win_rate', 0):.1%}")
    lines.append(f"  Profit Factor: {metrics.get('profit_factor', float('inf')):.2f}")
    lines.append(f"  Avg Win: ${metrics.get('avg_win', 0):.2f}")
    lines.append(f"  Avg Loss: ${metrics.get('avg_loss', 0):.2f}")
    lines.append(f"  Largest Win: ${metrics.get('largest_win', 0):.2f}")
    lines.append(f"  Largest Loss: ${metrics.get('largest_loss', 0):.2f}")

    return "\n".join(lines)

def run_single_backtest(strategy_name: str, start_date: str, end_date: str,
                        capital: float, output_file: str = None) -> None:
    """Run a backtest for a single strategy."""
    if strategy_name not in STRATEGIES:
        print(f"❌ Unknown strategy: {strategy_name}")
        print(f"Available strategies: {', '.join(STRATEGIES.keys())}")
        return

    strategy_class = STRATEGIES[strategy_name]

    print(f"🚀 Running backtest for {strategy_name}")
    print(f"📅 Period: {start_date} to {end_date}")
    print(f"💰 Starting Capital: ${capital}")
    print("-" * 50)

    try:
        result = run_strategy_backtest(
            strategy_class=strategy_class,
            start_date=start_date,
            end_date=end_date,
            capital=capital
        )

        # Display results
        print(format_metrics(result['metrics']))

        # Save detailed results if requested
        if output_file:
            output_data = {
                'strategy': result['strategy'],
                'start_date': start_date,
                'end_date': end_date,
                'initial_capital': capital,
                'metrics': result['metrics'],
                'trades': result['trades'],
                'capital_history': result['capital_history'],
                'run_timestamp': datetime.now().isoformat(),
            }

            with open(output_file, 'w') as f:
                json.dump(output_data, f, indent=2, default=str)

            print(f"\n💾 Detailed results saved to: {output_file}")

    except Exception as e:
        print(f"❌ Backtest failed: {e}")
        import traceback
        traceback.print_exc()

def run_comparison(strategies: List[str], start_date: str, end_date: str,
                  capital: float, output_file: str = None) -> None:
    """Run backtests for multiple strategies and compare results."""
    print(f"🚀 Running comparison backtest for {len(strategies)} strategies")
    print(f"📅 Period: {start_date} to {end_date}")
    print(f"💰 Starting Capital: ${capital}")
    print("-" * 50)

    results = []
    valid_strategies = []

    # Run backtests for each strategy
    for strategy_name in strategies:
        if strategy_name not in STRATEGIES:
            print(f"⚠️  Skipping unknown strategy: {strategy_name}")
            continue

        try:
            print(f"Running {strategy_name}...")
            result = run_strategy_backtest(
                strategy_class=STRATEGIES[strategy_name],
                start_date=start_date,
                end_date=end_date,
                capital=capital
            )
            results.append(result)
            valid_strategies.append(strategy_name)

        except Exception as e:
            print(f"❌ Failed to backtest {strategy_name}: {e}")

    if not results:
        print("❌ No strategies completed successfully")
        return

    # Compare results
    print("\n📊 STRATEGY COMPARISON")
    print("=" * 80)

    # Header
    header = "Strategy".ljust(15) + "Return".rjust(8) + "Sharpe".rjust(8) + "MaxDD".rjust(8) + "WinRate".rjust(8) + "Trades".rjust(6)
    print(header)
    print("-" * 80)

    # Rows
    for i, result in enumerate(results):
        metrics = result['metrics']
        strategy_name = valid_strategies[i]

        row = (
            strategy_name[:14].ljust(15) +
            f"{metrics.get('total_return', 0):.1%}".rjust(8) +
            f"{metrics.get('sharpe_ratio', 0):.2f}".rjust(8) +
            f"{metrics.get('max_drawdown', 0):.1%}".rjust(8) +
            f"{metrics.get('win_rate', 0):.0%}".rjust(8) +
            str(int(metrics.get('num_trades', 0))).rjust(6)
        )
        print(row)

    # Find best performer
    best_idx = max(range(len(results)),
                   key=lambda i: results[i]['metrics'].get('sharpe_ratio', -999))
    best_strategy = valid_strategies[best_idx]
    best_sharpe = results[best_idx]['metrics'].get('sharpe_ratio', 0)

    print("-" * 80)
    print(f"🏆 Best Sharpe Ratio: {best_strategy} ({best_sharpe:.2f})")

    # Save comparison if requested
    if output_file:
        comparison_data = {
            'comparison_type': 'multi_strategy',
            'strategies': valid_strategies,
            'start_date': start_date,
            'end_date': end_date,
            'initial_capital': capital,
            'results': results,
            'run_timestamp': datetime.now().isoformat(),
        }

        with open(output_file, 'w') as f:
            json.dump(comparison_data, f, indent=2, default=str)

        print(f"\n💾 Comparison results saved to: {output_file}")

def main():
    parser = argparse.ArgumentParser(description="Kalshi Strategy Backtester")
    parser.add_argument('strategy', nargs='?', help="Strategy to backtest (or 'all' for comparison)")
    parser.add_argument('--start-date', default='2024-01-01', help="Start date (YYYY-MM-DD)")
    parser.add_argument('--end-date', default='2024-12-31', help="End date (YYYY-MM-DD)")
    parser.add_argument('--capital', type=float, default=100.0, help="Starting capital")
    parser.add_argument('--output', '-o', help="Output file for detailed results")
    parser.add_argument('--list-strategies', action='store_true', help="List available strategies")

    args = parser.parse_args()

    if args.list_strategies:
        print("Available strategies:")
        for name, cls in STRATEGIES.items():
            print(f"  {name}: {cls.__name__}")
        return

    if not args.strategy:
        parser.print_help()
        return

    # Validate dates
    try:
        start = datetime.fromisoformat(args.start_date)
        end = datetime.fromisoformat(args.end_date)
        if start >= end:
            print("❌ Start date must be before end date")
            return
    except ValueError as e:
        print(f"❌ Invalid date format: {e}")
        return

    if args.strategy == 'all':
        # Run comparison of all strategies
        run_comparison(
            strategies=list(STRATEGIES.keys()),
            start_date=args.start_date,
            end_date=args.end_date,
            capital=args.capital,
            output_file=args.output
        )
    else:
        # Run single strategy backtest
        run_single_backtest(
            strategy_name=args.strategy,
            start_date=args.start_date,
            end_date=args.end_date,
            capital=args.capital,
            output_file=args.output
        )

if __name__ == '__main__':
    main()