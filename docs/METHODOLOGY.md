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

### Confounder 4 — Recomputed columns that no pattern can name

**Observed.** Austin's 311 dataset reported all 2,507 records in the stratum revised, on
every sweep, indefinitely. The delta was always the same:

```
sr_age_days: 417 -> 418
```

San Francisco's parking citations reported 4,083 records revised at once, with
`date_added` moving in 100% of them and `the_geom`, `supervisor_districts`,
`analysis_neighborhood`, `latitude` and `longitude` moving in 98.2%.

**Naive reading.** Two cities are editing their entire published record continuously.

**Actual cause.** `sr_age_days` is the age of a service request in days, computed at
export time. It is not a fact about a request; it is a clock, and it advances for every
record every day forever. `date_added` is when a row entered the current export.
`the_geom` and `supervisor_districts` are spatial joins recomputed on publication —
the same class as Socrata's `:@computed_region_*`, but without the prefix that makes
that class recognisable.

**Why Confounder 3's control was insufficient.** Nothing in the names `sr_age_days` or
`date_added` marks them as bookkeeping. A pattern set is a hypothesis about naming
conventions, and these publishers simply do not share it. Extending the patterns to
catch them would require guessing every future publisher's vocabulary, and each guess
that overreaches silently stops watching a real field.

**Control.** Stop guessing from names; measure behaviour. A column that changes in
≥90% of revised records across ≥3 separate sweeps is not carrying information about
individual records — it is being recomputed wholesale. This is a property of what the
archive *observed*, and the archive is precisely the instrument for observing it.
Such columns are excluded from revision claims, and a record whose entire delta
consists of them is reclassified as mechanism rather than revision.

This inverts the usual dependency: the analysis must run twice. The first pass exists
only to reveal how each publisher behaves; the second pass is the one that is reported.

### Confounder 5 — Keys that are unique but not stable

**Observed.** Austin's campaign finance contributions showed revisions in which the
donor name, amount, date, employer, occupation and address all changed at once:

```
record R20250101100720473-A00070
  donor:               Krumme, Gregg  ->  Burns, David
  contribution_amount: 450            ->  52.95
record R20250101100720473-A00165
  donor:               Mosser, Christopher -> Russell, James
  contribution_amount: 52.95          ->  450
```

Seattle's permit review data changed `permitnum` itself in 99.5% of apparent revisions.

**Naive reading.** Austin altered thousands of campaign contribution records — donors,
amounts and dates — and Seattle rewrote its permit history.

**Actual cause.** The key `...-A00070` is an *ordinal within a filing*, not an
identifier of a contribution. When the publisher re-sorts rows, sequence numbers
re-bind to different donors. Note the amounts trading places between the two records
above: nothing was edited, the labels moved. A permit number that changes is likewise
not identifying a permit.

**Why the existing identity controls did not catch it.** These keys are unique and
non-null, so they pass every structural test. Confounder 2's control compares
departures against arrivals, but no record departs — the same key set is present
throughout, pointing at different things.

**Why it initially looked refuted.** The obvious test is whether the multiset of
record contents is preserved: under pure re-labelling the population is unchanged.
That test returned 0% preservation, appearing to confirm genuine editing. It was
wrong because Confounder 4 was operating simultaneously — recomputed columns meant no
record's content hash matched anything, permutation or not. **The two confounders had
to be removed in the right order for either to be visible.**

**Control.** Once recomputed columns are set aside, a source where most records still
differ on a typical sweep is not tracking stable entities. Separately, when four or
more distinct columns all behave as "recomputed", that breadth is itself the signal:
recomputation touches a column or two, whereas an entire record changing means the key
has re-bound. For such sources, **all per-record claims are withdrawn** and stated as
withdrawn. Aggregate findings survive, because counts over a closed window do not
depend on knowing which record is which.

### Confounder 6 — Cases advancing through their own workflow

