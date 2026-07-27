"""What publishers admit, in their own timestamps.

The archive can only evidence a change it watched happen, which means the ledger
starts empty and fills over days. But for publishers whose timestamps are not
saturated, there is evidence available immediately — from the portal itself.

When a record describing a closed period carries a recent modification timestamp,
the publisher is asserting that it touched that record. That is weaker evidence
than an observed diff and the distinction is load-bearing:

    the publisher's timestamp tells us THAT a record changed.
    only our own prior observation can tell us WHAT it said before.

So these are reported as *assertions by the publisher*, never as observed
revisions, and they are kept in a separate section of the bundle. Conflating the
two would be borrowing the credibility of the archive to dress up a claim the
archive did not make.

This runs only against sources whose pipeline classification says the timestamps
mean something. On a republish portal every row carries a recent timestamp and
the query returns the entire table, which is not a finding about anything.
"""

from __future__ import annotations

import argparse
import json
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from .pipeline import PipelineClass
from .socrata import SocrataClient, SocrataError
from .store import Archive

log = logging.getLogger("palimpsest.retro")

# Classes where a modification timestamp is evidence rather than noise.
INFORMATIVE = {PipelineClass.INCREMENTAL, PipelineClass.STATIC}


def _age_bucket(event_iso: str | None, now: datetime) -> str:
    """How far back into the closed record does the modification reach?"""
    if not event_iso:
        return "unknown"
    try:
        d = datetime.fromisoformat(str(event_iso).replace("Z", "").split(".")[0])
    except ValueError:
        return "unknown"
    years = (now.replace(tzinfo=None) - d).days / 365.25
    if years < 2:
        return "1-2 years"
    if years < 4:
        return "2-4 years"
    if years < 7:
        return "4-7 years"
    if years < 12:
        return "7-12 years"
    return "12+ years"


def probe_source(
    client: SocrataClient, src: dict[str, Any], lookback_days: int = 30, sample: int = 40
) -> dict[str, Any] | None:
    """Count and sample the closed-period records a publisher says it modified."""
    domain, ff = src["domain"], src["fourfour"]
    date_field, stratum_end = src["date_field"], src["stratum_end"]
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=lookback_days)).strftime("%Y-%m-%dT%H:%M:%S")

    closed = f"{date_field} < '{stratum_end}'"
    touched = f"{closed} AND :updated_at > '{cutoff}'"

    try:
        total = client.scalar_count(domain, ff, closed)
        n = client.scalar_count(domain, ff, touched)
    except SocrataError as e:
        log.debug("retro probe failed for %s: %s", src["source_key"], e)
        return None

    if n == 0 or total == 0:
        return None

    try:
        # ":*,*" is the form SODA accepts for system fields alongside published
        # ones. Naming system columns individually beside "*" is rejected.
        rows = client.rows(
            domain, ff, select=":*,*", where=touched,
            order=":updated_at DESC", limit=sample,
        )
    except SocrataError as e:
        log.debug("sample fetch failed for %s: %s", src["source_key"], e)
        rows = []

    buckets = Counter(_age_bucket(r.get(date_field), now) for r in rows)
    oldest = None
    if rows:
        dated = [r.get(date_field) for r in rows if r.get(date_field)]
        if dated:
            oldest = min(str(d) for d in dated)

    return {
        "source_key": src["source_key"],
        "title": src["title"],
        "city": src["city"],
        "pipeline_class": src["pipeline_class"],
        "portal_url": f"https://{domain}/d/{ff}",
        "closed_period_records": total,
        "asserted_modified": n,
        "share": round(n / total, 6),
        "lookback_days": lookback_days,
        "cutoff": cutoff,
        "oldest_event_touched": oldest,
        "age_distribution": dict(buckets),
        "samples": [
            {
                "row_id": r.get(":id"),
                "created_at": r.get(":created_at"),
                "updated_at": r.get(":updated_at"),
                "event_date": r.get(date_field),
                "record": {
                    k: v for k, v in r.items()
                    if not k.startswith(":") and v not in (None, "")
                },
            }
            for r in rows[:6]
        ],
        "evidence_class": "publisher_assertion",
        # The republish threshold is a cliff at 50%, but a publisher rewriting
        # 46% of its closed past is not meaningfully more interpretable than one
        # rewriting 51%. Rather than quietly present such a source as a finding,
        # say that it sits close to the saturation boundary.
        "near_saturated": n / total > 0.2,
        "caveat": (
            "The publisher's own timestamp shows that this record was written "
            "recently. It does not show what the record previously said; only an "
            "independent prior observation can establish that."
        ) + (
            " This source rewrites a large share of its closed past, placing it "
            "close to the saturation boundary; treat the count as an upper bound "
            "on editing rather than a measure of it."
            if n / total > 0.2 else ""
        ),
    }


def run(arc: Archive, client: SocrataClient, lookback_days: int = 30) -> dict[str, Any]:
    sources = [s for s in arc.sources() if s.get("pipeline_class") in INFORMATIVE]
    log.info(
        "probing %d sources whose timestamps are informative (of %d watched)",
        len(sources), len(arc.sources()),
    )

    results = []
    for i, src in enumerate(sources, 1):
        r = probe_source(client, src, lookback_days)
        if r:
            results.append(r)
            log.info(
                "  %-3d %-44s %6s of %-10s (%.3f%%) oldest=%s",
                i, src["source_key"][-44:], f"{r['asserted_modified']:,}",
                f"{r['closed_period_records']:,}", r["share"] * 100,
                (r["oldest_event_touched"] or "")[:10],
            )

    results.sort(key=lambda r: -r["asserted_modified"])
    total_asserted = sum(r["asserted_modified"] for r in results)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "lookback_days": lookback_days,
        "sources_probed": len(sources),
        "sources_with_activity": len(results),
        "total_records_asserted_modified": total_asserted,
        "note": (
            "Counts closed-period records that the publisher's own metadata reports "
            "as recently written. Restricted to publishers whose timestamps are not "
            "saturated; on a republish portal this query returns the whole table and "
            "means nothing."
        ),
        "sources": results,
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Publisher-asserted retroactive writes.")
    ap.add_argument("--db", default="archive/palimpsest.db")
    ap.add_argument("--lookback", type=int, default=30)
    ap.add_argument("--out", default="archive/retroactive.json")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
    arc = Archive(args.db)
    res = run(arc, SocrataClient(), args.lookback)

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(res, fh, indent=1, ensure_ascii=False)

    log.info("")
    log.info(
        "%d of %d probed sources show closed-period writes; %s records in total",
        res["sources_with_activity"], res["sources_probed"],
        f"{res['total_records_asserted_modified']:,}",
    )
    log.info("wrote %s", args.out)
