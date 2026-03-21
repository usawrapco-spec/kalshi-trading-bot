# ✅ Deployment Checklist

Use this to track your deployment progress.

---

## Pre-Deployment (5 minutes)

- [ ] Downloaded kalshi-trading-bot-complete.tar.gz
- [ ] Extracted files
- [ ] Read ONE_CLICK_DEPLOY.md

---

## Supabase Setup (2 minutes)

- [ ] Created NEW Supabase project at https://supabase.com/dashboard
  - [ ] Name: `kalshi-trading-bot`
  - [ ] Saved database password
  - [ ] Waited for project init

- [ ] Ran database migration
  - [ ] Opened SQL Editor
  - [ ] Copied from `supabase/migrations/001_initial_schema.sql`
  - [ ] Clicked RUN
  - [ ] Verified 3 tables created: kalshi_trades, kalshi_positions, kalshi_bot_status

- [ ] Got Supabase credentials
  - [ ] Copied Project URL from Settings → API
  - [ ] Copied service_role key (NOT anon!) from Settings → API

---

## Kalshi API Setup (2 minutes)

- [ ] Logged into https://kalshi.com
- [ ] Went to Settings → API
- [ ] Generated API Key
- [ ] Saved Key ID
- [ ] Saved Private Key
- [ ] Decided on API host:
  - [ ] Demo (recommended first): `https://demo-api.kalshi.com`
  - [ ] Live (after testing): `https://trading-api.kalshi.com`

---

## GitHub Setup (2 minutes)

Choose one:

### Option A: GitHub CLI (Easiest)
- [ ] Ran: `gh repo create kalshi-trading-bot --private --source=. --push`

### Option B: Manual
- [ ] Created repo at https://github.com/new
  - [ ] Name: `kalshi-trading-bot`
  - [ ] Set to PRIVATE
  - [ ] Did NOT initialize with README
- [ ] Ran commands:
  ```bash
  git remote add origin https://github.com/YOUR-USERNAME/kalshi-trading-bot.git
  git branch -M main
  git push -u origin main
  ```

---

## Deployment (2 minutes)

Choose one:

### Option A: Railway (Recommended)
- [ ] Went to https://railway.app
- [ ] Clicked "New Project"
- [ ] Selected "Deploy from GitHub"
- [ ] Chose `kalshi-trading-bot` repository
- [ ] Added environment variables:
  - [ ] KALSHI_API_KEY_ID
  - [ ] KALSHI_PRIVATE_KEY
  - [ ] KALSHI_API_HOST (demo first!)
  - [ ] SUPABASE_URL
  - [ ] SUPABASE_SERVICE_KEY
- [ ] Verified deployment successful

### Option B: Render
- [ ] Went to https://render.com
- [ ] Clicked New → Background Worker
- [ ] Connected GitHub repository
- [ ] Added environment variables (4 secrets needed)
- [ ] Created service
- [ ] Verified deployment successful

### Option C: VPS
- [ ] SSH'd into server
- [ ] Installed Python 3
- [ ] Cloned repository
- [ ] Ran `./setup.sh`
- [ ] Created `.env` file with credentials
- [ ] Tested with `python bot.py --demo`
- [ ] Set up systemd service (see DEPLOYMENT.md)

---

## Verification (2 minutes)

- [ ] Checked deployment logs
  - [ ] Saw "✅ Kalshi client initialized"
  - [ ] Saw "✅ Supabase connected"
  - [ ] Saw "🚀 Bot is now running"

- [ ] Checked Supabase dashboard
  - [ ] Table Editor → kalshi_bot_status has data
  - [ ] `is_running` is `true`
  - [ ] `last_check` is recent

- [ ] Monitored for 5 minutes
  - [ ] Bot logs show "Analyzing X markets"
  - [ ] No errors in logs
  - [ ] Supabase `last_check` updating every ~30s

---

## Testing Phase (1 week minimum)

- [ ] Bot running in DEMO mode
- [ ] Monitoring daily
- [ ] Checking Supabase for any trades (rare but should see signals)
- [ ] No errors in logs
- [ ] Understanding strategy behavior

---

## Going Live (Only after thorough testing!)

- [ ] Tested in demo for 1+ week
- [ ] Understand bot behavior
- [ ] Set conservative limits:
  - [ ] MAX_POSITION_SIZE = 50 (or less)
  - [ ] MAX_DAILY_LOSS = 250 (or less)
  - [ ] MAX_ORDER_SIZE = 25 (or less)

- [ ] Changed environment variable:
  - [ ] KALSHI_API_HOST = `https://trading-api.kalshi.com`

- [ ] Redeployed/restarted bot

- [ ] Monitoring closely:
  - [ ] Checking logs every few hours (day 1)
  - [ ] Watching Supabase trades table
  - [ ] Verifying positions make sense
  - [ ] Confirming P&L is tracked correctly

---

## Ongoing Maintenance

Daily:
- [ ] Check bot is running (Supabase `last_check`)
- [ ] Review any trades made
- [ ] Check for errors in logs

Weekly:
- [ ] Review strategy performance
- [ ] Check Supabase trade history
- [ ] Adjust limits if needed

Monthly:
- [ ] Analyze P&L
- [ ] Review and optimize strategies
- [ ] Update bot if improvements available

---

## Quick Links

- [ ] Bookmarked: Railway/Render dashboard
- [ ] Bookmarked: Supabase dashboard  
- [ ] Bookmarked: Kalshi settings
- [ ] Saved: GitHub repo link

---

## Emergency Stop

If something goes wrong:

**Railway**: Dashboard → Service → "Stop"
**Render**: Dashboard → Service → "Suspend"
**VPS**: `sudo systemctl stop kalshi-bot`

Then investigate logs and Supabase data before restarting.

---

## Support

If stuck:
1. Check logs first (Railway/Render dashboard or VPS journalctl)
2. Verify environment variables are correct
3. Check Supabase tables for unexpected data
4. Review DEPLOYMENT.md troubleshooting section
5. Check GitHub Issues

---

**When this checklist is complete, your bot is live! 🎉**

Remember:
- Start small
- Monitor closely
- Don't trade money you can't afford to lose
- This is automated trading - understand the risks!
