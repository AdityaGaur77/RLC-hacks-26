"""Ask the publisher whether a record we call deleted is actually gone.

Deletion is the strongest claim this project makes, and the archive is the wrong
instrument to settle it alone. The archive sees one window of one dataset. A
record can leave that window for reasons that are not removal:

* **Its key was reformatted.** San Francisco published 676 parking citations with
  a leading quote — `"1006622610`, the artefact of a spreadsheet forcing a value
  to text — and later cleaned them. The old identity vanished and a new one
  appeared. Every one of those citations is still in the dataset. Reported
  naively, that is 676 deleted parking citations.

* **Its date was edited**, moving it outside the observation window while it sits
  perfectly intact in the dataset.

Neither can be distinguished from removal by comparing snapshots, because in the
archive's view the record is simply no longer there. Only the source can answer
it, so before a deletion is published, the source is asked.

This costs one request per claimed deletion. That is a lot of requests to avoid
saying something false, which is the correct trade.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from .socrata import SocrataClient, SocrataError
from .store import Archive

log = logging.getLogger("palimpsest.confirm")

# Values arrive wrapped in the residue of whatever produced them.
_WRAPPERS = "\"' \t\r\n"


def normalise_key(v: Any) -> str:
    return re.sub(r"\s+", " ", str(v).strip(_WRAPPERS)).strip()


def _escape(v: str) -> str:
    return v.replace("'", "''")


def confirm_deletion(
    client: SocrataClient, src: dict[str, Any], row_uid: str
) -> dict[str, Any]:
    """Is this record absent from the dataset, or merely absent from our window?"""
    key_cols = src["business_key"]
    if not key_cols or len(key_cols) != 1:
        return {"verdict": "unverifiable", "reason": "no single-column natural key"}
    key = key_cols[0]
    domain, ff, date_field = src["domain"], src["fourfour"], src["date_field"]

    exact = str(row_uid)
    cleaned = normalise_key(row_uid)

    # Try the identity as recorded, then the same identity with the publisher's
    # formatting residue removed. A hit on the second means the key was
    # reformatted, not that anything was deleted.
    for candidate, verdict in ((exact, "still_present"), (cleaned, "key_reformatted")):
        if verdict == "key_reformatted" and candidate == exact:
            continue
        try:
            rows = client.rows(
                domain, ff, select=f"{key},{date_field}",
                where=f"{key}='{_escape(candidate)}'", limit=1,
            )
        except SocrataError as e:
            return {"verdict": "unverifiable", "reason": str(e)[:200]}
        if rows:
            seen = rows[0].get(date_field)
            inside = (
                src["stratum_start"] <= str(seen) < src["stratum_end"]
                if seen else None
            )
            return {
                "verdict": verdict,
                "found_as": candidate,
                "date_field_value": seen,
                "inside_window": inside,
                "reason": (
                    "record is still published under a reformatted key"
                    if verdict == "key_reformatted"
                    else (
                        "record is still published; it left the observation window "
                        "rather than the dataset"
                        if inside is False
                        else "record is still published"
                    )
                ),
            }

    return {"verdict": "confirmed_absent",
            "reason": "not present in the dataset under either form of its key"}


def run(archive: Archive, client: SocrataClient, limit: int | None = None) -> dict[str, Any]:
    sources = {s["source_key"]: s for s in archive.sources()}
    rows = archive.conn.execute(
        "SELECT change_id, source_key, row_uid FROM changes WHERE kind='deletion' "
        "ORDER BY source_key, row_uid"
    ).fetchall()
    if limit:
        rows = rows[:limit]

    log.info("confirming %d claimed deletions against their sources", len(rows))
    tally: dict[str, int] = {}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    for i, r in enumerate(rows, 1):
        src = sources.get(r["source_key"])
        if not src:
            continue
        res = confirm_deletion(client, src, r["row_uid"])
        v = res["verdict"]
        tally[v] = tally.get(v, 0) + 1

        if v == "confirmed_absent":
            archive.conn.execute(
                "UPDATE changes SET detail = detail || ' | confirmed against the "
                "source: absent under either form of its key' WHERE change_id=?",
                (r["change_id"],),
            )
        elif v in ("still_present", "key_reformatted"):
            # Withdraw the claim. It stays in the record, as what it actually is.
            archive.conn.execute(
                "UPDATE changes SET kind='left_observation_window', significance=0.1, "
                "detail=? WHERE change_id=?",
                (
                    f"withdrawn on {now}: {res['reason']}"
                    + (f" (found as {res['found_as']!r})" if res.get("found_as") else ""),
                    r["change_id"],
                ),
            )

        if i % 100 == 0:
            archive.conn.commit()
            log.info("  %d/%d  %s", i, len(rows), tally)

    archive.conn.commit()
    log.info("done: %s", tally)
    return {"checked": len(rows), "verdicts": tally, "confirmed_at": now}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Confirm claimed deletions against sources.")
    ap.add_argument("--db", default="archive/palimpsest.db")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--out", default="archive/deletion_confirmation.json")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
    arc = Archive(args.db)
    res = run(arc, SocrataClient(), args.limit)

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(res, fh, indent=1)

    v = res["verdicts"]
    total = res["checked"] or 1
    log.info("")
    log.info("  confirmed absent      : %d (%.0f%%)",
             v.get("confirmed_absent", 0), 100 * v.get("confirmed_absent", 0) / total)
    log.info("  still published       : %d", v.get("still_present", 0))
    log.info("  key was reformatted   : %d", v.get("key_reformatted", 0))
    log.info("  could not verify      : %d", v.get("unverifiable", 0))
