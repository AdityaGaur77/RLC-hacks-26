# Palimpsest

*A palimpsest is a manuscript scraped clean and written over, where the erased text
still bleeds through.*

**Government publishes as though the web were permanent. It is not.**

Datasets are republished with the past rewritten. Records vanish. Values are corrected
without a diff, a version history, or a notice. There is no changelog for the public
record — which is why retroactive edits to it are not merely unnoticed, they are
*structurally invisible*.

Palimpsest is a tamper-evident archive that watches municipal open-data portals,
fingerprints what they publish, and detects when the past changes underneath them. It
can also prove that its own account of the past has not been altered — the property
the portals themselves lack.

---

## The problem this project almost got wrong

The first thing we measured looked like a scandal.

Chicago's crime dataset reports that **7,808,602 of its 7,981,155 records describing
events before 2024 — 98% — were modified within the last thirty days.** Evenly, across
every year back to 2001.

Read literally: a city rewrote eight million crime records.

Read correctly: the publisher drops and reloads the entire table on a schedule. Every
row receives a fresh internal identifier and a fresh modification timestamp on every
run, while **not one published fact changes**. Records describing April 2001 carry a
creation timestamp of yesterday.

Meanwhile San Francisco's 311 dataset reports **zero** such modifications, New York's
reports **4,484**, and Chicago's building permits report **5,906**.

Those four numbers are not comparable, and the difference between them is not data
quality — it is *publishing architecture*. Any tool that ranks cities by "edit rate"
without establishing this first is measuring deployment style and calling it integrity.

So Palimpsest is built around a distinction that most of the work goes into:

| | what moved | what it means |
|---|---|---|
| **Provenance churn** | platform ids, internal timestamps, "last updated" columns | the publisher's pipeline ran |
| **Semantic revision** | the hash of the record's *published fields* | a fact changed |

Only the second is a finding. The first is the false positive this project exists to
refuse — and on republish-style portals, it is 98% of the signal.

Ten distinct mechanisms turned out to masquerade as editing. Each was found by
investigating a result that looked like a scandal, and each is documented in
[docs/METHODOLOGY.md](docs/METHODOLOGY.md) with the observation that exposed it:

1. **Provenance churn** — the table is reloaded, so every row is "modified".
2. **Identity churn** — 1,417 deletions arriving with 1,417 insertions and no change in population.
3. **Bookkeeping columns** — `refresh_time` published as though it were a fact.
4. **Recomputed columns no pattern can name** — `sr_age_days: 417 → 418`, a clock that advances for every record every day.
5. **Keys that are unique but not stable** — an ordinal within a filing, re-binding to a different donor when rows are re-sorted.
6. **Cases advancing through their workflow** — a permit becoming Final is not the past being rewritten.
7. **The same values in a different order** — charges re-sorted within a cell, or traded between `charge_1` and `charge_2`.
8. **Records that come back** — 166 "deleted" homicide victim records were present again a few sweeps later.
9. **Zero as a placeholder, and pointers to "the latest"** — `fee: 0 → 149` is an invoice being raised; `last_doc` advances by design.
10. **Schema migrations** — a newly added column differs in every record, from absent to present.

The later ones are the interesting ones, because they defeat the controls built for the
earlier ones. Number 4 cannot be caught by naming rules at all, and forced the analysis
to measure publisher behaviour empirically instead of guessing from column names.
Number 5 initially appeared *refuted* by the obvious test — because number 4 was
running simultaneously and masking it. They had to be removed in the right order for
either to become visible.

The cumulative effect is the project's central claim. Over 36 sweeps of 112 datasets:

> **A blind pass sees 79,388 changes. After controlling for publishing mechanism,
> 6,627 remain. 91.7% of apparent change was the machinery, not the record.**

| | count | |
|---|---:|---|
| apparent changes, uncontrolled | 79,388 | what a naive tool reports |
| dissolved once mechanism was accounted for | 47,329 | recomputed columns, withdrawn sources |
| provenance churn | 17,842 | the pipeline ran; no published value moved |
| identity churn | 3,307 | keys moved, population unchanged |
| ordering changes | 370 | the same values rearranged |
| transient absence | 166 | missing once, present again later |
| lifecycle progression | 304 | a case advancing; not the past being rewritten |
| **isolated revisions** | **1,921** | a stated value replaced, with nothing systematic explaining it |
| coordinated revisions | 3,519 | mass migrations — real, but systematic and self-disclosing |
| deletions | 1,187 | records absent from every observation since |

Both counts are written into the archive by the analysis itself (`analysis_stages`) and
read from there by the site, so the ratio can be checked rather than taken on trust.
Re-run it with `scripts/analyse.ps1`; the two passes reproduce exactly.

A tool that skipped this work would have reported 67,275.

---

## How it works

```
discover.py   pick datasets; choose an event-date column, a natural key,
              and a frozen observation window for each
      |
pipeline.py   probe each publisher: does it edit rows, or rebuild the table?
              (decides whether its own timestamps mean anything at all)
      |
collect.py    every 3 hours: fingerprint every record in the frozen window,
              plus aggregate counts across the whole closed past
      |
   store.py   content-addressed, append-only, Merkle-rooted, hash-chained
      |
   diff.py    classify what moved: revision / deletion / retroactive append /
              schema drift / churn — and coordinated vs isolated
      |
 report.py    export a static evidence bundle anyone can re-verify
```

### Identity

