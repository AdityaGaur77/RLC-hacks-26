"""Core primitives: field classification, content addressing, Merkle trees.

The central problem this module solves is distinguishing a record whose *meaning*
changed from a record whose *plumbing* changed.

Open data portals frequently republish a dataset wholesale: the publisher drops
the table and reloads it. Every row therefore gets a fresh internal id and a
fresh modification timestamp, even though not one published fact is different.
Reading those timestamps as "edits" produces a headline like "city altered eight
million crime records" that is entirely false.

So identity and content are both computed from *declared semantic fields only*.
Everything the platform generates, everything derived, and everything that merely
records when the plumbing last ran is excluded from the content hash and tracked
separately as provenance.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

# --------------------------------------------------------------------------
# Field classification
# --------------------------------------------------------------------------

# Socrata exposes platform internals with a leading colon (":id", ":updated_at",
# ":version") and spatial joins it computed itself as ":@computed_region_*".
# None of these are published facts; they are artifacts of the hosting platform.
_SYSTEM_PREFIXES = (":",)

# Some portals surface the same computed spatial joins without the colon.
_DERIVED_PATTERNS = (
    re.compile(r"^:?@?computed_region", re.I),
    re.compile(r"^:?@computed", re.I),
)

# Columns that are genuinely published but only describe when the record was last
# written. A change confined to these is provenance, not revision.
#
# This list is deliberately generous, and each entry was earned by a false
# positive. A publisher's ETL clock appearing in a diff produces a finding that
# reads as "this permit's status was altered" when the true content is "the
# pipeline ran at 4am" — the exact conflation this project exists to refuse.
# Every pattern is anchored end-to-end so only whole column names that are
# purely bookkeeping match; a substring rule would swallow real fields.
# Portals stack time suffixes ("last_updated_date_time"), so allow up to two.
_TIME_SUFFIX = r"(_?(date|time|datetime|timestamp|at|on|ts|dttm|dt)){0,2}"

# The verbs that mark a column as describing when a row was *written*. Kept
# narrow on purpose. Widening this to bare nouns would swallow real fields —
# "applicant_last_name", "last_objection_date" and "last_doc_date" are facts
# about a case, not about the pipeline, and must keep being hashed.
_WRITE_VERB = r"(updated?|modified|changed|edited|revised)"

_VOLATILE_PATTERNS = (
    # updated_on, last_updated, data_updated_at, violation_last_modified_date
    re.compile(rf"^([a-z0-9]+_)?(last_?)?{_WRITE_VERB}{_TIME_SUFFIX}$", re.I),
    re.compile(rf"^(date|time)_?{_WRITE_VERB}$", re.I),
    # who touched it is provenance too
    re.compile(rf"^{_WRITE_VERB}_?by$", re.I),
    # pipeline and warehouse bookkeeping
    re.compile(
        rf"^(data_?)?(as_?of|refresh(ed)?|reload(ed)?|load(ed)?|extract(ed)?|"
        rf"ingest(ed)?|etl|sync(ed)?|snapshot|import(ed)?|publish(ed)?|"
        rf"process(ed)?|generat(ed|ion)){_TIME_SUFFIX}$",
        re.I,
    ),
    re.compile(rf"^last_?(run|refresh|load|sync|import|extract|update){_TIME_SUFFIX}$", re.I),
    # platform surrogate keys and integrity columns
    re.compile(r"^(row|record)_?(version|id|hash|checksum|guid|uuid)$", re.I),
)


def is_system_field(name: str) -> bool:
    """True for platform internals that are not published facts."""
    if name.startswith(_SYSTEM_PREFIXES):
        return True
    return any(p.match(name) for p in _DERIVED_PATTERNS)


def is_volatile_field(name: str, extra: Sequence[str] = ()) -> bool:
    """True for published columns that only record when the row was last touched."""
    if name in extra:
        return True
    return any(p.match(name) for p in _VOLATILE_PATTERNS)


def classify_fields(
    columns: Iterable[str], extra_volatile: Sequence[str] = ()
) -> dict[str, list[str]]:
    """Partition a dataset's columns into semantic / volatile / system."""
    semantic: list[str] = []
    volatile: list[str] = []
    system: list[str] = []
    for c in columns:
        if is_system_field(c):
            system.append(c)
        elif is_volatile_field(c, extra_volatile):
            volatile.append(c)
        else:
            semantic.append(c)
    return {
        "semantic": sorted(semantic),
        "volatile": sorted(volatile),
        "system": sorted(system),
    }


# --------------------------------------------------------------------------
# Value normalisation
# --------------------------------------------------------------------------

_TRAILING_ZEROS = re.compile(r"^(-?\d+)\.0+$")


def normalise_value(v: Any) -> Any:
    """Canonicalise a value so cosmetic serialisation changes are not read as edits.

    Portals are inconsistent about how they render the same fact between exports:
    "5" versus "5.0", padded whitespace, an empty string where a null was, and
    nested dicts whose key order is not stable. None of those are revisions.
    """
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        # Represent numbers as text so 5 and 5.0 collapse to one form.
        s = repr(float(v)) if isinstance(v, float) else str(v)
        m = _TRAILING_ZEROS.match(s)
        return m.group(1) if m else s
    if isinstance(v, str):
        s = v.strip()
        if s == "":
            return None
        m = _TRAILING_ZEROS.match(s)
        if m:
            return m.group(1)
        return s
    if isinstance(v, dict):
        return {k: normalise_value(v[k]) for k in sorted(v)}
    if isinstance(v, list):
        return [normalise_value(x) for x in v]
    return str(v)


