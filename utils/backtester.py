"""Backtesting framework for Kalshi trading strategies.

Tests strategies against historical market data to evaluate performance,
risk metrics, and robustness across different market conditions.
"""

import json
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
import pandas as pd
import numpy as np
from utils.kalshi_client import KalshiAPIClient
from utils.risk_manager import RiskManager
from utils.logger import setup_logger

logger = setup_logger('backtester')

class BacktestResult:
    """Container for backtest results and metrics."""

    def __init__(self, strategy_name: str, trades: List[Dict], capital_history: List[float]):
        self.strategy_name = strategy_name
        self.trades = trades
        self.capital_history = capital_history
        self.start_capital = capital_history[0] if capital_history else 100.0

    def calculate_metrics(self) -> Dict[str, float]:
        """Calculate comprehensive performance metrics."""
        if not self.trades or not self.capital_history:
            return {}

        # Basic metrics
        total_return = (self.capital_history[-1] - self.start_capital) / self.start_capital
        num_trades = len(self.trades)
        winning_trades = [t for t in self.trades if t.get('pnl', 0) > 0]
        win_rate = len(winning_trades) / num_trades if num_trades > 0 else 0

        # Risk metrics
        returns = pd.Series(self.capital_history).pct_change().dropna()
        if len(returns) > 0:
            volatility = returns.std() * np.sqrt(252)  # Annualized
            sharpe_ratio = returns.mean() / returns.std() * np.sqrt(252) if returns.std() > 0 else 0

            # Maximum drawdown
            cumulative = (1 + returns).cumprod()
            running_max = cumulative.expanding().max()
            drawdown = (cumulative - running_max) / running_max
            max_drawdown = drawdown.min()

            # Calmar ratio (annual return / max drawdown)
            annual_return = total_return * (252 / len(returns)) if len(returns) > 0 else 0
            calmar_ratio = abs(annual_return / max_drawdown) if max_drawdown != 0 else 0
        else:
            volatility = sharpe_ratio = max_drawdown = calmar_ratio = 0

        # Trade metrics
        if winning_trades:
            avg_win = np.mean([t['pnl'] for t in winning_trades])
            largest_win = max([t['pnl'] for t in winning_trades])
        else:
            avg_win = largest_win = 0

        losing_trades = [t for t in self.trades if t.get('pnl', 0) <= 0]
        if losing_trades:
            avg_loss = np.mean([t['pnl'] for t in losing_trades])
            largest_loss = min([t['pnl'] for t in losing_trades])
        else:
            avg_loss = largest_loss = 0

        profit_factor = abs(sum(t['pnl'] for t in winning_trades) /
                          sum(t['pnl'] for t in losing_trades)) if losing_trades else float('inf')

        return {
            'total_return': total_return,
            'annual_return': annual_return,
            'volatility': volatility,
            'sharpe_ratio': sharpe_ratio,
            'max_drawdown': max_drawdown,
            'calmar_ratio': calmar_ratio,
            'win_rate': win_rate,
            'num_trades': num_trades,
            'avg_win': avg_win,
            'avg_loss': avg_loss,
            'largest_win': largest_win,
            'largest_loss': largest_loss,
            'profit_factor': profit_factor,
        }

