<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# Graph-shape contract (VERIFIED—system: `cmms`, 2026-06-11)

> **Maintainer documentation**—working notes for contributors and AI agents,
> not part of the user-facing guide.

## 1. Purpose & status

Contract that S1.x resolver slices may depend on; refs ADR 0001 (deterministic resolver),
ADR 0002 (relationships in graph, formulas in metric layer). **Status: VERIFIED against live
system `cmms` (a deployed Amazon Neptune Analytics graph, example ID `g-example1234`, us-east-1) on 2026-06-11.** Evidence:
`agentic_digital_thread/tests/audit/last-run-report.json`.

This document pins the **shape** of the onboarding knowledge graph (node labels, edge
types, and key properties), so downstream resolver slices can build against a stable target.
Every claim below is grounded in a specific writer or schema docstring in the codebase
(citations inline) and, where labeled VERIFIED/observed, in the reconciliation audit run.

**i3X telemetry-layer extension (2026-07, static):** the `:Property` layer added by the
i3X port—the `Property` node (§2), the `HAS_PROPERTY` / `MODELED_BY` edges and the
`Asset`-level `LOCATED_AT` / `FROM_SYSTEM` endpoints (§3), and fact family (d) (§4)—is
documented from its writers (`agentic_digital_thread/agents/onboard_i3x.py`) and asserted
by the extended audit script, but is **not yet audit-verified**: it needs a reconciliation
run against a deployed environment with an onboarded `SiteWise (i3X)` system (the Neptune
graph is VPC-private, ADR-0024). Until then these items carry the doc's static/provisional
labels, not VERIFIED. *(Operationally exercised live 2026-07-23: a UI-onboarded
`SiteWise (i3X)` system's walker wrote 1 root / 118 `Asset` / 621 `Property` nodes with
zero warnings, and the orchestrator resolved element IDs + site linkage through the layer
in chat—the formal audit reconciliation run remains pending.)*

**Graph-metamodel + equipment-mapping extension (2026-07, static):** the graph
now **describes its own shape**: `:MetaNode` / `:MetaEdge` nodes (§2) document every node
label and edge type, written **idempotently at every provision** by
`agents/metamodel.py::write_metamodel` (called from `onboard.py`
`_provision_knowledge_post_pass`, non-fatal). The orchestrator fetches this metamodel at
cold start (`fetch_metamodel_text`) and appends it to its system prompt, so a new edge
type introduced by onboarding is traversable in the very next session with no prompt
edit and no redeploy. **Ownership rule (quoted from the module docstring):** *"code that
introduces a new label or edge type MUST add its entry here (and to
docs/graph-shape-contract.md—the unit test tests/unit/test_metamodel.py pins the two
vocabularies together). The fetch side also introspects the live graph and lists any
edge types missing from the metamodel, so drift is visible instead of silent."* The
machine-readable vocabulary block in §6 is set-equality pinned to
`agents/metamodel.py` by `tests/unit/test_graph_contract_metamodel_pin.py`. The same
round adds the equipment identity bridge: `ValueMapping.to_element_id`/`.to_path` fields
and the `(ValueMapping)-[:MAPS_TO_ASSET]->(Asset)` edge (§3), asserted by audit fact
family (e) (§4). Like the i3X layer, these are **not yet live-verified**.

**Audit reconciliation summary (system `cmms`):** all three fact families PASS.
- (a) field→concept: **61 `MAPS_TO` edges** across the cmms schema, **14 distinct concepts**,
  **0 edges missing `role`, 0 missing `confidence`**—role/confidence present on every edge.
- (b) authority: present and **queryable by the `AuthorityRule.concept` property**; the
  `ABOUT_CONCEPT` edge is **absent (0 edges)** at onboard time—confirms the lazy-backfill
  hazard (read the property, not the edge).
- (c) joins: **5 `JoinPath`s**, all internal to cmms; **property form and `FROM_SYSTEM`/
  `TO_SYSTEM` edge form agree** (5 = 5 = 5).
- identity probe: **`RESOLVES` = 0 and `LINKS_CONCEPT` = 0**—cmms is a lone/first system
  with no `IdentityRule`s, so the RESOLVES-vs-LINKS_CONCEPT mismatch (§3) is a
  code-level hazard **not yet observable**; it requires a ≥2-system onboard to confirm live.
- value read: not exercised locally (no route to private Amazon RDS from the audit host)—best-
  effort, non-blocking; deferred to S0.4.

Ubiquitous-language names (per the [glossary](glossary.md) "Meaning"/"Lineage"): **Concept** (canonical
meaning), **Mapping** (field→concept relationship), **Authority** (which system owns a
concept), **Join Path** (stored field-to-field relationship traversed deterministically),
**Value Mapping** (row-level value translation). These map to the `Concept`, `MAPS_TO`,
`AuthorityRule`, `JoinPath`, and `ValueMapping` graph constructs respectively.

Primary sources of truth:
- `agentic_digital_thread/agents/orchestrator.py`—the two embedded schema docstrings
  (`explore_graph`, lines ~25-40; Step-3 prompt block, lines ~199-216).
- Writers: `agentic_digital_thread/agents/onboard.py` (provision step), `agents/onboard_i3x.py`
  (the i3X hierarchy walker + post-schema bridge—the Asset/Property instance layer),
  `agents/server.py` (graph-repair / `ABOUT_CONCEPT` backfill), `agents/self_learning.py`
  (ValueMapping / disambiguation), `agents/resolve.py` (`RESOLVES`).

No design *decisions* are made here. Where code is internally inconsistent (for example an edge
read under one name and written under another), this doc **reports both** and defers the
decision to the post-execution review, seeded into
the graph audit (ADR-0011).

---

## 2. Node types

Full inventory with key properties. "Writer" cites the cypher that creates/​sets the node;
"⚠" flags a property or node this static pass could **not** confirm against an onboarding
writer (to be checked against the live graph in a reconciliation audit).

