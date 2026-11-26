<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# AgentCore Memory is keyed to the Conversation Session, not the Runtime Session

---
Status: accepted
---

AgentCore Memory binds to a `(actor_id, session_id)` pair. We set `actor_id` to
the authenticated user (Cognito subject in cloud, the configured dev user
locally) and `session_id` to the **caller-provided Conversation Session** from
the request payload — deliberately **not** the AgentCore Runtime Session
(`context.session_id`).

## Why this is recorded

The cloud path mints a fresh `runtimeSessionId` on every invocation on purpose,
to avoid stale/busy-session `IncompleteRead` failures (see
`AgentCoreChatClient.stream`). A future reader will see memory bound to the
payload `sessionId` instead of the obvious `context.session_id` and may try to
"fix" it — which would scatter every turn into a single-message session, making
the Events and Summaries views incoherent. The decoupling is intentional: the
Runtime Session is a transport mechanic; the Conversation Session is the durable
identity memory accrues to. See [the glossary](../glossary.md).

## Consequences

- Facts and preferences (namespaced by `actorId` only) accumulate across all of
  a user's turns regardless of runtime-session churn.
- Events and summaries (namespaced with `sessionId`) cohere only while the
  caller holds a stable Conversation Session — which the chat UI does, and which
  demo/briefing flows intentionally do not (one throwaway session per question).
