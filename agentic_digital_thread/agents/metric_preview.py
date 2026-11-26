# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Dry-run preview of a PENDING metric definition (ADR-0080).

A steward reviewing a proposed metric definition can ask "what number would
this card show?" BEFORE accepting it. The answer is a request-scoped,
read-only RESOLVE:

* the pending proposal's ``query_context`` goes through the SAME
  ``parse_definition`` → ``validate_definition`` → ``to_metric`` path accept
  uses (a preview never resolves a definition accept would reject);
* the value is computed by the SAME resolver a live card uses
  (``metric_resolver.resolve_definition`` — and the same multi-site fan-out
  gate as the dashboard), and rendered into the SAME card-metric register
  (``dashboard.card_entry_from_resolved``) — so ``ok`` / ``no-data`` /
  ``unavailable`` / ``invariant-violation`` / ``unresolved-predicate`` /
  ``ambiguous-binding`` mean exactly what they mean on the dashboard;
* a derived input that resolves to neither a served metric nor a pending id
  supplied with the request (``with=``) WITHHOLDS as ``unresolved-input``
  naming the id, and nothing executes (D3);
* it writes NOTHING: the served metric layer is read, never mutated or
  invalidated; no ADR-0066 D6 usage telemetry fires (the pending definition is
  handed to the resolver directly, never looked up through ``resolve_metric``);
  no dashboard cache, memory or proposal status is touched;
* its value executions run under an ``execution_trace(":preview")`` — Athena
  executions carry the ``:preview`` ``source_system`` attribution and the
  response reports the MEASURED cost (query count + engine-reported bytes;
  never estimated, never dollars — ADR-0053).

No LLM anywhere: parse, validation, resolution and status are computed.
"""
from __future__ import annotations

import re
from collections.abc import Mapping

from . import metric_resolver as mr
from .metric_execution_trace import execution_trace
from .metric_layer import CodeMetricLayer, DerivedMetric, Formula, Metric, MetricRef

#: Upper bound on pending companions one preview may resolve together.
MAX_WITH_IDS = 8
_WITH_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


def _no_cost() -> dict:
    """The measured cost of a preview that executed nothing."""
    return {"query_count": 0, "systems": []}


class PreviewParameterError(ValueError):
    """A malformed ``with=`` parameter (the route answers 400)."""


def parse_with_ids(raw: str | None, self_id: str) -> tuple[str, ...]:
    """``with=a,b`` → ``("a", "b")``: bounded, charset-checked, no duplicate,
    never the previewed proposal itself. Absent/blank ⇒ ``()``."""
    if raw is None or not raw.strip():
        return ()
    ids = tuple(part.strip() for part in raw.split(","))
    if len(ids) > MAX_WITH_IDS:
        raise PreviewParameterError(
            f"with= names {len(ids)} proposals; at most {MAX_WITH_IDS} are allowed")
    for mapping_id in ids:
        if not _WITH_ID_RE.fullmatch(mapping_id):
            raise PreviewParameterError(
                f"with= carries a malformed proposal id {mapping_id!r}")
    if len(set(ids)) != len(ids):
        raise PreviewParameterError("with= names the same proposal twice")
    if self_id in ids:
        raise PreviewParameterError("with= names the previewed proposal itself")
    return ids


def dry_run(raw_doc, with_docs: Mapping[str, str], context: tuple, *,
            layer=None) -> dict:
    """Resolve the pending definition ``raw_doc`` (plus pending companions
    ``with_docs`` keyed by mapping id) → the ``dry_run`` block. ZERO writes.

    ``computed_at`` is stamped when each outcome is FINAL (after resolution),
    never at request start — a slow bounded execution must not claim an
    earlier completion than the value it reports."""
    try:
        docs, pending = _build_pending(raw_doc, with_docs)
    except (ValueError, TypeError) as exc:
        return _invalid(None, str(exc))
    main_id = next(iter(docs))
    metric = pending[main_id]
    preview_layer = _preview_layer(layer or mr.get_metric_layer(), pending)
    missing = missing_inputs(metric, preview_layer)
    if missing:
        return _unresolved_input(metric, missing)
    error = _validation_error(docs, pending, context)
    if error:
        return _invalid(main_id, error)
    with execution_trace() as trace:
        entry = _resolve_entry(metric, preview_layer)
    return _stamp(entry, metric, trace.cost())


def _build_pending(raw_doc, with_docs: Mapping[str, str]) -> tuple[dict, dict]:
    """Parse every document through the accept path's parser and build its
    dataclass through the served layer's builder; main document first."""
    from .graph_metric_layer import build_metric
    from .metric_definition import MetricDefinitionError, parse_definition

    docs: dict[str, dict] = {}
    try:
        for raw in (raw_doc, *with_docs.values()):
            doc = parse_definition(raw or "")
            if doc["id"] in docs:
                raise ValueError(
                    f"metric id {doc['id']!r} is defined by more than one "
                    "previewed proposal")
            docs[doc["id"]] = doc
    except MetricDefinitionError as exc:
        raise ValueError(str(exc)) from exc
    return docs, {mid: build_metric(doc) for mid, doc in docs.items()}


def _preview_layer(served, pending: Mapping[str, Metric]) -> CodeMetricLayer:
    """A request-scoped layer: the served metrics with the pending ones laid
    over them. Built fresh per request; the served layer is only READ."""
    by_id = {m.id: m for m in served.list_metrics()}
    by_id.update(pending)
    return CodeMetricLayer(list(by_id.values()))


def _formula_refs(term) -> list[str]:
    if isinstance(term, MetricRef):
        return [term.metric]
    if isinstance(term, Formula):
        return [ref for child in term.inputs for ref in _formula_refs(child)]
    return []


