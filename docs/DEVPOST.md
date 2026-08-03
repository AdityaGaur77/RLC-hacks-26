# Palimpsest — Devpost submission copy

Paste-ready. Figures are live as of the final collection run and are reproducible from
the archive with `scripts/analyse.ps1`.

---

## Tagline

**Government publishes as though the web were permanent. It isn't. Palimpsest is a
tamper-evident archive that watches 112 municipal datasets, proves when the past changes
underneath them — and refuses to call it a scandal when it isn't.**

---

## Inspiration

Open data portals are treated as an append-only record of what happened. They are not.
Records get revised, removed, and inserted into periods that had already closed — and no
portal exposes a version history. There is no changelog for the public record, which is
why retroactive change isn't merely unnoticed: it is *structurally invisible*. If a city
quietly reclassified an arrest last night, there is no query you could run today that
would tell you.

The obvious build is a diff tool. That build is wrong, and finding out why became the
project.

The first thing we measured looked like a five-alarm fire. Chicago's crime dataset
reports that **97.8% of its records describing events before 2024 were modified within
the last thirty days** — spread evenly across every year back to 2001. Read literally, a
city rewrote eight million crime records.

It's false. Chicago drops and reloads the entire table on a schedule, so every row gets a
fresh identifier and timestamp while not one published fact changes. Records describing
April 2001 carry a creation timestamp of *yesterday*.

We nearly shipped that number. Everything after is the work of not shipping it.

---

## What it does

Palimpsest continuously observes 112 civic datasets across 7 US cities — crime, arrests,
permits, food inspections, 311, campaign finance, code complaints — and detects when
already-published facts change.

**It archives.** Every 2 hours it fetches a *frozen stratum* from each dataset: every
record inside a fixed window at least a year in the past. That window is closed. Its
contents should never legitimately change, so any movement demands an explanation.

**It proves.** Each observation commits to a Merkle root over the records it saw, and
each root is chained to its predecessor. The archive can therefore prove its own account
of the past hasn't been altered — the exact property we fault the portals for lacking. It
can produce an inclusion proof that a specific record held a specific value on a specific
date, and that proof can be checked by anyone against the published root.

**It distinguishes.** This is the substance. Apparent change in open data is dominated by
publishing machinery, and telling machinery from editing is most of the engineering.

Over 36 sweeps and 547,233 record observations:

> **A blind pass reports 67,275 changes. After controlling for publishing mechanism,
> 6,832 remain. 89.8% of apparent change was machinery, not the record.**

**It reports what it cannot know.** Two datasets were caught with keys that don't
identify stable entities. Rather than publish thousands of confident falsehoods about
them, Palimpsest withdraws every per-record claim for those sources and says so on the
page.

### What it actually found

**An arrest's lead charge was swapped.** Chicago arrest record `30477683`:

```
charge_1_description:  BATTERY - CAUSE BODILY HARM    →  RETAIL THEFT/DISP MERCH/<$300
charge_1_statute:      720 ILCS 5.0/12-3-A-1          →  720 ILCS 5.0/16-25-A-1
charge_2_description:  RETAIL THEFT/DISP MERCH/<$300  →  BATTERY - CAUSE BODILY HARM
```

The same two charges, in the opposite order. Position one is the lead charge — the one
that characterises the arrest. A violent offence and a petty property offence traded
places, silently, with no version history to record it.

**A permit fee rose retroactively.** Chicago transportation permit `2681619323365`:
`totalfees: 2955.13 → 3655.13`, +$700, with the application end date pushed a month.

**A construction project's recorded size changed 5.5×.** Austin permit `2025-015600 EP`:
`total_new_add_sqft: 463 → 2551`.

**A column measuring police accountability disappeared.** Seattle's Crisis Data dataset
dropped `cit_officer_requested` — whether a Crisis Intervention Team officer was
requested — between two observations.

**Closed windows are shrinking.** Austin Code Complaint Cases lost records from an
already-closed period across consecutive sweeps: 51,025 → 50,933 → 50,758 → 50,722.

**And publishers admit to far more.** Across 39 datasets whose timestamps aren't
saturated, publishers report rewriting **1,719,786 closed-period records in 30 days**,
reaching back to a Montgomery County service request from **1994**.

---

## How we built it

Python 3.11, no third-party dependencies. SQLite for the archive, SHA-256 Merkle trees
for the proofs, a static HTML page for the evidence bundle. ~2,900 lines.

- **Collector** — polite, rate-limited SODA client (0.7s between requests, identifies
  itself, read-only, public endpoints only). Two resolutions per source: the stratum for
  detail, plus aggregate counts over the entire closed past for the wide angle.
- **Archive** — append-only, content-addressed. A record unchanged across a hundred
  sweeps costs one row of storage, not a hundred; payloads are deflated. 547,233
  observations compress into a repository you can actually clone, which matters because
  an archive nobody can obtain is not evidence of anything.
- **Pipeline characterisation** — before interpreting *any* change, classify how each
  publisher operates: `republish`, `bulk_touch`, `incremental`, `static`.
- **Volatility measurement** — learn which columns each publisher recomputes wholesale,
  from observed behaviour rather than from column names.
- **Diff engine** — classify each change and score its significance.
- **Verification** — a first-class command, not a debug aid.

---

## Challenges we ran into

Seven distinct mechanisms masquerade as editing. Each was found by investigating a result
that looked like a scandal, and — this is the part that mattered — **each defeated the
controls built for the previous ones.**

