<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# Architecture Decision Records

This directory holds the Architecture Decision Records (ADRs) for the Agentic Digital Thread sample. ADRs are
maintainer design records: each one captures the context, the decision, and the alternatives considered for a
change that constrains later work. They complement the user-facing guides under [`docs/`](../). Some ADRs describe the AWS Workshop Studio packager (`workshop_infra/`) and other
maintainer tooling, which are not part of this sample.

Status values: **Accepted** (in force), **Accepted (amended)** (in force, with a later amendment recorded in
the ADR body), **Proposed** (design recorded, not yet accepted). Numbers 0032 and 0033 were never assigned.

The following table lists every ADR by number.

| Number | Title | Status |
|---|---|---|
| [ADR-0001](0001-deterministic-shared-resolver-endpoints.md) | Deterministic shared resolver endpoints for KPIs | Accepted |
| [ADR-0002](0002-hybrid-metric-storage.md) | Hybrid metric storage: relationships in the graph, formulas in a metric layer | Accepted |
| [ADR-0003](0003-unified-proposal-lifecycle.md) | Unified proposal lifecycle for corrections, with conflict policy | Accepted |
| [ADR-0004](0004-lineage-captured-at-resolve.md) | Lineage captured at resolve-time, at query-provenance depth | Accepted |
| [ADR-0005](0005-re-derivation-on-correction.md) | Re-derivation on correction: lineage-scoped invalidation, pushed to the client | Accepted |
| [ADR-0006](0006-coverage-is-downstream.md) | Dashboard coverage is a downstream concern; onboarding is never blocked by it | Accepted |
| [ADR-0007](0007-concept-system-type-affinity.md) | Concept↔system-type affinity drives mapping recommendations, separate from authority | Accepted |
| [ADR-0008](0008-configurability-deferred-seam.md) | Configurable layers ship as code defaults; admin-editability is a deferred seam | Accepted |
| [ADR-0009](0009-orchestrator-topology-graph-derived.md) | Orchestrator topology is graph-derived, never hardcoded in prompts | Accepted |
| [ADR-0010](0010-agentcore-unsupported-az-self-heal.md) | AgentCore runtimes self-heal around unsupported availability zones | Accepted |
| [ADR-0011](0011-canonical-graph-representations.md) | Canonical graph representations the resolver depends on | Accepted |
| [ADR-0012](0012-mapping-quality-eval.md) | Mapping quality is validated by eval against authored ground truth | Accepted |
| [ADR-0013](0013-oee-decomposition-and-phase1-tracer.md) | OEE is a derived metric over direct-concept inputs; tracer is `inventory-level` | Accepted |
| [ADR-0014](0014-metric-layer-design.md) | Metric layer design: a uniform Metric (direct or derived) with serializable op-tree formulas | Accepted |
| [ADR-0015](0015-deterministic-metric-resolver.md) | Deterministic metric resolver: one `resolve_metric` over an explicit QueryPlan, with graph-sourced runtime-settable authority | Accepted |
| [ADR-0016](0016-derived-metric-resolution.md) | Derived-metric resolution: formulas compose Metrics, evaluated at resolve-time, with an ADR-0002 stored-composite shortcut | Accepted |
| [ADR-0017](0017-resolve-time-lineage-capture.md) | Lineage is an ephemeral record captured on the value, not a persisted store | Accepted |
| [ADR-0018](0018-declarative-card-model.md) | Declarative metric-card model: a `CardLayer` provider, with a future `DashboardLayer` seam | Accepted |
| [ADR-0019](0019-demo-content-is-an-addon.md) | Demo content is an optional addon; the core runs without it | Accepted |
| [ADR-0020](0020-single-source-of-concept-truth.md) | Concept vocabulary has a single source of truth (catalog.py), the graph seed derives from it | Accepted |
| [ADR-0021](0021-two-layer-concept-model.md) | Two-layer concept model: entity (primary/total) + field (earned), graph-sourced, reuse-first | Accepted |
| [ADR-0022](0022-hybrid-local-development-stack.md) | Hybrid local development stack | Accepted |
| [ADR-0023](0023-local-mode-has-runtime-fallback.md) | `has_runtime` falls back instead of failing in local mode | Accepted |
| [ADR-0024](0024-neptune-private-graph-not-reachable-locally.md) | A private Neptune Analytics graph is not reachable from a local machine | Accepted |
| [ADR-0025](0025-local-transactional-data-via-simulation.md) | Local transactional data via the erp_simulation engine | Accepted |
| [ADR-0026](0026-memory-keyed-to-conversation-session.md) | AgentCore Memory is keyed to the Conversation Session, not the Runtime Session | Accepted |
| [ADR-0027](0027-local-mode-uses-cloud-agentcore-memory.md) | Both modes resolve cloud AgentCore Memory by name | Accepted |
| [ADR-0028](0028-memory-writes-use-strands-session-manager.md) | Memory writes use the Strands AgentCore session manager | Accepted |
| [ADR-0029](0029-dimensional-resolution-and-render-hints.md) | Dimensional metric resolution (grouped + LATEST), the sensor heatmap, active-issues, and the `kind` render-hint contract | Accepted |
| [ADR-0030](0030-demo-data-is-opt-in-base-default.md) | Demo data is opt-in; `base` is the default deployment | Accepted |
| [ADR-0031](0031-onboarding-driven-dynamic-connection-management.md) | Onboarding is the single source of truth for a system's connection | Accepted |
| [ADR-0034](0034-evidence-based-concept-suggestion-at-map-time.md) | Evidence-based concept suggestion at map time (value-overlap classifier) | Accepted |
| [ADR-0035](0035-system-agents-own-their-query.md) | System agents own their query composition (agent-owned query mode) | Accepted |
| [ADR-0036](0036-learning-loop-post-turn-elicited-scoped.md) | Learning loop is post-turn, human-elicited, scoped, and lifecycle-managed | Accepted |
| [ADR-0037](0037-sitewise-event-models-richer-discovery.md) | SiteWise event/metric models via richer same-system discovery | Accepted |
| [ADR-0038](0038-system-query-mode-default.md) | System query mode: orchestrator-owned (Mode B) is the default | Accepted (amended) |
| [ADR-0039](0039-bounded-parallel-delegation.md) | Bounded-parallel delegation (concurrency cap in code, not a prompt mandate) | Accepted |
| [ADR-0040](0040-dynamic-entity-and-identity-for-any-system.md) | Dynamic entity + identity resolution for ANY onboarded system | Accepted |
| [ADR-0041](0041-onboarding-time-capture-same-row-and-intra-system.md) | Onboarding-time capture: same-row identifier sets + intra-system join discovery at provision | Accepted |
| [ADR-0042](0042-subject-churn-cooldown-and-quarantine.md) | Subject-level churn control: supersede cooldown, flip-flop quarantine, rejection damping | Accepted |
| [ADR-0043](0043-per-value-to-rule-promotion.md) | Per-value → rule promotion: count of independent confirmations, not confidence | Accepted |
| [ADR-0044](0044-connection-state-on-records-demo-routing-in-demo-layer.md) | Connection state lives on persisted records; demo DB routing lives in the demo layer | Accepted |
| [ADR-0045](0045-multi-site-metric-fan-out.md) | Multi-site metric fan-out: concept-driven, TTL-cache-only, honest by construction | Accepted |
| [ADR-0046](0046-deterministic-response-evidence-contract.md) | Deterministic Mode A response evidence contract | Accepted |
| [ADR-0047](0047-response-integrity-enforcement-is-opt-in.md) | Response-integrity enforcement is opt-in (default report-only) | Accepted |
| [ADR-0048](0048-identifier-miss-reactive-translation-loop.md) | Reactive identifier-miss translation loop (Mode A) | Accepted |
| [ADR-0049](0049-user-identity-propagation-app-signed-claim.md) | User-identity propagation to system agents uses an app-signed claim, not gateway passthrough | Accepted |
| [ADR-0050](0050-vocabulary-and-artifact-management.md) | Vocabulary and deterministic-artifact management — one lifecycle, a fixed customization boundary | Accepted |
| [ADR-0051](0051-insight-finding-grain-graduation-and-site-scope.md) | Insight finding grain, corroboration graduation, and proposal-quality guards | Accepted |
| [ADR-0052](0052-learning-health-gate-evidence-ledger-and-authority-vouching.md) | Learning-health gate, usage-ledger evidence classes, and authority vouching | Accepted |
| [ADR-0053](0053-preflight-drift-tombstones-and-cost-honesty.md) | Onboarding preflight verdicts, schema drift as proposals, rejection tombstones, and cost honesty | Accepted |
| [ADR-0054](0054-measurement-honesty-and-experiment-harness.md) | Measurement honesty (word-boundary taxonomy, fabrication-scoped absence scoring), the variant A/B experiment harness, and per-system business context | Accepted |
| [ADR-0055](0055-bedrock-mantle-models-and-live-model-switching.md) | Bedrock Mantle models, one provider seam, and live model switching | Accepted |
| [ADR-0056](0056-update-agent-runtime-full-replacement-protocol-roundtrip.md) | update_agent_runtime is full-replacement: round-trip protocolConfiguration | Accepted |
| [ADR-0057](0057-semantic-layer-export-boundary.md) | The semantic-layer export boundary — what ADT can hand to a formal ontology, and what it cannot | Proposed |
| [ADR-0058](0058-post-provision-cross-system-mining-and-value-crosswalk-evidence.md) | Post-provision cross-system mining and value-crosswalk evidence | Proposed |
| [ADR-0059](0059-failure-signal-accumulator-and-runtime-steward-questions.md) | Failure-signal accumulator and runtime steward questions | Proposed |
| [ADR-0060](0060-deployment-learnings-generalization-review-lane.md) | Deployment-learnings generalization review lane | Proposed |
| [ADR-0061](0061-evals-admin-tab-and-answer-auditor.md) | Evals admin tab (centralized golden runs) and the deep-context answer auditor | Accepted |
| [ADR-0062](0062-review-queue-promote-to-golden-findings-ledger.md) | Review queue, promote-to-golden, findings ledger, and the bounded triage annotator | Accepted |
| [ADR-0063](0063-proposal-precision-and-requirement-promotion.md) | Proposal precision, requirement promotion, and onboarding schema-shape facts | Accepted |
| [ADR-0064](0064-customer-golden-set-runs-are-upload-per-run.md) | Customer golden-set runs are UPLOAD-PER-RUN — transient, zero server-side persistence | Accepted |
| [ADR-0065](0065-unattended-provisioning-fresh-account-findings.md) | Unattended provisioning in fresh accounts — asset staging, runtime discovery, first-invoke model access | Accepted |
| [ADR-0066](0066-graph-backed-metric-definitions.md) | Graph-backed metric definitions — formulas are data, learned through the one lifecycle, resolved deterministically | Accepted |
| [ADR-0067](0067-concept-evidence-shadow-check.md) | Shadow-only deterministic concept-evidence (label-vs-provenance) check | Accepted |
| [ADR-0068](0068-conversational-onboarding-context.md) | Conversational onboarding context — interview and concept proposals ride the onboarding chat | Accepted (amended) |
| [ADR-0069](0069-runtime-env-parity-contract-enforced.md) | Runtime env parity — the contract carries the graph, roles get one IAM home, and boot fails closed | Accepted |
| [ADR-0070](0070-cognito-authorization-at-edge-default.md) | Cognito Authorization@Edge is the default browser entry | Accepted |
| [ADR-0071](0071-dedicated-gateway-for-quick-query-bridge.md) | Dedicated AgentCore gateway for the Quick query-bridge | Accepted |
| [ADR-0072](0072-external-oidc-identity-provider-option.md) | Optional external OIDC identity provider for the hosted-UI sign-in | Accepted |
| [ADR-0073](0073-condition-monitor-raises-the-maintenance-event.md) | The story mill's maintenance event is raised from its vibration spectrum, not from a downtime | Proposed |
| [ADR-0074](0074-plant-systems-fed-from-the-simulator.md) | The workshop's Indianapolis plant systems are fed from the simulator | Proposed |
| [ADR-0075](0075-predicate-metric-definitions.md) | Predicate-carrying DIRECT metric definitions — a closed `filter` block resolved through concept bindings, withheld as `unresolved_predicate` when a predicate concept is unbound | Accepted |
| [ADR-0076](0076-binding-disambiguation-and-dotted-columns.md) | ONE deterministic concept-to-column binding rule (single → use; 2+ → steward-set `AuthorityRule.field` or WITHHOLD as `ambiguous_binding`) and dotted physical columns rendered as ONE quoted identifier | Accepted |
| [ADR-0077](0077-additive-schema-repair-on-drift-accept.md) | Additive schema drift is REPAIRED on steward accept — `column_added`/`table_added` materialise Field/SchemaEntry nodes through the ONE physical-field writer; destructive drift stays acknowledge-only; onboarding records discovery completeness | Accepted |
| [ADR-0078](0078-defined-metric-terms-answered-by-the-metric-layer.md) | A question that names a DEFINED metric is answered from the metric layer — the turn carries the computed result (or the honest withhold) BEFORE the model runs, and nothing is re-derived from raw columns | Proposed |
| [ADR-0079](0079-quick-gateway-external-oidc-delegated-user-tokens.md) | The Quick gateway accepts delegated (on-behalf-of) user tokens from an external OIDC identity provider | Proposed |
| [ADR-0080](0080-metric-definition-dry-run-preview.md) | A pending metric definition can be DRY-RUN — rendered as the card it would become — before the steward accepts it | Accepted |
| [ADR-0081](0081-vocabulary-drift-field-binding-proposals.md) | Vocabulary drift — a concept imported after onboarding files PENDING field-binding proposals for every onboarded system; no re-onboard | Accepted |
| [ADR-0082](0082-sfc-writes-sitewise-for-unattended-deployments.md) | SFC writes AWS IoT SiteWise for unattended deployments | Proposed |
| [ADR-0083](0083-indianapolis-fourth-site.md) | Indianapolis is a fourth simulated site; CMX-441 telemetry comes from its OPC UA server | Proposed |
