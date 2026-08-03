"""The collector: observe every watched source and commit what it said.

One sweep visits each source once. Per source we capture two things:

* The **stratum** — every record in a fixed past window, fingerprinted field by
  field. This is the high-resolution evidence: it can show a single value being
  altered inside a single record.
* **Aggregates** over the whole frozen past — total row count, and tallies across
  a few categorical columns. This is the wide-angle view: it costs three requests
  regardless of whether the dataset holds ten thousand rows or ten million, and
  it catches deletions and reclassifications happening outside the stratum.

Neither alone is sufficient. The stratum sees detail but only through a keyhole;
the aggregates see everything but only in outline.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from datetime import datetime, timezone
from typing import Any

from .core import classify_fields, fingerprint_record
from .socrata import SocrataClient, SocrataError, response_provenance
from .store import Archive

log = logging.getLogger("palimpsest.collect")

# No single source may hold a sweep longer than this. Generous next to a healthy
# source (the slowest legitimate one observed takes ~9 minutes) and decisive
# against one that has stopped responding properly.
SOURCE_DEADLINE_S = 15 * 60.0

_stop = False


def _handle_signal(signum, frame):  # pragma: no cover - signal path
    global _stop
    _stop = True
    log.warning("signal %s received; finishing current source then stopping", signum)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def collect_aggregates(
    client: SocrataClient, src: dict[str, Any]
) -> dict[str, Any]:
    """Cheap wide-angle fingerprint of the dataset's frozen past."""
    domain, ff = src["domain"], src["fourfour"]
    date_field = src["date_field"]
    stratum_end = src["stratum_end"]
    stratum_start = src["stratum_start"]

    agg: dict[str, Any] = {}

    # Everything older than the stratum's end. Should be monotonic: this window
    # is closed, so its population can only change if the publisher rewrites it.
    frozen_where = f"{date_field} < '{stratum_end}'"
    agg["frozen_past_count"] = client.scalar_count(domain, ff, frozen_where)

    stratum_where = (
        f"{date_field} >= '{stratum_start}' AND {date_field} < '{stratum_end}'"
    )
    agg["stratum_count"] = client.scalar_count(domain, ff, stratum_where)

    # Categorical tallies inside the stratum. A reclassification — a burglary
    # relabelled, a case status flipped — moves these before anything else.
    tallies: dict[str, dict[str, int]] = {}
    for dim in (src.get("agg_dimensions") or [])[:3]:
        try:
            rows = client.rows(
                domain, ff,
                select=f"{dim} AS v, count(*) AS n",
                where=stratum_where,
                group=dim,
                limit=200,
            )
            tallies[dim] = {
                str(r.get("v")): int(r.get("n", 0)) for r in rows if r.get("v") is not None
            }
        except SocrataError as e:
            log.debug("tally failed for %s.%s: %s", src["source_key"], dim, e)
    if tallies:
        agg["tallies"] = tallies

    return agg


