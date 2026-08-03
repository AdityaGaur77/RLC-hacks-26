"""The archive: an append-only, content-addressed, hash-chained SQLite store.

Design notes
------------
Records are stored by content hash, so a record that does not change across a
hundred snapshots occupies one row of storage, not a hundred. Snapshots hold
references. This keeps a week of dense observation small enough to commit to a
public repository, which matters: an archive nobody can independently obtain is
not evidence of anything.

Nothing is ever updated in place. Corrections are appended. Each snapshot commits
to its predecessor by hash, so the archive can demonstrate that its own account
of the past has not been altered — which is precisely the property we are
faulting the portals for lacking.
"""

from __future__ import annotations

import json
import sqlite3
import zlib
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from .core import (
    GENESIS,
    canonical_json,
    chain_hash,
    leaf_hash,
    merkle_root,
    sha256_hex,
)

# Record payloads are stored deflated. An archive nobody can download is not
# evidence of anything, so the whole week of observation has to stay small
# enough to publish. Civic records are extremely repetitive JSON and compress
# roughly six-fold, which is the difference between a repository and a rumour.
_COMPRESS_LEVEL = 9


def _pack(payload: str) -> bytes:
    return zlib.compress(payload.encode("utf-8"), _COMPRESS_LEVEL)


def _unpack(raw: Any) -> str:
    """Read a payload written by either the compressed or the plain-text path."""
    if isinstance(raw, str):
        return raw
    try:
        return zlib.decompress(raw).decode("utf-8")
    except zlib.error:
        return bytes(raw).decode("utf-8")

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- One row per dataset we watch.
CREATE TABLE IF NOT EXISTS sources (
    source_key      TEXT PRIMARY KEY,      -- "<domain>/<fourfour>"
    domain          TEXT NOT NULL,
    fourfour        TEXT NOT NULL,
    title           TEXT,
    city            TEXT,
    category        TEXT,
    date_field      TEXT,                  -- column holding the event date
    business_key    TEXT,                  -- JSON list: the natural identifier
    stratum_start   TEXT,                  -- frozen observation window (inclusive)
    stratum_end     TEXT,                  -- exclusive
    extra_volatile  TEXT,                  -- JSON list of publisher-specific noise
    agg_dimensions  TEXT,                  -- JSON list of categoricals to tally
    pipeline_class  TEXT,                  -- republish | incremental | unknown
    notes           TEXT,
    added_at        TEXT NOT NULL
);

-- One row per successful observation of a source.
CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    source_key      TEXT NOT NULL REFERENCES sources(source_key),
    captured_at     TEXT NOT NULL,         -- ISO-8601 UTC
    status          TEXT NOT NULL,         -- ok | error
    error           TEXT,
    row_count       INTEGER,
    merkle_root     TEXT,
    prev_chain_hash TEXT,
    chain_hash      TEXT,
    schema_hash     TEXT,
    columns         TEXT,                  -- JSON list
    field_classes   TEXT,                  -- JSON: semantic/volatile/system split
    aggregates      TEXT,                  -- JSON: counts over the frozen past
    http_meta       TEXT,                  -- JSON: portal's own freshness claims
    duration_s      REAL,
    -- When a sweep observes a provably identical record set (same Merkle root),
    -- it stores no observation rows and points here instead. Most sweeps over
    -- most sources see nothing change; writing two thousand identical rows to
    -- say so would make the archive far larger than the data it watches.
    observations_ref INTEGER REFERENCES snapshots(snapshot_id)
);
CREATE INDEX IF NOT EXISTS idx_snap_source ON snapshots(source_key, captured_at);

-- Content-addressed payloads. Written once per distinct record value, ever.
CREATE TABLE IF NOT EXISTS blobs (
    content_hash    TEXT PRIMARY KEY,
    payload         TEXT NOT NULL
);

