# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A graph write is sent again after an ambiguous failure only when its call
site says it is safe to run twice.

A write that failed with a 422 resubmit request, a 500, a 503 or a 409
conflict may have taken effect, or may not, so by default it is not sent
again (only a 429 throttle or a connection that was never made, when it
certainly never ran, are). Whether a write has the same effect run twice as
once is not decided from its text: its call site states it, with
``onboard.neptune_write_resubmit_safe`` (``neptune_write`` inside
``db.resubmit_safe_write()``, around exactly one statement) or, outside
``onboard.py``, a ``with resubmit_safe_write():`` block around one write.

Every graph write the strict provision path runs, and every graph write the
startup restore runs, was read for this (``report-neptune-resubmit.md``): the
MERGE / SET writes whose second run with the same parameters changes nothing
are opted in; CREATE, DELETE, counters, writes that read a value they
overwrite, and statements built by a function are not. ``OPTED_IN`` pins every
statement text each opted-in site can send, evaluated from the source
(``_resubmit_safe_writes``), so an edit to any of them, or a new opt-in, fails
this file and someone has to re-check it before updating the pin.
``OPTED_IN_AFTER_DROP`` pins the few also sent again after a dropped
connection.

Labels and properties are invented except in the pinned provision statements.
"""
from __future__ import annotations

import ast
import io
import json
import re
from pathlib import Path

import pytest
from botocore.exceptions import (
    ClientError,
    ConnectionClosedError,
    EndpointConnectionError,
    ReadTimeoutError,
)

import agents.db as db
import agents.onboard as onboard

from tests.unit import _resubmit_safe_writes as pins

URL = "https://g-abcdefghij.example/queries"
AGENTS = Path(__file__).resolve().parents[2] / "agents"
RESUBMIT_MESSAGE = "Retry for SDK query requests is suppressed, please resubmit the query."


def _client_error(code: str, status: int, message: str = "boom") -> ClientError:
    response = {"Error": {"Code": code, "Message": message},
                "ResponseMetadata": {"HTTPStatusCode": status}}
    return type(code, (ClientError,), {})(response, "ExecuteQuery")


# ── The pinned statements ────────────────────────────────────────────────────
#
# Each is safe to run twice with the same parameters: a MERGE matches what a
# first run created, and every SET writes a parameter or a constant. The one
# read-then-write accepted is ``r.version = coalesce(r.version, 1)``, a fixed
# point (a second run leaves the value the first left). Parameters are Python
# values built before the first send; the resend reuses the same mapping.

#: The provision Concept MERGE. ON CREATE is its only SET, so a second run
#: that matches the node the first created changes nothing.
CONCEPT_MERGE = (
    "MERGE (c:Concept {id: $cid}) ON CREATE SET c.name = $cid, c.description = $cid, "
    "c.created_by_system = $created_by_system, c.created_at = $created_at, "
    "c.created_by_run = $created_by_run")
ENTITY_CONCEPT_MERGE = (
    "MERGE (c:Concept {id: $cid}) ON CREATE SET c.name = $name, c.source = 'minted', "
    "c.layer = 'entity', c.description = $desc, c.created_by_system = $created_by_system, "
    "c.created_at = $created_at, c.created_by_run = $created_by_run")
FIELD_MAPS_TO = (
    "MATCH (f:Field {id: $fid}), (c:Concept {id: $cid}) MERGE (f)-[r:MAPS_TO]->(c) "
    "SET r.confidence = $conf, r.role = $role, r.version = coalesce(r.version, 1)")
FIELD_MAPS_TO_REUSE = (
    "MATCH (f:Field {id: $fid}), (c:Concept {id: $cid}) MERGE (f)-[r:MAPS_TO]->(c) "
    "SET r.confidence = $conf, r.role = 'field_concept', r.method = 'reuse', "
    "r.version = coalesce(r.version, 1)")
SCHEMA_ENTRY_MERGE = (
    "MERGE (se:SchemaEntry {id: $seid}) SET se.name = $name, se.system_id = $sid "
    "WITH se MERGE (s:System {id: $sid}) ON CREATE SET s.name = $sid, s.status = 'onboarded' "
    "MERGE (s)-[:HAS_SCHEMA]->(se)")
HAS_FIELD = "MATCH (se:SchemaEntry {id: $seid}), (f:Field {id: $fid}) MERGE (se)-[:HAS_FIELD]->(f)"
JP_FROM_SYSTEM = "MATCH (jp:JoinPath {id: $jpid}), (s:System {id: $sid}) MERGE (jp)-[:FROM_SYSTEM]->(s)"
JP_TO_SYSTEM = "MATCH (jp:JoinPath {id: $jpid}), (s:System {id: $sid}) MERGE (jp)-[:TO_SYSTEM]->(s)"
JP_INTERNAL = (
    "MERGE (jp:JoinPath {id: $jpid}) SET jp.from_system = $sys, jp.from_table = $ftbl, "
    "jp.from_field = $ffld, jp.to_system = $sys, jp.to_table = $ttbl, jp.to_field = $tfld, "
    "jp.transform = 'direct', jp.method = 'foreign_key_match', "
    "jp.basis = $basis, jp.constraint_name = $cname")
JP_SAMEROW = (
    "MERGE (jp:JoinPath {id: $jpid}) SET jp.from_system = $sys, jp.from_table = $tbl, "
    "jp.from_field = $ffld, jp.to_system = $sys, jp.to_table = $tbl, jp.to_field = $tfld, "
    "jp.transform = 'same_row', jp.method = 'same_row_identifier_set', "
    "jp.basis = $basis, jp.unique_constraints = $uniq")
IDENTITY_RULE = (
    "MERGE (ir:IdentityRule {id: $rid}) SET "
    "ir.concept = $concept, ir.source_system = $src_sys, ir.source_field = $src_field, "
    "ir.source_pattern = $src_pat, "
    "ir.target_system = $tgt_sys, ir.target_field = $tgt_field, ir.target_pattern = $tgt_pat, "
    "ir.confidence = $conf")
IR_PROVENANCE = ", ir.origin_context = $octx, ir.pattern_grammar = $grammar"
IR_METHOD = ", ir.method = $method"
IR_TABLE = ", ir.source_table = $src_tbl"
JP_RULE = (
    "MERGE (jp:JoinPath {id: $jpid}) SET jp.from_system = $fsys, jp.from_table = $ftbl, "
    "jp.from_field = $ffld, jp.to_system = $tsys, jp.to_table = $ttbl, jp.to_field = $tfld, "
    "jp.transform = $xform, jp.description = $desc, jp.method = $method")
JP_RULE_ORIGIN = ", jp.origin_context = $octx"
JP_RULE_EVIDENCE = ", jp.basis = $basis, jp.rule_id = $rule_id, jp.rule_method = $rule_method"
JP_RULE_OVERLAP = ", jp.overlap_count = $ocount"

# The system-owned cleanup deletes at the start of a provision. Each removes
# only what belongs to this system (its schema subgraph, its auto-generated
# intra-system JoinPaths, the measured records on its Fields, its System's
# links to other plants), and nothing else writes those for this system while
# its provision claim is held, so a second run matches nothing.
SCHEMA_SUBGRAPH_DELETE = (
    "MATCH (s:System {id: $sid})-[:HAS_SCHEMA]->(se:SchemaEntry) "
    "OPTIONAL MATCH (se)-[:HAS_FIELD]->(f:Field) DETACH DELETE se, f")
AUTO_INTERNAL_JOINPATH_DELETE = (
    "MATCH (jp:JoinPath) WHERE jp.from_system = $sid AND jp.to_system = $sid "
    "AND (jp.id STARTS WITH 'jp-internal-' OR jp.id STARTS WITH 'jp-vo-' "
    "OR jp.id STARTS WITH 'jp-samerow-') AND coalesce(jp.method, '') <> 'user_confirmed' "
    "DETACH DELETE jp")
DISTINCT_IDENTIFIER_DELETE = (
    "MATCH (f:Field {system_id: $sid})-[r:DIFFERENT_IDENTIFIER_FROM]->() DELETE r")
PLANT_LINK_DELETE = (
    "MATCH (s:System {id: $sid})-[r:LOCATED_AT]->(p:Plant) "
    "WHERE $pid = '' OR coalesce(p.id, '') <> $pid DELETE r")
#: The unanchored ValueMapping write c3 and c3b send: both System nodes
#: matched, the node MERGEd by id, every SET a parameter, its two edges
#: MERGEd. The anchored form (it deletes MAPS_TO_ASSET edges) is not pinned.
VALUE_MAPPING_UPSERT = (
    "MATCH (fs:System {id: $fsys}), (ts:System {id: $tsys}) "
    "WHERE coalesce(fs.removal_incomplete, false) = false "
    "AND coalesce(ts.removal_incomplete, false) = false "
    "MERGE (vm:ValueMapping {id: $vmid}) SET vm.from_system = $fsys, vm.from_field = $ffield, "
    "vm.from_value = $fval, vm.to_system = $tsys, vm.to_field = $tfield, vm.to_value = $tval, "
    "vm.concept = $concept, vm.confidence = $conf, vm.method = $method "
    "MERGE (vm)-[:FROM_SYSTEM]->(fs) MERGE (vm)-[:TO_SYSTEM]->(ts) RETURN vm.id AS id")
# The schema-rewrite marks: the start SETs and returns one precomputed token;
# the others compare that token or the exact stale value, then SET or REMOVE
# fixed values, so a second run leaves what the first left.
SCHEMA_REWRITE_MARK = (
    "MATCH (s:System {id: $sid}) SET s.schema_rewrite_started = $started "
    "RETURN s.schema_rewrite_started AS started")
SCHEMA_REWRITE_SETTLED = (
    "MATCH (s:System {id: $sid}) "
    "WHERE s.schema_rewrite_started IS NULL OR s.schema_rewrite_started = $token "
    "REMOVE s.schema_rewrite_started, s.schema_rewrite_failed")
SCHEMA_REWRITE_FAILED = (
    "MATCH (s:System {id: $sid}) "
    "WHERE s.schema_rewrite_started IS NULL OR s.schema_rewrite_started = $token "
    "SET s.schema_rewrite_failed = $failed REMOVE s.schema_rewrite_started")
SCHEMA_REWRITE_CLEAR_STALE = (
    "MATCH (s:System {id: $sid}) WHERE s.schema_rewrite_started = $started "
    "REMOVE s.schema_rewrite_started")
SCHEMA_REWRITE_CLEAR_STALE_FAILED = (
    "MATCH (s:System {id: $sid}) WHERE s.schema_rewrite_failed = $failed "
    "REMOVE s.schema_rewrite_failed")
#: The pinned statements allowed to DELETE / REMOVE (no other pin may).
DELETE_PINS = {SCHEMA_SUBGRAPH_DELETE, AUTO_INTERNAL_JOINPATH_DELETE, DISTINCT_IDENTIFIER_DELETE,
               PLANT_LINK_DELETE}
REMOVE_PINS = {SCHEMA_REWRITE_SETTLED, SCHEMA_REWRITE_FAILED, SCHEMA_REWRITE_CLEAR_STALE,
               SCHEMA_REWRITE_CLEAR_STALE_FAILED}
#: The startup core-concept seed (``server._send_core_concepts``), once
#: per core concept on every app start, and again from the background top-up
#: for the ones not confirmed, through ``db.neptune_write_outcome`` (never
#: again while a timed-out copy may still be running). ON CREATE is its only
#: SET, and every value is a parameter or a constant.
CORE_CONCEPT_SEED = (
    "MERGE (c:Concept {id: $cid}) ON CREATE SET c.name = $cid, c.description = $desc, "
    "c.source = 'core'")
#: The relation-kind provenance SETs the startup restore (for a system whose
#: graph schema lacks its types) and the provision both send: a parameter SET
#: on a node matched by id.
RELATION_KIND_SET = "MATCH (se:SchemaEntry {id: $seid}) SET se.relation_kind = $kind"
FIELD_DATA_TYPE_SET = "MATCH (f:Field {id: $fid}) SET f.data_type = $dtype"

#: ``(file, function)`` -> every statement text the site can send.
OPTED_IN = {
    ("onboard.py", "_dp_sitewise_write_asset"): {
        "MERGE (a:Asset {source_id: $sid}) SET a.name = $name, a.asset_type = $atype, "
        "a.system = $sys, a.level = $level, a.walker_level = $level, a.path = $path",
        "MATCH (c:Asset {source_id: $cid}), (p:Asset {source_id: $pid}) MERGE (c)-[:HAS_PARENT]->(p)",
        "MERGE (m:AssetModel {name: $mname}) WITH m MATCH (a:Asset {source_id: $sid}) "
        "MERGE (a)-[:INSTANCE_OF]->(m)",
        "MATCH (a:Asset {source_id: $sid}), (s:System {id: $sys}) MERGE (a)-[:FROM_SYSTEM]->(s)",
    },
    ("onboard.py", "_dp_sitewise_write_properties"): {
        "MERGE (p:Property {source_id: $sid}) SET p.name = $name, p.parent_id = $pid, "
        "p.system = $sys,     p.data_type = $dtype, p.unit = $unit, p.path = $path",
        "MATCH (a:Asset {source_id: $pid}), (p:Property {source_id: $sid}) "
        "MERGE (a)-[:HAS_PROPERTY]->(p)",
        "MATCH (p:Property {source_id: $sid}), (s:System {id: $sys}) MERGE (p)-[:FROM_SYSTEM]->(s)",
    },
    ("onboard.py", "_dp_sitewise_walk"): {
        "MATCH (a:Asset {source_id: $sid}) SET a.property_count = $n",
    },
    ("onboard.py", "_dp_agent_sitewise"): {
        "MERGE (s:System {id: $sid}) SET s.name = $name, s.status = 'onboarded', "
        "s.connection_type = 'SiteWise (Direct)'",
    },
    ("onboard.py", "_dp_system_node"): {
        "MERGE (p:Plant {id: $pid}) SET p.name = $pname",
        "MERGE (s:System {id: $sid}) SET s.name = $name, s.plant = $plant, s.schema = $schema, "
        "s.status = 'onboarded', s.connection_type = $ctype, s.query_guidance = $qg, "
        "s.capabilities = $caps, s.tool_prefix = $tp, s.agent_model = $amodel, "
        "s.system_type = $stype, s.connection_origin = $origin",
    },
    ("onboard.py", "_dp_link_plant"): {
        PLANT_LINK_DELETE,
        "MATCH (s:System {id: $sid}), (p:Plant {id: $pid}) MERGE (s)-[:LOCATED_AT]->(p)",
    },
    ("onboard.py", "_delete_stale_schema_subgraph"): {SCHEMA_SUBGRAPH_DELETE},
    ("onboard.py", "_delete_stale_auto_internal_joinpaths"): {AUTO_INTERNAL_JOINPATH_DELETE},
    ("onboard.py", "_delete_stale_distinct_identifier_records"): {DISTINCT_IDENTIFIER_DELETE},
    ("onboard.py", "_upsert_value_mapping"): {VALUE_MAPPING_UPSERT},
    ("onboard.py", "_mark_schema_rewrite"): {SCHEMA_REWRITE_MARK},
    # Takes back a run's own mark (exact value) when its store mark failed.
    ("onboard.py", "_unmark_schema_rewrite"): {SCHEMA_REWRITE_CLEAR_STALE},
    ("onboard.py", "_finish_schema_rewrite"): {SCHEMA_REWRITE_SETTLED, SCHEMA_REWRITE_FAILED},
    ("onboard.py", "_fresh_marker"): {SCHEMA_REWRITE_CLEAR_STALE, SCHEMA_REWRITE_CLEAR_STALE_FAILED},
    ("onboard.py", "_dp_schema_entries"): {
        SCHEMA_ENTRY_MERGE, ENTITY_CONCEPT_MERGE,
        "MATCH (se:SchemaEntry {id: $seid}), (c:Concept {id: $entity}) MERGE (se)-[:MAPS_TO]->(c)",
    },
    ("onboard.py", "_dp_commit_concept_edges"): {
        CONCEPT_MERGE, FIELD_MAPS_TO, FIELD_MAPS_TO + ", r.method = $method",
    },
    ("onboard.py", "_dp_field_row"): {
        "MERGE (f:Field {id: $fid}) SET f.name = $name, f.schema_id = $schema_id, "
        "f.system_id = $sid",
        HAS_FIELD,
    },
    ("onboard.py", "_dp_athena_physical_fields"): {  # through schema_fields' write=
        SCHEMA_ENTRY_MERGE, HAS_FIELD,
        "MERGE (f:Field {id: $fid}) SET f.name = $name, f.schema_id = $schema_id, "
        "f.system_id = $sid, f.is_partition = $isp, f.data_type = $dtype",
    },
    ("onboard.py", "_dp_discovery_completeness"): {  # through schema_fields' write=
        "MATCH (se:SchemaEntry {id: $seid}) SET se.discovery_complete = $complete, "
        "se.discovered_columns = $count",
    },
    ("onboard.py", "_dp_restore_record_keys"): {  # through record_key.restore/persist/clear
        "MATCH (s:System {id: $sid})-[:HAS_SCHEMA]->(se:SchemaEntry {id: $seid}) "
        "SET se.record_key = $record_key, se.record_key_by = $by, "
        "se.record_key_at = $at, se.record_key_source = $source",
        "MATCH (s:System {id: $sid}) SET s.record_key_rewrite = $carry",
    },
    ("onboard.py", "_take_record_keys"): {  # through record_key.persist_rewrite_carry
        "MATCH (s:System {id: $sid}) SET s.record_key_rewrite = $carry",
    },
    ("onboard.py", "_dp_write_deferred_edges"): {CONCEPT_MERGE, FIELD_MAPS_TO},
    ("onboard.py", "_dp_identity_key_edges"): {
        "MATCH (se:SchemaEntry {id: $seid}), (f:Field {id: $fid}) MERGE (se)-[:IDENTIFIED_BY]->(f)",
    },
    ("onboard.py", "_dp_write_reuse_edge"): {CONCEPT_MERGE, FIELD_MAPS_TO_REUSE},
    ("onboard.py", "_dp_internal_joinpaths"): {
        JP_INTERNAL, JP_INTERNAL + ", jp.reference_check = $rcheck",
        JP_INTERNAL + ", jp.reference_count = $rcount, jp.key_count = $kcount, jp.measured_at = $mat",
        JP_FROM_SYSTEM, JP_TO_SYSTEM,
    },
    ("onboard.py", "_dp_write_valueoverlap_pair"): {
        "MERGE (jp:JoinPath {id: $jpid}) SET jp.from_system = $sys, jp.from_table = $ftbl, "
        "jp.from_field = $ffld, jp.to_system = $sys, jp.to_table = $ttbl, jp.to_field = $tfld, "
        "jp.transform = $transform, jp.method = 'value_overlap', "
        "jp.overlap_count = $ocount, jp.overlap_ratio = $oratio, jp.basis = $basis",
        JP_FROM_SYSTEM, JP_TO_SYSTEM,
    },
    ("onboard.py", "_dp_samerow_table"): {
        JP_SAMEROW, JP_SAMEROW + ", jp.pair_count = $pcount", JP_FROM_SYSTEM, JP_TO_SYSTEM,
    },
    ("onboard.py", "_dp_link_rule_concept"): {
        CONCEPT_MERGE,
        "MATCH (ir:IdentityRule {id: $rid}), (c:Concept {id: $cid}) MERGE (ir)-[:LINKS_CONCEPT]->(c)",
    },
    ("onboard.py", "_dp_write_identity_rule"): {
        IDENTITY_RULE + IR_PROVENANCE, IDENTITY_RULE + IR_PROVENANCE + IR_METHOD,
        IDENTITY_RULE + IR_PROVENANCE + IR_TABLE,
        IDENTITY_RULE + IR_PROVENANCE + IR_METHOD + IR_TABLE,
    },
    ("onboard.py", "_dp_write_rule_joinpath"): {
        JP_RULE + JP_RULE_EVIDENCE, JP_RULE + JP_RULE_EVIDENCE + JP_RULE_OVERLAP,
        JP_RULE + JP_RULE_ORIGIN + JP_RULE_EVIDENCE,
        JP_RULE + JP_RULE_ORIGIN + JP_RULE_EVIDENCE + JP_RULE_OVERLAP,
        JP_FROM_SYSTEM, JP_TO_SYSTEM,
    },
    ("onboard.py", "_dp_authority_rules"): {
        "MERGE (ar:AuthorityRule {id: $arid}) SET ar.rule = $rule, ar.system_id = $sid, "
        "ar.concept = $concept, ar.priority = 1",
        "MATCH (ar:AuthorityRule {id: $arid}), (s:System {id: $sid}) MERGE (ar)-[:APPLIES_TO]->(s)",
    },
    ("onboard.py", "_dp_onboarding_log"): {
        "MATCH (s:System {id: $sid}) SET s.schema_version = $ver",
    },
    ("onboard.py", "_land_emergent_concepts"): {CONCEPT_MERGE, FIELD_MAPS_TO},
    ("self_learning.py", "_repoint_replace_edges"): {
        "MERGE (c:Concept {id: $concept}) ON CREATE SET c.name = $concept",
        "MATCH (f:Field {id: $fid}), (c:Concept {id: $concept}) MERGE (f)-[r:MAPS_TO]->(c) "
        "SET r.confidence = $conf, r.role = 'identifier', r.method = 'user_corrected', "
        "r.version = $ver, r.confirmed = true, r.approved_by = $approved_by",
    },
    ("connection_resolver.py", "_persist_node_connection_config"): {
        "MERGE (s:System {id: $sid}) SET s.connection_config = $cfg_json, s.secret_arn = $arn, "
        "s.connection_type = $ctype",
    },
    ("onboard.py", "_persist_schema_relation_kinds"): {RELATION_KIND_SET, FIELD_DATA_TYPE_SET},
    ("server.py", "_send_core_concepts"): {CORE_CONCEPT_SEED},
}

#: The opted-in sites also sent again after a dropped connection or a 502
#: (``resubmit_safe_write(after_drop=True)``), when the first copy may still be
#: running: a copy running alongside the resend, or landing after it and after
#: the writes that follow, must leave the graph as one run. A statement
#: qualifies when (1) it writes only MERGE steps, on a node matched by its id
#: or an edge between nodes matched by id: Neptune Analytics's MERGE step reads
#: concurrent writes, so concurrent copies create one element
#: (docs.aws.amazon.com/neptune-analytics/latest/userguide/query-isolation-level.html);
#: (2) every value it SETs outside ON CREATE is part of the node's own id
#: (``ID_PARTS``), so every writer of that id writes that value and a late
#: copy cannot overwrite a later write; (3) nothing later in the same run
#: deletes what it writes, so a late copy brings nothing back. A SET of any
#: other parameter does not qualify: a late copy could land after a later
#: write of the same property (the MAPS_TO edges: the user-correction repoint
#: and the retired-link sweep rewrite and delete them later in the run).
OPTED_IN_AFTER_DROP = {
    ("server.py", "_send_core_concepts"): {CORE_CONCEPT_SEED},
    # b: the table's SchemaEntry (name and system id are its id
    # ``<system>.<table>``; the System exists from a0, so ON CREATE never runs)
    # and its entity edge; the Concept is ON CREATE only. b0 deletes the old
    # subgraph before these; nothing in the run deletes them after.
    ("onboard.py", "_dp_schema_entries"): {
        SCHEMA_ENTRY_MERGE, ENTITY_CONCEPT_MERGE,
        "MATCH (se:SchemaEntry {id: $seid}), (c:Concept {id: $entity}) MERGE (se)-[:MAPS_TO]->(c)",
    },
    # b2: the Field (name, schema id and system id are its id
    # ``<system>.<table>.<column>``; b2a and the steward's drift accept write
    # the same three values) and its HAS_FIELD edge. The 2026-10-29 failure.
    ("onboard.py", "_dp_field_row"): {
        "MERGE (f:Field {id: $fid}) SET f.name = $name, f.schema_id = $schema_id, "
        "f.system_id = $sid",
        HAS_FIELD,
    },
    # The ON CREATE Concept MERGE before each concept edge; no provision step
    # deletes a Concept.
    ("onboard.py", "_dp_commit_concept_edges"): {CONCEPT_MERGE},
    ("onboard.py", "_dp_write_deferred_edges"): {CONCEPT_MERGE},
    ("onboard.py", "_dp_write_reuse_edge"): {CONCEPT_MERGE},
    ("onboard.py", "_dp_link_rule_concept"): {CONCEPT_MERGE},
    ("onboard.py", "_land_emergent_concepts"): {CONCEPT_MERGE},
}
#: For a node matched by ``{id: $<parameter>}``, keyed by its label and that
#: id parameter: the only properties an ``after_drop`` pin may SET outside ON
#: CREATE, each with the one value it must be set to, a part of that id
#: (``schema_fields.field_id`` / ``schema_entry_id``). A Field's ``$fid`` is
#: ``<system>.<table>.<column>``: ``$name`` is the column, ``$schema_id`` the
#: ``<system>.<table>`` prefix, ``$sid`` the system. A SchemaEntry's ``$seid``
#: is ``<system>.<table>``: ``$name`` is the table, ``$sid`` the system.
ID_PARTS = {
    ("Field", "fid"): {"name": "$name", "schema_id": "$schema_id", "system_id": "$sid"},
    ("SchemaEntry", "seid"): {"name": "$name", "system_id": "$sid"},
}

#: A real provision write that is NOT safe to run twice: every run CREATEs
#: another OnboardingLog node.
ONBOARDING_LOG_CREATE = (
    "CREATE (ol:OnboardingLog {id: $oid, system_id: $sid, timestamp: $ts, "
    "rules_created: $rc, tables_mapped: $tm})")

CONFLICT = _client_error("ConflictException", 409, "Concurrent modification")
#: The ends after which an opted-in write is sent again.
AMBIGUOUS = [
    _client_error("UnprocessableException", 422, RESUBMIT_MESSAGE),
    _client_error("InternalServerException", 500),
    _client_error("ServiceUnavailable", 503),
    CONFLICT,
]
#: A dropped connection: covered only by ``after_drop=True``.
DROPS = [
    ConnectionClosedError(endpoint_url=URL),
    _client_error("BadGateway", 502),
]
#: The ends no opt-in covers: a timeout, and a query the server timed out.
NOT_COVERED = [
    ReadTimeoutError(endpoint_url=URL),
    _client_error("GatewayTimeout", 504),
    _client_error("UnprocessableException", 422, "Query timed out."),
]


def _ids(errors):
    return [type(e).__name__ + str((getattr(e, "response", None) or {}).get(
        "ResponseMetadata", {}).get("HTTPStatusCode", "")) for e in errors]


class _Client:
    """Raises the queued errors in order on the statements containing
    ``only`` (every statement by default), then answers no rows."""

    def __init__(self, errors, only=""):
        self.errors = list(errors)
        self.only = only
        self.calls: list[str] = []

    def execute_query(self, **kwargs):
        self.calls.append(kwargs["queryString"])
        if self.errors and self.only in kwargs["queryString"]:
            raise self.errors.pop(0)
        return {"payload": io.BytesIO(json.dumps({"results": []}).encode())}


@pytest.fixture
def graph(monkeypatch):
    import agents.graph_resubmit as gr
    monkeypatch.setattr(gr, "_sleep", lambda _s: None)
    monkeypatch.setattr(db, "_graph_backend", lambda: "neptune")

    def use(errors=(), only=""):
        client = _Client(errors, only)
        monkeypatch.setattr(db.boto3, "client", lambda *a, **k: client)
        return client
    return use


@pytest.fixture
def strict_provision():
    token = onboard._STRICT_PROVISION_WRITES.set(True)
    yield
    onboard._STRICT_PROVISION_WRITES.reset(token)


class _Ctx:
    """The fields of the provision state the concept edge writer reads."""

    def __init__(self):
        self.system_id = "sys-a"
        self.committable = {"bin-code"}
        self.user_corrections = {}
        self.deferred_llm_edges = {}


def _commit_one_edge():
    return onboard._dp_commit_concept_edges(
        _Ctx(), "sys-a.bins.code",
        [{"concept": "bin-code", "role": "identifier", "confidence": 0.9}])


# ── The opted-in provision statement ─────────────────────────────────────────

@pytest.mark.parametrize("error", AMBIGUOUS, ids=_ids(AMBIGUOUS))
def test_the_provision_concept_merge_is_sent_again_after_an_ambiguous_failure(
        graph, strict_provision, error):
    client = graph([error])
    assert _commit_one_edge() is True
    assert client.calls[:2] == [CONCEPT_MERGE, CONCEPT_MERGE]
    assert len(client.calls) == 3  # the Concept MERGE twice, then the edge


def test_the_provision_concept_merge_stays_within_the_bound(graph, strict_provision):
    import agents.graph_resubmit as gr
    client = graph([AMBIGUOUS[0] for _ in range(gr.MAX_ATTEMPTS)])
    with pytest.raises(RuntimeError, match="provision graph write failed"):
        _commit_one_edge()
    assert client.calls == [CONCEPT_MERGE] * gr.MAX_ATTEMPTS


@pytest.mark.parametrize("error", NOT_COVERED, ids=_ids(NOT_COVERED))
def test_the_opt_in_does_not_cover_a_timeout(graph, strict_provision, error):
    client = graph([error])
    with pytest.raises(RuntimeError, match="provision graph write failed"):
        _commit_one_edge()
    assert client.calls == [CONCEPT_MERGE]


@pytest.mark.parametrize("error", DROPS, ids=_ids(DROPS))
def test_the_concept_merge_is_sent_again_after_a_drop(graph, strict_provision, error):
    """``after_drop=True`` at this site: ON CREATE is the Concept MERGE's only SET."""
    client = graph([error])
    assert _commit_one_edge() is True
    assert client.calls == [CONCEPT_MERGE, CONCEPT_MERGE, FIELD_MAPS_TO]


