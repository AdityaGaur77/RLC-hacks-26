"""Tests for the primitives everything else depends on.

The Merkle logic in particular has to be right the first time: a flaw discovered
after a week of collection invalidates the whole archive, and the archive is the
one thing that cannot be rebuilt after the fact.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from palimpsest.core import (  # noqa: E402
    EMPTY_ROOT,
    canonical_json,
    chain_hash,
    classify_fields,
    fingerprint_record,
    is_system_field,
    is_volatile_field,
    leaf_hash,
    merkle_proof,
    merkle_root,
    normalise_value,
    verify_proof,
)

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        failures.append(name)


print("\n-- field classification --")
cols = [
    ":id", ":created_at", ":updated_at", ":@computed_region_awaf_s7ux",
    "case_number", "primary_type", "date", "updated_on", "latitude",
]
fc = classify_fields(cols)
check("platform internals excluded from content",
      set(fc["system"]) == {":id", ":created_at", ":updated_at", ":@computed_region_awaf_s7ux"},
      str(fc["system"]))
check("published 'updated_on' treated as volatile, not semantic",
      fc["volatile"] == ["updated_on"], str(fc["volatile"]))
check("real facts remain semantic",
      set(fc["semantic"]) == {"case_number", "primary_type", "date", "latitude"},
      str(fc["semantic"]))

print("\n-- ETL bookkeeping is not a published fact --")
# Each of these appeared in a real diff and produced a false revision.
for name in [
    "refresh_time", "refresh_date", "refreshed_at", "last_updated", "updated_on",
    "date_modified", "load_date", "extract_ts", "ingested_at", "etl_date",
    "snapshot_date", "last_run_time", "row_version", "record_hash", "as_of_date",
    "processed_on", "data_as_of", "published_at",
    # These were found by auditing real columns, not by imagination.
    "data_updated_at", "data_loaded_at", "updated_datetime",
    "last_updated_date_time", "violation_last_modified_date", "modified_by",
    "last_edited_date", "modifiedon", "recordid", "last_update_date",
]:
    check(f"'{name}' classified as volatile", is_volatile_field(name))

# ...but real fields with adjacent names must survive. Every one of these is a
# genuine column from the watchlist that an over-broad rule would have eaten.
for name in [
    "status_date", "status_desc", "issue_date", "inspection_date", "update_type",
    "date", "arrest_date", "modification_request", "record_type", "load_capacity",
    "applicant_last_name", "homicide_victim_last_name", "owner_last_name",
    "last_objection_date", "last_doc_date", "last_status_type", "last_name",
    "issued_in_last_30_days", "filing_representative_last_name",
]:
    check(f"'{name}' remains semantic",
          not is_volatile_field(name) and not is_system_field(name))

print("\n-- value normalisation --")
check("5 and 5.0 collapse", normalise_value("5.0") == normalise_value(5) == "5")
check("whitespace stripped", normalise_value("  x  ") == "x")
check("empty string is null", normalise_value("") is None)
check("dict key order irrelevant",
      canonical_json(normalise_value({"b": 1, "a": 2}))
      == canonical_json(normalise_value({"a": 2, "b": 1})))

print("\n-- the republish problem --")
# The same published fact, before and after a table reload. Platform internals
# and the volatile column move; not one published value changes.
before = {
    ":id": "row-aaaa.bbbb-cccc", ":created_at": "2019-01-02T00:00:00Z",
    ":updated_at": "2019-01-02T00:00:00Z", "case_number": "JK123",
    "primary_type": "BURGLARY", "date": "2019-01-01T00:00:00", "updated_on": "2019-01-02",
}
after = {
    ":id": "row-zzzz~yyyy_xxxx", ":created_at": "2026-07-26T11:15:11Z",
    ":updated_at": "2026-07-26T11:15:16Z", "case_number": "JK123",
    "primary_type": "BURGLARY", "date": "2019-01-01T00:00:00", "updated_on": "2026-07-26",
}
fb = fingerprint_record(before, ["case_number"])
fa = fingerprint_record(after, ["case_number"])
check("identity survives a table reload", fb.row_uid == fa.row_uid == "JK123")
check("content hash unchanged when no fact changed", fb.content_hash == fa.content_hash)
check("volatile hash does change (churn is visible, just not counted)",
      fb.volatile_hash != fa.volatile_hash)

# Now a genuine reclassification, with platform internals held still.
revised = dict(before, primary_type="THEFT")
fr = fingerprint_record(revised, ["case_number"])
check("a real reclassification does change the content hash",
      fr.content_hash != fb.content_hash)
check("...and is attributable to the same record", fr.row_uid == fb.row_uid)

print("\n-- merkle --")
check("empty root is a fixed constant", merkle_root([]) == EMPTY_ROOT)

for n in (1, 2, 3, 4, 5, 7, 8, 9, 16, 17, 33, 100, 1001):
    leaves = [leaf_hash(f"uid-{i}", f"hash-{i}") for i in range(n)]
    root = merkle_root(leaves)
    ok = all(verify_proof(leaves[i], merkle_proof(leaves, i), root) for i in range(n))
    check(f"proofs verify for every leaf (n={n})", ok)

leaves = [leaf_hash(f"uid-{i}", f"hash-{i}") for i in range(9)]
root = merkle_root(leaves)
bad = leaf_hash("uid-0", "tampered")
check("a tampered leaf fails its own proof",
      not verify_proof(bad, merkle_proof(leaves, 0), root))
check("a valid leaf fails another leaf's proof",
      not verify_proof(leaves[0], merkle_proof(leaves, 3), root))

# Odd-node promotion must not let two different leaf sets share a root.
check("promotion does not collide with duplication",
      merkle_root(leaves[:3]) != merkle_root(leaves[:3] + [leaves[2]]))

# Order independence is deliberately NOT a property: the archive sorts by
# row_uid before hashing, so a reordering by the portal is not a change.
check("root is order-sensitive (hence the sort before hashing)",
      merkle_root(leaves) != merkle_root(list(reversed(leaves))))

print("\n-- chaining --")
c1 = chain_hash("genesis", "root1", {"n": 1})
c2 = chain_hash(c1, "root2", {"n": 2})
c3 = chain_hash(c2, "root3", {"n": 3})
# Rewrite history at snapshot 1 and everything downstream must diverge.
c1b = chain_hash("genesis", "root1-altered", {"n": 1})
c2b = chain_hash(c1b, "root2", {"n": 2})
c3b = chain_hash(c2b, "root3", {"n": 3})
check("altering the past breaks every subsequent link", c3 != c3b and c2 != c2b)
check("chaining is deterministic", chain_hash("genesis", "root1", {"n": 1}) == c1)

print()
if failures:
    print(f"FAILED: {len(failures)} check(s): {failures}")
    raise SystemExit(1)
print("All checks passed.")
