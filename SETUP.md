# Kalshi Trading Bot - Complete Setup Guide
## Fully Separate from Your Main App

This is a **completely standalone project** with its own:
- ✅ GitHub repository
- ✅ Supabase project (separate from app.usawrapco.com)
- ✅ Deployment (VPS/Railway/Render - NOT Vercel)
- ✅ No code sharing with your shop app

---

## 🚀 Quick Setup (5 Steps)

### 1. Create NEW Supabase Project (2 minutes)

1. Go to https://supabase.com/dashboard
2. Click "New Project"
3. Name: `kalshi-trading-bot`
4. Choose region
5. Set password → **Save it!**
6. Wait for init (~2 min)

**Get credentials:**
- Settings → API
- Copy `Project URL` 
- Copy `service_role` key (NOT anon!)

### 2. Setup Database (1 minute)

1. Supabase → SQL Editor → New Query
2. Copy/paste from `supabase/migrations/001_initial_schema.sql`
3. Click RUN
4. Verify tables: kalshi_trades, kalshi_positions, kalshi_bot_status

### 3. Create GitHub Repo (1 minute)

```bash
# In this directory
git init
git add .
git commit -m "Initial commit"

# With GitHub CLI:
gh repo create kalshi-trading-bot --private --source=. --push

# OR manually:
# 1. https://github.com/new
# 2. Name: kalshi-trading-bot (PRIVATE!)
# 3. Don't initialize
# 4. Then:
git remote add origin https://github.com/YOUR-USERNAME/kalshi-trading-bot.git
git branch -M main
git push -u origin main
```

### 4. Get Kalshi API Keys (2 minutes)

1. https://kalshi.com → Login
2. Settings → API
3. Generate API Key
4. **Save Key ID and Private Key!**

### 5. Deploy (Choose ONE)

#### Option A: Railway (Easiest - 1 click)

1. Go to https://railway.app
2. New Project → Deploy from GitHub
3. Select `kalshi-trading-bot` repo
4. Add environment variables (see below)
5. Deploy!

#### Option B: Render.com

1. Go to https://render.com
2. New → Background Worker
3. Connect GitHub repo
4. Build: `pip install -r requirements.txt`
5. Start: `python bot.py`
6. Add environment variables
7. Create Service

#### Option C: DigitalOcean/VPS ($6/month)

```bash
# SSH into server
ssh root@your-ip

# Setup
apt update && apt install python3 python3-pip git -y
git clone https://github.com/YOUR-USERNAME/kalshi-trading-bot.git
cd kalshi-trading-bot
./setup.sh

# Configure
cp .env.example .env
nano .env  # Add all credentials

# Test
source venv/bin/activate
python bot.py --demo

# If works, setup systemd service (see DEPLOYMENT.md)
```

---

## 🔐 Environment Variables

**Add these to your deployment platform:**

```bash
# Kalshi API
KALSHI_API_KEY_ID=your-kalshi-key-id
KALSHI_PRIVATE_KEY=your-kalshi-private-key
KALSHI_API_HOST=https://demo-api.kalshi.com  # Start with demo!

# Supabase (NEW PROJECT - not your shop app!)
SUPABASE_URL=https://xxxxx.supabase.co
SUPABASE_SERVICE_KEY=your-service-role-key

# Risk Management (adjust as needed)
MAX_POSITION_SIZE=100
MAX_DAILY_LOSS=500
MAX_ORDER_SIZE=50

# Strategies
ENABLE_ARBITRAGE=true
ENABLE_MOMENTUM=true
CHECK_INTERVAL_SECONDS=30
```

---

## 📊 Monitor Your Bot

### Supabase Dashboard
- Table Editor → `kalshi_trades` - See all trades
- Table Editor → `kalshi_positions` - Current positions
- Table Editor → `kalshi_bot_status` - Health checks

### Platform Logs
- **Railway**: Dashboard → Deployments → Logs
- **Render**: Dashboard → Logs tab
- **VPS**: `sudo journalctl -u kalshi-bot -f`

### Local Logs (if running locally)
```bash
tail -f logs/bot_*.log
cat data/trades.json | python -m json.tool
```

---

## ⚠️ Important Safety

