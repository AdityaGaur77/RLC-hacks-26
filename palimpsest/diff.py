"""Compare consecutive observations and classify what moved.

Three distinctions carry this module, and getting any of them wrong produces a
confident, false headline.

**Content versus provenance.** A republished table bumps every internal id and
timestamp while changing nothing that was published. That is churn. Only a
change in the content hash is a revision.

**Coordinated versus isolated.** Five thousand records changing ``BURGLARY`` to
``Burglary`` on the same sweep is a formatting migration. One homicide record
changing classification on its own is a different kind of event entirely. Both
alter the content hash identically, so they must be told apart afterwards, by
looking at whether a revision is part of a pattern.

**Absence versus deletion.** A record missing from the stratum may have been
deleted, or its event date may have been edited so it now falls outside the
window. These are genuinely different and we can often distinguish them, so we
should not collapse both into the more alarming label.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable

log = logging.getLogger("palimpsest.diff")


class ChangeKind:
    SEMANTIC_REVISION = "semantic_revision"
    COORDINATED_REVISION = "coordinated_revision"
    DELETION = "deletion"
    RETROACTIVE_APPEND = "retroactive_append"
    PROVENANCE_CHURN = "provenance_churn"
    SCHEMA_DRIFT = "schema_drift"
    FROZEN_PAST_SHIFT = "frozen_past_shift"
    TALLY_SHIFT = "tally_shift"
    #: Records left under one identity and arrived under another, with no net
    #: change in population. Nothing was removed; the keys moved.
    IDENTITY_CHURN = "identity_churn"
    #: The source's key does not identify a stable entity, so no per-record
    #: revision claim about it can be honest. Reported once, in place of them.
    WITHDRAWN_UNSTABLE_KEY = "withdrawn_unstable_key"
    #: A case advancing through its own workflow, or a blank being filled in.
    #: Real movement in the record, but not the rewriting of a stated fact.
    LIFECYCLE_PROGRESSION = "lifecycle_progression"


# How consequential is a change to this field? Altering what an event *was*, or
# when it happened, is a different order of thing from nudging a coordinate.
_FIELD_WEIGHT: list[tuple[str, float]] = [
    (r"(primary_)?type|classification|category|charge|offen[cs]e|iucr|crime", 1.0),
    (r"status|disposition|outcome|result|resolution|finding|verdict", 0.95),
    (r"date|time|occurr?ed|reported", 0.9),
    (r"amount|fine|penalty|cost|salary|value|total|count|fee", 0.85),
    (r"descript|narrative|detail|summary|comment", 0.7),
    (r"arrest|domestic|injur|fatal|death|weapon|force", 1.0),
    (r"agency|department|district|ward|beat|precinct|division", 0.5),
    (r"address|block|street|location|neighborhood|community", 0.45),
    (r"lat|lon|lng|x_coord|y_coord|point|geom|coordinate", 0.2),
    (r"id$|_id$|key$|number$", 0.3),
]

import re as _re  # noqa: E402

_FIELD_WEIGHT_C = [(_re.compile(p, _re.I), w) for p, w in _FIELD_WEIGHT]


def field_weight(name: str) -> float:
    for pat, w in _FIELD_WEIGHT_C:
        if pat.search(name):
            return w
    return 0.4


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_STATUS_FIELD = _re.compile(
    r"(status|stage|state|phase|disposition|current_?task|workflow)", _re.I
)


def _is_lifecycle(deltas: dict[str, list[Any]]) -> bool:
    """Is this a case moving through its workflow, or a stated fact being rewritten?

    An open case legitimately changes: a permit goes Active then Final, and the
    completion date it did not have gets filled in. Nothing that was previously
    asserted has been contradicted.

    Overwriting a value that was already stated is a different act. When a
    permit's recorded floor area goes from 463 to 2,551, the record's earlier
    account of the world has been replaced — and unless someone kept a copy,
    silently.

    So the test is not which field moved but *what kind of move it was*: filling
    a blank and advancing a status are progression; replacing one non-empty
    value with a different non-empty value is revision.
    """
    if not deltas:
        return False
    for name, (before, after) in deltas.items():
        blank_before = before in (None, "", [])
        if blank_before:
            continue  # information added where there was none
        if _STATUS_FIELD.search(name):
            continue  # a case advancing through its own workflow
        return False  # a stated value was replaced by a different one
    return True


def _field_deltas(before: dict[str, Any], after: dict[str, Any]) -> dict[str, list[Any]]:
    """Field-level difference between two record payloads."""
    out: dict[str, list[Any]] = {}
    for k in set(before) | set(after):
        b, a = before.get(k), after.get(k)
        if b != a:
            out[k] = [b, a]
    return out


def _revision_significance(deltas: dict[str, list[Any]]) -> float:
    """Score a single record revision in [0, 1].

    Driven by the heaviest field touched rather than the count of fields: one
    altered classification matters more than six shifted coordinates.
    """
    if not deltas:
        return 0.0
    weights = [field_weight(k) for k in deltas]
    top = max(weights)
    # Additional touched fields add a little, but cannot dominate the headline field.
    breadth = min(0.15, 0.03 * (len(deltas) - 1))
    return round(min(1.0, top + breadth), 3)


def diff_snapshots(
    archive, source_key: str, snap_a: dict[str, Any], snap_b: dict[str, Any]
) -> list[dict[str, Any]]:
    """Compare two observations of one source and return classified changes."""
    import json

    a_id, b_id = snap_a["snapshot_id"], snap_b["snapshot_id"]
    obs_a = archive.observations(a_id)
    obs_b = archive.observations(b_id)
    detected = _now()

    def base(kind: str, **kw) -> dict[str, Any]:
        return {
            "source_key": source_key, "from_snapshot": a_id, "to_snapshot": b_id,
            "detected_at": detected, "kind": kind, **kw,
        }

    changes: list[dict[str, Any]] = []

    # Columns the publisher recomputes on every run, learned from this archive's
    # own observations rather than guessed from their names. See volatility.py:
    # `sr_age_days` counts up daily for every record and is a clock, not a fact.
    from . import volatility

    ignored_fields = volatility.recomputed_fields(archive, source_key)

    # -- schema ------------------------------------------------------------
    cols_a = json.loads(snap_a["columns"] or "[]")
    cols_b = json.loads(snap_b["columns"] or "[]")
    added = sorted(set(cols_b) - set(cols_a))
    removed = sorted(set(cols_a) - set(cols_b))

    # A column appearing makes every record differ on it. That is one schema
    # change, reported once below -- not a revision of every record. Excluding
    # these from per-record deltas is what stops a dataset migration from
    # presenting as thousands of simultaneous edits.
    ignored_fields = ignored_fields | set(added) | set(removed)

    if cols_a != cols_b:
        if added or removed:
            changes.append(base(
                ChangeKind.SCHEMA_DRIFT,
                field_deltas={"added": [None, added], "removed": [removed, None]},
                # A column disappearing removes facts from the public record.
                significance=0.9 if removed else 0.5,
                detail=f"columns added={added} removed={removed}",
            ))

    # -- aggregates --------------------------------------------------------
    agg_a = json.loads(snap_a["aggregates"] or "{}")
    agg_b = json.loads(snap_b["aggregates"] or "{}")

    fa, fb = agg_a.get("frozen_past_count"), agg_b.get("frozen_past_count")
    if fa is not None and fb is not None and fa != fb:
        delta = fb - fa
        # Records appearing in a closed window are late filings or backfill;
        # records leaving it are removals from the published record.
        changes.append(base(
            ChangeKind.FROZEN_PAST_SHIFT,
            field_deltas={"frozen_past_count": [fa, fb]},
            significance=0.95 if delta < 0 else 0.55,
            detail=(
                f"{'records removed from' if delta < 0 else 'records added to'} "
                f"the frozen past: {fa:,} -> {fb:,} ({delta:+,})"
            ),
        ))

    ta = (agg_a.get("tallies") or {})
    tb = (agg_b.get("tallies") or {})
    for dim in set(ta) | set(tb):
        da, db = ta.get(dim, {}), tb.get(dim, {})
        moved = {
            k: [da.get(k, 0), db.get(k, 0)]
            for k in set(da) | set(db)
            if da.get(k, 0) != db.get(k, 0)
        }
        if moved:
            magnitude = sum(abs(v[1] - v[0]) for v in moved.values())
            changes.append(base(
                ChangeKind.TALLY_SHIFT,
                field_deltas=moved,
                significance=round(min(1.0, 0.4 + field_weight(dim) * 0.4), 3),
                detail=f"category distribution moved on '{dim}': {magnitude:,} records reclassified",
            ))

    # -- record level ------------------------------------------------------
    keys_a, keys_b = set(obs_a), set(obs_b)

    # Churn is detected in aggregate, from the constant-space digest over every
    # record's volatile fields. If that digest moved while the Merkle root held
    # still, the publisher rewrote rows without altering a single published fact.
    # This is the exact false positive the project exists to refuse, so it is
    # recorded once, at zero significance, rather than as thousands of findings.
    vd_a, vd_b = agg_a.get("volatile_digest"), agg_b.get("volatile_digest")
    if vd_a and vd_b and vd_a != vd_b and snap_a["merkle_root"] == snap_b["merkle_root"]:
        changes.append(base(
            ChangeKind.PROVENANCE_CHURN,
            significance=0.0,
            detail=(
                f"publisher rewrote all {snap_b['row_count']:,} observed records; "
                f"no published value changed (content root unchanged at "
                f"{snap_b['merkle_root'][:12]})"
            ),
        ))

    for uid in keys_a & keys_b:
        ra, rb = obs_a[uid], obs_b[uid]
        if ra["content_hash"] == rb["content_hash"]:
            continue

        before = archive.blob(ra["content_hash"]) or {}
        after = archive.blob(rb["content_hash"]) or {}
        deltas = _field_deltas(before, after)

        # Set aside movement the publisher makes in every record regardless of
        # content. What remains is the part that says something about this record.
        surviving = {k: v for k, v in deltas.items() if k not in ignored_fields}
        if not surviving:
            # The record's hash moved, but only through columns the publisher
            # rewrites wholesale. Nothing was revised.
            changes.append(base(
                ChangeKind.PROVENANCE_CHURN, row_uid=uid,
                before_hash=ra["content_hash"], after_hash=rb["content_hash"],
                significance=0.0,
                detail=(
                    "only recomputed columns moved ("
                    + ", ".join(sorted(deltas)[:6])
                    + "); no published value about this record changed"
                ),
            ))
            continue

        if _is_lifecycle(surviving):
            changes.append(base(
                ChangeKind.LIFECYCLE_PROGRESSION, row_uid=uid,
                before_hash=ra["content_hash"], after_hash=rb["content_hash"],
                field_deltas=surviving,
                significance=0.2,
                detail=(
                    "case advanced through its workflow or a blank was filled ("
                    + ", ".join(sorted(surviving))[:200]
                    + "); no previously stated value was replaced"
                ),
            ))
            continue

        sig = _revision_significance(surviving)
        note = ""
        # A status transition explains the changes that travel with it: when a
        # permit becomes Final, its expiry is truncated to the completion date.
        # These are still replacements of stated values and are not hidden — but
        # a revision with no such explanation is the more interesting one, and
        # should not be buried beneath dozens of routine completions.
        if any(_STATUS_FIELD.search(f) for f in surviving) and len(surviving) > 1:
            sig = round(sig * 0.4, 3)
            note = (
                " | accompanies a status transition, so the remaining changes may "
                "be consequences of it"
            )

        changes.append(base(
            ChangeKind.SEMANTIC_REVISION, row_uid=uid,
            before_hash=ra["content_hash"], after_hash=rb["content_hash"],
            field_deltas=surviving,
            significance=sig,
            detail=(
                f"{len(surviving)} previously stated value(s) replaced: "
                f"{', '.join(sorted(surviving))[:300]}{note}"
            ),
        ))

    gone = sorted(keys_a - keys_b)
    arrived = sorted(keys_b - keys_a)

    for uid in gone:
        changes.append(base(
            ChangeKind.DELETION, row_uid=uid, before_hash=obs_a[uid]["content_hash"],
            significance=1.0,
            detail="record present in the previous observation is absent from this one",
        ))

    for uid in arrived:
        changes.append(base(
            ChangeKind.RETROACTIVE_APPEND, row_uid=uid,
            after_hash=obs_b[uid]["content_hash"],
            significance=0.6,
            detail="record inserted into a window that had already closed",
        ))

    identity_mode = (agg_b.get("identity_mode") or agg_a.get("identity_mode")
                     or "business_key")
    changes = _resolve_identity_churn(changes, len(gone), len(arrived), identity_mode)
    changes = _withdraw_if_key_unstable(archive, source_key, changes, base)
    return _mark_coordinated(changes)


def _withdraw_if_key_unstable(
    archive, source_key: str, changes: list[dict[str, Any]], base
) -> list[dict[str, Any]]:
    """Drop per-record claims for sources whose key re-binds between publications.

    Seattle's permit review data changes `permitnum` in 99.5% of apparent
    revisions. A permit number that changes is not identifying a permit, so
    "this record was revised" is a claim about nothing. The honest report is that
    the source cannot support per-record claims at all — stated once, rather than
    thousands of confident falsehoods.

    Aggregate findings survive: counts over a closed window do not depend on
    knowing which record is which.
    """
    from . import volatility

    if not volatility.is_key_unstable(archive, source_key):
        return changes

    per_record = {
        ChangeKind.SEMANTIC_REVISION,
        ChangeKind.COORDINATED_REVISION,
        ChangeKind.DELETION,
        ChangeKind.RETROACTIVE_APPEND,
        ChangeKind.IDENTITY_CHURN,
        ChangeKind.PROVENANCE_CHURN,
    }
    dropped = sum(1 for c in changes if c["kind"] in per_record)
    kept = [c for c in changes if c["kind"] not in per_record]

    if dropped:
        detail = volatility.unstable_sources(archive).get(source_key, "")
        kept.append(base(
            ChangeKind.WITHDRAWN_UNSTABLE_KEY,
            significance=0.0,
            detail=(
                f"{dropped:,} apparent per-record changes withdrawn. {detail}"
            ),
        ))
    return kept


def _resolve_identity_churn(
    changes: list[dict[str, Any]], gone: int, arrived: int, identity_mode: str
) -> list[dict[str, Any]]:
    """Refuse to call a re-identification a deletion.

    Two situations produce departures and arrivals that are not removals:

    Under **content identity** — used where a dataset's natural key turned out to
    collide — a record's identity *is* its content. Any edit therefore presents as
    the old record vanishing and a new one appearing. Deletion, insertion and
    revision are formally indistinguishable, and reporting the alarming reading of
    an ambiguous signal would be exactly backwards.

    Under a **stable key**, departures that are matched one-for-one by arrivals with
    no net change in population mean the keys were rewritten, not that records were
    removed. Only the unmatched excess is a candidate deletion.
    """
    if not gone and not arrived:
        return changes

    dels = [c for c in changes if c["kind"] == ChangeKind.DELETION]
    apps = [c for c in changes if c["kind"] == ChangeKind.RETROACTIVE_APPEND]
    paired = min(gone, arrived)

    if identity_mode == "content":
        for c in dels + apps:
            c["kind"] = ChangeKind.IDENTITY_CHURN
            c["significance"] = 0.1
            c["detail"] = (
                f"record identity moved ({gone} left, {arrived} arrived). This source "
                f"keys records by content because its natural key collides, so an edit, "
                f"an insertion and a removal are indistinguishable here — no claim of "
                f"deletion is made"
            )
        return changes

    if paired == 0:
        return changes

    # Balanced departures and arrivals: the population held, the keys did not.
    balance = paired / max(gone, arrived)
    if balance >= 0.9:
        for c in dels + apps:
            c["kind"] = ChangeKind.IDENTITY_CHURN
            c["significance"] = 0.15
            c["detail"] = (
                f"{gone} records left and {arrived} arrived with no material change in "
                f"population — consistent with the publisher rewriting record keys "
                f"rather than removing anything"
            )
        return changes

    # Partial overlap: discount the matched portion, keep the excess as a finding.
    for c in dels:
        c["significance"] = round(c["significance"] * (1 - balance), 3)
        c["detail"] = (c.get("detail") or "") + (
            f" | {paired} of {gone} departures are matched by arrivals in the same "
            f"sweep and may be re-identification rather than removal"
        )
    for c in apps:
        c["significance"] = round(c["significance"] * (1 - balance), 3)
    return changes


def _mark_coordinated(changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Separate mass migrations from individually altered records.

    Revisions sharing an identical field-level transformation are almost always
    one systematic operation — a code table remap, a case normalisation. Reported
    as thousands of separate findings they would drown everything else, and each
    would carry an insinuation that is not warranted.
    """
    revisions = [c for c in changes if c["kind"] == ChangeKind.SEMANTIC_REVISION]
    if len(revisions) < 2:
        return changes

    signature: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
    for c in revisions:
        d = c.get("field_deltas") or {}
        sig = tuple(sorted((k, repr(v[0]), repr(v[1])) for k, v in d.items()))
        signature[sig].append(c)

    # Also detect the weaker pattern: same fields touched, differing values.
    shape: dict[tuple, int] = Counter()
    for c in revisions:
        shape[tuple(sorted((c.get("field_deltas") or {}).keys()))] += 1

    COORDINATION_THRESHOLD = 25

    for sig, group in signature.items():
        if len(group) >= COORDINATION_THRESHOLD:
            for c in group:
                c["kind"] = ChangeKind.COORDINATED_REVISION
                # Systematic and disclosed-by-its-own-scale; individually mild,
                # collectively still worth surfacing once.
                c["significance"] = round(c["significance"] * 0.25, 3)
                c["detail"] = (
                    f"part of a coordinated change affecting {len(group):,} records "
                    f"identically — consistent with a systematic migration rather "
                    f"than an individual edit"
                )

    for c in revisions:
        if c["kind"] != ChangeKind.SEMANTIC_REVISION:
            continue
        touched = tuple(sorted((c.get("field_deltas") or {}).keys()))
        # An isolated edit inside an otherwise-quiet sweep is the strongest signal
        # available: nothing systematic explains it.
        if shape[touched] <= 3:
            c["significance"] = round(min(1.0, c["significance"] + 0.2), 3)
            c["detail"] = (c.get("detail") or "") + (
                " | isolated edit — no comparable change in this sweep"
            )

    return changes


