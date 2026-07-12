"""Ad-hoc DB browser for manual testing. Run with no args to dump every
table, or pass table names to see only those.

    python scripts/inspect_db.py
    python scripts/inspect_db.py call_session approval_request
"""
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).resolve().parents[1] / "ivcc.db"


def main() -> None:
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row

    tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    wanted = sys.argv[1:] or tables

    for table in wanted:
        if table not in tables:
            print(f"-- unknown table: {table} --")
            continue
        rows = db.execute(f"SELECT * FROM {table}").fetchall()
        print(f"\n=== {table} ({len(rows)} rows) ===")
        for row in rows:
            print(dict(row))


if __name__ == "__main__":
    main()
