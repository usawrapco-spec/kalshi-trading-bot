-- Kalshi Trading Bot Database Schema
-- Run this in your Supabase SQL Editor

-- Trades table - stores all executed trades
CREATE TABLE IF NOT EXISTS kalshi_trades (
    id BIGSERIAL PRIMARY KEY,
    ticker TEXT NOT NULL,
    action TEXT NOT NULL, -- 'buy' or 'sell'
    side TEXT NOT NULL, -- 'yes' or 'no'
    count INTEGER NOT NULL,
    strategy TEXT, -- 'arbitrage', 'momentum', etc.
    reason TEXT,
    confidence DECIMAL(3,2),
    order_id TEXT,
    price DECIMAL(10,2),
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Positions table - current open positions
CREATE TABLE IF NOT EXISTS kalshi_positions (
    id BIGSERIAL PRIMARY KEY,
    ticker TEXT UNIQUE NOT NULL,
    position INTEGER NOT NULL DEFAULT 0, -- positive = long, negative = short
    pnl DECIMAL(10,2) DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Bot status table - health monitoring
CREATE TABLE IF NOT EXISTS kalshi_bot_status (
    id BIGSERIAL PRIMARY KEY,
    is_running BOOLEAN NOT NULL DEFAULT true,
    daily_pnl DECIMAL(10,2) DEFAULT 0,
    trades_today INTEGER DEFAULT 0,
    balance DECIMAL(10,2) DEFAULT 0,
    active_positions INTEGER DEFAULT 0,
    last_check TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Create indexes for performance
CREATE INDEX IF NOT EXISTS idx_trades_ticker ON kalshi_trades(ticker);
CREATE INDEX IF NOT EXISTS idx_trades_timestamp ON kalshi_trades(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_trades_strategy ON kalshi_trades(strategy);
CREATE INDEX IF NOT EXISTS idx_positions_ticker ON kalshi_positions(ticker);
CREATE INDEX IF NOT EXISTS idx_bot_status_last_check ON kalshi_bot_status(last_check DESC);

-- Enable Row Level Security (RLS)
ALTER TABLE kalshi_trades ENABLE ROW LEVEL SECURITY;
ALTER TABLE kalshi_positions ENABLE ROW LEVEL SECURITY;
ALTER TABLE kalshi_bot_status ENABLE ROW LEVEL SECURITY;

-- Create policies (service role can do everything)
CREATE POLICY "Service role can do everything on trades" ON kalshi_trades
    FOR ALL USING (auth.role() = 'service_role');

CREATE POLICY "Service role can do everything on positions" ON kalshi_positions
    FOR ALL USING (auth.role() = 'service_role');

CREATE POLICY "Service role can do everything on bot_status" ON kalshi_bot_status
    FOR ALL USING (auth.role() = 'service_role');

-- Optional: Allow public read access (for dashboard)
CREATE POLICY "Public can view trades" ON kalshi_trades
    FOR SELECT USING (true);

CREATE POLICY "Public can view positions" ON kalshi_positions
    FOR SELECT USING (true);

CREATE POLICY "Public can view bot_status" ON kalshi_bot_status
    FOR SELECT USING (true);
