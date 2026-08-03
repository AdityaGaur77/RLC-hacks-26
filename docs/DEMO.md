# How to demo Palimpsest

Every command here runs against the real archive and prints real output. Nothing is
staged. Run them from the repository root.

---

## The 90-second version

```bash
python -m palimpsest.verify --db archive/palimpsest.db
```

Recomputes every Merkle root from stored records, replays every hash chain from genesis,
rehashes payloads against their content keys, re-derives inclusion proofs, and tries to
sneak tampered leaves past those same proofs. Ends in `RESULT: archive intact`.

Then open the site and read three numbers off the header: **89.8%**, **6,832**,
**intact**.

That is the whole project: a lot of apparent change, very little real change, and an
archive that can prove it didn't cook either figure.

---

## The full demo, in the order that makes sense

### 1. Show the trap first

Nothing lands unless people first believe the naive version is reasonable.

```bash
python scratchpad/probe2.py
```

*(or just show the saved output — it takes ~40s live)*

Chicago reports **97.8%** of its pre-2024 crime records modified in 30 days. Then point at
a single row: `date: 2001-04-01`, `:created_at: <yesterday>`. The record describes an
arrest from 2001 and the row was created yesterday. The table is rebuilt on a schedule;
nothing was edited.

**The line to say:** *"That's the number a diff tool would have published."*

### 2. Show what the controls remove

```bash
python scripts/summary.py web/data.json
```

Or on the site, the **"What the controls remove"** ladder:

| stage | count |
|---|---:|
| apparent changes, uncontrolled | 79,388 |
| dissolved once mechanism was measured | 47,329 |
| provenance churn | 17,842 |
| identity churn | 3,307 |
| ordering changes | 370 |
| transient absence | 166 |
| lifecycle progression | 304 |
| **isolated revisions** | **1,921** |

### 3. Show the one that caught us

Do this *before* the real findings. It is the most persuasive thing in the demo, and
skipping it looks like hiding it.

An `ORDERING CHANGE` card — e.g. SF District Attorney case resolutions:

```
list_of_filed_charges
  was: 245A1/M/0, 245A4/M/0, 242/M/0
  now: 242/M/0, 245A1/M/0, 245A4/M/0
```

**The line to say:** *"A Chicago arrest record showed its lead charge changing from
battery to retail theft. I wrote that up as the headline finding. It's a re-sort — the
same charges in the opposite order. 328 findings were that. They're now their own
category, and the tool no longer claims a reclassification it can't prove."*

### 4. Show the real findings

In descending order of how hard they are to argue with:

- **Seattle Crisis Data** — column `cit_officer_requested` removed entirely. Whether a
  Crisis Intervention Team officer was requested at a mental health call. Schema-level, so
  no identity or ordering problem can touch it.
  ([source](https://data.seattle.gov/d/i2q9-thny))
- **Austin Code Complaint Cases** — a closed window shrinking across consecutive sweeps:
  51,025 → 50,933 → 50,758 → 50,722. An aggregate count, immune to every key problem,
  because counting doesn't require knowing which record is which.
- **Austin permit `2025-015600 EP`** — `total_new_add_sqft: 463 → 2551`. One scalar field,
  no status change, nothing systematic to explain it.
  ([source](https://data.austintexas.gov/d/3syk-w9eu))
- **Austin permit `2025-039073 PP`** — contractor of record replaced:
  `Dahl Plumbing Co.` → `MEK Homes, LLC`, `Donald Dahl` → `Mathew Kruger`.

### 4. Show the proof

On the site, the **Verify** section carries a worked inclusion proof: a record from a
Seattle snapshot, its content hash, the Merkle root, and all 13 sibling hashes — leaf
2,992 of 5,984. Anyone can recompute the root from the leaf and the siblings.

Then show that the proof can *fail*:

```bash
python tests/test_core.py
```

40 tampered leaves are rejected by the same proofs that accept the real ones. A proof that
can't fail proves nothing.

### 5. Show what it refuses to claim

This is the part to end on.

```bash
python -m palimpsest.volatility --db archive/palimpsest.db
```

Two sources come back `WITHDRAWN`. Austin's campaign finance keys are ordinals within a
filing — re-sorting re-binds them to different donors. Seattle's permit review data
changes `permitnum` in 99.5% of apparent revisions, and a permit number that changes isn't
identifying a permit.

Palimpsest drops every per-record claim about both and states it on the page.

---

## Reproducing the headline from scratch

```bash
powershell -ExecutionPolicy Bypass -File scripts/analyse.ps1
```

Two passes. The first classifies blind and records 67,275. Its output is then used to
measure how each publisher behaves, and the second pass reclassifies with that knowledge.
Both counts land in the `analysis_stages` table, which is where the site reads the ratio
from — so it's checkable rather than asserted.

The two passes reproduce exactly across runs.

---

## Questions you'll get, and the honest answers

**"Isn't a status change just normal?"**
Yes, and it's classified separately as `lifecycle_progression`. Filling a blank and
advancing a status are progression; replacing one non-empty value with a different one is
revision. A permit becoming `Final` doesn't appear as a finding.

**"How do you know a change is retroactive and not a late filing?"**
Two ways. Every stratum sits at least a year in the past, so ordinary case activity has
long since settled. And aggregate counts over the *entire* closed past are tracked
separately — those are immune to key problems, because counting doesn't require knowing
which record is which.

**"Couldn't you have missed changes?"**
Yes, and this is stated in the limitations. The stratum is a keyhole: a change to a record
outside the window is only visible in aggregate. Sources whose keys are unstable can't
support per-record claims at all. And anything that changed and changed back between two
sweeps is invisible — which is why the interval is two hours rather than daily.

**"Is this legal / were you polite?"**
Public, unauthenticated endpoints only. Read-only. One request every 0.7 seconds, a
self-identifying User-Agent, retry-after honoured. Roughly 50 requests per portal per hour
against a budget of about a thousand.

**"How much is AI-written?"**
Most of the implementation, disclosed in `docs/DEVPOST.md`. Worth stating plainly in both
directions: several confounders were caught by the agent investigating its own suspicious
results — and one overclaim was introduced by that agent before being caught. Both are
disclosed.

---

## Do not do these things

- **Don't lead with the findings.** They're unimpressive without the 89.8% context —
  four permit edits and a charge swap sounds small. The point is that four survived
  sixty-seven thousand.
- **Don't call it a scandal.** Nothing here shows intent, and the moment you overclaim,
  a judge who works with data pipelines stops believing the rest.
- **Don't hide the withdrawn sources.** They're the most credible thing on the page.
