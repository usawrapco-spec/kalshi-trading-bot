"""Logging utilities for the trading bot."""

import logging
import os
from datetime import datetime
from config import Config


def setup_logger(name='kalshi_bot'):
    """Set up logger with file and console handlers."""
    
    # Create logs directory if it doesn't exist
    os.makedirs(Config.LOGS_DIR, exist_ok=True)
    
    # Create logger
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, Config.LOG_LEVEL))
    
    # Prevent duplicate handlers
    if logger.handlers:
        return logger
    
    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_format = logging.Formatter(
        '%(asctime)s - %(levelname)s - %(message)s',
        datefmt='%H:%M:%S'
    )
    console_handler.setFormatter(console_format)
    
    # File handler
    log_file = os.path.join(
        Config.LOGS_DIR,
        f'bot_{datetime.now().strftime("%Y%m%d")}.log'
    )
    file_handler = logging.FileHandler(log_file)
    file_handler.setLevel(logging.DEBUG)
    file_format = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    file_handler.setFormatter(file_format)
    
    # Add handlers
    logger.addHandler(console_handler)
    logger.addHandler(file_handler)
    
    return logger


def log_trade(trade_data, filename='trades.json'):
    """Log trade to JSON file for analysis."""
    import json
    
    os.makedirs(Config.DATA_DIR, exist_ok=True)
    filepath = os.path.join(Config.DATA_DIR, filename)
    
    # Load existing trades
    if os.path.exists(filepath):
        with open(filepath, 'r') as f:
            trades = json.load(f)
    else:
        trades = []
    
    # Add new trade
    trade_data['timestamp'] = datetime.now().isoformat()
    trades.append(trade_data)
    
    # Save
    with open(filepath, 'w') as f:
        json.dump(trades, f, indent=2)
