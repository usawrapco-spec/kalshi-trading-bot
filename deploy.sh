#!/bin/bash
# Automated deployment script for Kalshi Trading Bot
# This script helps you deploy to Railway, Render, or setup locally

set -e

echo "================================================"
echo "Kalshi Trading Bot - Deployment Helper"
echo "================================================"
echo ""

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Check if we're in the right directory
if [ ! -f "bot.py" ]; then
    echo -e "${RED}Error: bot.py not found${NC}"
    echo "Please run this script from the kalshi-trading-bot directory"
    exit 1
fi

echo "Select deployment option:"
echo "1) Railway (Easiest - 1 click)"
echo "2) Render.com (Simple)"
echo "3) Local VPS setup"
echo "4) Just setup GitHub repo"
echo "5) Exit"
echo ""
read -p "Enter choice [1-5]: " choice

case $choice in
    1)
        echo ""
        echo -e "${YELLOW}Railway Deployment${NC}"
        echo "================================================"
        echo ""
        echo "Steps to deploy to Railway:"
        echo ""
        echo "1. Push to GitHub first (option 4 below)"
        echo "2. Go to https://railway.app"
        echo "3. Click 'New Project'"
        echo "4. Select 'Deploy from GitHub'"
        echo "5. Choose 'kalshi-trading-bot' repository"
        echo "6. Railway will auto-detect Python"
        echo ""
        echo "7. Add environment variables in Railway dashboard:"
        echo "   - KALSHI_API_KEY_ID"
        echo "   - KALSHI_PRIVATE_KEY"
        echo "   - KALSHI_API_HOST (use demo first!)"
        echo "   - SUPABASE_URL"
        echo "   - SUPABASE_SERVICE_KEY"
        echo ""
        echo "8. Railway will automatically deploy!"
        echo ""
        read -p "Press enter to continue..."
        ;;
    
    2)
        echo ""
        echo -e "${YELLOW}Render.com Deployment${NC}"
        echo "================================================"
        echo ""
        echo "Steps to deploy to Render:"
        echo ""
        echo "1. Push to GitHub first (option 4 below)"
        echo "2. Go to https://render.com"
        echo "3. Click 'New' → 'Background Worker'"
        echo "4. Connect your GitHub repository"
        echo "5. Configure:"
        echo "   - Name: kalshi-trading-bot"
        echo "   - Environment: Python 3"
        echo "   - Build Command: pip install -r requirements.txt"
        echo "   - Start Command: python bot.py"
        echo ""
        echo "6. Add environment variables (same as Railway)"
        echo "7. Create service"
        echo ""
        read -p "Press enter to continue..."
        ;;
    
    3)
        echo ""
        echo -e "${YELLOW}VPS Setup${NC}"
        echo "================================================"
        echo ""
        echo "Run these commands on your VPS:"
        echo ""
        echo "# SSH into server"
        echo "ssh your-user@your-server-ip"
        echo ""
        echo "# Install dependencies"
        echo "sudo apt update"
        echo "sudo apt install python3 python3-pip python3-venv git -y"
        echo ""
        echo "# Clone repository"
        echo "git clone https://github.com/YOUR-USERNAME/kalshi-trading-bot.git"
        echo "cd kalshi-trading-bot"
        echo ""
        echo "# Setup"
        echo "./setup.sh"
        echo ""
        echo "# Configure"
        echo "cp .env.example .env"
        echo "nano .env  # Add your credentials"
        echo ""
        echo "# Test"
        echo "source venv/bin/activate"
        echo "python bot.py --demo"
        echo ""
        echo "# Setup as service (see DEPLOYMENT.md for systemd setup)"
        echo ""
        read -p "Press enter to continue..."
        ;;
    
    4)
        echo ""
        echo -e "${YELLOW}GitHub Repository Setup${NC}"
        echo "================================================"
        echo ""
        
        # Check if git is initialized
        if [ ! -d ".git" ]; then
            echo "Initializing git repository..."
            git init
            echo -e "${GREEN}✓ Git initialized${NC}"
        else
            echo -e "${GREEN}✓ Git already initialized${NC}"
        fi
        
        # Check for changes to commit
        if [ -n "$(git status --porcelain)" ]; then
            echo ""
            read -p "Commit changes? [y/n]: " commit_choice
            if [ "$commit_choice" = "y" ]; then
                git add .
                read -p "Commit message (default: 'Update bot'): " commit_msg
                commit_msg=${commit_msg:-"Update bot"}
                git commit -m "$commit_msg"
                echo -e "${GREEN}✓ Changes committed${NC}"
            fi
        fi
        
        echo ""
        echo "Choose GitHub setup method:"
        echo "1) GitHub CLI (gh) - automatic"
        echo "2) Manual setup"
        read -p "Enter choice [1-2]: " gh_choice
        
        if [ "$gh_choice" = "1" ]; then
            # Check if gh is installed
            if command -v gh &> /dev/null; then
                echo ""
                echo "Creating private GitHub repository..."
                gh repo create kalshi-trading-bot --private --source=. --push
                echo -e "${GREEN}✓ Repository created and pushed!${NC}"
                echo ""
                echo "View at: https://github.com/$(gh repo view --json owner --jq .owner.login)/kalshi-trading-bot"
            else
                echo -e "${RED}GitHub CLI not installed${NC}"
                echo "Install from: https://cli.github.com/"
                echo ""
                echo "Or use manual setup (option 2)"
            fi
        else
            echo ""
            echo "Manual GitHub setup:"
            echo "1. Go to https://github.com/new"
            echo "2. Repository name: kalshi-trading-bot"
            echo "3. Set to PRIVATE"
            echo "4. Do NOT initialize with README"
            echo "5. Click 'Create repository'"
            echo ""
            read -p "Enter your GitHub username: " gh_user
            echo ""
            echo "Now run these commands:"
            echo ""
            echo "git remote add origin https://github.com/$gh_user/kalshi-trading-bot.git"
            echo "git branch -M main"
            echo "git push -u origin main"
            echo ""
            read -p "Execute these commands now? [y/n]: " exec_choice
            
            if [ "$exec_choice" = "y" ]; then
                git remote add origin https://github.com/$gh_user/kalshi-trading-bot.git 2>/dev/null || git remote set-url origin https://github.com/$gh_user/kalshi-trading-bot.git
                git branch -M main
                git push -u origin main
                echo -e "${GREEN}✓ Pushed to GitHub!${NC}"
            fi
        fi
        ;;
    
    5)
        echo "Exiting..."
        exit 0
        ;;
    
    *)
        echo -e "${RED}Invalid choice${NC}"
        exit 1
        ;;
esac

echo ""
echo "================================================"
echo -e "${GREEN}Next Steps:${NC}"
echo "================================================"
echo ""
echo "1. Create Supabase project (if not done)"
echo "   → https://supabase.com/dashboard"
echo ""
echo "2. Run database migration"
echo "   → Copy from: supabase/migrations/001_initial_schema.sql"
echo ""
echo "3. Get Kalshi API credentials"
echo "   → https://kalshi.com → Settings → API"
echo ""
echo "4. Deploy and add environment variables"
echo ""
echo "5. Monitor in Supabase dashboard!"
echo ""
echo "Full guide: SETUP.md"
echo ""