1. **Provenance churn.** The table is reloaded; every row is "modified". *Control:* hash
   declared semantic fields only, and characterise the publisher before interpreting it.

2. **Identity churn.** A San Francisco dataset showed 1,417 deletions — alongside 1,417
   insertions, with population unchanged. Content-keyed sources render every edit as a
   death and a birth. *Control:* reconcile departures against arrivals. Reported
   deletions fell from 1,535 to 118.

3. **Bookkeeping columns.** `refresh_time` published as an ordinary column. *Control:* an
   anchored pattern set, audited against all 2,165 columns in the watchlist — which also
   confirmed that real fields like `applicant_last_name` and `last_objection_date`
   survive.

4. **Recomputed columns no pattern can name.** Austin's 311 data reported all 2,507
   stratum records revised on every sweep, forever. The delta was always
   `sr_age_days: 417 → 418` — the age of a service request, recomputed at export. It is
   not a fact; it is a clock. Nothing in that name marks it as bookkeeping, and no pattern
   set can anticipate every publisher's vocabulary. *Control:* stop guessing from names
   and **measure behaviour** — a column moving in ≥90% of revised records across ≥3 sweeps
   is being recomputed. This inverted the architecture: the analysis now runs twice, and
   the first pass exists only to characterise the publisher.

5. **Keys that are unique but not stable.** Austin campaign finance keys are *ordinals
   within a filing*. Re-sorting re-binds them, so donors, amounts and dates appear to
   change wholesale — and the amounts visibly trade places between records. Such keys are
   unique and non-null and pass every structural test. *Control:* detect by breadth —
   recomputation touches a column or two; an entire record changing means the key moved.
   Per-record claims are withdrawn for those sources.

   **This one initially looked refuted.** The natural test — *is the record population
   preserved?* — returned 0%, apparently confirming genuine edits. It was wrong because
   confounder 4 was operating simultaneously, so no content hash matched under any
   permutation. The two had to be removed in the right order for either to become visible.

6. **Cases advancing through their workflow.** A permit becoming `Final`, and a blank
   completion date being filled, is real movement but not the past being rewritten.
   *Control:* the test is what *kind* of move occurred — filling a blank and advancing a
   status are progression; replacing one non-empty value with a different one is revision.

7. **Schema migrations.** A newly added column differs in every record, absent → present.
   SF's parking citations showed 4,083 "revisions" in the sweep that added seven columns.
   *Control:* exclude added and removed columns from per-record deltas; the schema change
   is one finding.

Two more worth naming. A single Austin endpoint consumed **29 hours** fetching 2,170 rows
— a socket timeout applies per read, so a server that trickles a body without finishing it
never trips one; one source stalled an entire sweep. And the two-pass analysis was briefly
**circular**: the probe pass inherited the previous run's volatility measurement, so it
filtered out exactly the movement it existed to observe. We caught it because a clean run
suddenly found *zero* recomputed columns where the previous run found twelve.

---

## Accomplishments we're proud of

**We didn't publish the exciting number.** Four separate times a result looked like a
scandal and turned out to be plumbing. Each time the answer was to build a control rather
than a headline.

**Verification caught a real defect in our own archive.** Colliding natural keys were
being collapsed on write while the Merkle root had been computed *before* the collapse —
so every inclusion proof against those snapshots failed. The proofs are only worth
something because they can fail, and they did.

**We corrected our own overclaim.** An early version counted `static` publishers as
"uninterpretable", producing a headline of 80%. That's wrong — a publisher touching
nothing has the *cleanest* timestamps, because an edit would have nowhere to hide.
Saturation destroys signal, not activity. The honest figure is 49%, and shipping 80%
would have been precisely the error the project condemns.

**The claims are checkable.** Both endpoints of the 89.8% ratio are written into the
archive by the analysis itself, so it can be verified against the evidence rather than
taken on trust. The site ships worked inclusion proofs with their sibling hashes.

---

## What we learned

That the interesting problem was not detection. Detecting change is a hash comparison.
The problem is that **every naive signal in this domain is dominated by publishing
mechanics**, and each control you build reveals the next confounder underneath it.

And that a measurement instrument has to be honest about what it cannot measure. Two
datasets are shown on the site with all per-record claims explicitly withdrawn. That's a
worse-looking result and a better tool.

---

## What's next

Extend beyond Socrata to Legistar meeting agendas — items that vanish the night before a
vote. Publish the archive as a citable, timestamped release so a researcher can point at
what a record said on a given date. Alerting for a watched record. And a public
transparency ranking of publishers by whether their own data can evidence its own edits.

---

## Built with

`python` · `sqlite` · `sha-256 merkle trees` · `socrata soda api` · `html/css/js` ·
`github actions` · `vercel`

No third-party Python dependencies.

---

## AI disclosure

Required by the rules, and stated plainly.

This project was built in collaboration with **Claude Opus 5 (Anthropic)** used as a
coding agent inside Claude Code. AI assistance covered architecture discussion, the
majority of implementation, methodology documentation, and this write-up. The direction,
project selection, review, and all judgment calls about what to publish were the author's.

Specifically worth noting, because it cuts both ways: several of the confounders
documented above were surfaced *by the AI agent investigating its own suspicious results
rather than reporting them* — and one overclaim (the 80% figure) was also introduced by
that agent before being caught and corrected. Both are disclosed.

All data is public, unauthenticated municipal open data retrieved read-only from official
city portals. No credentials, no scraping of protected resources, no personal data
compiled beyond what the cities themselves publish. Every finding links to its source
dataset so it can be checked independently.