@pytest.mark.parametrize("error", DROPS, ids=_ids(DROPS))
def test_the_maps_to_edge_is_not_sent_again_after_a_drop(graph, strict_provision, error):
    """Opted in, but not ``after_drop``: a late copy could undo a later
    repoint or retired-link delete of the same edge."""
    client = graph([error], only="MAPS_TO")
    with pytest.raises(RuntimeError, match="provision graph write failed"):
        _commit_one_edge()
    assert client.calls == [CONCEPT_MERGE, FIELD_MAPS_TO]


@pytest.mark.parametrize("error", AMBIGUOUS, ids=_ids(AMBIGUOUS))
def test_the_maps_to_edge_after_the_concept_merge_is_sent_again(graph, strict_provision, error):
    """The MAPS_TO edge write that follows the Concept MERGE (fixed parameters,
    ``r.version = coalesce(r.version, 1)``) is safe to run twice: a 500, a 503,
    a 422 resubmit request or a 409 on it no longer fails the provision."""
    client = graph([error], only="MAPS_TO")
    assert _commit_one_edge() is True
    assert client.calls[0] == CONCEPT_MERGE
    assert client.calls[1:] == [FIELD_MAPS_TO, FIELD_MAPS_TO]


@pytest.mark.parametrize("error", AMBIGUOUS, ids=_ids(AMBIGUOUS))
def test_a_write_without_the_opt_in_is_not_sent_again_after_an_ambiguous_failure(graph, error):
    client = graph([error])
    assert onboard.neptune_write(CONCEPT_MERGE, {"cid": "bin-code"}) is False
    assert client.calls == [CONCEPT_MERGE]


