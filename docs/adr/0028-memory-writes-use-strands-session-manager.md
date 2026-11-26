<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# Memory writes use the Strands AgentCore session manager

---
Status: accepted
---

The orchestrator persists conversation turns to AgentCore Memory via the
canonical Strands `AgentCoreMemorySessionManager` rather than a hand-rolled
`create_event` writer. This was gated on a spike (the choice was not made
up front) because the session manager rehydrates prior turns into the agent,
which risked regressing the bounded-context fix (B9) that the
`SlidingWindowConversationManager` exists to provide.

## Why this is recorded

A reader will see two viable integration styles and wonder why we took the
framework path despite the B9 risk. The spike settled it with evidence against
the installed packages (strands-agents 1.42.0, bedrock-agentcore 1.1.1):

- The two managers **coexist** — `Agent` accepts both `session_manager` and
  `conversation_manager`; they register as independent hooks. Mutual exclusion
  only applies to a stateful model, which `BedrockModel` is not.
- Rehydration is **double-bounded**: `initialize()` restores at most ~100 recent
  events (SDK page cap), and `SlidingWindowConversationManager(per_turn=True)`
  trims to `window_size` (20; up to 40 to keep whole turns, see Consequences) on
  a `BeforeModelCallEvent` *before the first model
  call* — so nothing unbounded ever reaches the model or the stream. B9 is not
  regressed.

The session manager is the canonical, lower-code pattern (right thing to show in
a public reference sample) and yields `create_event` writes plus long-term
retrieval injection for free.

## Consequences

- The session manager is attached **only when `has_memory` is True**. With
  `MEMORY_ID` unset, the orchestrator is built without it and chat is unaffected
  (`AgentCoreMemoryConfig` rejects `memory_id=None` anyway). [protects ADR-0027]
- To honor "only interactive chat writes" (see the build's D1 decision), the
  Agent is constructed **without** the session manager when `write_memory` is
  False, since the manager persists every appended turn. Synthetic flows
  (warmup, scheduler, briefings, `/api/ask/sync`, onboard-verify, demo) therefore
  do not write.
- Raw conversation turns persist to a managed store with a **7-day TTL**
  (`eventExpiryDuration`, in days). Documented for the public sample.
- `keep per_turn=True` is load-bearing for the B9 bound and must not be removed.
- **Turn-boundary cut (walk 2026-10-19).** The window is now
  `TurnKeepingSlidingWindow` (`agentic_digital_thread/agents/conversation_window.py`),
  a `SlidingWindowConversationManager` subclass. The stock cut moves to the first
  plain user question past the overflow point, so a follow-up after a turn that
  filled the window lost every earlier tool result before its first model call
  (the answer then had no `[E<k>]` source to cite), and a turn longer than the
  window lost its own question mid-turn. The subclass cuts at the previous or
  current turn's question while the kept messages stay within a hard cap of
  2 × `window_size` (40); past the cap the stock cut applies, so the bound holds.
  Its stored state keeps the stock class name, so chats stored before or after
  the change restore on either image (strands rejects a state whose name differs).
- **LTM-injection shape (hardening fix).** In bedrock-agentcore 1.1.1 the SDK's
  `retrieve_customer_context` hook injected retrieved long-term memory as a
  *trailing assistant message*
  (`role="assistant"`, `<user_context>…`). That leaves the conversation ending on
  an assistant turn, which Bedrock models **without assistant-prefill support**
  reject — `us.anthropic.claude-sonnet-4-6` (our orchestrator model) raises
  `ValidationException: "This model does not support assistant message prefill.
  The conversation must end with a user message."`, crashing *every* turn once an
  actor has any stored memory (i.e. every returning user; new actors are fine
  because nothing is retrieved yet). We wrap the session-manager instance's
  `retrieve_customer_context` to merge the injected context **into the user turn**
  instead, so retrieval still reaches the model but the conversation still ends
  with the user message (model-agnostic). bedrock-agentcore 1.18.1, which the
  sample pins, already puts the retrieved context first in the last user
  message, so the wrapper only strips recalled citation markers from that block
  in place. See `_inject_ltm_into_user_turn` and `_clean_ltm_in_user_turn` in
  `agentic_digital_thread/agents/orchestrator.py`. This was caught by behavioral
  hardening (a returning actor), not the first-touch happy-path E2E.
- **PII redaction on writes (data-handling hardening).** Guardrails protect
  responses, not the persisted store, so the session-manager instance's
  `create_message` is wrapped to redact structured identifiers (SSN, email,
  phone, credit-card-shaped) from the persisted *copy* of each turn before the
  event is written — keeping them out of events and the derived Facts/Summaries
  while leaving the live conversation the model reads untouched. Best-effort and
  deterministic; does not cover names/unstructured PII (use a guardrail NER scrub
  for production). See `_redact_pii` and the README "Memory and PII" note.
