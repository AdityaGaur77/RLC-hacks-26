"""Export the archive as a static, self-contained evidence bundle.

Deliberately a static export rather than a live API. A finding that only exists
while a server is up is a claim; a finding published with the hashes needed to
check it is evidence. Anyone can take this file, re-derive every root from the
raw records, and confirm or refute what it says.
"""

from __future__ import annotations

import argparse
import json
import logging
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .core import leaf_hash, merkle_proof, merkle_root
from .diff import ChangeKind
from .pipeline import DESCRIPTIONS as PIPELINE_DESCRIPTIONS
from .store import Archive

log = logging.getLogger("palimpsest.report")

# Kinds that represent an actual movement in the public record, as opposed to
# the publisher's plumbing running.
MATERIAL_KINDS = {
    ChangeKind.SEMANTIC_REVISION,
    ChangeKind.COORDINATED_REVISION,
    ChangeKind.LIFECYCLE_PROGRESSION,
    ChangeKind.DELETION,
    ChangeKind.RETROACTIVE_APPEND,
    ChangeKind.SCHEMA_DRIFT,
    ChangeKind.FROZEN_PAST_SHIFT,
    ChangeKind.TALLY_SHIFT,
}

# Kinds that describe the publisher's machinery rather than the record. Shown,
# because how a portal behaves is itself worth knowing, but never counted as a
# change to the public record — that conflation is the error this project exists
# to refuse.
ARTIFACT_KINDS = {
    ChangeKind.PROVENANCE_CHURN,
    ChangeKind.IDENTITY_CHURN,
    ChangeKind.WITHDRAWN_UNSTABLE_KEY,
    ChangeKind.ORDERING_CHANGE,
    ChangeKind.TRANSIENT_ABSENCE,
    ChangeKind.LEFT_OBSERVATION_WINDOW,
}

DISPLAY_KINDS = MATERIAL_KINDS | ARTIFACT_KINDS


def _iso(s: str | None) -> str | None:
    return s


def overview(arc: Archive) -> dict[str, Any]:
    st = arc.stats()
    row = arc.conn.execute(
        "SELECT MIN(captured_at) a, MAX(captured_at) b FROM snapshots WHERE status='ok'"
    ).fetchone()
    cities = [
        r["city"]
        for r in arc.conn.execute(
            "SELECT DISTINCT city FROM sources WHERE city IS NOT NULL ORDER BY city"
        )
    ]
    # A sweep is one pass over the whole watchlist. Sources are stamped
    # individually as they are visited, so counting distinct timestamps would
    # count sources, not rounds; the number of rounds is how many times the
    # most-observed source has been revisited.
    sweeps = arc.conn.execute(
        "SELECT COALESCE(MAX(n), 0) m FROM ("
        "  SELECT COUNT(*) n FROM snapshots WHERE status='ok' GROUP BY source_key"
        ")"
    ).fetchone()["m"]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sources_watched": st["sources"],
        "cities": cities,
        "city_count": len(cities),
        "snapshots_ok": st["snapshots_ok"],
        "snapshots_failed": st["snapshots_error"],
        "sweeps": sweeps,
        "observations": st["observations"],
        "distinct_record_states": st["blobs"],
        "changes_recorded": st["changes"],
        "first_observation": _iso(row["a"] if row else None),
        "last_observation": _iso(row["b"] if row else None),
        "archive_bytes": st["db_bytes"],
    }


def integrity(arc: Archive, sample_proofs: int = 3) -> dict[str, Any]:
    """Verify every hash chain, and produce a few worked inclusion proofs.

    The proofs are the point. A published root that nobody can check against a
    specific record is decoration.
    """
    results = []
    broken = []
    for src in arc.sources():
        v = arc.verify_chain(src["source_key"])
        results.append({"source_key": src["source_key"], **v})
        if not v["ok"]:
            broken.append(src["source_key"])

    proofs = []
    rows = arc.conn.execute(
        "SELECT snapshot_id, source_key, captured_at, merkle_root, row_count "
        "FROM snapshots WHERE status='ok' AND row_count > 4 "
        "ORDER BY row_count DESC LIMIT ?",
        (sample_proofs,),
    ).fetchall()
    for r in rows:
        leaves_src = arc.leaves_for(r["snapshot_id"])
        if not leaves_src:
            continue
        leaves = [leaf_hash(u, h) for u, h in leaves_src]
        idx = len(leaves) // 2
        uid, chash = leaves_src[idx]
        payload = arc.blob(chash)
        proofs.append({
            "source_key": r["source_key"],
            "snapshot_id": r["snapshot_id"],
            "captured_at": r["captured_at"],
            "record_uid": uid,
            "content_hash": chash,
            "leaf_index": idx,
            "leaf_count": len(leaves),
            "merkle_root": r["merkle_root"],
            "recomputed_root": merkle_root(leaves),
            "proof": merkle_proof(leaves, idx),
            "record": payload,
        })

    return {
        "chains_verified": len(results),
        "chains_ok": sum(1 for r in results if r["ok"]),
        "chains_broken": broken,
        "all_ok": not broken,
        "worked_proofs": proofs,
    }


