"""Private-host WSGI entry point. Use one process and Telegram max_connections=1."""
import hmac
import json
import os
from threading import Lock

from .bot import handle_update
from .config import Settings
from .monitor import utcnow
from .storage import Storage
from .telegram import TelegramClient


def create_app(cfg, secret, telegram):
    if not secret or len(secret) < 32 or cfg.on_actions:
        raise ValueError("Webhook requires a private host and a separate secret.")
    lock = Lock()

    def app(env, respond):
        def finish(status):
            respond(status, [("Content-Length", "0")])
            return [b""]

        if env.get("PATH_INFO") != "/telegram" or env.get("REQUEST_METHOD") != "POST":
            return finish("404 Not Found")
        header = env.get("HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN", "")
        if not hmac.compare_digest(header.encode(), secret.encode()):
            return finish("403 Forbidden")
        try:
            size = int(env.get("CONTENT_LENGTH", ""))
            if not 0 < size <= 1024 * 1024:
                return finish("413 Payload Too Large")
            update = json.loads(env["wsgi.input"].read(size))
            if not isinstance(update, dict) or type(update.get("update_id")) is not int:
                return finish("400 Bad Request")
        except (ValueError, UnicodeError):
            return finish("400 Bad Request")

        with lock:
            store = Storage(cfg.db_path)
            try:
                with store.db:
                    store.db.execute("CREATE TABLE IF NOT EXISTS webhook_seen(id INTEGER PRIMARY KEY,at INTEGER NOT NULL)")
                if not store.db.execute("SELECT 1 FROM webhook_seen WHERE id=?", (update["update_id"],)).fetchone():
                    now = utcnow()
                    handle_update(cfg, store, telegram, update, now)
                    with store.db:
                        store.db.execute("INSERT INTO webhook_seen VALUES (?,?)", (update["update_id"], int(now.timestamp())))
                        store.db.execute("DELETE FROM webhook_seen WHERE at<?", (int(now.timestamp())-172800,))
            finally:
                store.close()
        return finish("200 OK")
    return app


def from_env():
    cfg = Settings.from_env(need_portals=False)
    return create_app(cfg, os.environ["TELEGRAM_WEBHOOK_SECRET"], TelegramClient(cfg.telegram_token))
