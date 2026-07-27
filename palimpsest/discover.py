"""Build the watchlist.

For every candidate dataset we must answer three questions before it can be
watched at all:

1. Which column carries the *event* date? Not when the row was written — when
   the thing described actually happened. This is what makes a window "frozen".
2. What is the dataset's natural key? The platform's internal row id is useless
   here: publishers routinely reload tables, which regenerates every internal id
   while the underlying facts stay put.
3. Which past window should we watch? Small enough to fetch cheaply and often,
   large enough that a change landing in it is not a fluke.

A dataset that fails any of these is skipped rather than guessed at. A watchlist
of two hundred datasets we can reason about is worth more than a thousand we
cannot.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from .socrata import SocrataClient, SocrataError

log = logging.getLogger("palimpsest.discover")

TIMESTAMP_TYPES = {
    "calendar_date", "date", "floating_timestamp", "fixed_timestamp", "datetime",
}

# Names suggesting the column records when the described event occurred.
_EVENT_DATE_HINTS = [
    (re.compile(r"^(incident|occurr?ed|offense|crime|violation|collision|crash)", re.I), 100),
    (re.compile(r"(^|_)(date_?of_?(incident|occurrence|service|event))", re.I), 95),
    (re.compile(r"^(reported|report_?date)", re.I), 85),
    (re.compile(r"^(created|requested|opened|filed|received|application)", re.I), 80),
    (re.compile(r"^(issue|issued|permit_?date|approval)", re.I), 75),
    (re.compile(r"^(inspection|visit|service)_?date", re.I), 75),
    (re.compile(r"^date$", re.I), 70),
    (re.compile(r"(^|_)date$", re.I), 40),
    (re.compile(r"(^|_)datetime$", re.I), 40),
]

# Names suggesting the column is a natural identifier.
_KEY_HINTS = [
    (re.compile(r"^(case|complaint|incident|service_?request|permit|record)_?(number|no|id|num)$", re.I), 100),
    (re.compile(r"^unique_?key$", re.I), 100),
    (re.compile(r"^(case|incident|permit|licence|license|application)_?", re.I), 70),
    (re.compile(r"_(number|no|id)$", re.I), 55),
    (re.compile(r"^id$", re.I), 50),
]

# Columns worth tallying: low-cardinality categoricals whose distribution moving
# is itself a story (a reclassification shows up here before anywhere else).
_DIMENSION_HINTS = re.compile(
    r"(type|category|status|classification|description|agency|department|"
    r"disposition|result|outcome|borough|district|ward|neighborhood|beat)",
    re.I,
)

# Datasets whose subject matter makes a silent revision consequential.
_CIVIC_WEIGHT = [
    (re.compile(r"(crime|arrest|police|shooting|homicide|use.of.force|misconduct|stop)", re.I), 100),
    (re.compile(r"(311|service.request|complaint)", re.I), 80),
    (re.compile(r"(permit|zoning|building|construction|violation|inspection|code.enforcement)", re.I), 75),
    (re.compile(r"(eviction|housing|homeless|shelter)", re.I), 75),
    (re.compile(r"(health|restaurant|food|lead|water|air.quality|environment)", re.I), 70),
    (re.compile(r"(budget|spend|contract|procure|payroll|salary|lobby|campaign|ethic)", re.I), 70),
    (re.compile(r"(traffic|collision|crash|speed|red.light|citation|towed?)", re.I), 60),
    (re.compile(r"(election|vote|voter|precinct)", re.I), 60),
    (re.compile(r"(school|education|student)", re.I), 50),
]

PORTALS = [
    ("data.cityofchicago.org", "Chicago, IL"),
    ("data.cityofnewyork.us", "New York, NY"),
    ("data.sfgov.org", "San Francisco, CA"),
    ("data.lacity.org", "Los Angeles, CA"),
    ("data.seattle.gov", "Seattle, WA"),
    ("data.austintexas.gov", "Austin, TX"),
    ("data.baltimorecity.gov", "Baltimore, MD"),
    ("data.nashville.gov", "Nashville, TN"),
    ("data.kcmo.org", "Kansas City, MO"),
    ("data.montgomerycountymd.gov", "Montgomery County, MD"),
]


def _score(patterns: list[tuple[re.Pattern, int]], name: str) -> int:
    return max((w for p, w in patterns if p.search(name)), default=0)


def civic_weight(title: str, description: str, category: str) -> int:
    blob = f"{title} {category} {description or ''}"
    return _score(_CIVIC_WEIGHT, blob)


def pick_date_field(columns: list[dict[str, Any]]) -> str | None:
    best, best_score = None, 0
    for c in columns:
        if c.get("dataTypeName") not in TIMESTAMP_TYPES:
            continue
        name = c.get("fieldName", "")
        # An "updated"/"modified" column describes the row, not the event.
        if re.search(r"(updated?|modified|refresh|closed|resolution)", name, re.I):
            continue
        s = _score(_EVENT_DATE_HINTS, name)
        if s > best_score:
            best, best_score = name, s
    return best if best_score > 0 else None


def pick_business_key(
    columns: list[dict[str, Any]], sample: list[dict[str, Any]]
) -> list[str] | None:
    """Choose a natural key, verified unique against a real sample.

    Uniqueness is tested rather than assumed. A column named ``case_number`` that
    repeats is not a key, and treating it as one would manufacture phantom
    revisions out of ordinary distinct records.
    """
    if not sample:
        return None
    n = len(sample)
    candidates: list[tuple[int, str]] = []
    for c in columns:
        name = c.get("fieldName", "")
        if name.startswith(":"):
            continue
        hint = _score(_KEY_HINTS, name)
        if hint == 0:
            continue
        values = [r.get(name) for r in sample]
        present = [v for v in values if v not in (None, "")]
        if len(present) < n:  # a key may not be null
            continue
        if len(set(map(str, present))) != n:  # nor may it repeat
            continue
        candidates.append((hint, name))
    if not candidates:
        return None
    candidates.sort(key=lambda t: (-t[0], len(t[1])))
    return [candidates[0][1]]


def pick_dimensions(columns: list[dict[str, Any]], sample: list[dict[str, Any]]) -> list[str]:
    """Pick up to three low-cardinality categoricals to tally per snapshot."""
    out: list[tuple[int, str]] = []
    for c in columns:
        name = c.get("fieldName", "")
        if name.startswith(":") or c.get("dataTypeName") != "text":
            continue
        if not _DIMENSION_HINTS.search(name):
            continue
        vals = {str(r.get(name)) for r in sample if r.get(name) not in (None, "")}
        # Too many distinct values and the tally is a fingerprint of nothing.
        if 1 < len(vals) <= 60:
            out.append((len(vals), name))
    out.sort()
    return [n for _, n in out[:3]]


def choose_stratum(
    client: SocrataClient,
    domain: str,
    fourfour: str,
    date_field: str,
    target_rows: int = 2500,
    max_rows: int = 6000,
) -> tuple[str, str, int] | None:
    """Find a past window holding a workable number of records.

    The window sits at least a year in the past. Recent data legitimately churns
    as cases close and reports are filed late; a year on, any movement demands an
    explanation.
    """
    now = datetime.now(timezone.utc)
    anchor = now - timedelta(days=420)

    for span_days in (31, 92, 183, 365, 7):
        start = anchor.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=span_days)
        s_iso = start.strftime("%Y-%m-%dT00:00:00")
        e_iso = end.strftime("%Y-%m-%dT00:00:00")
        where = f"{date_field} >= '{s_iso}' AND {date_field} < '{e_iso}'"
        try:
            n = client.scalar_count(domain, fourfour, where)
        except SocrataError as e:
            log.debug("stratum probe failed for %s/%s: %s", domain, fourfour, e)
            return None
        if n == 0:
            continue
        if n <= max_rows:
            return s_iso, e_iso, n
        # Too dense — narrow proportionally and re-probe once.
        shrink = max(1, int(span_days * target_rows / n))
        end = start + timedelta(days=shrink)
        e_iso = end.strftime("%Y-%m-%dT00:00:00")
        where = f"{date_field} >= '{s_iso}' AND {date_field} < '{e_iso}'"
        try:
            n2 = client.scalar_count(domain, fourfour, where)
        except SocrataError:
            return None
        if 0 < n2 <= max_rows:
            return s_iso, e_iso, n2
    return None


def evaluate_dataset(
    client: SocrataClient, domain: str, city: str, entry: dict[str, Any]
) -> dict[str, Any] | None:
    """Turn a catalog entry into a watchlist row, or reject it with a reason."""
    res = entry.get("resource", {})
    fourfour = res.get("id")
    title = res.get("name") or ""
    if not fourfour:
        return None

    classification = entry.get("classification", {})
    category = (classification.get("domain_category") or "").strip()
    description = res.get("description") or ""
    weight = civic_weight(title, description, category)
    if weight == 0:
        return None

    try:
        meta = client.metadata(domain, fourfour)
    except SocrataError as e:
        log.debug("metadata failed %s/%s: %s", domain, fourfour, e)
        return None

    columns = [
        {"fieldName": c.get("fieldName", ""), "dataTypeName": c.get("dataTypeName", "")}
        for c in meta.get("columns", [])
    ]
    if len(columns) < 3:
        return None

    date_field = pick_date_field(columns)
    if not date_field:
        return None

    try:
        sample = client.rows(domain, fourfour, limit=400, order=":id")
    except SocrataError:
        return None
    if len(sample) < 50:
        return None

    business_key = pick_business_key(columns, sample)
    if not business_key:
        # Without a natural key we can still see appends and deletions, but not
        # in-place revision — which is the finding we care most about.
        return None

    stratum = choose_stratum(client, domain, fourfour, date_field)
    if not stratum:
        return None
    s_start, s_end, s_count = stratum

    return {
        "source_key": f"{domain}/{fourfour}",
        "domain": domain,
        "fourfour": fourfour,
        "title": title[:300],
        "city": city,
        "category": category,
        "date_field": date_field,
        "business_key": business_key,
        "stratum_start": s_start,
        "stratum_end": s_end,
        "extra_volatile": [],
        "agg_dimensions": pick_dimensions(columns, sample),
        "pipeline_class": "unknown",
        "notes": f"civic_weight={weight}; stratum_rows_at_discovery={s_count}",
        "added_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "_weight": weight,
        "_stratum_rows": s_count,
    }


def discover(
    client: SocrataClient,
    portals: list[tuple[str, str]] | None = None,
    per_portal: int = 60,
    catalog_scan: int = 220,
    on_accept=None,
) -> list[dict[str, Any]]:
    """Scan portals and return accepted watchlist entries, best first."""
    portals = portals or PORTALS
    accepted: list[dict[str, Any]] = []

    for domain, city in portals:
        log.info("scanning %s (%s)", domain, city)
        entries: list[dict[str, Any]] = []
        try:
            offset = 0
            while len(entries) < catalog_scan:
                page = client.catalog(domain, limit=100, offset=offset)
                results = page.get("results", [])
                if not results:
                    break
                entries.extend(results)
                offset += len(results)
        except SocrataError as e:
            log.warning("catalog failed for %s: %s", domain, e)
            continue

        # Rank by subject-matter consequence so the budget goes to what matters.
        ranked = sorted(
            entries,
            key=lambda e: -civic_weight(
                (e.get("resource") or {}).get("name") or "",
                (e.get("resource") or {}).get("description") or "",
                (e.get("classification") or {}).get("domain_category") or "",
            ),
        )

        taken = 0
        for entry in ranked:
            if taken >= per_portal:
                break
            try:
                row = evaluate_dataset(client, domain, city, entry)
            except Exception as e:  # never let one dataset kill the scan
                log.debug("evaluate failed: %s", e)
                continue
            if row:
                accepted.append(row)
                taken += 1
                log.info(
                    "  + %-58s key=%s stratum=%s rows=%s",
                    row["title"][:58], row["business_key"],
                    row["stratum_start"][:10], row["_stratum_rows"],
                )
                if on_accept:
                    on_accept(row)

    accepted.sort(key=lambda r: -r["_weight"])
    return accepted


if __name__ == "__main__":
    import argparse

    from .store import Archive

    ap = argparse.ArgumentParser(description="Build the Palimpsest watchlist.")
    ap.add_argument("--db", default="archive/palimpsest.db")
    ap.add_argument("--per-portal", type=int, default=60)
    ap.add_argument("--out", default="archive/watchlist.json")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s"
    )

    archive = Archive(args.db)
    client = SocrataClient()

    rows = discover(client, per_portal=args.per_portal, on_accept=archive.upsert_source)

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, indent=2)

    by_city: dict[str, int] = {}
    for r in rows:
        by_city[r["city"]] = by_city.get(r["city"], 0) + 1
    log.info("accepted %d datasets across %d cities", len(rows), len(by_city))
    for c, n in sorted(by_city.items(), key=lambda t: -t[1]):
        log.info("  %-24s %d", c, n)
    log.info("requests issued: %d", client.request_count)
