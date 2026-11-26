<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# Bounded-parallel delegation (concurrency cap in code, not a prompt mandate)

---
Status: accepted
---

> Env knob: `ORCHESTRATOR_DELEGATION_CONCURRENCY` (integer ≥ 1, **default 3**;
> value **1 == sequential**). Enforced by a per-turn `asyncio.Semaphore` around the
> gateway MCP client's `call_tool_async` in
> `agents/orchestrator.py::_wrap_gateway_client_with_retry`. Pairs with
> [ADR-0035](0035-system-agents-own-their-query.md) (system agents own their query)
> and [ADR-0038](0038-system-query-mode-default.md) (mode default).

## Context

The orchestrator system prompt previously **mandated sequential delegation** —
"call ONE `query_<system>` tool, WAIT for its result, then call the NEXT" — and
justified it by claiming that *"issuing concurrent or rapid-fire calls to multiple
runtimes at once causes 'No endpoint or agent found' / endpoint-not-found errors and
the retry storms that follow."*

That rationale was **false**. The "No endpoint or agent found" churn was a
**stale-gateway-target bug**: the query gateway's targets could drift out of sync
with the live per-system runtimes, so a call routed to a stale target failed with an
endpoint error regardless of concurrency. That bug is now fixed independently —
`deploy.sh` step `[3b]` **auto-reconciles the query-gateway targets** on every deploy
— so the transient error is no longer a function of how many systems are called at
once. The sequential mandate was therefore needlessly serializing multi-system
latency (a multi-system question paid the full per-system runtime latency in series).

An audit of the prompt also found the mandate had accreted a hardcoded special-case —
*"consistent with the SiteWise 'sequential traversal required' note — sequential is
the rule for every system"* — baking one (not-even-onboarded) system's ordering
assumption into the generic orchestrator.

## Decision

**Allow bounded-parallel delegation.** The prompt now permits the model to delegate
to all relevant systems in the same step (still **at most one call per system**), with
no cross-system ordering requirement unless a specific tool/system documents one in
its own guidance. The hardcoded SiteWise/`connection_type` sequential special-case is
**removed** from the orchestrator entirely — a real ordering constraint must be
expressed by the tool/system's own contract, not by the generic orchestrator.

**The real cap lives in code, not the prompt.** Strands' default
`ConcurrentToolExecutor` genuinely runs each `tool_use` block the model emits as its
own `asyncio` task, all sharing the single per-request gateway `MCPClient`. A prompt
rule cannot enforce a ceiling on that. So the cap is a per-turn `asyncio.Semaphore`
created inside `_wrap_gateway_client_with_retry` (one per gateway client, i.e. one per
request/turn) and acquired around each underlying `call_tool_async`. It is sized by
`ORCHESTRATOR_DELEGATION_CONCURRENCY` (default 3; value 1 == sequential).

**The transient-retry wrapper is preserved.** Only TRANSIENT errors are retried, and
on exhaustion the original error result is returned unchanged so the honest-unavailable
contract still applies (no fabrication). The semaphore is **released during the retry
backoff `asyncio.sleep`** so a slow-retrying system does not hold a concurrency slot
away from its peers.

## Why code, not prompt

- A prompt "delegate sequentially" instruction is unenforceable given
  `ConcurrentToolExecutor`; the model can (and does) emit multiple `tool_use` blocks
  that fire concurrently regardless of prose.
- A semaphore at the single gateway-client choke point is the only hard cap, and it
  binds cleanly to the running event loop (constructed synchronously, acquired via
  `async with`), avoiding cross-event-loop problems.

## Consequences / rollback

- **Rollback without a code change:** set `ORCHESTRATOR_DELEGATION_CONCURRENCY=1` to
  restore fully sequential delegation.
- The knob is threaded into the orchestrator runtime env via `deploy.sh` (`ORCH_ENV`)
  and the app task-def, following the same pattern as `SYSTEM_QUERY_MODE` /
  `BEDROCK_MODEL`. Default (unset) = 3.
- Multi-system questions should complete faster (delegations overlap up to the cap)
  without reintroducing the endpoint churn (that was the stale-target bug, now
  reconciled at deploy).

## Re-measure procedure

Run
`scripts/smoke_prompts.py --report` twice — once with
`ORCHESTRATOR_DELEGATION_CONCURRENCY=1` (sequential baseline →
`docs/perf/smoke-seq.json`) and once with the default 3 (parallel →
`docs/perf/smoke-par.json`) — and compare `avg_latency_seconds` /
`max_latency_seconds` plus the per-question error taxonomy (incl.
`RuntimeUnavailable`) and fabrication-fallback flags. The reports are working-tree
artifacts for review with real data; they are not committed.
