# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""R2 — the repo-wide engine domain-neutrality RATCHET gate (ADR-0044).

T1.2's gate scans only ``CORE_CONCEPT_DEFINITIONS`` values; a repo-wide
measurement (2026-07-29) found **128 demo-literal hits across 14 engine files**
outside ``agents/demo/`` — the map prompt was still domain-contaminated, just
not via the vocabulary. ADR-0044 forbids demo literals in engine code outside
``agents/demo/``. Fixing all of them in one commit would be unreviewable and
would change prompt behaviour in ways only lane M can validate, so this gate
does two things instead:

* **stop the bleeding everywhere** — any NEW demo literal in any engine file
  fails immediately;
* **ratchet the debt down** — ``KNOWN_CONTAMINATION`` is an enumerated
  baseline of the still-dirty files with their EXACT remaining hit counts,
  asserted in BOTH directions: a count above baseline fails (new
  contamination), and a count below baseline ALSO fails until the baseline is
  tightened (progress must be recorded, so the number can only go down).

``KNOWN_CONTAMINATION`` is **tracked debt with a burn-down order** — do not
"fix" a failure here by raising a number; fix the file or (after a genuine
cleanup) lower/remove its entry. The intended tranche sequence, by measured
harm (prompt text > functional logic > mechanical content move > comments):

1. **DONE (this commit)** — ``onboard_agent.py`` live map/resolve prompt text
   (biased mapping for EVERY customer); 13 -> 1 (the one left is a docstring
   example inside the grandfathered ``connect_new_system`` — see baseline).
2. ``onboard.py`` (6) + ``value_promotion.py`` (2) — prompt text and
   functional pattern/prefix logic in the mapping/resolution pipeline
   (``resolve.py`` reached zero on 2026-09-18 when the entity-picker
   demo-schema fallback was removed).
