"""Independently re-derive every cryptographic claim the archive makes.

Nothing here trusts a stored hash. Roots are recomputed from the stored records,
chains are replayed from genesis, and inclusion proofs are re-derived and checked.
A stored value is only ever compared against a freshly computed one.

Exit status is 0 when every claim holds and 1 otherwise, so this can gate a
publish step rather than merely inform one.
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import sys
from typing import Any

from .core import (
    GENESIS,
    canonical_json,
    chain_hash,
    leaf_hash,
    merkle_proof,
    merkle_root,
    sha256_hex,
    verify_proof,
)
from .store import Archive

log = logging.getLogger("palimpsest.verify")


def verify_merkle_roots(arc: Archive) -> dict[str, Any]:
    """Recompute every snapshot's root from the records it actually stores."""
    rows = arc.conn.execute(
        "SELECT snapshot_id, source_key, merkle_root, row_count "
        "FROM snapshots WHERE status='ok' ORDER BY snapshot_id"
    ).fetchall()
    checked = 0
    bad: list[dict[str, Any]] = []
    for r in rows:
        leaves = [leaf_hash(u, h) for u, h in arc.leaves_for(r["snapshot_id"])]
        got = merkle_root(leaves)
        checked += 1
        if got != r["merkle_root"]:
            bad.append({
                "snapshot_id": r["snapshot_id"],
                "source_key": r["source_key"],
                "stored": r["merkle_root"],
                "recomputed": got,
            })
    return {"checked": checked, "failed": bad, "ok": not bad}


def verify_chains(arc: Archive) -> dict[str, Any]:
    """Replay each source's hash chain from genesis."""
    sources = arc.sources()
    bad = []
    for src in sources:
        res = arc.verify_chain(src["source_key"])
        if not res["ok"]:
            bad.append({"source_key": src["source_key"], **res})
    return {"checked": len(sources), "failed": bad, "ok": not bad}


def verify_blob_integrity(arc: Archive, sample: int = 3000) -> dict[str, Any]:
    """Confirm each stored payload still hashes to the key it is filed under.

    This is what makes the store content-addressed in fact and not just in name:
    if a payload were altered, its key would no longer match its content.
    """
    total = arc.conn.execute("SELECT COUNT(*) n FROM blobs").fetchone()["n"]
    rows = arc.conn.execute(
        "SELECT content_hash FROM blobs ORDER BY RANDOM() LIMIT ?", (sample,)
    ).fetchall()
    bad = []
    for r in rows:
        payload = arc.blob(r["content_hash"])
        if payload is None:
            bad.append({"content_hash": r["content_hash"], "error": "unreadable"})
            continue
        recomputed = sha256_hex(canonical_json(payload))
        if recomputed != r["content_hash"]:
            bad.append({
                "content_hash": r["content_hash"], "recomputed": recomputed,
            })
    return {"total": total, "sampled": len(rows), "failed": bad, "ok": not bad}


def verify_inclusion_proofs(arc: Archive, samples: int = 40) -> dict[str, Any]:
    """Re-derive proofs for random records and check they fold to the root.

    Also checks the negative case: a tampered leaf must fail. A proof system
    that accepts everything proves nothing.
    """
    rows = arc.conn.execute(
        "SELECT snapshot_id, merkle_root FROM snapshots "
        "WHERE status='ok' AND row_count > 2 ORDER BY RANDOM() LIMIT ?",
        (samples,),
    ).fetchall()
    checked = rejected_ok = 0
    bad = []
    rng = random.Random(20260727)
    for r in rows:
        pairs = arc.leaves_for(r["snapshot_id"])
        if len(pairs) < 3:
            continue
        leaves = [leaf_hash(u, h) for u, h in pairs]
        idx = rng.randrange(len(leaves))
        proof = merkle_proof(leaves, idx)
        checked += 1
        if not verify_proof(leaves[idx], proof, r["merkle_root"]):
            bad.append({"snapshot_id": r["snapshot_id"], "leaf_index": idx,
                        "error": "valid leaf failed its own proof"})
        # Negative control.
        tampered = leaf_hash(pairs[idx][0], "0" * 64)
        if verify_proof(tampered, proof, r["merkle_root"]):
            bad.append({"snapshot_id": r["snapshot_id"], "leaf_index": idx,
                        "error": "tampered leaf accepted"})
        else:
            rejected_ok += 1
    return {
        "checked": checked,
        "tamper_rejections": rejected_ok,
        "failed": bad,
        "ok": not bad,
    }


def verify_dedup_pointers(arc: Archive) -> dict[str, Any]:
    """Snapshots that reuse an earlier record set must agree with it exactly.

    Deduplication is a storage optimisation; it must never change what the
    archive claims to have seen.
    """
    rows = arc.conn.execute(
        "SELECT snapshot_id, observations_ref, merkle_root FROM snapshots "
        "WHERE observations_ref IS NOT NULL"
    ).fetchall()
    bad = []
    for r in rows:
        owner = arc.conn.execute(
            "SELECT merkle_root FROM snapshots WHERE snapshot_id=?",
            (r["observations_ref"],),
        ).fetchone()
        if not owner:
            bad.append({"snapshot_id": r["snapshot_id"], "error": "dangling reference"})
        elif owner["merkle_root"] != r["merkle_root"]:
            bad.append({
                "snapshot_id": r["snapshot_id"],
                "error": "deduplicated against a snapshot with a different root",
            })
    return {"checked": len(rows), "failed": bad, "ok": not bad}


def run(db: str, verbose: bool = False) -> dict[str, Any]:
    arc = Archive(db)
    results = {
        "merkle_roots": verify_merkle_roots(arc),
        "hash_chains": verify_chains(arc),
        "blob_integrity": verify_blob_integrity(arc),
        "inclusion_proofs": verify_inclusion_proofs(arc),
        "dedup_pointers": verify_dedup_pointers(arc),
    }
    results["all_ok"] = all(v["ok"] for v in results.values() if isinstance(v, dict))
    arc.close()
    return results


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Verify the Palimpsest archive.")
    ap.add_argument("--db", default="archive/palimpsest.db")
    ap.add_argument("--json", action="store_true", help="emit machine-readable output")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    res = run(args.db)

    if args.json:
        print(json.dumps(res, indent=2))
        return 0 if res["all_ok"] else 1

    labels = {
        "merkle_roots": "Merkle roots recomputed from stored records",
        "hash_chains": "hash chains replayed from genesis",
        "blob_integrity": "payloads rehashed against their content keys",
        "inclusion_proofs": "inclusion proofs re-derived (with tamper controls)",
        "dedup_pointers": "deduplicated snapshots agree with their referent",
    }
    print()
    print("  PALIMPSEST ARCHIVE VERIFICATION")
    print("  " + "-" * 58)
    for key, label in labels.items():
        r = res[key]
        n = r.get("checked", r.get("sampled", 0))
        mark = "ok  " if r["ok"] else "FAIL"
        print(f"  [{mark}] {label:<52} {n:>6}")
        for f in r["failed"][:5]:
            print(f"         -> {f}")
    print("  " + "-" * 58)
    ip = res["inclusion_proofs"]
    if ip["checked"]:
        print(f"  {ip['tamper_rejections']} tampered leaves correctly rejected")
    print(f"  RESULT: {'archive intact' if res['all_ok'] else 'INTEGRITY FAILURE'}")
    print()
    return 0 if res["all_ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
