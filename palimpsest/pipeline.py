"""Characterise how a publisher actually pushes data, before interpreting it.

This module exists because of a specific near-miss.

Chicago's crime dataset reports that 7,808,602 of its 7,981,155 pre-2024 records
were modified within the last thirty days — 98%, spread evenly across every year
back to 2001. Read literally, a city rewrote eight million crime records. Read
correctly, the publisher drops and reloads the table on a schedule, so every row
receives a fresh internal id and a fresh modification timestamp while not one
published fact changes. Records describing April 2001 have a creation timestamp
of yesterday.

San Francisco's 311 dataset reports zero such modifications. New York's reports
4,484. These numbers are not comparable, and the difference is not data quality —
it is publishing architecture. A tool that ranks cities by "edit rate" without
establishing this first is measuring deployment style and calling it integrity.

So: characterise the pipeline, then interpret. Never the reverse.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from .socrata import SocrataClient, SocrataError

log = logging.getLogger("palimpsest.pipeline")


class PipelineClass:
    #: Table is dropped and reloaded; internal ids regenerate. Platform
    #: timestamps carry no information about editorial change whatsoever.
    REPUBLISH = "republish"

    #: Rows persist (ids stable) but the publisher touches all of them on each
    #: run. Timestamps are unusable; content hashing still works.
    BULK_TOUCH = "bulk_touch"

    #: Rows are updated selectively. Platform timestamps are meaningful, and old
    #: records with recent timestamps are genuine candidate retroactive edits.
    INCREMENTAL = "incremental"

    #: The frozen past is not being touched at all.
    STATIC = "static"

    UNKNOWN = "unknown"


DESCRIPTIONS = {
    PipelineClass.REPUBLISH: (
        "Publisher reloads the entire table. Internal row ids regenerate on every "
        "run, so platform timestamps say nothing about whether a fact changed. "
        "Only content hashing can detect revision here."
    ),
    PipelineClass.BULK_TOUCH: (
        "Rows persist but every one is rewritten on each run. Platform timestamps "
        "are saturated and carry no editorial signal. Content hashing required."
    ),
    PipelineClass.INCREMENTAL: (
        "Publisher updates records selectively. Platform timestamps are "
        "informative: an old record carrying a recent modification timestamp is a "
        "genuine candidate for retroactive revision."
    ),
    PipelineClass.STATIC: (
        "The closed past is not being touched. Platform timestamps are informative "
        "here by virtue of having no noise floor: any row acquiring a recent "
        "modification timestamp would stand out immediately."
    ),
    PipelineClass.UNKNOWN: "Could not be determined.",
}


def characterise(
    client: SocrataClient, src: dict[str, Any], lookback_days: int = 30
) -> dict[str, Any]:
    """Probe one source and classify its publishing behaviour.

    Costs four requests regardless of dataset size.
    """
    domain, ff = src["domain"], src["fourfour"]
    date_field, stratum_end = src["date_field"], src["stratum_end"]
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=lookback_days)
    ).strftime("%Y-%m-%dT%H:%M:%S")

    frozen = f"{date_field} < '{stratum_end}'"
    out: dict[str, Any] = {
        "source_key": src["source_key"],
        "lookback_days": lookback_days,
        "cutoff": cutoff,
    }

    try:
        total = client.scalar_count(domain, ff, frozen)
        out["frozen_past_count"] = total
        if total == 0:
            out["pipeline_class"] = PipelineClass.UNKNOWN
            out["reason"] = "frozen past is empty"
            return out

        updated_recent = client.scalar_count(
            domain, ff, f"{frozen} AND :updated_at > '{cutoff}'"
        )
        created_recent = client.scalar_count(
            domain, ff, f"{frozen} AND :created_at > '{cutoff}'"
        )

        out["updated_recent"] = updated_recent
        out["created_recent"] = created_recent
        out["updated_ratio"] = round(updated_recent / total, 6)
        out["created_ratio"] = round(created_recent / total, 6)

        # How many distinct modification instants are there? A reload stamps a
        # single instant across the whole table; genuine editing spreads out.
        try:
            sample = client.rows(
                domain, ff, select=":updated_at", where=frozen,
                order=":updated_at DESC", limit=1000,
            )
            stamps = {r.get(":updated_at") for r in sample if r.get(":updated_at")}
            out["distinct_stamps_in_1000"] = len(stamps)
        except SocrataError:
            out["distinct_stamps_in_1000"] = None

    except SocrataError as e:
        out["pipeline_class"] = PipelineClass.UNKNOWN
        out["reason"] = f"probe failed: {e}"
        return out

    ur, cr = out["updated_ratio"], out["created_ratio"]

    # A record describing 2019 whose row was *created* last month means the row
    # is not the original — the table was rebuilt beneath it.
    if cr > 0.5:
        cls, reason = PipelineClass.REPUBLISH, (
            f"{cr:.1%} of records describing closed periods were created within "
            f"the last {lookback_days} days — the table is being rebuilt, not edited"
        )
    elif ur > 0.5:
        cls, reason = PipelineClass.BULK_TOUCH, (
            f"{ur:.1%} of the frozen past carries a modification timestamp inside "
            f"{lookback_days} days while ids remain stable — every row is touched per run"
        )
    elif ur > 0.0005:
        cls, reason = PipelineClass.INCREMENTAL, (
            f"{updated_recent:,} of {total:,} records ({ur:.3%}) in the frozen past "
            f"were modified recently — selective, therefore meaningful"
        )
    else:
        cls, reason = PipelineClass.STATIC, (
            f"only {updated_recent:,} of {total:,} records show recent modification"
        )

    out["pipeline_class"] = cls
    out["reason"] = reason

    # A static publisher's timestamps are not merely usable, they are the
    # cleanest available: with no modification noise floor, a single row bearing
    # a recent timestamp stands out unambiguously. Only saturation destroys the
    # signal. Counting `static` as uninterpretable would inflate the headline
    # from 49% to 80% — the precise species of overclaim this project exists to
    # refuse, and it would be indefensible to commit it in our own reporting.
    out["timestamps_informative"] = cls in (
        PipelineClass.INCREMENTAL,
        PipelineClass.STATIC,
    )
    return out


def candidate_retroactive_edits(
    client: SocrataClient, src: dict[str, Any], lookback_days: int = 30, limit: int = 50
) -> list[dict[str, Any]]:
    """For incremental publishers only: fetch old records the publisher says it changed.

    This is evidence available immediately, without waiting for our own second
    observation — but it is the *publisher's own assertion* that these rows were
    modified, not something we watched happen. Reported as such. It tells us a
    record was touched; it cannot tell us what it previously said.
    """
    domain, ff = src["domain"], src["fourfour"]
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=lookback_days)
    ).strftime("%Y-%m-%dT%H:%M:%S")
    where = (
        f"{src['date_field']} < '{src['stratum_end']}' AND :updated_at > '{cutoff}'"
    )
    try:
        return client.rows(
            domain, ff, select=":*,*", where=where,
            order=":updated_at DESC", limit=limit,
        )
    except SocrataError as e:
        log.debug("candidate fetch failed for %s: %s", src["source_key"], e)
        return []


def run(archive, client: SocrataClient, lookback_days: int = 30) -> list[dict[str, Any]]:
    """Characterise every watched source and persist the classification."""
    results = []
    sources = archive.sources()
    log.info("characterising %d sources", len(sources))
    for i, src in enumerate(sources, 1):
        res = characterise(client, src, lookback_days)
        archive.set_pipeline_class(
            src["source_key"], res["pipeline_class"], res.get("reason", "")
        )
        results.append(res)
        log.info(
            "  %-3d %-13s %-42s %s",
            i, res["pipeline_class"], src["source_key"][-42:],
            res.get("reason", "")[:88],
        )
    return results


def census(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarise how the watched portals publish, as a finding in its own right."""
    by_class: dict[str, int] = {}
    for r in results:
        by_class[r["pipeline_class"]] = by_class.get(r["pipeline_class"], 0) + 1
    informative = sum(1 for r in results if r.get("timestamps_informative"))
    return {
        "total": len(results),
        "by_class": dict(sorted(by_class.items(), key=lambda t: -t[1])),
        "timestamps_informative": informative,
        "timestamps_useless": len(results) - informative,
        "share_uninterpretable": (
            round(1 - informative / len(results), 3) if results else None
        ),
    }


if __name__ == "__main__":
    import argparse
    import json

    from .store import Archive

    ap = argparse.ArgumentParser(description="Characterise publishing pipelines.")
    ap.add_argument("--db", default="archive/palimpsest.db")
    ap.add_argument("--lookback", type=int, default=30)
    ap.add_argument("--out", default="archive/pipeline_census.json")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
    arc = Archive(args.db)
    cli = SocrataClient()
    res = run(arc, cli, args.lookback)
    cen = census(res)

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"census": cen, "sources": res}, fh, indent=2)

    log.info("")
    log.info("=== publishing census ===")
    for k, v in cen["by_class"].items():
        log.info("  %-13s %3d   %s", k, v, DESCRIPTIONS[k][:74])
    log.info("")
    log.info(
        "platform timestamps are uninterpretable for %d of %d watched datasets (%.0f%%)",
        cen["timestamps_useless"], cen["total"], 100 * (cen["share_uninterpretable"] or 0),
    )
