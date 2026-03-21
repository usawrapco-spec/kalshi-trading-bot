"""API resilience utilities for robust external API handling.

Provides timeout, retry, and fallback mechanisms for all external API calls.
"""

import time
import requests
from functools import wraps
from utils.logger import setup_logger

logger = setup_logger('api_resilience')

class APIResilience:
    """Handles resilient API calls with timeouts, retries, and fallbacks."""

    @staticmethod
    def call_with_retry(func_name, api_call, timeout=10, retries=1, backoff_factor=2):
        """
        Execute API call with timeout and retry logic.

        Args:
            func_name: Name for logging
            api_call: Function that makes the API call
            timeout: Timeout in seconds
            retries: Number of retries
            backoff_factor: Exponential backoff multiplier

        Returns:
            API response or None if all attempts fail
        """
        for attempt in range(retries + 1):
            try:
                start_time = time.time()
                result = api_call(timeout=timeout)
                elapsed = time.time() - start_time

                if attempt > 0:
                    logger.info(f"API RECOVERED: {func_name} succeeded on attempt {attempt + 1} ({elapsed:.1f}s)")
                else:
                    logger.debug(f"API SUCCESS: {func_name} ({elapsed:.1f}s)")

                return result

            except requests.exceptions.Timeout:
                elapsed = time.time() - start_time
                logger.warning(f"API TIMEOUT: {func_name} attempt {attempt + 1}/{retries + 1} after {elapsed:.1f}s")

            except requests.exceptions.ConnectionError as e:
                logger.warning(f"API CONNECTION: {func_name} attempt {attempt + 1}/{retries + 1} - {e}")

            except requests.exceptions.HTTPError as e:
                logger.warning(f"API HTTP ERROR: {func_name} attempt {attempt + 1}/{retries + 1} - {e}")

            except Exception as e:
                logger.error(f"API UNEXPECTED: {func_name} attempt {attempt + 1}/{retries + 1} - {e}")

            if attempt < retries:
                sleep_time = backoff_factor ** attempt
                logger.info(f"API RETRY: {func_name} waiting {sleep_time}s before retry")
                time.sleep(sleep_time)

        logger.error(f"API FAILED: {func_name} all {retries + 1} attempts failed")
        return None

    @staticmethod
    def grok_call(api_call, fallback=None):
        """Grok API call with 15s timeout, 1 retry, skip if fails."""
        return APIResilience.call_with_retry(
            "Grok API",
            api_call,
            timeout=15,
            retries=1
        ) or fallback

    @staticmethod
    def claude_call(api_call, fallback=None):
        """Claude API call with 15s timeout, 1 retry, skip debate if fails."""
        return APIResilience.call_with_retry(
            "Claude API",
            api_call,
            timeout=15,
            retries=1
        ) or fallback

    @staticmethod
    def polymarket_call(api_call, fallback=None):
        """Polymarket API call with 10s timeout, use cached data if fails."""
        return APIResilience.call_with_retry(
            "Polymarket API",
            api_call,
            timeout=10,
            retries=0  # No retry, use cache
        ) or fallback

    @staticmethod
    def open_meteo_call(api_call, fallback=None):
        """Open-Meteo API call with 10s timeout, use cached forecast if fails."""
        return APIResilience.call_with_retry(
            "Open-Meteo API",
            api_call,
            timeout=10,
            retries=0  # No retry, use cache
        ) or fallback

    @staticmethod
    def kalshi_call(api_call, fallback=None):
        """Kalshi API call with 10s timeout, 2 retries, skip cycle if fails."""
        return APIResilience.call_with_retry(
            "Kalshi API",
            api_call,
            timeout=10,
            retries=2
        ) or fallback

def resilient_strategy(func):
    """Decorator to wrap strategy execution in try/except."""
    @wraps(func)
    def wrapper(*args, **kwargs):
        strategy_name = args[0].__class__.__name__ if args else "UnknownStrategy"
        try:
            return func(*args, **kwargs)
        except Exception as e:
            logger.error(f"STRATEGY CRASH: {strategy_name} failed - {e}", exc_info=True)
            return []  # Return empty signals list on failure
    return wrapper