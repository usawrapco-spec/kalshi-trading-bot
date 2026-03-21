-- Self-Improvement System Tables
-- Run this in your Supabase SQL Editor

-- Table for storing analysis results and parameter recommendations
CREATE TABLE IF NOT EXISTS improvement_logs (
    id BIGSERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    analysis_json JSONB,                    -- Full analysis results
    strategy_verdicts JSONB,               -- Quick strategy verdicts for dashboard
    new_parameters JSONB                   -- Recommended new parameters
);

-- Table for storing currently active parameters
CREATE TABLE IF NOT EXISTS active_parameters (
    id TEXT PRIMARY KEY DEFAULT 'current',
    parameters JSONB NOT NULL,             -- Current bot parameters
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Indexes for performance
CREATE INDEX IF NOT EXISTS idx_improvement_logs_timestamp
ON improvement_logs(timestamp DESC);

CREATE INDEX IF NOT EXISTS idx_active_parameters_updated
ON active_parameters(updated_at DESC);

-- Enable Row Level Security
ALTER TABLE improvement_logs ENABLE ROW LEVEL SECURITY;
ALTER TABLE active_parameters ENABLE ROW LEVEL SECURITY;

-- Create policies (service role can do everything)
CREATE POLICY "Service role can do everything on improvement_logs" ON improvement_logs
    FOR ALL USING (auth.role() = 'service_role');

CREATE POLICY "Service role can do everything on active_parameters" ON active_parameters
    FOR ALL USING (auth.role() = 'service_role');

-- Allow public read access for dashboard
CREATE POLICY "Public can view improvement_logs" ON improvement_logs FOR SELECT USING (true);
CREATE POLICY "Public can view active_parameters" ON active_parameters FOR SELECT USING (true);

-- Insert default parameters
INSERT INTO active_parameters (id, parameters) VALUES ('current', '{
  "generated_at": "2024-01-01T00:00:00Z",
  "analysis_lookback_days": 7,
  "strategy_allocations": {
    "grok_news": 0.15,
    "weather_edge": 0.20,
    "prob_arb": 0.15,
    "sports_no": 0.10,
    "near_certainty": 0.10,
    "mention_markets": 0.10,
    "high_prob_lock": 0.10,
    "orderbook_edge": 0.05,
    "cross_platform": 0.03,
    "market_making": 0.02
  },
  "strategy_min_edge": {},
  "strategy_min_confidence": {},
  "strategy_enabled": {
    "grok_news": true,
    "weather_edge": true,
    "prob_arb": true,
    "sports_no": true,
    "near_certainty": true,
    "mention_markets": true,
    "high_prob_lock": true,
    "orderbook_edge": true,
    "cross_platform": true,
    "market_making": true
  },
  "best_trading_hours": [],
  "min_volume_filter": 10,
  "min_reward_to_risk": 2.0,
  "debate_mode": "grok_leads",
  "data_collection_mode": false
}'::jsonb) ON CONFLICT (id) DO NOTHING;