| Node | Key properties (as written) | Writer (file:line region) |
|------|------------------------------|----------------------------|
| **Plant** | `{id, name}` | `onboard.py` ~1513 `MERGE (p:Plant {id}) SET p.name` |
| **System** | `{id, name, plant, schema, status, connection_type, query_guidance, capabilities, tool_prefix, agent_model, asset_coverage?, asset_coverage_at?, schema_rewrite_started?, schema_rewrite_failed?}`—`schema_rewrite_started` is the epoch milliseconds a re-provision began rewriting the system's schema subgraph (set before the b0 delete, and only once the write returned this run's value: otherwise the provision stops before deleting anything; at b3c, after the emergent-concept landing and the retired-link sweep that are the last steps changing the system's MAPS_TO edges, the run removes its own mark, or, when the window did not settle because a step raised, the emergent landing did not land every write or the sweep failed every attempt, replaces it with `schema_rewrite_failed`, the epoch milliseconds of that failure; a settled rewrite also removes an earlier failed marker). Another system's provision reads both in one statement with the field whose MAPS_TO edges it re-measures (`onboard._TARGET_FIELD_SNAPSHOT_CYPHER`): while the mark is younger than `onboard._SCHEMA_REWRITE_STALE_S` it waits until one per-run deadline, while the failed marker is that young its re-measurement could not run; a reader that sees an older one removes that exact value. Both are operational state: the graph view, node details, `explore_graph` results and graph snapshots leave them out, and `explore_graph` refuses a query that names one (`graph_internal_props`, `cypher_union_check.names_property`); a query that reads properties by a computed key (`UNWIND keys(s) AS k RETURN s[k]`) is not refused and can read them (a known limit: they carry no answer data). `asset_coverage` is one compact JSON string (Neptune Analytics property values are primitives): for each site (or each root, when the system's hierarchy has no `site` level) how many of the system's assets of each stored `level` sit under it, at most 8 anchors plus `more_sites`, and `unplaced` when assets whose parent could not be told were left out; `asset_coverage_at` is its ISO-8601 UTC write time. Absent for a system with no `:Asset` nodes. | `onboard.py` ~1538 `MERGE (s:System {id}) SET s.name, s.plant, s.schema, s.status='onboarded', s.connection_type, s.query_guidance, s.capabilities, s.tool_prefix, s.agent_model`; `asset_coverage`/`asset_coverage_at`: `asset_role_pass.py` `classify_system` (sole writer, last write of the pass, `MATCH (s:System {id}) SET s.asset_coverage, s.asset_coverage_at`; REMOVEd when the system has no assets left and by `restore_system`). Read by the route seed (`orchestrator._ROUTE_SEED_INVENTORY_CYPHER`/`_ROUTE_SEED_CYPHER`). `schema_rewrite_started`: written by `onboard._mark_schema_rewrite` (b0, `_begin_schema_rewrite`), REMOVEd by `_finish_schema_rewrite` (b3c, `_dp_schema_rewritten`, owner-safe: only the run's own mark) and, once stale, by `_fresh_marker`'s compare-and-clear; `schema_rewrite_failed`: written and REMOVEd by `_finish_schema_rewrite`, and once stale by `_fresh_marker`; both read by `_target_field_snapshot`. Goes with the node on per-system reset/offboard (`DETACH DELETE s`). |
| **SchemaEntry** | `{id, name, system_id, discovery_complete?, discovered_columns?}`: `name` = table name; the two optional props are Amazon Athena discovery completeness (ADR-0077 D3; absent = unknown) | `onboard.py` ~1559 `MERGE (se:SchemaEntry {id}) SET se.name, se.system_id`; physical-schema writer home `schema_fields.py` (shared by `onboard._dp_athena_physical_fields` and `schema_repair.materialise_drift`, ADR-0077) |
| **Field** | `{id, name, schema_id, system_id, disambiguation?}`—`name` = column name; `disambiguation` set lazily | `onboard.py` ~1573 `MERGE (f:Field {id}) SET f.name, f.schema_id, f.system_id`; physical columns (`data_type`, `is_partition`) via `schema_fields.py` (provision b2a + additive drift accept, ADR-0077); `self_learning.py` ~231 `SET f.disambiguation` |
| **Concept** | `{id, name, description, isa95_level?, source?, created_by_system?, created_at?, created_by_run?}`—onboard sets `name=description=id`; `isa95_level`/`source` come from the core-concept seed, not onboarding; `created_by_system`/`created_at`/`created_by_run` are set ON CREATE by an onboarding's provision (the system and the one provision run that created the concept — `created_by_run` is what the concept suggestions check `onboard_reconcile` reads, so a re-onboard's fresh run id never resurfaces an earlier run's or a removed system's concepts; `created_by_system` stays as durable creator metadata), never overwritten | `onboard.py` ~1596 `MERGE (c:Concept {id}) ON CREATE SET c.name=$cid, c.description=$cid, c.created_by_system, c.created_at, c.created_by_run`; read w/ `isa95_level`/`source` in `graph_analytics.py` ~22/27 |
| **IdentityRule** | `{id, concept, source_system, source_field, source_pattern, target_system, target_field, target_pattern, confidence}`. Pattern grammar: `{name}` (conventionally `{id}`) is the captured identifier core; `{*}` is an uncaptured part that varies across several records sharing one core, so a translation through it is a candidate set, never one value; everything else is literal ([ADR-0086](adr/0086-identity-rule-wildcard-for-one-to-many-cores.md)). The bare `"*"` direct-match convention is separate. `pattern_grammar = 'literal'` marks a rule written with that grammar (every writer since 2026-10-21); a row without it is read as before (the resolver reads its literal text as a regular expression). | `onboard.py` ~1700 `MERGE (ir:IdentityRule {id}) SET ir.concept, ir.source_system, ir.source_field, ir.source_pattern, ir.target_system, ir.target_field, ir.target_pattern, ir.confidence`; also `self_learning.py` ~196 |
| **JoinPath** | `{id, from_system, from_table, from_field, to_system, to_table, to_field, transform, method}`—⚠ `description` is in the task spec/​orchestrator docstring but the **internal-FK writer sets `transform`/`method`, not `description`**. Evidence (additive; readers key on `method`, which is unchanged except that a join written from a direct identity rule reads `identity_rule_match`, not `foreign_key_match`, since 2026-10-19; the readers that key on `foreign_key_match` read only joins inside one system): every provision writer sets `basis` (`declared_foreign_key`, `column_name_pattern`, `measured_reference`, `value_overlap`, `same_row_identifiers`, `identity_rule`) plus what applies of `constraint_name`, `overlap_count`/`overlap_ratio`, `pair_count`/`unique_constraints`, `rule_id`/`rule_method`, `reference_count`/`key_count`/`measured_at` (a `*_id` name-pattern join b3 measured: every referencing value found in a target column with one distinct value per row, and in no other table the name could mean; read at `measured_at`, UTC; evidence for the identifier-forms pairing only, every other reader treats it as `column_name_pattern`) or `reference_check` (why a name-pattern join stayed `column_name_pattern`) (`agents/connection_evidence.py`). A JoinPath without `basis` predates the evidence. | `onboard.py` ~1624 `MERGE (jp:JoinPath {id}) SET jp.from_system, jp.from_table, jp.from_field, jp.to_system, jp.to_table, jp.to_field, jp.transform='direct', jp.method='foreign_key_match'` |
| **ValueMapping** | `{id, from_system, from_field, from_value, to_system, to_field, to_value, concept, confidence, method, to_element_id?, to_path?}`—equipment mappings (`concept='equipment'`) carry `to_element_id` (the target Asset's telemetry UUID = `Asset.source_id`; `""` when unresolved) and `to_path` (the Asset's slash hierarchy path) | **Two sanctioned writers:** `self_learning.py` ~244 `MERGE (vm:ValueMapping {id}) SET vm.from_system, vm.from_field, vm.from_value, vm.to_system, vm.to_field, vm.to_value, vm.concept, vm.confidence, vm.method='user_confirmed'` (no `to_element_id`/`to_path`); and the equipment-VM provision post-pass `onboard.py` `_persist_equipment_value_mappings` ~5330 (same SET shape **plus** `vm.to_element_id, vm.to_path`) |
| **Asset** | `{source_id, name, asset_type, system, level, path, property_count?}`—`source_id` is the merge key. `property_count` = the asset's OWN telemetry-channel count, stamped on **every** asset either walker writes (the i3X walker and the AWS IoT SiteWise (Direct) walk `onboard.py` `_dp_sitewise_walk`); **0 = structural-only** (an asset holding no telemetry of its own—for example a site or line container). `level` is TYPE-derived (`onboard_i3x._level_for`: Enterprise/Site/Area/Line by model name, else `equipment`/`component` by depth) in both walkers. After provision the role pass (`asset_role_pass.py`) adds `role` (`enterprise`/`site`/`area`/`work_center`/`work_unit`/`equipment`/`equipment_part`/`data_stream`/`unclassified`, one per asset model of a system, `asset_roles.classify_models`), and `depth` (distance from the asset's root, root = 0), and rewrites `level` from the role (`work_center`→`line`, `work_unit`→`work_unit`, `equipment_part`/`data_stream`→`component`; an `unclassified` model keeps the walker's label). A SiteWise (Direct) orphan root (a TOP_LEVEL asset no Site reaches) has no `HAS_PARENT` to any Site. | **Two sanctioned Asset-layer writers:** `onboard.py` `_dp_sitewise_write_asset` ~5091 (SiteWise Direct, driven by `_dp_sitewise_walk`) `MERGE (a:Asset {source_id}) SET a.name, a.asset_type, a.system, a.level, a.walker_level, a.path` (plus `SET a.property_count` in `_dp_sitewise_walk`); both walkers stamp `walker_level` = the `level` they wrote, which is what the role pass restores with `ADT_ASSET_ROLES=off`; and the i3X walker `onboard_i3x.py` `provision_i3x_hierarchy` ~257 (same SET shape, plus `SET a.property_count` ~221/~252). No other code may MERGE or delete `:Asset`; the role pass only SETs `role`/`depth`/`level` (and `walker_level` on graphs walked before the stamp existed), or on rollback restores `level` from `walker_level` and REMOVEs `role`/`depth`, on assets the walkers wrote. |
| **Property** | `{source_id, name, parent_id, system, data_type, path}`—`source_id` is the merge key; `parent_id` = owning asset's `source_id`; `data_type` = the URN tail (`DOUBLE`/`INTEGER`/`BOOLEAN`/`STRING`/`STRUCT`). **Metadata ONLY**—property *values* are never stored (rules-engine constraint). | `onboard_i3x.py` `_write_property_child` ~106 `MERGE (p:Property {source_id}) SET p.name, p.parent_id, p.system, p.data_type, p.path`; the SiteWise (Direct) walk writes the same shape (`onboard.py` `_dp_sitewise_write_properties` ~5115, plus `p.unit`)—these two are the only Property writers. Deleted per-system on reset/offboard (`onboard.py` ~558 `MATCH (p:Property {system:$sid}) DETACH DELETE p`) and included in the global-reset label list. |
| **AssetModel** | `{name}` | `onboard.py` ~1477 `MERGE (m:AssetModel {name})`; also MERGEd (same shape) by the i3X walker `onboard_i3x.py` ~150/~235. Orphan AssetModels (no remaining `INSTANCE_OF` inbound) are deleted on per-system reset (`onboard.py` ~560). |
| **AssetModelRole** | `{system, model, role, source, evidence, needs_review, status, updated_at}`—`(system, model)` is the merge key (`AssetModel` is shared by name across systems, its role is not). `source` is the deciding rule (`steward`/`name`/`value_evidence`/`structure`/`identifier_gap`/`unresolved`; `identifier_gap` = equipment flagged because, in every parent asset holding it, a sibling is matched by another system's equipment identifier and it is not, so it may be the container's own data); `status='steward'` marks a steward-confirmed role, read as input and never rewritten; anything else is `derived`. No API writes a steward node yet: one is set by hand (an audited override route is a planned follow-up). | `asset_role_pass.py` `classify_system` (sole writer) `MERGE (mr:AssetModelRole {system, model}) SET …` + `FROM_SYSTEM`. Per-system reset deletes the derived ones (a steward role survives a re-onboard); global reset wipes all. |
| **AuthorityRule** | `{id, concept, system_id, rule, priority}`—`priority` hardcoded `1` | `onboard.py` ~1912 `MERGE (ar:AuthorityRule {id}) SET ar.rule, ar.system_id, ar.concept, ar.priority=1` |
| **MetricDefinition** | `{id, label, unit, kind, definition_json, synonyms_json, source, created_by, version, created_at, updated_at}`—`id` is the merge key. STANDALONE node, **no edges** (ADR-0050 D1 / ADR-0066 D1): a deployment-defined deterministic metric (formula, denominator basis, valid range) resolved by the metric layer, NOT prompt text. `definition_json` is the canonical validated document; `label`/`synonyms_json` are UNTRUSTED customer text sanitized+validated at write (`untrusted_text`). A code-registry id collision is NEVER written (rejected at propose and accept, skipped+reported at read). | `self_learning.py` `_apply_metric_definition` (accept path of `proposed_type='metric_definition'`) via `metric_definition_store.write_definition`—MERGE keyed on `id` then EXACT byte readback (mismatch raises; the proposal stays pending). Sole writer (ONE lifecycle, ADR-0003). Read by `metric_definition_store.load_definitions` (TTL snapshot; cold failure raises, warm failure serves stale—never fabricates emptiness). |
| **ConceptOverlay** | `{id, concept_id, pack_id, source, examples, synonyms, value_patterns, note, created_at, created_by, updated_at, version}`—`id` = `ovl:<pack_id>:<concept_id>` (the merge key). STANDALONE node, **no edges** (ADR-0050 D1—node properties are customer data; the edge taxonomy is engine-invariant): linked to its Concept by the `concept_id` property only. Text fields are UNTRUSTED customer/pack content—sanitized+validated at write (`untrusted_text`, T1.7) and fence-rendered with an origin label at map-prompt composition (`catalog.compose_concept_block`). The target Concept's `source` stays `core`—overlays never fork a concept (ADR-0020). Uninstall = delete by `pack_id`. | `self_learning.py` `_apply_concept_overlay` (accept path of `proposed_type='concept_overlay'`) `MERGE (o:ConceptOverlay {id}) SET o.concept_id, o.pack_id, o.source, o.examples, o.synonyms, o.value_patterns, o.note, o.created_at, o.created_by, o.updated_at, o.version`—sole writer (ONE lifecycle, ADR-0003). Read by `catalog.get_concept_overlays` (lazy, graph-unavailable ⇒ absent). |
| **Correlation** | `{id, pattern, confidence, concept_a, concept_b}`—⚠ **read-only referenced** (`server.py` ~719, `graph_analytics.py` ~125); no onboarding writer found in the audited files | listed in `orchestrator.py` docstring node inventory ~26 |
| **DecisionBoundary** | `{id, concept, threshold, action}`—⚠ **read-only referenced** (`server.py` ~720, `graph_analytics.py` ~131); no onboarding writer found in the audited files | listed in `orchestrator.py` docstring node inventory ~26 |
| **OnboardingLog** | `{id, system_id, timestamp, rules_created, tables_mapped}` | `onboard.py` ~1928 `CREATE (ol:OnboardingLog {id, system_id, timestamp, rules_created, tables_mapped})` |
| **MetaNode** | `{label, description, key_properties}`—`label` is the merge key; one row per documented node label. The graph's self-describing vocabulary (§1 graph-metamodel note). | `metamodel.py` `write_metamodel`—one parameterized `MERGE (mn:MetaNode {label}) SET mn.description, mn.key_properties` per `META_NODES` entry; **idempotent, written at every provision** (`onboard.py` `_provision_knowledge_post_pass`, non-fatal). Sole writer. |
| **MetaEdge** | `{name, from_label, to_label, description, guidance}`—merge key is `{name, from_label}`; `guidance` is an optional cypher usage hint rendered into the orchestrator prompt. | `metamodel.py` `write_metamodel`—one parameterized `MERGE (me:MetaEdge {name, from_label}) SET me.to_label, me.description, me.guidance` per `META_EDGES` entry; same idempotent provision write. Sole writer. |

Notes:
- The AWS IoT SiteWise (Direct) path writes a **reduced** System node (`onboard.py` ~1436:
  `SET s.name, s.status='onboarded', s.connection_type='SiteWise (Direct)'`), so
  `query_guidance`/`capabilities`/`tool_prefix`/`agent_model` may be absent for that
  connection type. The full property set above is guaranteed only for the
  catalog-driven PostgreSQL/​MCP onboarding path.
- The i3X walker likewise MERGEs an **early, reduced** System node
  (`onboard_i3x.py` ~270: `SET s.name, s.status='onboarded',
  s.connection_type='SiteWise (i3X)'`) so its Asset/Property `FROM_SYSTEM` edges
  have a target; the base provision graph write re-MERGEs the full property set
  afterwards (the walker runs as a post-pass, `onboard.py`
  `_provision_i3x_post_pass` ~5126).
- `status='onboarded'` is set on the System node by every writer and is the property
  the catalog/​audit `pick_target_system` flow keys off.
- `MetaNode`/`MetaEdge` are deliberately **NOT** in `onboard.py::_RESET_NODE_LABELS`:
  the metamodel describes the *vocabulary*, not onboarding-derived data, so it
  survives a global reset and is re-MERGEd (idempotently) by the next provision.
  Transitional edges slated for removal by ADR-0011 (`RESOLVES`, `ABOUT_CONCEPT`)
  are deliberately not documented as MetaEdges—while they still exist in a live
  graph the fetch-side drift check surfaces them, which is the accurate status for
  a to-be-removed edge.

---

## 3. Edge types

| Edge | Properties | Direction / endpoints | Writer or source |
|------|-----------|------------------------|------------------|
| **HAS_SCHEMA** |—| `(System)→(SchemaEntry)` | `onboard.py` ~1561 |
| **HAS_FIELD** |—| `(SchemaEntry)→(Field)` | `onboard.py` ~1581; `schema_fields.py` (ADR-0077) |
| **MAPS_TO** | `{confidence, role}` (+ `method` on edges a later step wrote: `user_corrected`, `minted`, `reuse`, or `measured_mirror` for a provision b3a measure tag taken across a measured join, `agents/measure_equivalence.py`) | `(Field)→(Concept)` | `onboard.py` ~1602 `MERGE (f)-[r:MAPS_TO]->(c) SET r.confidence, r.role` |
| **LOCATED_AT** |—| `(System)→(Plant)`; also `(Asset)→(Plant)` (site-level i3X assets only) | `onboard.py` ~1549 (System); `onboard_i3x.py` `bridge_i3x_schema` ~328 links `level='site'` Assets to a name-matching Plant (normalized-containment match, `'all sites'` excluded) so i3X telemetry shares the plant dimension with the relational systems. |
| **HAS_PARENT** |—| `(Asset)→(Asset)` (child→parent) | **Canonical—and the ONLY—Asset-hierarchy edge, i3X included:** written by `onboard.py` `_dp_sitewise_write_asset` ~5091 and by the i3X walker `onboard_i3x.py` ~146 (`MERGE (c)-[:HAS_PARENT]->(p)`), **traversed** by `orchestrator.py` as `<-[:HAS_PARENT*]-` (descendants of a site). ✅ **F6 resolved**—standardized on this single edge; neither writer emits `HAS_CHILD`. |
| ~~**HAS_CHILD**~~ |—|—| ✅ **Removed (F6)**—the redundant inverse edge is no longer written; the orchestrator traverses the canonical `HAS_PARENT` instead. |
| **INSTANCE_OF** |—| `(Asset)→(AssetModel)` | `onboard.py` ~1477 (SiteWise Direct); `onboard_i3x.py` ~150/~240 (i3X walker, same cypher shape) |
| **HAS_PROPERTY** |—| `(Asset)→(Property)` | `onboard_i3x.py` `_write_property_child` ~116 `MERGE (a)-[:HAS_PROPERTY]->(p)`—one edge per telemetry channel the asset holds directly. The owning asset's `property_count` equals its `HAS_PROPERTY` out-degree (audited, §4d); the orchestrator's `property_count=0` guard reads the **stamp**, not the edges. |
| **MODELED_BY** |—| `(AssetModel)→(SchemaEntry)` | `onboard_i3x.py` `bridge_i3x_schema` ~316 (normalized-name match, post-schema pass—MUST run after the SchemaEntry/Field write or it matches zero). Bridges the instance layer to the concept layer: `Property<-HAS_PROPERTY-Asset-INSTANCE_OF->AssetModel-MODELED_BY->SchemaEntry-HAS_FIELD->Field-MAPS_TO->Concept`. |
| **FROM_SYSTEM** |—| `(JoinPath)→(System)`, `(ValueMapping)→(System)`, `(Asset)→(System)`, `(Property)→(System)`, `(AssetModelRole)→(System)` | `onboard.py` ~1631 (JoinPath); `self_learning.py` ~256 (ValueMapping); `onboard.py` `_dp_sitewise_write_asset` + `onboard_i3x.py` (Asset); `onboard_i3x.py` `_write_property_child` ~119 and `onboard.py` `_dp_sitewise_write_properties` (Property); `asset_role_pass.py` (AssetModelRole) |
| **TO_SYSTEM** |—| `(JoinPath)→(System)`, `(ValueMapping)→(System)` | `onboard.py` ~1632 (JoinPath); `self_learning.py` ~258 (ValueMapping) |
| **LINKS_CONCEPT** |—| `(IdentityRule)→(Concept)`, `(ValueMapping)→(Concept)` | `onboard.py` ~1733 (IdentityRule); `self_learning.py` (ValueMapping) |
| **MAPS_TO_ASSET** |—| `(ValueMapping)→(Asset)`—equipment identity bridge: maintenance-system tag (`vm.from_value`) ↔ historian Asset, one traversal apart from the hierarchy. Written **only** for equipment VMs with a non-empty `to_element_id`; the target Asset's `source_id` equals `vm.to_element_id` (audited, §4e). | `onboard.py` `_persist_equipment_value_mappings` → `_write_equipment_vm`: the one-statement mapping write (`_value_mapping_upsert(..., asset_anchor=True)`) MATCHes the one `(a:Asset {source_id: $aid})` behind a `WITH count(n) AS assets WHERE assets = 1` guard, deletes the mapping's prior `MAPS_TO_ASSET` edges and MERGEs `(vm)-[:MAPS_TO_ASSET]->(a)`; an anchor readback must show exactly that target—guarded on non-empty `to_element_id`; sole writer. |
| **RESOLVES** |—| `(IdentityRule)→(Concept)` | **read** by `resolve.py` ~91; **written** by `resolve.py` ~503/511. ⚠ **To be removed—ADR 0011** names `LINKS_CONCEPT` canonical; this edge is redundant and goes away in S1.x. |
| **APPLIES_TO** |—| `(AuthorityRule)→(System)` | `onboard.py` ~1922 |
| **DIFFERENT_IDENTIFIER_FROM** | `{concept, read_a, read_b, signature_a, signature_b, measured_at, basis}` | `(Field)→(Field)`: two same-named columns of ONE system, both mapped as identifiers of the same concept, measured at onboarding to hold different identifiers (each column's values have their own shape; none was found in the other column). From the Field of the table that sorts first. `signature_a`/`signature_b` are each column's measured shape, the IDENTIFIER-MISS signature as compact JSON, stored whether or not the schema note can print an example of it; `basis` is `measured` (ADR-0098). | `onboard.py` `_dp_store_distinct_identifiers` (provision step e, PostgreSQL only): each end MATCHed by Field id through `(:System {id:$sid})-[:HAS_SCHEMA]->(:SchemaEntry)-[:HAS_FIELD]->`, then `CREATE`; sole writer. Read back by `DISTINCT_IDENTIFIER_RECORDS_READ` (`read_distinct_identifier_records`; the provision and the startup restore): the baked schema note and the system agent's identifier-shape guard are built from that read, and a row without two valid signatures fails it (`UnusableRecordError`). Removed by `_DISTINCT_IDENTIFIER_RECORDS_DELETE` (b0 of every PostgreSQL provision, and the system removal) and by the Field deletes. |
| **ABOUT_CONCEPT** |—| `(AuthorityRule)→(Concept)` | `server.py` ~690 (lazy backfill, **not** written at onboard time). ⚠ **To be removed—ADR 0011** names `AuthorityRule.concept` (property) canonical; no reader exists, edge goes away in S1.x. |
| **SOURCE_SYSTEM** |—| ⚠ **No edge writer found.** Cross-system identity is modeled as the **`IdentityRule.source_system` property**, not a `SOURCE_SYSTEM` edge (`onboard.py` ~1701, `resolve.py` ~92, `graph_analytics.py` ~184). Listed here because it appears in the task edge inventory. |—|
| **TARGET_SYSTEM** |—| ⚠ **No edge writer found.** Same as above—modeled as the **`IdentityRule.target_system` property** (`onboard.py` ~1701, `resolve.py` ~93). |—|
| **GOVERNS** |—| ⚠ **Not present.** No writer or reader anywhere in the code; **confirmed absent** in the audited cmms graph. Authority is expressed through `APPLIES_TO` + `AuthorityRule.system_id`/`.concept`. Appears only in the original task edge inventory—**not part of the schema.** |—|

Edge-shape hazards surfaced (decisions deferred to the audit findings):
- **`HAS_CHILD` vs `HAS_PARENT`**—✅ **RESOLVED (F6).** Standardized on a single
  canonical `HAS_PARENT` (child→parent) edge: the orchestrator now traverses
  `<-[:HAS_PARENT*]-` (descendants of a site) and the redundant inverse `HAS_CHILD`
  write was removed from `onboard.py`. No direction mismatch remains.
  **DECIDED:** standardize on the **written** direction—`HAS_PARENT` (child→parent).
  Code follow-up: fix the orchestrator to traverse `<-[:HAS_PARENT*]-`; do **not** add a
  second `HAS_CHILD` edge (one direction, one representation). Not S1.x-blocking; needed
  before any asset-tree feature.
- **`LINKS_CONCEPT` vs `RESOLVES`**—onboarding links `IdentityRule→Concept` with
  `LINKS_CONCEPT` (`onboard.py` ~1733), while the resolver *reads* `RESOLVES`
  (`resolve.py` ~91) and also *writes* `RESOLVES` (`resolve.py` ~503/511). An
  IdentityRule created by onboarding alone (never re-touched by resolve) is therefore
  reachable by `LINKS_CONCEPT` but **not** by the resolver's `RESOLVES` read. **DECIDED
  (ADR 0011):** `LINKS_CONCEPT` is canonical, resolver reads it, `RESOLVES` is removed—code
  fix + ≥2-system live confirm pending S1.x.

---

## 4. The resolver-relevant subgraph

The deterministic resolver needs three fact families for an onboarded system, plus a
fourth **conditional** family for telemetry-rich (i3X) systems and a fifth
**conditional** family for the equipment identity bridge. The cypher
below is copied **verbatim** from the audit-script spec (`audit_onboarding_graph.py`)
so this doc and the script assert against identical strings. `$sid` = system id.

### (a) field→concept mappings—Mapping
`(System)-[:HAS_SCHEMA]->(SchemaEntry)-[:HAS_FIELD]->(Field)-[:MAPS_TO {confidence, role}]->(Concept)`.
`SchemaEntry.name` = table, `Field.name` = column, `Field.disambiguation` = optional note.

```cypher
MATCH (s:System {id:$sid})-[:HAS_SCHEMA]->(se:SchemaEntry)-[:HAS_FIELD]->(f:Field)-[r:MAPS_TO]->(c:Concept) RETURN se.name AS table, f.name AS column, c.id AS concept, r.role AS role, r.confidence AS confidence
```

> **Observed (cmms, 2026-06-11):** 61 rows, 14 distinct concepts, 0 missing `role`, 0 missing
> `confidence`. PASS.

Target-system discovery (used by `pick_target_system`):
```cypher
MATCH (s:System)-[:HAS_SCHEMA]->(:SchemaEntry)-[:HAS_FIELD]->(:Field)-[:MAPS_TO]->(:Concept) RETURN DISTINCT s.id
```

### (b) authority—Authority
`(AuthorityRule {id, concept, system_id, rule, priority})-[:APPLIES_TO]->(System)`, with a
lazily-backfilled `-[:ABOUT_CONCEPT]->(Concept)` edge (created in `server.py` ~690, **not**
at onboard time).

```cypher
MATCH (ar:AuthorityRule)-[:APPLIES_TO]->(s:System {id:$sid}) RETURN ar.concept, ar.system_id, ar.rule, ar.priority
```

> **Observed (cmms, 2026-06-11):** AuthorityRules present and resolvable by the `ar.concept`
> property (`queryable_by_property=true`); `ABOUT_CONCEPT` edge **absent** (`about_concept_edge_count=0`,
> `queryable_by_edge=false`). Read the property, not the edge. PASS.

### (c) joins—Join Path
`(JoinPath {from_system, from_table, from_field, to_system, to_table, to_field, ...})`, with
`-[:FROM_SYSTEM]->(System)` and `-[:TO_SYSTEM]->(System)` edges. Endpoints exist as **both**
properties and edges (duality—see §5).

Property form:
```cypher
MATCH (jp:JoinPath) WHERE jp.from_system=$sid OR jp.to_system=$sid RETURN jp.from_system, jp.from_table, jp.from_field, jp.to_system, jp.to_table, jp.to_field
```

Edge form:
```cypher
MATCH (jp:JoinPath)-[:FROM_SYSTEM]->(s:System {id:$sid})
```

> **Observed (cmms, 2026-06-11):** 5 JoinPaths (all internal to cmms); property form and edge
> form agree (`property_count=5`, `from_system_edge_count=5`, `to_system_edge_count=5`,
> `property_edge_agreement=true`). PASS.

### (d) i3X telemetry / property layer—conditional

Asserted only for an onboarded system that owns `:Property` nodes (a `SiteWise (i3X)`
system—the i3X walker is the sole Property writer); the audit **skips (pass)** the
per-system checks when no such system exists, so relational-only deployments stay green.
The graph-wide orphan check (d3) always runs and passes trivially on a Property-free
graph. Cypher below is copied **verbatim** from `audit_onboarding_graph.py`, like (a)–(c).

Target discovery (used by `pick_i3x_target_system`):
```cypher
MATCH (s:System)<-[:FROM_SYSTEM]-(:Property) RETURN DISTINCT s.id
```

**(d1) ownership**—every Property of the system has an owning Asset through `HAS_PROPERTY`
(`assert_property_ownership`; an unowned Property is unreachable by the orchestrator's
asset-centric traversals):
```cypher
MATCH (p:Property {system:$sid}) WHERE NOT (p)<-[:HAS_PROPERTY]-(:Asset) RETURN count(p) AS n
```
Pass = the system has ≥1 Property (total via
`MATCH (p:Property {system:$sid}) RETURN count(p) AS n`) and the unowned count is 0.

**(d2) count/degree agreement**—`Asset.property_count` equals the actual
`HAS_PROPERTY` out-degree for every asset of the system
(`assert_property_count_degree`; the walker stamps EVERY asset, so a missing stamp is
itself a mismatch—0 means structural-only, absent means broken):
```cypher
MATCH (a:Asset {system:$sid}) OPTIONAL MATCH (a)-[:HAS_PROPERTY]->(p:Property) RETURN a.source_id AS source_id, a.name AS name, a.property_count AS property_count, count(p) AS degree
```

**(d3) no orphaned Property (graph-wide, after-reset invariant)**—reset/offboard
DETACH-DELETEs the system's Property layer alongside its Assets (`onboard.py` ~558;
`Property` is also in the global-reset label list), so any orphan indicates an
interrupted cleanup or a writer that skipped the `HAS_PROPERTY` edge
(`assert_no_orphaned_properties`):
```cypher
MATCH (p:Property) WHERE NOT (p)<-[:HAS_PROPERTY]-(:Asset) RETURN count(p) AS n
```

> **Not yet observed live**—pending a reconciliation run against a deployed environment
> with an onboarded i3X system (§1 status note). Offline coverage:
> `tests/unit/test_audit_i3x_property_layer.py`.

### (e) equipment ValueMapping / MAPS_TO_ASSET layer—conditional

The equipment identity bridge: `(ValueMapping {concept:'equipment'})-[:MAPS_TO_ASSET]->(Asset)`,
written by `onboard.py::_persist_equipment_value_mappings` **guarded on a non-empty
`to_element_id`**. The audit **skips (pass)** when no equipment VM carries `to_element_id`
AND no `MAPS_TO_ASSET` edge exists, so relational-only / pre-round-2 graphs stay green.
Cypher below is copied **verbatim** from `audit_onboarding_graph.py`, like (a)–(d).

**(e1/e2) link integrity**—every equipment-concept ValueMapping with a non-empty
`to_element_id` has **exactly one** `MAPS_TO_ASSET` edge, and the target Asset's
`source_id` equals `vm.to_element_id` (`assert_equipment_vm_asset_links`):
```cypher
MATCH (vm:ValueMapping {concept:$concept}) WHERE vm.to_element_id <> '' OPTIONAL MATCH (vm)-[:MAPS_TO_ASSET]->(a:Asset) RETURN vm.id AS vm_id, vm.to_element_id AS to_element_id, count(a) AS edge_count, collect(a.source_id) AS asset_source_ids
```

**(e3) writer guard**—no `MAPS_TO_ASSET` edge leaves a VM missing `to_element_id`
(`assert_no_bare_vm_asset_edges`; the writer MERGEs the edge only when the element id is
non-empty, so a bare-VM edge means a foreign writer or a corrupted VM—trivially passes
on a graph with no `MAPS_TO_ASSET` edges):
```cypher
MATCH (vm:ValueMapping)-[:MAPS_TO_ASSET]->(:Asset) WHERE vm.to_element_id IS NULL OR vm.to_element_id = '' RETURN count(vm) AS n
```

### metamodel reconciliation probe—report-only, never pass/fail

`probe_metamodel_edges` reconciles `:MetaEdge` rows against live edge types **both
directions**: `documented_but_absent` is **INFO not FAIL** (the metamodel may lead the
data—for example `MAPS_TO_ASSET` documented before the first equipment onboard);
`live_but_undocumented` mirrors the fetch-side drift suffix the orchestrator sees
(`metamodel.fetch_metamodel_text`), so the two views stay reconcilable.
`status='no_metamodel'` flags a graph provisioned before the metamodel write existed.
```cypher
MATCH (me:MetaEdge) RETURN DISTINCT me.name AS name
```
```cypher
MATCH ()-[r]->() RETURN DISTINCT type(r) AS rel
```

> **Not yet observed live**—pending the same deployed-env reconciliation run as (d).
> Offline coverage: `tests/unit/test_audit_metamodel_vm_layer.py`.

---

## 5. What S1.x may depend on versus NOT guaranteed

Each item is labeled **guaranteed** (traceable to a specific writer, holds for every
catalog-driven onboarding) or **provisional—see findings** (pending a live-graph
run). No item here is a design decision; provisional items are candidate ADR/​CONTEXT edits
for follow-up review.

| # | Claim | Label | Basis |
|---|-------|-------|-------|
| 1 | The `(System)-[:HAS_SCHEMA]->(SchemaEntry)-[:HAS_FIELD]->(Field)-[:MAPS_TO]->(Concept)` spine exists for any onboarded system that mapped ≥1 field. | **guaranteed ✓ verified** | `onboard.py` ~1559–1602. cmms: 61 edges across the spine. |
| 2 | `MAPS_TO.confidence` and `MAPS_TO.role` are **set** by the onboarding writer. | **guaranteed (new edges) ✓ verified** | `onboard.py` ~1602 always `SET r.confidence, r.role`. cmms: 0 of 61 missing either. |
| 3 | `MAPS_TO.role`/`.confidence` are **present on every edge in the live graph**. | **guaranteed ✓ verified** | cmms: 0/61 missing either. Onboarding always sets both (`onboard.py` ~1602). Pre-release there are no legacy edges, so the `role='identifier'` fallback in `graph_analytics.py` ~114 is dead—**code follow-up: remove it** and rely on the guarantee (fail loudly if `role`/`confidence` is ever absent rather than silently defaulting). |
| 4 | Authority is resolvable at the **system** level for a concept: `AuthorityRule.system_id` + `AuthorityRule.concept`, reachable through `APPLIES_TO`. | **guaranteed (system-level) ✓ verified** | `onboard.py` ~1912–1922. cmms: multiple AuthorityRules reachable through APPLIES_TO. |
| 5 | Authority is available at **field** level. | **NOT in v1—system-level by decision** | DECIDED: authority is **system-level**—`AuthorityRule {concept, system_id}` reached through `APPLIES_TO`, matching `the glossary` "Authority". Field-level is a **designed extension point**, not built in v1: add an optional `AuthorityRule.field` (absent ⇒ system-level default; set ⇒ that field of that system overrides for the concept), with the override taking precedence over the system default in the resolver. Purely additive—deferred per ADR 0008 (no validated need yet). |
| 6 | Authority is queryable by the **`AuthorityRule.concept` property** (not only the `ABOUT_CONCEPT` edge). | **DECIDED—ADR 0011** (property canonical) ✓ verified; edge confirmed ABSENT at onboard | Property set at onboard (`onboard.py` ~1912). cmms: `queryable_by_property=true`, `about_concept_edge_count=0`. The `ABOUT_CONCEPT` edge is lazily backfilled in `server.py` ~690 with **no reader**—**resolver reads the property**; the edge is removed in S1.x per ADR 0011. |
| 7 | JoinPath endpoints are available as **properties** (`from_system`/`to_system`/`from_table`/…). | **DECIDED—ADR 0011: canonical** ✓ verified | `onboard.py` ~1624. cmms: 5/5 JoinPaths carry the properties. Properties carry the full system+table+field tuple—the resolver reads them. |
| 8 | JoinPath endpoints are available as **edges** (`FROM_SYSTEM`/`TO_SYSTEM`). | **DECIDED—ADR 0011: secondary index** (verified for cmms; provisional for non-FK/cross-system) | cmms: property and edge forms agree (5=5=5). Edges encode only the System endpoints (not table/field); retained as a traversal index that must stay consistent with the properties—**properties win on disagreement**. |
| 9 | `IdentityRule→Concept` is reachable through **`LINKS_CONCEPT`** (the canonical identity edge; resolver reads it). | **DECIDED—ADR 0011** (code fix + live confirm pending S1.x) | ADR 0011 names `LINKS_CONCEPT` canonical (onboarding is the authoritative writer) and removes the `RESOLVES` edge. Resolver currently still reads `RESOLVES` (`resolve.py` ~91)—S1.x fixes the read and drops the `RESOLVES` writes (~503/511). cmms (lone system) has **0** IdentityRules, so live confirmation needs a ≥2-system onboard added to the audit. |
| 10 | `GOVERNS`, `SOURCE_SYSTEM`, `TARGET_SYSTEM` edges exist. | **NOT guaranteed (absent)** | No writers found. Cross-system identity uses `IdentityRule.source_system`/`.target_system` *properties*; no `GOVERNS` edge anywhere. |
| 11 | `Concept.isa95_level` / `Concept.source` exist on every Concept. | **NOT guaranteed—seed-only** | Onboarding-discovered Concepts get only `name`/`description` (`onboard.py` ~1596 `ON CREATE SET c.name=$cid, c.description=$cid`); `isa95_level`/`source` are set only on **core-seeded** Concepts (`catalog.py` / `graph_analytics.py` ~22/27). The resolver must treat them as optional—present on seeded concepts, absent on onboarding-discovered ones. |
| 12 | For an onboarded i3X system, the `(:Asset)-[:HAS_PROPERTY]->(:Property)` spine exists and every Property is owned by an Asset and linked to its System through `FROM_SYSTEM`. | **provisional—static** (writer-traced; pending live i3X reconciliation, §1 note) | `onboard_i3x.py` `_write_property_child` ~106–119 writes all three in one pass (Property MERGE, HAS_PROPERTY, FROM_SYSTEM). Audited by §4(d1); offline coverage in `tests/unit/test_onboard_i3x_walker.py` + `test_audit_i3x_property_layer.py`. |
| 13 | Every i3X-written Asset carries `property_count` equal to its `HAS_PROPERTY` out-degree; **0 = structural-only**. SiteWise (Direct)-written Assets carry it with the same semantics. | **provisional—static** (pending live i3X reconciliation) | The walker stamps every asset (`onboard_i3x.py` ~221/~252) after walking its children. The SiteWise (Direct) walk stamps every asset it writes too (`onboard.py` `_dp_sitewise_walk`). The orchestrator's `property_count=0` guard depends on the stamp. Audited by §4(d2). |
| 14 | No orphaned `:Property` survives a reset/offboard (graph-wide invariant). | **provisional—static** (pending live i3X reconciliation) | Per-system reset DETACH-DELETEs `Property {system:$sid}` before Assets (`onboard.py` ~558); global reset includes `Property` in `_RESET_NODE_LABELS`. Audited by §4(d3), which also catches a writer that skipped `HAS_PROPERTY`. |
| 15 | `AssetModel-[:MODELED_BY]->SchemaEntry` bridges every AssetModel whose normalized name matches a SchemaEntry of the same system; site-level i3X Assets link to a name-matching Plant through `LOCATED_AT`. | **NOT guaranteed—best-effort bridge** | `onboard_i3x.py` `bridge_i3x_schema` ~316/~328 is a **name-match** post-pass (non-fatal, ordering-dependent: it must run after the SchemaEntry write, guaranteed by `_provision_i3x_post_pass`). A model with no name-matching SchemaEntry, or a site with no name-matching Plant, silently gets no edge—consumers must treat both edges as optional enrichment, not a guaranteed spine. |
| 16 | After a provision, `:MetaNode`/`:MetaEdge` nodes exist documenting the full vocabulary (one MetaNode per label, one MetaEdge per edge type), and they survive resets (not in `_RESET_NODE_LABELS`). | **provisional—static** (best-effort write; pending live reconciliation) | `metamodel.py::write_metamodel`, called from `onboard.py` `_provision_knowledge_post_pass`—**non-fatal** (a write failure degrades prompt enrichment, never a provision), so presence is expected, not hard-guaranteed. The orchestrator's fetch falls back to the static prompt on absence. Probed report-only by the audit (`probe_metamodel_edges`, `status='no_metamodel'`). |
| 17 | Every equipment-concept ValueMapping written by the provision post-pass carries `to_element_id`/`to_path` (possibly `""` when the asset was not resolved), and, when `to_element_id` is non-empty, exactly one `MAPS_TO_ASSET` edge to the Asset with that `source_id`. | **provisional—static** (pending live reconciliation, audited §4e) | `onboard.py::_persist_equipment_value_mappings` ~5348 always SETs both fields; the edge MERGE ~5364 is guarded on non-empty `to_element_id`. Self-learning ValueMappings (`self_learning.py` ~244) do NOT carry the fields—consumers must treat them as optional on the label, guaranteed only for `method`-tagged equipment mappings from the post-pass. The post-pass also writes registry-to-registry mappings (`method='equipment_name_match'`, `_cross_registry_vms_or_none`, [ADR-0098](adr/0098-equipment-identity-across-registries-from-equal-names.md)): `to_element_id=""`, `to_path=""`, no `MAPS_TO_ASSET` edge, and an optional `provenance` property (computed text naming the two name columns, where each side's site came from, and the row counts compared) that only these mappings carry. |
| 18 | No `MAPS_TO_ASSET` edge exists from a ValueMapping whose `to_element_id` is null/empty. | **provisional—static** (audited §4e3, graph-wide) | The sole writer guards on non-empty `to_element_id`; the audit's writer-guard check fails loud if a foreign writer violates this. |

**Audit evidence:** counts above are from `agentic_digital_thread/tests/audit/last-run-report.json`
(target `cmms`, graph ID sanitized to `g-example1234`, 2026-06-11): field_concept 61 rows / 14 concepts /
0 missing role / 0 missing confidence; authority property-queryable, ABOUT_CONCEPT edge count 0;
joins 5 with property↔edge agreement; identity probe RESOLVES 0 / LINKS_CONCEPT 0.

---

## 6. Metamodel vocabulary (machine-readable cross-pin)

The block below is the doc-side copy of the self-describing vocabulary in
`agentic_digital_thread/agents/metamodel.py` (`META_NODES` labels /
`META_EDGES` names). It is parsed and **set-equality pinned** to the metamodel
by `agentic_digital_thread/tests/unit/test_graph_contract_metamodel_pin.py`
(which also checks each name appears in the §2/§3 tables)—so this doc, the
metamodel, and the pinned sets in `tests/unit/test_metamodel.py` cannot drift
from each other silently. **Edit all of them together** (the metamodel module's
ownership rule, §1). Transitional ADR-0011 edges (`RESOLVES`, `ABOUT_CONCEPT`)
and never-written names (`SOURCE_SYSTEM`, `TARGET_SYSTEM`, `GOVERNS`) are
documented in §3 for history but are deliberately NOT vocabulary.

<!-- METAMODEL-VOCAB:BEGIN -->
```
node-labels: Plant, System, SchemaEntry, Field, Concept, Asset, Property, AssetModel, AssetModelRole, ValueMapping, JoinPath, IdentityRule, AuthorityRule, ConceptOverlay, MetaNode, MetaEdge, MetricDefinition
edge-types: HAS_SCHEMA, HAS_FIELD, MAPS_TO, HAS_PARENT, HAS_PROPERTY, INSTANCE_OF, MODELED_BY, LOCATED_AT, FROM_SYSTEM, TO_SYSTEM, LINKS_CONCEPT, APPLIES_TO, MAPS_TO_ASSET, DIFFERENT_IDENTIFIER_FROM
```
<!-- METAMODEL-VOCAB:END -->