**Observed.** Austin's construction permits produced revisions such as
`status_current: Active -> Final` with `completed_date: (empty) -> 2026-07-27`, and
`status_current: Active -> Expired`.

**Naive reading.** Permit records are being rewritten.

**Actual cause.** An open case legitimately changes. A permit becomes Final, and the
completion date it never had is filled in. Nothing previously asserted has been
contradicted.

**Control.** The distinction is not *which* field moved but *what kind of move* it was.
Filling a blank, and advancing a status field, are progression. Replacing one non-empty
value with a different non-empty value is revision — the record's earlier account of
the world has been withdrawn and replaced, and unless somebody kept a copy, silently.

In the same dataset and the same sweep, this separates

```
status_current:      Active -> Final          progression
completed_date:     (empty) -> 2026-07-27     progression
```

from

```
total_new_add_sqft:      463 -> 2551          revision
```

A construction project's recorded floor area changing five-fold is the finding. The
permit becoming Final is not, and reporting both at equal weight would bury the first
under the second.

### Confounder 7 — The same values in a different order

**Observed.** San Francisco's District Attorney case resolutions produced revisions to
the charges filed against a defendant:

```
list_of_filed_charges
  was: 245A1/M/0, 245A4/M/0, 242/M/0
  now: 242/M/0, 245A1/M/0, 245A4/M/0
```

And a Chicago arrest record, `30477683`, appeared to have its lead charge changed:

```
charge_1_description:  BATTERY - CAUSE BODILY HARM    ->  RETAIL THEFT/DISP MERCH/<$300
charge_2_description:  RETAIL THEFT/DISP MERCH/<$300  ->  BATTERY - CAUSE BODILY HARM
```

**Naive reading.** A prosecutor reordered the charges against a defendant; a violent
offence and a petty property offence exchanged rank on an arrest record.

**This project published exactly that reading before catching it.** The Chicago record was
written up as the headline finding — "the lead charge on an arrest was swapped" — and it
was wrong. The multiset of values before the change is identical to the multiset after.
Six fields differ and the arrest is charged with precisely what it was charged with
before.

**Actual cause.** Two shapes of the same thing. A multi-valued cell re-sorted, and values
permuted across parallel columns (`charge_1_*`, `charge_2_*`).

**Why the earlier controls did not catch it.** Every one of these is a genuine
replacement of a non-empty value by a different non-empty value — which is the exact test
Confounder 6 uses to separate revision from progression, and it passes. The content hash
legitimately changed. Nothing about a single record, examined on its own terms, reveals
the problem; it is only visible when the record's values are compared *as a set*.

**Control.** Parse each changed value on common delimiters and compare multisets, both
within a field and across all changed fields of the record. If the record holds the same
values arranged differently, it is reported as an ordering change, not a revision.

**What is deliberately not claimed.** Whether position encodes primacy — whether
`charge_1` really is the lead charge — is a question about a publisher's internal
conventions that the data cannot answer. A deliberate re-ranking and an arbitrary re-sort
are indistinguishable here. Since the alarming reading of an ambiguous signal is exactly
what this project exists to refuse, these are surfaced as their own low-significance kind
rather than counted as facts being rewritten. **332 changes were reclassified this way.**

### Confounder 8 — Records that come back

**Observed.** Chicago's *Violence Reduction — Victims of Homicides and Non-Fatal
Shootings* produced 166 deletions, each reported at the maximum significance this
project assigns to anything.

**Naive reading.** A city removed 166 homicide and shooting victim records.

**Actual cause.** They were all present again in a later observation. The publisher
reloads its table, and a table sampled mid-reload is missing rows that were never
removed.

**Why the earlier controls did not catch it.** Confounder 2 reconciles departures
against arrivals *within a single pair of observations*. These records depart and return
several sweeps later, so no arrival is available to pair them with at the moment they
vanish. Two adjacent snapshots simply cannot distinguish a removal from a gap.

