"""Print a short summary of the exported evidence bundle."""

import json
import sys
from pathlib import Path

path = Path(sys.argv[1] if len(sys.argv) > 1 else "web/data.json")
bundle = json.loads(path.read_text(encoding="utf-8"))

o = bundle["overview"]
c = bundle["churn"]
cs = bundle["census"]
pa = bundle.get("publisher_assertions") or {}

print(f"  datasets      : {o['sources_watched']} across {o['city_count']} cities")
print(f"  sweeps        : {o['sweeps']}")
print(f"  observations  : {o['observations']:,}")
print(f"  record states : {o['distinct_record_states']:,}")
print(f"  chains        : {bundle['integrity']['chains_ok']}"
      f"/{bundle['integrity']['chains_verified']} verified")

share = cs.get("share_uninterpretable")
if share is not None:
    print(f"  saturated     : {cs['timestamps_uninterpretable']} of "
          f"{cs['characterised']} ({share * 100:.0f}%)")

print(f"  material      : {c['material_changes']:,} changes")
print(f"    revisions   : {c['isolated_revisions']:,} isolated, "
      f"{c['coordinated_revisions']:,} coordinated")
print(f"    deletions   : {c['deletions']:,}")
print(f"  discarded     : {c['discarded_as_mechanism']:,} as publishing mechanism")

if pa:
    print(f"  admitted      : {pa.get('total_records_asserted_modified', 0):,} "
          f"closed-period records, per publishers")

print(f"  bundle        : {path.stat().st_size / 1024:.0f} KB")
