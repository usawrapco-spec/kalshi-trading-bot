-- Enhanced logging tables for Kalshi Trading Bot Dashboard
-- Run this in your Supabase SQL Editor after the initial schema

-- Signal log - tracks ALL signals including skipped ones
CREATE TABLE IF NOT EXISTS signal_log (
    id BIGSERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    strategy TEXT NOT NULL,
    ticker TEXT NOT NULL,
    side TEXT, -- 'yes', 'no', or null for skipped
    confidence FLOAT,
    edge FLOAT,
    market_price FLOAT,
    our_probability FLOAT,
    grok_opinion TEXT,
    claude_opinion TEXT,
    action TEXT NOT NULL, -- 'TRADE', 'SKIP', 'LEARNING'
    skip_reason TEXT,
    trade_size INTEGER,
    market_title TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Debate log - tracks Grok vs Claude decision history
CREATE TABLE IF NOT EXISTS debate_log (
    id BIGSERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ticker TEXT NOT NULL,
    market_title TEXT,
    grok_probability FLOAT,
    claude_probability FLOAT,
    grok_recommendation TEXT,
    claude_recommendation TEXT,
    agreement BOOLEAN,
    final_decision TEXT, -- 'TRADE', 'SKIP', 'HALF_SIZE'
    size_modifier FLOAT DEFAULT 1.0, -- 1.0 = full size, 0.5 = half size
    debate_reasoning TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Equity snapshots - balance over time for charting
CREATE TABLE IF NOT EXISTS equity_snapshots (
    id BIGSERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    balance DECIMAL(10,2) NOT NULL,
    open_positions INTEGER DEFAULT 0,
    unrealized_pnl DECIMAL(10,2) DEFAULT 0,
    realized_pnl DECIMAL(10,2) DEFAULT 0,
    total_trades INTEGER DEFAULT 0,
    active_trades INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Strategy performance tracking
CREATE TABLE IF NOT EXISTS strategy_performance (
    id BIGSERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    strategy_name TEXT NOT NULL,
    total_trades INTEGER DEFAULT 0,
    settled_trades INTEGER DEFAULT 0,
    wins INTEGER DEFAULT 0,
    losses INTEGER DEFAULT 0,
    total_pnl DECIMAL(10,2) DEFAULT 0,
    win_rate DECIMAL(5,2),
    avg_confidence DECIMAL(5,2),
    avg_edge DECIMAL(5,2),
    expectancy DECIMAL(5,2), -- (win_rate * avg_win) - ((1-win_rate) * avg_loss)
    consecutive_losses INTEGER DEFAULT 0,
    is_paused BOOLEAN DEFAULT FALSE,
    pause_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Risk metrics tracking
CREATE TABLE IF NOT EXISTS risk_metrics (
    id BIGSERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    kelly_fraction DECIMAL(5,4),
    cash_reserve_percentage DECIMAL(5,2),
    daily_loss_limit DECIMAL(10,2),
    current_daily_loss DECIMAL(10,2),
    max_trade_size_percentage DECIMAL(5,2),
    portfolio_concentration DECIMAL(5,2), -- max position as % of balance
    active_positions INTEGER DEFAULT 0,
    circuit_breaker_active BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Weather forecast vs actual tracking
CREATE TABLE IF NOT EXISTS weather_tracking (
    id BIGSERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    city TEXT NOT NULL,
    forecast_date DATE NOT NULL,
    forecast_high_f DECIMAL(4,1),
    actual_high_f DECIMAL(4,1),
    market_ticker TEXT,
    market_close_price DECIMAL(5,4),
    our_prediction DECIMAL(5,4),
    outcome TEXT, -- 'HIT', 'MISS', 'PENDING'
    pnl_impact DECIMAL(10,2),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Create indexes for performance
CREATE INDEX IF NOT EXISTS idx_signal_log_timestamp ON signal_log(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_signal_log_strategy ON signal_log(strategy);
CREATE INDEX IF NOT EXISTS idx_signal_log_ticker ON signal_log(ticker);
CREATE INDEX IF NOT EXISTS idx_signal_log_action ON signal_log(action);

CREATE INDEX IF NOT EXISTS idx_debate_log_timestamp ON debate_log(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_debate_log_ticker ON debate_log(ticker);

CREATE INDEX IF NOT EXISTS idx_equity_snapshots_timestamp ON equity_snapshots(timestamp ASC);

CREATE INDEX IF NOT EXISTS idx_strategy_performance_timestamp ON strategy_performance(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_strategy_performance_strategy ON strategy_performance(strategy_name);

CREATE INDEX IF NOT EXISTS idx_risk_metrics_timestamp ON risk_metrics(timestamp DESC);

CREATE INDEX IF NOT EXISTS idx_weather_tracking_forecast_date ON weather_tracking(forecast_date);
CREATE INDEX IF NOT EXISTS idx_weather_tracking_city ON weather_tracking(city);

-- Enable Row Level Security
ALTER TABLE signal_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE debate_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE equity_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE strategy_performance ENABLE ROW LEVEL SECURITY;
ALTER TABLE risk_metrics ENABLE ROW LEVEL SECURITY;
ALTER TABLE weather_tracking ENABLE ROW LEVEL SECURITY;

-- Create policies (service role can do everything)
CREATE POLICY "Service role can do everything on signal_log" ON signal_log
    FOR ALL USING (auth.role() = 'service_role');

CREATE POLICY "Service role can do everything on debate_log" ON debate_log
    FOR ALL USING (auth.role() = 'service_role');

CREATE POLICY "Service role can do everything on equity_snapshots" ON equity_snapshots
    FOR ALL USING (auth.role() = 'service_role');

CREATE POLICY "Service role can do everything on strategy_performance" ON strategy_performance
    FOR ALL USING (auth.role() = 'service_role');

CREATE POLICY "Service role can do everything on risk_metrics" ON risk_metrics
    FOR ALL USING (auth.role() = 'service_role');

CREATE POLICY "Service role can do everything on weather_tracking" ON weather_tracking
    FOR ALL USING (auth.role() = 'service_role');

-- Allow public read access for dashboard
CREATE POLICY "Public can view signal_log" ON signal_log FOR SELECT USING (true);
CREATE POLICY "Public can view debate_log" ON debate_log FOR SELECT USING (true);
CREATE POLICY "Public can view equity_snapshots" ON equity_snapshots FOR SELECT USING (true);
CREATE POLICY "Public can view strategy_performance" ON strategy_performance FOR SELECT USING (true);
CREATE POLICY "Public can view risk_metrics" ON risk_metrics FOR SELECT USING (true);
CREATE POLICY "Public can view weather_tracking" ON weather_tracking FOR SELECT USING (true);