def run_source(archive, source_key: str, force: bool = False) -> int:
    """Diff every consecutive pair of observations for one source."""
    snaps = archive.snapshots_for(source_key)
    if len(snaps) < 2:
        return 0
    total = 0
    for a, b in zip(snaps, snaps[1:]):
        if not force and archive.diff_already_run(a["snapshot_id"], b["snapshot_id"]):
            continue
        try:
            changes = diff_snapshots(archive, source_key, a, b)
        except Exception as e:
            log.warning("diff failed for %s (%s->%s): %s",
                        source_key, a["snapshot_id"], b["snapshot_id"], e)
            continue
        n = archive.record_changes(changes)
        archive.mark_diff_run(a["snapshot_id"], b["snapshot_id"], _now(), n)
        total += n
    return total


def run_all(archive, force: bool = False) -> dict[str, int]:
    out: dict[str, int] = {}
    for src in archive.sources():
        n = run_source(archive, src["source_key"], force=force)
        if n:
            out[src["source_key"]] = n
    return out


def summarise(archive, min_significance: float = 0.5, limit: int = 50) -> list[dict[str, Any]]:
    rows = archive.conn.execute(
        "SELECT c.*, s.title, s.city FROM changes c "
        "JOIN sources s ON s.source_key = c.source_key "
        "WHERE c.significance >= ? ORDER BY c.significance DESC, c.change_id DESC LIMIT ?",
        (min_significance, limit),
    ).fetchall()
    return [dict(r) for r in rows]


if __name__ == "__main__":
    import argparse

    from .store import Archive

    ap = argparse.ArgumentParser(description="Diff Palimpsest snapshots.")
    ap.add_argument("--db", default="archive/palimpsest.db")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--min-significance", type=float, default=0.5)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
    arc = Archive(args.db)
    res = run_all(arc, force=args.force)
    log.info("recorded %d changes across %d sources", sum(res.values()), len(res))
    for row in summarise(arc, args.min_significance):
        log.info(
            "[%.2f] %-22s %-34s %s",
            row["significance"], row["kind"], (row["title"] or "")[:34],
            (row["detail"] or "")[:110],
        )