**Control.** Deletion is the strongest claim this project makes, so it must survive the
rest of the archive rather than one comparison. A record is only reported as deleted if
it is absent from **every** subsequent observation. Records that reappear are reported as
transient absence.

**Effect.** Reported deletions fell from 1,353 to 1,187 — and the 166 removed were
concentrated almost entirely in a single dataset about homicide victims, which is exactly
where a false claim would have done the most damage.

### Confounder 9 — Zero as a placeholder, and pointers to "the latest"

**Observed.** Two recurring shapes, both reported as replaced facts at maximum
significance:

```
fee:             0 -> 149        invoice_amount: 0 -> 149
last_doc:        CEQA – B -> Withdrawn
last_doc_date:   2026-07-27 -> 2026-07-30
```

**Naive reading.** A fee was imposed retroactively; a filed document was rewritten.

**Actual cause.** Zero is how these publishers spell "not set yet" on an amount column —
both columns leaving zero together is an invoice being raised. And `last_doc` does not
state a historical fact; it points at the most recent document, and advances by design
when a new one is filed.

**Control.** Zero is treated as blank in the *earlier* position only. The asymmetry is
deliberate: `0 → 149` is an invoice being raised, but `149 → 0` is a waiver, and that is
a real change to the record. Separately, fields named `last_*`, `latest_*`,
`most_recent_*` and `current_*` are recognised as pointers rather than assertions — with
a negative lookahead so that `last_name`, a person's surname, is not swallowed by that
rule.

### Confounder 10 — Fields that are other fields' arithmetic

**Observed.** A San Francisco permit reported three values replaced at once:

```
completed_date          2026-06-30T12:13:25 -> 2026-07-29T09:57:19
submit_to_complete_biz                   83 -> 103
submit_to_complete_cal                  119 -> 148
```

And a District Attorney record appeared to show a charge being downgraded:

```
crime_type              Willful Homicide (Att.) -> Assault
list_of_booked_charges  664/187A,205,245A1 -> 205,245A1,664/187A
```

**Naive reading.** Three facts about a permit were rewritten. A prosecutor
reclassified an attempted homicide as an assault.

**Actual cause.** `submit_to_complete_biz` and `submit_to_complete_cal` are the interval
between submission and completion in business and calendar days. They are not additional
facts; they are that date, subtracted. One change was reported as three.

The second is worse. The charge list was re-sorted — the same three charges — and
`crime_type` names whichever charge sits in first position. Read as an independent field
it alleges a prosecutorial decision that never happened.

**Why the ordering control did not catch it.** Confounder 7 requires the record to hold
the same values arranged differently. Here `crime_type` genuinely holds a *different*
value afterwards, so the multiset does not match and the record fails that test. The
re-sort is real but only explains one of the two fields.

**Control.** A field that has essentially never moved on its own is not independent
evidence. For each source, the conditional P(driver moved | field moved) is measured
across the archive; at ≥97% over at least 8 movements, the field is recorded as following
its driver. Such fields are discounted from the change count and the significance score —
but only when the field they follow actually moved in the same delta — and they remain
visible, labelled with what they follow. Separately, a re-sorted multi-valued field
anywhere in a change now discounts the whole change, because a re-sort is a mechanical
explanation for whatever moved with it.

**Detection has to be measured, not named.** Nothing about `crime_type` marks it as
derived. Only the archive's own record of what moves with what reveals it.

### Confounder 11 — Schema migrations

**Observed.** San Francisco's parking citations showed 4,083 simultaneous revisions in
the same snapshot pair that added the columns `analysis_neighborhood`, `data_as_of`,
`data_loaded_at` and `latitude`.

**Actual cause.** A column that did not previously exist differs in *every* record, from
absent to present.

**Control.** Columns added or removed between two observations are excluded from
per-record deltas. The schema change is a finding, reported once, rather than a
revision of every record simultaneously.

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