def test_the_opted_in_merge_is_sent_again_after_a_conflict(graph):
    client = graph([CONFLICT])
    with db.resubmit_safe_write():
        assert db.neptune_write(CONCEPT_MERGE, {"cid": "bin-code"}) is True
    assert client.calls == [CONCEPT_MERGE, CONCEPT_MERGE]


class _LogCtx:
    """The fields of the provision state ``_dp_onboarding_log`` reads."""

    def __init__(self):
        self.system_id = "sys-a"
        self.rules = []
        self.tables_seen = ["bins"]
        self.nodes = 0


@pytest.mark.parametrize("error", AMBIGUOUS, ids=_ids(AMBIGUOUS))
def test_a_provision_create_still_fails_at_once(graph, strict_provision, error):
    """The OnboardingLog write CREATEs a node on every run, so it is not opted
    in: one 500, 503, 422 resubmit request or 409 on it fails the provision
    with nothing sent again, and the System write after it never runs."""
    client = graph([error], only="CREATE")
    with pytest.raises(RuntimeError, match="provision graph write failed"):
        onboard._dp_onboarding_log(_LogCtx())
    assert client.calls == [ONBOARDING_LOG_CREATE]


def test_the_schema_version_write_after_it_is_sent_again(graph, strict_provision):
    client = graph([AMBIGUOUS[1]], only="schema_version")
    onboard._dp_onboarding_log(_LogCtx())
    assert client.calls[0] == ONBOARDING_LOG_CREATE
    assert client.calls[1] == client.calls[2] and "schema_version" in client.calls[1]


