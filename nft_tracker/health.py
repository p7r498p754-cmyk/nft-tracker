"""Local read-only health probe for the polling host. Prints no user data."""
import json
import os
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from .portals import parse_date


def check(path, interval, now):
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
        row = db.execute("SELECT value FROM state WHERE key='cycle'").fetchone()
    cycle = json.loads(row[0]) if row else {}
    stamp = parse_date(cycle.get("ok_at"))
    if cycle.get("error") or not stamp:
        return "market_unavailable"
    if not 0 <= (now - stamp).total_seconds() <= max(180, interval * 2):
        return "market_stale"
    return "ok"


if __name__ == "__main__":
    try:
        status = check(os.environ["DB_PATH"], int(os.getenv("SCAN_SECONDS", "600")), datetime.now(timezone.utc))
    except (KeyError, OSError, ValueError, sqlite3.Error, AttributeError):
        status = "state_unavailable"
    print(status)
    sys.exit(int(status != "ok"))