def canonical_json(obj: Any) -> str:
    """Deterministic JSON: sorted keys, no incidental whitespace."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


# --------------------------------------------------------------------------
# Content addressing
# --------------------------------------------------------------------------


def sha256_hex(data: str | bytes) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


@dataclass
class RecordFingerprint:
    """The three-way split of a single record."""

    row_uid: str
    content: dict[str, Any] = field(default_factory=dict)
    volatile: dict[str, Any] = field(default_factory=dict)
    system: dict[str, Any] = field(default_factory=dict)

    @property
    def content_hash(self) -> str:
        return sha256_hex(canonical_json(self.content))

    @property
    def volatile_hash(self) -> str:
        return sha256_hex(canonical_json(self.volatile))


def fingerprint_record(
    row: Mapping[str, Any],
    business_key: Sequence[str],
    extra_volatile: Sequence[str] = (),
) -> RecordFingerprint:
    """Split one record into content / volatile / system and derive its identity.

    ``business_key`` is the dataset's own natural identifier (a case number, a
    permit number, a service request id). It must not be the platform's internal
    row id: on a republish that id is regenerated and every record would appear
    to be simultaneously deleted and created.
    """
    content: dict[str, Any] = {}
    volatile: dict[str, Any] = {}
    system: dict[str, Any] = {}

    for k, v in row.items():
        nv = normalise_value(v)
        if is_system_field(k):
            system[k] = nv
        elif is_volatile_field(k, extra_volatile):
            volatile[k] = nv
        else:
            content[k] = nv

    if business_key:
        parts = [str(normalise_value(row.get(k)) or "") for k in business_key]
        uid = "|".join(parts)
        # A blank composite key is not an identity; fall back rather than collide.
        if uid.strip("|"):
            row_uid = uid
        else:
            row_uid = "sha256:" + sha256_hex(canonical_json(content))
    else:
        # No declared natural key: identity is the content itself. Such a dataset
        # can show appends and deletions but cannot show in-place revision,
        # because a revised record is indistinguishable from a swap.
        row_uid = "sha256:" + sha256_hex(canonical_json(content))

    return RecordFingerprint(row_uid, content, volatile, system)


# --------------------------------------------------------------------------
# Merkle tree
# --------------------------------------------------------------------------

# Domain separation: hashing leaves and interior nodes with different prefixes
# prevents an interior node from being presented as a leaf.
_LEAF = b"\x00"
_NODE = b"\x01"

EMPTY_ROOT = sha256_hex(b"palimpsest/empty")


def leaf_hash(row_uid: str, content_hash: str) -> str:
    return hashlib.sha256(
        _LEAF + canonical_json([row_uid, content_hash]).encode("utf-8")
    ).hexdigest()


def _pair(a: str, b: str) -> str:
    return hashlib.sha256(_NODE + bytes.fromhex(a) + bytes.fromhex(b)).hexdigest()


def merkle_root(leaves: Sequence[str]) -> str:
    """Root over an ordered list of leaf hashes.

    Odd nodes are promoted rather than duplicated. Duplicating the final node
    lets two different leaf sets produce one root, which would undermine the
    whole point of publishing the root.
    """
    if not leaves:
        return EMPTY_ROOT
    level = list(leaves)
    while len(level) > 1:
        nxt: list[str] = []
        for i in range(0, len(level) - 1, 2):
            nxt.append(_pair(level[i], level[i + 1]))
        if len(level) % 2:
            nxt.append(level[-1])
        level = nxt
    return level[0]


def merkle_proof(leaves: Sequence[str], index: int) -> list[dict[str, str]]:
    """Sibling path proving ``leaves[index]`` is committed to by the root."""
    if not leaves or not (0 <= index < len(leaves)):
        raise IndexError("leaf index out of range")
    proof: list[dict[str, str]] = []
    level = list(leaves)
    idx = index
    while len(level) > 1:
        nxt: list[str] = []
        for i in range(0, len(level) - 1, 2):
            left, right = level[i], level[i + 1]
            if i == idx:
                proof.append({"side": "right", "hash": right})
            elif i + 1 == idx:
                proof.append({"side": "left", "hash": left})
            nxt.append(_pair(left, right))
        if len(level) % 2:
            if idx == len(level) - 1:
                pass  # promoted unchanged; nothing joins it at this level
            nxt.append(level[-1])
        idx //= 2
        level = nxt
    return proof


def verify_proof(leaf: str, proof: Sequence[Mapping[str, str]], root: str) -> bool:
    cur = leaf
    for step in proof:
        if step["side"] == "right":
            cur = _pair(cur, step["hash"])
        else:
            cur = _pair(step["hash"], cur)
    return cur == root


# --------------------------------------------------------------------------
# Snapshot chaining
# --------------------------------------------------------------------------

GENESIS = sha256_hex(b"palimpsest/genesis/v1")


def chain_hash(prev: str, merkle_root_hex: str, meta: Mapping[str, Any]) -> str:
    """Link a snapshot to its predecessor.

    Each snapshot commits to the one before it, so the archive cannot be quietly
    rewritten after the fact: altering any past observation changes every chain
    hash that follows it.
    """
    return sha256_hex(canonical_json([prev, merkle_root_hex, meta]))
