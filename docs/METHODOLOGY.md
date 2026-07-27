# Methodology

## The question

Municipal open-data portals publish datasets that are widely treated as an append-only
record of what happened. They are not append-only. Records are revised, removed, and
inserted into closed periods, and none of these portals expose a version history. The
question is therefore narrow and empirical:

> **Can retroactive change to a published civic dataset be detected reliably from
> outside, using only what the portal serves to the public?**

The answer is yes, but only after controlling for confounders that dominate the naive
signal. Establishing what those confounders are, and how much of the apparent change
they account for, turned out to be the substance of the work.

---

## The central finding: apparent change is mostly mechanism

Every obvious signal we tried was dominated by how publishers deploy data rather than by
anything happening to the records. Three distinct confounders emerged, each discovered
by investigating a result that looked like a scandal, and each requiring a different
control.

### Confounder 1 — Provenance churn

**Observed.** Chicago's crime dataset reports that 7,808,602 of 7,981,155 records
describing events before 2024 (97.8%) were modified within thirty days, distributed
evenly across every year back to 2001.

**Naive reading.** A city rewrote eight million crime records.

**Actual cause.** The publisher rewrites the whole table on a schedule. Every row
receives a fresh modification timestamp — and in some datasets a fresh internal
identifier — while no published value changes. Records describing April 2001 carry a
creation timestamp of the previous day.

**Why it is not a data-quality quirk.** The magnitude varies wildly by publisher for
reasons unrelated to editing:

| dataset | records in closed past reporting recent modification |
|---|---|
| Chicago crime | 97.8% |
| Chicago building permits | 1.04% |
| New York 311 | 4,484 records |
| San Francisco 311 | 0 |

These four numbers are not comparable. Ranking cities by "edit rate" from them measures
deployment style and calls it integrity.

**Control.** Identity and content are computed from *declared semantic fields only*.
Platform internals (`:id`, `:created_at`, `:updated_at`, `:@computed_region_*`) are
excluded from the content hash and tracked separately. A change is counted only when the
hash of the published fields moves. Churn is preserved as a constant-space digest per
snapshot, so it remains countable without being attributable — churn is worth counting,
never worth blaming on individual records.

**Additional control.** Because the same timestamp means different things at different
portals, every source is classified before its changes are interpreted:

- `republish` — table rebuilt, internal ids regenerate; timestamps carry no editorial signal
- `bulk_touch` — ids stable but every row rewritten per run; timestamps saturated
- `incremental` — rows updated selectively; timestamps are meaningful evidence
- `static` — the closed past is not being touched

Only for `incremental` publishers does a platform timestamp constitute evidence of
anything. Interpreting first and characterising afterwards inverts the conclusion.

### Confounder 2 — Identity churn

**Observed.** A San Francisco dataset produced 1,417 deletions between two consecutive
observations — alongside 1,417 insertions. Row count, stratum population, and total
closed-past count were all unchanged.

**Naive reading.** Fourteen hundred records were deleted.

**Actual cause.** That source's natural key collides, so it falls back to keying records
by their content. Under content identity a record's identity *is* its content, so any
edit presents as the old record vanishing and a new one appearing. Deletion, insertion,
and revision become formally indistinguishable.

**Control.** Departures and arrivals are reconciled before anything is reported:

- Under content identity, departures and arrivals are reclassified as `identity_churn`
  and no claim of deletion is made at all.
- Under a stable key, departures matched one-for-one by arrivals with no net change in
  population are also `identity_churn` — the keys were rewritten, not the records
  removed. Only the unmatched excess survives as a candidate deletion.

Applying this reduced reported deletions across the archive from 1,535 to 118.

### Confounder 3 — Bookkeeping columns published as data

**Observed.** A Los Angeles permits dataset produced revisions reading
`3 fields changed: refresh_time, status_date, status_desc`.

**Naive reading.** A permit's status was altered.

**Actual cause.** `refresh_time` is the publisher's ETL clock, published as an ordinary
column. It moves on every pipeline run regardless of whether anything changed.

**Control.** Column names are classified against an anchored pattern set covering
write-timestamps (`last_updated`, `date_modified`, `updated_datetime`,
`last_updated_date_time`), warehouse bookkeeping (`data_as_of`, `data_loaded_at`,
`refresh_time`, `etl_date`, `snapshot_date`), authorship of the write (`modified_by`),
and platform surrogate keys (`row_version`, `record_hash`). These are excluded from the
content hash and diffed separately.

**How the pattern set was built.** Not from imagination. Every distinct column across
the watchlist — 2,165 of them — was classified and the results audited for bookkeeping-
shaped names still being hashed as facts. That audit is what surfaced `data_updated_at`,
`updated_datetime`, `violation_last_modified_date`, and `modified_by`.

The audit equally constrains the patterns in the other direction. `applicant_last_name`,
`homicide_victim_last_name`, `last_objection_date`, `last_doc_date`, and
`issued_in_last_30_days` are real fields that a careless "contains *last* or *update*"
rule would silently stop watching. Over-exclusion hides genuine revisions and leaves no
trace that it did so, which makes it the more dangerous error of the two. Every pattern
is anchored end-to-end and matches only whole column names.

---

## What is actually measured

### Two resolutions

