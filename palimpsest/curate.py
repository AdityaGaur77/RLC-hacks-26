"""Prune the watchlist.

Discovery ranks candidates by subject-matter consequence and takes the best that
pass, so the tail of each portal is weak: datasets with a handful of records in
the observation window. Watching those costs four requests every sweep for a week
and can evidence almost nothing — a stratum of eleven records is not a sample of
anything, and a change in it is as likely to be noise as signal.

Removing them is not merely an optimisation. Requests spent on a reference table
of speed camera locations are requests not spent on arrest records.
"""

from __future__ import annotations

import argparse
import json
import logging
from typing import Any

from .store import Archive

log = logging.getLogger("palimpsest.curate")

MIN_STRATUM_ROWS = 120


def _stratum_rows(arc: Archive, source_key: str, notes: str | None) -> int | None:
    """Prefer an observed row count; fall back to the discovery-time estimate."""
    row = arc.conn.execute(
        "SELECT row_count FROM snapshots WHERE source_key=? AND status='ok' "
        "ORDER BY snapshot_id DESC LIMIT 1",
        (source_key,),
    ).fetchone()
    if row and row["row_count"] is not None:
        return int(row["row_count"])
    if notes and "stratum_rows_at_discovery=" in notes:
        try:
            return int(notes.split("stratum_rows_at_discovery=")[1].split()[0])
        except (ValueError, IndexError):
            return None
    return None


def plan(arc: Archive, min_rows: int = MIN_STRATUM_ROWS) -> list[dict[str, Any]]:
    doomed = []
    for src in arc.sources():
        n = _stratum_rows(arc, src["source_key"], src.get("notes"))
        if n is not None and n < min_rows:
            doomed.append({
                "source_key": src["source_key"],
                "title": src["title"],
                "city": src["city"],
                "stratum_rows": n,
            })
    return doomed


def prune(arc: Archive, min_rows: int = MIN_STRATUM_ROWS, dry_run: bool = True) -> dict[str, Any]:
    doomed = plan(arc, min_rows)
    if not dry_run and doomed:
        keys = [d["source_key"] for d in doomed]
        with arc.tx() as c:
            marks = ",".join("?" for _ in keys)
            snap_ids = [
                r[0] for r in c.execute(
                    f"SELECT snapshot_id FROM snapshots WHERE source_key IN ({marks})",
                    keys,
                ).fetchall()
            ]
            if snap_ids:
                sm = ",".join("?" for _ in snap_ids)
                c.execute(f"DELETE FROM observations WHERE snapshot_id IN ({sm})", snap_ids)
                c.execute(f"DELETE FROM diff_runs WHERE to_snapshot IN ({sm})", snap_ids)
            c.execute(f"DELETE FROM changes WHERE source_key IN ({marks})", keys)
            c.execute(f"DELETE FROM snapshots WHERE source_key IN ({marks})", keys)
            c.execute(f"DELETE FROM sources WHERE source_key IN ({marks})", keys)

        # Payloads referenced by nothing are now dead weight.
        with arc.tx() as c:
            c.execute(
                "DELETE FROM blobs WHERE content_hash NOT IN "
                "(SELECT DISTINCT content_hash FROM observations)"
            )

    return {
        "removed": len(doomed) if not dry_run else 0,
        "would_remove": len(doomed),
        "remaining": len(arc.sources()),
        "detail": doomed,
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Prune weak sources from the watchlist.")
    ap.add_argument("--db", default="archive/palimpsest.db")
    ap.add_argument("--min-rows", type=int, default=MIN_STRATUM_ROWS)
    ap.add_argument("--apply", action="store_true", help="actually delete (default is a dry run)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    arc = Archive(args.db)
    res = prune(arc, args.min_rows, dry_run=not args.apply)

    for d in res["detail"]:
        log.info("  %-5s %-52s %s", d["stratum_rows"], d["title"][:52], d["city"])
    verb = "removed" if args.apply else "would remove"
    log.info("")
    log.info(
        "%s %d sources with fewer than %d records in the observation window; %d remain",
        verb, res["would_remove"], args.min_rows, res["remaining"],
    )
    if not args.apply:
        log.info("(dry run — pass --apply to delete)")
