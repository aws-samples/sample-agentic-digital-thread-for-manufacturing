# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The per-system MANIFEST of tables that carry a steward-declared row exclusion.

A row exclusion is stored on its table's ``SchemaEntry`` (``row_exclusion``).
Deciding from those facts alone whether a system has any exclusion means a
graph read per table, so an unreadable table list or a failed read could not
tell "no exclusion" from "unknown", and a graph fault made every table of every
system unknown. The manifest answers the system-wide question in ONE read of
the System node, kept with the system's own configuration: the property
:data:`MANIFEST_PROPERTY` names each table that may carry an exclusion, with
an opaque ``token`` that changes on every steward write to that table and a
``pending`` flag that is set while a write is in progress (or was interrupted).

* no property (or a null one) on a System node that exists: no table of the
  system carries an exclusion (a confirmed empty manifest);
* a read that fails, returns no System row, or holds a value this module
  cannot parse: the manifest is UNKNOWN (:class:`ManifestUnknown`), never
  empty;
* the steward route writes it with a compare-and-set (:data:`SWAP_MANIFEST`,
  :func:`update`), so two concurrent writers never lose each other's entry.

The write ORDER is the route's (``row_exclusion.set_row_exclusion`` /
``clear_row_exclusion``): on set, the table is listed (pending) BEFORE its fact
is written and settled after; on clear, it is marked pending, its fact cleared,
and only then removed. A write interrupted at any point leaves every stored
fact listed. Each listing takes the table's entry with a new token, and the
rest of that write holds only while the entry keeps it: the fact write is
conditional on it in the same graph statement, and :func:`settling` /
:func:`removing` raise :class:`TokenLost` once a concurrent write of the same
table has taken it.

