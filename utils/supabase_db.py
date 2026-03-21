"""Supabase integration for storing trade data."""

import os
from supabase import create_client, Client
from datetime import datetime
from config import Config
from utils.logger import setup_logger

logger = setup_logger('supabase_db')


class SupabaseDB:
    """Handle all Supabase database operations."""
    
    def __init__(self):
        """Initialize Supabase client."""
        self.supabase_url = os.getenv('SUPABASE_URL')
        self.supabase_key = os.getenv('SUPABASE_KEY') or os.getenv('SUPABASE_SERVICE_KEY')

        if not self.supabase_url or not self.supabase_key:
            logger.warning("Supabase credentials not found - using local storage")
            self.client = None
            return

        # Log key prefix for debugging
        logger.info(f"Supabase URL: {self.supabase_url}")
        logger.info(f"Supabase key starts with: {self.supabase_key[:20]}...")

        # The new sb_secret_ / sb_publishable_ keys do NOT work with
        # supabase-py's create_client() — it sends the key as a Bearer JWT
        # token, which Supabase rejects for non-JWT keys.
        # You must use the legacy JWT service_role key (starts with "eyJ").
        if self.supabase_key.startswith('sb_'):
            logger.error(
                "SUPABASE_KEY uses the new sb_secret_ format which is NOT "
                "compatible with supabase-py. Go to Supabase Dashboard > "
                "Settings > API and copy the 'service_role' key (starts with "
                "'eyJ...'). Set that as SUPABASE_KEY in Railway."
            )
            self.client = None
            return

        try:
            self.client: Client = create_client(self.supabase_url, self.supabase_key)
            logger.info("Supabase connected successfully")
        except Exception as e:
            logger.error(f"Supabase connection failed: {e}")
            self.client = None
    
    def log_trade(self, trade_data):
        """Log a trade to Supabase."""
        if not self.client:
            return None
        
        try:
            data = {
                'ticker': trade_data.get('ticker'),
                'action': trade_data.get('action'),
                'side': trade_data.get('side'),
                'count': trade_data.get('count'),
                'strategy': trade_data.get('strategy'),
                'reason': trade_data.get('reason'),
                'confidence': trade_data.get('confidence'),
                'order_id': trade_data.get('order_id'),
                'price': trade_data.get('price'),
                'timestamp': datetime.now().isoformat()
            }
            
            result = self.client.table('kalshi_trades').insert(data).execute()
            logger.info(f"✅ Trade logged to Supabase: {trade_data.get('ticker')}")
            return result
        except Exception as e:
            logger.error(f"❌ Failed to log trade to Supabase: {e}")
            return None
    
    def get_trades(self, limit=100, ticker=None):
        """Get trade history from Supabase."""
        if not self.client:
            return []
        
        try:
            query = self.client.table('kalshi_trades').select('*').order('timestamp', desc=True).limit(limit)
            
            if ticker:
                query = query.eq('ticker', ticker)
            
            result = query.execute()
            return result.data
        except Exception as e:
            logger.error(f"❌ Failed to get trades: {e}")
            return []
    
    def log_position_update(self, ticker, position, pnl=0):
        """Update current positions in Supabase."""
        if not self.client:
            return None
        
        try:
            data = {
                'ticker': ticker,
                'position': position,
                'pnl': pnl,
                'updated_at': datetime.now().isoformat()
            }
            
            # Upsert (insert or update)
            result = self.client.table('kalshi_positions').upsert(
                data,
                on_conflict='ticker'
            ).execute()
            
            return result
        except Exception as e:
            logger.error(f"❌ Failed to update position: {e}")
            return None
    
    def get_positions(self):
        """Get current positions."""
        if not self.client:
            return []
        
        try:
            result = self.client.table('kalshi_positions').select('*').execute()
            return result.data
        except Exception as e:
            logger.error(f"❌ Failed to get positions: {e}")
            return []
    
    def log_bot_status(self, status_data):
        """Log bot status/health check."""
        if not self.client:
            return None
        
        try:
            data = {
                'is_running': status_data.get('is_running', True),
                'daily_pnl': status_data.get('daily_pnl', 0),
                'trades_today': status_data.get('trades_today', 0),
                'balance': status_data.get('balance', 0),
                'active_positions': status_data.get('active_positions', 0),
                'last_check': datetime.now().isoformat()
            }
            
            # Always insert new status
            result = self.client.table('kalshi_bot_status').insert(data).execute()
            return result
        except Exception as e:
            logger.error(f"❌ Failed to log bot status: {e}")
            return None
