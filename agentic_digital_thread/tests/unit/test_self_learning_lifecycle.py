# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline behavioral proof of ADR 0003's unified proposal lifecycle (S3.1, Task 3.1).

No AWS, no Neptune, no network. The DynamoDB table is replaced by an in-memory
``FakeTable`` (a dict keyed by ``(mapping_id, discovered_at)``) wired in via
``monkeypatch.setattr(self_learning, "_get_table", lambda: fake)``; the Neptune
write seam is replaced by a spy via
``monkeypatch.setattr(self_learning, "_neptune_write", spy)`` — matching the
exact module-global seam names ``self_learning`` actually uses (mirrors the
``monkeypatch.setattr(mr, "neptune_query", ...)`` style in
``tests/unit/test_metric_resolver.py``).

Covers (against the code's ACTUAL collision tags / dict shapes):
  - dup pending → no insert, returns existing id;
  - dup accepted → no-op, status stays accepted;
  - dup rejected → inserts + new item carries ``prior_rejections`` history;
  - same-subject/diff-target agent-over-agent → prior ``superseded``, new present;
  - source-aware: agent cannot supersede an accepted user correction; a user
    correction supersedes a prior accepted agent proposal;
  - monotonic version chain v1 → v2 → v3 along a supersede chain;
  - fail-loud: table raising on query/put_item, and a Neptune stub that raises
    OR returns ``False``, each make the critical-path fns RAISE (no swallow).
"""
from __future__ import annotations

import pytest

import agents.self_learning as self_learning
from tests.unit._value_mapping_write_seam import route_returning_writes


# --------------------------------------------------------------------------- #
# In-memory test doubles
# --------------------------------------------------------------------------- #
class ConditionalCheckFailedException(Exception):
    """Fake of the DynamoDB conditional-failure error — production detection is
    by class name (boto3's ClientError subclass carries the same name)."""


class FakeTable:
    """In-memory stand-in for the ``adt-proposed-mappings`` DynamoDB table.

    Keyed by ``(mapping_id, discovered_at)`` (the real PK/SK). ``query`` inspects
    the exact kwargs ``self_learning`` passes: ``subject-key-index`` (on
    ``subject_key``), ``status-index`` (on ``status``), and the main-table query
    by ``mapping_id`` (no ``IndexName``) used by ``accept_mapping``.
    """

    def __init__(self, *, raise_on_query: bool = False, raise_on_put: bool = False) -> None:
        self._items: dict[tuple[str, str], dict] = {}
        self._raise_on_query = raise_on_query
        self._raise_on_put = raise_on_put

    # -- helpers for assertions --------------------------------------------- #
    def __len__(self) -> int:
        return len(self._items)

    def all_items(self) -> list[dict]:
        return [dict(v) for v in self._items.values()]

    def by_id(self, mapping_id: str) -> dict:
        for v in self._items.values():
            if v.get("mapping_id") == mapping_id:
                return dict(v)
        raise KeyError(mapping_id)

    # -- DynamoDB-shaped surface -------------------------------------------- #
    def put_item(self, Item: dict) -> None:
        if self._raise_on_put:
            raise RuntimeError("put_item boom")
        self._items[(Item["mapping_id"], Item["discovered_at"])] = dict(Item)

    def query(self, **kwargs) -> dict:
        if self._raise_on_query:
            raise RuntimeError("query boom")
        index = kwargs.get("IndexName")
        values = kwargs.get("ExpressionAttributeValues", {})
        items = list(self._items.values())
        if index == "subject-key-index":
            result = [i for i in items if i.get("subject_key") == values[":sk"]]
        elif index == "status-index":
            result = [i for i in items if i.get("status") == values[":status"]]
        else:  # main-table query by mapping_id (accept_mapping / reject_mapping)
            result = [i for i in items if i.get("mapping_id") == values[":mid"]]
        reverse = not kwargs.get("ScanIndexForward", True)
        result.sort(key=lambda x: x.get("discovered_at", ""), reverse=reverse)
        return {"Items": [dict(i) for i in result]}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues=None,
                    ExpressionAttributeNames=None, ConditionExpression=None) -> None:
        item = self._items[(Key["mapping_id"], Key["discovered_at"])]
        names = ExpressionAttributeNames or {}
        values = ExpressionAttributeValues or {}
        if ConditionExpression is not None:
            # Only the '#s = :expected' shape is emitted by _update_status.
            assert ConditionExpression == "#s = :expected"
            if item.get(names.get("#s", "status")) != values[":expected"]:
                raise ConditionalCheckFailedException(Key["mapping_id"])
        expr = UpdateExpression.strip()
        set_part, remove_part = expr, None
        if " REMOVE " in expr:
            set_part, remove_part = expr.split(" REMOVE ", 1)
        if set_part.upper().startswith("SET "):
            for clause in set_part[4:].split(","):
                lhs, rhs = (s.strip() for s in clause.split("="))
                item[names.get(lhs, lhs)] = values[rhs]
        if remove_part:
            for tok in remove_part.split(","):
                item.pop(names.get(tok.strip(), tok.strip()), None)

    def delete_item(self, Key) -> None:
        self._items.pop((Key["mapping_id"], Key["discovered_at"]), None)

    def scan(self, **kwargs) -> dict:
        return {"Items": self.all_items()}


class NeptuneSpy:
    """Spy/stub for the ``_neptune_write`` seam (returns bool, FLAG d)."""

    def __init__(self, *, result=True, raises: bool = False) -> None:
        self.calls: list[tuple[str, dict | None]] = []
        self._result = result
        self._raises = raises

    def __call__(self, cypher: str, parameters: dict | None = None):
        self.calls.append((cypher, parameters))
        if self._raises:
            raise RuntimeError("neptune boom")
        return self._result

    def last_version(self):
        """The ``ver`` param of the most recent write that carried one."""
        for _cypher, params in reversed(self.calls):
            if params and "ver" in params:
                return params["ver"]
        return None


# --------------------------------------------------------------------------- #
# Wiring helpers
# --------------------------------------------------------------------------- #
def _install(monkeypatch, table: FakeTable, neptune: NeptuneSpy | None = None) -> NeptuneSpy:
    neptune = neptune or NeptuneSpy()
    monkeypatch.setattr(self_learning, "_get_table", lambda: table)
    monkeypatch.setattr(self_learning, "_neptune_write", neptune)
    route_returning_writes(monkeypatch, neptune)
    # ADR-0051 P5: threshold 1 disables the co-reference staging buffer so the
    # lifecycle/collision tests below keep exercising single-call inserts. The
    # dedicated P5 staging tests re-enable it (threshold 2) explicitly.
    monkeypatch.setattr(self_learning, "PROPOSAL_STAGING_MIN_COREFERENCES", 1)
    return neptune


def _log(*, from_value="c-100", to_field="accounts.id", to_value="acct-7",
         proposed_type="value_mapping", source="agent", query_context="ctx") -> str:
    """Log a value_mapping proposal. Same subject unless ``from_value`` changes;
    different target by varying ``to_value``/``to_field``."""
    return self_learning.log_proposed_mapping(
        "erp", "orders.customer_id", from_value,
        "crm", to_field, to_value,
        "customer", 0.9, query_context,
        proposed_type=proposed_type, source=source,
    )


def _correct(*, from_value="c-100", to_field="accounts.id", to_value="acct-7") -> dict:
    return self_learning.record_user_correction(
        "erp", "orders.customer_id", from_value,
        "crm", to_field, to_value,
        "customer",
    )


# --------------------------------------------------------------------------- #
# Collision policy (ADR 0003 C)
# --------------------------------------------------------------------------- #
def test_dup_pending_no_insert_returns_existing_id(monkeypatch):
    fake = FakeTable()
    _install(monkeypatch, fake)

    first = _log()
    assert len(fake) == 1

    second = _log()  # identical target → dup_pending
    assert second == first          # returns the existing id
    assert len(fake) == 1           # no second insert
    assert fake.by_id(first)["status"] == "pending"


def test_dup_accepted_is_noop(monkeypatch):
    fake = FakeTable()
    _install(monkeypatch, fake)

    mid = _log()
    self_learning.accept_mapping(mid)
    assert fake.by_id(mid)["status"] == "accepted"

    again = _log()                  # identical target, prior accepted → noop
    assert again == mid
    assert len(fake) == 1
    assert fake.by_id(mid)["status"] == "accepted"


def test_dup_rejected_inserts_with_rejection_history(monkeypatch):
    fake = FakeTable()
    _install(monkeypatch, fake)

    rejected_id = _log()
    self_learning.reject_mapping(rejected_id)
    rejected_row = fake.by_id(rejected_id)
    assert rejected_row["status"] == "rejected"
    # ADR-0042 D3: reject_mapping stamps rejected_at so damping/reason dates
    # are queryable.
    assert rejected_row.get("rejected_at")

    new_id = _log()                 # identical target, recent rejection → tombstoned (ADR-0053 E6)
    assert new_id != rejected_id
    assert len(fake) == 2
    new_item = fake.by_id(new_id)
    assert new_item["status"] == "pending"
    # ADR-0042 D3 shape change (R3): prior_rejections is a list of
    # {"mapping_id", "rejected_at"} dicts (was bare ids) — additive, new rows
    # only; readers handle both shapes. This is the ONE lifecycle assertion the
    # plan updates for the new shape.
    assert new_item.get("prior_rejections") == [
        {"mapping_id": rejected_id, "rejected_at": rejected_row["rejected_at"]}
    ]
    # ADR-0053 E6: a rejection inside the tombstone TTL flags the re-proposal
    # with the PINNED tombstone review reason (naming date + mapping id).
    assert new_item.get("review_reason") == (
        f"re-proposal of a shape the steward rejected on "
        f"{rejected_row['rejected_at'][:10]} (mapping {rejected_id})")


def test_same_subject_diff_target_agent_over_agent_supersedes(monkeypatch):
    fake = FakeTable()
    _install(monkeypatch, fake)

    prior_id = _log(to_value="acct-7")
    new_id = _log(to_value="acct-9")   # same subject, different target

    assert new_id != prior_id
    assert fake.by_id(prior_id)["status"] == "superseded"
    new_item = fake.by_id(new_id)
    assert new_item["status"] == "pending"
    assert new_item["version"] == "2"


# --------------------------------------------------------------------------- #
# Source-aware supersede (ADR 0003 D)
# --------------------------------------------------------------------------- #
def test_agent_cannot_supersede_accepted_user_correction(monkeypatch):
    fake = FakeTable()
    _install(monkeypatch, fake)

    user_result = _correct(to_value="acct-user")   # accepted user correction on the subject
    assert user_result["status"] == "accepted"
    user_item = next(i for i in fake.all_items() if i["source"] == "user")

    agent_id = _log(to_value="acct-agent")          # agent, same subject, different target

    agent_item = fake.by_id(agent_id)
    assert agent_item["status"] == "pending"        # record_disagreement → inserted pending
    assert agent_item["source"] == "agent"
    # prior accepted USER correction is untouched (agent cannot clobber it)
    assert fake.by_id(user_item["mapping_id"])["status"] == "accepted"


def test_user_correction_supersedes_prior_accepted_agent(monkeypatch):
    fake = FakeTable()
    _install(monkeypatch, fake)

    agent_id = _log(to_value="acct-agent")
    self_learning.accept_mapping(agent_id)
    assert fake.by_id(agent_id)["status"] == "accepted"

    result = _correct(to_value="acct-user")          # user wins → supersede the agent proposal
    assert result["status"] == "accepted"
    assert fake.by_id(agent_id)["status"] == "superseded"
    user_item = next(i for i in fake.all_items() if i["source"] == "user")
    assert user_item["status"] == "accepted"


# --------------------------------------------------------------------------- #
# Monotonic version chain (ADR 0003 E)
# --------------------------------------------------------------------------- #
def test_version_chain_v1_v2_v3(monkeypatch):
    fake = FakeTable()
    neptune = _install(monkeypatch, fake)

    a = _log(to_value="t1")                          # v1
    b = _log(to_value="t2")                          # supersede a → v2
    assert fake.by_id(a)["version"] == "1"
    assert fake.by_id(a)["status"] == "superseded"
    assert fake.by_id(b)["version"] == "2"

    result = _correct(to_value="t3")                 # user correction supersedes b → v3
    assert result["version"] == 3
    assert fake.by_id(b)["status"] == "superseded"
    # version stamped on the applied Neptune node (the SET ... vm.version = $ver)
    assert neptune.last_version() == 3


def test_accept_result_carries_version(monkeypatch):
    fake = FakeTable()
    neptune = _install(monkeypatch, fake)

    _log(to_value="t1")
    b = _log(to_value="t2")                           # v2
    result = self_learning.accept_mapping(b)
    assert result["status"] == "accepted"
    assert result["version"] == 2
    assert neptune.last_version() == 2


# --------------------------------------------------------------------------- #
# Fail-loud on the critical path (ADR 0003 F) — no swallow
# --------------------------------------------------------------------------- #
def test_log_raises_when_query_fails(monkeypatch):
    fake = FakeTable(raise_on_query=True)
    _install(monkeypatch, fake)
    with pytest.raises(Exception):
        _log()


def test_log_raises_when_put_fails(monkeypatch):
    fake = FakeTable(raise_on_put=True)              # query returns [] then put raises
    _install(monkeypatch, fake)
    with pytest.raises(Exception):
        _log()


def test_record_user_correction_raises_when_query_fails(monkeypatch):
    fake = FakeTable(raise_on_query=True)
    _install(monkeypatch, fake)
    with pytest.raises(Exception):
        _correct()


def test_record_user_correction_raises_when_put_fails(monkeypatch):
    fake = FakeTable(raise_on_put=True)
    _install(monkeypatch, fake)
    with pytest.raises(Exception):
        _correct()


def test_accept_raises_when_neptune_raises(monkeypatch):
    fake = FakeTable()
    _install(monkeypatch, fake, NeptuneSpy(raises=True))
    # log uses no Neptune; only accept hits the write seam
    mid = _log()
    with pytest.raises(Exception):
        self_learning.accept_mapping(mid)


def test_accept_raises_when_neptune_returns_false(monkeypatch):
    fake = FakeTable()
    _install(monkeypatch, fake, NeptuneSpy(result=False))
    mid = _log()
    with pytest.raises(RuntimeError):
        self_learning.accept_mapping(mid)


def test_record_user_correction_raises_when_neptune_raises(monkeypatch):
    fake = FakeTable()
    _install(monkeypatch, fake, NeptuneSpy(raises=True))
    with pytest.raises(Exception):
        _correct()


def test_record_user_correction_raises_when_neptune_returns_false(monkeypatch):
    fake = FakeTable()
    _install(monkeypatch, fake, NeptuneSpy(result=False))
    with pytest.raises(RuntimeError):
        _correct()


# --------------------------------------------------------------------------- #
# S3.2 / W2.1 — concept_mapping repoint of the authoritative MAPS_TO edge.
#
# The concept_mapping branch in _apply_to_neptune does NOT write a VM/JP/IR node;
# it (1) reads the current max r.version via the _neptune_query seam, (2) DELETEs
# every prior MAPS_TO edge for that (system, concept), then (3) MERGEs the
# corrected Field→Concept edge with role='identifier', method='user_corrected'
# and a monotonically bumped r.version. We capture both seams.
# --------------------------------------------------------------------------- #
class NeptuneGraphFake:
    """Write+query seam stub modelling the MAPS_TO edge version in the graph.

    ``query`` answers the max-version read with the last ``ver`` written, so a
    re-correction sees the bumped value (FLAG a: r.version = max + 1).
    """

    def __init__(self, *, write_result=True, write_raises: bool = False,
                 field_exists: bool = True, system_exists: bool = True,
                 graph_down: bool = False) -> None:
        self.writes: list[tuple[str, dict | None]] = []
        self.queries: list[tuple[str, dict | None]] = []
        self._max_ver = None
        self._write_result = write_result
        self._write_raises = write_raises
        self._field_exists = field_exists
        self._system_exists = system_exists
        self._graph_down = graph_down

    def write(self, cypher: str, parameters: dict | None = None):
        self.writes.append((cypher, parameters))
        if self._write_raises:
            raise RuntimeError("neptune boom")
        if self._write_result is False:
            return False
        if parameters and "ver" in parameters:
            self._max_ver = parameters["ver"]
        return True

    def query(self, cypher: str, parameters: dict | None = None):
        self.queries.append((cypher, parameters))
        if self._graph_down:
            # db.neptune_query swallows backend errors and returns [] — model a
            # graph outage as EVERY read coming back empty.
            return []
        # The repoint's fail-loud pre-check: does the corrected Field node exist?
        if "count(f)" in cypher:
            return [{"n": 1 if self._field_exists else 0}]
        # The honesty guard's System cross-check (graph outage vs missing column).
        if "count(s)" in cypher:
            return [{"n": 1 if self._system_exists else 0}]
        return [{"v": self._max_ver}]

    # -- assertion helpers -------------------------------------------------- #
    def edge_write(self):
        """The most recent MERGE that creates a corrected MAPS_TO edge (fid+ver)."""
        for cypher, params in reversed(self.writes):
            if "MERGE (f)-[r:MAPS_TO]->(c)" in cypher:
                return cypher, params
        raise AssertionError("no MAPS_TO edge MERGE captured")

    def cyphers(self):
        return [c for c, _ in self.writes]


def _install_graph(monkeypatch, table: FakeTable, graph: NeptuneGraphFake) -> None:
    monkeypatch.setattr(self_learning, "_get_table", lambda: table)
    monkeypatch.setattr(self_learning, "_neptune_write", graph.write)
    route_returning_writes(monkeypatch, graph.write)
    monkeypatch.setattr(self_learning, "_neptune_query", graph.query)


def _correct_concept(*, to_system="cmms", concept="mro-inventory",
                     to_field="parts.qty_on_hand") -> dict:
    return self_learning.record_user_correction(
        from_system="", from_field="", from_value="",
        to_system=to_system, to_field=to_field, to_value="",
        concept=concept, proposed_type="concept_mapping",
    )


def test_concept_mapping_repoint_delete_then_merge(monkeypatch):
    fake = FakeTable()
    graph = NeptuneGraphFake()
    _install_graph(monkeypatch, fake, graph)

    result = _correct_concept()

    assert result["status"] == "accepted"
    assert result["proposed_type"] == "concept_mapping"
    # The repoint surfaces the deterministic applied-node id.
    assert result["neptune_node_id"] == "mapsto-cmms-mro-inventory"

    cyphers = graph.cyphers()
    delete_idx = next(i for i, c in enumerate(cyphers) if "DELETE r" in c)
    edge_idx = next(i for i, c in enumerate(cyphers) if "MERGE (f)-[r:MAPS_TO]->(c)" in c)
    # DELETE the prior authoritative edge(s) BEFORE merging the corrected one.
    assert delete_idx < edge_idx
    # Concept MERGE-first safety.
    assert any("MERGE (c:Concept" in c for c in cyphers)

    edge_cypher, edge_params = graph.edge_write()
    assert "r.role = 'identifier'" in edge_cypher
    assert "r.method = 'user_corrected'" in edge_cypher
    # Field id format f"{system}.{table}.{column}" from "table.column".
    assert edge_params["fid"] == "cmms.parts.qty_on_hand"
    # No prior edge in-graph (query → None) ⇒ r.version = 1 + 1 = prior+1.
    assert edge_params["ver"] == 2


def test_concept_mapping_supersede_on_recorrection(monkeypatch):
    fake = FakeTable()
    graph = NeptuneGraphFake()
    _install_graph(monkeypatch, fake, graph)

    first = _correct_concept(to_field="parts.qty_on_hand")    # graph r.version → 2
    second = _correct_concept(to_field="inventory.on_hand")   # graph r.version → 3

    # DynamoDB supersede chain (version is the proposal-store monotonic counter).
    assert first["version"] == 1
    assert second["version"] == 2
    first_row = next(i for i in fake.all_items() if i["to_field"] == "parts.qty_on_hand")
    second_row = next(i for i in fake.all_items() if i["to_field"] == "inventory.on_hand")
    assert first_row["status"] == "superseded"
    assert second_row["status"] == "accepted"

    # Graph r.version is monotonic: first correction prior+1 (=2), second prior+2 (=3).
    _cypher, params = graph.edge_write()
    assert params["ver"] == 3
    assert params["fid"] == "cmms.inventory.on_hand"


def test_concept_mapping_fail_loud_when_neptune_returns_false(monkeypatch):
    fake = FakeTable()
    graph = NeptuneGraphFake(write_result=False)
    _install_graph(monkeypatch, fake, graph)
    with pytest.raises(RuntimeError):
        _correct_concept()


def test_concept_mapping_fail_loud_when_neptune_raises(monkeypatch):
    fake = FakeTable()
    graph = NeptuneGraphFake(write_raises=True)
    _install_graph(monkeypatch, fake, graph)
    with pytest.raises(Exception):
        _correct_concept()


# --------------------------------------------------------------------------- #
# Regression: the four legacy proposed_types still route to their own branches
# (no MAPS_TO repoint, no _neptune_query). Uses the write-only NeptuneSpy.
# --------------------------------------------------------------------------- #
def _cyphers(neptune: NeptuneSpy) -> list[str]:
    return [c for c, _ in neptune.calls]


def test_legacy_value_mapping_routes_to_valuemapping_node(monkeypatch):
    fake = FakeTable()
    neptune = _install(monkeypatch, fake)
    _correct(to_value="acct-7")  # value_mapping (the existing helper)
    cyphers = _cyphers(neptune)
    assert any("MERGE (vm:ValueMapping" in c for c in cyphers)
    assert not any("MAPS_TO" in c and "DELETE r" in c for c in cyphers)


def test_legacy_join_path_routes_to_joinpath_node(monkeypatch):
    fake = FakeTable()
    neptune = _install(monkeypatch, fake)
    self_learning.record_user_correction(
        "erp", "orders.customer_id", "", "crm", "accounts.id", "",
        "customer", proposed_type="join_path",
    )
    cyphers = _cyphers(neptune)
    assert any("MERGE (jp:JoinPath" in c for c in cyphers)
    assert not any("MAPS_TO" in c and "DELETE r" in c for c in cyphers)


def test_legacy_identity_rule_routes_to_identityrule_node(monkeypatch):
    fake = FakeTable()
    neptune = _install(monkeypatch, fake)
    self_learning.record_user_correction(
        "erp", "orders.customer_id", "", "crm", "accounts.id", "",
        "customer", proposed_type="identity_rule",
    )
    cyphers = _cyphers(neptune)
    assert any("MERGE (ir:IdentityRule" in c for c in cyphers)
    assert not any("MAPS_TO" in c and "DELETE r" in c for c in cyphers)


def test_legacy_field_annotation_routes_to_disambiguation(monkeypatch):
    fake = FakeTable()
    neptune = _install(monkeypatch, fake)
    self_learning.record_user_correction(
        "erp", "orders.ship_to", "", "", "", "",
        "", proposed_type="field_annotation",
        query_context="Auto-analysis: represents the ship date",
    )
    cyphers = _cyphers(neptune)
    assert any("f.disambiguation" in c for c in cyphers)
    assert not any("MAPS_TO" in c and "DELETE r" in c for c in cyphers)


# --------------------------------------------------------------------------- #
# S5.2 / W2.2 — field_concept_mint apply + column-keyed edge cleanup (R5).
#
# These tests need a Neptune double that ACTUALLY TRACKS MAPS_TO edges and
# Concept nodes — a write-only spy would falsely pass the edge-cleanup
# assertions (it never models a stale edge being removed). ``EdgeGraphFake``
# interprets the exact Cypher the apply path emits:
#   • column-keyed delete  MATCH (f:Field {id:$fid})-[r:MAPS_TO]->() DELETE r
#   • concept-keyed delete MATCH (s:System...)-[r:MAPS_TO]->(c:Concept {id:$concept}) DELETE r
#   • Concept MERGE        MERGE (c:Concept {id:$cid|$concept}) ...
#   • edge MERGE           MERGE (f)-[r:MAPS_TO]->(c) SET ...
# and answers the max-version read from the live edges, so re-mint/correction
# version bumps are exercised end-to-end.
# --------------------------------------------------------------------------- #
class EdgeGraphFake:
    """In-memory MAPS_TO graph: tracks Field→Concept edges and Concept nodes."""

    def __init__(self, *, write_result=True, write_raises: bool = False) -> None:
        self.writes: list[tuple[str, dict | None]] = []
        self.queries: list[tuple[str, dict | None]] = []
        # edges: list of {"fid", "concept", "role", "method", "confidence", "version"}
        self.edges: list[dict] = []
        self.concepts: dict[str, dict] = {}
        self._write_result = write_result
        self._write_raises = write_raises

    def write(self, cypher: str, parameters: dict | None = None):
        self.writes.append((cypher, parameters))
        if self._write_raises:
            raise RuntimeError("neptune boom")
        if self._write_result is False:
            return False
        p = parameters or {}

        if "DELETE r" in cypher:
            if "(f:Field {id: $fid})-[r:MAPS_TO]->()" in cypher:
                # column-keyed cleanup: drop EVERY edge leaving this column
                fid = p["fid"]
                self.edges = [e for e in self.edges if e["fid"] != fid]
            elif "Concept {id: $concept}" in cypher:
                # concept-keyed (system-scoped) cleanup
                sys_, concept = p["sys"], p["concept"]
                self.edges = [
                    e for e in self.edges
                    if not (e["concept"] == concept and e["fid"].startswith(sys_ + "."))
                ]
            return True

        if "MERGE (c:Concept" in cypher:
            cid = p.get("cid", p.get("concept"))
            if cid not in self.concepts:  # ON CREATE semantics
                node = {"id": cid}
                if "c.source = 'minted'" in cypher:
                    node["source"] = "minted"
                    node["name"] = p.get("name", cid)
                    node["version"] = p.get("ver")
                else:
                    node["name"] = p.get("name", p.get("concept", cid))
                self.concepts[cid] = node
            return True

        if "MERGE (f)-[r:MAPS_TO]->(c)" in cypher:
            fid = p["fid"]
            concept = p.get("cid", p.get("concept"))
            attrs = {"fid": fid, "concept": concept,
                     "confidence": p.get("conf"), "version": p.get("ver")}
            if "r.role = 'identifier'" in cypher:
                attrs["role"], attrs["method"] = "identifier", "user_corrected"
            elif "r.role = 'field_concept'" in cypher:
                attrs["role"], attrs["method"] = "field_concept", "minted"
            existing = next(
                (e for e in self.edges if e["fid"] == fid and e["concept"] == concept), None)
            if existing:
                existing.update(attrs)
            else:
                self.edges.append(attrs)
            return True

        return True  # FROM_SYSTEM / TO_SYSTEM / other merges — ignored

    def query(self, cypher: str, parameters: dict | None = None):
        self.queries.append((cypher, parameters))
        p = parameters or {}
        # The repoint's fail-loud pre-check: discovered columns exist by default
        # in this fake (set ``field_exists = False`` to model an undiscovered one).
        if "count(f)" in cypher:
            return [{"n": 1 if getattr(self, "field_exists", True) else 0}]
        sys_, concept = p.get("sys"), p.get("concept")
        vers = [e["version"] for e in self.edges
                if e["concept"] == concept and e["fid"].startswith(str(sys_) + ".")
                and e["version"] is not None]
        return [{"v": max(vers) if vers else None}]

    # -- assertion helpers -------------------------------------------------- #
    def edges_from(self, fid: str) -> list[dict]:
        return [e for e in self.edges if e["fid"] == fid]


def _install_edges(monkeypatch, table: FakeTable, graph: EdgeGraphFake) -> None:
    monkeypatch.setattr(self_learning, "_get_table", lambda: table)
    monkeypatch.setattr(self_learning, "_neptune_write", graph.write)
    route_returning_writes(monkeypatch, graph.write)
    monkeypatch.setattr(self_learning, "_neptune_query", graph.query)


def test_mint_creates_minted_concept_and_maps_to_edge(monkeypatch):
    fake = FakeTable()
    graph = EdgeGraphFake()
    _install_edges(monkeypatch, fake, graph)

    result = self_learning.mint_field_concept(
        "cmms", "parts", "qty_on_hand", "qty-on-hand", "Quantity On Hand",
        0.9, source="user",
    )

    assert result["status"] == "accepted"
    assert result["proposed_type"] == "field_concept_mint"
    assert result["neptune_node_id"] == "mint-cmms-parts-qty_on_hand"

    # Concept node minted with non-null source (survives onboard orphan cleanup).
    minted = graph.concepts["qty-on-hand"]
    assert minted["source"] == "minted"
    assert minted["name"] == "Quantity On Hand"

    # Exactly one MAPS_TO from the column's Field, pointing at the minted concept.
    fid = "cmms.parts.qty_on_hand"
    edges = graph.edges_from(fid)
    assert len(edges) == 1
    assert edges[0]["concept"] == "qty-on-hand"
    assert edges[0]["role"] == "field_concept"


def test_remint_same_column_diff_concept_leaves_one_live_edge(monkeypatch):
    fake = FakeTable()
    graph = EdgeGraphFake()
    _install_edges(monkeypatch, fake, graph)

    first = self_learning.mint_field_concept(
        "cmms", "parts", "qty_on_hand", "qty-on-hand", "Quantity On Hand",
        0.9, source="user",
    )
    second = self_learning.mint_field_concept(
        "cmms", "parts", "qty_on_hand", "inventory-qty", "Inventory Quantity",
        0.9, source="user",
    )

    # Proposal store: same COLUMN subject ⇒ prior superseded, monotonic version.
    assert first["version"] == 1
    assert second["version"] == 2
    first_row = next(i for i in fake.all_items() if i["concept"] == "qty-on-hand")
    second_row = next(i for i in fake.all_items() if i["concept"] == "inventory-qty")
    assert first_row["status"] == "superseded"
    assert second_row["status"] == "accepted"

    # ONE live edge from the column; the stale mint edge is gone (column-keyed R5).
    fid = "cmms.parts.qty_on_hand"
    edges = graph.edges_from(fid)
    assert len(edges) == 1
    assert edges[0]["concept"] == "inventory-qty"
    assert edges[0]["version"] == 2


def test_user_concept_mapping_correction_wins_over_prior_mint(monkeypatch):
    fake = FakeTable()
    graph = EdgeGraphFake()
    _install_edges(monkeypatch, fake, graph)

    # 1) mint the column at one concept.
    self_learning.mint_field_concept(
        "cmms", "parts", "qty_on_hand", "qty-on-hand", "Quantity On Hand",
        0.9, source="user",
    )
    fid = "cmms.parts.qty_on_hand"
    assert [e["concept"] for e in graph.edges_from(fid)] == ["qty-on-hand"]

    # 2) later USER concept_mapping correction on the SAME column to a DIFFERENT
    #    concept. Its subject is (to_system, concept), so the concept-keyed delete
    #    alone would NOT touch the mint edge — the column-keyed cleanup must.
    result = self_learning.record_user_correction(
        from_system="", from_field="", from_value="",
        to_system="cmms", to_field="parts.qty_on_hand", to_value="",
        concept="mro-inventory", proposed_type="concept_mapping",
    )
    assert result["status"] == "accepted"

    # The column's live edge now points at the corrected concept; mint edge gone.
    edges = graph.edges_from(fid)
    assert len(edges) == 1
    assert edges[0]["concept"] == "mro-inventory"
    assert edges[0]["role"] == "identifier"
    # No edge anywhere still points the column at the stale minted concept.
    assert not any(e["concept"] == "qty-on-hand" for e in graph.edges)


def test_agent_mint_does_not_clobber_accepted_user_mapping(monkeypatch):
    fake = FakeTable()
    graph = EdgeGraphFake()
    _install_edges(monkeypatch, fake, graph)

    # User mint enters accepted and writes the authoritative edge.
    user_result = self_learning.mint_field_concept(
        "cmms", "parts", "qty_on_hand", "qty-on-hand", "Quantity On Hand",
        0.95, source="user",
    )
    assert user_result["status"] == "accepted"
    fid = "cmms.parts.qty_on_hand"
    assert [e["concept"] for e in graph.edges_from(fid)] == ["qty-on-hand"]

    writes_before = len(graph.writes)

    # Agent mint on the SAME column to a different concept: source-aware policy
    # ⇒ record_disagreement (pending), NOT an apply. The live edge is untouched.
    agent_id = self_learning.mint_field_concept(
        "cmms", "parts", "qty_on_hand", "spare-stock", "Spare Stock",
        0.6, source="agent",
    )
    assert isinstance(agent_id, str)
    agent_row = fake.by_id(agent_id)
    assert agent_row["status"] == "pending"
    assert agent_row["source"] == "agent"

    # The accepted user mint is untouched, and no new Neptune write occurred.
    user_row = next(i for i in fake.all_items() if i["source"] == "user")
    assert user_row["status"] == "accepted"
    assert len(graph.writes) == writes_before
    edges = graph.edges_from(fid)
    assert len(edges) == 1
    assert edges[0]["concept"] == "qty-on-hand"


# --------------------------------------------------------------------------- #
# Concept descriptions first-class (ADR 0021) — the minted concept's cosmetic
# definition threads through the proposal lifecycle (to_description) and is
# persisted on the minted Concept node ONLY when non-empty.
# --------------------------------------------------------------------------- #
def test_agent_mint_with_description_stores_to_description(monkeypatch):
    fake = FakeTable()
    _install(monkeypatch, fake)

    mid = self_learning.mint_field_concept(
        "cmms", "parts", "qty_on_hand", "qty-on-hand", "Quantity On Hand",
        0.6, source="agent", description="Quantity of spare parts on hand.",
    )
    item = fake.by_id(mid)
    assert item["to_description"] == "Quantity of spare parts on hand."


def test_agent_mint_without_description_omits_to_description_key(monkeypatch):
    fake = FakeTable()
    _install(monkeypatch, fake)

    mid = self_learning.mint_field_concept(
        "cmms", "parts", "qty_on_hand", "qty-on-hand", "Quantity On Hand",
        0.6, source="agent",
    )
    item = fake.by_id(mid)
    assert "to_description" not in item  # never write an empty-string DynamoDB attr


def test_apply_mint_with_description_writes_concept_description(monkeypatch):
    fake = FakeTable()
    neptune = _install(monkeypatch, fake)  # write-only spy captures cypher+params

    self_learning.mint_field_concept(
        "cmms", "parts", "qty_on_hand", "qty-on-hand", "Quantity On Hand",
        0.9, source="user", description="Quantity of spare parts on hand.",
    )
    desc_writes = [
        (c, p) for c, p in neptune.calls if "c.description = $desc" in c
    ]
    assert len(desc_writes) == 1
    _cypher, params = desc_writes[0]
    assert params["cid"] == "qty-on-hand"
    assert params["desc"] == "Quantity of spare parts on hand."


def test_apply_mint_without_description_writes_no_description(monkeypatch):
    fake = FakeTable()
    neptune = _install(monkeypatch, fake)

    self_learning.mint_field_concept(
        "cmms", "parts", "qty_on_hand", "qty-on-hand", "Quantity On Hand",
        0.9, source="user",
    )
    assert not any("c.description" in c for c, _ in neptune.calls)


def test_apply_mint_with_core_cid_never_mutates_core_description(monkeypatch):
    """GUARD (ADR 0021): a mint whose cid collides with a CURATED CORE concept id must
    NEVER overwrite the core definition — no ``SET c.description`` write is emitted, even
    when the proposal carries a ``to_description``."""
    from agents.catalog import CORE_CONCEPT_DEFINITIONS
    core_cid = "mro-inventory"
    assert core_cid in CORE_CONCEPT_DEFINITIONS  # sanity: it really is curated/core

    fake = FakeTable()
    neptune = _install(monkeypatch, fake)

    self_learning.mint_field_concept(
        "cmms", "parts", "qty_on_hand", core_cid, "MRO Inventory",
        0.9, source="user", description="A bogus override of the curated core text.",
    )
    # The curated core description is immutable: no description write at all.
    assert not any("c.description = $desc" in c for c, _ in neptune.calls)


def test_apply_mint_with_non_core_cid_still_writes_description(monkeypatch):
    """A non-core (minted/dynamic) cid with a to_description DOES emit the description
    write — the guard only protects curated core ids."""
    from agents.catalog import CORE_CONCEPT_DEFINITIONS
    non_core_cid = "lead-time-days"
    assert non_core_cid not in CORE_CONCEPT_DEFINITIONS

    fake = FakeTable()
    neptune = _install(monkeypatch, fake)

    self_learning.mint_field_concept(
        "cmms", "spare_parts", "lead_time_days", non_core_cid, "Lead Time Days",
        0.9, source="user", description="Days between ordering a part and its receipt.",
    )
    desc_writes = [(c, p) for c, p in neptune.calls if "c.description = $desc" in c]
    assert len(desc_writes) == 1
    _cypher, params = desc_writes[0]
    assert params["cid"] == non_core_cid
    assert params["desc"] == "Days between ordering a part and its receipt."


# --------------------------------------------------------------------------- #
# ADR-0051 P3 — reverse-direction conflict flag (the audited B→A′ defect)
# --------------------------------------------------------------------------- #
def _accepted_forward_row(table_fake: FakeTable, monkeypatch) -> str:
    """Seed an ACCEPTED agent A→B value_mapping through the real path."""
    _install(monkeypatch, table_fake)
    mid = _log()  # erp.orders.customer_id c-100 → crm.accounts.id acct-7
    self_learning.accept_mapping(mid)
    self_learning._reset_accepted_coverage_cache()  # snapshot must see the accept
    return mid


def test_reverse_direction_conflict_flagged(monkeypatch):
    fake = FakeTable()
    accepted_id = _accepted_forward_row(fake, monkeypatch)

    # B→A′: reversed direction over the same anchor (crm acct-7) but a
    # DIFFERENT counterpart value — a contradiction, not a restatement.
    conflicting = self_learning.log_proposed_mapping(
        "crm", "accounts.id", "acct-7",
        "erp", "orders.customer_id", "c-999",
        "customer", 0.9, "reverse observation",
    )
    row = fake.by_id(conflicting)
    assert row["status"] == "pending"  # flagged, not blocked
    assert row["review_reason"] == (
        f"conflicts-with-accepted-reverse-mapping {accepted_id}")
    # No supersede happened — the accepted row is untouched.
    assert fake.by_id(accepted_id)["status"] == "accepted"


def test_reverse_direction_consistent_is_not_flagged(monkeypatch):
    fake = FakeTable()
    _accepted_forward_row(fake, monkeypatch)

    # B→A with the SAME value pair — a consistent restatement, never flagged.
    consistent = self_learning.log_proposed_mapping(
        "crm", "accounts.id", "acct-7",
        "erp", "orders.customer_id", "c-100",
        "customer", 0.9, "reverse observation",
    )
    row = fake.by_id(consistent)
    assert "review_reason" not in row


def test_reverse_conflict_probe_is_agent_only(monkeypatch):
    fake = FakeTable()
    _accepted_forward_row(fake, monkeypatch)

    # ADR-0003 C: a USER-source proposal is never flagged by the agent probe.
    user_row = self_learning.log_proposed_mapping(
        "crm", "accounts.id", "acct-7",
        "erp", "orders.customer_id", "c-999",
        "customer", 1.0, "user correction", source="user",
    )
    assert "review_reason" not in fake.by_id(user_row)


# --------------------------------------------------------------------------- #
# ADR-0051 P10 — the tool-calls fiction is gone from stored items
# --------------------------------------------------------------------------- #
def test_item_has_no_tool_calls_after(monkeypatch):
    fake = FakeTable()
    _install(monkeypatch, fake)
    mid = _log()
    assert "tool_calls_after" not in fake.by_id(mid)


def test_item_omits_tool_calls_before(monkeypatch):
    fake = FakeTable()
    _install(monkeypatch, fake)
    mid = _log()
    # Absent (not "0"): the optional frontend type + both render guards treat
    # absent as nothing-to-show; value evidence is the ADR-0050 D6 usage ledger.
    assert "tool_calls_before" not in fake.by_id(mid)


def test_agents_package_never_writes_tool_calls_after():
    # Grep-style pin: no writer of the fictional field remains in agents/.
    from pathlib import Path
    import agents
    agents_dir = Path(agents.__file__).parent
    offenders = [
        str(p) for p in sorted(agents_dir.glob("*.py"))
        if '"tool_calls_after"' in p.read_text(encoding="utf-8")
        or "'tool_calls_after'" in p.read_text(encoding="utf-8")
    ]
    assert offenders == []


# --------------------------------------------------------------------------- #
# ADR-0051 P5 — co-reference staging buffer (agent cross-system VM/JP only).
# The suite-wide _install disables staging; these tests re-enable it.
# --------------------------------------------------------------------------- #
def _install_with_staging(monkeypatch, table: FakeTable) -> None:
    _install(monkeypatch, table)
    monkeypatch.setattr(self_learning, "PROPOSAL_STAGING_MIN_COREFERENCES", 2)


def _staged_rows(table: FakeTable) -> list[dict]:
    return [i for i in table.all_items()
            if str(i.get("mapping_id", "")).startswith("staged#")]


def test_first_agent_observation_stages_not_pending(monkeypatch):
    table = FakeTable()
    _install_with_staging(monkeypatch, table)
    assert _log(query_context="first co-reference") is None
    (staged,) = _staged_rows(table)
    assert "status" not in staged                      # invisible to lifecycle reads
    assert staged["subject_key"].startswith("staged#")  # invisible to subject lookups
    assert len(table) == 1                              # NO pending row exists
    assert self_learning.get_pending_mappings() == []


def test_second_coreference_promotes_to_pending(monkeypatch):
    table = FakeTable()
    _install_with_staging(monkeypatch, table)
    assert _log(query_context="turn one: joined order to account") is None
    mid = _log(query_context="turn two: same pair resolved again")
    assert mid is not None
    stored = table.by_id(mid)
    assert stored["status"] == "pending"
    # The released row's query_context names BOTH observations.
    assert "2 observed co-references" in stored["query_context"]
    assert "turn one" in stored["query_context"]
    assert "turn two" in stored["query_context"]
    assert _staged_rows(table) == []  # buffer consumed on release


def test_identical_reobservation_is_one_coreference(monkeypatch):
    # The same query replayed is NOT a second co-reference — still staged.
    table = FakeTable()
    _install_with_staging(monkeypatch, table)
    assert _log(query_context="same ctx") is None
    assert _log(query_context="same ctx") is None
    (staged,) = _staged_rows(table)
    assert len(staged["staged_digests"]) == 1


def test_user_source_bypasses_staging(monkeypatch):
    table = FakeTable()
    _install_with_staging(monkeypatch, table)
    mid = _log(source="user", query_context="steward assertion")
    assert mid is not None
    assert table.by_id(mid)["status"] == "pending"
    assert _staged_rows(table) == []


def test_promotion_sweep_provenance_bypasses_staging(monkeypatch):
    # ADR-0043 promoted artifacts already carry >= 3-value evidence — the
    # value_promotion provenance goes straight to a pending row.
    table = FakeTable()
    _install_with_staging(monkeypatch, table)
    mid = self_learning.log_proposed_mapping(
        "erp", "orders.customer_id", "c-100",
        "crm", "accounts.id", "acct-7",
        "customer", 0.9, "Promoted from 3 pending per-value mapping(s)",
        proposed_type="join_path", source="agent",
        provenance="value_promotion",
    )
    assert mid is not None
    assert table.by_id(mid)["status"] == "pending"
    assert _staged_rows(table) == []


def test_same_system_observation_bypasses_staging(monkeypatch):
    # A same-system code→label observation is single-system evidence, not a
    # cross-system co-reference — stored directly (e.g. reason-code learning).
    table = FakeTable()
    _install_with_staging(monkeypatch, table)
    mid = self_learning.log_proposed_mapping(
        "erp", "downtime.reason_code", "417",
        "erp", "downtime.reason_text", "bearing overheat",
        "failure-mode", 0.9, "code observed with label",
        proposed_type="value_mapping", source="agent",
    )
    assert mid is not None


def test_response_integrity_provenance_bypasses_staging(monkeypatch):
    # Integrity-typed proposals carry VERIFIED current-turn
    # proof (provenance 'deterministic-response-integrity') — the
    # ≥2-co-reference bar is met by construction, so they are never buffered.
    table = FakeTable()
    _install_with_staging(monkeypatch, table)
    mid = self_learning.log_proposed_mapping(
        "erp", "orders.customer_id", "c-100",
        "crm", "accounts.id", "acct-7",
        "customer", 0.9, "Validated structural learning proposal",
        proposed_type="value_mapping", source="agent",
        provenance="deterministic-response-integrity",
    )
    assert mid is not None
    assert table.by_id(mid)["status"] == "pending"
    assert _staged_rows(table) == []


def test_staged_evidence_survives_failing_pending_put(monkeypatch):
    # The release path must not delete the staged buffer before
    # the pending put succeeds — a raise keeps the evidence for the retry.
    table = FakeTable()
    _install_with_staging(monkeypatch, table)
    assert _log(query_context="first co-reference") is None
    real_put = self_learning._put_item

    def _pending_put_boom(tbl, item):
        if item.get("status") == "pending":
            raise RuntimeError("pending put boom")
        return real_put(tbl, item)

    monkeypatch.setattr(self_learning, "_put_item", _pending_put_boom)
    with pytest.raises(RuntimeError, match="pending put boom"):
        _log(query_context="second co-reference")
    (staged,) = _staged_rows(table)  # evidence intact after the failed release
    assert len(staged["staged_digests"]) == 1
    monkeypatch.setattr(self_learning, "_put_item", real_put)
    mid = _log(query_context="second co-reference")  # retry releases cleanly
    assert mid is not None
    assert table.by_id(mid)["status"] == "pending"
    assert _staged_rows(table) == []


def test_last_propose_outcome_distinguishes_staged_from_stored(monkeypatch):
    # Synchronous callers (the save_value_mapping tool) read
    # last_propose_outcome() to tell a STAGED observation apart from a drop.
    table = FakeTable()
    _install_with_staging(monkeypatch, table)
    assert _log(query_context="first co-reference") is None
    assert self_learning.last_propose_outcome() == "staged"
    assert _log(query_context="first co-reference") is None  # identical replay
    assert self_learning.last_propose_outcome() == "staged"
    mid = _log(query_context="a second distinct observation")
    assert mid is not None
    assert self_learning.last_propose_outcome() == ""  # reset per call
    assert _staged_rows(table) == []


def test_staged_rows_invisible_to_get_all_mappings(monkeypatch):
    table = FakeTable()
    _install_with_staging(monkeypatch, table)
    assert _log(query_context="first co-reference") is None
    assert len(_staged_rows(table)) == 1
    assert self_learning.get_all_mappings() == []


def test_stale_staged_row_expires_and_restarts_the_buffer(monkeypatch):
    table = FakeTable()
    _install_with_staging(monkeypatch, table)
    assert _log(query_context="ancient observation") is None
    (staged,) = _staged_rows(table)
    # Age the staged row past STAGING_TTL_DAYS in place.
    old = dict(staged)
    old["discovered_at"] = "2020-01-01T00:00:00+00:00"
    table.put_item(Item=old)
    table.delete_item(Key={"mapping_id": staged["mapping_id"],
                           "discovered_at": staged["discovered_at"]})
    # A distinct second observation does NOT promote — the first decayed.
    assert _log(query_context="fresh observation years later") is None
    (fresh,) = _staged_rows(table)
    assert len(fresh["staged_digests"]) == 1
    assert self_learning.get_pending_mappings() == []


def test_get_all_mappings_follows_last_evaluated_key(monkeypatch):
    """A single table.scan() returns at most one 1 MB page — the scan MUST
    follow LastEvaluatedKey to the end. Live-found 2026-08-06: the bell and
    the Vocabulary review both undercounted a month's queue (486 of 850+)
    because only the first page was read."""

    class _PagedTable:
        def __init__(self):
            self.calls = 0

        def scan(self, **kwargs):
            self.calls += 1
            if not kwargs.get("ExclusiveStartKey"):
                return {"Items": [{"mapping_id": f"a{i}", "status": "pending"}
                                  for i in range(3)],
                        "LastEvaluatedKey": {"mapping_id": "a2"}}
            return {"Items": [{"mapping_id": f"b{i}", "status": "pending"}
                              for i in range(2)]}

    table = _PagedTable()
    monkeypatch.setattr(self_learning, "_get_table", lambda: table)
    rows = self_learning.get_all_mappings()
    assert len(rows) == 5, "second scan page was dropped"
    assert table.calls == 2


def test_ledger_and_cost_rows_invisible_to_get_all_mappings(monkeypatch):
    """usage#/cost# row families share the table (ADR-0052 evidence ledger,
    ADR-0053 measured cost) but are NOT lifecycle rows — they leaked into the
    review list as status-less "Unknown type" noise (live-found 2026-08-06)."""

    class _Table:
        def scan(self, **kwargs):
            return {"Items": [
                {"mapping_id": "usage#value_mapping#abc", "reads": 5},
                {"mapping_id": "cost#turns", "turns": 485},
                {"mapping_id": "staged#x", "evidence": 1},
                {"mapping_id": "real-1", "status": "pending"},
            ]}

    monkeypatch.setattr(self_learning, "_get_table", lambda: _Table())
    rows = self_learning.get_all_mappings()
    assert [r["mapping_id"] for r in rows] == ["real-1"]


# --------------------------------------------------------------------------- #
# ADR-0063 context-placement diagnostic lifecycle
# --------------------------------------------------------------------------- #
def test_accepting_context_placement_finding_performs_zero_graph_writes(monkeypatch):
    """Placement rows reuse the REAL acknowledge-only coverage-gap dispatch."""
    from agents import context_placement as cp

    table = FakeTable()
    spy = _install(monkeypatch, table)
    monkeypatch.setattr(self_learning, "known_systems", lambda: [
        {"id": "sys-a", "name": "System A"},
        {"id": "sys-b", "name": "System B"},
    ])
    finding = {"term": "CODE_ONE", "source_system": "sys-a",
               "target_system": "sys-b", "evidence_kind": "sample-value",
               "evidence_ref": "events.code"}
    assert cp._file_context_placement_proposals([finding]) == 1
    mapping_id = table.all_items()[0]["mapping_id"]
    writes_before_accept = list(spy.calls)

    result = self_learning.accept_mapping(mapping_id, actor="steward@example.com")

    assert result == {"status": "accepted", "acknowledged": True,
                      "proposed_type": "coverage_gap", "version": 1}
    assert table.by_id(mapping_id)["status"] == "accepted"
    assert spy.calls == writes_before_accept
