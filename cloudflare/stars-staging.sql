CREATE TABLE IF NOT EXISTS orders (
    payload TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    plan TEXT NOT NULL,
    stars INTEGER NOT NULL CHECK (stars > 0),
    expires_at INTEGER NOT NULL,
    checkout_id TEXT UNIQUE,
    charge_id TEXT UNIQUE,
    paid_at INTEGER,
    refunded INTEGER NOT NULL DEFAULT 0 CHECK (refunded IN (0, 1))
);
CREATE UNIQUE INDEX IF NOT EXISTS pending_checkout
    ON orders(user_id) WHERE checkout_id IS NOT NULL AND charge_id IS NULL;
CREATE INDEX IF NOT EXISTS user_access ON orders(user_id, paid_at);

CREATE TABLE IF NOT EXISTS staging_webhook_mode (
    mode TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status='ready')
);
