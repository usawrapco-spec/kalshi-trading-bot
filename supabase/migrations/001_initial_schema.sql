-- Kalshi Paper Trading Bot — Single Table Schema
CREATE TABLE IF NOT EXISTS trades (
    id SERIAL PRIMARY KEY,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    action TEXT NOT NULL,
    price NUMERIC NOT NULL,
    count INTEGER DEFAULT 1,
    pnl NUMERIC,
    strategy TEXT,
    reason TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_trades_action_pnl ON trades(action, pnl);
CREATE INDEX IF NOT EXISTS idx_trades_ticker_side ON trades(ticker, side);
