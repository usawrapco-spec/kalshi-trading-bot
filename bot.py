"""Entry point — runs MATCHER bundle-arb bot (Kalshi YES+NO lock-in)."""
import os
from threading import Thread
from matcher import bot_loop, app, init_db

PORT = int(os.environ.get('PORT', 8080))

if __name__ == '__main__':
    init_db()
    Thread(target=bot_loop, daemon=True).start()
    app.run(host='0.0.0.0', port=PORT)
