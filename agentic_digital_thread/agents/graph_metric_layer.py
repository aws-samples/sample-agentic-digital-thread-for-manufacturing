# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Composed metric layer (ADR-0066 D2/C5): the code seed registry PLUS the
graph-resident metric definitions, behind the UNCHANGED ``MetricLayer`` surface.

``ComposedMetricLayer`` subclasses ``CodeMetricLayer``, so the 23 seed metrics
are the SAME objects served by the same construction-time registry and every
existing consumer — ``metric_resolver.resolve_metric``, ``card_layer``,
``card_live``, ``metric_multisite``, ``metric_dimension``, ``coverage``,
``concept_layer``, ``heatmap_resolver``, ``rederive``,
``metric_term_resolution`` — works with zero edits. With no graph half present
the layer is byte-identical to ``CodeMetricLayer()``: exactly the seed registry,
in the seed order (pinned by ``tests/unit/test_graph_metric_layer.py``, and by
``tests/unit/test_metric_layer.py`` passing unmodified).

Precedence (C5, never silent): **code metric ids always win.** A graph row whose
id collides with a code metric is SKIPPED here with a ``logger.warning`` (the
read-time arm) and is reported as ``shadowed`` by
``metric_definition_store.shadowed_or_invalid()`` — the store excludes it from
the snapshot for the same reason, so this layer's skip is the residual belt for a
row that predates the check or arrives through a monkeypatched seam. It never
resolves, and it never disappears from view.

Unknown ids keep the EXACT ``KeyError`` shape of the code layer
(``unknown metric id: '<id>'``), so ``resolve_metric``'s ``MetricResolverError``
text stays stable.

**Degradation is deliberately different from the pre-check's, and the difference
is the honest part.** The store raises on a cold-cache load failure (ADR-0066 D4)
because the metric-TERM pre-check must never turn a graph outage into a
fabricated "undefined in this deployment" claim; it keeps that raise and fails
open to silence. A metric LAYER, by contrast, is on the path of every card,
coverage scan and resolve: raising there would convert a graph blink into a
broken dashboard. So this layer catches the store's failure, logs it, and serves
code metrics only. That is not the rejected alternative in D4: nothing here ever
claims a term is undefined — a graph-defined id simply does not resolve, which
raises the SAME honest ``KeyError``/``MetricResolverError`` an unknown metric has
always raised, and the card/coverage paths already read that as uncovered rather
than as a value (ADR-0045 honest-data rule). A failed load also opens a short
retry cooldown so a failing (or absent) graph is not re-queried once per caller.

Import purity (ADR 0001): module level imports stdlib plus the import-pure
``metric_layer``. The store and the document→dataclass builder are reached ONLY
through the lazy, monkeypatchable seams below, so a deployment without the graph
half (local dev, the CDK venv) keeps working — the seam raises ``ImportError``
and the layer stays code-only for the rest of the process.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Iterable, Mapping

from .metric_layer import CodeMetricLayer, Metric

logger = logging.getLogger(__name__)

# A failed (or absent) graph must not be re-queried once per ``list_metrics``
# caller. Successful loads are bounded by the STORE's TTL — this constant only
# bounds retry pressure after a failure, so it is deliberately much shorter than
# that TTL: an accept calls ``invalidate()``, which clears the cooldown outright.
GRAPH_FAILURE_COOLDOWN_SECONDS = 60.0


# --- Lazy seams (import purity + the monkeypatch points the offline lane uses) -

def load_definitions() -> dict[str, dict]:
    """Lazy, monkeypatchable metric-definition snapshot seam (store C4)."""
    from .metric_definition_store import load_definitions as _load
    return _load()


def invalidate_definitions() -> None:
    """Lazy, monkeypatchable snapshot-invalidation seam (store C4)."""
    from .metric_definition_store import invalidate as _invalidate
    _invalidate()


def build_metric(doc: Mapping[str, Any]) -> Metric:
    """Lazy, monkeypatchable document → frozen dataclass seam (C1 ``to_metric``)."""
    from .metric_definition import to_metric
    return to_metric(doc)


