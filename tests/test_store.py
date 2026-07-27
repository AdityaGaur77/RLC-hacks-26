"""Tests for the archive's storage invariants.

The central one, learned the hard way: **the published Merkle root must commit to
exactly the record set the archive can reproduce.** Storage is keyed on
(snapshot, row_uid), so a duplicate identity is silently collapsed on write. When
the root was computed before that collapse, it described a record set that could
no longer be re-derived from the archive — and every inclusion proof against it
failed. A verifier caught this; these tests keep it caught.
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from palimpsest.core import fingerprint_record, leaf_hash, merkle_root  # noqa: E402
from palimpsest.store import Archive  # noqa: E402
from palimpsest.verify import (  # noqa: E402
    verify_blob_integrity,
    verify_chains,
    verify_dedup_pointers,
    verify_inclusion_proofs,
    verify_merkle_roots,
)

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        failures.append(name)


def new_archive() -> Archive:
    d = tempfile.mkdtemp(prefix="palimpsest-test-")
    a = Archive(Path(d) / "test.db")
    a.upsert_source({
        "source_key": "example.org/aaaa-bbbb", "domain": "example.org",
        "fourfour": "aaaa-bbbb", "title": "Test", "city": "Testville",
        "category": "test", "date_field": "date", "business_key": ["case"],
        "stratum_start": "2025-01-01T00:00:00", "stratum_end": "2025-02-01T00:00:00",
        "extra_volatile": [], "agg_dimensions": [], "pipeline_class": "unknown",
        "notes": "", "added_at": "2026-07-27T00:00:00+00:00",
    })
    return a


def rows_to_fps(rows, key=("case",)):
    return [fingerprint_record(r, list(key)) for r in rows]


SRC = "example.org/aaaa-bbbb"
COLS = ["case", "type", "date"]
FC = {"semantic": COLS, "volatile": [], "system": []}


print("\n-- root commits to exactly what is stored --")
arc = new_archive()
# Two records sharing a natural key: the collision that broke verification.
rows = [
    {"case": "A1", "type": "THEFT", "date": "2025-01-05"},
    {"case": "A1", "type": "BURGLARY", "date": "2025-01-06"},
    {"case": "A2", "type": "ASSAULT", "date": "2025-01-07"},
]
res = arc.write_snapshot(SRC, "2026-07-27T00:00:00+00:00", rows_to_fps(rows),
                         COLS, FC, {}, {}, 0.1)
stored = arc.leaves_for(res["snapshot_id"])
recomputed = merkle_root([leaf_hash(u, h) for u, h in stored])
check("root matches the stored record set even with a colliding key",
      recomputed == res["merkle_root"], f"{recomputed} != {res['merkle_root']}")
check("collapse is disclosed rather than silent",
      res["row_count"] == len(stored), f"{res['row_count']} vs {len(stored)}")
v = verify_merkle_roots(arc)
check("verifier accepts the snapshot", v["ok"], str(v["failed"]))
p = verify_inclusion_proofs(arc, samples=5)
check("every inclusion proof folds to the root", p["ok"], str(p["failed"]))

print("\n-- content identity keeps genuinely distinct records apart --")
arc2 = new_archive()
fps = [fingerprint_record(r, []) for r in rows]  # no business key
res2 = arc2.write_snapshot(SRC, "2026-07-27T00:00:00+00:00", fps, COLS, FC, {}, {}, 0.1)
check("distinct content survives the fallback", res2["row_count"] == 3,
      str(res2["row_count"]))
dupe = [{"case": "Z", "type": "X", "date": "2025-01-01"}] * 2
fps_d = [fingerprint_record(r, []) for r in dupe]
res3 = arc2.write_snapshot(SRC, "2026-07-27T01:00:00+00:00", fps_d, COLS, FC, {}, {}, 0.1)
check("byte-identical records collapse (they are indistinguishable)",
      res3["row_count"] == 1, str(res3["row_count"]))

print("\n-- deduplication of unchanged sweeps --")
arc3 = new_archive()
base = [{"case": f"C{i}", "type": "THEFT", "date": "2025-01-05"} for i in range(20)]
s1 = arc3.write_snapshot(SRC, "2026-07-27T00:00:00+00:00", rows_to_fps(base),
                         COLS, FC, {"frozen_past_count": 100}, {}, 0.1)
s2 = arc3.write_snapshot(SRC, "2026-07-27T03:00:00+00:00", rows_to_fps(base),
                         COLS, FC, {"frozen_past_count": 100}, {}, 0.1)
check("an unchanged sweep stores no duplicate rows", s2.get("deduplicated") is True)
check("roots agree across the deduplicated pair", s1["merkle_root"] == s2["merkle_root"])
check("the deduplicated snapshot still resolves its records",
      len(arc3.observations(s2["snapshot_id"])) == 20)
check("proofs still work through the pointer",
      verify_inclusion_proofs(arc3, samples=5)["ok"])
check("dedup pointers agree with their referent", verify_dedup_pointers(arc3)["ok"])

changed = list(base)
changed[3] = {"case": "C3", "type": "ARSON", "date": "2025-01-05"}
s3 = arc3.write_snapshot(SRC, "2026-07-27T06:00:00+00:00", rows_to_fps(changed),
                         COLS, FC, {"frozen_past_count": 100}, {}, 0.1)
check("a real change breaks deduplication", not s3.get("deduplicated"))
check("...and moves the root", s3["merkle_root"] != s2["merkle_root"])

print("\n-- chain integrity --")
check("chains replay from genesis", verify_chains(arc3)["ok"])
check("payloads still hash to their keys", verify_blob_integrity(arc3)["ok"])

# Tamper with a stored payload and confirm the archive notices.
arc3.conn.execute(
    "UPDATE blobs SET payload=? WHERE content_hash=(SELECT content_hash FROM blobs LIMIT 1)",
    (b'{"case":"TAMPERED"}',),
)
arc3.conn.commit()
bi = verify_blob_integrity(arc3)
check("a tampered payload is detected", not bi["ok"], str(bi["failed"][:1]))

print()
if failures:
    print(f"FAILED: {len(failures)} check(s): {failures}")
    raise SystemExit(1)
print("All checks passed.")
