# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Regression tests for the W-group onboarding-writer quality fixes.

Each test reproduces an AUDITED defect from the 2026-07-24 learning-quality
audits (Management + Pilot) and pins the fix:

* W1 — content-hash rule/joinpath ids: the same rule emitted twice in one LLM
  response used to mint TWO index-keyed nodes (``ir-<sys>-14..17`` ×4 in the
  Management audit); a content digest collapses them to ONE MERGE id, stable
  across runs. The ``ir-<system_id>-`` prefix is KEPT — it is a live contract
  with ``_stamp_identity_rule_provenance``.
* W2 — ``_dp_rule_joinpaths`` used to re-walk ALL rules including rejected
  ones (the ``jp-plm-*-downtime`` family's production path); rejected rules
  now never produce a JoinPath.
* W3 — ``_is_valid_identity_rule`` was allow-all; the semantic-role gate
  rejects identifier→measure pairings (the audited ``bom_id`` →
  ``duration_minutes`` rule), stream-name-as-value endpoints, and
  site⊃equipment grain mixes — while UUID/wildcard direct-match rules still
  pass (the reason the gate was originally opened).
* W4 — LLM-supplied confidence is capped at 0.7 without a passed live
  value-sample check, and divided by K on source-field fan-out (the audited
  one-source→4-targets rules all carried full confidence).

All impure seams (Neptune, Postgres, catalog) are stubbed — no network, no
AWS. Fixture names are generic; the only audited literals reproduced are the
defect field names themselves, which live ONLY in this test file (ADR-0044
governs ``agents/*.py``, and the gate itself is pure shape rules).

Runnable two ways (matches ``test_onboard_provision_node_write.py``):
  * pytest:      pytest agentic_digital_thread/tests/unit/test_onboard_rule_writer_quality.py
  * standalone via pytest main block at the bottom.
"""
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

# Make `agents...` importable (agentic_digital_thread/ is two levels up from here).
_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

# A password MUST be set or demo.db_routing / db raise; tests never connect.
os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.onboard as onboard  # noqa: E402
from tests.unit._rule_check_db import measured_as_sent  # noqa: E402


# --------------------------------------------------------------------------- #
# Fixtures / seams
# --------------------------------------------------------------------------- #

class _FakeCatalog:
    """Target systems resolve as onboarded; entries are unknown (no schema),
    so protocol-specific field checks are skipped — the writer path under test
    is the id/confidence/rejection logic, not Postgres introspection."""

    def __init__(self, onboarded):
        self._onboarded = set(onboarded)

    def get(self, _system_id):
        return None

    def get_onboarded_ids(self):
        return set(self._onboarded)


def _rule(**over):
    """A generic, gate-passing identity rule; override fields per test."""
    base = {
        "concept": "order-id",
        "source_field": "work_order_id",
        "source_table": "orders_table",
        "source_pattern": "",
        "target_system": "target-sys",
        "target_field": "order_ref",
        "target_pattern": "",
        "confidence": 0.9,
    }
    base.update(over)
    return base


def _make_ctx(rules):
    """A minimal _ProvisionState for the c/c2 phase helpers. A non-SQL
    connection_type keeps the live-sample pattern check skipped (so
    sample_check_passed is False — the W4 cap applies) and the c2 format gate
    pass-through."""
    return onboard._ProvisionState(
        body=SimpleNamespace(value_mappings=[]),
        system_id="source-sys",
        system_name="Source System",
        plant="",
        schema="",
        rules=rules,
        mappings=[],
        connection_type="MCP Server",
        cm_items=[],
        committable=set(),
    )


@pytest.fixture
def writes(monkeypatch):
    """Record every neptune_write; neutralize all impure seams."""
    recorded: list[tuple[str, dict]] = []
    monkeypatch.setattr(onboard, "neptune_write",
                        lambda c, p=None: recorded.append((c, p or {})))

    def _nq(cypher, params=None):
        if "HAS_FIELD" in cypher:  # c2 target-table derivation
            return [{"tbl": "target_table"}]
        return []

    monkeypatch.setattr(onboard, "neptune_query", _nq)
    monkeypatch.setattr(onboard, "pg", lambda *_a, **_k: {"rows": []})
    monkeypatch.setattr(onboard, "catalog",
                        _FakeCatalog(onboarded={"target-sys", "other-sys",
                                                "third-sys"}))
    return recorded


def _ir_ids(recorded):
    return [p["rid"] for c, p in recorded if c.startswith("MERGE (ir:IdentityRule")]


def _ir_params(recorded):
    return [p for c, p in recorded if c.startswith("MERGE (ir:IdentityRule")]


def _jp_ids(recorded):
    return [p["jpid"] for c, p in recorded if c.startswith("MERGE (jp:JoinPath")]


# --------------------------------------------------------------------------- #
# W1 — content-hash ids
# --------------------------------------------------------------------------- #

def test_duplicate_rule_fixture_writes_one_node(writes):
    """AUDITED DEFECT: the same rule emitted twice in one LLM response minted
    two index-keyed nodes (Mgmt ir-cmms-14..17 ×4; Pilot ir-lake-athena-0/1 +
    jp-...-0/1). Content-digest MERGE ids collapse the pair to ONE IdentityRule
    node and ONE JoinPath node."""
    ctx = _make_ctx([_rule(), _rule()])  # identical content, different positions
    onboard._dp_identity_rules(ctx)
    onboard._dp_rule_joinpaths(ctx)
    ir, jp = _ir_ids(writes), _jp_ids(writes)
    assert len(ir) == 2 and len(set(ir)) == 1, ir  # both MERGEs hit the SAME id
    assert len(jp) == 2 and len(set(jp)) == 1, jp
    # The system prefix is a live contract (_stamp_identity_rule_provenance
    # stamps `ir-<system_id>-`); the digest replaces only the loop index.
    assert ir[0].startswith("ir-source-sys-")
    assert jp[0].startswith("jp-source-sys-")


def test_id_is_stable_across_runs(writes):
    """The same rule dict must produce the same id on every run (idempotent
    re-provision — no new node minted for unchanged content)."""
    first = _make_ctx([_rule()])
    onboard._dp_identity_rules(first)
    second = _make_ctx([_rule()])
    onboard._dp_identity_rules(second)
    ir = _ir_ids(writes)
    assert len(ir) == 2 and ir[0] == ir[1]
    assert onboard._rule_digest_for("source-sys", _rule()) == \
        onboard._rule_digest_for("source-sys", _rule())


def _exact(source_table, target_table, column="customer_code"):
    """An exact-agreement rule as the affinity pass emits it (tables carried)."""
    return _rule(concept="customer", source_field=f"{source_table}.{column}",
                 source_table=source_table, target_field=f"{target_table}.{column}",
                 target_table=target_table, method="value_exact_match", transform="identity",
                 shared_count=18, confidence=0.85,
                 reasoning="18 exact values ... both columns shape AAA-9999; concept customer")


def _jp_tables(recorded):
    return sorted((p["ftbl"], p["ttbl"]) for c, p in recorded if c.startswith("MERGE (jp:JoinPath"))


@pytest.mark.parametrize("pair", [
    (("customers", "accounts"), ("customer_credit", "accounts")),    # two sources, one target
    (("accounts", "customers"), ("accounts", "customer_credit"))])   # one source, two targets
def test_two_joins_that_differ_only_in_a_table_write_two_nodes(writes, pair):
    """A base table and its one-to-one extension hold the same key column
    name; both join the peer's column under one concept. Leaf names and the
    concept are equal, so the content digest must also read the tables: two
    IdentityRule ids, two JoinPath ids, each JoinPath on its own table."""
    rules = [_exact(*pair[0]), _exact(*pair[1])]
    ctx = _make_ctx(rules)
    onboard._dp_identity_rules(ctx)
    onboard._dp_rule_joinpaths(ctx)
    assert ctx.rules_rejected == []
    assert len(set(_ir_ids(writes))) == 2 and len(set(_jp_ids(writes))) == 2
    assert _jp_tables(writes) == sorted((f".{s}", t) for s, t in pair)


def test_an_llm_rule_on_another_table_and_an_exact_rule_get_two_ids(writes):
    """The resolve keeps an LLM rule scoped to other tables beside the exact
    rule for the same column names. The model wrote its tables only in its
    table-qualified fields (the prompt's shape), so those are its tables:
    two IdentityRule ids, not one MERGE. A schema prefix on a table is the
    same table."""
    llm = _rule(concept="customer", source_field="clients.customer_code",
                target_field="accounts.customer_code", source_table="", target_table="",
                source_pattern="C-{id}", target_pattern="CU-{id}")
    same_table = _rule(concept="customer", source_field="erp_data.customers.customer_code",
                       target_field="accounts.customer_code", source_table="", target_table="")
    ctx = _make_ctx([llm, _exact("customers", "accounts"), same_table])
    onboard._dp_identity_rules(ctx)
    ids = _ir_ids(writes)
    assert len(set(ids)) == 2 and ids[1] == ids[2]


def test_a_rule_without_a_table_twin_keeps_its_id(writes):
    """Only rules whose leaf digest collides with a rule on other tables read
    the tables; every other rule keeps the id it had (re-provision stays
    idempotent), including an exact rule and a duplicate of itself."""
    lone, plain = _exact("customers", "accounts"), _rule()
    ctx = _make_ctx([lone, dict(lone), plain])
    onboard._dp_identity_rules(ctx)
    leaf = onboard._rule_content_digest
    assert sorted(set(_ir_ids(writes))) == sorted({
        f"ir-source-sys-{leaf('source-sys', 'customer_code', 'target-sys', 'customer_code', 'customer')}",
        f"ir-source-sys-{leaf('source-sys', 'work_order_id', 'target-sys', 'order_ref', 'order-id')}"})


def test_different_concept_different_id():
    """Distinct content (here: the concept) must produce distinct ids."""
    a = onboard._rule_digest_for("source-sys", _rule(concept="order-id"))
    b = onboard._rule_digest_for("source-sys", _rule(concept="material-id"))
    assert a != b


# --------------------------------------------------------------------------- #
# W3 — semantic-role gate (implemented before W2, which depends on it)
# --------------------------------------------------------------------------- #

def test_identifier_to_measure_rule_rejected_with_reason():
    """AUDITED DEFECT: an identity rule pairing ``bom_id`` with
    ``duration_minutes`` shipped as 'identity'. A measure endpoint is never a
    join key; the rejection reason must name the measure pairing."""
    valid, reason = onboard._is_valid_identity_rule(
        _rule(source_field="bom_id", target_field="duration_minutes"))
    assert valid is False
    assert "pairs an identifier with a measure" in reason
    assert "duration_minutes" in reason
    # The pilot-env fan-out source was itself a measure — also rejected outright.
    valid2, reason2 = onboard._is_valid_identity_rule(
        _rule(source_field="quantity_at_event", target_field="order_ref"))
    assert valid2 is False and "measure" in reason2


def test_identifier_to_measure_honors_graph_role():
    """A graph-known role wins over name shape: role='measure' rejects even a
    neutral name; role='identifier' lifts the unit-token heuristic (but never
    the shared measure blocklist — join_discovery posture)."""
    valid, reason = onboard._is_valid_identity_rule(
        _rule(target_field="cycle_reading", target_role="measure"))
    assert valid is False and "measure" in reason
    valid2, _ = onboard._is_valid_identity_rule(
        _rule(source_field="duration_minutes", source_role="identifier",
              target_field="order_ref"))
    assert valid2 is True


@pytest.mark.parametrize("field", ["device_state", "conveyor_alarm", "line_oee"])
def test_stream_value_suffix_rejected(field):
    """A stream-name-as-value endpoint (…_state/_alarm/_oee) names a telemetry
    stream, not an entity identity."""
    valid, reason = onboard._is_valid_identity_rule(_rule(target_field=field))
    assert valid is False
    assert "not an identity endpoint" in reason and field in reason


@pytest.mark.parametrize("field", ["DeviceState", "ConveyorAlarm", "LineOEE",
                                   "Line1_DeviceState"])
def test_camelcase_stream_value_suffix_rejected(field):
    """CamelCase compounds must reject exactly like their
    snake_case twins — lowering FIRST erased the word boundary ('devicestate'
    carries no '_state' suffix), so every CamelCase spelling evaded the gate."""
    valid, reason = onboard._is_valid_identity_rule(_rule(target_field=field))
    assert valid is False
    assert "not an identity endpoint" in reason


def test_camelcase_measure_compound_rejected():
    """Same blind spot: 'CycleCount' must measure-check —
    _field_leaf_tokens / join_discovery._norm_tokens split camelCase
    boundaries before lowering, so the 'count' token reaches the blocklist."""
    valid, reason = onboard._is_valid_identity_rule(
        _rule(source_field="CycleCount", target_field="order_ref"))
    assert valid is False and "measure" in reason


def test_equipment_site_grain_rejected():
    """Site⊃equipment containment is not identity: one endpoint at site/plant
    grain, the other at equipment grain."""
    valid, reason = onboard._is_valid_identity_rule(
        _rule(source_field="site_id", target_field="equipment_id"))
    assert valid is False
    assert reason == "containment (site⊃equipment) is not identity"


def test_plain_identifier_rule_still_passes():
    """Regression guard: UUID/wildcard direct-match rules keep flowing — the
    reason the gate was originally opened (a '*' pattern means 'direct match,
    no transform needed')."""
    valid, reason = onboard._is_valid_identity_rule(
        _rule(source_pattern="*", target_pattern="*"))
    assert valid is True and reason == ""
    # Same-grain identifier pairs are untouched by the grain check.
    valid2, _ = onboard._is_valid_identity_rule(
        _rule(source_field="equipment_id", target_field="machine_code"))
    assert valid2 is True


# --------------------------------------------------------------------------- #
# W2 — JoinPaths never derive from rejected rules
# --------------------------------------------------------------------------- #

def test_rejected_rule_produces_no_joinpath(writes):
    """AUDITED DEFECT: _dp_rule_joinpaths re-walked ALL rules including
    rejected ones (the jp-plm-*-downtime family's production path). A rule the
    W3 gate rejects must land in ctx.rules_rejected and produce ZERO JoinPath
    writes — while an accepted sibling still produces its JoinPath."""
    bad = _rule(source_field="bom_id", target_field="duration_minutes")
    good = _rule()
    ctx = _make_ctx([bad, good])
    onboard._dp_identity_rules(ctx)
    assert [e["rule"] for e in ctx.rules_rejected] == [bad]
    onboard._dp_rule_joinpaths(ctx)
    jp = _jp_ids(writes)
    assert jp == [f"jp-source-sys-{onboard._rule_digest_for('source-sys', good)}"]
    assert onboard._rule_digest_for("source-sys", bad) not in {
        j.removeprefix("jp-source-sys-") for j in jp}


# --------------------------------------------------------------------------- #
# W4 — confidence calibration
# --------------------------------------------------------------------------- #

def test_confidence_capped_without_sample_check():
    """LLM-supplied 0.95 with NO passed live sample check stores 0.7."""
    assert onboard._calibrated_rule_confidence(
        {"confidence": 0.95}, sample_check_passed=False, fanout_k=1) == 0.7


def test_confidence_uncapped_with_sample_pass_only_for_a_measured_method():
    """A passed live value-sample transform check lifts the cap only for a
    rule with a measured method (a core match: 0.95 stays). A method-less
    model rule stays capped (2026-10-28): its fit shows one column has the
    pattern's shape, not that a value joins the two columns."""
    assert onboard._calibrated_rule_confidence(
        {"method": "value_core_match", "confidence": 0.95},
        sample_check_passed=True, fanout_k=1) == 0.95
    assert onboard._calibrated_rule_confidence(
        {"confidence": 0.95}, sample_check_passed=True, fanout_k=1) == 0.7


def test_fanout_divides_confidence(writes):
    """AUDITED DEFECT: one source field mapped to several distinct targets in
    the same run (quantity_at_event → 4 targets), each at full confidence. One
    source → 3 targets ⇒ each stored confidence = input/3 (here 0.6/3 = 0.2;
    the non-SQL ctx means no sample check, so the ≤0.7 cap is a no-op at 0.6)."""
    rules = [
        _rule(source_field="lot_code", target_system="target-sys",
              target_field="order_ref", confidence=0.6),
        _rule(source_field="lot_code", target_system="other-sys",
              target_field="batch_ref", confidence=0.6),
        _rule(source_field="lot_code", target_system="third-sys",
              target_field="lot_ref", confidence=0.6),
    ]
    ctx = _make_ctx(rules)
    onboard._dp_identity_rules(ctx)
    confs = [p["conf"] for p in _ir_params(writes)]
    assert confs == [0.2, 0.2, 0.2]
    # Pure-helper form: a method-less rule stays capped even with a passed
    # sample check (2026-10-28), then divided: 0.9 -> 0.7 -> /3.
    assert onboard._calibrated_rule_confidence(
        {"confidence": 0.9}, sample_check_passed=True, fanout_k=3) == 0.2333


def test_single_target_confidence_not_divided(writes):
    """K=1 leaves confidence at the (possibly capped) value: 0.9 → 0.7."""
    ctx = _make_ctx([_rule(confidence=0.9)])
    onboard._dp_identity_rules(ctx)
    assert [p["conf"] for p in _ir_params(writes)] == [0.7]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))


class TestAgentBranchCachePop:
    """Every _dp_agent_* cloud branch must pop the stale in-memory agent entry
    before start_agent — without it a re-provision after an external runtime
    deletion early-returns from the process cache and NO runtime is recreated
    (observed live: sitewise-i3x reonboard left adt_sitewise_i3x ABSENT)."""

    def test_all_agent_branches_pop_stale_cache(self):
        import inspect
        import agents.onboard as ob

        # The Athena and S3 Tables branches pop + start through the shared
        # _start_replacement_agent helper (R9 refactor); the others inline it.
        # Either the branch itself pops before start_agent, or it delegates to
        # a helper that does.
        pop = "_agents.pop(system_id, None)"
        helper_src = inspect.getsource(ob._start_replacement_agent)
        assert pop in helper_src and helper_src.index("_agents.pop") < helper_src.index("start_agent(")
        for fn_name in ("_dp_agent_i3x_native", "_dp_agent_tulip", "_dp_agent_mcp",
                        "_dp_agent_sitewise", "_dp_agent_athena"):
            src = inspect.getsource(getattr(ob, fn_name))
            if "_start_replacement_agent(" in src:
                continue   # pops via the shared helper, asserted above
            assert pop in src, (
                f"{fn_name} must pop the stale in-memory agent entry before "
                f"start_agent (re-provision-after-deletion gap)")
            # the pop must come BEFORE the start_agent call
            assert src.index("_agents.pop") < src.index("start_agent("), fn_name


class TestReplaceNotAccrete:
    """Onboarding-owned ir-/jp- artifacts are REPLACED per provision — an
    id-scheme change must never leave the old generation accreting next to
    the new one (observed live post-W1: index-keyed quadruplicates survived
    alongside their digest replacements)."""

    def test_delete_cyphers_spare_steward_artifacts(self):
        import agents.onboard as ob
        for cy in (ob._ONBOARDING_RULE_DELETE_CYPHER,
                   ob._ONBOARDING_JOINPATH_DELETE_CYPHER):
            assert "user_confirmed" in cy and "STARTS WITH" in cy
        # steward rows are subj-… — the prefix match can never touch them
        assert "'ir-'" in ob._ONBOARDING_RULE_DELETE_CYPHER
        assert "'jp-'" in ob._ONBOARDING_JOINPATH_DELETE_CYPHER

    def test_delete_runs_before_any_rule_writer(self):
        import inspect
        import agents.onboard as ob
        src = inspect.getsource(ob)
        # the call site must sit with the schema replace, before b3/c writers
        call = src.index("_delete_stale_onboarding_rule_artifacts(system_id)")
        b3 = src.index("_dp_internal_joinpaths(ctx)         # b3")
        assert call < b3, "stale-rule delete must precede every jp/ir writer"

    def test_failed_delete_aborts_provision(self, monkeypatch):
        import agents.onboard as ob
        monkeypatch.setattr(ob, "neptune_write", lambda *a, **k: False)
        import pytest as _pytest
        with _pytest.raises(RuntimeError, match="mixed stale\\+fresh"):
            ob._delete_stale_onboarding_rule_artifacts("sys-x")


# --------------------------------------------------------------------------- #
# ADR-0087 — exact-agreement rules through the SAME checkpoint -> c/c2 path
# --------------------------------------------------------------------------- #

_EXACT_REASONING = ("19 exact values of runs.run_code exist in target-sys lines.batch_ref "
                    "(membership-probed); both columns shape AAA-9999-99; concept batch")


def _exact_rule(**over):
    from agents.identity_affinity import EXACT_METHOD, EXACT_TRANSFORM
    return _rule(concept="batch", source_field="runs.run_code", source_table="runs",
                 target_field="lines.batch_ref", target_table="lines", method=EXACT_METHOD,
                 transform=EXACT_TRANSFORM, reasoning=_EXACT_REASONING, shared_count=19, **over)


def _jp_writes(recorded):
    return [(c, p) for c, p in recorded if c.startswith("MERGE (jp:JoinPath")]


def _delete_matches(node: dict, system_id: str) -> bool:
    """The JoinPath replace-not-accrete predicate, evaluated on one node (the
    cypher text is pinned alongside, so the two cannot drift)."""
    cypher = onboard._ONBOARDING_JOINPATH_DELETE_CYPHER
    assert "(jp.from_system = $sid OR jp.to_system = $sid)" in cypher
    assert "jp.id STARTS WITH 'jp-'" in cypher
    assert "coalesce(jp.method, '') <> 'user_confirmed'" in cypher
    return (system_id in (node["from_system"], node["to_system"])
            and node["id"].startswith("jp-") and node["method"] != "user_confirmed")


def test_exact_rule_writes_an_honest_joinpath_and_identity_rule(writes, monkeypatch):
    measured_as_sent(monkeypatch)  # the writer under test, not the re-measurement
    ctx = _make_ctx([_exact_rule()])
    onboard._dp_identity_rules(ctx)
    onboard._dp_rule_joinpaths(ctx)
    ((cypher, params),) = _jp_writes(writes)
    assert params["jpid"].startswith("jp-source-sys-")
    assert (params["method"], params["xform"]) == ("value_exact_match", "identity")
    assert "jp.origin_context = $octx" in cypher and params["octx"] == _EXACT_REASONING
    edges = [(c.split("MERGE (jp)-[:")[1].split("]")[0], p["sid"]) for c, p in writes
             if "MERGE (jp)-[:" in c]
    assert edges == [("FROM_SYSTEM", "source-sys"), ("TO_SYSTEM", "target-sys")]
    (ir,) = _ir_params(writes)
    assert (ir["method"], ir["octx"]) == (
        "value_exact_match", _EXACT_REASONING + "; provision re-measured it as value_exact_match")
    assert (ir["src_pat"], ir["tgt_pat"]) == ("", "")


def test_exact_joinpath_is_replaced_on_every_reprovision(writes):
    """Replace-not-accrete: the exact JoinPath is onboarding-owned, so the
    provision of EITHER endpoint deletes it before rewriting the fresh one."""
    ctx = _make_ctx([_exact_rule()])
    onboard._dp_rule_joinpaths(ctx)
    ((_c, p),) = _jp_writes(writes)
    node = {"id": p["jpid"], "from_system": p["fsys"], "to_system": p["tsys"], "method": p["method"]}
    assert _delete_matches(node, "source-sys") and _delete_matches(node, "target-sys")
    assert not _delete_matches({**node, "method": "user_confirmed"}, "source-sys")


def test_llm_direct_rule_joinpath_says_it_came_from_an_identity_rule(writes):
    onboard._dp_rule_joinpaths(_make_ctx([_rule()]))
    ((cypher, params),) = _jp_writes(writes)
    assert (params["method"], params["xform"]) == ("identity_rule_match", "direct")
    assert "origin_context" not in cypher and "octx" not in params


def test_pattern_rule_joinpath_is_unchanged(writes):
    onboard._dp_rule_joinpaths(_make_ctx([_rule(source_pattern="A-{id}", target_pattern="{id}")]))
    ((_cypher, params),) = _jp_writes(writes)
    assert (params["method"], params["xform"]) == ("value_format_match", "pattern:A-{id}->{id}")


class _TypedCatalog:
    """``{system_id: system_type}`` or ``{system_id: (system_type, plant)}``;
    a bare type sits at plant ``Plant 1``."""
    def __init__(self, types):
        self._types = types

    def get(self, system_id):
        kind = self._types.get(system_id)
        if kind is None:
            return None
        kind, plant = kind if isinstance(kind, tuple) else (kind, "Plant 1")
        return {"id": system_id, "system_type": kind, "plant": plant, "schema": f"s_{system_id}",
                "connection_type": "PostgreSQL"}


def test_same_class_exact_rule_at_different_sites_is_refused(monkeypatch):
    """ADR-0051 P8 / ADR-0052: two systems of the same class at DIFFERENT
    sites (two MES sites) are never joined by the exact-agreement pass; other
    rules pass as before."""
    monkeypatch.setattr(onboard, "catalog", _TypedCatalog(
        {"source-sys": ("MES", "Plant 1"), "target-sys": ("ERP", "Plant 1"),
         "twin-sys": ("MES", "Plant 2")}))
    monkeypatch.setattr(onboard.self_learning, "_gate_cross_site_namespace",
                        lambda *_a: ("", ""))
    cross = _exact_rule()
    twin = _exact_rule(target_system="twin-sys")
    pattern_twin = _rule(target_system="twin-sys", source_pattern="A-{id}", target_pattern="{id}")
    kept = onboard._affinity_cross_site_guard("source-sys", [cross, twin, pattern_twin])
    assert kept == [cross, pattern_twin]
    assert "never joined" in onboard._exact_rule_refusal("source-sys", twin)


def test_same_class_exact_rule_at_the_same_site_is_allowed(monkeypatch):
    """Same class, same site (``site_of``, the P8 site notion): one plant's
    two systems of one class may be joined on exact evidence."""
    monkeypatch.setattr(onboard, "catalog", _TypedCatalog(
        {"source-sys": ("MES", "Plant 1"), "twin-sys": ("mes", "Plant 1")}))
    rule = _exact_rule(target_system="twin-sys")
    assert onboard._exact_rule_refusal("source-sys", rule) is None
    assert onboard._affinity_cross_site_guard("source-sys", [rule]) == [rule]


def test_same_class_exact_rule_with_an_unsited_side_fails_closed(monkeypatch):
    """A missing or wildcard site is not proof of the SAME site: refused."""
    monkeypatch.setattr(onboard, "catalog", _TypedCatalog(
        {"source-sys": ("MES", "Plant 1"), "all-sys": ("MES", "All Sites"), "none-sys": ("MES", "")}))
    for target in ("all-sys", "none-sys"):
        assert onboard._exact_rule_refusal("source-sys", _exact_rule(target_system=target)) == \
            "site unknown"


@pytest.mark.parametrize("kind", ["", "Custom", " custom ", None])
def test_unknown_class_exact_rule_is_refused(monkeypatch, kind):
    """An empty class, a class-less (absent) system, or the ConnectRequest
    default ``Custom`` is UNKNOWN: fail closed, whatever the other side is."""
    from agents.models import ConnectRequest
    assert ConnectRequest(name="x").system_type == "Custom"
    types = {"source-sys": "MES"}
    if kind is not None:
        types["odd-sys"] = kind
    monkeypatch.setattr(onboard, "catalog", _TypedCatalog(types))
    rule = _exact_rule(target_system="odd-sys")
    assert onboard._exact_rule_refusal("source-sys", rule) == "system class unknown"
    assert onboard._exact_rule_refusal("odd-sys", _exact_rule(target_system="source-sys")) == \
        "system class unknown"
    assert onboard._affinity_cross_site_guard("source-sys", [rule]) == []


def test_refused_exact_rule_logs_its_reason(monkeypatch, caplog):
    import logging
    monkeypatch.setattr(onboard, "catalog", _TypedCatalog({"source-sys": "MES", "odd-sys": "Custom"}))
    with caplog.at_level(logging.INFO, logger=onboard.logger.name):
        onboard._affinity_cross_site_guard("source-sys", [_exact_rule(target_system="odd-sys")])
    assert "refused: system class unknown" in caplog.text


def test_same_class_refusal_names_the_p8_reason_when_it_fires(monkeypatch):
    monkeypatch.setattr(onboard, "catalog", _TypedCatalog({"source-sys": ("MES", "Plant 1"),
                                                           "twin-sys": ("MES", "Plant 2")}))
    seen = []
    monkeypatch.setattr(onboard.self_learning, "_gate_cross_site_namespace",
                        lambda *a: seen.append(a) or ("drop", "site-local namespaces collide"))
    assert onboard._exact_rule_refusal("source-sys", _exact_rule(target_system="twin-sys")) == \
        "site-local namespaces collide"
    assert seen == [("identity_rule", "source-sys", "twin-sys", "batch", "machine")]


def test_resolve_injects_the_probe_and_guards_exact_rules(monkeypatch):
    import agents.identity_affinity as aff
    monkeypatch.setattr(onboard, "catalog", _TypedCatalog({"source-sys": "MES", "target-sys": "ERP",
                                                           "twin-sys": ("MES", "Plant 2")}))
    monkeypatch.setattr(onboard.self_learning, "_gate_cross_site_namespace", lambda *_a: ("", ""))
    monkeypatch.setattr(onboard, "_affinity_new_fields", lambda *_a: ["new"])
    monkeypatch.setattr(onboard, "_affinity_existing_fields", lambda *_a: ["old"])
    seen = {}

    def find(new, existing, probe=None, stats=None):
        seen["probe"] = probe
        return [_exact_rule(), _exact_rule(target_system="twin-sys")]
    monkeypatch.setattr(aff, "find_affinity_rules", find)
    body = SimpleNamespace(system_id="source-sys", mappings=[])
    out = onboard._augment_with_affinity_rules(body, {"rules": []})
    assert callable(seen["probe"])
    assert [r["target_system"] for r in out["rules"]] == ["target-sys"]


def test_affinity_probe_asks_the_fields_own_system(monkeypatch):
    import agents.resolve_evidence as resolve_evidence
    from agents.identity_affinity import SampledField
    monkeypatch.setattr(onboard, "catalog", _TypedCatalog({"target-sys": "ERP"}))
    calls = []
    monkeypatch.setattr(resolve_evidence, "probe_field_values",
                        lambda **kw: calls.append(kw) or ["A-1"])
    probe = onboard._affinity_probe()
    field = SampledField("target-sys", "lines", "batch_ref", "batch", ("A-1",))
    assert probe(field, ("A-1", "A-2")) == ["A-1"]
    assert calls == [{"system_id": "target-sys", "connection_type": "PostgreSQL",
                      "schema": "s_target-sys", "table": "lines", "field": "batch_ref",
                      "values": ("A-1", "A-2")}]


def test_affinity_probe_falls_back_to_the_fields_graph_schema(monkeypatch):
    """Like ``_affinity_existing_fields``: a system without a catalog schema is
    probed in the schema its field was sampled from (the graph ``s.schema``)."""
    import agents.resolve_evidence as resolve_evidence
    from agents.identity_affinity import SampledField
    monkeypatch.setattr(onboard, "catalog", _TypedCatalog({}))
    calls = []
    monkeypatch.setattr(resolve_evidence, "probe_field_values",
                        lambda **kw: calls.append(kw) or [])
    field = SampledField("graph-sys", "lines", "batch_ref", "batch", ("A-1",), schema="g_lines")
    assert onboard._affinity_probe()(field, ("A-1",)) == []
    assert (calls[0]["schema"], calls[0]["connection_type"]) == ("g_lines", "PostgreSQL")


def test_existing_fields_carry_the_schema_they_were_sampled_from(monkeypatch):
    import agents.resolve_evidence as resolve_evidence
    monkeypatch.setattr(onboard, "catalog", SimpleNamespace(
        get=lambda _sid: None, get_onboarded_ids=lambda: {"graph-sys"}))
    monkeypatch.setattr(onboard, "neptune_query", lambda *_a, **_k: [
        {"sys_id": "graph-sys", "sys_schema": "g_lines", "table_name": "lines",
         "field_name": "batch_ref", "concept": "batch"}])
    monkeypatch.setattr(resolve_evidence, "sample_field_values", lambda **kw: ["A-1"])
    (field,) = onboard._affinity_existing_fields("source-sys")
    assert (field.schema, field.values) == ("g_lines", ("A-1",))


def test_exact_rule_replacing_an_llm_rule_is_logged(monkeypatch, caplog):
    import logging
    import agents.identity_affinity as aff
    monkeypatch.setattr(onboard, "catalog", _TypedCatalog({"source-sys": "MES", "target-sys": "ERP"}))
    monkeypatch.setattr(onboard, "_affinity_new_fields", lambda *_a: [])
    monkeypatch.setattr(onboard, "_affinity_existing_fields", lambda *_a: [])
    exact = _exact_rule()
    monkeypatch.setattr(aff, "find_affinity_rules", lambda *_a, **_k: [exact])
    llm = _rule(concept="batch", source_field="runs.run_code", source_table="runs",
                target_field="lines.batch_ref", target_table="lines")
    body = SimpleNamespace(system_id="source-sys", mappings=[])
    with caplog.at_level(logging.INFO, logger=onboard.logger.name):
        out = onboard._augment_with_affinity_rules(body, {"rules": [llm]})
    assert out["rules"] == [exact]
    assert "replaced 1 LLM rule(s) with exact rule(s)" in caplog.text


def test_no_replacement_log_when_the_llm_pattern_rule_wins(monkeypatch, caplog):
    import logging
    import agents.identity_affinity as aff
    monkeypatch.setattr(onboard, "catalog", _TypedCatalog({"source-sys": "MES", "target-sys": "ERP"}))
    monkeypatch.setattr(onboard, "_affinity_new_fields", lambda *_a: [])
    monkeypatch.setattr(onboard, "_affinity_existing_fields", lambda *_a: [])
    monkeypatch.setattr(aff, "find_affinity_rules", lambda *_a, **_k: [_exact_rule()])
    llm = _rule(concept="batch", source_field="runs.run_code", source_table="runs",
                target_field="lines.batch_ref", target_table="lines",
                source_pattern="A-{id}", target_pattern="{id}")
    body = SimpleNamespace(system_id="source-sys", mappings=[])
    with caplog.at_level(logging.INFO, logger=onboard.logger.name):
        out = onboard._augment_with_affinity_rules(body, {"rules": [llm]})
    assert out["rules"] == [llm]
    assert "exact rule(s)" not in caplog.text


# --------------------------------------------------------------------------- #
# An exact-agreement rule keeps its probe-derived confidence
# --------------------------------------------------------------------------- #

def test_exact_rule_confidence_is_not_capped_or_divided():
    """AUDITED DEFECT (frozen-one-clock-2 5-ir-all.json): ir-plm-59713594…
    value_exact_match stored 0.35 = min(0.85, 0.7) / 2 — the LLM-guess
    calibration applied to a membership-probed rule whose source field also
    feeds a second target. The probe IS a live value check, so no cap, and
    each target was verified pair by pair, so no fan-out division."""
    from agents.identity_affinity import AFFINITY_CONFIDENCE, EXACT_METHOD
    rule = {"method": EXACT_METHOD, "confidence": AFFINITY_CONFIDENCE}
    assert onboard._calibrated_rule_confidence(
        rule, sample_check_passed=False, fanout_k=2) == AFFINITY_CONFIDENCE == 0.85
    assert onboard._calibrated_rule_confidence(
        rule, sample_check_passed=False, fanout_k=1) == 0.85


def test_llm_and_core_rule_calibration_unchanged():
    """The LLM rule keeps the 0.7 cap and /K; value_core_match keeps its /K
    exemption (the cap still applies without a sample check)."""
    assert onboard._calibrated_rule_confidence(
        {"confidence": 0.85}, sample_check_passed=False, fanout_k=2) == 0.35
    assert onboard._calibrated_rule_confidence(
        {"method": "value_core_match", "confidence": 0.85},
        sample_check_passed=False, fanout_k=2) == 0.7
    assert onboard._calibrated_rule_confidence(
        {"method": "value_core_match", "confidence": 0.85},
        sample_check_passed=True, fanout_k=2) == 0.85


def test_exact_rule_pattern_check_never_reports_a_sample_pass(writes):
    """Second cause, pinned: an exact rule has EMPTY patterns, so the live
    pattern-sample check is skipped even on a PostgreSQL source and
    ``sample_check_passed`` is False — calibration must not depend on it."""
    ctx = _make_ctx([])
    ctx.connection_type, ctx.schema = "PostgreSQL", "src_schema"
    assert onboard._dp_rule_pattern_check(ctx, _exact_rule()) == (None, "", False)


def test_exact_rule_writer_stores_probe_confidence_under_fanout(writes, monkeypatch):
    """Writer-level: the exact rule shares its source field with an LLM rule
    to another target (K=2). The exact rule is written at 0.85; the LLM
    rule is still 0.9 → 0.7 → /2 = 0.35."""
    from agents.identity_affinity import AFFINITY_CONFIDENCE
    measured_as_sent(monkeypatch)  # the writer under test, not the re-measurement
    exact = _exact_rule(confidence=AFFINITY_CONFIDENCE)
    llm = _rule(concept="batch", source_field="runs.run_code", source_table="runs",
                target_system="other-sys", target_field="batch_ref")
    ctx = _make_ctx([exact, llm])
    onboard._dp_identity_rules(ctx)
    confs = {p["tgt_sys"]: p["conf"] for p in _ir_params(writes)}
    assert confs == {"target-sys": 0.85, "other-sys": 0.35}


def test_identity_shaped_llm_rule_is_replaced_and_written_at_probe_confidence(
        writes, monkeypatch, caplog):
    """2026-09-30 (frozen-12201093 step 4): the LLM proposed ``AB-*``/``AB-*``
    — identity-shaped, non-empty — so the exact rule used to be dropped and
    the LLM rule was written at 0.35. Now the exact rule replaces it (the log
    line names the rule and why) and the writer stores 0.85 under fan-out."""
    import logging
    import agents.identity_affinity as aff
    from agents.identity_affinity import AFFINITY_CONFIDENCE
    monkeypatch.setattr(onboard, "_affinity_new_fields", lambda *_a: [])
    monkeypatch.setattr(onboard, "_affinity_existing_fields", lambda *_a: [])
    monkeypatch.setattr(onboard, "_affinity_cross_site_guard", lambda _s, found: found)
    exact = _exact_rule(confidence=AFFINITY_CONFIDENCE)
    monkeypatch.setattr(aff, "find_affinity_rules", lambda *_a, **_k: [exact])
    same_pair = _rule(concept="batch", source_field="runs.run_code", source_table="runs",
                      target_field="lines.batch_ref", target_table="lines",
                      source_pattern="AB-*", target_pattern="AB-*", confidence=0.7)
    other = _rule(concept="batch", source_field="runs.run_code", source_table="runs",
                  target_system="other-sys", target_field="batch_ref")
    body = SimpleNamespace(system_id="source-sys", mappings=[])
    with caplog.at_level(logging.INFO, logger=onboard.logger.name):
        out = onboard._augment_with_affinity_rules(body, {"rules": [same_pair, other]})
    assert out["rules"] == [exact, other]
    assert ("replaced 1 LLM rule(s) with exact rule(s): runs.run_code -> "
            "target-sys.lines.batch_ref (LLM patterns 'AB-*'->'AB-*' identity-shaped)") in caplog.text
    measured_as_sent(monkeypatch)  # the writer under test, not the re-measurement
    ctx = _make_ctx(out["rules"])
    onboard._dp_identity_rules(ctx)
    params = {p["tgt_sys"]: p for p in _ir_params(writes)}
    assert (params["target-sys"]["conf"], params["target-sys"]["method"]) == (0.85, "value_exact_match")
    assert params["target-sys"]["src_pat"] == params["target-sys"]["tgt_pat"] == ""
    assert params["other-sys"]["conf"] == 0.35