class ComposedMetricLayer(CodeMetricLayer):
    """The code registry composed with the TTL-cached graph-definition snapshot.

    Thread-safe: the graph half is rebuilt under a lock and only when the
    snapshot CONTENT changes, so the inherited concept-closure memo never spans
    two snapshot generations (it is cleared on every rebuild — correctness over
    micro-speed).
    """

    def __init__(self, metrics: Iterable[Metric] | None = None) -> None:
        super().__init__(metrics)
        self._code_ids: frozenset[str] = frozenset(self._by_id)
        self._graph_lock = threading.RLock()
        self._graph_docs: dict[str, dict] | None = None
        self._graph_by_id: dict[str, Metric] = {}
        self._graph_importable = True
        self._cooldown_until: float | None = None

    # --- MetricLayer surface -------------------------------------------------

    def get_metric(self, metric_id: str) -> Metric:
        """Code first (C5), then the graph snapshot; unknown ⇒ the code layer's
        exact ``KeyError`` shape."""
        try:
            return super().get_metric(metric_id)
        except KeyError:
            pass
        try:
            return self._graph_metrics()[metric_id]
        except KeyError:
            raise KeyError(f"unknown metric id: {metric_id!r}") from None

    def list_metrics(self) -> list[Metric]:
        """The seed registry (same objects, same order) followed by the
        graph-defined metrics in id order."""
        metrics = super().list_metrics()
        metrics.extend(self._graph_metrics().values())
        return metrics

    # --- Composition state ---------------------------------------------------

    def is_graph_defined(self, metric_id: str) -> bool:
        """Whether ``metric_id`` is served by the graph snapshot rather than the
        code registry (the ADR-0052 usage-telemetry seam; a shadowed id is code
        defined, so it is False)."""
        if metric_id in self._code_ids:
            return False
        return metric_id in self._graph_metrics()

    def graph_metric_ids(self) -> frozenset[str]:
        """Ids currently served from the graph snapshot."""
        return frozenset(self._graph_metrics())

    def invalidate(self) -> None:
        """Drop the graph half (this layer's build AND the store snapshot).

        Called in-process by the accept path so an accepted definition is
        visible immediately; other processes converge within the store TTL.
        """
        with self._graph_lock:
            self._graph_docs = None
            self._graph_by_id = {}
            self._cooldown_until = None
            self._graph_importable = True
            self._concepts_memo.clear()
        try:
            invalidate_definitions()
        except Exception as exc:  # graph half absent ⇒ nothing to drop
            logger.warning(
                "Metric-definition snapshot invalidation skipped category=%s",
                type(exc).__name__,
            )

    # --- Internals -----------------------------------------------------------

    def _graph_metrics(self) -> dict[str, Metric]:
        """The graph half as dataclasses, rebuilt only when the snapshot changes."""
        with self._graph_lock:
            docs = self._load_docs()
            if self._graph_docs is not None and self._graph_docs == docs:
                return self._graph_by_id
            self._graph_docs = docs
            self._graph_by_id = self._build(docs)
            # The closure memo may hold entries whose walk crossed the previous
            # snapshot — never carry it across a generation.
            self._concepts_memo.clear()
            return self._graph_by_id

    def _load_docs(self) -> dict[str, dict]:
        """Fetch the snapshot, failing OPEN to code-only (see the module docstring)."""
        if not self._graph_importable:
            return {}
        now = time.monotonic()
        if self._cooldown_until is not None and now < self._cooldown_until:
            return {}
        try:
            docs = load_definitions()
        except ImportError as exc:
            self._graph_importable = False
            logger.warning(
                "Metric-definition graph half unavailable; serving code metrics "
                "only for this process category=%s", type(exc).__name__,
            )
            return {}
        except Exception as exc:
            self._cooldown_until = now + GRAPH_FAILURE_COOLDOWN_SECONDS
            logger.warning(
                "Metric-definition snapshot unavailable; serving code metrics "
                "only category=%s", type(exc).__name__,
            )
            return {}
        self._cooldown_until = None
        if not isinstance(docs, Mapping):
            logger.warning(
                "Metric-definition snapshot has an unexpected shape category=%s",
                type(docs).__name__,
            )
            return {}
        return dict(docs)

    def _build(self, docs: Mapping[str, Any]) -> dict[str, Metric]:
        built: dict[str, Metric] = {}
        for metric_id in sorted(docs):
            if metric_id in self._code_ids:
                logger.warning(
                    "Graph metric definition %s is shadowed by a code metric "
                    "id; serving the code definition", metric_id,
                )
                continue
            try:
                metric = build_metric(docs[metric_id])
            except Exception as exc:
                logger.warning(
                    "Graph metric definition %s is unusable category=%s",
                    metric_id, type(exc).__name__,
                )
                continue
            if metric.id != metric_id:
                logger.warning(
                    "Graph metric definition %s carries a different metric id "
                    "%s; skipping", metric_id, metric.id,
                )
                continue
            built[metric_id] = metric
        return built
