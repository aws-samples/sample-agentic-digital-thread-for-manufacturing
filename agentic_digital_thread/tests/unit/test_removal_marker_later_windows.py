# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The removal marker holds AFTER the candidate reads too.

``test_resolve_skips_removing_system.py`` covers a peer whose removal had
already started when onboarding read its peers. A removal can also start
later: while the model answers, between provision's System inventory and its
write, while join discovery samples a peer, before a steward accepts what
discovery filed, or between that accept's pre-check and its write. And a
same-site peer being removed must neither block the new system's authority
rule nor stay selectable next to it. Each test here makes
the marker appear at one of those points (or be there for a read that did
not look at it) and checks that nothing is proposed, written or selected for
the system being removed. Fixture ids are generic.
"""
import copy
import json
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))
os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.join_discovery as jd  # noqa: E402
import agents.metric_resolver as mr  # noqa: E402
import agents.onboard as onboard  # noqa: E402
import agents.onboard_agent as oa  # noqa: E402
import agents.resolve_gates as resolve_gates  # noqa: E402
import agents.self_learning as self_learning  # noqa: E402
from agents.catalog import CONCEPT_AUTHORITY  # noqa: E402
from agents.models import OnboardResolveRequest  # noqa: E402
from tests.unit.test_chat_provision_is_the_wizard_provision import (  # noqa: E402,F401 - fixture
    SID, world)
from tests.unit.test_offboard_deletes_value_mappings import _MiniGraph, _cross_vm  # noqa: E402

NEW = "plant-cmms"
REMOVING = "site-api"   # its removal starts at the point each test names
PEER = "corp-erp"


def _marker_rows(marked) -> list:
    return [{"sid": sid} for sid in sorted(marked)]


# ---------------------------------------------------------------- 1. the chat's resolve

def test_chat_resolve_drops_a_peer_whose_removal_started_during_the_model_call(world):
    """The peer read saw both peers unmarked; the removal of one started while
    the model was answering. Neither the answer nor the session keeps a rule
    or value mapping to it."""
    for sid, kind in ((REMOVING, "MES"), (PEER, "ERP")):
        world.catalog.add({"id": sid, "name": sid, "status": "onboarded", "connection_type": "REST API",
                           "schema": "", "system_type": kind})
    marked: set = set()
    peer_rows = [{"sys_id": sid, "sys_name": sid, "sys_schema": "", "field_name": "stop_code",
                  "concept": "downtime", "table_name": "stops", "removal_incomplete": False}
                 for sid in (REMOVING, PEER)]

    def query(cypher, params=None):
        if "WHERE s.removal_incomplete = true" in cypher:
            return _marker_rows(marked)
        if "HAS_FIELD" in cypher and "MAPS_TO" in cypher:
            return copy.deepcopy(peer_rows)
        return []

    world.mp.setattr(oa, "neptune_query", query)
    world.mp.setattr(oa, "pg", lambda *_a, **_k: {"rows": []})
    world.share_sampled_values()
    from tests.unit.test_chat_provision_is_the_wizard_provision import _chat_map_answer

    def model(prompt, *_a, **_k):
        if "Tables and columns:" in prompt:
            return _chat_map_answer(prompt)
        marked.add(REMOVING)     # the removal starts while the model answers
        rule = {"concept": "downtime", "source_field": "get_tags.Down", "target_field": "stops.stop_code"}
        vm = {"from_system": SID, "from_field": "Down", "from_value": "1", "to_field": "stop_code",
              "to_value": "D1", "concept": "downtime"}
        return json.dumps({"rules": [dict(rule, target_system=REMOVING), dict(rule, target_system=PEER)],
                           "value_mappings": [dict(vm, to_system=REMOVING), dict(vm, to_system=PEER)]})

    world.mp.setattr(oa, "bedrock_call", model)
    assert "error" not in oa.discover(SID)
    assert "error" not in oa.map_fields(SID)
    resolved = oa.resolve_identities(SID)
    assert [r["target_system"] for r in resolved["rules"]] == [PEER], resolved
    assert [v["to_system"] for v in resolved["value_mappings"]] == [PEER], resolved
    session = world.store.get_session(SID)
    assert [r["target_system"] for r in session["rules"]] == [PEER]
    assert [v["to_system"] for v in session["value_mappings"]] == [PEER]


# ---------------------------------------------------------------- 2. provision's value mappings

@pytest.fixture
def vm_graph(monkeypatch):
    """The fake graph that evaluates the one-statement ValueMapping write."""
    g = _MiniGraph(systems=(NEW, REMOVING, PEER))
    monkeypatch.setattr(onboard, "_db_neptune_write_returning", g.mutate, raising=False)
    monkeypatch.setattr(onboard, "neptune_query_checked", g.query)
    monkeypatch.setattr(onboard, "neptune_write", g.write)
    return g


def _vm_ctx():
    vms = [_cross_vm(NEW, REMOVING, "R1"), _cross_vm(NEW, PEER, "P1")]
    return SimpleNamespace(system_id=NEW, value_mappings_written=0, nodes=0,
                           body=SimpleNamespace(value_mappings=vms))


def _written_targets(graph) -> list:
    return sorted(row["to_system"] for row in graph.value_mappings().values())


def test_provision_writes_no_value_mapping_to_a_system_marked_before_it_ran(vm_graph):
    vm_graph.nodes[("System", REMOVING)]["removal_incomplete"] = True
    ctx = _vm_ctx()
    onboard._dp_cross_system_vms(ctx)
    assert _written_targets(vm_graph) == [PEER]
    assert ctx.value_mappings_written == 1
    # The checked inventory already left it out: no write was even tried.
    assert not [p for _c, p in vm_graph.writes if REMOVING in (p.get("fsys"), p.get("tsys"))]


def test_provision_writes_no_value_mapping_to_a_system_marked_after_the_inventory(vm_graph):
    """The inventory read both peers unmarked; the removal started before
    the write. The write itself matches only unmarked endpoints."""
    inventory = onboard._graph_system_ids_checked()
    assert REMOVING in inventory
    vm_graph.nodes[("System", REMOVING)]["removal_incomplete"] = True
    ctx = _vm_ctx()
    written = [onboard._dp_write_cross_vm(ctx, vm, idx, inventory)
               for idx, vm in enumerate(ctx.body.value_mappings)]
    assert written == [False, True]
    assert _written_targets(vm_graph) == [PEER]


# ---------------------------------------------------------------- 3. join discovery

@pytest.fixture
def discovery(monkeypatch):
    """Join discovery over three systems that share one identifier column.
    The peer list was read before the removal started; the marker read
    answers whatever ``marked`` holds when it runs."""
    codes = [f"K{n:03d}" for n in range(1, 9)]
    fields = {sid: [{"table": "assets", "field": "asset_code", "concept": "equipment-id",
                     "role": "identifier", "data_type": "varchar"}] for sid in (NEW, REMOVING, PEER)}
    marked: set = set()
    filed: list = []
    monkeypatch.setattr(jd, "_onboarded_systems", lambda: sorted([NEW, REMOVING, PEER]))
    monkeypatch.setattr(jd, "_identifier_fields", lambda sid: copy.deepcopy(fields[sid]))
    monkeypatch.setattr(jd, "_timestamp_fields", lambda sid: [])
    monkeypatch.setattr(jd, "_has_established_join", lambda a, b: False)

    def sample(sid, tbl, fld):
        marked.add(REMOVING)    # the removal starts while discovery samples
        return list(codes)

    def query(cypher, params=None):
        return _marker_rows(marked) if "removal_incomplete" in cypher else []

    monkeypatch.setattr(jd, "_sample", sample)
    monkeypatch.setattr(jd, "_neptune_query", query)
    monkeypatch.setattr(jd, "_log_proposed_mapping",
                        lambda **kw: filed.append(kw) or f"m-{len(filed)}")
    return filed


def test_join_discovery_files_nothing_against_a_peer_marked_after_the_peer_list_read(discovery):
    out = jd.discover_joins_for_system(NEW)
    named = {s for p in discovery for s in (p["from_system"], p["to_system"])}
    assert REMOVING not in named
    assert named == {NEW, PEER}
    assert out["joins"]


class _ProposalTable:
    def __init__(self):
        self.updates: list = []

    def update_item(self, **kwargs):
        self.updates.append(kwargs)


def test_resolve_job_supersedes_and_hides_the_join_proposals_naming_a_system_being_removed(monkeypatch):
    """Discovery filed one proposal per peer; by the job's end the removal of
    one peer had started. The resolve panel shows only the other one, and the
    stale pending proposal is superseded, never rejected."""
    rows = [{"mapping_id": "m-stale", "discovered_at": "t1", "status": "pending", "source": jd.MINER_SOURCE,
             "proposed_type": "join_path", "from_system": NEW, "from_field": "assets.asset_code",
             "to_system": REMOVING, "to_field": "assets.asset_code", "confidence": 0.9},
            {"mapping_id": "m-ok", "discovered_at": "t2", "status": "pending", "source": jd.MINER_SOURCE,
             "proposed_type": "join_path", "from_system": NEW, "from_field": "assets.asset_code",
             "to_system": PEER, "to_field": "assets.asset_code", "confidence": 0.9}]
    table = _ProposalTable()
    monkeypatch.setattr(self_learning, "get_pending_mappings", lambda: copy.deepcopy(rows))
    monkeypatch.setattr(self_learning, "_query_by_status", lambda status, **_k: copy.deepcopy(rows))
    monkeypatch.setattr(self_learning, "_get_table", lambda: table)
    monkeypatch.setattr(onboard, "_do_resolve", lambda body: {"rules": []})
    monkeypatch.setattr(resolve_gates, "gate_overlap_rules", lambda result: result)
    monkeypatch.setattr(onboard, "_augment_with_affinity_rules", lambda body, result: result)
    monkeypatch.setattr(onboard, "_discover_joins_logged",
                        lambda job_id, body: {"joins": ["m-stale", "m-ok"], "crosswalks": []})
    monkeypatch.setattr(onboard, "neptune_query",
                        lambda cypher, params=None: (_marker_rows({REMOVING})
                                                     if "removal_incomplete" in cypher else []))
    body = OnboardResolveRequest(system_id=NEW, mappings=[])
    onboard._run_resolve_job("job-joins", body)
    job = onboard._resolve_jobs.pop("job-joins")
    assert job["status"] == "done", job
    assert [c["mapping_id"] for c in job["result"]["join_candidates"]] == ["m-ok"]
    (update,) = table.updates
    assert update["Key"] == {"mapping_id": "m-stale", "discovered_at": "t1"}
    values = update["ExpressionAttributeValues"]
    assert values[":status"] == "superseded" and values[":expected"] == "pending"
    assert "system_being_removed" in values.values()


def _accept_item(to_system):
    return {"mapping_id": "m-1", "from_system": NEW, "from_field": "assets.asset_code", "from_value": "",
            "to_system": to_system, "to_field": "assets.asset_code", "to_value": "",
            "concept": "equipment-id", "confidence": 0.9}


@pytest.mark.parametrize("apply", [
    lambda item: self_learning._apply_join_path(item, "subj-1", 1, "equipment-id"),
    lambda item: self_learning._apply_value_mapping(item, "subj-1", 1, "equipment-id", "crosswalk"),
], ids=["join_path", "crosswalk"])
def test_accepting_a_proposal_refuses_an_endpoint_being_removed(monkeypatch, apply):
    """Both System nodes still exist while the removal runs (and after a
    failed one); the accept refuses the marked endpoint and writes nothing."""
    writes: list = []

    def presence(cypher, params=None):
        sid = (params or {}).get("sid")
        return [{"n": 1, "removing": 1 if sid == REMOVING else 0}]

    monkeypatch.setattr(self_learning, "_neptune_query", presence)
    monkeypatch.setattr(self_learning, "_neptune_write_loud", lambda c, p=None: writes.append(c))
    monkeypatch.setattr(self_learning, "_neptune_write_returning",
                        lambda c, p=None: writes.append(c) or [{"id": "subj-1"}])
    with pytest.raises(ValueError, match="being removed"):
        apply(_accept_item(REMOVING))
    assert writes == []
    apply(_accept_item(PEER))          # an unmarked endpoint still lands
    assert writes


#: The one-statement accept write, as the fake graph below evaluates it: both
#: System nodes matched with the removal-marker guard, then the node, its SET
#: and both edges, returning the node id.
_GUARDED_ACCEPT = re.compile(
    r"^MATCH \(fs:System \{id: \$fsys\}\), \(ts:System \{id: \$tsys\}\) "
    r"WHERE coalesce\(fs\.removal_incomplete, false\) = false "
    r"AND coalesce\(ts\.removal_incomplete, false\) = false "
    r"MERGE \((\w+):(JoinPath|Correlation) \{id: \$sid\}\) SET .+ "
    r"MERGE \(\1\)-\[:FROM_SYSTEM\]->\(fs\) MERGE \(\1\)-\[:TO_SYSTEM\]->\(ts\) "
    r"RETURN \1\.id AS id$", re.S)


class _AcceptGraph:
    """Two System nodes; ``marked`` holds the ones whose removal has started.
    The accept's pre-check runs BEFORE the removal starts (it reads no
    marker); the marker is written between the pre-check and the write. The
    guarded one-statement write writes nothing for a marked endpoint; any
    other statement creating a JoinPath or Correlation writes it
    unconditionally, as the graph would."""

    def __init__(self):
        self.marked: set = set()
        self.nodes: dict = {}

    def presence(self, cypher, params=None):
        self.marked.add(REMOVING)      # the removal starts right after this read
        return [{"n": 1, "removing": 0}]

    def write(self, cypher, params=None):
        for label in ("JoinPath", "Correlation"):
            if cypher.startswith("MERGE (") and f":{label} {{id: $sid}}" in cypher:
                self.nodes[(label, params["sid"])] = dict(params)
        return True

    def write_returning(self, cypher, params=None):
        m = _GUARDED_ACCEPT.match(cypher)
        if not m:
            self.write(cypher, params)
            return [{"id": params.get("sid")}]
        if {params["fsys"], params["tsys"]} & self.marked:
            return []
        self.nodes[(m.group(2), params["sid"])] = dict(params)
        return [{"id": params["sid"]}]


@pytest.mark.parametrize("label, apply", [
    ("JoinPath", lambda item: self_learning._apply_join_path(item, "subj-1", 1, "equipment-id")),
    ("Correlation", lambda item: self_learning._apply_correlation(item, "subj-1", 1)),
], ids=["join_path", "correlation"])
def test_an_accept_writes_nothing_when_the_removal_starts_after_its_pre_check(monkeypatch, label, apply):
    graph = _AcceptGraph()
    monkeypatch.setattr(self_learning, "_neptune_query", graph.presence)
    monkeypatch.setattr(self_learning, "_neptune_write", graph.write)
    monkeypatch.setattr(self_learning, "_neptune_write_returning", graph.write_returning)
    with pytest.raises(ValueError, match="being removed"):
        apply(_accept_item(REMOVING))
    assert graph.nodes == {}
    graph.marked.clear()
    monkeypatch.setattr(self_learning, "_neptune_query", lambda c, p=None: [{"n": 1, "removing": 0}])
    assert apply(_accept_item(PEER)) == "subj-1"          # unmarked endpoints still land
    assert set(graph.nodes) == {(label, "subj-1")}


def test_an_accept_whose_write_is_refused_or_reads_back_wrong_fails_loud(monkeypatch):
    monkeypatch.setattr(self_learning, "_neptune_query", lambda c, p=None: [{"n": 1, "removing": 0}])
    for answer in (None, [{"id": "other"}]):
        monkeypatch.setattr(self_learning, "_neptune_write_returning", lambda c, p=None, a=answer: a)
        with pytest.raises(RuntimeError, match="was not written"):
            self_learning._apply_join_path(_accept_item(PEER), "subj-1", 1, "equipment-id")


# ---------------------------------------------------------------- 4. authority

CONCEPT = "spare-bin-code"   # no configured authoritative system type


class _AuthorityCatalog:
    def get(self, sid):
        return {"id": sid, "system_type": "CMMS", "plant": "Plant 1"}


def _authority_ctx():
    return SimpleNamespace(system_id=NEW, system_name="Plant CMMS", plant="Plant 1",
                           authority_rules_written=0, nodes=0)


def _mint(monkeypatch, competitors) -> list:
    """Run the new system's c4 pass; ``competitors`` is what the same-plant
    competing-mapping read answers. Returns the minted rule ids."""
    assert CONCEPT_AUTHORITY.get(CONCEPT) is None
    writes: list = []

    def query(cypher, params=None):
        if "s.plant = $plant" in cypher:
            return copy.deepcopy(competitors)
        if "RETURN DISTINCT c.id AS concept" in cypher:
            return [{"concept": CONCEPT}]
        return []

    monkeypatch.setattr(onboard, "neptune_query", query)
    monkeypatch.setattr(onboard, "neptune_write", lambda c, p=None: writes.append((c, p or {})) or True)
    monkeypatch.setattr(onboard, "catalog", _AuthorityCatalog())
    onboard._dp_authority_rules(_authority_ctx())
    return [p["arid"] for c, p in writes if c.startswith("MERGE (ar:AuthorityRule")]


def test_a_same_site_peer_being_removed_does_not_block_the_new_systems_authority_rule(monkeypatch):
    removing = [{"sys_id": REMOVING, "sys_name": "Site API", "removal_incomplete": True}]
    assert _mint(monkeypatch, removing) == [f"ar-{NEW}-{CONCEPT}"]


def test_a_same_site_peer_still_onboarded_keeps_blocking_it(monkeypatch):
    onboarded = [{"sys_id": PEER, "sys_name": "Corp ERP", "removal_incomplete": False}]
    assert _mint(monkeypatch, onboarded) == []


def _resolver_over(monkeypatch, rule_rows, field_rows=()):
    def query(cypher, params=None):
        if "AuthorityRule" in cypher:
            return copy.deepcopy(list(rule_rows))
        if "MAPS_TO" in cypher:
            return copy.deepcopy(list(field_rows))
        return []
    monkeypatch.setattr(mr, "neptune_query", query)
    return mr.GraphAuthorityResolver()


def test_after_a_failed_removal_its_rule_is_kept_but_never_selected(monkeypatch):
    """The failed removal leaves its marker and its active rule (audit); the
    new system's rule is the one authority, not an ambiguous pair."""
    resolver = _resolver_over(monkeypatch, [{"sid": NEW, "removal_incomplete": False},
                                            {"sid": REMOVING, "removal_incomplete": True}])
    assert resolver.authoritative_system(CONCEPT) == NEW


def test_after_a_completed_removal_the_new_systems_rule_is_the_authority(monkeypatch):
    resolver = _resolver_over(monkeypatch, [{"sid": NEW, "removal_incomplete": False}])
    assert resolver.authoritative_system(CONCEPT) == NEW


def test_a_mapping_system_being_removed_is_not_counted_for_the_single_system_fallback(monkeypatch):
    resolver = _resolver_over(monkeypatch, [], [{"sid": NEW, "removal_incomplete": False},
                                                {"sid": REMOVING, "removal_incomplete": True}])
    source = mr._resolve_authority(CONCEPT, resolver)
    assert (source.system_id, source.source) == (NEW, "single_system_map")


def test_a_rule_of_a_system_being_removed_never_vouches_across_sites(monkeypatch):
    rows = [{"id": "ar-rm", "concept": CONCEPT, "sid": REMOVING, "method": "user_confirmed",
             "approved_by": "", "evidence": "", "removal_incomplete": True}]
    monkeypatch.setattr(self_learning, "_neptune_query", lambda c, p=None: copy.deepcopy(rows))
    assert self_learning._authority_vouch_verdict(CONCEPT, {}) == ("none", "")
