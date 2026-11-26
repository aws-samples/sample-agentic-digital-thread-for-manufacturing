<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# Derived-metric resolution: formulas compose Metrics, evaluated at resolve-time, with an ADR-0002 stored-composite shortcut

---
Status: accepted
---

This change brings the **derived** path online — the op-tree *evaluator* ADR 0014 deferred — and
makes `oee` resolvable. Reviewing the design against the as-built code fixed the resolution design.
Grounding: `metric_layer.py` defines the op-tree TYPE (`Formula{op, inputs[]}`, leaves
`MetricRef`/`ConceptRef`/`Scalar`); the seed `oee` composed **`ConceptRef`s**; `resolve_metric`
previously raised `NotImplementedError` for derived metrics; `availability`/`performance` are MES
concepts and `quality-metric` is a catch-all (ratios *and* counts). Refs ADR 0001 (deterministic,
no LLM), 0002 (hybrid metric storage; metric layer = formulas), 0004 (lineage), 0013 (OEE =
availability×performance×quality), 0014 (op-tree schema, evaluator deferred), 0015 (resolver).

## Decision A — Formulas compose `MetricRef`s, not bare `ConceptRef`s

A formula input must be a `MetricRef` (a defined direct Metric that carries its own
aggregation + unit), a `Scalar`, or a nested `Formula`. A bare `ConceptRef` cannot become a
scalar without an *implied aggregation* the model doesn't define, so it is **not** a valid
formula input. The `oee` seed changes from `product(ConceptRef…)` to
`product(MetricRef(availability), MetricRef(performance), MetricRef(quality-rate))`.

New direct input Metrics (added to the seed):
- `availability` — concept `availability`, `avg`, unit `ratio`.
- `performance` — concept `performance`, `avg`, unit `ratio`.
- `quality-rate` — concept `quality-metric`, `avg`, unit `ratio`. A **pinned quality ratio**:
  it sidesteps `quality-metric`'s count/ratio overload at the *metric* level without splitting
  the concept now. The overload (which mapped field) is a mapping-time concern, caught by the
  ADR-0012 eval when an MES is onboarded — not silently wrong (per "discover as we onboard").

- *Rejected — keep `ConceptRef` + teach the resolver a default per-concept aggregation:*
  reintroduces the implicit-aggregation ambiguity we rejected for direct metrics, and does not
  resolve the `quality-metric` overload.

## Decision B — Evaluate the op-tree at resolve-time (closed vocabulary, fail-loud)

`resolve_metric(derived)` evaluates the formula: `MetricRef` → resolve the sub-Metric via the
existing direct path; `Scalar` → its value; `Formula` → recurse then combine. Ops:
`product` (∏), `sum` (Σ), `ratio` (binary a/b; divide-by-zero → raise), `scale` (term×scalar),
`weighted_sum` (Σ scalarᵢ·termᵢ). A `ConceptRef` encountered as a formula input raises (enforces
A). No `eval`/`exec`; no LLM (ADR 0001).

## Decision C — Hybrid stored-composite shortcut (ADR 0002)

`DerivedMetric` gains an optional `prefer_stored: str | None` (a direct-Metric id for a
system-supplied composite). `resolve_metric(derived)`: if `prefer_stored` is set **and that
Metric resolves**, return it; **otherwise** evaluate the formula. A new direct `oee-score`
Metric (concept `oee-score`) is added and `oee.prefer_stored = "oee-score"` — the ADR-0002
hybrid: read the stored OEE when a system publishes it, else derive from the three inputs.

## Decision D — Fail loud on unsatisfiable inputs (no fake values)

If any input Metric cannot resolve (concept unmapped / no authority — e.g. no MES maps
`availability`), the derived resolution raises a clear `MetricResolverError` naming the
unresolvable input and cause. No zero/placeholder substitution. (Coverage/empty-state UX is
the card-coverage work.)

## Decision E — Ratio contract; normalization deferred

Input Metrics declare `unit="ratio"` (0–1 expected). `%→ratio` normalization is **not** built
now (no MES onboarded to validate against) and is documented as a mapping/eval concern.
Aggregation is `avg` with a noted caveat: true OEE aggregates components
(Σgood/Σplanned …), not an average of per-row ratios — acceptable for the demo KPI, revisited
if a window/group-by dimension lands. **The group-by dimension landed in
[ADR-0029](0029-dimensional-resolution-and-render-hints.md)** (dimensional resolution); a
per-group component-sum OEE can revisit this caveat there.

## Consequences

- `metric_layer.py`: add the three input Metrics + `oee-score`; change `oee.formula` to
  `MetricRef`s; add `prefer_stored` to `DerivedMetric`; `validate_metric_layer` already checks
  `MetricRef` resolution (no change needed beyond the new ids existing).
- `metric_resolver.py`: replace the derived `NotImplementedError` with the evaluator + the
  `prefer_stored` branch; reuse the direct path for sub-Metrics; propagate sub-resolution
  failures as a clear derived-unavailable error.
- **Live OEE is gated on MES onboarding** (no system maps `availability`/`performance`/
  `quality-metric`/`oee-score` today); this change ships the capability + offline unit tests (mocked
  sub-resolves) + a gated e2e, consistent with the cloud-lane gating.
- Sets up the follow-on work.
