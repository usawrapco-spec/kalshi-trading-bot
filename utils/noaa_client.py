"""NOAA weather forecast client for cross-referencing weather markets."""

import requests

NOAA_CITIES = {
    'NYC': (40.7128, -74.0060),
    'CHI': (41.8781, -87.6298),
    'MIA': (25.7617, -80.1918),
    'LAX': (34.0522, -118.2437),
    'DEN': (39.7392, -104.9903),
}


def get_noaa_forecast(city_code):
    coords = NOAA_CITIES.get(city_code)
    if not coords:
        return None
    try:
        lat, lon = coords
        headers = {'User-Agent': 'KalshiBot/1.0'}
        resp = requests.get(f"https://api.weather.gov/points/{lat},{lon}", headers=headers, timeout=10)
        forecast_url = resp.json()['properties']['forecast']
        resp2 = requests.get(forecast_url, headers=headers, timeout=10)
        for p in resp2.json()['properties']['periods']:
            if p.get('isDaytime'):
                return {'high': p['temperature'], 'unit': p['temperatureUnit'], 'name': p['name']}
    except Exception:
        pass
    return None
