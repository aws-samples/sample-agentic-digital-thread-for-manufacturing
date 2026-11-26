<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# System query mode: orchestrator-owned (Mode B) is the default

> **Historical naming.** The project and its demo seed database were renamed
> to `agentic_digital_thread` (ADT) and `adt_demo` after this record was
> written; identifiers in the historical text may reflect the old names.
> Current equivalents: SSM parameters and secrets use the `adt` prefix
> (`/adt/*`, `adt/conn/<id>`). The decision itself is unaffected.


---
Status: amended 2026-07-14 — the default is now Mode A (`agent`)
---

> **Amendment (2026-07-14).** The shipped default flipped to **Mode A
> (`SYSTEM_QUERY_MODE=agent`)**. Rationale: Mode A was proven live in a deployed
> environment (cross-account Athena + SiteWise systems, cross-system
> verification green), per-system agents self-correct their own SQL against
> the single schema they know, and every app-side code default already read
> `agent` — only the infra layer pinned `orchestrator`, an inconsistency that
> caused a silent mode revert on deploy (2026-07-14, fixed by the sticky SSM
> param + this flip). Mode B remains fully supported via
> `deploy.sh --query-mode orchestrator` / `-c system_query_mode=orchestrator`;
> the sticky SSM parameter `/adt/system-query-mode` preserves an
> env's choice across deploys. The Context/Decision sections below record the
> original 2026 decision and remain for history.

> Toggle: `SYSTEM_QUERY_MODE` ∈ {`agent` (Mode A, default), `orchestrator` (Mode B)},
> settable per deploy via `deploy.sh --query-mode {orchestrator|agent}` and the CDK
> context `-c system_query_mode=...` (wired through `ORCH_ENV` and the
> `vf_compute_stack` app task-def env). Pairs with
> [ADR-0035](0035-system-agents-own-their-query.md) (system agents own their query)
> and [ADR-0037](0037-sitewise-event-models-richer-discovery.md).

## Context

ADT can compose cross-system SQL in one of two ways:

- **Mode A — agent-owned query (`SYSTEM_QUERY_MODE=agent`).** The orchestrator
  delegates each system's data pull to that system's per-system agent (Amazon Nova
  Lite). Each agent composes and self-corrects its own SQL against the one schema it
  owns, and returns rows. Retries happen *inside* each agent and are not visible at
  the orchestrator's tool boundary.
- **Mode B — orchestrator-owned query (`SYSTEM_QUERY_MODE=orchestrator`).** A single
  orchestrator (Anthropic Claude) composes SQL for every system directly and calls a
  generic `run_query` tool per system, reading the graph-injected schema.

We ran both modes head-to-head against the live cross-account data (a single
144-column nested Athena table plus the SiteWise event/metric models) over the same
7-question suite, across five deploy iterations that tried to drive Mode B's visible
error count down (schema cache, self-correction, schema-with-routing, guaranteed
schema inject).

**Findings (data-backed, honest):**

- **Mode B is ~3× faster** end-to-end (~10 min / 7Q vs ~30 min for Mode A), with
  **correct answers and zero fabrication** in both modes.
- **Mode B shows ~17–25 visible, self-recovered tool-call retries** per suite
  (`COLUMN_NOT_FOUND` / `QueryError`) because one LLM composes SQL against a hard
  144-column nested table (and a SiteWise property-read verb) without the per-schema
  focus a dedicated agent has. None of the five iterations eliminated these — they are
  an inherent cost of one model juggling every schema.
- **Mode A's "0 errors" is largely presentational:** its per-system agents make the
  *same* fumbles and retry internally, below the orchestrator's telemetry. It is not
  that Mode A avoids the retries — it hides them.
- The retries never reach the user's answer (they self-recover), and as of the trace
  work they are also suppressed from the investigation trace graph (a transient
  `COLUMN_NOT_FOUND` no longer renders as a "No data" finding).

So the choice is a genuine trade-off, not a correctness question: **speed with visible
self-recovery (B)** vs **slower with clean per-system telemetry (A)**.

## Decision

**Default to Mode B (`SYSTEM_QUERY_MODE=orchestrator`).** Keep Mode A as a
first-class, one-flag toggle.

Rationale:

- **Speed and correctness win for the primary use.** A conversational demo lives or
  dies on responsiveness; ~3× faster with correct, non-fabricated answers is the
  better default experience.
- **The retries are self-recovering and no longer user-visible** (answer is correct;
  trace graph hides the transient failures), so their main downside — looking broken —
  is addressed at the presentation layer rather than by taking the slower mode.
- **Honesty about telemetry.** We explicitly reject choosing Mode A to manufacture a
  "0 errors" number, because Mode A does not actually avoid the retries — it relocates
  them out of view. Picking B keeps the trade-off honest and the telemetry truthful.
- **Mode A stays available** for demos or environments that specifically want clean
  per-system tool telemetry (e.g., a talking point about agent-per-system isolation):
  `./deploy.sh --query-mode agent`.

## Consequences

- The shipped default is Mode B; the live demo env runs Mode B.
- Operators can switch modes per deploy with a single flag; no code change. The flag
  is wired end-to-end (deploy.sh → CDK context → `ORCH_ENV` + app task-def env).
- Mode B's self-recovered retries remain in the orchestrator runtime logs (useful for
  debugging) but are kept out of the user-facing investigation trace graph.
- If a future change gives the orchestrator per-schema focus (e.g., schema-scoped
  sub-prompts) that measurably cuts the retry count without losing Mode B's speed, the
  trade-off should be re-measured — but that does not change this default.
- This ADR records a default, not a lock-in: the A/B capability itself
  (per [ADR-0035](0035-system-agents-own-their-query.md)) is retained.