def test_conflicts_on_an_opted_in_write_stay_within_the_bound(graph):
    import agents.graph_resubmit as gr
    client = graph([CONFLICT for _ in range(gr.MAX_ATTEMPTS)])
    with db.resubmit_safe_write():
        assert db.neptune_write(CONCEPT_MERGE, {"cid": "bin-code"}) is False
    assert len(client.calls) == gr.MAX_ATTEMPTS


# ── The four other provision writers of the same statement ──────────────────

class _CatalogStub:
    def get(self, _system_id):
        return {"system_type": "erp"}


class _MintCtx:
    """The fields of the provision state ``_dp_mint_unmapped`` reads."""

    def __init__(self):
        self.system_id = "sys-a"
        self.schema = "shop"
        self.connection_type = "PostgreSQL"
        self.unmapped_columns = [("bins", "bin_code", "sys-a.bins.bin_code")]
        self.nodes = 0


def _deferred_edges():
    onboard._dp_write_deferred_edges(
        [{"fid": "sys-a.bins.code", "cid": "bin-code", "conf": 0.9, "role": "identifier"}],
        "sys-a")


def _mint_unmapped(monkeypatch):
    monkeypatch.setattr(onboard, "catalog", _CatalogStub())
    monkeypatch.setattr(onboard, "entity_for_table", lambda _s, _t: "bin")
    monkeypatch.setattr(onboard, "is_generic_field", lambda _c: False)
    monkeypatch.setattr(onboard, "reuse_first_suggestions", lambda *_a: [{"concept": "bin-code"}])
    monkeypatch.setattr(onboard, "supported_reuse_choice",
                        lambda _s: {"concept": "bin-code", "score": 0.8})
    onboard._dp_mint_unmapped(_MintCtx())


