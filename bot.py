"""
Main entry point — runs RAZOR (live) and SHADOW (paper) bots.
"""

import os
from flask import Flask, redirect
from threading import Thread
from razor import razor_bp, razor_loop, init_razor_db
from shadow import shadow_bp, shadow_loop, init_shadow_db

PORT = int(os.environ.get('PORT', 8080))

app = Flask(__name__)
app.register_blueprint(razor_bp)
app.register_blueprint(shadow_bp)


@app.route('/')
def index():
    return """<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Kalshi Bots</title>
<style>
body{background:#0a0a0a;color:#e0e0e0;font-family:'JetBrains Mono',monospace;display:flex;justify-content:center;align-items:center;height:100vh;gap:40px}
a{text-decoration:none;padding:40px 60px;border-radius:12px;text-align:center;display:block;transition:transform .1s}
a:hover{transform:scale(1.05)}
.razor{background:#111;border:2px solid #00d673;color:#00d673}
.shadow{background:#111;border:2px solid #ffaa00;color:#ffaa00}
.name{font-size:28px;font-weight:700;margin-bottom:8px}
.desc{font-size:11px;color:#555}
</style></head><body>
<a href="/dashboard" class="razor"><div class="name">RAZOR</div><div class="desc">LIVE TRADING</div></a>
<a href="/shadow" class="shadow"><div class="name">SHADOW</div><div class="desc">PAPER TESTING</div></a>
</body></html>"""


if __name__ == '__main__':
    init_razor_db()
    init_shadow_db()

    razor_thread = Thread(target=razor_loop, daemon=True)
    razor_thread.start()

    shadow_thread = Thread(target=shadow_loop, daemon=True)
    shadow_thread.start()

    app.run(host='0.0.0.0', port=PORT)