def missing_inputs(metric: Metric, layer) -> tuple[str, ...]:
    """Every formula ``MetricRef`` (transitively) the layer cannot supply."""
    missing: set[str] = set()
    seen: set[str] = set()
    stack = [metric]
    while stack:
        current = stack.pop()
        if current.id in seen or not isinstance(current, DerivedMetric):
            continue
        seen.add(current.id)
        for ref in _formula_refs(current.formula):
            try:
                stack.append(layer.get_metric(ref))
            except KeyError:
                missing.add(ref)
    return tuple(sorted(missing))


def _validation_error(docs: Mapping[str, dict], pending: Mapping[str, Metric],
                      context: tuple) -> str:
    """The accept path's validator over every previewed document, against the
    current universe extended by the pending ids. ``""`` when all validate."""
    from .self_learning import _validate_metric_definition

    concepts, metric_ids, code_ids, definitions = context
    extended = (concepts, frozenset(metric_ids) | frozenset(pending), code_ids,
                {**dict(definitions), **dict(docs)})
    for doc in docs.values():
        try:
            _validate_metric_definition(doc, extended)
        except ValueError as exc:
            return str(exc)
    return ""


def _fan_out_systems(metric_id: str, layer) -> list[dict] | None:
    """The dashboard's multi-site gate: ≥2 onboarded AND >1 covering system ⇒
    the onboarded list to fan out over; otherwise ``None`` (single authority).
    Fail-safe to ``None`` — the fan-out is an enrichment, as on the dashboard."""
    from .catalog import catalog
    from . import metric_multisite
    try:
        onboarded = catalog.get_onboarded() or []
        if len(onboarded) < 2:
            return None
        covering = metric_multisite.systems_covering(
            metric_id, metric_layer=layer, systems=onboarded, readable_only=False)
    except Exception:  # nosec B110 - enrichment gate is best-effort, as on the dashboard
        return None
    return onboarded if len(covering) > 1 else None


def _connector_refusals() -> tuple[type[BaseException], ...]:
    """The typed connector refusals a preview resolve can raise that mean "this
    preview cannot be computed under the data source's limits": the SAME
    structured set the system agent treats as a tool refusal. Rendered as the
    unavailable card (a plain reason), never a 500. Unknown exceptions keep
    their current behaviour and still propagate."""
    from . import athena_tools
    return (athena_tools.AthenaEngineScanLimitError,
            athena_tools.AthenaScanBudgetError,
            athena_tools.AthenaQueryTimeoutError)


def _refusal_card(metric_id: str, exc: BaseException) -> dict:
    """A typed connector refusal → the unavailable card with a plain,
    source-free reason (never the raw connector message)."""
    from . import athena_tools, dashboard
    if isinstance(exc, athena_tools.AthenaQueryTimeoutError):
        reason = "the preview query did not finish within this data source's time limit"
    else:  # scan-limit (engine) or app-side scan budget
        reason = "the preview query is over this data source's per-query scan limit"
    return dashboard.unavailable_card_entry(metric_id, RuntimeError(reason))


def _resolve_entry(metric: Metric, layer) -> dict:
    """Resolve through the live card's path → a card metric entry."""
    from . import dashboard, metric_multisite

    def resolver(metric_id: str, *, authority=None):
        return mr.resolve_definition(layer.get_metric(metric_id), metric_id=metric_id,
                                     authority=authority, layer=layer)

    systems = _fan_out_systems(metric.id, layer)
    if systems is not None:
        try:
            multi = metric_multisite.resolve_metric_multi(
                metric.id, resolver=resolver, metric_layer=layer, systems=systems)
            return dashboard.multi_card_entry(metric.id, multi, attach_sites=True)
        except (mr.MetricResolverError, NotImplementedError, KeyError):
            pass  # fail-soft to the single-authority path, as the dashboard does
        except _connector_refusals() as refusal:
            return _refusal_card(metric.id, refusal)
    try:
        return dashboard.card_entry_from_resolved(resolver(metric.id))
    except (mr.MetricResolverError, NotImplementedError, KeyError) as exc:
        return dashboard.unavailable_card_entry(metric.id, exc)
    except _connector_refusals() as refusal:
        return _refusal_card(metric.id, refusal)


def _stamp(entry: dict, metric: Metric, cost: dict) -> dict:
    """Mark a FINAL card entry as a PREVIEW with its window, cost and the
    completion timestamp (taken now — the outcome is already computed)."""
    return {**entry, "preview": True, "metric_id": metric.id,
            "window": metric.window, "computed_at": mr._utcnow_iso(),
            "cost": cost}


def _unresolved_input(metric: Metric, missing: tuple[str, ...]) -> dict:
    """D3 WITHHOLD: nothing executed; every missing input NAMED."""
    from . import dashboard
    detail = "; ".join(str(mr.UnresolvedInputError(ref)) for ref in missing)
    withheld = mr.ResolvedMetric(
        metric_id=metric.id, value=None, unit=metric.unit, label=metric.label,
        query_plan=None, system_id="derived", lineage=None,
        status=mr.UNRESOLVED_INPUT_STATUS, detail=detail,
    )
    entry = dashboard.card_entry_from_resolved(withheld)
    entry["missing_inputs"] = list(missing)
    return _stamp(entry, metric, _no_cost())


def _invalid(metric_id: str | None, error: str) -> dict:
    """A definition accept would reject is never resolved: nothing executed."""
    return {"preview": True, "metric_id": metric_id, "status": "invalid",
            "value": None, "detail": error, "window": None,
            "computed_at": mr._utcnow_iso(), "cost": _no_cost()}