def _link_rule_concept():
    onboard._dp_link_rule_concept(_Ctx(), "ir-1", "bin-code")


def _emergent_concepts(monkeypatch):
    monkeypatch.setattr(onboard.self_learning, "accepted_user_concept_mappings", lambda _s: {})
    onboard._land_emergent_concepts("sys-a", "shop", [
        {"table": "bins", "field": "bins.vibration",
         "mappings": [{"concept": "bin-vibration", "role": "measure", "confidence": 0.7}]}])


SITE_DRIVERS = {
    "_dp_write_deferred_edges": lambda _mp: _deferred_edges(),
    "_dp_write_reuse_edge": _mint_unmapped,  # driven through _dp_mint_unmapped
    "_dp_link_rule_concept": lambda _mp: _link_rule_concept(),
    "_land_emergent_concepts": _emergent_concepts,
}


@pytest.mark.parametrize("error", AMBIGUOUS, ids=_ids(AMBIGUOUS))
@pytest.mark.parametrize("site", sorted(SITE_DRIVERS))
def test_each_opted_in_site_sends_its_merge_again_after_an_ambiguous_failure(
        graph, strict_provision, monkeypatch, site, error):
    client = graph([error], only="MERGE (c:Concept")
    SITE_DRIVERS[site](monkeypatch)
    assert client.calls[:2] == [CONCEPT_MERGE, CONCEPT_MERGE]
    assert len(client.calls) == 3  # the Concept MERGE twice, then its edge


