"""Entry point — runs RAZOR scalper bot."""
from threading import Thread
from razor import razor_loop, app, init_razor_db

PORT = __import__('os').environ.get('PORT', 8080)

if __name__ == '__main__':
    init_razor_db()
    Thread(target=razor_loop, daemon=True).start()
    app.run(host='0.0.0.0', port=int(PORT))