Record identity uses each dataset's **natural key** (`case_number`, `unique_key`,
`permit_number`), verified unique against a live sample before the dataset is accepted.
It deliberately does *not* use the platform's internal row id: on a table reload every
internal id is regenerated, and every record would appear to be simultaneously deleted
and created.

### Two resolutions at once

- **The stratum** — every record inside a fixed past window, hashed field by field.
  High resolution, but a keyhole.
- **Aggregates** — row counts over the entire closed past plus categorical tallies.
  Three requests regardless of whether the dataset holds ten thousand rows or ten
  million. Wide angle, but only in outline.

Neither is sufficient alone. A deletion outside the stratum is invisible to the first;
a single altered value is invisible to the second.

### Coordinated versus isolated

Five thousand records changing `BURGLARY` to `Burglary` in one sweep is a formatting
migration. One homicide record changing classification on its own is a different kind
of event. Both move the content hash identically, so revisions sharing an identical
field-level transformation are grouped and de-emphasised, while an edit with no
comparable change in the same sweep is scored *up*.

### Tamper evidence

Each observation commits to a Merkle root over `(record identity, content hash)` pairs,
sorted so the root does not depend on arrival order. Odd nodes are promoted rather than
duplicated — duplicating the final node lets two different record sets produce the same
root, which would defeat the purpose of publishing it. Each root is chained to its
predecessor, so altering any past observation changes every hash after it.

The published bundle includes **worked inclusion proofs**: the sibling path showing that
a specific record held a specific value at a specific moment, checkable without the
other several thousand records. A root nobody can check against an actual record is
decoration.

---

## What this cannot tell you

Stated plainly, because a tool about honest record-keeping should keep an honest record
of itself.

1. **It cannot see edits made before it started watching.** The archive begins when
   collection begins. For incrementally-published datasets it can surface records the
   *publisher itself* says it modified — but that is the publisher's assertion about
   the fact of a change, and it reveals nothing about what the record previously said.

2. **Absence in the stratum is not proof of deletion.** A record can leave the window
   because it was deleted, or because its event date was edited so it now falls outside.
   These are reported distinctly where they can be distinguished.

3. **Aggregate counts can hide offsetting changes.** A deletion and an insertion in the
   same closed window net to zero. The stratum catches this within its span; outside it,
   this class of change can pass unseen.

4. **Identity depends on the natural key being genuinely unique and stable.** Key
   collisions are detected and logged, and revision is not attributed for sources where
   they occur — but a key that is unique in the sample and duplicated elsewhere in the
   dataset remains a source of error.

5. **The coordination threshold (25 identical transformations) is a heuristic**, not a
   derived constant. A migration affecting twenty records will read as twenty isolated
   edits.

6. **Socrata portals only.** Legistar's public API returned HTTP 500 throughout
   development, and non-Socrata portals are not yet covered. Coverage is a convenience
   sample of large US municipal portals, not a representative one — no claim is made
   that these cities are typical.

7. **We observe what the API returns.** A CDN serving a stale response is not
   distinguishable from a publisher reverting a value. The portal's own freshness
   headers (`Last-Modified`, `ETag`, `X-SODA2-Truth-Last-Modified`) are archived with
   every observation so this can be checked rather than assumed.

---

## Verify it yourself

The archive is designed to be checked, not trusted.

```bash
python -m palimpsest.verify --db archive/palimpsest.db
```

This recomputes every Merkle root from the stored records, replays every hash chain from
genesis, and re-derives a sample of inclusion proofs. Any discrepancy is reported with
the snapshot where the chain first breaks.

To confirm a specific finding against the live portal, every finding in the bundle
carries its source URL, the record's natural key, both content hashes, and the exact
observation timestamps.

---

## Running it

```bash
python -m palimpsest.discover --per-portal 35    # build the watchlist
python -m palimpsest.pipeline                    # characterise publishers
python -m palimpsest.collect --interval 3        # observe, forever
python -m palimpsest.diff                        # classify what moved
python -m palimpsest.report                      # export the evidence bundle
```

The collector is designed to run continuously; `scripts/install-watchdog.ps1` keeps it
alive across reboots on Windows, and `scripts/uninstall-watchdog.ps1` removes it.

Requires Python 3.11+. No third-party dependencies.

---

## Conduct

Palimpsest reads **public endpoints only**, at one request every 700ms, identifying
itself in the `User-Agent`, honouring `Retry-After`, and backing off on error. It never
authenticates, never writes, never submits a form, and never accesses anything that was
not published to any member of the public who asked. Everything in the archive was
already public; the only thing added is memory.

## Data sources

Municipal open-data portals operated by Chicago, New York City, San Francisco, Los
Angeles, Seattle, Austin, Baltimore, Nashville, Kansas City, and Montgomery County MD,
served via the Socrata SODA 2.0 API. Dataset titles, publishers, and canonical URLs are
carried in the exported bundle for every source watched.

## Disclosure of AI use

This project was built in collaboration with Claude (Anthropic), used as a coding
assistant throughout: drafting the collector, store, diff engine, and interface, and
writing this documentation. All architectural decisions, the methodology, and the
framing were developed in dialogue and reviewed by the author.

Notably, the central methodological correction in this project came from *checking*
rather than accepting an AI-assisted first result: the initial probe reported a 98% edit
rate on Chicago crime data, and investigating that number — rather than publishing it —
is what produced the provenance-versus-revision distinction the whole tool is now built
around. No generative model is used anywhere in the detection path; every finding is
produced by deterministic hashing and set comparison, and is reproducible from the
archive by anyone.

## Licence

MIT.