-- Which records a snapshot saw, and what they said at that moment.
CREATE TABLE IF NOT EXISTS observations (
    snapshot_id     INTEGER NOT NULL REFERENCES snapshots(snapshot_id),
    row_uid         TEXT NOT NULL,
    content_hash    TEXT NOT NULL REFERENCES blobs(content_hash),
    volatile_hash   TEXT,
    volatile_json   TEXT,
    PRIMARY KEY (snapshot_id, row_uid)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_obs_uid ON observations(row_uid);

-- Findings produced by the diff engine. Append-only, like everything else.
CREATE TABLE IF NOT EXISTS changes (
    change_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    source_key      TEXT NOT NULL REFERENCES sources(source_key),
    from_snapshot   INTEGER NOT NULL,
    to_snapshot     INTEGER NOT NULL,
    detected_at     TEXT NOT NULL,
    kind            TEXT NOT NULL,         -- see diff.ChangeKind
    row_uid         TEXT,
    before_hash     TEXT,
    after_hash      TEXT,
    field_deltas    TEXT,                  -- JSON: {field: [before, after]}
    significance    REAL,
    detail          TEXT
);
CREATE INDEX IF NOT EXISTS idx_chg_source ON changes(source_key, detected_at);
CREATE INDEX IF NOT EXISTS idx_chg_kind ON changes(kind);

-- Records that a diff pass has already been run for a snapshot pair, so the
-- engine is idempotent and safe to re-run.
CREATE TABLE IF NOT EXISTS diff_runs (
    from_snapshot   INTEGER NOT NULL,
    to_snapshot     INTEGER NOT NULL,
    completed_at    TEXT NOT NULL,
    change_count    INTEGER NOT NULL,
    PRIMARY KEY (from_snapshot, to_snapshot)
);
"""


class Archive:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), timeout=60.0)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Additive migrations. The archive is append-only; so is its schema."""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(snapshots)")}
        if "observations_ref" not in cols:
            self.conn.execute(
                "ALTER TABLE snapshots ADD COLUMN observations_ref INTEGER"
            )

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # -- sources -----------------------------------------------------------

    def upsert_source(self, src: dict[str, Any]) -> None:
        cols = (
            "source_key", "domain", "fourfour", "title", "city", "category",
            "date_field", "business_key", "stratum_start", "stratum_end",
            "extra_volatile", "agg_dimensions", "pipeline_class", "notes", "added_at",
        )
        row = {k: src.get(k) for k in cols}
        for j in ("business_key", "extra_volatile", "agg_dimensions"):
            if isinstance(row[j], (list, tuple)):
                row[j] = canonical_json(list(row[j]))
        placeholders = ",".join("?" for _ in cols)
        updates = ",".join(f"{c}=excluded.{c}" for c in cols if c != "source_key")
        with self.tx() as c:
            c.execute(
                f"INSERT INTO sources ({','.join(cols)}) VALUES ({placeholders}) "
                f"ON CONFLICT(source_key) DO UPDATE SET {updates}",
                [row[c_] for c_ in cols],
            )

    def sources(self, active_only: bool = True) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM sources ORDER BY source_key").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            for j in ("business_key", "extra_volatile", "agg_dimensions"):
                d[j] = json.loads(d[j]) if d[j] else []
            out.append(d)
        return out

    def set_pipeline_class(self, source_key: str, cls: str, notes: str = "") -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE sources SET pipeline_class=?, notes=? WHERE source_key=?",
                (cls, notes, source_key),
            )

    # -- snapshots ---------------------------------------------------------

    def last_chain_hash(self, source_key: str) -> str:
        r = self.conn.execute(
            "SELECT chain_hash FROM snapshots WHERE source_key=? AND status='ok' "
            "ORDER BY snapshot_id DESC LIMIT 1",
            (source_key,),
        ).fetchone()
        return r["chain_hash"] if r and r["chain_hash"] else GENESIS

    def _last_ok_snapshot(self, source_key: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT snapshot_id, merkle_root, observations_ref FROM snapshots "
            "WHERE source_key=? AND status='ok' ORDER BY snapshot_id DESC LIMIT 1",
            (source_key,),
        ).fetchone()

    def resolve_observations_owner(self, snapshot_id: int) -> int:
        """Follow the deduplication pointer to the snapshot that holds the rows."""
        r = self.conn.execute(
            "SELECT observations_ref FROM snapshots WHERE snapshot_id=?", (snapshot_id,)
        ).fetchone()
        if r and r["observations_ref"]:
            return int(r["observations_ref"])
        return snapshot_id

    def write_snapshot(
        self,
        source_key: str,
        captured_at: str,
        fingerprints: Sequence[Any],
        columns: Sequence[str],
        field_classes: dict[str, list[str]],
        aggregates: dict[str, Any],
        http_meta: dict[str, str],
        duration_s: float,
    ) -> dict[str, Any]:
        """Commit one observation of one source."""
        # Deterministic order: the Merkle root must not depend on arrival order.
        ordered = sorted(fingerprints, key=lambda f: f.row_uid)

        # The root must commit to exactly what is stored, never to more. Storage
        # is keyed on (snapshot, row_uid), so any residual duplicate would be
        # collapsed on write and the published root would then describe a record
        # set that cannot be reproduced from the archive. Collapsing here instead
        # keeps that invariant provable. Callers resolve key collisions upstream;
        # what survives to this point is genuinely indistinguishable records.
        deduped: list[Any] = []
        seen: set[str] = set()
        for f in ordered:
            if f.row_uid in seen:
                continue
            seen.add(f.row_uid)
            deduped.append(f)
        collapsed = len(ordered) - len(deduped)
        ordered = deduped

        leaves = [leaf_hash(f.row_uid, f.content_hash) for f in ordered]
        root = merkle_root(leaves)
        if collapsed:
            aggregates = dict(aggregates, indistinguishable_records_collapsed=collapsed)

        # The Merkle root commits to content only, so on its own it cannot show
        # that a publisher rewrote every row without changing a single fact. This
        # digest preserves that signal in constant space. Churn is worth counting,
        # never worth attributing to individual records.
        volatile_digest = sha256_hex(
            canonical_json([[f.row_uid, f.volatile_hash] for f in ordered])
        )
        aggregates = dict(aggregates, volatile_digest=volatile_digest)

        schema_hash = sha256_hex(canonical_json(sorted(columns)))

        meta = {
            "source_key": source_key,
            "captured_at": captured_at,
            "row_count": len(ordered),
            "schema_hash": schema_hash,
            "aggregates": aggregates,
        }
        prev = self.last_chain_hash(source_key)
        ch = chain_hash(prev, root, meta)

        # If the record set is provably identical to the last observation, point
        # at it rather than storing a second copy.
        previous = self._last_ok_snapshot(source_key)
        ref: int | None = None
        if previous and previous["merkle_root"] == root:
            ref = int(previous["observations_ref"] or previous["snapshot_id"])

        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO snapshots (source_key, captured_at, status, row_count, "
                "merkle_root, prev_chain_hash, chain_hash, schema_hash, columns, "
                "field_classes, aggregates, http_meta, duration_s, observations_ref) "
                "VALUES (?,?,'ok',?,?,?,?,?,?,?,?,?,?,?)",
                (
                    source_key, captured_at, len(ordered), root, prev, ch, schema_hash,
                    canonical_json(list(columns)), canonical_json(field_classes),
                    canonical_json(aggregates), canonical_json(http_meta), duration_s,
                    ref,
                ),
            )
            snapshot_id = cur.lastrowid

            if ref is None:
                c.executemany(
                    "INSERT OR IGNORE INTO blobs (content_hash, payload) VALUES (?,?)",
                    [(f.content_hash, _pack(canonical_json(f.content))) for f in ordered],
                )
                c.executemany(
                    "INSERT OR REPLACE INTO observations "
                    "(snapshot_id, row_uid, content_hash, volatile_hash, volatile_json) "
                    "VALUES (?,?,?,?,?)",
                    [
                        (snapshot_id, f.row_uid, f.content_hash, f.volatile_hash, None)
                        for f in ordered
                    ],
                )

        return {
            "snapshot_id": snapshot_id,
            "merkle_root": root,
            "chain_hash": ch,
            "row_count": len(ordered),
            "deduplicated": ref is not None,
        }

    def write_failure(
        self, source_key: str, captured_at: str, error: str, duration_s: float
    ) -> None:
        """Record that an observation was attempted and failed.

        Failures are archived too. A gap in the record that is not itself
        recorded is indistinguishable from a period of no change.
        """
        with self.tx() as c:
            c.execute(
                "INSERT INTO snapshots (source_key, captured_at, status, error, duration_s) "
                "VALUES (?,?,'error',?,?)",
                (source_key, captured_at, error[:2000], duration_s),
            )

    def snapshots_for(self, source_key: str, ok_only: bool = True) -> list[dict[str, Any]]:
        q = "SELECT * FROM snapshots WHERE source_key=?"
        if ok_only:
            q += " AND status='ok'"
        q += " ORDER BY snapshot_id"
        return [dict(r) for r in self.conn.execute(q, (source_key,)).fetchall()]

    def observations(self, snapshot_id: int) -> dict[str, dict[str, Any]]:
        owner = self.resolve_observations_owner(snapshot_id)
        rows = self.conn.execute(
            "SELECT row_uid, content_hash, volatile_hash, volatile_json "
            "FROM observations WHERE snapshot_id=?",
            (owner,),
        ).fetchall()
        return {r["row_uid"]: dict(r) for r in rows}

    def record_seen_after(self, source_key: str, row_uid: str, snapshot_id: int) -> bool:
        """Was this record observed again in any later snapshot of this source?

        A record can be absent from one observation and present in the next.
        Publishers reload tables, and a table sampled mid-reload is missing rows
        that were never removed. Comparing two adjacent snapshots cannot tell a
        removal from a gap; the rest of the archive can.
        """
        return (
            self.conn.execute(
                "SELECT 1 FROM observations o "
                "JOIN snapshots s ON s.snapshot_id = o.snapshot_id "
                "WHERE o.row_uid = ? AND s.source_key = ? AND s.snapshot_id > ? "
                "LIMIT 1",
                (row_uid, source_key, snapshot_id),
            ).fetchone()
            is not None
        )

    def blob(self, content_hash: str) -> dict[str, Any] | None:
        r = self.conn.execute(
            "SELECT payload FROM blobs WHERE content_hash=?", (content_hash,)
        ).fetchone()
        return json.loads(_unpack(r["payload"])) if r else None

    def leaves_for(self, snapshot_id: int) -> list[tuple[str, str]]:
        """Ordered (row_uid, content_hash) pairs, for regenerating proofs."""
        owner = self.resolve_observations_owner(snapshot_id)
        rows = self.conn.execute(
            "SELECT row_uid, content_hash FROM observations "
            "WHERE snapshot_id=? ORDER BY row_uid",
            (owner,),
        ).fetchall()
        return [(r["row_uid"], r["content_hash"]) for r in rows]

    # -- changes -----------------------------------------------------------

    def record_changes(self, rows: Iterable[dict[str, Any]]) -> int:
        rows = list(rows)
        if not rows:
            return 0
        with self.tx() as c:
            c.executemany(
                "INSERT INTO changes (source_key, from_snapshot, to_snapshot, "
                "detected_at, kind, row_uid, before_hash, after_hash, field_deltas, "
                "significance, detail) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        r["source_key"], r["from_snapshot"], r["to_snapshot"],
                        r["detected_at"], r["kind"], r.get("row_uid"),
                        r.get("before_hash"), r.get("after_hash"),
                        canonical_json(r["field_deltas"]) if r.get("field_deltas") else None,
                        r.get("significance"), r.get("detail"),
                    )
                    for r in rows
                ],
            )
        return len(rows)

    def mark_diff_run(self, a: int, b: int, when: str, count: int) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO diff_runs VALUES (?,?,?,?)", (a, b, when, count)
            )

    def diff_already_run(self, a: int, b: int) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM diff_runs WHERE from_snapshot=? AND to_snapshot=?", (a, b)
            ).fetchone()
            is not None
        )

    # -- integrity ---------------------------------------------------------

    def verify_chain(self, source_key: str) -> dict[str, Any]:
        """Recompute every chain link for a source and report the first break."""
        snaps = self.snapshots_for(source_key)
        prev = GENESIS
        for s in snaps:
            meta = {
                "source_key": source_key,
                "captured_at": s["captured_at"],
                "row_count": s["row_count"],
                "schema_hash": s["schema_hash"],
                "aggregates": json.loads(s["aggregates"]) if s["aggregates"] else {},
            }
            expect = chain_hash(prev, s["merkle_root"], meta)
            if expect != s["chain_hash"]:
                return {
                    "ok": False,
                    "broken_at": s["snapshot_id"],
                    "captured_at": s["captured_at"],
                    "expected": expect,
                    "stored": s["chain_hash"],
                }
            prev = s["chain_hash"]
        return {"ok": True, "snapshots": len(snaps), "head": prev}

    def verify_merkle(self, snapshot_id: int) -> dict[str, Any]:
        """Recompute a snapshot's Merkle root from its stored observations."""
        r = self.conn.execute(
            "SELECT merkle_root FROM snapshots WHERE snapshot_id=?", (snapshot_id,)
        ).fetchone()
        if not r:
            return {"ok": False, "error": "no such snapshot"}
        leaves = [leaf_hash(u, h) for u, h in self.leaves_for(snapshot_id)]
        got = merkle_root(leaves)
        return {"ok": got == r["merkle_root"], "recomputed": got, "stored": r["merkle_root"]}

    def stats(self) -> dict[str, Any]:
        q = lambda s: self.conn.execute(s).fetchone()[0]  # noqa: E731
        return {
            "sources": q("SELECT COUNT(*) FROM sources"),
            "snapshots_ok": q("SELECT COUNT(*) FROM snapshots WHERE status='ok'"),
            "snapshots_error": q("SELECT COUNT(*) FROM snapshots WHERE status='error'"),
            "observations": q("SELECT COUNT(*) FROM observations"),
            "blobs": q("SELECT COUNT(*) FROM blobs"),
            "changes": q("SELECT COUNT(*) FROM changes"),
            "db_bytes": self.path.stat().st_size if self.path.exists() else 0,
        }
