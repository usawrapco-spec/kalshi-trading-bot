"""Configuration management for Kalshi trading bot."""
import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    """Bot configuration."""

    # Kalshi API
    KALSHI_API_KEY_ID = os.getenv('KALSHI_API_KEY_ID')
    KALSHI_PRIVATE_KEY = os.getenv('KALSHI_PRIVATE_KEY')
    KALSHI_API_HOST = os.getenv('KALSHI_API_HOST', 'https://demo-api.kalshi.co')

    # AI API Keys
    XAI_API_KEY = os.getenv('XAI_API_KEY')

    # Supabase
    SUPABASE_URL = os.getenv('SUPABASE_URL')
    SUPABASE_SERVICE_KEY = os.getenv('SUPABASE_KEY') or os.getenv('SUPABASE_SERVICE_KEY')

    # Risk Management
    MAX_POSITION_SIZE = int(os.getenv('MAX_POSITION_SIZE', 100))
    MAX_DAILY_LOSS = int(os.getenv('MAX_DAILY_LOSS', 500))
    MAX_ORDER_SIZE = int(os.getenv('MAX_ORDER_SIZE', 50))

    # Paper Trading
    PAPER_BALANCE = float(os.getenv('PAPER_BALANCE', 100.0))  # $100 starting balance
    ENABLE_TRADING = os.getenv('ENABLE_TRADING', 'false').lower() == 'true'  # False = paper only

    # Live Strategy Control — comma-separated list of strategy names that place REAL orders
    # Only active when ENABLE_TRADING=true. Empty = all paper.
    LIVE_STRATEGIES = [s.strip() for s in os.getenv('LIVE_STRATEGIES', '').split(',') if s.strip()]

    # Strategy Toggles
    ENABLE_WEATHER = os.getenv('ENABLE_WEATHER', 'true').lower() == 'true'
    ENABLE_GROK = os.getenv('ENABLE_GROK', 'true').lower() == 'true'
    ENABLE_PROB_ARB = os.getenv('ENABLE_PROB_ARB', 'true').lower() == 'true'
    ENABLE_SPORTS_NO = os.getenv('ENABLE_SPORTS_NO', 'true').lower() == 'true'
    ENABLE_NEAR_CERTAINTY = os.getenv('ENABLE_NEAR_CERTAINTY', 'true').lower() == 'true'
    ENABLE_MENTION = os.getenv('ENABLE_MENTION', 'true').lower() == 'true'
    ENABLE_HIGH_PROB = os.getenv('ENABLE_HIGH_PROB', 'true').lower() == 'true'
    ENABLE_ORDERBOOK = os.getenv('ENABLE_ORDERBOOK', 'true').lower() == 'true'
    ENABLE_CROSS_PLATFORM = os.getenv('ENABLE_CROSS_PLATFORM', 'true').lower() == 'true'
    ENABLE_MARKET_MAKING = os.getenv('ENABLE_MARKET_MAKING', 'false').lower() == 'true'

    # Monitoring
    CHECK_INTERVAL_SECONDS = int(os.getenv('CHECK_INTERVAL_SECONDS', 30))
    LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')

    # Paths
    DATA_DIR = 'data'
    LOGS_DIR = 'logs'

    @classmethod
    def validate(cls):
        if not cls.KALSHI_API_KEY_ID:
            raise ValueError("KALSHI_API_KEY_ID not set in .env")
        if not cls.KALSHI_PRIVATE_KEY:
            raise ValueError("KALSHI_PRIVATE_KEY not set in .env")
        if 'demo' in cls.KALSHI_API_HOST:
            print("[DEMO] Using DEMO API - No real money at risk")
        else:
            print("[LIVE] Using LIVE API - Real money trading enabled!")
        if not cls.ENABLE_TRADING:
            print("[PAPER] Paper trading mode - no real orders placed")
        return True

    @classmethod
    def is_demo(cls):
        return 'demo' in cls.KALSHI_API_HOST.lower()