class KalshiBacktester:
    """Backtesting engine for Kalshi strategies."""

    def __init__(self, start_date: str, end_date: str, initial_capital: float = 100.0):
        """
        Initialize backtester.

        Args:
            start_date: Start date in YYYY-MM-DD format
            end_date: End date in YYYY-MM-DD format
            initial_capital: Starting capital for backtest
        """
        self.start_date = datetime.fromisoformat(start_date)
        self.end_date = datetime.fromisoformat(end_date)
        self.initial_capital = initial_capital

        # Try to initialize client, but don't fail if credentials aren't available
        try:
            self.client = KalshiAPIClient()
            self.has_credentials = True
        except Exception as e:
            logger.warning(f"No Kalshi credentials available for backtesting: {e}")
            logger.info("Running in DEMO mode - using mock data")
            self.client = None
            self.has_credentials = False

        # Cache for historical data
        self.market_cache = {}
        self.price_cache = {}

        logger.info(f"Backtester initialized: {start_date} to {end_date}, ${initial_capital} capital")

    def load_historical_data(self, tickers: Optional[List[str]] = None) -> Dict[str, pd.DataFrame]:
        """
        Load historical market data from Kalshi API or generate demo data.

        Args:
            tickers: Specific tickers to load, or None for all active markets

        Returns:
            Dictionary of ticker -> DataFrame with historical data
        """
        if not self.has_credentials:
            return self._generate_demo_data()

        logger.info("Loading historical market data from Kalshi API...")

        # Get all markets that existed during our timeframe
        markets_data = {}

        # For each date in our range, sample markets
        current_date = self.start_date
        sample_dates = []

        while current_date <= self.end_date:
            sample_dates.append(current_date)
            current_date += timedelta(days=7)  # Sample weekly to avoid API limits

        for sample_date in sample_dates:
            try:
                # Get markets active on this date
                markets = self.client.get_markets(
                    status='active',
                    limit=500,
                    # Note: Kalshi API may not support date filtering directly
                )

                for market in markets.get('markets', []):
                    ticker = market.get('ticker')
                    if not ticker or ticker in markets_data:
                        continue

                    # Load historical prices for this market
                    try:
                        history = self.client.get_market_history(ticker, limit=1000)
                        if history and 'history' in history:
                            df = pd.DataFrame(history['history'])
                            df['timestamp'] = pd.to_datetime(df['timestamp'])
                            df = df.set_index('timestamp').sort_index()

                            # Filter to our date range
                            df = df[(df.index >= self.start_date) & (df.index <= self.end_date)]

                            if not df.empty:
                                markets_data[ticker] = df
                                markets_data[ticker + '_meta'] = market  # Store market metadata

                    except Exception as e:
                        logger.debug(f"Failed to load history for {ticker}: {e}")

                time.sleep(1)  # Rate limiting

            except Exception as e:
                logger.error(f"Failed to load markets for {sample_date}: {e}")

        logger.info(f"Loaded historical data for {len(markets_data)//2} markets")
        return markets_data

    def _generate_demo_data(self) -> Dict[str, pd.DataFrame]:
        """
        Generate realistic demo market data for backtesting when API isn't available.

        Returns:
            Dictionary with demo market data
        """
        logger.info("Generating demo market data for backtesting...")

        markets_data = {}
        np.random.seed(42)  # For reproducible results

        # Demo markets with realistic characteristics
        demo_markets = [
            {
                'ticker': 'KXDEMO1',
                'title': 'Will the S&P 500 close above 4000 on Jan 31?',
                'category': 'Stocks',
                'close_time': (self.start_date + timedelta(days=30)).isoformat(),
            },
            {
                'ticker': 'KXDEMO2',
                'title': 'Will Bitcoin exceed $50,000 by Feb 15?',
                'category': 'Crypto',
                'close_time': (self.start_date + timedelta(days=45)).isoformat(),
            },
            {
                'ticker': 'KXDEMO3',
                'title': 'Will it snow in New York on Jan 15?',
                'category': 'Weather',
                'close_time': (self.start_date + timedelta(days=14)).isoformat(),
            },
            {
                'ticker': 'KXDEMO4',
                'title': 'Will the Fed raise rates in January?',
                'category': 'Economics',
                'close_time': (self.start_date + timedelta(days=31)).isoformat(),
            },
            {
                'ticker': 'KXDEMO5',
                'title': 'Will the temperature in Miami exceed 80°F on Jan 20?',
                'category': 'Weather',
                'close_time': (self.start_date + timedelta(days=19)).isoformat(),
            },
        ]

        # Generate price history for each market
        for market in demo_markets:
            ticker = market['ticker']

            # Create date range for this market
            close_date = datetime.fromisoformat(market['close_time'].replace('Z', '+00:00'))
            start_for_market = max(self.start_date, close_date - timedelta(days=30))
            dates = pd.date_range(start_for_market, min(close_date, self.end_date), freq='D')

            if len(dates) < 2:
                continue

            # Generate realistic price movements
            base_price = 0.5
            prices = []
            current_price = base_price

            # Add some trend and volatility based on market type
            if market['category'] == 'Stocks':
                trend = 0.001  # Slight upward trend
                volatility = 0.02
            elif market['category'] == 'Crypto':
                trend = 0.002  # Stronger upward trend
                volatility = 0.05
            elif market['category'] == 'Weather':
                trend = 0.000  # No trend
                volatility = 0.03
            else:  # Economics
                trend = -0.001  # Slight downward trend
                volatility = 0.025

            for i, date in enumerate(dates):
                # Random walk with trend
                change = np.random.normal(trend, volatility)
                current_price = np.clip(current_price + change, 0.01, 0.99)
                prices.append(current_price)

            # Create DataFrame
            df = pd.DataFrame({
                'timestamp': dates,
                'yes_price': prices,
                'no_price': [1 - p for p in prices],
                'volume': np.random.randint(100, 10000, len(dates)),
            })
            df = df.set_index('timestamp')

            # Determine outcome (for settled markets)
            if close_date <= self.end_date:
                # Random outcome with slight bias toward no
                outcome = np.random.choice(['yes', 'no'], p=[0.45, 0.55])
                df['result'] = outcome
                market['result'] = outcome

            markets_data[ticker] = df
            markets_data[ticker + '_meta'] = market

        logger.info(f"Generated demo data for {len(markets_data)//2} markets")
        return markets_data

    def run_backtest(self, strategy_class, strategy_config: Optional[Dict] = None) -> BacktestResult:
        """
        Run backtest for a given strategy.

        Args:
            strategy_class: The strategy class to test
            strategy_config: Optional configuration for the strategy

        Returns:
            BacktestResult with trades and performance metrics
        """
        logger.info(f"Running backtest for {strategy_class.__name__}")

        # Initialize strategy and risk manager
        risk_manager = RiskManager()
        risk_manager.paper_balance = self.initial_capital

        if strategy_config:
            strategy = strategy_class(risk_manager, None, None, **strategy_config)
        else:
            strategy = strategy_class(risk_manager, None, None)

        # Load historical data
        historical_data = self.load_historical_data()

        trades = []
        capital_history = [self.initial_capital]

        # Simulate trading day by day
        current_date = self.start_date
        while current_date <= self.end_date:
            try:
                # Get markets active on this date
                markets_snapshot = self._get_markets_snapshot(current_date, historical_data)

                if markets_snapshot:
                    # Run strategy analysis
                    signals = strategy.analyze(markets_snapshot)

                    # Execute signals (paper trading)
                    for signal in signals:
                        if risk_manager.can_trade(signal.get('ticker'), signal.get('count', 1),
                                                signal.get('confidence', 0)):
                            success = risk_manager.record_paper_trade(
                                ticker=signal['ticker'],
                                side=signal['side'],
                                count=signal['count'],
                                entry_price=self._get_market_price(signal['ticker'], current_date, historical_data),
                                strategy=signal.get('strategy_type', 'backtest'),
                                title=signal.get('title', '')
                            )

                            if success:
                                trades.append({
                                    'date': current_date.isoformat(),
                                    'ticker': signal['ticker'],
                                    'side': signal['side'],
                                    'count': signal['count'],
                                    'entry_price': self._get_market_price(signal['ticker'], current_date, historical_data),
                                    'strategy': signal.get('strategy_type', 'backtest'),
                                    'confidence': signal.get('confidence', 0),
                                })

                # Check for settlements (markets that closed)
                self._check_settlements(current_date, historical_data, risk_manager, trades)

                # Record capital
                capital_history.append(risk_manager.paper_balance)

            except Exception as e:
                logger.error(f"Error on {current_date}: {e}")

            current_date += timedelta(days=1)

        return BacktestResult(strategy_class.__name__, trades, capital_history)

    def _get_markets_snapshot(self, date: datetime, historical_data: Dict) -> List[Dict]:
        """Get snapshot of markets active on a given date."""
        markets = []

        for key, data in historical_data.items():
            if key.endswith('_meta'):
                ticker = key[:-5]  # Remove '_meta' suffix
                if ticker in historical_data:
                    market_meta = data
                    price_data = historical_data[ticker]

                    # Check if market was active on this date
                    if date in price_data.index:
                        current_price = price_data.loc[date, 'yes_price'] if 'yes_price' in price_data.columns else 0.5

                        market = market_meta.copy()
                        market['yes_price'] = current_price
                        market['status'] = 'open'
                        markets.append(market)

        return markets

    def _get_market_price(self, ticker: str, date: datetime, historical_data: Dict) -> float:
        """Get the price for a market on a specific date."""
        if ticker in historical_data:
            df = historical_data[ticker]
            if date in df.index:
                return df.loc[date, 'yes_price'] if 'yes_price' in df.columns else 0.5
        return 0.5  # Default fallback

    def _check_settlements(self, date: datetime, historical_data: Dict,
                          risk_manager: RiskManager, trades: List[Dict]):
        """Check for market settlements and update P&L."""
        if not risk_manager.positions:
            return

        for ticker, position in list(risk_manager.positions.items()):
            # Check if market resolved on this date
            if ticker in historical_data:
                df = historical_data[ticker]
                if date in df.index and 'result' in df.columns:
                    result = df.loc[date, 'result']
                    if result is not None:
                        # Market has resolved
                        resolved_yes = result == 'yes' or result is True
                        risk_manager.settle_paper_trade(ticker, resolved_yes)

                        # Update trade record with outcome
                        for trade in trades:
                            if trade['ticker'] == ticker and 'pnl' not in trade:
                                entry_price = trade['entry_price']
                                exit_price = 1.0 if resolved_yes else 0.0
                                pnl = (exit_price - entry_price) * trade['count']
                                trade['pnl'] = pnl
                                trade['exit_date'] = date.isoformat()
                                break

    def compare_strategies(self, strategy_results: List[BacktestResult]) -> pd.DataFrame:
        """Compare multiple strategy backtest results."""
        comparison = []

        for result in strategy_results:
            metrics = result.calculate_metrics()
            metrics['strategy'] = result.strategy_name
            comparison.append(metrics)

        return pd.DataFrame(comparison).set_index('strategy')

    def walk_forward_analysis(self, strategy_class, train_window_days: int = 90,
                            test_window_days: int = 30) -> List[BacktestResult]:
        """
        Perform walk-forward analysis to test strategy robustness.

        Args:
            strategy_class: Strategy to test
            train_window_days: Days of training data
            test_window_days: Days of testing data

        Returns:
            List of backtest results for each walk-forward period
        """
        results = []
        current_train_start = self.start_date

        while current_train_start + timedelta(days=train_window_days + test_window_days) <= self.end_date:
            train_end = current_train_start + timedelta(days=train_window_days)
            test_end = train_end + timedelta(days=test_window_days)

            # Run backtest on test period
            backtester = KalshiBacktester(
                train_end.strftime('%Y-%m-%d'),
                test_end.strftime('%Y-%m-%d'),
                self.initial_capital
            )

            result = backtester.run_backtest(strategy_class)
            results.append(result)

            # Move window forward
            current_train_start += timedelta(days=test_window_days)

        return results

def run_strategy_backtest(strategy_class, start_date: str = '2024-01-01',
                         end_date: str = '2024-12-31', capital: float = 100.0) -> Dict:
    """
    Convenience function to run a complete backtest.

    Returns:
        Dictionary with results and metrics
    """
    backtester = KalshiBacktester(start_date, end_date, capital)
    result = backtester.run_backtest(strategy_class)
    metrics = result.calculate_metrics()

    return {
        'strategy': strategy_class.__name__,
        'metrics': metrics,
        'trades': result.trades,
        'capital_history': result.capital_history,
        'num_trades': len(result.trades),
    }