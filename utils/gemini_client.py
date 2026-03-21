"""Gemini AI client for third-brain debate voting."""

import os
import requests

GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', '')


def ask_gemini(prompt, max_tokens=500):
    if not GEMINI_API_KEY:
        return None
    try:
        url = f'https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}'
        resp = requests.post(url, json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.3, "maxOutputTokens": max_tokens}
        }, timeout=15)
        if resp.status_code == 200:
            parts = resp.json().get('candidates', [{}])[0].get('content', {}).get('parts', [])
            if parts:
                return parts[0].get('text', '')
    except Exception:
        pass
    return None
