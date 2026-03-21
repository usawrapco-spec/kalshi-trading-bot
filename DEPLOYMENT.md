# Deployment Guide

## Push to GitHub

### Option 1: Create New Repository on GitHub

1. Go to https://github.com/new
2. Create a new repository (e.g., `kalshi-trading-bot`)
3. **Don't** initialize with README (we already have one)
4. Run these commands:

```bash
cd /home/claude/kalshi-trading-bot
git remote add origin https://github.com/YOUR-USERNAME/kalshi-trading-bot.git
git branch -M main
git push -u origin main
```

### Option 2: Use GitHub CLI

```bash
# Install GitHub CLI first (https://cli.github.com/)
gh repo create kalshi-trading-bot --private --source=. --push
```

## Deploy to a Server

### VPS Setup (DigitalOcean, AWS EC2, etc.)

1. **SSH into your server:**
   ```bash
   ssh your-user@your-server-ip
   ```

2. **Clone the repository:**
   ```bash
   git clone https://github.com/YOUR-USERNAME/kalshi-trading-bot.git
   cd kalshi-trading-bot
   ```

3. **Run setup:**
   ```bash
   ./setup.sh
   ```

4. **Add your credentials:**
   ```bash
   nano .env
   # Add your KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY
   ```

5. **Test in demo mode:**
   ```bash
   source venv/bin/activate
   python bot.py --demo
   ```

6. **Run in background (systemd):**

Create service file:
```bash
sudo nano /etc/systemd/system/kalshi-bot.service
```

Add:
```ini
[Unit]
Description=Kalshi Trading Bot
After=network.target

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/home/YOUR_USER/kalshi-trading-bot
Environment="PATH=/home/YOUR_USER/kalshi-trading-bot/venv/bin"
ExecStart=/home/YOUR_USER/kalshi-trading-bot/venv/bin/python bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Enable and start:
```bash
sudo systemctl daemon-reload
sudo systemctl enable kalshi-bot
sudo systemctl start kalshi-bot

# Check status
sudo systemctl status kalshi-bot

# View logs
sudo journalctl -u kalshi-bot -f
```

### Alternative: Screen or tmux

```bash
# Using screen
screen -S kalshi-bot
source venv/bin/activate
python bot.py
# Press Ctrl+A, then D to detach

# Reattach later
screen -r kalshi-bot

# Using tmux
tmux new -s kalshi-bot
source venv/bin/activate
python bot.py
# Press Ctrl+B, then D to detach

# Reattach later
tmux attach -t kalshi-bot
```

## Run Locally (Mac/Linux)

```bash
# Clone
git clone https://github.com/YOUR-USERNAME/kalshi-trading-bot.git
cd kalshi-trading-bot

# Setup
./setup.sh

# Configure
cp .env.example .env
nano .env  # Add your API credentials

# Run
source venv/bin/activate
python bot.py --demo
```

## Run Locally (Windows)

```powershell
# Clone
git clone https://github.com/YOUR-USERNAME/kalshi-trading-bot.git
cd kalshi-trading-bot

# Create virtual environment
python -m venv venv
.\venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Configure
copy .env.example .env
notepad .env  # Add your API credentials

# Run
python bot.py --demo
```

## Monitoring

### Check Logs
```bash
# Real-time logs
tail -f logs/bot_*.log

# All logs
cat logs/bot_*.log
```

### Check Trades
```bash
# Pretty print trade history
cat data/trades.json | python -m json.tool

# Count trades
cat data/trades.json | python -m json.tool | grep "ticker" | wc -l
```

### Monitor Performance
```bash
# Watch the bot
watch -n 5 'tail -20 logs/bot_*.log'
```

## Security Best Practices

1. **Never commit .env file** (it's in .gitignore)
2. **Use SSH keys** for GitHub/server access
3. **Keep API keys secure** - treat them like passwords
4. **Use demo API** for testing
5. **Start with small position sizes**
6. **Monitor regularly** - don't set and forget
7. **Keep repository private** if it contains trading logic

## Backup Strategy

```bash
# Backup trade history
tar -czf backup-$(date +%Y%m%d).tar.gz data/ logs/

# Restore
tar -xzf backup-YYYYMMDD.tar.gz
```

## Updating the Bot

```bash
# Pull latest changes
git pull origin main

# Reinstall dependencies (if requirements.txt changed)
source venv/bin/activate
pip install -r requirements.txt --upgrade

# Restart service (if using systemd)
sudo systemctl restart kalshi-bot
```

## Troubleshooting

**Bot stops running**
- Check logs: `tail -f logs/bot_*.log`
- Check system resources: `htop` or `top`
- Restart service: `sudo systemctl restart kalshi-bot`

**Can't connect to Kalshi API**
- Verify API credentials in .env
- Check network connectivity
- Verify API host URL is correct

**High CPU/Memory usage**
- Increase CHECK_INTERVAL_SECONDS in .env
- Reduce number of strategies
- Optimize strategy code

## Cost Estimate

**VPS Hosting:**
- DigitalOcean Droplet: $6-12/month
- AWS EC2 t2.micro: ~$8/month
- Linode Nanode: $5/month

**Local Machine:**
- Free (electricity cost negligible)
- Need to keep computer on