def collect_source(
    client: SocrataClient, archive: Archive, src: dict[str, Any]
) -> dict[str, Any] | None:
    """Observe one source once and commit the result."""
    started = time.time()
    key = src["source_key"]
    domain, ff = src["domain"], src["fourfour"]

    try:
        where = (
            f"{src['date_field']} >= '{src['stratum_start']}' "
            f"AND {src['date_field']} < '{src['stratum_end']}'"
        )

        # ":*,*" asks for platform internals alongside published columns. We need
        # both: the internals are what let us tell a table reload apart from an
        # edit, even though they are excluded from the content hash.
        rows: list[dict[str, Any]] = []
        first_headers: dict[str, str] = {}
        page_size = 2000
        offset = 0
        # A per-source wall-clock deadline. One Austin endpoint once consumed
        # 29 hours fetching 2,170 rows: a socket timeout is applied per read, so
        # a server that trickles a response body without ever finishing it never
        # trips one. That single source stalled an entire sweep, and a collector
        # that can be halted indefinitely by one slow publisher gathers nothing.
        # Abandoning the source costs one observation; hanging costs the sweep.
        deadline = started + SOURCE_DEADLINE_S
        while True:
            if time.time() > deadline:
                raise SocrataError(
                    f"exceeded {SOURCE_DEADLINE_S:.0f}s deadline after "
                    f"{len(rows)} rows ({offset} offset); abandoning this source "
                    f"for this sweep"
                )
            resp = client.query(
                domain, ff,
                select=":*,*",
                where=where,
                order=f"{src['date_field']}, :id",
                limit=page_size,
                offset=offset,
            )
            if not first_headers:
                first_headers = resp.headers
            page = resp.data or []
            rows.extend(page)
            if len(page) < page_size or len(rows) >= 25000:
                break
            offset += page_size

        if not rows:
            raise SocrataError("stratum returned zero rows")

        columns = sorted({k for r in rows for k in r})
        field_classes = classify_fields(columns, src.get("extra_volatile") or [])

        extra_volatile = src.get("extra_volatile") or []
        fingerprints = [
            fingerprint_record(r, src["business_key"], extra_volatile) for r in rows
        ]

        # A natural key that collides is not a key. Discovery verifies uniqueness
        # against a sample, which cannot rule out a duplicate elsewhere in the
        # stratum. Rather than fold two distinct records into one — which would
        # both lose a record and manufacture a phantom edit between them — the
        # source falls back to content identity for this observation.
        #
        # The cost is stated honestly rather than hidden: under content identity a
        # revision is indistinguishable from a deletion plus an insertion, so this
        # source can evidence appends and disappearances but not in-place edits.
        identity_mode = "business_key"
        uids = [f.row_uid for f in fingerprints]
        collisions = len(uids) - len(set(uids))
        if collisions:
            log.warning(
                "%s: key %s collides on %d of %d rows — falling back to content "
                "identity; in-place revision cannot be attributed for this source",
                key, src["business_key"], collisions, len(uids),
            )
            fingerprints = [fingerprint_record(r, [], extra_volatile) for r in rows]
            identity_mode = "content"
            uids = [f.row_uid for f in fingerprints]

        aggregates = collect_aggregates(client, src)
        aggregates["stratum_rows_fetched"] = len(rows)
        aggregates["distinct_row_uids"] = len(set(uids))
        aggregates["identity_mode"] = identity_mode
        aggregates["key_collisions"] = collisions

        result = archive.write_snapshot(
            source_key=key,
            captured_at=utcnow(),
            fingerprints=fingerprints,
            columns=columns,
            field_classes=field_classes,
            aggregates=aggregates,
            http_meta=response_provenance(first_headers),
            duration_s=time.time() - started,
        )
        log.info(
            "  ok  %-46s rows=%-6d root=%s",
            key[-46:], result["row_count"], result["merkle_root"][:12],
        )
        return result

    except Exception as e:
        archive.write_failure(key, utcnow(), f"{type(e).__name__}: {e}", time.time() - started)
        log.warning("  ERR %-46s %s: %s", key[-46:], type(e).__name__, e)
        return None


def sweep(client: SocrataClient, archive: Archive, limit: int | None = None) -> dict[str, int]:
    sources = archive.sources()
    if limit:
        sources = sources[:limit]
    log.info("sweep starting over %d sources", len(sources))
    started = time.time()
    ok = err = 0
    for i, src in enumerate(sources, 1):
        if _stop:
            log.warning("stopping early at %d/%d", i, len(sources))
            break
        if collect_source(client, archive, src):
            ok += 1
        else:
            err += 1
    dur = time.time() - started
    log.info(
        "sweep done: %d ok, %d failed, %.1f min, %d requests total",
        ok, err, dur / 60, client.request_count,
    )
    return {"ok": ok, "error": err, "seconds": int(dur)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Collect Palimpsest snapshots.")
    ap.add_argument("--db", default="archive/palimpsest.db")
    ap.add_argument("--once", action="store_true", help="run a single sweep and exit")
    ap.add_argument("--interval", type=float, default=3.0, help="hours between sweeps")
    ap.add_argument("--limit", type=int, default=None, help="cap sources per sweep")
    ap.add_argument("--min-interval", type=float, default=0.7, help="seconds between requests")
    ap.add_argument("--log", default="archive/collector.log")
    args = ap.parse_args(argv)

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if args.log:
        from pathlib import Path

        Path(args.log).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(args.log, encoding="utf-8"))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        handlers=handlers,
    )

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handle_signal)
        except (ValueError, OSError):  # pragma: no cover - platform dependent
            pass

    archive = Archive(args.db)
    client = SocrataClient(min_interval=args.min_interval)

    if not archive.sources():
        log.error("watchlist is empty — run `python -m palimpsest.discover` first")
        return 1

    if args.once:
        sweep(client, archive, args.limit)
        log.info("archive: %s", archive.stats())
        return 0

    while not _stop:
        sweep(client, archive, args.limit)
        log.info("archive: %s", archive.stats())
        if _stop:
            break
        wake = time.time() + args.interval * 3600
        log.info("sleeping until %s", datetime.fromtimestamp(wake).strftime("%H:%M:%S"))
        while time.time() < wake and not _stop:
            time.sleep(5)

    log.info("collector stopped cleanly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
