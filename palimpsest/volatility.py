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
import re
from collections import Counter, defaultdict
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

#: A field that essentially never moves unless another specific field moves is
#: not independent evidence — it is that field's arithmetic.
DEPENDENCY_CONFIDENCE = 0.97

#: ...measured over at least this many movements, so a handful of coincidences
#: cannot manufacture a dependency.
MIN_DEPENDENCY_OBS = 8

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

CREATE TABLE IF NOT EXISTS field_dependency (
    source_key   TEXT NOT NULL,
    field        TEXT NOT NULL,   -- the dependent field
    driver       TEXT NOT NULL,   -- the field it follows
    observations INTEGER NOT NULL,
    conditional  REAL NOT NULL,   -- P(driver moved | field moved)
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
    dependency = _measure_dependency(archive, rows, recomputed)

    return {
        "fields_measured": written,
        "sources_with_recomputed_fields": len(recomputed),
        "recomputed_fields": {k: sorted(v) for k, v in recomputed.items() if v},
        "stability": stability,
        "dependency": dependency,
    }


#: Names of quantities that are computed from something else. Used only to
#: orient a dependency whose direction the movement counts cannot settle —
#: never to create one.
_DERIVED_SHAPE = re.compile(
    r"(_to_|days?$|_days|duration|elapsed|age|count|total|subtotal|sum|avg|"
    r"percent|pct|rate|_calc|coordinate|latitude|longitude)",
    re.I,
)


def _derived_shape(name: str) -> bool:
    return bool(_DERIVED_SHAPE.search(name))


def _measure_dependency(archive, rows, recomputed: dict[str, set[str]]) -> dict[str, Any]:
    """Find fields that only ever move when some other field moves.

    Two shapes of this were reported as separate edits, inflating one change
    into three:

        completed_date          2026-06-30 -> 2026-07-29
        submit_to_complete_biz          83 -> 103
        submit_to_complete_cal         119 -> 148

    The two counters are the interval between submission and completion. They
    are not additional facts about the permit; they are that date, subtracted.

        list_of_booked_charges  664/187A,205,245A1 -> 205,245A1,664/187A
        crime_type              Willful Homicide (Att.) -> Assault

    The charge list was re-sorted, and `crime_type` names whatever is in first
    position. Read as an independent field this says a prosecutor downgraded an
    attempted homicide to assault, which is a serious claim to get wrong.

    Neither is caught by measuring volatility: these fields are quiet most of the
    time, and move only when their driver moves. So measure the conditional
    instead — if a field has essentially never moved on its own, it is not
    independent evidence of anything.
    """
    from datetime import datetime, timezone

    # source -> field -> count of movements, and co-movement counts
    moves: dict[str, Counter] = defaultdict(Counter)
    co: dict[str, dict[str, Counter]] = defaultdict(lambda: defaultdict(Counter))

    for r in rows:
        try:
            fields = list(json.loads(r["field_deltas"]))
        except (TypeError, json.JSONDecodeError):
            continue
        if len(fields) < 1:
            continue
        src = r["source_key"]
        for f in fields:
            moves[src][f] += 1
            for g in fields:
                if g != f:
                    co[src][f][g] += 1

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    found: dict[str, dict[str, str]] = defaultdict(dict)

    for src, counts in moves.items():
        drop = recomputed.get(src, set())
        candidates: dict[str, tuple[str, int, float]] = {}

        for field, n in counts.items():
            if n < MIN_DEPENDENCY_OBS or field in drop:
                continue
            partners = co[src].get(field) or Counter()
            # A recomputed column moves in nearly every record, so everything
            # co-occurs with it. It explains nothing and cannot be a driver:
            # without this, `location` is found to "follow" `sr_age_days`, a
            # clock that advances for every record every day.
            partners = Counter({
                g: c for g, c in partners.items()
                if g not in drop and counts.get(g, 0) >= n
            })
            if not partners:
                continue
            driver, together = partners.most_common(1)[0]
            confidence = together / n
            if confidence >= DEPENDENCY_CONFIDENCE:
                candidates[field] = (driver, n, round(confidence, 4))

        # Two fields that always move together each look like the other's
        # consequence. Left as a cycle, both would be discounted and the change
        # would report zero independent fields. Keep one direction only: the
        # field that moves no more often than its driver is the dependent.
        #
        # When they move equally often the counts cannot decide it, and the
        # answer still matters for how the finding reads: a duration is derived
        # from a date, never the reverse. Reporting "completed_date follows
        # submit_to_complete_biz" is backwards even though it discounts the
        # right number of fields.
        for field, (driver, n, conf) in list(candidates.items()):
            back = candidates.get(driver)
            if back and back[0] == field:
                if n != back[1]:
                    if n > back[1]:
                        continue  # the more active field is not the dependent
                elif _derived_shape(field) != _derived_shape(driver):
                    if not _derived_shape(field):
                        continue  # the other one looks like the computed quantity
                elif field > driver:
                    continue  # nothing to choose between them; be deterministic
            archive.conn.execute(
                "INSERT OR REPLACE INTO field_dependency VALUES (?,?,?,?,?,?)",
                (src, field, driver, n, conf, now),
            )
            found[src][field] = driver

    archive.conn.commit()
    return {
        "sources_with_dependencies": len(found),
        "dependent_fields": sum(len(v) for v in found.values()),
        "by_source": {k: dict(v) for k, v in found.items()},
    }


def dependent_fields(archive, source_key: str) -> dict[str, str]:
    """Map of dependent field -> the field it follows, for one source."""
    try:
        return {
            r["field"]: r["driver"]
            for r in archive.conn.execute(
                "SELECT field, driver FROM field_dependency WHERE source_key=?",
                (source_key,),
            )
        }
    except Exception:
        return {}


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

    dep = res["dependency"]
    log.info("")
    log.info("=== fields that are other fields' arithmetic ===")
    log.info(
        "  %d dependent fields across %d sources",
        dep["dependent_fields"], dep["sources_with_dependencies"],
    )
    for src, fields in sorted(dep["by_source"].items())[:12]:
        title = arc.conn.execute(
            "SELECT title FROM sources WHERE source_key=?", (src,)
        ).fetchone()
        log.info("  %s", (title["title"] if title else src)[:60])
        for f, drv in sorted(fields.items())[:6]:
            log.info("      %-30s follows %s", f, drv)

    st = res["stability"]
    log.info("")
    log.info("=== key stability ===")
    log.info("  %d sources assessed, %d stable", st["sources_assessed"], st["stable"])
    for src in st["key_unstable"]:
        title = arc.conn.execute(
            "SELECT title FROM sources WHERE source_key=?", (src,)
        ).fetchone()
        log.info("  WITHDRAWN  %s", (title["title"] if title else src)[:62])
