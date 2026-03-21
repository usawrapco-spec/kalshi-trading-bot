# Kalshi Trading Bot

Automated trading bot for Kalshi prediction markets.

## Features

- 🤖 Automated market monitoring and trading
- 📊 Multiple trading strategies (arbitrage, momentum, event-based)
- 🛡️ Built-in risk management and position limits
- 📈 Portfolio tracking and performance logging
- 🔄 Automatic retry logic and error handling
- 📝 Detailed trade logging

## Setup

### 1. Get Kalshi API Credentials

1. Log into your Kalshi account at https://kalshi.com
2. Navigate to Settings → API
3. Generate a new API key
4. Save your Key ID and Private Key securely

### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure Environment

Copy the example environment file and add your credentials:

```bash
cp .env.example .env
```

Edit `.env` and add:
```
KALSHI_API_KEY_ID=your-key-id-here
KALSHI_PRIVATE_KEY=your-private-key-here
KALSHI_API_HOST=https://trading-api.kalshi.com
```

**Important:** Use `https://demo-api.kalshi.com` for testing!

### 4. Run the Bot

```bash
# Test mode (demo API)
python bot.py --demo

# Live trading
python bot.py
```

## Project Structure

```
kalshi-trading-bot/
├── bot.py                 # Main bot entry point
├── config.py              # Configuration management
├── strategies/            # Trading strategies
│   ├── arbitrage.py       # Cross-market arbitrage
│   ├── momentum.py        # Momentum-based trading
│   └── base.py            # Base strategy class
├── utils/
│   ├── kalshi_client.py   # Kalshi API wrapper
│   ├── risk_manager.py    # Risk management
│   └── logger.py          # Logging utilities
└── data/                  # Trade history (gitignored)
```

## Trading Strategies

### Arbitrage Strategy
Monitors multiple related markets for pricing inefficiencies.

### Momentum Strategy  
Trades based on rapid price movements and volume.

### Event-Based Strategy
Monitors external events and places trades based on news/data.

## Risk Management

The bot includes several safety features:

- **Max position size**: Limits exposure per market
- **Daily loss limit**: Stops trading after reaching loss threshold
- **Order size limits**: Prevents accidentally large orders
- **Dry-run mode**: Test strategies without real money

## Configuration

Edit `config.py` to adjust:

- Maximum position sizes
- Risk tolerance
- Trading strategies to enable
- Monitoring intervals

## Safety Notes

⚠️ **IMPORTANT:**

1. **Start with demo API** - Test thoroughly before live trading
2. **Start small** - Use minimal position sizes when starting
3. **Monitor closely** - Check bot performance regularly
4. **Set limits** - Configure max daily loss and position limits
5. **Understand markets** - Only trade markets you understand

## Logging

All trades and decisions are logged to:
- Console output (real-time)
- `logs/bot.log` (persistent)
- `data/trades.json` (trade history)

## Development

To add a new strategy:

1. Create a new file in `strategies/`
2. Inherit from `BaseStrategy`
3. Implement `analyze()` and `execute()` methods
4. Register in `config.py`

## License

MIT

## Disclaimer

This bot is for educational purposes. Trading involves risk. Only trade with money you can afford to lose.
