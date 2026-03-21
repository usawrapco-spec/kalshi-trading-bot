"""Configuration management for Kalshi trading bot."""
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

class Config:
    """Bot configuration."""
    
    # API Configuration
    KALSHI_API_KEY_ID = os.getenv('KALSHI_API_KEY_ID')
    KALSHI_PRIVATE_KEY = os.getenv('KALSHI_PRIVATE_KEY')
    KALSHI_API_HOST = os.getenv('KALSHI_API_HOST', 'https://demo-api.kalshi.co')
    
    # Supabase Configuration
    SUPABASE_URL = os.getenv('SUPABASE_URL')
    SUPABASE_SERVICE_KEY = os.getenv('SUPABASE_SERVICE_KEY')
    
    # Risk Management
    MAX_POSITION_SIZE = int(os.getenv('MAX_POSITION_SIZE', 100))
    MAX_DAILY_LOSS = int(os.getenv('MAX_DAILY_LOSS', 500))
    MAX_ORDER_SIZE = int(os.getenv('MAX_ORDER_SIZE', 50))
    
    # Strategy Settings
    ENABLE_ARBITRAGE = os.getenv('ENABLE_ARBITRAGE', 'true').lower() == 'true'
    ENABLE_MOMENTUM = os.getenv('ENABLE_MOMENTUM', 'true').lower() == 'true'
    ENABLE_EVENT_BASED = os.getenv('ENABLE_EVENT_BASED', 'false').lower() == 'true'
    
    # Monitoring
    CHECK_INTERVAL_SECONDS = int(os.getenv('CHECK_INTERVAL_SECONDS', 30))
    LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')
    
    # Paths
    DATA_DIR = 'data'
    LOGS_DIR = 'logs'
    
    @classmethod
    def validate(cls):
        """Validate that required configuration is present."""
        if not cls.KALSHI_API_KEY_ID:
            raise ValueError("KALSHI_API_KEY_ID not set in .env")
        if not cls.KALSHI_PRIVATE_KEY:
            raise ValueError("KALSHI_PRIVATE_KEY not set in .env")
        
        # Check if using demo API
        if 'demo' in cls.KALSHI_API_HOST:
            print("[DEMO] Using DEMO API - No real money at risk")
        else:
            print("[LIVE] Using LIVE API - Real money trading enabled!")
        
        return True
    
    @classmethod
    def is_demo(cls):
        """Check if running in demo mode."""
        return 'demo' in cls.KALSHI_API_HOST.lower()