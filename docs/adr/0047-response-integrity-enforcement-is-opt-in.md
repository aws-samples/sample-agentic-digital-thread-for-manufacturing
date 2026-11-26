<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# ADR-0047: Response-integrity enforcement is opt-in (default report-only)

## Status

Accepted — 2026-07-17. Amends [ADR-0046](0046-deterministic-response-evidence-contract.md).

## Context

[ADR-0046](0046-deterministic-response-evidence-contract.md) made Mode A
(`SYSTEM_QUERY_MODE=agent`, the default) enforce a typed-evidence contract:
system agents must emit a `SystemAnswerDraft` structured output that passes
deterministic validation, otherwise their model-authored text is discarded and
replaced by a metadata-only JSON envelope.

On a deployed cross-account demo this regressed the conversational path: the haiku
system agents frequently fail to produce a valid typed envelope, so the
orchestrator receives `{"status":"error","error_type":"incomplete",...}`,
retries repeatedly, and users see spinning progress followed by raw metadata
envelopes rendered into the chat. The demo was effectively unusable in the
default configuration.

## Decision

Enforcement of the ADR-0046 contract is gated by a new environment flag,
**`RESPONSE_INTEGRITY_MODE`**, with a single source of truth in
`agents/config.py` (`response_integrity_mode()` /
`response_integrity_enforced()`):

- **`report-only` (the default — any unset or unrecognized value):** the
  legacy, pre-ADR-0046 answer flow is restored — system agents run without the
  typed structured-output elicitation and their model-authored answers are
  returned as-is; the orchestrator runtime and chat clients use the legacy
  paths. The integrity **capture machinery still runs in shadow** (tool-result
  hooks keep populating invocation state for shadow metrics) — there is no
  gating and no discarding. An agent that produces no final text still falls
  back to the honest metadata-only envelope rather than fabricating.
- **`annotate` (amendment, 2026-07-17 — the recommended production posture):**
  the model-authored answer stays the TRANSPORT; the capture layer becomes an
  AUDIT. After the legacy answer is produced, the server deterministically
  verifies the answer TEXT against the invocation's captured observations
  (`agents/response_integrity_annotate.py`): numeric literals and identifier
  tokens are extracted (question echoes, list markers, and duration prose are
  excluded) and checked against the trusted evidence values. This yields a
  grounding sidecar (`checked`/`verified`/`unverified`/`score`, logged for
  shadow metrics and future UI badges) and ONE hard rule: an answer that
  asserts factual tokens while the turn captured **zero** trusted observations
  is replaced by an honest structured refusal
  (`FABRICATION_BLOCKED` + `NO_CURRENT_INVOCATION_EVIDENCE`). Unverified
  tokens with nonzero evidence only lower the score — models paraphrase and
  derive; that is annotation territory, never blocking territory. Rationale:
  every enforce-mode failure class observed on the deployed demo was the model
  fumbling the *paperwork* of the typed draft (locators, operations,
  value-shape), not fabricating data — so verification must not depend on
  model cooperation, and the audit must never replace the data channel.
- **`enforce`:** the full ADR-0046 behavior — typed `SystemAnswerDraft`
  elicitation, the deterministic evidence gate, pre-SSE enforcement, and the
  metadata-only fallback envelope. Retained as an opt-in strict posture for
  compliance-driven deployments where a thin-but-provable answer is preferred
  over a rich-but-unproven one.

Gated call sites (all consult the one helper):

- `agents/chat_client.py` — `_local_integrity_mode()` and `_cloud_mode_a()`.
- `agents/runtime_app.py` — `_runtime_entrypoint` selection of
  `_invoke_integrity_runtime` vs the legacy entrypoint.
- `agents/system_agent.py` — `_invoke_mode_a_agent` (structured-output
  elicitation and envelope extraction) and the `TYPED EVIDENCE OUTPUT` system
  prompt segment (enforcement-only instructions are omitted in report-only).

The flag must be set **consistently on the app AND the agent runtimes** — the
default-in-code means plain redeploys are consistent without any deploy-script
changes.

## Consequences

- Default deployments trade deterministic evidence gating for a functioning
  conversational answer path: answers are model-authored again, with the
  ADR-0046 capture machinery running in shadow only.
- `annotate` is the recommended posture for demos and production: full-fidelity
  answers, a visible grounding score, and protection against the one
  reputation-damaging failure (invented figures when a source system returned
  nothing). It is deliberately model-cooperation-free — its reliability is
  bounded by the capture layer, not by the model's schema discipline.
- `enforce` remains available for deployments that require provable claims
  end-to-end and accept the answer-fidelity cost. Follow-ups if it is ever to
  become a default: a bounded structured-output elicitation retry, turn-budget
  tuning for tool-heavy connectors, and an orchestrator retry-once policy on
  incomplete envelopes.
- Unit tests that pin enforce-path semantics must opt in explicitly
  (monkeypatch `RESPONSE_INTEGRITY_MODE=enforce`), annotate-path tests set
  `annotate`; the default test posture, like the default deployment, is
  report-only.
