"""Tests for the classification rules that separate editing from mechanism.

Every case here is drawn from real observed data. Each one, misclassified,
produces a confident public claim that is false — which is the failure mode this
project exists to avoid, so the rules are pinned down.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from palimpsest.diff import (  # noqa: E402
    ChangeKind,
    _is_lifecycle,
    _is_ordering_only,
    _mark_coordinated,
    _resolve_identity_churn,
    _revision_significance,
    field_weight,
)

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        failures.append(name)


print("\n-- progression versus revision --")
# Austin construction permits, one sweep, two different kinds of movement.
check("permit becoming Final is progression",
      _is_lifecycle({"status_current": ["Active", "Final"]}))
check("a blank being filled is progression",
      _is_lifecycle({"completed_date": [None, "2026-07-27T00:00:00.000"]}))
check("status advance plus filled blank is progression",
      _is_lifecycle({
          "status_current": ["Active", "Final"],
          "completed_date": ["", "2026-07-27T00:00:00.000"],
      }))
check("floor area changing 463 -> 2551 is a revision",
      not _is_lifecycle({"total_new_add_sqft": ["463", "2551"]}))
check("a stated date being replaced is a revision",
      not _is_lifecycle({"issue_date": ["2025-01-04", "2025-03-19"]}))
check("mixed progression and overwrite counts as revision",
      not _is_lifecycle({
          "status_current": ["Active", "Final"],
          "total_new_add_sqft": ["463", "2551"],
      }))
check("empty deltas are not progression", not _is_lifecycle({}))

print("\n-- ordering versus reclassification --")
# San Francisco DA case resolutions: one cell, charges re-sorted.
check("a re-sorted list in one cell is ordering only",
      _is_ordering_only({"list_of_filed_charges": [
          "245A1/M/0, 245A4/M/0, 242/M/0",
          "242/M/0, 245A1/M/0, 245A4/M/0"]}))

# Chicago arrest 30477683: values traded places across parallel columns.
check("values swapping between parallel columns is ordering only",
      _is_ordering_only({
          "charge_1_description": ["BATTERY - CAUSE BODILY HARM",
                                   "RETAIL THEFT/DISP MERCH/<$300"],
          "charge_2_description": ["RETAIL THEFT/DISP MERCH/<$300",
                                   "BATTERY - CAUSE BODILY HARM"],
          "charge_1_statute": ["720 ILCS 5.0/12-3-A-1", "720 ILCS 5.0/16-25-A-1"],
          "charge_2_statute": ["720 ILCS 5.0/16-25-A-1", "720 ILCS 5.0/12-3-A-1"],
      }))

# A charge genuinely added must survive.
check("an added charge is a real change",
      not _is_ordering_only({"list_of_filed_charges": [
          "242/M/0, 245A1/M/0", "242/M/0, 245A1/M/0, 459/F/2"]}))
check("a substituted charge is a real change",
      not _is_ordering_only({"charge_1_description": ["BATTERY", "HOMICIDE"]}))
check("a dropped charge is a real change",
      not _is_ordering_only({"list_of_filed_charges": [
          "242/M/0, 245A1/M/0, 459/F/2", "242/M/0, 245A1/M/0"]}))
check("a single scalar edit is not ordering",
      not _is_ordering_only({"total_new_add_sqft": ["463", "2551"]}))
check("empty deltas are not ordering", not _is_ordering_only({}))

print("\n-- identity churn --")


def _base(kind, **kw):
    return {"source_key": "s", "from_snapshot": 1, "to_snapshot": 2,
            "detected_at": "t", "kind": kind, **kw}


# San Francisco: 1,417 departures, 1,417 arrivals, population unchanged.
balanced = ([_base(ChangeKind.DELETION, row_uid=f"d{i}", significance=1.0) for i in range(1417)]
            + [_base(ChangeKind.RETROACTIVE_APPEND, row_uid=f"a{i}", significance=0.6)
               for i in range(1417)])
out = _resolve_identity_churn(list(balanced), 1417, 1417, "business_key")
kinds = {c["kind"] for c in out}
check("balanced departures/arrivals become identity churn",
      kinds == {ChangeKind.IDENTITY_CHURN}, str(kinds))
check("...and are not reported as deletions",
      not any(c["kind"] == ChangeKind.DELETION for c in out))

# Under content identity nothing can be attributed at all.
content_keyed = [_base(ChangeKind.DELETION, row_uid="x", significance=1.0)]
out = _resolve_identity_churn(list(content_keyed), 1, 0, "content")
check("content-keyed sources never claim deletion",
      out[0]["kind"] == ChangeKind.IDENTITY_CHURN)

# A genuine mass deletion must survive.
only_gone = [_base(ChangeKind.DELETION, row_uid=f"d{i}", significance=1.0) for i in range(50)]
out = _resolve_identity_churn(list(only_gone), 50, 0, "business_key")
check("unmatched departures remain deletions",
      all(c["kind"] == ChangeKind.DELETION for c in out))
check("...at full significance", out[0]["significance"] == 1.0)

# Partial overlap: discount, but keep the excess.
mixed = ([_base(ChangeKind.DELETION, row_uid=f"d{i}", significance=1.0) for i in range(100)]
         + [_base(ChangeKind.RETROACTIVE_APPEND, row_uid=f"a{i}", significance=0.6)
            for i in range(10)])
out = _resolve_identity_churn(list(mixed), 100, 10, "business_key")
dels = [c for c in out if c["kind"] == ChangeKind.DELETION]
check("partially matched departures stay deletions", len(dels) == 100)
check("...but are discounted", 0 < dels[0]["significance"] < 1.0,
      str(dels[0]["significance"]))

print("\n-- coordinated versus isolated --")
# 5,000 records changing identically is a migration, not 5,000 edits.
mass = [_base(ChangeKind.SEMANTIC_REVISION, row_uid=f"r{i}", significance=1.0,
              field_deltas={"primary_type": ["BURGLARY", "Burglary"]})
        for i in range(60)]
out = _mark_coordinated(list(mass))
check("identical mass change is marked coordinated",
      all(c["kind"] == ChangeKind.COORDINATED_REVISION for c in out))
check("...and heavily discounted", out[0]["significance"] < 0.3,
      str(out[0]["significance"]))

# A lone edit in a quiet sweep is the strongest signal available.
lone = [_base(ChangeKind.SEMANTIC_REVISION, row_uid="r1", significance=0.8,
              field_deltas={"primary_type": ["HOMICIDE", "ASSAULT"]}),
        _base(ChangeKind.SEMANTIC_REVISION, row_uid="r2", significance=0.5,
              field_deltas={"ward": ["3", "4"]})]
out = _mark_coordinated(list(lone))
check("isolated edits stay semantic revisions",
      all(c["kind"] == ChangeKind.SEMANTIC_REVISION for c in out))
check("...and are promoted", out[0]["significance"] > 0.8, str(out[0]["significance"]))
check("...with the reason stated", "isolated" in out[0]["detail"])

print("\n-- significance --")
check("classification outweighs coordinates",
      field_weight("primary_type") > field_weight("latitude"))
check("a changed classification scores high",
      _revision_significance({"primary_type": ["A", "B"]}) >= 0.9)
check("a nudged coordinate scores low",
      _revision_significance({"latitude": ["41.1", "41.2"]}) <= 0.35)
check("one heavy field beats several light ones",
      _revision_significance({"primary_type": ["A", "B"]})
      > _revision_significance({"latitude": ["1", "2"], "longitude": ["1", "2"],
                                "x_coord": ["1", "2"]}))

print()
if failures:
    print(f"FAILED: {len(failures)} check(s): {failures}")
    raise SystemExit(1)
print("All checks passed.")
