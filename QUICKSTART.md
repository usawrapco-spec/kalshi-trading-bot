# Quick Start Guide

Get your Kalshi bot running in 5 minutes.

## Step 1: Get API Credentials

1. Go to https://kalshi.com
2. Log in to your account
3. Navigate to **Settings → API**
4. Click **Generate API Key**
5. Save your **Key ID** and **Private Key** somewhere safe

## Step 2: Install

```bash
# Run the setup script
./setup.sh

# Or manually:
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Step 3: Configure

```bash
# Copy example config
cp .env.example .env

# Edit .env and add your credentials
nano .env  # or use your favorite editor
```

Your `.env` should look like:
```
KALSHI_API_KEY_ID=your-actual-key-id
KALSHI_PRIVATE_KEY=your-actual-private-key
KALSHI_API_HOST=https://demo-api.kalshi.com
```

## Step 4: Test in Demo Mode

```bash
# Activate virtual environment (if not already active)
source venv/bin/activate

# Run in demo mode (no real money)
python bot.py --demo
```

You should see:
```
====================================================
KALSHI TRADING BOT STARTING
====================================================
⚠️  Using DEMO API - No real money at risk
✅ Kalshi client initialized successfully
...
🚀 Bot is now running. Press Ctrl+C to stop.
```

## Step 5: Monitor

Watch the console output. The bot will:
- Fetch open markets every 30 seconds
- Analyze them with enabled strategies
- Log any trading opportunities found
- In demo mode, it won't place actual orders

Press `Ctrl+C` to stop the bot.

## Going Live (BE CAREFUL!)

**Only after testing thoroughly in demo mode:**

1. Edit `.env` and change:
   ```
   KALSHI_API_HOST=https://trading-api.kalshi.com
   ```

2. Start with very small position sizes in config

3. Run without --demo flag:
   ```bash
   python bot.py
   ```

## Troubleshooting

**"API credentials not found"**
- Check your .env file exists
- Verify KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY are set

**"No trading opportunities found"**
- This is normal! Opportunities are rare
- Markets need to have inefficiencies to trade
- Try lowering thresholds in strategies/

**"Daily loss limit reached"**
- Risk manager working as intended
- Check data/trades.json to see what happened
- Will reset tomorrow

## What's Next?

- Read the full [README.md](README.md) for details
- Customize strategies in `strategies/`
- Adjust risk limits in `.env`
- Monitor `logs/` and `data/` directories
- Add your own strategies!

## Need Help?

Check the logs:
```bash
tail -f logs/bot_*.log
```

Review trades:
```bash
cat data/trades.json | python -m json.tool
```
