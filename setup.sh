#!/bin/bash
# Setup script for Kalshi Trading Bot

echo "================================================"
echo "Kalshi Trading Bot - Setup"
echo "================================================"
echo ""

# Check Python version
echo "Checking Python version..."
python3 --version

if [ $? -ne 0 ]; then
    echo "❌ Python 3 is required but not found"
    echo "Please install Python 3.8 or higher"
    exit 1
fi

echo "✅ Python found"
echo ""

# Create virtual environment
echo "Creating virtual environment..."
python3 -m venv venv

if [ $? -ne 0 ]; then
    echo "❌ Failed to create virtual environment"
    exit 1
fi

echo "✅ Virtual environment created"
echo ""

# Activate virtual environment
echo "Activating virtual environment..."
source venv/bin/activate

# Install dependencies
echo "Installing dependencies..."
pip install --upgrade pip
pip install -r requirements.txt

if [ $? -ne 0 ]; then
    echo "❌ Failed to install dependencies"
    exit 1
fi

echo "✅ Dependencies installed"
echo ""

# Create .env file if it doesn't exist
if [ ! -f .env ]; then
    echo "Creating .env file..."
    cp .env.example .env
    echo "✅ .env file created"
    echo ""
    echo "⚠️  IMPORTANT: Edit .env and add your Kalshi API credentials!"
    echo ""
else
    echo "✅ .env file already exists"
    echo ""
fi

# Create data and logs directories
mkdir -p data logs

echo "================================================"
echo "Setup Complete! 🎉"
echo "================================================"
echo ""
echo "Next steps:"
echo "1. Edit .env file with your Kalshi API credentials"
echo "2. Activate virtual environment: source venv/bin/activate"
echo "3. Run in demo mode first: python bot.py --demo"
echo ""
echo "For more information, see README.md"
echo ""
