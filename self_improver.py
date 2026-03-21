"""
Self-Improvement Engine for Kalshi Trading Bot
Analyzes settled signal_evaluations to find what actually works.
Outputs updated strategy parameters for continuous learning.
"""

import json
from datetime import datetime, timedelta
from collections import defaultdict
import statistics

from utils.supabase_db import SupabaseDB
from utils.logger import setup_logger

logger = setup_logger('self_improver')


class SelfImprover:
    """AI that learns from trading data and improves bot parameters."""

    def __init__(self, supabase_client=None):
        self.db = supabase_client or SupabaseDB().client
        if not self.db:
            raise ValueError("Supabase client required for self-improvement")

    def run_full_analysis(self, lookback_days=7):
        """Run complete analysis and output recommendations."""
        print(f"\n{'='*80}")
        print(f"🤖 SELF-IMPROVEMENT ANALYSIS — {datetime.utcnow().isoformat()}")
        print(f"📊 Lookback: {lookback_days} days")
        print(f"{'='*80}\n")

        results = {}

        # 1. Per-strategy performance analysis
        results['strategy_performance'] = self.analyze_strategies(lookback_days)

        # 2. Optimal edge thresholds
        results['edge_thresholds'] = self.find_optimal_edge_thresholds(lookback_days)

        # 3. Optimal confidence thresholds
        results['confidence_thresholds'] = self.find_optimal_confidence_thresholds(lookback_days)

        # 4. Time-of-day analysis
        results['time_analysis'] = self.analyze_time_patterns(lookback_days)

        # 5. Volume vs win rate correlation
        results['volume_analysis'] = self.analyze_volume_correlation(lookback_days)

        # 6. AI debate accuracy analysis
        results['debate_analysis'] = self.analyze_debate_accuracy(lookback_days)

        # 7. Reward-to-risk analysis
        results['rr_analysis'] = self.analyze_reward_to_risk(lookback_days)

        # 8. Market category analysis
        results['category_analysis'] = self.analyze_market_categories(lookback_days)

        # 9. Generate new parameters
        results['new_parameters'] = self.generate_new_parameters(results)

        # Log the analysis
        self.log_analysis(results)

        # Print summary
        self.print_summary(results)

        return results

    def analyze_strategies(self, lookback_days):
        """Which strategies actually make money?"""
        cutoff = (datetime.utcnow() - timedelta(days=lookback_days)).isoformat()

        # Query settled signals grouped by strategy
        settled = self.db.table('signal_evaluations') \
            .select('strategy, was_correct, virtual_pnl, r_multiple, edge, confidence, ticker') \
            .eq('settled', True) \
            .eq('action', 'VIRTUAL_TRADE') \
            .gte('timestamp', cutoff) \
            .execute()

        # Group by strategy
        by_strategy = defaultdict(lambda: {
            'trades': 0, 'wins': 0, 'losses': 0,
            'total_pnl': 0.0, 'r_multiples': [],
            'edges': [], 'confidences': [], 'tickers': set()
        })

        for row in settled.data:
            s = row['strategy']
            stats = by_strategy[s]
            stats['trades'] += 1
            if row['was_correct']:
                stats['wins'] += 1
            else:
                stats['losses'] += 1
            stats['total_pnl'] += row['virtual_pnl'] or 0
            if row['r_multiple']:
                stats['r_multiples'].append(row['r_multiple'])
            if row['edge'] is not None:
                stats['edges'].append(row['edge'])
            if row['confidence'] is not None:
                stats['confidences'].append(row['confidence'])
            stats['tickers'].add(row['ticker'])

        # Calculate derived metrics
        for s, data in by_strategy.items():
            data['win_rate'] = data['wins'] / max(data['trades'], 1)
            data['avg_r'] = statistics.mean(data['r_multiples']) if data['r_multiples'] else 0
            data['avg_edge'] = statistics.mean(data['edges']) if data['edges'] else 0
            data['avg_confidence'] = statistics.mean(data['confidences']) if data['confidences'] else 0
            data['expectancy'] = (data['win_rate'] * abs(data['avg_r'])) - ((1 - data['win_rate']) * 1.0)
            data['total_markets'] = len(data['tickers'])

            # Determine strategy health
            if data['trades'] < 10:
                data['verdict'] = 'INSUFFICIENT_DATA'
                data['confidence_level'] = 'LOW'
            elif data['expectancy'] > 0.3:
                data['verdict'] = 'STRONG_PERFORMER'
                data['confidence_level'] = 'HIGH'
            elif data['expectancy'] > 0.1:
                data['verdict'] = 'MODERATE_PERFORMER'
                data['confidence_level'] = 'MEDIUM'
            elif data['expectancy'] > 0:
                data['verdict'] = 'WEAK_PERFORMER'
                data['confidence_level'] = 'LOW'
            else:
                data['verdict'] = 'LOSING_STRATEGY'
                data['confidence_level'] = 'HIGH'

        return dict(by_strategy)

    def find_optimal_edge_thresholds(self, lookback_days):
        """What minimum edge actually produces profitable trades?"""
        cutoff = (datetime.utcnow() - timedelta(days=lookback_days)).isoformat()

        settled = self.db.table('signal_evaluations') \
            .select('strategy, edge, was_correct, virtual_pnl') \
            .eq('settled', True) \
            .gte('timestamp', cutoff) \
            .execute()

        # Test different edge thresholds
        thresholds = [0.01, 0.02, 0.03, 0.05, 0.08, 0.10, 0.15, 0.20]
        results_by_strategy = defaultdict(dict)

        for row in settled.data:
            s = row['strategy']
            for t in thresholds:
                key = f"edge_{t}"
                if key not in results_by_strategy[s]:
                    results_by_strategy[s][key] = {'trades': 0, 'wins': 0, 'pnl': 0}
                if abs(row['edge'] or 0) >= t:
                    results_by_strategy[s][key]['trades'] += 1
                    if row['was_correct']:
                        results_by_strategy[s][key]['wins'] += 1
                    results_by_strategy[s][key]['pnl'] += row['virtual_pnl'] or 0

        # Find optimal threshold per strategy (highest expectancy with enough trades)
        optimal = {}
        for s, thresholds_data in results_by_strategy.items():
            best_threshold = None
            best_expectancy = -999
            for key, data in thresholds_data.items():
                if data['trades'] >= 5:  # Minimum sample size
                    wr = data['wins'] / data['trades']
                    exp = data['pnl'] / data['trades']  # Avg PnL per trade
                    if exp > best_expectancy:
                        best_expectancy = exp
                        best_threshold = float(key.replace('edge_', ''))
            optimal[s] = {
                'recommended_min_edge': best_threshold,
                'expected_pnl_per_trade': best_expectancy,
                'sample_size': thresholds_data.get(f'edge_{best_threshold}', {}).get('trades', 0)
            }

        return optimal

    def find_optimal_confidence_thresholds(self, lookback_days):
        """Same as edge but for confidence levels."""
        cutoff = (datetime.utcnow() - timedelta(days=lookback_days)).isoformat()

        settled = self.db.table('signal_evaluations') \
            .select('strategy, confidence, was_correct, virtual_pnl') \
            .eq('settled', True) \
            .gte('timestamp', cutoff) \
            .execute()

        thresholds = [0.50, 0.60, 0.70, 0.80, 0.90]
        results_by_strategy = defaultdict(dict)

        for row in settled.data:
            s = row['strategy']
            for t in thresholds:
                key = f"conf_{t}"
                if key not in results_by_strategy[s]:
                    results_by_strategy[s][key] = {'trades': 0, 'wins': 0, 'pnl': 0}
                if (row['confidence'] or 0) >= t:
                    results_by_strategy[s][key]['trades'] += 1
                    if row['was_correct']:
                        results_by_strategy[s][key]['wins'] += 1
                    results_by_strategy[s][key]['pnl'] += row['virtual_pnl'] or 0

        optimal = {}
        for s, data in results_by_strategy.items():
            best_conf = None
            best_exp = -999
            best_sample = 0
            for key, vals in data.items():
                if vals['trades'] >= 5:
                    exp = vals['pnl'] / vals['trades']
                    if exp > best_exp:
                        best_exp = exp
                        best_conf = float(key.replace('conf_', ''))
                        best_sample = vals['trades']
            optimal[s] = {
                'recommended_min_confidence': best_conf,
                'expected_pnl': best_exp,
                'sample_size': best_sample
            }

        return optimal

    def analyze_time_patterns(self, lookback_days):
        """Are certain hours more profitable? (timezone arb)"""
        cutoff = (datetime.utcnow() - timedelta(days=lookback_days)).isoformat()

        settled = self.db.table('signal_evaluations') \
            .select('timestamp, was_correct, virtual_pnl, strategy') \
            .eq('settled', True) \
            .gte('timestamp', cutoff) \
            .execute()

        by_hour = defaultdict(lambda: {'trades': 0, 'wins': 0, 'pnl': 0, 'strategies': set()})
        for row in settled.data:
            try:
                hour = datetime.fromisoformat(row['timestamp'].replace('Z', '+00:00')).hour
                by_hour[hour]['trades'] += 1
                if row['was_correct']:
                    by_hour[hour]['wins'] += 1
                by_hour[hour]['pnl'] += row['virtual_pnl'] or 0
                by_hour[hour]['strategies'].add(row['strategy'])
            except:
                continue

        # Calculate stats for each hour
        for hour, data in by_hour.items():
            data['win_rate'] = data['wins'] / max(data['trades'], 1)
            data['avg_pnl'] = data['pnl'] / max(data['trades'], 1)
            data['strategy_count'] = len(data['strategies'])

        # Find best and worst hours
        profitable_hours = [
            h for h, d in by_hour.items()
            if d.get('avg_pnl', 0) > 0 and d.get('trades', 0) >= 10
        ]

        return {
            'by_hour': dict(by_hour),
            'profitable_hours': sorted(profitable_hours),
            'best_hour': max(by_hour.items(), key=lambda x: x[1]['avg_pnl'])[0] if by_hour else None,
            'worst_hour': min(by_hour.items(), key=lambda x: x[1]['avg_pnl'])[0] if by_hour else None
        }

    def analyze_volume_correlation(self, lookback_days):
        """Do higher volume markets produce better results?"""
        cutoff = (datetime.utcnow() - timedelta(days=lookback_days)).isoformat()

        settled = self.db.table('signal_evaluations') \
            .select('volume_24h, was_correct, virtual_pnl, strategy') \
            .eq('settled', True) \
            .gte('timestamp', cutoff) \
            .execute()

        # Bucket by volume ranges
        buckets = {
            '0-10': {'trades': 0, 'wins': 0, 'pnl': 0, 'strategies': set()},
            '10-100': {'trades': 0, 'wins': 0, 'pnl': 0, 'strategies': set()},
            '100-1k': {'trades': 0, 'wins': 0, 'pnl': 0, 'strategies': set()},
            '1k+': {'trades': 0, 'wins': 0, 'pnl': 0, 'strategies': set()}
        }

        for row in settled.data:
            vol = row['volume_24h'] or 0
            if vol < 10:
                bucket = '0-10'
            elif vol < 100:
                bucket = '10-100'
            elif vol < 1000:
                bucket = '100-1k'
            else:
                bucket = '1k+'

            buckets[bucket]['trades'] += 1
            if row['was_correct']:
                buckets[bucket]['wins'] += 1
            buckets[bucket]['pnl'] += row['virtual_pnl'] or 0
            buckets[bucket]['strategies'].add(row['strategy'])

        # Calculate stats
        analysis = {}
        for bucket, data in buckets.items():
            if data['trades'] > 0:
                analysis[bucket] = {
                    'trades': data['trades'],
                    'win_rate': data['wins'] / data['trades'],
                    'total_pnl': data['pnl'],
                    'avg_pnl': data['pnl'] / data['trades'],
                    'strategy_count': len(data['strategies'])
                }

        # Determine optimal volume filter
        best_bucket = max(analysis.items(), key=lambda x: x[1]['avg_pnl'])
        recommended_min_volume = {
            '0-10': 0,
            '10-100': 10,
            '100-1k': 100,
            '1k+': 1000
        }.get(best_bucket[0], 10)

        return {
            'buckets': analysis,
            'best_volume_bucket': best_bucket[0],
            'recommended_min_volume': recommended_min_volume
        }

    def analyze_debate_accuracy(self, lookback_days):
        """Is the Grok+Claude debate actually helping?"""
        cutoff = (datetime.utcnow() - timedelta(days=lookback_days)).isoformat()

        settled = self.db.table('signal_evaluations') \
            .select('grok_recommendation, claude_recommendation, debate_agreement, was_correct, virtual_pnl, strategy') \
            .eq('settled', True) \
            .gte('timestamp', cutoff) \
            .not_('grok_recommendation', 'is', None) \
            .execute()

        categories = {
            'both_agree_trade': {'trades': 0, 'wins': 0, 'pnl': 0, 'strategies': set()},
            'grok_only': {'trades': 0, 'wins': 0, 'pnl': 0, 'strategies': set()},
            'claude_only': {'trades': 0, 'wins': 0, 'pnl': 0, 'strategies': set()},
            'both_skip': {'trades': 0, 'wins': 0, 'pnl': 0, 'strategies': set()},
            'disagree': {'trades': 0, 'wins': 0, 'pnl': 0, 'strategies': set()}
        }

        for row in settled.data:
            grok = (row.get('grok_recommendation') or '').upper()
            claude = (row.get('claude_recommendation') or '').upper()

            if 'TRADE' in grok and 'TRADE' in claude:
                cat = 'both_agree_trade'
            elif 'TRADE' in grok and 'TRADE' not in claude:
                cat = 'grok_only'
            elif 'TRADE' not in grok and 'TRADE' in claude:
                cat = 'claude_only'
            elif row.get('debate_agreement') == False:
                cat = 'disagree'
            else:
                cat = 'both_skip'

            categories[cat]['trades'] += 1
            if row['was_correct']:
                categories[cat]['wins'] += 1
            categories[cat]['pnl'] += row['virtual_pnl'] or 0
            categories[cat]['strategies'].add(row['strategy'])

        # Calculate stats
        for cat, data in categories.items():
            data['win_rate'] = data['wins'] / max(data['trades'], 1)
            data['avg_pnl'] = data['pnl'] / max(data['trades'], 1)
            data['strategy_count'] = len(data['strategies'])

        # Determine debate effectiveness
        both_agree_wr = categories['both_agree_trade']['win_rate']
        grok_only_wr = categories['grok_only']['win_rate']
        claude_only_wr = categories['claude_only']['win_rate']

        if both_agree_wr > grok_only_wr + 0.05 and both_agree_wr > claude_only_wr + 0.05:
            debate_verdict = 'AGREEMENT_IMPROVES_ACCURACY'
        elif grok_only_wr > both_agree_wr + 0.05:
            debate_verdict = 'GROK_SOLO_BETTER'
        elif claude_only_wr > both_agree_wr + 0.05:
            debate_verdict = 'CLAUDE_SOLO_BETTER'
        else:
            debate_verdict = 'DEBATE_NEUTRAL'

        return {
            'categories': categories,
            'debate_verdict': debate_verdict,
            'grok_accuracy': grok_only_wr,
            'claude_accuracy': claude_only_wr,
            'agreement_accuracy': both_agree_wr
        }

    def analyze_reward_to_risk(self, lookback_days):
        """What R:R ratio actually produces winners?"""
        cutoff = (datetime.utcnow() - timedelta(days=lookback_days)).isoformat()

        settled = self.db.table('signal_evaluations') \
            .select('reward_to_risk, was_correct, virtual_pnl, strategy') \
            .eq('settled', True) \
            .gte('timestamp', cutoff) \
            .execute()

        # Bucket by R:R ranges
        buckets = {
            '<1': {'trades': 0, 'wins': 0, 'pnl': 0, 'strategies': set()},
            '1-2': {'trades': 0, 'wins': 0, 'pnl': 0, 'strategies': set()},
            '2-3': {'trades': 0, 'wins': 0, 'pnl': 0, 'strategies': set()},
            '3-5': {'trades': 0, 'wins': 0, 'pnl': 0, 'strategies': set()},
            '5+': {'trades': 0, 'wins': 0, 'pnl': 0, 'strategies': set()}
        }

        for row in settled.data:
            rr = row['reward_to_risk'] or 0
            if rr < 1:
                bucket = '<1'
            elif rr < 2:
                bucket = '1-2'
            elif rr < 3:
                bucket = '2-3'
            elif rr < 5:
                bucket = '3-5'
            else:
                bucket = '5+'

            buckets[bucket]['trades'] += 1
            if row['was_correct']:
                buckets[bucket]['wins'] += 1
            buckets[bucket]['pnl'] += row['virtual_pnl'] or 0
            buckets[bucket]['strategies'].add(row['strategy'])

        # Calculate stats
        analysis = {}
        for bucket, data in buckets.items():
            if data['trades'] > 0:
                analysis[bucket] = {
                    'trades': data['trades'],
                    'win_rate': data['wins'] / data['trades'],
                    'total_pnl': data['pnl'],
                    'avg_pnl': data['pnl'] / data['trades'],
                    'strategy_count': len(data['strategies'])
                }

        # Find optimal R:R threshold
        best_bucket = max(analysis.items(), key=lambda x: x[1]['avg_pnl'])
        recommended_min_rr = {
            '<1': 0.5,
            '1-2': 1.0,
            '2-3': 2.0,
            '3-5': 3.0,
            '5+': 5.0
        }.get(best_bucket[0], 2.0)

        return {
            'buckets': analysis,
            'best_rr_bucket': best_bucket[0],
            'recommended_min_rr': recommended_min_rr
        }

    def analyze_market_categories(self, lookback_days):
        """Which market categories perform best?"""
        cutoff = (datetime.utcnow() - timedelta(days=lookback_days)).isoformat()

        # This would require market category data from Kalshi API
        # For now, return placeholder
        return {
            'note': 'Market category analysis requires category field in signal_evaluations table',
            'categories': {}
        }

    def generate_new_parameters(self, analysis_results):
        """Based on all analysis, generate updated bot parameters."""
        params = {
            'generated_at': datetime.utcnow().isoformat(),
            'analysis_lookback_days': 7,
            'strategy_allocations': {},
            'strategy_min_edge': {},
            'strategy_min_confidence': {},
            'strategy_enabled': {},
            'best_trading_hours': [],
            'min_volume_filter': 10,
            'min_reward_to_risk': 2.0,
            'debate_mode': 'grok_leads',
            'data_collection_mode': False
        }

        # Strategy allocations based on performance
        strat_perf = analysis_results.get('strategy_performance', {})
        total_positive_exp = sum(
            max(s.get('expectancy', 0), 0)
            for s in strat_perf.values()
            if s.get('verdict') != 'INSUFFICIENT_DATA'
        )

        for strategy, data in strat_perf.items():
            if data['verdict'] == 'LOSING_STRATEGY':
                params['strategy_allocations'][strategy] = 0.02  # 2% min
                params['strategy_enabled'][strategy] = True  # Keep collecting data
            elif data['verdict'] == 'INSUFFICIENT_DATA':
                params['strategy_allocations'][strategy] = 0.08  # Give it room to prove itself
                params['strategy_enabled'][strategy] = True
            elif data['verdict'] in ['STRONG_PERFORMER', 'MODERATE_PERFORMER']:
                # Proportional to expectancy
                exp = max(data.get('expectancy', 0), 0.01)
                allocation = round(exp / max(total_positive_exp, 0.01), 2)
                params['strategy_allocations'][strategy] = max(allocation, 0.05)  # Min 5%
                params['strategy_enabled'][strategy] = True
            else:
                params['strategy_allocations'][strategy] = 0.05
                params['strategy_enabled'][strategy] = True

        # Edge thresholds from analysis
        edge_data = analysis_results.get('edge_thresholds', {})
        for strategy, data in edge_data.items():
            if data.get('recommended_min_edge') and data.get('sample_size', 0) >= 10:
                params['strategy_min_edge'][strategy] = data['recommended_min_edge']

        # Confidence thresholds
        conf_data = analysis_results.get('confidence_thresholds', {})
        for strategy, data in conf_data.items():
            if data.get('recommended_min_confidence') and data.get('sample_size', 0) >= 10:
                params['strategy_min_confidence'][strategy] = data['recommended_min_confidence']

        # Best trading hours
        time_data = analysis_results.get('time_analysis', {})
        profitable_hours = time_data.get('profitable_hours', [])
        if profitable_hours:
            params['best_trading_hours'] = profitable_hours

        # Volume filter
        vol_data = analysis_results.get('volume_analysis', {})
        if vol_data.get('recommended_min_volume') is not None:
            params['min_volume_filter'] = vol_data['recommended_min_volume']

        # R:R filter
        rr_data = analysis_results.get('rr_analysis', {})
        if rr_data.get('recommended_min_rr'):
            params['min_reward_to_risk'] = rr_data['recommended_min_rr']

        # Debate mode
        debate_data = analysis_results.get('debate_analysis', {})
        verdict = debate_data.get('debate_verdict', 'DEBATE_NEUTRAL')
        if verdict == 'GROK_SOLO_BETTER':
            params['debate_mode'] = 'grok_solo'
        elif verdict == 'AGREEMENT_IMPROVES_ACCURACY':
            params['debate_mode'] = 'require_agreement'
        else:
            params['debate_mode'] = 'grok_leads'

        return params

    def log_analysis(self, results):
        """Save analysis results to Supabase for dashboard display."""
        try:
            self.db.table('improvement_logs').insert({
                'timestamp': datetime.utcnow().isoformat(),
                'analysis_json': json.dumps(results, default=str),
                'strategy_verdicts': json.dumps({
                    s: d.get('verdict', 'UNKNOWN')
                    for s, d in results.get('strategy_performance', {}).items()
                }),
                'new_parameters': json.dumps(results.get('new_parameters', {}))
            }).execute()
        except Exception as e:
            logger.error(f"Failed to log analysis: {e}")

    def apply_parameters(self, params):
        """
        Apply the learned parameters to the bot's config.
        In data_collection mode: just log them.
        In live_paper mode: actually update the bot's behavior.
        """
        try:
            # Save to active_parameters table
            self.db.table('active_parameters').upsert({
                'id': 'current',
                'parameters': json.dumps(params),
                'updated_at': datetime.utcnow().isoformat()
            }).execute()

            print(f"\n✅ NEW PARAMETERS APPLIED AT {datetime.utcnow().isoformat()}:")
            print(json.dumps(params, indent=2, default=str))
            return True
        except Exception as e:
            logger.error(f"Failed to apply parameters: {e}")
            return False

    def print_summary(self, results):
        """Print a human-readable summary of the analysis."""
        perf = results.get('strategy_performance', {})

        print("🎯 STRATEGY PERFORMANCE SUMMARY:")
        for strategy, data in sorted(perf.items(), key=lambda x: x[1].get('expectancy', 0), reverse=True):
            verdict = data.get('verdict', 'UNKNOWN')
            exp = data.get('expectancy', 0)
            wr = data.get('win_rate', 0)
            trades = data.get('trades', 0)
            print(f"  {strategy}: {verdict} (Exp: {exp:.2f}, WR: {wr:.1%}, Trades: {trades})")

        params = results.get('new_parameters', {})
        print(f"\n🔧 NEW PARAMETERS GENERATED:")
        print(f"  Strategy Allocations: {params.get('strategy_allocations', {})}")
        print(f"  Min Volume Filter: {params.get('min_volume_filter', 'N/A')}")
        print(f"  Min R:R Ratio: {params.get('min_reward_to_risk', 'N/A')}")
        print(f"  Debate Mode: {params.get('debate_mode', 'N/A')}")

        print(f"\n📊 ANALYSIS COMPLETE — {len(perf)} strategies analyzed")


# CLI interface for manual analysis
if __name__ == '__main__':
    improver = SelfImprover()
    results = improver.run_full_analysis(lookback_days=7)
    print(f"\nAnalysis saved to improvement_logs table")