def publishing_census(arc: Archive) -> dict[str, Any]:
    """How the watched portals actually publish — a finding in its own right."""
    rows = arc.conn.execute(
        "SELECT pipeline_class, COUNT(*) n FROM sources GROUP BY pipeline_class"
    ).fetchall()
    by_class = {r["pipeline_class"] or "unknown": r["n"] for r in rows}
    total = sum(by_class.values())

    # Only sources that have actually been probed can support a claim about how
    # they publish. Counting un-probed sources as "uninterpretable" would report
    # our own incomplete work as a finding about someone else's data — the same
    # error, in miniature, that this project exists to refuse.
    characterised = total - by_class.get("unknown", 0)

    # Saturation is what destroys the signal, not activity. `static` publishers
    # have the cleanest timestamps of all — no noise floor to hide an edit in.
    informative = by_class.get("incremental", 0) + by_class.get("static", 0)

    per_city: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in arc.conn.execute(
        "SELECT city, pipeline_class, COUNT(*) n FROM sources GROUP BY city, pipeline_class"
    ):
        per_city[r["city"]][r["pipeline_class"] or "unknown"] = r["n"]

    return {
        "total": total,
        "characterised": characterised,
        "by_class": dict(sorted(by_class.items(), key=lambda t: -t[1])),
        "descriptions": PIPELINE_DESCRIPTIONS,
        "timestamps_informative": informative,
        "timestamps_uninterpretable": characterised - informative,
        "share_uninterpretable": (
            round(1 - informative / characterised, 3) if characterised else None
        ),
        "by_city": {k: dict(v) for k, v in sorted(per_city.items())},
    }


def findings(arc: Archive, limit: int = 420, per_kind: int = 30) -> list[dict[str, Any]]:
    """Changes, most consequential first, each carrying its evidence.

    Sampled per kind rather than taken straight off the top. Deletions all score
    1.0, so a flat "highest significance first" cut filled 398 of 400 slots with
    deletions and left two revisions — making the most interesting category, an
    edit with nothing to explain it, effectively unbrowsable. Each kind gets its
    own allowance and they are merged afterwards.
    """
    placeholders = ",".join("?" for _ in DISPLAY_KINDS)
    rows = arc.conn.execute(
        f"SELECT * FROM ("
        f"  SELECT c.*, s.title, s.city, s.domain, s.fourfour, s.pipeline_class, "
        f"         s.date_field, s.stratum_start, s.stratum_end, "
        f"         ROW_NUMBER() OVER ("
        f"           PARTITION BY c.kind ORDER BY c.significance DESC, c.change_id DESC"
        f"         ) AS rank_in_kind "
        f"  FROM changes c JOIN sources s ON s.source_key = c.source_key "
        f"  WHERE c.kind IN ({placeholders})"
        f") WHERE rank_in_kind <= ? "
        f"ORDER BY significance DESC, change_id DESC LIMIT ?",
        (*DISPLAY_KINDS, per_kind, limit),
    ).fetchall()

    out = []
    for r in rows:
        d = dict(r)
        d.pop("rank_in_kind", None)
        d["field_deltas"] = json.loads(d["field_deltas"]) if d["field_deltas"] else None

        snap = arc.conn.execute(
            "SELECT captured_at, merkle_root FROM snapshots WHERE snapshot_id=?",
            (r["to_snapshot"],),
        ).fetchone()
        prev = arc.conn.execute(
            "SELECT captured_at, merkle_root FROM snapshots WHERE snapshot_id=?",
            (r["from_snapshot"],),
        ).fetchone()
        d["observed_before"] = prev["captured_at"] if prev else None
        d["observed_after"] = snap["captured_at"] if snap else None
        d["root_before"] = prev["merkle_root"] if prev else None
        d["root_after"] = snap["merkle_root"] if snap else None

        # Attach the full record on both sides so a reader can see the whole
        # context of an edit, not just the fields that moved.
        if d.get("before_hash"):
            d["record_before"] = arc.blob(d["before_hash"])
        if d.get("after_hash"):
            d["record_after"] = arc.blob(d["after_hash"])

        d["source_url"] = f"https://{r['domain']}/resource/{r['fourfour']}.json"
        d["portal_url"] = f"https://{r['domain']}/d/{r['fourfour']}"
        out.append(d)
    return out


