"""Learn which fields are recomputed, instead of guessing from their names.

Name patterns caught `refresh_time` and `data_loaded_at`. They cannot catch
these, which were found only by looking at what the archive actually observed:

    sr_age_days      417 -> 418     the age of a service request, in days
    date_added       Jan -> Jul     when the row entered this export
    the_geom         changes daily  a spatial join recomputed on publish
    supervisor_districts            ditto

`sr_age_days` is not a fact about a service request. It is a clock: it advances
for every record, every day, forever. Treating it as content means every record
in the dataset appears to be edited daily, which is both false and — because it
is so uniform — obviously false the moment anyone looks.

So volatility is measured rather than assumed. A field that moves in nearly
every record on nearly every sweep is not evidence about any individual record;
it is the publisher recomputing a column. This is a property of observed
behaviour, and the archive is exactly the instrument for observing it.

The same measurement answers a second question. Once recomputed fields are set
aside, a source where *most records still change on every sweep* does not have a
stable identifier: its key is re-binding to different real-world entities
between publications. Seattle's permit review data changes `permitnum` in 99.5%
of apparent revisions — a permit number that changes is not identifying a
permit. For such a source, per-record revision claims are withdrawn entirely;
only aggregate movement can be honestly reported.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from typing import Any

log = logging.getLogger("palimpsest.volatility")

#: A field changing in at least this share of revised records, on a given sweep,
#: is behaving like a recomputed column rather than an edited value.
RECOMPUTED_SHARE = 0.90

#: ...and it must do so across at least this many sweeps before we act, so a
#: single genuine mass correction is not mistaken for a recomputation.
MIN_PAIRS = 3

#: After recomputed fields are excluded, a source still revising this share of
#: its records on most sweeps is not tracking stable entities.
UNSTABLE_KEY_SHARE = 0.5

#: This many columns all behaving as "recomputed" is not recomputation. It is a
#: key that has re-bound to a different entity, and the breadth of the symptom
#: must not be allowed to explain it away one column at a time.
MANY_FIELDS_RECOMPUTED = 4

SCHEMA = """
CREATE TABLE IF NOT EXISTS field_volatility (
    source_key   TEXT NOT NULL,
    field        TEXT NOT NULL,
    pairs_seen   INTEGER NOT NULL,
    mean_share   REAL NOT NULL,
    verdict      TEXT NOT NULL,   -- recomputed | ordinary
    measured_at  TEXT NOT NULL,
    PRIMARY KEY (source_key, field)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS source_stability (
    source_key      TEXT PRIMARY KEY,
    pairs_seen      INTEGER NOT NULL,
    mean_revised    REAL NOT NULL,   -- after recomputed fields are excluded
    verdict         TEXT NOT NULL,   -- stable | key_unstable
    measured_at     TEXT NOT NULL,
    detail          TEXT
);
"""


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def measure(archive) -> dict[str, Any]:
    """Measure field volatility and key stability for every source."""
    archive.conn.executescript(SCHEMA)
    archive.conn.commit()

    # Every per-record observation of a field moving, whatever it was later
    # classified as. Filtering by kind here would be circular: a change already
    # judged to be lifecycle progression or churn would become invisible to the
    # measurement that is supposed to inform that judgement, and the second pass
    # would find nothing because the first pass had already hidden it.
    rows = archive.conn.execute(
        "SELECT source_key, from_snapshot, to_snapshot, kind, field_deltas "
        "FROM changes WHERE field_deltas IS NOT NULL AND row_uid IS NOT NULL"
    ).fetchall()

    # (source, pair) -> {field: count}, plus the number of revisions in that pair
    per_pair: dict[tuple, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    pair_totals: dict[tuple, int] = defaultdict(int)

    for r in rows:
        pair = (r["source_key"], r["from_snapshot"], r["to_snapshot"])
        pair_totals[pair] += 1
        try:
            deltas = json.loads(r["field_deltas"])
        except (TypeError, json.JSONDecodeError):
            continue
        for f in deltas:
            per_pair[pair][f] += 1

    # Per source: for each field, the share of revised records it appeared in,
    # averaged over sweeps.
    shares: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for pair, fields in per_pair.items():
        src = pair[0]
        total = pair_totals[pair]
        if total < 20:  # too few revisions to characterise a field's behaviour
            continue
        for f, n in fields.items():
            shares[src][f].append(n / total)

    recomputed: dict[str, set[str]] = defaultdict(set)
    written = 0
    for src, fields in shares.items():
        for f, ss in fields.items():
            mean = sum(ss) / len(ss)
            verdict = (
                "recomputed"
                if mean >= RECOMPUTED_SHARE and len(ss) >= MIN_PAIRS
                else "ordinary"
            )
            if verdict == "recomputed":
                recomputed[src].add(f)
            archive.conn.execute(
                "INSERT OR REPLACE INTO field_volatility VALUES (?,?,?,?,?,?)",
                (src, f, len(ss), round(mean, 4), verdict, _now()),
            )
            written += 1
    archive.conn.commit()

    stability = _measure_stability(archive, per_pair, pair_totals, recomputed)

    return {
        "fields_measured": written,
        "sources_with_recomputed_fields": len(recomputed),
        "recomputed_fields": {k: sorted(v) for k, v in recomputed.items() if v},
        "stability": stability,
    }


def _measure_stability(
    archive, per_pair, pair_totals, recomputed: dict[str, set[str]]
) -> dict[str, Any]:
    """Decide, per source, whether its key identifies stable entities."""
    # Stratum size per snapshot, to turn revision counts into a share.
    sizes = {
        r["snapshot_id"]: r["row_count"]
        for r in archive.conn.execute(
            "SELECT snapshot_id, row_count FROM snapshots WHERE status='ok'"
        )
    }

    by_source: dict[str, list[float]] = defaultdict(list)
    for pair, fields in per_pair.items():
        src, a, b = pair
        size = sizes.get(b) or sizes.get(a) or 0
        if not size:
            continue
        # Count only revisions that touch at least one field we still believe.
        substantive = 0
        drop = recomputed.get(src, set())
        for f, n in fields.items():
            if f not in drop:
                substantive = max(substantive, n)
        by_source[src].append(min(1.0, substantive / size))

    unstable, results = [], {}
    for src, shares in by_source.items():
        if len(shares) < MIN_PAIRS:
            continue
        mean = sum(shares) / len(shares)
        wide = recomputed.get(src, set())

        # A publisher recomputing a derived column touches one or two fields: a
        # clock, a spatial join. When nearly every column of a record moves in
        # nearly every record, the record is not being recomputed -- it is a
        # different record wearing the same key. Seattle's permit data changes
        # `permitnum` itself in 99.5% of apparent revisions, and a permit number
        # that changes is not identifying a permit.
        #
        # Without this, the breadth of the problem conceals it: every field gets
        # excused as "recomputed" and the source is pronounced stable.
        breadth_implies_rebinding = len(wide) >= MANY_FIELDS_RECOMPUTED

        if breadth_implies_rebinding:
            verdict = "key_unstable"
            detail = (
                f"{len(wide)} separate columns change in nearly every record on "
                f"nearly every sweep. Recomputation touches a column or two; this "
                f"is the whole record changing, which means the key is binding to a "
                f"different entity between publications. Per-record revision claims "
                f"are withdrawn for this source"
            )
        elif mean >= UNSTABLE_KEY_SHARE:
            verdict = "key_unstable"
            detail = (
                f"after excluding recomputed columns, {mean:.0%} of records still "
                f"differ on a typical sweep; a key identifying stable entities does "
                f"not behave this way, so per-record revision claims are withdrawn "
                f"for this source"
            )
        else:
            verdict = "stable"
            detail = f"{mean:.1%} of records differ on a typical sweep"
        archive.conn.execute(
            "INSERT OR REPLACE INTO source_stability VALUES (?,?,?,?,?,?)",
            (src, len(shares), round(mean, 4), verdict, _now(), detail),
        )
        results[src] = verdict
        if verdict == "key_unstable":
            unstable.append(src)
    archive.conn.commit()

    return {
        "sources_assessed": len(results),
        "key_unstable": sorted(unstable),
        "stable": sum(1 for v in results.values() if v == "stable"),
    }


def recomputed_fields(archive, source_key: str) -> set[str]:
    return {
        r["field"]
        for r in archive.conn.execute(
            "SELECT field FROM field_volatility WHERE source_key=? AND verdict='recomputed'",
            (source_key,),
        )
    }


def is_key_unstable(archive, source_key: str) -> bool:
    r = archive.conn.execute(
        "SELECT verdict FROM source_stability WHERE source_key=?", (source_key,)
    ).fetchone()
    return bool(r and r["verdict"] == "key_unstable")


def unstable_sources(archive) -> dict[str, str]:
    try:
        return {
            r["source_key"]: r["detail"]
            for r in archive.conn.execute(
                "SELECT source_key, detail FROM source_stability WHERE verdict='key_unstable'"
            )
        }
    except Exception:
        return {}


if __name__ == "__main__":
    import argparse

    from .store import Archive

    ap = argparse.ArgumentParser(description="Measure field volatility and key stability.")
    ap.add_argument("--db", default="archive/palimpsest.db")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
    arc = Archive(args.db)
    res = measure(arc)

    log.info("measured %d source/field pairs", res["fields_measured"])
    log.info("")
    log.info("=== columns the publisher recomputes (excluded from revision claims) ===")
    for src, fields in sorted(res["recomputed_fields"].items()):
        title = arc.conn.execute(
            "SELECT title FROM sources WHERE source_key=?", (src,)
        ).fetchone()
        log.info("  %s", (title["title"] if title else src)[:64])
        log.info("      %s", ", ".join(fields[:10]))

    st = res["stability"]
    log.info("")
    log.info("=== key stability ===")
    log.info("  %d sources assessed, %d stable", st["sources_assessed"], st["stable"])
    for src in st["key_unstable"]:
        title = arc.conn.execute(
            "SELECT title FROM sources WHERE source_key=?", (src,)
        ).fetchone()
        log.info("  WITHDRAWN  %s", (title["title"] if title else src)[:62])
