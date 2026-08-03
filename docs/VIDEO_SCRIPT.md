# Palimpsest — video script

**Target: 3:00.** Screen recording with voiceover. No talking head needed; if you want one,
use it only for the first and last lines.

Two rules that matter more than polish:

- **Never show a number you don't immediately explain.** The whole thesis is that raw
  numbers in this domain lie.
- **Show the terminal at least twice.** Judges include people who have seen a hundred
  dashboards over a fake dataset. Live output is the differentiator.

---

## 0:00 – 0:22 · The hook

> **VISUAL** — Slow scroll of a city open-data portal page. Ordinary. Boring.
> Then hard cut to black with white type: **"There is no changelog for the public record."**

**VO:**
> Every city publishes open data now. Crime, arrests, permits, inspections.
> And everyone treats it as a record of what happened.
>
> It isn't. Records get revised. Records get removed. Records get inserted into periods
> that already closed. And not one of these portals keeps a version history.
>
> So if a city quietly changed a record last night, there is no query you could run today
> that would tell you.

---

## 0:22 – 0:55 · The trap

> **VISUAL** — Terminal. Run the probe live. Let the number land on screen.
> Then overlay a single record showing `date: 2001-04-01`, `:created_at: yesterday`.

**VO:**
> Here's the obvious version of this project. Ask Chicago how many old crime records it
> changed recently.
>
> Ninety-eight percent. Nearly eight million records, going back to 2001.
>
> That's a headline. It's also completely false.

> **VISUAL** — Highlight the `:created_at` field.

**VO:**
> Chicago rebuilds this whole table on a schedule. Every row gets a new ID and a new
> timestamp, whether or not anything changed. This record describes an arrest from April
> 2001 — and the row was created yesterday.
>
> Nothing was edited. The pipeline just ran.
>
> I nearly shipped that number. Everything in this project is the work of not shipping it.

---

## 0:55 – 1:30 · What it actually does

> **VISUAL** — The site. Scroll the standing figures. Land on **89.8%**.

**VO:**
> Palimpsest watches 112 datasets across 7 cities and archives what they say — every two
> hours, for a fixed window at least a year in the past. That window is closed. Nothing in
> it should ever change.
>
> Every observation is hashed into a Merkle root, and every root is chained to the one
> before it. So the archive can prove its own memory hasn't been altered — which is
> exactly the property the portals don't have.
>
> And then it does the hard part.

> **VISUAL** — The control ladder table, scrolling down row by row.

**VO:**
> A blind pass over this archive reports seventy-six thousand changes.
> After controlling for how publishers actually operate — six thousand seven hundred.
>
> Ninety-one percent of apparent change was machinery, not the record.
> That gap is the finding.

---

## 1:30 – 2:00 · The one that caught us

> **VISUAL** — A ledger card showing an ordering change: `was: 245A1, 245A4, 242` /
> `now: 242, 245A1, 245A4`. Let it sit.

**VO:**
> Here's how easy this is to get wrong. I know, because I got it wrong.
>
> A Chicago arrest record showed its lead charge changing from battery to retail theft.
> I wrote it up as the headline finding.
>
> It's a re-sort. The same two charges, in the opposite order. The arrest is charged with
> exactly what it was charged with before — the city just reordered the columns.
>
> Three hundred and twenty-eight findings were that. They're now their own category, and
> the tool doesn't claim a reclassification it can't prove.

---

## 2:00 – 2:25 · What actually survived

> **VISUAL** — Seattle Crisis Data schema-drift entry. Then two more findings, ~5s each.

**VO:**
> So here's what's left after all of that.
>
> A column in Seattle's police crisis data — whether a crisis intervention officer was
> requested at a mental health call — that simply stopped existing.
>
> An Austin construction project whose recorded floor area went from four hundred and
> sixty-three square feet to two thousand five hundred and fifty-one.
>
> And a closed complaint window in Austin that keeps shrinking: fifty-one thousand
> twenty-five, then nine hundred thirty-three, then seven hundred fifty-eight.

> **VISUAL** — The proof panel: record ID, content hash, Merkle root, sibling hashes.

**VO:**
> Each comes with a cryptographic proof that this archive saw that value, on that date,
> and hasn't altered its own record of it since.

---

## 2:25 – 2:42 · The part I'd score highest

> **VISUAL** — Scroll to a source card marked **withdrawn**. Rest on it.

**VO:**
> This one matters more than the findings.
>
> Two datasets have keys that look fine — unique, never empty — but silently point at a
> different record each time the city re-sorts its rows. Any per-record claim about them
> would be nonsense.
>
> So Palimpsest withdraws all of them, and says so, right on the page. A tool that measures
> the public record has to be honest about what it can't measure.

---

## 2:42 – 3:00 · Close

> **VISUAL** — Terminal: run `python -m palimpsest.verify`. Let it print
> `RESULT: archive intact`. Hold.

**VO:**
> Every claim here is reproducible. Both ends of that eighty-nine percent are written into
> the archive by the analysis itself, so you can check the ratio instead of trusting me.
>
> Cities aren't required to remember what they published.
> Now something does.

> **VISUAL** — Cut to black. URL + repo link.

---

## Shot checklist

| # | Shot | Where |
|---|---|---|
| 1 | Portal page, ordinary scroll | any city open-data page |
| 2 | Terminal: the 98% probe | `scratchpad/probe2.py` output, or re-run live |
| 3 | Single record, `:created_at` = yesterday vs `date` = 2001 | same output |
| 4 | Standing figures, land on 89.8% | site header |
| 5 | Control ladder table | "What the controls remove" |
| 6 | Chicago arrest charge swap | ledger, record `30477683` |
| 7 | Fee / sqft / vanished column | ledger + schema drift entries |
| 8 | Inclusion proof panel | "Verify" section |
| 9 | Withdrawn source card | sources table |
| 10 | `palimpsest.verify` → `archive intact` | terminal |

## Recording notes

- Record at 1280×800. Zoom to ~150% for the field-level diffs — the before/after values
  are the payload and they must be legible on a phone.
- Kill the browser chrome and any bookmarks bar.
- The two charge lines in shot 6 need a full 4 seconds. Let people read it.
- Keep the pace conversational. The material is strong enough that overselling it makes it
  sound weaker.