def churn_summary(arc: Archive) -> dict[str, Any]:
    """How much observed 'change' was nothing at all.

    The headline number this project exists to prevent someone from publishing.
    """
    n_churn = arc.conn.execute(
        "SELECT COUNT(*) n FROM changes WHERE kind=?", (ChangeKind.PROVENANCE_CHURN,)
    ).fetchone()["n"]
    n_material = arc.conn.execute(
        f"SELECT COUNT(*) n FROM changes WHERE kind IN "
        f"({','.join('?' for _ in MATERIAL_KINDS)})",
        tuple(MATERIAL_KINDS),
    ).fetchone()["n"]
    n_coord = arc.conn.execute(
        "SELECT COUNT(*) n FROM changes WHERE kind=?", (ChangeKind.COORDINATED_REVISION,)
    ).fetchone()["n"]
    n_isolated = arc.conn.execute(
        "SELECT COUNT(*) n FROM changes WHERE kind=?", (ChangeKind.SEMANTIC_REVISION,)
    ).fetchone()["n"]
    n_identity = arc.conn.execute(
        "SELECT COUNT(*) n FROM changes WHERE kind=?", (ChangeKind.IDENTITY_CHURN,)
    ).fetchone()["n"]
    n_deletion = arc.conn.execute(
        "SELECT COUNT(*) n FROM changes WHERE kind=?", (ChangeKind.DELETION,)
    ).fetchone()["n"]
    n_lifecycle = arc.conn.execute(
        "SELECT COUNT(*) n FROM changes WHERE kind=?",
        (ChangeKind.LIFECYCLE_PROGRESSION,),
    ).fetchone()["n"]
    n_withdrawn = arc.conn.execute(
        "SELECT COUNT(*) n FROM changes WHERE kind=?",
        (ChangeKind.WITHDRAWN_UNSTABLE_KEY,),
    ).fetchone()["n"]
    n_ordering = arc.conn.execute(
        "SELECT COUNT(*) n FROM changes WHERE kind=?",
        (ChangeKind.ORDERING_CHANGE,),
    ).fetchone()["n"]
    n_transient = arc.conn.execute(
        "SELECT COUNT(*) n FROM changes WHERE kind=?",
        (ChangeKind.TRANSIENT_ABSENCE,),
    ).fetchone()["n"]
    total = arc.conn.execute("SELECT COUNT(*) n FROM changes").fetchone()["n"]

    # What a blind pass saw, before any control for publishing mechanism was
    # applied. Recorded by the analysis itself so the headline ratio can be
    # checked against the archive rather than taken on trust.
    try:
        stages = dict(
            arc.conn.execute("SELECT stage, change_count FROM analysis_stages")
        )
    except Exception:
        stages = {}
    probe = stages.get("probe")

    mechanism = n_churn + n_identity + n_withdrawn + n_ordering + n_transient

    return {
        "apparent_changes_before_control": probe,
        "dissolved_by_control": (probe - total) if probe else None,
        "share_that_was_mechanism": (
            round(1 - (n_isolated + n_coord + n_deletion) / probe, 4) if probe else None
        ),
        "total_apparent_changes": total,
        "provenance_churn_events": n_churn,
        "identity_churn_events": n_identity,
        "withdrawn_unstable_key": n_withdrawn,
        # The same values rearranged. Counted as mechanism because the record
        # holds exactly what it held before — this is the one that caught us.
        "ordering_changes": n_ordering,
        # Records that were missing once and came back. Deletion is the
        # strongest claim made here, so it has to survive the whole archive.
        "transient_absence": n_transient,
        "material_changes": n_material,
        "coordinated_revisions": n_coord,
        "isolated_revisions": n_isolated,
        "deletions": n_deletion,
        # Real movement, but a case advancing rather than the past being
        # rewritten. Counted apart from both buckets: folding it into "rewriting"
        # would inflate the finding with routine administrative progression,
        # and folding it into "mechanism" would deny that anything happened.
        "lifecycle_progression": n_lifecycle,
        # How much apparent change dissolved once publishing mechanics were
        # accounted for. This ratio is the project's central claim in one number.
        "discarded_as_mechanism": mechanism,
        "rewriting_of_stated_facts": n_isolated + n_coord + n_deletion,
    }


