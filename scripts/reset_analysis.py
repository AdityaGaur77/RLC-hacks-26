"""Clear all derived analysis so a run starts from observations alone.

Snapshots and observations are never touched: those are the archive. Everything
here is a conclusion drawn from them, and conclusions must be reproducible from
the evidence rather than inherited from the last attempt.
"""

import sqlite3
import sys

db = sys.argv[1] if len(sys.argv) > 1 else "archive/palimpsest.db"
conn = sqlite3.connect(db)

for table in (
    "changes",
    "diff_runs",
    "field_volatility",
    "source_stability",
    # Omitting this one left 71 rows from a previous run in place, including
    # both directions of dependencies whose cycles a later fix was meant to
    # break. The stale pairs cancelled each other out and the control silently
    # did nothing.
    "field_dependency",
):
    # deletion_confirmations is deliberately NOT in this list. A verdict from
    # the publisher is an observation about the world, not a classification we
    # derived, and it costs an hour of requests to obtain. Clearing it here
    # threw exactly that away once.
    try:
        n = conn.execute(f"SELECT COUNT(1) FROM {table}").fetchone()[0]
        conn.execute(f"DELETE FROM {table}")
        print(f"  cleared {table}: {n} rows")
    except sqlite3.OperationalError:
        print(f"  {table}: not present yet")

conn.commit()
conn.close()
