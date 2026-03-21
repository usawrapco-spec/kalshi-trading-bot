-- Signal Evaluations Table for Data Collection Mode
-- Run this in your Supabase SQL Editor

CREATE TABLE IF NOT EXISTS signal_evaluations (
    id BIGSERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    cycle_id TEXT,                    -- Unique ID per bot cycle
    strategy TEXT NOT NULL,
    ticker TEXT NOT NULL,
    market_title TEXT,
    event_ticker TEXT,
    side TEXT,                        -- 'yes' or 'no'
    -- Market data at time of signal
    yes_price FLOAT,
    no_price FLOAT,
    spread FLOAT,
    volume_24h FLOAT,
    time_to_close_hours FLOAT,
    -- Strategy analysis
    our_probability FLOAT,           -- What we think the probability is
    market_probability FLOAT,        -- What the market says
    edge FLOAT,                      -- our_prob - market_prob
    confidence FLOAT,                -- Strategy's confidence score
    -- AI opinions (if applicable)
    grok_probability FLOAT,
    grok_recommendation TEXT,
    claude_probability FLOAT,
    claude_recommendation TEXT,
    debate_agreement BOOLEAN,
    -- Decision
    action TEXT,                      -- 'VIRTUAL_TRADE' or 'SKIP'
    skip_reason TEXT,                 -- Why it was skipped (low edge, low volume, etc)
    virtual_trade_size FLOAT,         -- How much we "would" trade
    virtual_entry_price FLOAT,
    -- For tracking reward/risk
    potential_profit FLOAT,
    potential_loss FLOAT,
    reward_to_risk FLOAT,
    kelly_fraction FLOAT,
    -- Settlement tracking (filled in later by settlement checker)
    settled BOOLEAN DEFAULT FALSE,
    settlement_price FLOAT,          -- 0 or 1
    virtual_pnl FLOAT,              -- What we would have made/lost
    settled_at TIMESTAMPTZ,
    was_correct BOOLEAN,
    r_multiple FLOAT                 -- PnL / risk amount
);

-- Indexes for performance
CREATE INDEX IF NOT EXISTS idx_signal_evals_unsettled
ON signal_evaluations(settled, ticker) WHERE settled = FALSE;

CREATE INDEX IF NOT EXISTS idx_signal_evals_strategy
ON signal_evaluations(strategy, settled, was_correct);

CREATE INDEX IF NOT EXISTS idx_signal_evals_timestamp
ON signal_evaluations(timestamp DESC);

CREATE INDEX IF NOT EXISTS idx_signal_evals_cycle
ON signal_evaluations(cycle_id);

CREATE INDEX IF NOT EXISTS idx_signal_evals_ticker
ON signal_evaluations(ticker);

-- Enable Row Level Security
ALTER TABLE signal_evaluations ENABLE ROW LEVEL SECURITY;

-- Create policies (service role can do everything)
CREATE POLICY "Service role can do everything on signal_evaluations" ON signal_evaluations
    FOR ALL USING (auth.role() = 'service_role');

-- Allow public read access for dashboard
CREATE POLICY "Public can view signal_evaluations" ON signal_evaluations FOR SELECT USING (true);