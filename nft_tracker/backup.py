"""Consistent SQLite snapshot. Destination is always a new private file."""
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path


def snapshot(source, destination):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    try:
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as src:
            with closing(sqlite3.connect(destination)) as dst:
                src.backup(dst)
                if dst.execute("PRAGMA integrity_check").fetchall() != [("ok",)] or dst.execute("PRAGMA foreign_key_check").fetchone():
                    raise ValueError("Повреждённая база")
    except BaseException:
        destination.unlink()
        raise


if __name__ == "__main__":
    snapshot(*sys.argv[1:])