A re-provision's schema rewrite deletes the SchemaEntries, and the facts
stored on them. Before it does, ONE compare-and-set adds a REWRITE MARKER to
the same stored value (:data:`REWRITE_KEY`, :func:`begin_rewrite`), holding a
copy of every listed table's fact with its provenance; the restore writes the
facts back from that stored copy and removes the marker in its last write
(:func:`end_rewrite`). While the marker stands, a system agent holds every
read of a listed table and a steward write raises :class:`RewriteInProgress`
before it changes anything; because every write here is conditional on the
whole stored value, a steward write in flight when the marker lands writes
nothing more. The marker names the run that owns it (``owner``): only that run
writes facts back under it or removes it. A later re-provision that finds a
marker standing (a crashed or failed run's) takes it over with one
compare-and-set and carries its stored copy as it is; the earlier run, if it
is still going, can then no longer remove it. Pure apart from the injected
graph seams.
"""
from __future__ import annotations

import json
import secrets
from dataclasses import dataclass

#: The System node property that holds the manifest (a JSON string).
MANIFEST_PROPERTY = "row_exclusion_manifest"
READ_MANIFEST = (
    "MATCH (s:System {id: $sid}) "
    "RETURN s.row_exclusion_manifest AS row_exclusion_manifest")
#: Replace the manifest only when it still holds ``$expected`` (``''`` for
#: none); ``$manifest`` null removes the property. Answers one row when it wrote.
SWAP_MANIFEST = (
    "MATCH (s:System {id: $sid}) "
    "WHERE coalesce(s.row_exclusion_manifest, '') = $expected "
    "SET s.row_exclusion_manifest = $manifest RETURN s.id AS id")
#: Compare-and-set attempts before a concurrent writer is reported.
SWAP_ATTEMPTS = 5
#: Longest manifest value read back (a bound on what one property may hold).
MAX_MANIFEST_CHARS = 200_000
#: The key of the stored value that marks a re-provision's schema rewrite in
#: progress: ``{"owner": str, "tables": {table: {"pending": bool,
#: "record": {...} | null}}}``, ``owner`` the token of the run that owns it and
#: ``record`` the table's stored fact and provenance (:data:`RECORD_KEYS`).
REWRITE_KEY = "rewrite"
#: What a carried record holds: the stored fact and its provenance.
RECORD_KEYS = ("row_exclusion", "row_exclusion_by", "row_exclusion_at", "row_exclusion_source")


@dataclass(frozen=True)
class Entry:
    """One listed table: ``token`` changes on every steward write to it, and
    ``pending`` is set while a write is in progress or was interrupted."""

    token: str
    pending: bool = False


class ManifestUnknown(RuntimeError):
    """The manifest could not be read, its System node was not found, or its
    stored value does not parse: which tables carry an exclusion is unknown."""


class ManifestWriteFailed(RuntimeError):
    """A manifest write did not land (the graph refused it, or a concurrent
    writer kept changing it)."""


class ManifestContended(ManifestWriteFailed):
    """A manifest write kept losing to concurrent writers (the value moved,
    or the graph aborted the write on a concurrent modification, on every
    attempt): nothing more of the write is applied, and a steward's change
    is reported as a conflict, never as a failure of the graph."""


class TokenLost(RuntimeError):
    """A steward write of one table found that table's manifest entry no
    longer holds its token: a concurrent write of the SAME table took it.
    Nothing more of the write is applied (its fact write is conditional on the
    token, :func:`settling` and :func:`removing` refuse), and the caller must
    report the conflict, never success."""


class RewriteInProgress(RuntimeError):
    """A steward write found the system's manifest holding a re-provision's
    rewrite marker (:data:`REWRITE_KEY`): nothing more of the write is
    applied, and the caller must report the conflict, never success."""


def new_token() -> str:
    """A fresh opaque token for one steward write."""
    return secrets.token_hex(8)


def _stored(raw) -> dict:
    """The stored value as a JSON object (``{}`` for none);
    ``ManifestUnknown`` for one that is not."""
    if raw is None or raw == "":
        return {}
    try:
        parsed = json.loads(raw) if isinstance(raw, str) and len(raw) <= MAX_MANIFEST_CHARS \
            else None
    except (ValueError, TypeError):
        parsed = None
    if not isinstance(parsed, dict) or not isinstance(parsed.get("tables"), dict):
        raise ManifestUnknown("the stored manifest does not parse")
    return parsed


def parse(raw) -> dict:
    """``{table: Entry}`` from a stored value; ``{}`` for none.
    ``ManifestUnknown`` for a value that is not the manifest's shape."""
    tables = _stored(raw).get("tables") or {}
    out = {}
    for table, entry in tables.items():
        token = entry.get("token") if isinstance(entry, dict) else None
        pending = entry.get("pending", False) if isinstance(entry, dict) else None
        if (not isinstance(table, str) or not table or not isinstance(token, str)
                or not token or not isinstance(pending, bool)):
            raise ManifestUnknown("the stored manifest does not parse")
        out[table] = Entry(token, pending)
    return out


def _carried_record(record):
    """One carried record (``None``: nothing was stored), else ``ManifestUnknown``."""
    if record is None:
        return None
    if (not isinstance(record, dict) or not set(record) <= set(RECORD_KEYS)
            or not isinstance(record.get("row_exclusion"), str)
            or not all(v is None or isinstance(v, str) for v in record.values())):
        raise ManifestUnknown("the stored rewrite marker does not parse")
    return dict(record)


def _marker(raw):
    """The stored rewrite marker as an object, else ``None``.
    ``ManifestUnknown`` for one that is not its shape."""
    marker = _stored(raw).get(REWRITE_KEY)
    if marker is None:
        return None
    owner = marker.get("owner") if isinstance(marker, dict) else None
    if (not isinstance(marker, dict) or not isinstance(marker.get("tables"), dict)
            or not (owner is None or (isinstance(owner, str) and owner))):
        raise ManifestUnknown("the stored rewrite marker does not parse")
    return marker


def rewrite_owner(raw):
    """The token of the run that owns the rewrite marker, else ``None`` (no
    marker, or one written with no owner)."""
    marker = _marker(raw)
    return None if marker is None else marker.get("owner")


def rewrite_of(raw):
    """The rewrite marker's carried copy, ``{table: {"pending", "record"}}``,
    else ``None`` (no rewrite in progress). ``ManifestUnknown`` for a marker
    that is not its shape."""
    marker = _marker(raw)
    if marker is None:
        return None
    tables = marker["tables"]
    out = {}
    for table, carried in tables.items():
        if (not isinstance(table, str) or not table or not isinstance(carried, dict)
                or not isinstance(carried.get("pending"), bool)):
            raise ManifestUnknown("the stored rewrite marker does not parse")
        out[table] = {"pending": carried["pending"],
                      "record": _carried_record(carried.get("record"))}
    return out


def serialize(entries: dict, rewrite=None, owner=None):
    """The stored value for ``entries`` (sorted) and, during a re-provision,
    its rewrite marker's carried copy and ``owner``; ``None`` for neither (the
    property is removed)."""
    if not entries and rewrite is None:
        return None
    out: dict = {"tables": {t: {"token": e.token, "pending": e.pending}
                            for t, e in sorted(entries.items())}}
    if rewrite is not None:
        marker: dict = {"tables": {t: rewrite[t] for t in sorted(rewrite)}}
        if owner is not None:
            marker["owner"] = owner
        out[REWRITE_KEY] = marker
    return json.dumps(out, separators=(",", ":"))


def from_rows(rows) -> tuple:
    """``(raw, {table: Entry})`` from a :data:`READ_MANIFEST` answer. No row
    (no System node), an unparsable value or an unparsable rewrite marker is
    :class:`ManifestUnknown`."""
    rows = list(rows or ())
    if not rows or not isinstance(rows[0], dict):
        raise ManifestUnknown("no System row")
    raw = rows[0].get("row_exclusion_manifest")
    entries = parse(raw)
    rewrite_of(raw)
    return raw, entries


def read(system_id: str, *, query) -> tuple:
    """``(raw, {table: Entry})`` for the system; raises on any failure
    (:class:`ManifestUnknown` or the read's own error)."""
    return from_rows(query(READ_MANIFEST, {"sid": system_id}))


def check_rewrite(raw, *, during_rewrite: bool = False, owner=None):
    """The stored value's rewrite marker (:func:`rewrite_of`), checked
    against who writes: a steward write (``during_rewrite`` false) raises
    :class:`RewriteInProgress` while a marker stands; the re-provision's own
    restore (``during_rewrite`` true) raises :class:`ManifestWriteFailed`
    once its marker is gone (another run ended it) or, given its ``owner``,
    once another run has taken it over."""
    marker = rewrite_of(raw)
    if marker is not None and not during_rewrite:
        raise RewriteInProgress("a re-provision is rewriting this system's schema")
    if marker is None and during_rewrite:
        raise ManifestWriteFailed("the schema rewrite marker is gone")
    if during_rewrite and owner is not None and rewrite_owner(raw) != owner:
        raise ManifestWriteFailed("another run owns the schema rewrite marker")
    return marker


def update(system_id: str, change, *, query, write, during_rewrite: bool = False) -> dict:
    """Apply ``change`` (``{table: Entry}`` to a new map) to the system's
    manifest with a compare-and-set, re-reading on a concurrent change; the
    new map. ``ManifestUnknown`` when it cannot be read (a corrupt value is
    never overwritten), :class:`RewriteInProgress` while a re-provision's
    marker stands (:func:`check_rewrite`; the marker is kept by a write made
    ``during_rewrite``), :class:`ManifestWriteFailed` when the write does not
    land. ``write`` answers the written rows (``None`` when the graph refused
    it)."""
    for attempt in range(SWAP_ATTEMPTS):
        raw, entries = read(system_id, query=query)
        marker = check_rewrite(raw, during_rewrite=during_rewrite)
        new = change(dict(entries))
        if new == entries:
            return entries
        if _swap(system_id, raw, serialize(new, rewrite=marker, owner=rewrite_owner(raw)),
                 write, attempt):
            return new
    raise ManifestContended("the manifest kept changing under a concurrent writer")


def _swap(system_id: str, expected, value, write, attempt: int = 0) -> bool:
    """ONE compare-and-set of the stored value (:data:`SWAP_MANIFEST`): True
    when it wrote, False when the value had moved, or when the graph aborted
    the write on a concurrent modification (``db.GraphWriteConflict``: it
    did not run, so the caller reads again and resends, after a short pause,
    :func:`_conflict_pause`); :class:`ManifestWriteFailed` when the graph
    refused it, or before it is sent when the value would be longer than a
    read accepts (:data:`MAX_MANIFEST_CHARS`), which would make the manifest
    unknown to every reader."""
    if value is not None and len(value) > MAX_MANIFEST_CHARS:
        raise ManifestWriteFailed("the manifest would exceed its stored bound")
    try:
        rows = write(SWAP_MANIFEST, {"sid": system_id, "expected": expected or "",
                                     "manifest": value})
    except Exception as exc:  # noqa: BLE001 - only a conflict is read as "moved"
        if not is_write_conflict(exc):
            raise
        _conflict_pause(attempt)
        return False
    if rows is None or rows is False:
        raise ManifestWriteFailed("the manifest write was refused")
    return bool(rows)


def is_write_conflict(exc: BaseException) -> bool:
    """Whether ``exc`` is the graph aborting a write on a concurrent
    modification (``db.GraphWriteConflict``), which did not run."""
    try:
        from .db import GraphWriteConflict
    except ImportError:   # the store imported without the app runtime
        return False
    return isinstance(exc, GraphWriteConflict)


#: The longest pause, in seconds, before a write the graph aborted on a
#: concurrent modification is read again and resent.
CONFLICT_PAUSE_S = 0.25


def _conflict_pause(attempt: int) -> None:
    """A short jittered pause before a write the graph aborted on a
    concurrent modification is read again and resent, longer each attempt,
    so two writers racing on one value do not keep colliding."""
    import random
    import time
    time.sleep(CONFLICT_PAUSE_S * min(1.0, (attempt + 1) / SWAP_ATTEMPTS) * random.random())


def begin_rewrite(system_id: str, carry, *, owner: str, query, write) -> dict:
    """Mark a re-provision's schema rewrite on the System node BEFORE its
    delete, in ONE compare-and-set of the manifest's stored value: the
    entries unchanged plus the rewrite marker, owned by ``owner``, holding
    ``carry(entries)``, the carried copy ``{table: {"pending", "record"}}``
    of every listed table. The write is conditional on the value read just
    before the copy was taken, so a steward write that moved it meanwhile
    makes this read and copy again. A marker already standing (a crashed or
    failed run's) keeps its stored copy as it is; this run takes it over with
    one compare-and-set that names ``owner``, so the run that wrote it can no
    longer remove it, and the stored copy is returned. The caller holds the
    system's provision lease, so the run that wrote that marker no longer
    does. Raises when the manifest cannot be read (``ManifestUnknown``) or
    the marker is not written (:class:`ManifestWriteFailed`)."""
    for attempt in range(SWAP_ATTEMPTS):
        raw, entries = read(system_id, query=query)
        marker = rewrite_of(raw)
        if marker is not None:
            if rewrite_owner(raw) == owner:
                return marker
            if _swap(system_id, raw, serialize(entries, rewrite=marker, owner=owner), write,
                     attempt):
                return marker
            continue
        carried = carry(dict(entries))
        if _swap(system_id, raw, serialize(entries, rewrite=carried, owner=owner), write,
                 attempt):
            return carried
    raise ManifestContended("the manifest kept changing under a concurrent writer")


def end_rewrite(system_id: str, change, *, owner: str, query, write) -> dict:
    """The LAST write of a re-provision's restore: ONE compare-and-set that
    applies ``change`` to the entries and removes the rewrite marker ``owner``
    owns; the new map. :class:`ManifestWriteFailed` when the marker is gone
    or another run has taken it over (that run removes it), and otherwise as
    :func:`update`."""
    for attempt in range(SWAP_ATTEMPTS):
        raw, entries = read(system_id, query=query)
        check_rewrite(raw, during_rewrite=True, owner=owner)
        new = change(dict(entries))
        if _swap(system_id, raw, serialize(new), write, attempt):
            return new
    raise ManifestContended("the manifest kept changing under a concurrent writer")


def listing(table: str, token: str, *, pending: bool):
    """A ``change`` for :func:`update`: list ``table`` with ``token``."""
    def change(entries: dict) -> dict:
        entries[table] = Entry(token, pending)
        return entries
    return change


def settling(table: str, token: str):
    """A ``change`` that clears ``table``'s pending flag while it still holds
    ``token`` (already settled: no change). :class:`TokenLost` when the entry
    is gone or holds another writer's token."""
    def change(entries: dict) -> dict:
        entry = entries.get(table)
        if entry is None or entry.token != token:
            raise TokenLost(f"the manifest entry of {table!r} no longer holds this write's token")
        if entry.pending:
            entries[table] = Entry(token, False)
        return entries
    return change


def removing(table: str, token: str):
    """A ``change`` that removes ``table`` while it still holds ``token``.
    :class:`TokenLost` when the entry is gone or holds another writer's
    token."""
    def change(entries: dict) -> dict:
        entry = entries.get(table)
        if entry is None or entry.token != token:
            raise TokenLost(f"the manifest entry of {table!r} no longer holds this write's token")
        entries.pop(table)
        return entries
    return change
