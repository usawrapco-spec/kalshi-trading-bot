# Claude Code Setup Instructions

Complete automation script for Claude Code to set up your Kalshi trading bot with GitHub, Vercel, and Supabase.

## Step 1: Create New Supabase Project

**DO THIS MANUALLY FIRST:**
1. Go to https://supabase.com/dashboard
2. Click "New Project"
3. Name it: `kalshi-trading-bot` 
4. Choose a region
5. Set a database password (save it!)
6. Wait for project to initialize (~2 minutes)

**Get your credentials:**
- Go to Project Settings → API
- Copy `Project URL` (looks like: https://xxxxx.supabase.co)
- Copy `service_role` key (NOT anon key!)

## Step 2: Set Up Database Schema

1. In Supabase dashboard → SQL Editor
2. Click "New Query"
3. Copy/paste entire content from `supabase/migrations/001_initial_schema.sql`
4. Click "Run"
5. Verify tables created: kalshi_trades, kalshi_positions, kalshi_bot_status

## Step 3: GitHub Repository Setup

Run these commands in Claude Code:

```bash
# Navigate to the project
cd /path/to/kalshi-trading-bot

# Initialize git if not already done
git init
git add .
git commit -m "Initial commit: Kalshi trading bot"

# Create GitHub repo and push
gh repo create kalshi-trading-bot --private --source=. --push

# Or if you don't have GitHub CLI, manually:
# 1. Go to https://github.com/new
# 2. Create repo named "kalshi-trading-bot" (PRIVATE!)
# 3. Then run:
git remote add origin https://github.com/YOUR-USERNAME/kalshi-trading-bot.git
git branch -M main
git push -u origin main
```

## Step 4: Deploy to Vercel (Optional - for monitoring dashboard)

**Note:** The bot itself can't run on Vercel (needs long-running process). Vercel is only for a monitoring dashboard. Skip this if you want to run the bot on a VPS instead.

```bash
# Install Vercel CLI if needed
npm i -g vercel

# Deploy
cd /path/to/kalshi-trading-bot
vercel

# Follow prompts:
# - Link to existing project? No
# - Project name: kalshi-trading-bot
# - Directory: ./
# - Override settings? No

# Set environment variables
vercel env add KALSHI_API_KEY_ID
# Paste your Kalshi API Key ID

vercel env add KALSHI_PRIVATE_KEY  
# Paste your Kalshi Private Key

vercel env add KALSHI_API_HOST
# Enter: https://demo-api.kalshi.com (or trading-api for live)

vercel env add SUPABASE_URL
# Paste your Supabase project URL

vercel env add SUPABASE_SERVICE_KEY
# Paste your Supabase service role key

# Deploy to production
vercel --prod
```

## Step 5: VPS Deployment (Recommended for running the bot)

### Option A: DigitalOcean Droplet

1. Create droplet ($6/month)
2. SSH into server:

```bash
ssh root@your-server-ip

# Install Python
apt update
apt install python3 python3-pip python3-venv git -y

# Clone repository
git clone https://github.com/YOUR-USERNAME/kalshi-trading-bot.git
cd kalshi-trading-bot

# Setup
./setup.sh

# Configure environment
cp .env.example .env
nano .env
# Add all your credentials:
# - KALSHI_API_KEY_ID
# - KALSHI_PRIVATE_KEY
# - KALSHI_API_HOST
# - SUPABASE_URL
# - SUPABASE_SERVICE_KEY

# Test in demo mode
source venv/bin/activate
python bot.py --demo

# If it works, set up as systemd service
sudo nano /etc/systemd/system/kalshi-bot.service
```

**Paste this into the service file:**
```ini
[Unit]
Description=Kalshi Trading Bot
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/kalshi-trading-bot
Environment="PATH=/root/kalshi-trading-bot/venv/bin"
ExecStart=/root/kalshi-trading-bot/venv/bin/python bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

**Enable and start:**
```bash
sudo systemctl daemon-reload
sudo systemctl enable kalshi-bot
sudo systemctl start kalshi-bot

# Check status
sudo systemctl status kalshi-bot

# View logs
sudo journalctl -u kalshi-bot -f
```

### Option B: Railway.app (Easiest)

1. Go to https://railway.app
2. Click "New Project" → "Deploy from GitHub"
3. Select your `kalshi-trading-bot` repository
4. Add environment variables in Railway dashboard
5. Deploy automatically runs!

### Option C: Render.com

1. Go to https://render.com
2. New → Background Worker
3. Connect GitHub repo
4. Build Command: `pip install -r requirements.txt`
5. Start Command: `python bot.py`
6. Add environment variables
7. Create service

## Step 6: Get Kalshi API Credentials

1. Go to https://kalshi.com
2. Settings → API
3. Generate new API key
4. Copy Key ID and Private Key
5. **Start with demo API first!** (`https://demo-api.kalshi.com`)

## Step 7: Environment Variables Checklist

Make sure these are set in your .env file (local) or deployment platform:

- [ ] `KALSHI_API_KEY_ID` - From Kalshi dashboard
- [ ] `KALSHI_PRIVATE_KEY` - From Kalshi dashboard  
- [ ] `KALSHI_API_HOST` - Start with demo: `https://demo-api.kalshi.com`
- [ ] `SUPABASE_URL` - From Supabase project settings
- [ ] `SUPABASE_SERVICE_KEY` - From Supabase project settings (service_role)
- [ ] `MAX_POSITION_SIZE` - Default: 100
- [ ] `MAX_DAILY_LOSS` - Default: 500
- [ ] `MAX_ORDER_SIZE` - Default: 50

## Step 8: Test Everything

```bash
# Test locally first
source venv/bin/activate
python bot.py --demo

# You should see:
# ✅ Kalshi client initialized
# ✅ Supabase connected
# 🚀 Bot is now running

# Check Supabase
# Go to Supabase → Table Editor
# You should see data in kalshi_bot_status table
```

## Step 9: Go Live (ONLY AFTER THOROUGH TESTING)

1. Change `.env`:
   ```
   KALSHI_API_HOST=https://trading-api.kalshi.com
   ```
2. Start with very small limits
3. Monitor closely for first 24 hours
4. Check trades in Supabase dashboard

## Monitoring Commands

```bash
# View logs
tail -f logs/bot_*.log

# Check trades in Supabase
# Go to Supabase → Table Editor → kalshi_trades

# Check systemd service
sudo systemctl status kalshi-bot
sudo journalctl -u kalshi-bot -f

# Stop bot
sudo systemctl stop kalshi-bot

# Restart bot
sudo systemctl restart kalshi-bot
```

## Troubleshooting

**"Supabase connection failed"**
- Verify SUPABASE_URL and SUPABASE_SERVICE_KEY in .env
- Check Supabase project is running
- Make sure you're using service_role key, not anon key

**"Kalshi API error"**
- Verify API credentials
- Check if using demo vs live API correctly
- Ensure API key has trading permissions

**"Bot not making trades"**
- Check logs for "No trading opportunities found" - this is normal!
- Markets need to have inefficiencies
- Try lowering min_edge threshold in strategies

## Quick Deploy with Claude Code

Just run this entire block in Claude Code:

```bash
# 1. Create GitHub repo
gh repo create kalshi-trading-bot --private --source=. --push

# 2. Deploy to Railway (if using Railway)
# Install Railway CLI first: npm i -g @railway/cli
railway login
railway init
railway up

# 3. Add environment variables via Railway dashboard
# Then bot auto-deploys!
```

Done! Your bot should be running in the cloud with data streaming to Supabase.
