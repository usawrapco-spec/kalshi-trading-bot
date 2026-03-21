# 🚀 One-Click Deployment Guide

Deploy your Kalshi bot in under 5 minutes with Railway or Render.

---

## Option 1: Railway (Recommended - Easiest)

### Step 1: Prepare Supabase (2 minutes)

1. **Create project**: https://supabase.com/dashboard → New Project
   - Name: `kalshi-trading-bot`
   - Save the password!

2. **Run migration**: Supabase → SQL Editor → New Query
   - Copy entire content from `supabase/migrations/001_initial_schema.sql`
   - Click RUN

3. **Get credentials**: Settings → API
   - Copy `Project URL`
   - Copy `service_role` key (NOT anon!)

### Step 2: Deploy to Railway (2 minutes)

1. **Push to GitHub**:
   ```bash
   # If you have GitHub CLI
   gh repo create kalshi-trading-bot --private --source=. --push
   
   # Or manually at github.com/new, then:
   git remote add origin https://github.com/YOUR-USERNAME/kalshi-trading-bot.git
   git branch -M main
   git push -u origin main
   ```

2. **Deploy**: https://railway.app
   - Click "New Project"
   - Select "Deploy from GitHub"
   - Choose `kalshi-trading-bot`
   - Railway auto-detects Python ✓

3. **Add environment variables** (Railway dashboard → Variables):
   ```
   KALSHI_API_KEY_ID          = (from kalshi.com → Settings → API)
   KALSHI_PRIVATE_KEY         = (from kalshi.com)
   KALSHI_API_HOST            = https://demo-api.kalshi.com
   SUPABASE_URL               = (from Supabase)
   SUPABASE_SERVICE_KEY       = (from Supabase service_role)
   ```

4. **Done!** Railway auto-deploys. Check logs to see bot running.

---

## Option 2: Render.com

### Step 1: Same Supabase setup as above

### Step 2: Deploy to Render

1. **Push to GitHub** (same as Railway)

2. **Deploy**: https://render.com
   - New → Background Worker
   - Connect GitHub → Select `kalshi-trading-bot`
   - Render auto-detects `render.yaml` ✓

3. **Add secrets** (Render dashboard → Environment):
   - Only add the ones marked `sync: false` in render.yaml:
     - `KALSHI_API_KEY_ID`
     - `KALSHI_PRIVATE_KEY`
     - `SUPABASE_URL`
     - `SUPABASE_SERVICE_KEY`

4. **Deploy!** Render auto-deploys.

---

## Get Kalshi API Keys

1. Go to https://kalshi.com
2. Login → Settings → API
3. Click "Generate API Key"
4. **Save Key ID and Private Key immediately!**

**Start with demo API**: `https://demo-api.kalshi.com`

---

## Verify Deployment

### Check Railway/Render Logs:
You should see:
```
✅ Kalshi client initialized successfully
✅ Supabase connected
✅ Arbitrage strategy enabled
✅ Momentum strategy enabled
🚀 Bot is now running. Press Ctrl+C to stop.
```

### Check Supabase:
Go to Table Editor → `kalshi_bot_status`
- Should see a row with `is_running: true`

### Monitor Trades:
Table Editor → `kalshi_trades`
- Will populate when bot finds opportunities

---

## Go Live (After Testing)

**Only after 1+ week of demo testing:**

1. Railway/Render → Environment Variables
2. Change: `KALSHI_API_HOST` = `https://trading-api.kalshi.com`
3. Start with small limits:
   ```
   MAX_POSITION_SIZE = 50
   MAX_DAILY_LOSS = 250
   MAX_ORDER_SIZE = 25
   ```
4. Monitor closely for 24 hours

---

## Costs

- **Railway**: $5/month (500 hours included, bot uses ~720/month)
- **Render**: $7/month (Background Worker)
- **Supabase**: Free tier (plenty for this bot)
- **Total**: ~$5-7/month

---

## Quick Links

- **Railway Dashboard**: https://railway.app/dashboard
- **Render Dashboard**: https://dashboard.render.com
- **Supabase Dashboard**: https://supabase.com/dashboard
- **Kalshi Settings**: https://kalshi.com/settings

---

## Troubleshooting

**"Bot not starting"**
- Check logs in Railway/Render dashboard
- Verify all environment variables are set
- Check Supabase credentials

**"No trades"**
- Normal! Opportunities are rare
- Check logs for "Analyzing X markets"
- Supabase `kalshi_bot_status` should update every 30s

**"Supabase error"**
- Verify service_role key (not anon)
- Check Supabase project is running
- Test SQL migration ran successfully

---

## Stop/Restart Bot

**Railway**: Dashboard → Service → Stop/Restart
**Render**: Dashboard → Service → Manual Deploy → Suspend/Resume

---

That's it! Your bot should be running in the cloud with data streaming to Supabase. 🎉
