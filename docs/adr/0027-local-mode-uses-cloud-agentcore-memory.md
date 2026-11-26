<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# Both modes resolve cloud AgentCore Memory by name

> **Historical naming.** The project and its demo seed database were renamed
> to `agentic_digital_thread` (ADT) and `adt_demo` after this record was
> written; identifiers in the historical text may reflect the old names.
> Current equivalents: SSM parameters and secrets use the `adt` prefix
> (`/adt/*`, `adt/conn/<id>`). The decision itself is unaffected.


---
Status: accepted
---

Unlike the Neptune knowledge graph, which is private and unreachable from a
developer machine (see [ADR-0024](0024-neptune-private-graph-not-reachable-locally.md)),
AgentCore Memory is a managed AWS endpoint reachable with credentials from
anywhere — exactly like Bedrock, which local mode already calls. So local mode
reads and writes the same cloud Memory resource rather than running without
memory.

Both the ECS app and the orchestrator runtime now resolve that resource the
same way: by **name** via the control plane (`config._discover_memory_id`
filters `ListMemories` to the exact stack-name prefix
`adt_memory`). The `MEMORY_ID` env var remains an optional
explicit override — when set it is used verbatim and no discovery call is made.
There is no longer a cross-stack `MEMORY_ID` export/import wiring the resource
id from the AgentCore stack into the compute stack.

## Why this is recorded

The local-dev ADRs (0023, 0024) establish that local mode bypasses the
runtime/gateway layer and cannot reach Neptune. A reader could reasonably assume
Memory is likewise unavailable locally and skip wiring it. It is not — Memory
sits outside the bypassed runtime layer, so the in-process local orchestrator
talks to it directly.

The second question a reader will have is why cloud mode discovers by name
instead of consuming a cross-stack CloudFormation export, which would be the
obvious wiring. The reason is evolvability: changing the Memory's strategies
(for example, the language-aware custom strategies — see
[ADR-0028](0028-memory-writes-use-strands-session-manager.md)) requires
**replacing** the `CfnMemory` resource, and a CloudFormation export cannot be
changed or removed while an importing stack still references it — the two stacks
deadlock. Name-based resolution removes that coupling so the Memory can be
replaced freely, and as a bonus makes local and cloud resolve identically.

## Consequences

- `MEMORY_ID` is an optional override, not the source of truth. When unset (the
  default in both modes now), the app and runtime discover the resource by name.
  If none is found, `has_memory` is `False` and the memory views stay empty
  rather than crashing.
- Trade-off: a control-plane `ListMemories` call at startup (cached per process,
  via `get_config`), account-wide `ListMemories` IAM on the runtime/task role,
  and **fail-loud** behavior — more than one ACTIVE match logs an ERROR listing
  the ids and returns `None` rather than silently guessing.
- Benefit: no brittle cross-stack-export-replace deadlock, so the Memory's
  strategies can be evolved without a stack-ordering dance.
- Local and cloud records separate naturally by `actor_id` (local uses the dev
  user; cloud uses Cognito subjects), so a shared resource does not mix data.