@pytest.mark.parametrize("error", DROPS, ids=_ids(DROPS))
@pytest.mark.parametrize("site", sorted(SITE_DRIVERS))
def test_each_concept_site_sends_its_merge_again_after_a_drop(
        graph, strict_provision, monkeypatch, site, error):
    client = graph([error], only="MERGE (c:Concept")
    SITE_DRIVERS[site](monkeypatch)
    assert client.calls[:2] == [CONCEPT_MERGE, CONCEPT_MERGE]
    assert len(client.calls) == 3


@pytest.mark.parametrize("error", DROPS, ids=_ids(DROPS))
@pytest.mark.parametrize("site", sorted(SITE_DRIVERS))
def test_each_concept_site_still_fails_on_a_drop_of_its_edge_write(
        graph, strict_provision, monkeypatch, site, error):
    """The edge after each Concept MERGE is not ``after_drop``."""
    client = graph([error], only="MATCH")
    with pytest.raises(RuntimeError, match="provision graph write failed"):
        SITE_DRIVERS[site](monkeypatch)
    assert len(client.calls) == 2 and client.calls[0] == CONCEPT_MERGE


@pytest.mark.parametrize("error", AMBIGUOUS, ids=_ids(AMBIGUOUS))
@pytest.mark.parametrize("site", sorted(SITE_DRIVERS))
def test_each_opted_in_site_sends_its_edge_write_again(
        graph, strict_provision, monkeypatch, site, error):
    client = graph([error], only="MATCH")
    SITE_DRIVERS[site](monkeypatch)
    assert client.calls[0] == CONCEPT_MERGE
    assert len(client.calls) == 3 and client.calls[1] == client.calls[2]


# ── Other opted-in writes, driven through their real functions ──────────────

def test_the_plant_link_and_its_delete_are_both_sent_again(graph, strict_provision):
    """``_dp_link_plant`` deletes the System's edges to any other plant, then
    MERGEs the one to its plant. Both are safe to run twice: the delete
    removes only this System's edges, and only this provision (claim and
    connection lock held) writes them."""
    client = graph([AMBIGUOUS[1]], only="MERGE")
    assert onboard._dp_link_plant("sys-a", "Plant A") == 1
    assert "DELETE r" in client.calls[0]
    assert client.calls[1] == client.calls[2] and "LOCATED_AT" in client.calls[1]
    client = graph([AMBIGUOUS[1]], only="DELETE")
    assert onboard._dp_link_plant("sys-a", "Plant A") == 1
    assert client.calls[0] == client.calls[1] == PLANT_LINK_DELETE


def test_a_user_correction_repoint_resends_its_merges_not_its_deletes(graph, monkeypatch):
    """``self_learning._repoint_replace_edges``: the two DELETEs are not
    opted in, the Concept MERGE and the corrected MAPS_TO MERGE are."""
    import agents.self_learning as sl
    client = graph([AMBIGUOUS[1]], only="MERGE (f)-[r:MAPS_TO]")
    sl._repoint_replace_edges("sys-a.bins.code", "sys-a", "bin-code", 1.0, 3)
    merges = [c for c in client.calls if "MERGE (f)-[r:MAPS_TO]" in c]
    assert len(merges) == 2 and merges[0] == merges[1]
    client = graph([AMBIGUOUS[1]], only="DELETE")
    with pytest.raises(RuntimeError):
        sl._repoint_replace_edges("sys-a.bins.code", "sys-a", "bin-code", 1.0, 3)
    assert len(client.calls) == 1 and "DELETE" in client.calls[0]


def test_a_required_connection_node_write_is_sent_again(graph):
    """A user registration's System-node write is required
    (``require_node=True``): one 500 on it no longer fails the provision."""
    import agents.connection_resolver as cr
    client = graph([AMBIGUOUS[1]])
    cr._persist_node_connection_config("sys-a", {"url": "https://bins.example"}, "REST API",
                                       "arn:aws:secretsmanager:eu-west-1:000000000000:secret:x",
                                       require_node=True)
    assert len(client.calls) == 2 and client.calls[0] == client.calls[1]


@pytest.mark.parametrize("statement", sorted(set().union(*(
    v for (f, _fn), v in OPTED_IN.items() if f == "onboard.py"))))