**The stratum.** Every record inside a fixed past window, hashed field by field. The
window sits at least a year back, because recent data legitimately churns as cases close
and late reports arrive; a year on, movement demands an explanation. High resolution,
but a keyhole.

**Aggregates.** Row count over the entire closed past, plus categorical tallies. Three
requests regardless of whether the dataset holds ten thousand rows or ten million. Wide
angle, but only in outline — it catches a mass deletion or a reclassification anywhere in
the dataset, at the cost of saying nothing about which record.

Neither alone suffices. A single altered value outside the stratum is invisible to the
aggregates; a mass deletion outside the stratum is invisible to the stratum.

### Identity

Record identity uses each dataset's natural key (`case_number`, `unique_key`,
`permit_number`), verified unique against a live sample at discovery and re-checked on
every collection. It deliberately does not use the platform's internal row id, which
regenerates on reload.

When a key is found to collide during collection — Chicago's building violations repeat
`nov_number` across 1,298 of 2,080 records, because one notice carries several violations
— the source falls back to content identity for that observation, and the resulting loss
of capability is stated rather than hidden: such a source can evidence appends and
disappearances but not in-place edits.

### Coordinated versus isolated revision

Five thousand records changing `BURGLARY` to `Burglary` in one sweep is a formatting
migration. One record changing classification alone is a different kind of event. Both
move the content hash identically.

Revisions sharing an identical field-level transformation are therefore grouped: at 25 or
more, they are reclassified as `coordinated_revision` and de-emphasised. Conversely, a
revision whose touched-field shape has no more than three counterparts in the same sweep
is scored *up* — an isolated edit inside an otherwise quiet sweep is the strongest signal
available, because nothing systematic explains it.

The threshold of 25 is a heuristic, not a derived constant, and a migration affecting
twenty records will read as twenty isolated edits.

### Significance

Scored by the heaviest field touched rather than the count of fields, because one altered
classification matters more than six shifted coordinates. Fields encoding what an event
*was* (type, classification, charge), its disposition, or when it happened carry the most
weight; geographic coordinates carry the least, since they are frequently refined by
re-geocoding.

---

## Tamper evidence

Each observation commits to a Merkle root over `(record identity, content hash)` pairs,
sorted so the root does not depend on arrival order.

Two details matter and are easy to get wrong:

- **Domain separation.** Leaves and interior nodes are hashed with different prefixes, so
  an interior node cannot be presented as a leaf.
- **Odd nodes are promoted, not duplicated.** Duplicating a final node lets two different
  record sets produce the same root, which would defeat the purpose of publishing it.

Each root is chained to its predecessor, so altering any past observation changes every
hash that follows it.

**The root must commit to exactly the record set the archive can reproduce.** This
invariant was violated in an early build: storage is keyed on `(snapshot, row_uid)`, so
colliding identities were silently collapsed on write while the root had been computed
before the collapse. Every inclusion proof against those snapshots failed. It was caught
by a verifier written to re-derive every hash rather than trust any stored one, and it is
now enforced by regression test. Had the verifier not existed, the project would have
shipped unverifiable proofs — the worst possible failure for a tool whose entire claim is
tamper evidence.

Verification is a first-class operation, not a debug aid:

```bash
python -m palimpsest.verify
```

This recomputes every Merkle root from stored records, replays every chain from genesis,
rehashes stored payloads against their content keys, and re-derives inclusion proofs —
including negative controls confirming that tampered leaves are rejected.

---

## Limitations

1. **No visibility before collection began.** The archive starts when it starts. For
   incremental publishers it can surface records the publisher itself reports as
   modified, but that is the publisher's assertion about the *fact* of a change and says
   nothing about the prior value.

2. **Absence in the stratum is not proof of deletion.** A record can leave the window
   because it was removed, or because its event date was edited so it now falls outside.

3. **Aggregates can hide offsetting changes.** A deletion and an insertion in the same
   closed window net to zero outside the stratum.

4. **Key uniqueness is verified by sampling.** A key unique in the sample and duplicated
   elsewhere remains a source of error, though collisions are detected at collection and
   the affected source is downgraded.

5. **Field classification is frozen for the life of an archive.** Content hashes commit
   to a specific semantic projection; changing the classification makes prior snapshots
   incomparable and requires restarting collection. This happened twice during
   development and is why the 2,165-column audit was done before the final run.

6. **Coverage is a convenience sample.** Ten large US Socrata portals were scanned; seven
   yielded usable datasets. Baltimore, Nashville, and Kansas City produced none that met
   the criteria. Legistar's public API returned HTTP 500 throughout development, so
   meeting agendas are not covered. No claim is made that these cities are representative.

7. **We observe what the API returns.** A CDN serving a stale response is not
   distinguishable from a publisher reverting a value. Portal freshness headers
   (`Last-Modified`, `ETag`, `X-SODA2-Truth-Last-Modified`) are archived with every
   observation so this can be checked rather than assumed.

---

## Conduct

Public endpoints only, one request per 700ms, identifying User-Agent, `Retry-After`
honoured, exponential backoff on error. No authentication, no writes, no form
submissions, no access to anything not served to any member of the public who asks.
Failed observations are archived alongside successful ones, because a gap in the record
that is not itself recorded is indistinguishable from a period of no change.