1. **Start with DEMO API** - Test thoroughly first!
   ```
   KALSHI_API_HOST=https://demo-api.kalshi.com
   ```

2. **Small position sizes** - Start tiny, scale up slowly

3. **Monitor daily** - Check Supabase for unexpected behavior

4. **Set strict limits** - MAX_DAILY_LOSS protects you

5. **Go live carefully:**
   - After 1+ week demo testing
   - Change to: `KALSHI_API_HOST=https://trading-api.kalshi.com`
   - Start with $50-100 max exposure
   - Watch for 24 hours before increasing

---

## 🔧 Customization

### Add New Strategy

1. Create `strategies/your_strategy.py`:
```python
from strategies.base import BaseStrategy

class YourStrategy(BaseStrategy):
    def analyze(self, markets):
        # Your logic
        return signals
    
    def execute(self, signal, dry_run=False):
        # Execute trade
        pass
```

2. Add to `bot.py`:
```python
from strategies.your_strategy import YourStrategy

# In _initialize_strategies():
if Config.ENABLE_YOUR_STRATEGY:
    self.strategies.append(YourStrategy(self.client, self.risk_manager, self.db))
```

3. Add to `.env`:
```
ENABLE_YOUR_STRATEGY=true
```

### Adjust Risk Limits

Edit `.env` or deployment environment variables:
```bash
MAX_POSITION_SIZE=50     # Max contracts per market
MAX_DAILY_LOSS=250       # Stop trading if lose this much
MAX_ORDER_SIZE=25        # Max single order size
CHECK_INTERVAL_SECONDS=60  # Check markets every 60s
```

---

## 🐛 Troubleshooting

**Bot not starting:**
```bash
# Check logs for error
# Railway/Render: Dashboard → Logs
# VPS: sudo journalctl -u kalshi-bot -n 50
```

**"Supabase connection failed":**
- Verify SUPABASE_URL and SUPABASE_SERVICE_KEY
- Check Supabase project is running
- Use service_role key, not anon

**"No trading opportunities found":**
- This is normal! Opportunities are rare
- Markets need inefficiencies
- Lower thresholds in strategies if testing

**High error rate:**
- Check Kalshi API credentials
- Verify using correct API host (demo vs live)
- Check rate limits

---

## 📁 Project Structure

```
kalshi-trading-bot/
├── bot.py                    # Main entry point
├── config.py                 # Configuration
├── requirements.txt          # Dependencies
│
├── strategies/               # Trading strategies
│   ├── base.py              # Base class
│   ├── arbitrage.py         # Arbitrage strategy
│   └── momentum.py          # Momentum strategy
│
├── utils/                    # Utilities
│   ├── kalshi_client.py     # Kalshi API wrapper
│   ├── risk_manager.py      # Risk management
│   ├── logger.py            # Logging
│   └── supabase_db.py       # Supabase integration
│
├── supabase/                 # Database
│   └── migrations/
│       └── 001_initial_schema.sql
│
├── data/                     # Local trade logs
├── logs/                     # Local logs
│
└── docs/
    ├── README.md            # This file
    ├── QUICKSTART.md        # 5-min guide
    ├── DEPLOYMENT.md        # Detailed deployment
    └── CLAUDE_CODE_SETUP.md # Claude Code automation
```

---

## 💡 Tips

- **Test in demo first** - Can't stress this enough
- **Start small** - Increase size gradually
- **Monitor Supabase** - Real-time trade data
- **Check daily** - Don't set and forget
- **Keep private** - GitHub repo should be PRIVATE

---

## 📞 Support

Check these in order:
1. Logs (deployment platform or local)
2. Supabase tables for unexpected data
3. DEPLOYMENT.md for detailed troubleshooting
4. GitHub Issues

---

## 🎯 Next Steps

1. [ ] Create Supabase project
2. [ ] Run database migration
3. [ ] Create GitHub repository
4. [ ] Get Kalshi API keys
5. [ ] Deploy to Railway/Render/VPS
6. [ ] Set environment variables
7. [ ] Test in demo mode (1+ week)
8. [ ] Monitor Supabase dashboard
9. [ ] Go live (carefully!)
10. [ ] Customize strategies

Good luck! 🚀
