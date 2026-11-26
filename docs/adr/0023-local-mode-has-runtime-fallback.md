<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# `has_runtime` falls back instead of failing in local mode

---
Status: accepted
---

`AppConfig.has_runtime` previously *raised* `RuntimeError` whenever no AgentCore
runtime ARN was configured. But its three call sites (`chat_client.get_chat_client`,
the prompt scheduler, and `/api/ask`) are all written as
`if cfg.has_runtime: <cloud> else: <in-process orchestrator>` — so the raise made
the local fallback branch unreachable and crashed the first chat query in local
mode. We changed `has_runtime` to return `False` (use the in-process orchestrator)
when `AGENT_MODE=local` or `DEV_MODE` is set, while **still raising in cloud mode**
when an ARN is genuinely expected but missing.

## Why this is recorded

A future reader sees a deliberate `raise` ("No runtime ARN = fatal") and might
re-add it. The raise was correct for cloud deployments but wrong once local mode
became a supported path. The guard is preserved for cloud — it is only relaxed
when the operator explicitly opts into local development.

## Consequences

- Local development (`AGENT_MODE=local`) uses the in-process Strands orchestrator
  calling Bedrock directly; the AgentCore runtime/gateway layer is bypassed.
- Cloud deployments retain fail-fast behavior: a missing runtime ARN without
  `AGENT_MODE=local`/`DEV_MODE` still raises at the call site.
