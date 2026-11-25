"""Rehearse an upgrade using temporary private copies of an existing database."""
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory

from .backup import snapshot
from .storage import Storage


def check(source):
    with TemporaryDirectory() as tmp:
        directory = Path(tmp)
        before, candidate, rollback = (directory / name for name in ("before", "candidate", "rollback"))
        snapshot(source, before)
        with closing(sqlite3.connect(before.as_uri() + "?mode=ro", uri=True)) as db:
            if not {"users", "state", "sales", "deliveries"} <= {
                name for name, in db.execute("SELECT name FROM sqlite_schema WHERE type='table'")
            }:
                raise ValueError("Source is not an NFT Tracker database")
        snapshot(before, candidate)
        Storage(candidate).close()
        with closing(sqlite3.connect(candidate.as_uri(), uri=True)) as db:
            db.execute("ATTACH DATABASE ? AS original", (before.as_uri() + "?mode=ro",))
            tables = db.execute("SELECT name FROM original.sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'")
            for name, in tables.fetchall():
                table = '"' + name.replace('"', '""') + '"'
                # Preserve both column values and multiplicities across additive migrations.
                columns = [row[1] for row in db.execute(f"PRAGMA original.table_info({table})")]
                projection = ",".join('"' + col.replace('"','""') + '"' for col in columns)
                fields = projection + ",count(*)"
                if db.execute(f"SELECT {fields} FROM original.{table} GROUP BY {projection} EXCEPT "
                              f"SELECT {fields} FROM main.{table} GROUP BY {projection} LIMIT 1").fetchone():
                    raise ValueError("Existing records changed during migration")
            if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)] or db.execute("PRAGMA foreign_key_check").fetchone():
                raise ValueError("Candidate integrity check failed")
        snapshot(before, rollback)
        with closing(sqlite3.connect(before)) as b, closing(sqlite3.connect(rollback)) as r:
            if list(b.iterdump()) != list(r.iterdump()):
                raise ValueError("Restored database differs from the backup")


if __name__ == "__main__":
    check(sys.argv[1])
    print("Migration preserved existing rows. Rollback restored the original schema and data.")