3. **DONE (2026-09-27)** — ``admin_routes.py`` (71 -> 0): the demo MES
   schema-view DDL block (``mes_milwaukee``/``mes_detroit``/
   ``mes_cedarrapids``). A mechanical content move into ``agents/demo/``
   (ADR-0044's sanctioned home), no behaviour change.
4. ``runtime_app.py`` (3) + ``concept_tagging.py`` (3) + ``fleet.py`` (3) +
   ``branding.py`` (4) — demo schema/name routing tables and display names;
   move behind the ``agents/demo/db_routing.py``-style seam.
5. ``system_normalize.py`` (8) + ``self_learning.py`` (1) +
   ``response_integrity_annotate.py`` (2) + ``system_agent.py`` (1) +
   ``determinism.py`` (2) — docstring/comment examples and two denylist
   FALSE POSITIVES (see baseline comments); lowest harm, rewrite or annotate
   last.

Offline, pure — no AWS, no graph. Scans source text at collection time.

**Scope widening (R3, 2026-08-05).** The scan is no longer limited to
``agents/**/*.py``: a customer literal shipped in ``scripts/`` with every lane
green because the ratchet never looked there. The scan now ALSO covers
``scripts/**/*.py`` (repo root and app), ``agentic_digital_thread/tests/**/*.py``
and ``agentic_digital_thread/frontend/src/**/*.{ts,tsx,js}``. Exempt by
definition: ``agents/demo/`` (ADR-0044's sanctioned demo home) and the three
DETECTOR files whose pinned denylists necessarily NAME the forbidden terms
(this file, ``test_core_concept_neutrality.py`` and
``test_interview_questions.py``).
Baseline keys are now REPO-ROOT-relative posix paths.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

# Roots (tests run with PYTHONPATH=agentic_digital_thread).
APP_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = APP_ROOT.parent
AGENTS_ROOT = APP_ROOT / "agents"

_THIS_FILE = Path(__file__).resolve()
# Detector files: their pinned denylists must NAME the forbidden terms to
# detect them — the ONE sanctioned code home for those terms. Never
# "neutralize" a detector; that deletes the gate, not the debt.
_DETECTOR_FILES = frozenset({
    _THIS_FILE,
    _THIS_FILE.parent / "test_core_concept_neutrality.py",
    _THIS_FILE.parent / "test_interview_questions.py",
})

# (root, glob patterns) pairs the ratchet scans. agents/demo/ is exempted in
# _engine_files(); demo golden JSON is out of scope by extension.
_SCAN_ROOTS: tuple[tuple[Path, tuple[str, ...]], ...] = (
    (AGENTS_ROOT, ("*.py",)),
    (REPO_ROOT / "scripts", ("*.py",)),
    (APP_ROOT / "scripts", ("*.py",)),
    (APP_ROOT / "tests", ("*.py",)),
    (APP_ROOT / "frontend" / "src", ("*.ts", "*.tsx", "*.js")),
)

# PINNED denylist classes (do not weaken; extend only). Same families as the
# T1.2 core-concept gate, widened to the repo-wide measurement set.
DENYLIST: dict[str, str] = {
    "site-codes": r"\bMKE\b|\bDET\b|\bCRR\b",
    "city-names": r"(?i)milwaukee|detroit|cedar\s*rapids|indianapolis",
    "id-prefixes": r"MKE-|DET-|CRR-|WO-MKE|LOT-DET",
    "demo-schemas": r"\bmes_\w+\.\w+|MILWAUKEE_BOTTLING|DETROIT_AUTOPARTS|CEDARRAPIDS_FOOD",
    "demo-lines": r"Bottling-L\d|AutoParts-L\d|FoodProc-L\d|AnyCompany",
    "equip-classes": r"(?i)\b(washer|filler|capper|labeler|checkweigher|case_packer|palletizer)\b",
    # Customer-identity family (target baseline: ZERO). The real names are
    # not published with the sample; this placeholder keeps the gates that
    # compose the customer-* classes (test_customer_golden_boundary.py) wired.
    "customer-names": r"(?i)\bexample-customer-name\b",
}

# ---------------------------------------------------------------------------
# THE RATCHET BASELINE — file (posix path relative to the REPO ROOT) -> exact
# remaining hit count. Re-measured 2026-08-05: the R3 scope widening jumped
# the baseline (agents-only 113/12 files -> widened 1144/159 files), then
# the same-day customer-identity burn-down lowered it to 645/97 files —
# every customer-* class hit is GONE (their target is ZERO: any new one
# fails immediately as an unlisted-file/over-baseline hit). Entries may
# only DECREASE or DISAPPEAR; a file reaching 0 must be REMOVED (no zero
# values). The trailing comment names the hit classes; the remaining
# demo-class hits in tests are largely fixture couplings to agents/demo/
# data.
# NOTE: the demo-schemas needle stays anchored (``\bmes_``) because the
# unanchored form classified ordinary identifiers — ``names_after.get``
# matched via its 'mes_after.get' substring. Same principle as the pinned
# error-taxonomy needles (word boundaries, never a bare substring): a gate
# whose floor contains phantom hits makes the burn-down list untrustworthy.
# ---------------------------------------------------------------------------
KNOWN_CONTAMINATION: dict[str, int] = {
    # admin_routes.py reached ZERO on 2026-09-27 (demo MES schema-view DDL
    # moved to agents/demo/site_views.py) — entry deleted; any hit there fails as unlisted.
    # Demo plant display-name table (functional demo routing).
    "agentic_digital_thread/agents/branding.py": 4,  # city-names
    # Demo system/schema rows in a docstring example table (comments).
    "agentic_digital_thread/agents/concept_tagging.py": 3,  # city-names
    # Demo agent display names (functional demo routing).
    "agentic_digital_thread/agents/fleet.py": 3,  # city-names
    # Demo id-prefix examples in identity-pattern prompt text (prompt text).
    "agentic_digital_thread/agents/onboard.py": 6,  # id-prefixes,site-codes
    # onboard_agent.py reached ZERO on 2026-10-08 (the connect_new_system
    # "milwaukee-mes" docstring example moved out with the register_system
    # split) — entry deleted; any hit there fails as unlisted.
    # resolve.py reached ZERO on 2026-09-18 (entity-picker demo-schema fallback
    # in thread_entities removed) — entry deleted; any hit there fails as unlisted.
    # FALSE-POSITIVE-ADJACENT: 'MKE-FILL-001' in an identifier-shape comment (comments).
    "agentic_digital_thread/agents/response_integrity_annotate.py": 2,  # id-prefixes,site-codes
    # Demo schema fallback routing (functional logic; agents/demo/db_routing.py seam per ADR-0044).
    "agentic_digital_thread/agents/runtime_app.py": 3,  # city-names
    # Docstring example (comments).
    "agentic_digital_thread/agents/self_learning.py": 1,  # city-names
    # Docstring/comment name-normalization examples (comments).
    "agentic_digital_thread/agents/system_normalize.py": 8,  # city-names
    # Docstring value-pair example 'LINE1 -> DET-L1' (comments).
    "agentic_digital_thread/agents/value_promotion.py": 2,  # id-prefixes,site-codes
    "agentic_digital_thread/frontend/src/chat/charts.ts": 1,  # city-names
    # chat/tool-labels.ts and lib/arch-store.ts reached ZERO on 2026-10-09
    # (their demo-plant label tables gave way to the catalog-backed
    # lib/system-names.ts resolver) — entries deleted; any hit there fails
    # as unlisted.
    "agentic_digital_thread/frontend/src/pages/admin/tabs/SettingsTab.tsx": 1,  # demo-lines
    "agentic_digital_thread/frontend/src/test/admin-logic.test.ts": 3,  # demo-lines
    "agentic_digital_thread/frontend/src/test/admin-queries.test.tsx": 3,  # demo-lines
    "agentic_digital_thread/frontend/src/test/analysis-panel.test.tsx": 3,  # city-names
    "agentic_digital_thread/frontend/src/test/answer-sections.test.tsx": 3,  # city-names
    "agentic_digital_thread/frontend/src/test/charts.test.ts": 2,  # city-names
    "agentic_digital_thread/frontend/src/test/chat-interface.test.ts": 1,  # city-names
    "agentic_digital_thread/frontend/src/test/chat-thread.test.tsx": 2,  # equip-classes
    "agentic_digital_thread/frontend/src/test/dashboard-lib.test.ts": 3,  # city-names
    "agentic_digital_thread/frontend/src/test/inv-graph-layout.test.ts": 1,  # city-names
    "agentic_digital_thread/frontend/src/test/kg-focus-node.test.ts": 2,  # demo-lines
    "agentic_digital_thread/frontend/src/test/kg-label.test.ts": 1,  # city-names
    "agentic_digital_thread/frontend/src/test/line-state-drill.test.tsx": 7,  # city-names
    "agentic_digital_thread/frontend/src/test/md-component.test.tsx": 9,  # city-names,equip-classes
    "agentic_digital_thread/frontend/src/test/provenance.test.ts": 2,  # city-names
    "agentic_digital_thread/frontend/src/test/settings-feedback-exploration.test.tsx": 1,  # demo-lines
    "agentic_digital_thread/frontend/src/test/system-map-first-load.test.ts": 1,  # city-names
    "agentic_digital_thread/frontend/src/test/thread-store.test.ts": 2,  # equip-classes
    "agentic_digital_thread/frontend/src/views/architecture/inv-graph.ts": 1,  # city-names
    "agentic_digital_thread/frontend/src/views/dashboard/lib.ts": 3,  # city-names
    "agentic_digital_thread/tests/integration/test_resolver_integration.py": 4,  # city-names,equip-classes,id-prefixes,site-codes
    "agentic_digital_thread/tests/integration/test_two_system_identity.py": 2,  # id-prefixes,site-codes
    "agentic_digital_thread/tests/test_memory_client.py": 4,  # city-names
    "agentic_digital_thread/tests/unit/test_accept_all_pending_skips_insight.py": 2,  # id-prefixes,site-codes
    "agentic_digital_thread/tests/unit/test_active_issues.py": 12,  # city-names
    "agentic_digital_thread/tests/unit/test_agent_analysis_panel.py": 4,  # city-names,equip-classes
    "agentic_digital_thread/tests/unit/test_business_context.py": 1,  # equip-classes
    "agentic_digital_thread/tests/unit/test_concept_authority_mes_ot.py": 2,  # demo-schemas
    "agentic_digital_thread/tests/unit/test_connection_resolver.py": 1,  # city-names
    "agentic_digital_thread/tests/unit/test_connection_type_guidance.py": 2,  # city-names
    "agentic_digital_thread/tests/unit/test_cross_site_proposal_guard.py": 6,  # city-names
    "agentic_digital_thread/tests/unit/test_cross_site_vouching.py": 4,  # city-names
    "agentic_digital_thread/tests/unit/test_dashboard_cards.py": 12,  # city-names,demo-schemas
    "agentic_digital_thread/tests/unit/test_dashboard_drill_machines.py": 8,  # city-names
    "agentic_digital_thread/tests/unit/test_dashboard_line_state.py": 8,  # city-names
    "agentic_digital_thread/tests/unit/test_db_conn_params_records_first.py": 3,  # city-names
    "agentic_digital_thread/tests/unit/test_demo_db_routing.py": 4,  # city-names
    "agentic_digital_thread/tests/unit/test_demo_schema_routing_single_source.py": 3,  # city-names
    "agentic_digital_thread/tests/unit/test_demo_systems_manifest.py": 9,  # city-names
    "agentic_digital_thread/tests/unit/test_digital_thread_server.py": 4,  # id-prefixes,site-codes
    "agentic_digital_thread/tests/unit/test_entity_render.py": 3,  # city-names
    "agentic_digital_thread/tests/unit/test_equipment_value_mappings.py": 2,  # city-names
    "agentic_digital_thread/tests/unit/test_heatmap_resolver.py": 15,  # city-names,demo-schemas
    "agentic_digital_thread/tests/unit/test_intra_system_discovery.py": 30,  # id-prefixes,site-codes
    "agentic_digital_thread/tests/unit/test_known_systems_source.py": 2,  # city-names
    "agentic_digital_thread/tests/unit/test_line_state_ttl_cache.py": 1,  # city-names
    "agentic_digital_thread/tests/unit/test_map_prompt_shape.py": 4,  # city-names,demo-lines
    "agentic_digital_thread/tests/unit/test_map_reconciliation.py": 3,  # city-names,demo-lines
    "agentic_digital_thread/tests/unit/test_map_telemetry_rich_i3x.py": 3,  # city-names,demo-lines,equip-classes
    "agentic_digital_thread/tests/unit/test_mes_cmms_bridge.py": 52,  # city-names,demo-schemas,id-prefixes,site-codes
    "agentic_digital_thread/tests/unit/test_metamodel.py": 7,  # city-names,demo-lines,id-prefixes,site-codes
    "agentic_digital_thread/tests/unit/test_metric_multisite.py": 2,  # city-names
    "agentic_digital_thread/tests/unit/test_metric_resolver.py": 50,  # city-names,demo-schemas
    "agentic_digital_thread/tests/unit/test_onboard_connect.py": 9,  # city-names
    "agentic_digital_thread/tests/unit/test_onboard_discover_connection_source.py": 2,  # city-names,demo-lines
    "agentic_digital_thread/tests/unit/test_onboarding_smoketest_connection_input.py": 4,  # city-names
    "agentic_digital_thread/tests/unit/test_onboarding_smoketest_guard.py": 1,  # city-names
    "agentic_digital_thread/tests/unit/test_orchestrator_i3x_prompt_rules.py": 3,  # city-names,demo-lines,equip-classes
    "agentic_digital_thread/tests/unit/test_orchestrator_prompt_guard.py": 17,  # city-names,site-codes
    "agentic_digital_thread/tests/unit/test_prompts_demo_gate.py": 4,  # city-names,equip-classes
    "agentic_digital_thread/tests/unit/test_proposal_scoring.py": 4,  # id-prefixes,site-codes
    "agentic_digital_thread/tests/unit/test_provision_entity_edge.py": 1,  # city-names
    "agentic_digital_thread/tests/unit/test_provision_intra_system_capture.py": 14,  # city-names,demo-schemas,site-codes
    "agentic_digital_thread/tests/unit/test_reconcile_query_gateway.py": 2,  # city-names
    "agentic_digital_thread/tests/unit/test_register_demo_systems.py": 1,  # city-names
    "agentic_digital_thread/tests/unit/test_resolve_by_dimension_route.py": 1,  # city-names
    "agentic_digital_thread/tests/unit/test_runtime_insight_routing.py": 12,  # city-names
    "agentic_digital_thread/tests/unit/test_seed_defect_chain.py": 6,  # city-names
    "agentic_digital_thread/tests/unit/test_self_learning_system_normalization.py": 4,  # city-names
    "agentic_digital_thread/tests/unit/test_sitewise_batch_tool.py": 1,  # equip-classes
    "agentic_digital_thread/tests/unit/test_sitewise_schema_seed.py": 6,  # equip-classes
    "agentic_digital_thread/tests/unit/test_subject_churn_quarantine.py": 2,  # id-prefixes,site-codes
    "agentic_digital_thread/tests/unit/test_system_agent_concept_mappings.py": 2,  # equip-classes
    "agentic_digital_thread/tests/unit/test_system_agent_identifier_miss.py": 7,  # city-names,site-codes
    "agentic_digital_thread/tests/unit/test_system_agent_schema_prompt.py": 2,  # city-names
    "agentic_digital_thread/tests/unit/test_system_normalize.py": 23,  # city-names
    "agentic_digital_thread/tests/unit/test_value_promotion.py": 46,  # id-prefixes,site-codes
    "agentic_digital_thread/tests/unit/test_vocab_prompt_block.py": 2,  # city-names,demo-lines
    "scripts/graph_parity_check.py": 3,  # city-names
    "scripts/seed_local_db.py": 5,  # city-names
}


def _engine_files() -> list[Path]:
    """Every file under the scan roots EXCEPT agents/demo/ (ADR-0044's
    sanctioned demo home is exempt by definition) and the detector files
    (their denylists must name the terms they detect)."""
    files: set[Path] = set()
    for root, patterns in _SCAN_ROOTS:
        if not root.is_dir():
            continue
        for pattern in patterns:
            for p in root.rglob(pattern):
                if p.resolve() in _DETECTOR_FILES:
                    continue
                if root == AGENTS_ROOT and \
                        "demo" in p.relative_to(AGENTS_ROOT).parts[:-1]:
                    continue
                files.add(p)
    return sorted(files)


def _scan_file(path: Path) -> tuple[int, list[tuple[str, int, int]]]:
    """Total denylist hits in one file, plus (class, line_no, hits) detail.

    Counting semantics (pinned): the number of regex MATCHES summed over all
    denylist classes and lines — one line can contribute several hits and can
    hit several classes.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    total, detail = 0, []
    for cls, pattern in DENYLIST.items():
        rx = re.compile(pattern)
        for line_no, line in enumerate(lines, 1):
            n = len(rx.findall(line))
            if n:
                total += n
                detail.append((cls, line_no, n))
    return total, detail


def _render(rel: str, detail: list[tuple[str, int, int]]) -> str:
    """Readable per-file failure listing: class, count, offending line numbers."""
    by_class: dict[str, list[tuple[int, int]]] = {}
    for cls, line_no, n in detail:
        by_class.setdefault(cls, []).append((line_no, n))
    parts = [f"{rel}:"]
    for cls in sorted(by_class):
        rows = sorted(by_class[cls])
        count = sum(n for _, n in rows)
        lines_txt = ", ".join(f"L{ln}" + (f"(x{n})" if n > 1 else "")
                              for ln, n in rows)
        parts.append(f"  [{cls}] {count} hit(s) at {lines_txt}")
    return "\n".join(parts)


def _measure() -> tuple[dict[str, int], dict[str, list[tuple[str, int, int]]]]:
    counts: dict[str, int] = {}
    details: dict[str, list[tuple[str, int, int]]] = {}
    for path in _engine_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        total, detail = _scan_file(path)
        if total:
            counts[rel] = total
            details[rel] = detail
    return counts, details


def test_baseline_has_no_zero_or_negative_entries():
    """A baseline file that reaches 0 must be REMOVED from the dict — a zero
    entry would let contamination silently return up to 'baseline'."""
    bad = {f: n for f, n in KNOWN_CONTAMINATION.items() if n <= 0}
    assert not bad, (
        f"KNOWN_CONTAMINATION entries must be positive; remove cleaned files "
        f"outright: {sorted(bad)}"
    )


def test_engine_domain_neutrality_ratchet():
    """BOTH directions fail: new contamination (actual > baseline, or any hit
    in an unlisted file) AND unrecorded improvement (actual < baseline) — the
    baseline can only shrink, and only by editing it to match reality."""
    counts, details = _measure()
    problems: list[str] = []

    for rel in sorted(set(counts) - set(KNOWN_CONTAMINATION)):
        problems.append(
            f"NEW demo/customer contamination added to {rel} (not in "
            f"KNOWN_CONTAMINATION — scanned files must stay clean; demo "
            f"literals belong in agents/demo/, ADR-0044; customer identity "
            f"belongs NOWHERE):\n"
            + _render(rel, details[rel])
        )
    for rel in sorted(set(counts) & set(KNOWN_CONTAMINATION)):
        actual, baseline = counts[rel], KNOWN_CONTAMINATION[rel]
        if actual > baseline:
            problems.append(
                f"new demo/customer contamination added to {rel}: {actual} "
                f"hits > baseline {baseline}. Remove the new literal(s) — the "
                f"baseline is a ratchet, never raise it:\n"
                + _render(rel, details[rel])
            )
        elif actual < baseline:
            problems.append(
                f"{rel} improved to {actual} (baseline {baseline}) — "
                f"tighten KNOWN_CONTAMINATION to {actual} so the ratchet "
                f"holds the gain (remove the entry if 0)."
            )
    for rel in sorted(set(KNOWN_CONTAMINATION) - set(counts)):
        problems.append(
            f"{rel} improved to 0 (baseline "
            f"{KNOWN_CONTAMINATION[rel]}) or no longer exists — remove its "
            f"KNOWN_CONTAMINATION entry so the ratchet holds the gain."
        )

    assert not problems, (
        "engine domain-neutrality ratchet violated:\n\n"
        + "\n\n".join(problems)
    )


# ---------------------------------------------------------------------------
# The R2 prompt-text tranche: onboard_agent.py's map/resolve prompt text is
# CLEAN, and the concrete demo instances moved out of it reach a composed map
# prompt through the T1.3 overlay path (demonstrated, not narrated).
# ---------------------------------------------------------------------------
def _denylist_hits(text: str) -> list[str]:
    return [f"{cls}: {m.group(0)!r}"
            for cls, pattern in DENYLIST.items()
            for m in re.finditer(pattern, text)]


def test_mapfields_prompt_is_domain_neutral_both_branches():
    """The live map prompt (SQL and MCP branches) carries NONE of the six
    denylist classes — the neutral shape guidance replaced the demo instances."""
    import agents.onboard_agent as onboard_agent
    for is_mcp in (False, True):
        prompt = onboard_agent._build_mapfields_prompt(
            "sysid", "concept-a, concept-b", "  concept-a: neutral hint",
            "table_a: col_a, col_b", is_mcp=is_mcp)
        hits = _denylist_hits(prompt)
        assert not hits, f"is_mcp={is_mcp} map prompt contaminated: {hits}"


def test_mapfields_prompt_keeps_structural_instructions():
    """Every structural mapping instruction survived the neutral rewrite —
    only the domain instances changed (role force, concept force, MCP
    grouping/tag rules, two-layer mapping, field-role rules)."""
    import agents.onboard_agent as onboard_agent
    prompt = onboard_agent._build_mapfields_prompt(
        "sysid", "concept-a", "  hint", "t: c", is_mcp=True)
    for marker in (
        "CRITICAL OPC UA TAG PATH MAPPING",
        "<Enterprise>/<Site>/<Area>/<Line>/<Device>/<Signal>",
        "- Tags ending in _Down, _Blocked, _Starved → concept 'downtime', role 'identifier'",
        "The equipment station name (table name like Line1Washer, Line2CNC1) → concept 'equipment-id', role 'identifier'",
        "The line segment in the path (Line1, Line2, Line3) → concept 'production-line', role 'identifier'",
        "CRITICAL MANUFACTURING LINE MAPPING",
        "MUST map to concept 'production-line' with role 'identifier'",
        "Do NOT map these to 'work-order', 'location', or 'equipment-id'",
        "TWO-LAYER MAPPING",
        "FIELD ROLE RULES",
    ):
        assert marker in prompt, f"structural instruction dropped: {marker!r}"


def test_moved_demo_instances_reach_composed_prompt_via_overlay(monkeypatch):
    """Round-trip proof: the demo instances removed from the prompt text live
    in DEMO_CONCEPT_OVERLAYS['production-line'] and, rendered as a
    ConceptOverlay row through catalog's shared composition point, land in
    the composed concept_hints surface that _build_mapfields_prompt embeds —
    equivalent example coverage when demo is enabled, via the T1.3 path."""
    import agents.catalog as catalog
    import agents.db as db
    import agents.onboard_agent as onboard_agent
    from agents.demo.concept_overlays import DEMO_CONCEPT_OVERLAYS

    fields = DEMO_CONCEPT_OVERLAYS["production-line"]
    row = {"concept_id": "production-line", "pack_id": "demo",
           "source": "pack:demo@1", "examples": fields.get("examples", ""),
           "synonyms": "", "value_patterns": fields.get("value_patterns", ""),
           "note": ""}

    def _q(cypher, parameters=None):
        return [dict(row)] if "ConceptOverlay" in cypher else []

    monkeypatch.setattr(db, "neptune_query", _q)
    hints = catalog.concept_hints_block()
    prompt = onboard_agent._build_mapfields_prompt(
        "sysid", "production-line", hints, "t: c", is_mcp=True)
    # The overlay body must fit the per-overlay cap — nothing truncated away.
    from agents.catalog import CONCEPT_OVERLAY_MAX_CHARS, _overlay_line
    line = _overlay_line("production-line", row)
    assert len(line) < CONCEPT_OVERLAY_MAX_CHARS + len("⟂ production-line [pack:demo@1] ")
    for literal in ("Bottling-L1", "AutoParts-L2", "FoodProc-L3", "MKE-L1",
                    "AnyCompany/Milwaukee/Bottling/Line1/Line1Washer/Line1Washer_Down"):
        assert literal in prompt, (
            f"moved demo instance {literal!r} did not reach the composed map "
            f"prompt through the overlay path")
    # And without overlays the same surface stays clean (the neutral default).
    monkeypatch.setattr(db, "neptune_query",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    clean = onboard_agent._build_mapfields_prompt(
        "sysid", "production-line", catalog.concept_hints_block(), "t: c",
        is_mcp=True)
    assert not _denylist_hits(clean)
