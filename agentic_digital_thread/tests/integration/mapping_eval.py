# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Mapping-quality eval — score live field->concept mappings vs an authored key.

ADR-0012: structural assertions ("\u22651 MAPS_TO exists") prove the mapping is
*present*, not *correct*. This module scores the LLM-produced mappings against a
ground-truth key authored from domain meaning (``expected_mappings/<system>.yaml``)
with **thresholds**, not equality:

  * a small **must-map tracer set** is asserted at 100% (present, actual concept
    in its acceptable set, AND confidence >= the must-map gate), and
  * an **overall accuracy floor** covers the remaining keyed fields.

It is strictly READ-ONLY: it runs the audit's ``CYPHER_FIELD_CONCEPT`` via
``agents.db.neptune_query`` (graph reads only — NO ``neptune_write``) and never
invokes an LLM (the eval is deterministic given a fixed graph).

Run standalone from ``agentic_digital_thread/``::

    python -m tests.integration.mapping_eval --system-id cmms

Exit codes mirror the audit's skip-clean contract: ``0`` pass, ``1`` threshold
fail, ``0`` with a ``nothing_to_eval`` message when no graph is reachable / the
system has no mappings.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from typing import Any

import yaml

from agents.catalog import CORE_CONCEPTS

# Reuse the audit's read seam VERBATIM (never re-declare the cypher or helper) so
# the eval and the shape audit provably read identical graph structure.
from tests.audit.audit_onboarding_graph import CYPHER_FIELD_CONCEPT, _row_get
from agents.db import neptune_query

DEFAULT_OVERALL_FLOOR = 0.90
DEFAULT_MUST_MAP_MIN_CONFIDENCE = 0.60

KEY_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "expected_mappings")
NOTHING_TO_EVAL_MSG = "no reachable graph / no mappings for system — nothing to eval"


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class ScoreReport:
    """Structured eval outcome (also serialized to the JSON demo artifact)."""

    system: str
    status: str  # "pass" | "fail" | "nothing_to_eval"
    overall_accuracy: float = 0.0
    overall_floor: float = DEFAULT_OVERALL_FLOOR
    must_map_min_confidence: float = DEFAULT_MUST_MAP_MIN_CONFIDENCE
    total_key_fields: int = 0
    present: int = 0
    hits: int = 0
    must_map_total: int = 0
    must_map_pass: int = 0
    misses: list[dict] = field(default_factory=list)
    must_map_failures: list[dict] = field(default_factory=list)
    out_of_key: list[dict] = field(default_factory=list)
    ignored_present: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        # Status-independent so score() can derive status from it. A
        # nothing_to_eval report has present==0 -> not passed.
        return (
            self.present > 0
            and self.must_map_pass == self.must_map_total
            and self.overall_accuracy >= self.overall_floor
        )


# ---------------------------------------------------------------------------
# Key loading + validation
# ---------------------------------------------------------------------------

def key_path_for(system_id: str) -> str:
    """Return the expected-mapping key path for a system id."""
    return os.path.join(KEY_DIR, f"{system_id}.yaml")


def _iter_concept_ids(fields: dict) -> set[str]:
    ids: set[str] = set()
    for spec in fields.values():
        ids.update(spec.get("acceptable", []) or [])
    return ids


def load_key(path: str) -> dict:
    """Load + validate an authored key from ``path``.

    Validates EVERY concept id in ``fields[*].acceptable`` against
    ``catalog.CORE_CONCEPTS`` and raises ``ValueError`` naming the offending id
    (guards key drift). Returns the parsed dict with thresholds defaulted.
    """
    with open(path) as fh:
        key = yaml.safe_load(fh) or {}

    fields = key.get("fields") or {}
    if not fields:
        raise ValueError(f"key {path!r} has no 'fields' section")

    unknown = sorted(_iter_concept_ids(fields) - set(CORE_CONCEPTS))
    if unknown:
        raise ValueError(
            f"key {path!r} references concept id(s) not in catalog.CORE_CONCEPTS: "
            f"{unknown}. Fix the key (author from meaning) or use a real core concept id."
        )

    key.setdefault("overall_floor", DEFAULT_OVERALL_FLOOR)
    key.setdefault("must_map_min_confidence", DEFAULT_MUST_MAP_MIN_CONFIDENCE)
    key.setdefault("ignore", [])
    return key


# ---------------------------------------------------------------------------
# Live read (neptune_query-only)
# ---------------------------------------------------------------------------

