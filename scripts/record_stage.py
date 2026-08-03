"""Record how many changes each analysis stage produced.

The project's central claim is a ratio: apparent change before controlling for
publishing mechanism, against what survives afterwards. Quoting that from a
console log would make the headline unverifiable, so both counts are written
into the archive and the site reads them from there.
"""

import sqlite3
import sys
from datetime import datetime, timezone

db = sys.argv[1] if len(sys.argv) > 1 else "archive/palimpsest.db"
stage = sys.argv[2] if len(sys.argv) > 2 else "probe"

conn = sqlite3.connect(db)
conn.execute("""
CREATE TABLE IF NOT EXISTS analysis_stages (
    stage       TEXT PRIMARY KEY,   -- probe | controlled
    change_count INTEGER NOT NULL,
    recorded_at TEXT NOT NULL
)
""")

n = conn.execute("SELECT COUNT(1) FROM changes").fetchone()[0]
conn.execute(
    "INSERT OR REPLACE INTO analysis_stages VALUES (?,?,?)",
    (stage, n, datetime.now(timezone.utc).isoformat(timespec="seconds")),
)
conn.commit()

rows = dict(conn.execute("SELECT stage, change_count FROM analysis_stages"))
probe, controlled = rows.get("probe"), rows.get("controlled")
print(f"  {stage}: {n:,} changes")
if probe and controlled is not None and probe:
    print(f"  -> {1 - controlled / probe:.1%} of apparent change was publishing mechanism")
conn.close()
