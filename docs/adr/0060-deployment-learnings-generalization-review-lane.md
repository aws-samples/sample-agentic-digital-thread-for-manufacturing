<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# ADR-0060: Deployment-learnings generalization review lane

## Status

**Proposed — 2026-08-07.** Written before implementation to pin the contract;
the lane ships in the same sweep behind this decision record.

Builds on [ADR-0044](0044-connection-state-on-records-demo-routing-in-demo-layer.md)
(engine neutrality — nothing customer-specific may enter engine defaults),
[ADR-0053](0053-preflight-drift-tombstones-and-cost-honesty.md) (measurement
honesty; computed verdicts — an LLM never produces one), and
[ADR-0057](0057-semantic-layer-export-boundary.md) (the export boundary — this
report is explicitly NOT the semantic-layer export). It generalizes an
observed precedent: a query-guidance rule learned in one deployment (an engine
dialect behavior, not a customer fact) was lifted by a **human pull request**
into the engine's un-displaceable prompt, where it now benefits every
deployment.

## Context

A matured deployment accumulates two kinds of learned knowledge, and today
nothing separates them:

- **Customer-specific** — crosswalks between one customer's identifier
  vocabularies, site names, business context. This must never leave the
  deployment (neutrality, and the trust boundary).
- **Dialect-generic** — "this connector type needs temporal literals quoted
  this way", "this engine paginates like so". This is engine knowledge that
  happened to be discovered in the field, and every deployment that lacks it
  will re-learn it the hard way — or answer wrongly until someone notices.

The lifting path exists (a human PR, with a test, past the neutrality ratchet)
but *finding* the candidates means a maintainer manually reading accepted
proposals and promoted guidance across deployments. That review has happened
exactly once, by accident, when a maintainer recognized a learned rule as
generic. The product should make the review systematic without making the
promotion automatic.

## Decision

### D1 — A script, not a service

One CLI in `agentic_digital_thread/scripts/` (the `graph_snapshot.py`
conventions: stdlib argparse, import-pure module top level, loadable
standalone). No runtime component, no scheduler, **no auto-promotion of any
kind** — the output is a markdown report a human reads before PRing anything.

Rejected: a runtime surface (admin tab, periodic sweep). The consumer is an
engine maintainer, not a deployment steward; the cadence is release-time, not
continuous; and a runtime component would need auth, cost, and flood answers
this use case does not justify.

### D2 — Inputs are read-only projections of the learned set

- Accepted proposals filtered to steward-confirmed methods (the set the
  orchestrator already privileges as applied truth);
- promoted query guidance, split through the single-source `LEARNED:` note
  parser;
- business context per system — hash and length only by default, with an
  explicit `--include-text` opt-in, because business context is untrusted
  steward prose (ADR-0054) and copying it into a report that leaves the
  deployment would launder it across a trust boundary.

Source selection: `--snapshot <file>` (offline, against a graph-snapshot
export) first — it needs zero new API; `--base-url` (live API mode, the
existing auth pattern) as the follow-on.

### D3 — Classification is computed heuristics; the LLM may suggest, never verdict

Pure, unit-tested heuristics classify each artifact:

- **customer-specific markers** — onboarded system ids, sampled values, site
  literals, quoted value pairs — derived at runtime *from the deployment being
  analyzed*, never from a hardcoded list (a hardcoded marker list would itself
  be a neutrality violation);
- **dialect-generic markers** — SQL/engine vocabulary (temporal literals,
  quote styles, pagination, retention) and phrasing that names a connector
  TYPE rather than an instance;
- anything else falls to **unclear** — never silently to generic.

An optional `--llm-assist` flag adds a suggestion column
(`customer-specific | dialect-generic | unclear`) rendered beside the computed
verdict, which is always shown; the report states in its header that the human
decides. The flag sends note TEXT (guidance prose), never sampled production
values, and the model import happens only when the flag is passed. This is the
golden-lane discipline applied to a review aid: the LLM may label, but the
label gates nothing.

Rejected: LLM-primary classification with heuristic fallback. It inverts the
honesty posture — the reproducible verdict must be the one the report leads
with, or two runs of the same review disagree for no inspectable reason.

### D4 — Output is a checklist-bearing report, and promotion stays a human PR

The report (`generalization-review-<date>.md`) renders one table per artifact
class — text (or hash), source system, heuristic verdict with matched markers,
optional LLM suggestion, and a blank "candidate engine home" column for the
human — and ends with the neutrality reminder and the promotion checklist from
the observed precedent: write the test first, run the ratchet, open the PR.
Nothing in the lane writes to any deployment, any graph, or the engine.

Rejected: auto-generating the engine PR for dialect-generic hits. Even a
correct classification does not decide *where* the knowledge belongs (a
connector-type default, prompt text, or a code path), and an auto-PR would
put a machine verdict one approval click away from every deployment's prompt.

## Considered alternatives

- **Do nothing; rely on maintainers noticing.** Rejected: the one observed
  promotion was accidental, and the snapshot-diff evidence shows the cost of
  unpromoted generic knowledge recurs per deployment.
- **Cross-deployment telemetry aggregation** (phone-home of learned rules for
  central review). Rejected outright: learned rules contain customer
  identifiers; this sample makes no outbound transmission of learned content,
  and a review lane must not introduce one. The report is generated by the
  operator, inside the deployment boundary, and leaves only by their hand.
- **Fold this into the ADR-0057 export.** Rejected: the export is a
  customer-facing projection of applied truth with a compatibility surface;
  this is an internal maintainer aid whose format can change freely. Sharing a
  seam would couple their lifecycles for no gain.

## Consequences

- **Positive:** the engine gains a systematic intake for field-earned generic
  knowledge with the neutrality ratchet as the hard gate; the review becomes
  repeatable and its verdicts inspectable (matched markers are shown per row).
- **Negative / risks:** heuristics will misclassify borderline prose (mitigated
  by the unclear bucket and the human-decides framing); the lane's value
  depends on someone actually running it at release time — it is deliberately
  not wired to any automation that could force the issue.
- **Verification:** classification table tests (generic dialect note ⇒
  dialect-generic; a note quoting a system id or sampled value ⇒
  customer-specific; ambiguous ⇒ unclear, never generic); the marker set is
  proven derived from input, not hardcoded; report rendering shows both verdict
  columns; without `--llm-assist` the module imports zero model code (pinned by
  an import-purity test). Acceptance uses a real matured-deployment snapshot
  kept outside the repo (the adjacent-folder convention): the known promoted
  dialect rule must classify dialect-generic and the site-specific crosswalk
  notes customer-specific.
