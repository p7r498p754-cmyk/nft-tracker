CREATE TABLE IF NOT EXISTS monitor (
 id INTEGER PRIMARY KEY CHECK(id=1), lease_until INTEGER NOT NULL DEFAULT 0,
 last_attempt INTEGER, last_success INTEGER, gap_seconds INTEGER, last_error TEXT
);
INSERT OR IGNORE INTO monitor(id) VALUES (1);
CREATE TABLE IF NOT EXISTS catalog (
 at INTEGER PRIMARY KEY, collections INTEGER NOT NULL, snapshot TEXT NOT NULL
);
