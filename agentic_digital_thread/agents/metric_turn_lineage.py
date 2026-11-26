# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""ADR-0078 — the lineage summary a COMPUTED metric-layer result carries.

Split from ``metric_turn_notice`` for the file-size cap. Pure: it reads a
``metric_resolver.Lineage`` / ``ResolvedMetric`` that already exists and never
resolves anything.

Two shapes (D5): ``direct`` — ONE stated read (a direct metric, or a derived
metric whose ``prefer_stored`` input was hit) whose window fields are that
read's own; ``formula`` — the metric layer applied an op over its inputs, so
the temporal scope is carried PER EXECUTED INPUT (``input_windows``), never
flattened into a single token the formula does not have.
"""
from __future__ import annotations

from .metric_turn_render import LINEAGE_CUT_MARKER as CUT_MARKER  # noqa: F401 - re-export
from .metric_turn_render import LINEAGE_INCOMPLETE_KEY as INCOMPLETE_KEY
from .metric_turn_render import MAX_DETAIL_CHARS

MAX_LIST_ITEMS = 6


def _input_ids(lineage, out: list[str] | None = None) -> list[str]:
    out = [] if out is None else out
    for child in getattr(lineage, "inputs", ()) or ():
        if child is None or len(out) >= MAX_LIST_ITEMS:
            continue
        if child.metric_id:
            if child.metric_id not in out:
                out.append(child.metric_id)
        else:
            _input_ids(child, out)
    return out


def _leaf_name(lineage) -> str:
    return lineage.metric_id or lineage.concept or "input"


def input_windows(lineage) -> tuple[list[dict], bool]:
    """``(windows, truncated)`` — one ``{"input", "requested", "applied"}``
    per EXECUTED direct read under a formula (nested formulas walked; a
    stored-hit derived input contributes its stored read). ``truncated`` ⇒
    more reads than :data:`MAX_LIST_ITEMS`, so the list is not the whole
    story and the renderer must not claim a uniform window."""
    out: list[dict] = []
    stack = list(reversed(getattr(lineage, "inputs", ()) or ()))
    truncated = False
    while stack:
        child = stack.pop()
        if child is None:
            continue
        if getattr(child, "kind", "direct") == "derived":
            stack.extend(reversed(child.inputs or ()))
            continue
        if len(out) >= MAX_LIST_ITEMS:
            truncated = True
            break
        out.append({"input": _leaf_name(child),
                    "requested": child.window_requested, "applied": child.window})
    return out, truncated


def _plan_text(plan) -> str:
    # Delegate to the canonical measure-aware formatter so a measure plan whose
    # concept column is empty renders duration(start, end) rather than an empty
    # slot. Lazy import keeps this module import-pure (metric_resolver does not
    # import it, so there is no cycle).
    from . import metric_resolver
    return metric_resolver.plan_lineage(plan)


def _scan_fields(scan) -> dict:
    """How the read stayed under the scan limit, only when it did not run as
    defined in one query (``metric_athena_scope``): no keys otherwise."""
    from .metric_athena_scope import inexact, scan_text
    text = scan_text(scan)
    if not text:
        return {}
    fields = {"scan": text, "scan_narrowed": scan.get("mode") == "narrowed"}
    if inexact(scan):
        fields["scan_inexact"] = True
    return fields


def lineage_summary(resolved) -> dict:
    """``shape`` direct (one stated read) or formula, plus lineage + windows."""
    lineage = resolved.lineage
    kind = "derived" if getattr(lineage, "kind", "direct") == "derived" else "direct"
    read = lineage
    plan = resolved.query_plan
    if lineage is not None and lineage.prefer_stored_hit and lineage.inputs:
        read, plan = lineage.inputs[0], None
    if read is not None and read.kind == "derived":
        read = None
    plan = plan or getattr(read, "query_plan", None)
    if kind == "direct" or (read is not None and plan is not None):
        return {"kind": kind, "shape": "direct", **_read_lineage(read, plan),
                "window_requested": getattr(read, "window_requested", None),
                "window_applied": getattr(read, "window", None),
                **_scan_fields(getattr(read, "scan", None))}
    op = getattr(lineage, "op", None) or "formula"
    windows, truncated = input_windows(lineage)
    return {"kind": kind, "shape": "formula",
            "lineage": f"{op}({', '.join(_input_ids(lineage))})",
            "window_requested": None, "window_applied": None,
            "input_windows": windows, "input_windows_truncated": truncated}


def _read_lineage(read, plan) -> dict:
    """``lineage`` (and ``lineage_incomplete``) of ONE stated read. A duration
    read carries its rendered definition (:func:`definition_lineage`); any
    other read keeps its plan text unchanged (whether that text omits a
    declared filter matters only for a named site:
    ``metric_turn_sites.with_named_sites``)."""
    if plan is None:
        return {"lineage": "direct read"}
    short = _plan_text(plan)
    if getattr(plan, "measure", None) is not None:
        return definition_lineage(getattr(read, "metric_id", None), plan.system_id,
                                  plan.table, short)
    return {"lineage": short}


def grouped_lineage(grm) -> dict:
    """``lineage`` (and ``lineage_incomplete``) of a GROUPED computed result:
    the metric's rendered definition, grouping included
    (:func:`definition_lineage`)."""
    plan = grm.query_plan
    return definition_lineage(grm.metric_id, plan.system_id, plan.measure_table,
                              f"grouped by {grm.dimension_concept}")


def definition_lineage(metric_id, system_id: str, table: str, short: str) -> dict:
    """``{"lineage": "<system>.<table>: <rendered definition>"}``, the served
    metric's definition (``metric_definition.render_metric``) so a system
    agent can apply exactly it at a narrower scope.

    A rendering longer than the notice's lineage bound (the runtime
    sanitizer's ``MAX_DETAIL_CHARS``) is carried up to that bound and ends in
    :data:`CUT_MARKER`; a definition that cannot be read leaves ``short``.
    Either way :data:`INCOMPLETE_KEY` is set, so no block calls it an exact
    lineage to apply. Never raises."""
    try:
        from . import metric_definition, metric_resolver
        metric = metric_resolver.get_metric_layer().get_metric(metric_id)
        text = " ".join(f"{system_id}.{table}: "
                        f"{metric_definition.render_metric(metric)}".split())
    except Exception:  # noqa: BLE001 - the short form is still true, but not the definition
        return {"lineage": short, INCOMPLETE_KEY: True}
    if len(text) <= MAX_DETAIL_CHARS:
        return {"lineage": text}
    cut = text[:MAX_DETAIL_CHARS - len(CUT_MARKER)].rstrip()
    return {"lineage": cut + CUT_MARKER, INCOMPLETE_KEY: True}