def test_the_onboard_helper_sends_each_pinned_statement_again(graph, strict_provision, statement):
    client = graph([AMBIGUOUS[1]])
    assert onboard.neptune_write_resubmit_safe(statement, {"x": 1}) is True
    assert client.calls == [statement, statement]


def test_the_resend_reuses_the_parameters_of_the_first_send(graph, monkeypatch):
    """A statement's parameters are built before the first send; the resend
    sends that same mapping, so nothing computed after the first send can
    reach the second."""
    sent = []
    client = graph([AMBIGUOUS[1]])
    real = client.execute_query

    def execute_query(**kwargs):
        sent.append(kwargs.get("parameters"))
        return real(**kwargs)
    client.execute_query = execute_query
    params = {"cid": "bin-code", "created_at": "t"}
    assert onboard.neptune_write_resubmit_safe(CONCEPT_MERGE, params) is True
    assert len(sent) == 2 and sent[0] is sent[1] and sent[0] == params


@pytest.mark.parametrize("error", AMBIGUOUS, ids=_ids(AMBIGUOUS))
def test_the_db_write_helpers_honour_the_opt_in(graph, error):
    cypher = "MERGE (b:Bin {id: $id}) ON CREATE SET b.label = $id"
    client = graph([error])
    with db.resubmit_safe_write():
        assert db.neptune_write(cypher, {"id": "b1"}) is True
    assert len(client.calls) == 2
    client = graph([error])
    with db.resubmit_safe_write():
        assert db.neptune_write_returning(cypher + " RETURN b.id AS id", {"id": "b1"}) == []
    assert len(client.calls) == 2


def test_the_opt_in_ends_with_its_block(graph):
    with db.resubmit_safe_write():
        with db.resubmit_safe_write():
            pass
        client = graph([AMBIGUOUS[0]])
        assert db.neptune_write("MERGE (b:Bin {id: $id})", {"id": "b1"}) is True
    client = graph([AMBIGUOUS[0]])
    assert db.neptune_write("MERGE (b:Bin {id: $id})", {"id": "b1"}) is False
    assert len(client.calls) == 1


# ── The pin ─────────────────────────────────────────────────────────────────

def pin_mismatches(found: dict, problems: list) -> list:
    """Every way ``found`` (the scanner's opted-in sites and texts) differs
    from ``OPTED_IN``, plus the scanner's problems; empty when they agree."""
    out = list(problems)
    for site in sorted(set(found) - set(OPTED_IN)):
        out.append(f"{site}: opts in but is not pinned")
    for site in sorted(set(OPTED_IN) - set(found)):
        out.append(f"{site}: pinned but no longer opts in")
    for site in sorted(set(found) & set(OPTED_IN)):
        if set(found[site]) != OPTED_IN[site]:
            out.append(f"{site}: an opted-in statement changed")
    return out


def test_only_the_pinned_sites_opt_in_and_each_sends_only_its_pinned_statements():
    """A new opt-in, a changed opted-in statement, or an opt-in the scanner
    cannot tie to one pinned write fails here: re-check that running the
    statement twice with the same parameters leaves the graph as running it
    once, then update ``OPTED_IN``."""
    found, problems = pins.opted_in_statements()
    assert pin_mismatches(found, problems) == []


def test_only_the_pinned_sites_are_sent_again_after_a_drop():
    """A new ``after_drop=True``, or a changed statement under one, fails
    here: re-check that a copy still running alongside the resend leaves the
    graph as one run, then update ``OPTED_IN_AFTER_DROP``."""
    found, problems = pins.opted_in_after_drop()
    assert problems == []
    assert {site: set(texts) for site, texts in found.items()} == OPTED_IN_AFTER_DROP


#: The clauses an ``after_drop`` pin is read in (OPTIONAL MATCH only to refuse it).
_CLAUSE = re.compile(r"\b(OPTIONAL\s+MATCH|MATCH|MERGE|WITH|RETURN|ON\s+CREATE\s+SET|"
                     r"ON\s+MATCH\s+SET|SET)\b", re.I)
_NODE = r"\(\s*(\w+)\s*(?::\s*(\w+))?\s*(\{[^}]*\})?\s*\)"
#: An edge between two bare node variables, with a type and no properties.
_EDGE = re.compile(r"\(\s*(\w+)\s*\)\s*<?-\s*\[\s*\w*\s*:\s*\w+\s*\]\s*->?\s*\(\s*(\w+)\s*\)")
_BY_ID = re.compile(r"\{\s*id\s*:\s*\$(\w+)\s*\}")


def _bind_by_id(var, label, props, bound: dict, where: str) -> list:
    """Bind ``var`` to ``(label, id parameter)`` when its pattern is a
    labelled ``{id: $x}`` node; otherwise the problem."""
    by_id = _BY_ID.fullmatch(props or "")
    if not label or not by_id:
        return [f"{where} ({var}{':' + label if label else ''} {props or ''}) "
                "is not a labelled node matched by {id: $...}"]
    bound[var] = (label, by_id.group(1))
    return []


def _assignments(body: str) -> list:
    """The items of a SET body: ``var.prop = value`` pairs and any whole-node
    ``var = map`` or ``var += map`` (which the caller refuses). A comma inside
    a value, as in ``coalesce(r.version, 1)``, does not split it."""
    return [a.strip() for a in re.split(r",\s*(?=\w+(?:\.\w+)?\s*\+?=)", body.strip())]


def _clause_problems(kw: str, body: str, bound: dict) -> list:
    """One clause of an ``after_drop`` pin against rules (1) and (2), with
    ``bound`` (variable -> ``(label, id parameter)``) updated in place."""
    if kw == "MATCH":
        nodes = list(re.finditer(_NODE, body))
        rest = re.sub(_NODE, "", body).replace(",", "").strip()
        problems = [f"MATCH {body!r} is not only nodes"] if rest or not nodes else []
        for node in nodes:
            problems += _bind_by_id(*node.groups(), bound, "MATCH")
        return problems
    if kw == "MERGE":
        node, edge = re.fullmatch(_NODE, body), _EDGE.fullmatch(body)
        if node:
            return _bind_by_id(*node.groups(), bound, "MERGE")
        if edge:
            return [f"edge MERGE endpoint {var} is not a node matched by its id"
                    for var in edge.groups() if var not in bound]
        return [f"MERGE {body!r} is neither one node by id nor one edge between nodes by id"]
    if kw == "WITH":
        kept = [v.strip() for v in body.split(",")]
        if not all(v in bound for v in kept):
            return [f"WITH {body!r} keeps something that is not a node matched by id"]
        for var in set(bound) - set(kept):
            del bound[var]
        return []
    if kw in ("SET", "ON MATCH SET", "ON CREATE SET"):
        problems = []
        for item in _assignments(body):
            m = re.fullmatch(r"(\w+)\.(\w+)\s*=\s*(.+)", item, flags=re.S)
            if not m or m.group(1) not in bound:
                problems.append(f"{kw} {item!r} is not on a node matched by id")
            elif m.group(2) == "id" and m.group(3).strip() != "$" + bound[m.group(1)][1]:
                problems.append(f"{kw} {item!r} changes the id {m.group(1)} was matched by")
            elif kw != "ON CREATE SET" and \
                    ID_PARTS.get(bound[m.group(1)], {}).get(m.group(2)) != m.group(3).strip():
                problems.append(f"{kw} {item!r} is not a part of {m.group(1)}'s id (ID_PARTS)")
        return problems
    return [] if kw == "RETURN" else [f"{kw} is not allowed"]