def sources_table(arc: Archive) -> list[dict[str, Any]]:
    rows = arc.conn.execute(
        "SELECT s.*, "
        "  (SELECT COUNT(*) FROM snapshots n WHERE n.source_key=s.source_key "
        "     AND n.status='ok') AS snapshots, "
        "  (SELECT COUNT(*) FROM snapshots n WHERE n.source_key=s.source_key "
        "     AND n.status='error') AS failures, "
        "  (SELECT MAX(captured_at) FROM snapshots n WHERE n.source_key=s.source_key "
        "     AND n.status='ok') AS last_seen, "
        "  (SELECT COUNT(DISTINCT merkle_root) FROM snapshots n "
        "     WHERE n.source_key=s.source_key AND n.status='ok') AS distinct_states, "
        "  (SELECT aggregates FROM snapshots n WHERE n.source_key=s.source_key "
        "     AND n.status='ok' ORDER BY n.snapshot_id DESC LIMIT 1) AS last_aggregates "
        "FROM sources s ORDER BY s.city, s.title"
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        for j in ("business_key", "extra_volatile", "agg_dimensions"):
            d[j] = json.loads(d[j]) if d[j] else []
        agg = json.loads(d.pop("last_aggregates") or "{}")
        # Sources that fell back to content identity cannot evidence an in-place
        # edit — a revision there is indistinguishable from a delete plus an
        # insert. Carried through to the interface so the watchlist does not
        # imply a capability the source does not have.
        d["identity_mode"] = agg.get("identity_mode", "business_key")
        d["key_collisions"] = agg.get("key_collisions", 0)
        d["can_attribute_revision"] = d["identity_mode"] == "business_key"
        d["frozen_past_count"] = agg.get("frozen_past_count")
        d["portal_url"] = f"https://{d['domain']}/d/{d['fourfour']}"
        out.append(d)
    return out


def publisher_assertions(db_dir: Path) -> dict[str, Any] | None:
    """Fold in what publishers report about their own retroactive writes.

    Kept in its own section, never merged into `findings`. These are the
    publisher's assertions that a record was written recently; the ledger holds
    changes this archive independently watched happen. Merging them would lend
    the archive's evidentiary weight to a claim the archive did not make.
    """
    p = db_dir / "retroactive.json"
    if not p.exists():
        return None
    try:
        with open(p, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None

    srcs = data.get("sources", [])
    deepest = sorted(
        (s for s in srcs if s.get("oldest_event_touched")),
        key=lambda s: str(s["oldest_event_touched"]),
    )[:10]
    return {
        **{k: v for k, v in data.items() if k != "sources"},
        "sources": srcs,
        "deepest_reach": [
            {
                "oldest_event_touched": s["oldest_event_touched"],
                "title": s["title"],
                "city": s["city"],
                "asserted_modified": s["asserted_modified"],
                "portal_url": s["portal_url"],
            }
            for s in deepest
        ],
    }


def build(db: str, out_path: str, findings_limit: int = 420) -> dict[str, Any]:
    arc = Archive(db)
    bundle = {
        "overview": overview(arc),
        "integrity": integrity(arc),
        "census": publishing_census(arc),
        "churn": churn_summary(arc),
        "findings": findings(arc, findings_limit),
        "publisher_assertions": publisher_assertions(Path(db).parent),
        "sources": sources_table(arc),
        "method_note": (
            "A change is only counted when the hash of a record's published fields "
            "moves. Platform-generated identifiers, derived spatial joins, and "
            "columns that merely record when a row was last written are excluded "
            "from that hash and tracked separately as provenance. This distinction "
            "is not cosmetic: on a portal that reloads its tables, every row shows "
            "a fresh modification timestamp on every run while nothing published "
            "has changed at all."
        ),
    }
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(bundle, fh, indent=1, ensure_ascii=False)
    arc.close()
    return bundle


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Export the Palimpsest evidence bundle.")
    ap.add_argument("--db", default="archive/palimpsest.db")
    ap.add_argument("--out", default="web/data.json")
    ap.add_argument("--limit", type=int, default=400)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
    b = build(args.db, args.out, args.limit)
    o, c = b["overview"], b["census"]
    log.info("watching %d datasets across %d cities", o["sources_watched"], o["city_count"])
    log.info("%d observations in %d sweeps", o["observations"], o["sweeps"])
    log.info(
        "integrity: %d/%d chains verified",
        b["integrity"]["chains_ok"], b["integrity"]["chains_verified"],
    )
    if c["total"]:
        log.info(
            "publishing census: %s",
            ", ".join(f"{k}={v}" for k, v in c["by_class"].items()),
        )
    log.info("material findings: %d", len(b["findings"]))
    log.info("wrote %s (%.1f KB)", args.out, Path(args.out).stat().st_size / 1024)