def _pick_mapping(rows: list[dict]) -> dict:
    """Pick one mapping for a field: identifier-role first, else highest confidence."""
    def conf(r):
        try:
            return float(r.get("confidence") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    identifiers = [r for r in rows if (r.get("role") or "").lower() == "identifier"]
    pool = identifiers or rows
    return max(pool, key=conf)


def read_live_mappings(system_id: str) -> dict[str, dict]:
    """Return ``{"table.column": {concept, role, confidence}}`` from the live graph.

    Runs the imported ``CYPHER_FIELD_CONCEPT`` via ``neptune_query`` (read-only).
    A field with multiple ``MAPS_TO`` collapses to one mapping via ``_pick_mapping``
    (identifier role wins, else highest confidence — mirrors onboarding selection).
    Returns ``{}`` when the graph is unreachable or nothing is onboarded.
    """
    rows = neptune_query(CYPHER_FIELD_CONCEPT, {"sid": system_id}) or []
    grouped: dict[str, list[dict]] = {}
    for r in rows:
        table = _row_get(r, "table", "se.name")
        column = _row_get(r, "column", "f.name")
        if not table or not column:
            continue
        grouped.setdefault(f"{table}.{column}", []).append({
            "concept": _row_get(r, "concept", "c.id"),
            "role": _row_get(r, "role", "r.role"),
            "confidence": _row_get(r, "confidence", "r.confidence"),
        })
    return {fld: _pick_mapping(rows) for fld, rows in grouped.items()}


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _confidence(mapping: dict) -> float:
    try:
        return float(mapping.get("confidence") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _score_must_map(name: str, spec: dict, mapping: dict | None,
                    floor: float) -> tuple[bool, dict | None]:
    """Return (passed, failure_record) for one must-map field."""
    gate = max(floor, float(spec.get("min_confidence", floor)))
    if mapping is None:
        return False, {"field": name, "reason": "not present in graph",
                       "acceptable": spec["acceptable"]}
    concept = mapping.get("concept")
    if concept not in spec["acceptable"]:
        return False, {"field": name, "reason": "concept not in acceptable set",
                       "actual": concept, "acceptable": spec["acceptable"]}
    conf = _confidence(mapping)
    if conf < gate:
        return False, {"field": name, "reason": f"confidence {conf:.2f} < gate {gate:.2f}",
                       "actual": concept, "confidence": conf}
    return True, None


def score(key: dict, live: dict[str, dict]) -> ScoreReport:
    """Score live mappings against an authored key (ADR-0012 thresholds)."""
    fields = key["fields"]
    ignore = set(key.get("ignore", []))
    floor = float(key["overall_floor"])
    mm_min_conf = float(key["must_map_min_confidence"])
    report = ScoreReport(
        system=key.get("system", "?"), status="nothing_to_eval",
        overall_floor=floor, must_map_min_confidence=mm_min_conf,
        total_key_fields=len(fields),
    )
    if not live:
        return report

    for name, spec in fields.items():
        mapping = live.get(name)
        if spec.get("must_map"):
            report.must_map_total += 1
            ok, fail = _score_must_map(name, spec, mapping, mm_min_conf)
            if ok:
                report.must_map_pass += 1
            elif fail:
                report.must_map_failures.append(fail)
        if mapping is None:
            continue
        report.present += 1
        if mapping.get("concept") in spec["acceptable"]:
            report.hits += 1
        else:
            report.misses.append({"field": name, "actual": mapping.get("concept"),
                                  "acceptable": spec["acceptable"]})

    for name, mapping in live.items():
        if name in fields:
            continue
        if name in ignore:
            report.ignored_present.append(name)
        else:
            report.out_of_key.append({"field": name, "concept": mapping.get("concept")})

    report.overall_accuracy = (report.hits / report.present) if report.present else 0.0
    report.status = "pass" if report.passed else "fail"
    return report


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def _report_path(system: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), f"mapping-eval-{system}.json")


def format_report(report: ScoreReport) -> str:
    """Return the human/demo-artifact line and persist the JSON report."""
    try:
        with open(_report_path(report.system), "w") as fh:
            json.dump(asdict(report), fh, indent=2, default=str)
    except OSError as e:  # never fail the eval on the artifact write
        print(f"warning: could not write report: {e}", file=sys.stderr)

    if report.status == "nothing_to_eval":
        return f"{report.system} mapping eval: {NOTHING_TO_EVAL_MSG}"

    pct = round(report.overall_accuracy * 100)
    if report.must_map_pass == report.must_map_total:
        critical = f"all {report.must_map_total} critical fields exact"
    else:
        names = ", ".join(f["field"] for f in report.must_map_failures)
        critical = (f"{report.must_map_pass}/{report.must_map_total} critical fields exact "
                    f"(FAILED: {names})")
    return (f"{report.system} mapping eval: mapped {report.hits}/{report.present} "
            f"fields correctly ({pct}%), {critical}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def run(system_id: str, floor: float | None = None) -> ScoreReport:
    """Load the key, read live mappings, and score — the CLI/test entry point."""
    key = load_key(key_path_for(system_id))
    if floor is not None:
        key["overall_floor"] = floor
    return score(key, read_live_mappings(system_id))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mapping_eval",
        description="Score live field->concept mappings vs an authored key (ADR-0012).",
    )
    parser.add_argument("--system-id", default="cmms", help="System id to eval (default: cmms).")
    parser.add_argument("--floor", type=float, default=None,
                        help="Override the overall accuracy floor (default: from key/0.90).")
    args = parser.parse_args(argv)

    report = run(args.system_id, args.floor)
    print(format_report(report))
    if report.status == "nothing_to_eval":
        print(f"\n{NOTHING_TO_EVAL_MSG}", file=sys.stderr)
        return 0
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
