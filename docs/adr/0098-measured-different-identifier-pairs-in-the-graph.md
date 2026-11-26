<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# ADR-0098: Measured different-identifier column pairs live in the knowledge graph

## Status

**Proposed: 2026-10-18.** Verified offline (unit lane) and on a local PostgreSQL 16
server with the graph faked. The openCypher statements have not run on Neptune or
Memgraph yet. Not yet deployed to an AWS account.

## Context

Onboarding measures pairs of same-named columns of one system that are both mapped as
identifiers of one concept but hold different identifiers: each column's values have
their own shape and none was found in the other column
(`agents/distinct_identifiers.py`, `measure`). Until now that fact reached agents only
as prose baked into the schema text at provision
(`schema_format.with_distinct_identifiers`), and the system agent's identifier-shape
guard re-parsed that prose (`system_agent._identifier_shapes`,
`identifier_shape_guard.context_from`). The guard refuses only the two lookups that
cannot match: a value of one column's shape compared with the other column, and an
equality between the two columns.

The product owner's rule is that deterministic connections between columns, within a
system or across systems, live in the knowledge graph and not in prompt prose.

## Decision

1. **One edge per measured pair.** Provision step e writes
   `(:Field)-[:DIFFERENT_IDENTIFIER_FROM {concept, read_a, read_b, signature_a,
   signature_b, measured_at, basis}]->(:Field)` from the Field of the table that sorts
   first to the other (`onboard._dp_store_distinct_identifiers`). Each end is matched by
   Field id through the system's own `HAS_SCHEMA` / `HAS_FIELD` path, so the write
   creates nothing for a column that is not part of that schema. `basis` is `measured`;
   `measured_at` is UTC ISO 8601 to the second, as for a measured internal JoinPath.
   `signature_a` / `signature_b` are each column's measured shape: the system agent's
   IDENTIFIER-MISS signature (`system_agent._shape_signature`) as compact JSON
   (`distinct_identifiers.encode_signature`). They are stored for every measured pair,
   whether or not an example of the shape can be printed in the schema note: a shape
   with a space or a one-character symbol segment is still kept. A measured pair without
   both signatures is never written. A stored row without two signatures that function
   could produce (one missing or invalid, or a shape-only edge an earlier build wrote)
   is not a record: the record read raises `distinct_identifiers.UnusableRecordError`
   rather than skip it ("no record") or hand on a record without signatures, which
   would switch the guard's note fallback off.
2. **An edge, not a Field property.** The fact is about two columns. An edge holds it
   once, with both ends as real Field nodes, and is traversable. A property would name
   the partner by string on one or both Fields and could disagree with itself.
3. **The note is a rendering of the graph.** After the writes, provision reads the
   system's records back (`onboard.DISTINCT_IDENTIFIER_RECORDS_READ`) and renders the
   schema note from that read: the same text as before when the record matches. The
   shape examples in the note are rendered from the stored signatures
   (`distinct_identifiers.shape_example`), and left out when one cannot be printed
   there. A measured pair the read does not return is not named, and the provision
   payload lists it as `distinct_identifier_not_stored`. Graph errors are not swallowed:
   a refused write raises inside the provision, and an unreadable graph raises
   `GraphQueryError`, so a note is never baked that the graph does not hold.
4. **The guard reads the same records.** The records ride to the system agent in its
   agent config (`distinct_identifiers`, beside `concept_mappings`; carried forward by a
   connection re-sync). The guard is built from their signatures, printable or not.
   Only an agent configured with no record, one provisioned before this change, parses
   the baked note, and it logs that it did; so does one whose configured records are
   not all usable records (`distinct_identifiers.signed_records`), which never switch
   that fallback off. No new prompt text: the only agent-facing text about the pair is
   the existing note.
5. **Replace and remove.** b0 of every PostgreSQL provision deletes the records on the
   system's Field nodes before anything is written, and so does the system removal
   (`_RESET_GRAPH_DELETES`). The Field deletes already drop the records on the Fields
   they reach; the explicit delete also covers a Field no longer under its SchemaEntry.
6. **Metamodel.** `DIFFERENT_IDENTIFIER_FROM` is documented in `agents/metamodel.py`
   and `docs/graph-shape-contract.md` (ownership rule). Its entry describes the edge and
   carries no guidance or rule.
7. **A restore reads the records too.** The startup restore (`server._restore`, also run
   by the graph-snapshot repair) rebuilds a PostgreSQL system's schema from the graph
   and, when the runtime is gone or not READY (or in local mode), rewrites its agent
   config. It reads the records with the provision's read
   (`onboard.read_distinct_identifier_records`), renders the note from them onto the
   restored schema and passes them as `distinct_identifiers`, so the guard survives. A
   record read that fails, or returns a row that is not a usable record, leaves that
   one system unregistered (logged) instead of restoring it without a guard.
8. **An upgrade restore carries the old guard forward.** When the graph holds no record
   for a PostgreSQL system (it was provisioned before this change), the cloud restore
   reads its current `adt/agent-config/<id>` and, for the same host and database,
   carries forward its `distinct_identifiers` when every one is a usable record, else
   its baked note when the fallback parser reads at least one shaped pair from it and
   each such pair's columns are in the restored schema. Nothing is migrated into the
   graph here; the next provision stores the pairs. A secret that exists but can't be
   read (any error but "not found") is never rewritten: a READY runtime still
   registers by the fast path, which writes no config, and otherwise that one system
   stays unregistered (logged) until a later restore can read it. Local mode keeps no
   config across a restart, so there is nothing to carry.

## Consequences

- The orchestrator's live-metamodel section gains one descriptive edge line. Without it
  the drift check would list the edge name there anyway.
- A cloud restart reads `adt/agent-config/<id>` once for each PostgreSQL system the
  graph holds no record for, also when its runtime is READY and nothing is rewritten.
- The Step Functions provisioner (`agents/provisioner`) is unchanged. Its
  `create_runtime` writes `adt/agent-config/<id>` from its connection input alone, with
  no `tables_desc`, `concept_mappings` or records, as it did before this ADR. No caller in
  this repository starts it: the onboarding chat hides the onboarding gateway's
  `provision_system` and runs the wizard's provision in the app.
- Athena, SiteWise, i3X, REST and MCP systems are unchanged: only PostgreSQL measures.

## Update 2026-10-29

The Step Functions provisioner was removed (it had no caller). The `agents/provisioner` handlers it ran stay, used by `scripts/provision_runtimes.py`.