def after_drop_problems(text: str) -> list:
    """Why ``text`` breaks rule (1) or (2) of ``OPTED_IN_AFTER_DROP``; empty
    when it keeps both. Rule (1): it writes only through MERGE, every node is
    bound by a labelled ``{id: $...}`` pattern (a MATCH, or a MERGE of that
    one node), and every edge MERGE joins two variables bound that way and
    carries no properties. Rule (2): every SET outside ON CREATE is an
    ``ID_PARTS`` pair for its node's label and id parameter. Covers the Cypher
    the pins use (MATCH, MERGE, WITH, SET, ON CREATE SET, RETURN)."""
    upper = text.upper().replace("ON CREATE", "ON_CREATE")
    problems = []
    if re.search(r"\b(CREATE|DELETE|DETACH|REMOVE|CALL|UNWIND|LOAD|FOREACH)\b", upper):
        problems.append("writes other than through MERGE")
    if not re.search(r"\bMERGE\b", upper):
        problems.append("no MERGE")
    parts = _CLAUSE.split(text)
    if parts[0].strip():
        problems.append(f"{parts[0].strip()!r} before the first clause")
    bound: dict = {}
    for keyword, body in zip(parts[1::2], parts[2::2]):
        problems += _clause_problems(" ".join(keyword.upper().split()), body.strip(), bound)
    return problems


def test_every_after_drop_pin_converges_and_sets_only_its_own_key():
    """A reader's backstop on ``OPTED_IN_AFTER_DROP`` (rules (1) and (2) of
    its comment, :func:`after_drop_problems`; rule (3) is read per site):
    each text is also an ordinary pin and breaks neither rule."""
    for site, statements in OPTED_IN_AFTER_DROP.items():
        assert statements <= OPTED_IN[site], site
        for text in statements:
            assert after_drop_problems(text) == [], text


#: Statements the backstop must refuse, each breaking rule (1) or (2).
BACKSTOP_REFUSES = {
    "a node MERGE not by id": "MERGE (f:Field)",
    "an edge endpoint not matched by id":
        "MATCH (se:SchemaEntry) MERGE (se)-[:HAS_FIELD]->(f)",
    "a key property SET to another value":
        "MERGE (f:Field {id: $fid}) SET f.name = $unrelated",
    "a key property of a node matched by another id":
        "MERGE (f:Field {id: $other}) SET f.name = $name",
    "an edge MERGE that creates its endpoint":
        "MATCH (se:SchemaEntry {id: $seid}) MERGE (se)-[:HAS_FIELD]->(f:Field {id: $fid})",
    "an edge with properties":
        "MATCH (a:Field {id: $fid}), (b:Concept {id: $cid}) MERGE (a)-[:MAPS_TO {role: $role}]->(b)",
    "a variable dropped by WITH":
        "MATCH (se:SchemaEntry {id: $seid}), (f:Field {id: $fid}) WITH se "
        "MERGE (se)-[:HAS_FIELD]->(f)",
    "the MAPS_TO edge SETs": FIELD_MAPS_TO,
    "a Field data-type SET": FIELD_DATA_TYPE_SET,
    # Review 2026-10-29 (round 2): a create that changes the id the MERGE
    # matched on, and a whole-node map assignment hidden after a pair.
    "an ON CREATE SET that changes the id":
        "MERGE (f:Field {id: $fid}) ON CREATE SET f.id = $other",
    "a whole-node map assignment after a pair":
        "MERGE (f:Field {id: $fid}) ON CREATE SET f.name = $name, f = $props",
    "a whole-node map merge":
        "MERGE (f:Field {id: $fid}) ON CREATE SET f.name = $name, f += $props",
}


@pytest.mark.parametrize("text", BACKSTOP_REFUSES.values(), ids=BACKSTOP_REFUSES.keys())
def test_the_backstop_refuses_what_its_two_rules_do_not_allow(text):
    """REPRODUCTION (round 2): 1a813d9e's backstop accepted the first three
    (a node with no properties passed, and a key property passed whatever its
    value)."""
    assert after_drop_problems(text) != [], text


def test_no_pinned_statement_creates_or_counts_and_only_the_cleanups_delete():
    """A reader's backstop on the pins themselves, not the decision rule: no
    pinned text has CREATE outside MERGE's ON CREATE, an arithmetic counter or
    a clock/random function; only the four system-owned cleanups DELETE and
    only the token-guarded rewrite marks REMOVE."""
    import re
    for site, statements in OPTED_IN.items():
        for text in statements:
            upper = text.upper().replace("ON CREATE", "ON_CREATE")
            assert not re.search(r"\bCREATE\b|\+ ?\d|"
                                 r"TIMESTAMP\(|DATETIME\(|RAND\(|RANDOMUUID\(", upper), (site, text)
            assert bool(re.search(r"\bDELETE\b", upper)) == (text in DELETE_PINS), (site, text)
            assert bool(re.search(r"\bREMOVE\b", upper)) == (text in REMOVE_PINS), (site, text)


def test_the_onboarding_log_create_is_not_opted_in():
    assert all(ONBOARDING_LOG_CREATE not in v for v in OPTED_IN.values())
    found, _ = pins.opted_in_statements()
    assert all(ONBOARDING_LOG_CREATE not in v for v in found.values())


def test_nothing_else_in_agents_sets_the_opt_in():
    """The flags are set only by ``db.resubmit_safe_write`` and read only where
    the write is executed."""
    uses = []
    for path in sorted(AGENTS.rglob("*.py")):
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            if "_RESUBMIT_SAFE" in line or "_RESUBMIT_AFTER_DROP" in line:
                uses.append((path.name, line.strip()))
    assert [u for u in uses if not u[1].startswith(
        ("#", "_RESUBMIT_SAFE:", "_RESUBMIT_AFTER_DROP:"))] == [
        ("db.py", "token = _RESUBMIT_SAFE.set(True)"),
        ("db.py", "drop_token = _RESUBMIT_AFTER_DROP.set(bool(after_drop))"),
        ("db.py", "_RESUBMIT_AFTER_DROP.reset(drop_token)"),
        ("db.py", "_RESUBMIT_SAFE.reset(token)"),
        ("db.py", "cypher, log=logger, resubmit_safe=_RESUBMIT_SAFE.get(),"),
        ("db.py", "after_drop=_RESUBMIT_AFTER_DROP.get())"),
    ]
