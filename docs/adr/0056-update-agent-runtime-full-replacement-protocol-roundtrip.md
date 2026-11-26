<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# ADR-0056 — update_agent_runtime is full-replacement: round-trip protocolConfiguration

Status: Accepted (2026-07-28)
Related: ADR-0054 (business-context refresh — the first live path to trip
this), ADR-0055 (live model switching — every system agent would have been
bricked the same way)

## Context — the defect, root-caused live (2026-07-28)

`update_agent_runtime` is a **full-replacement** API: any field omitted from
the call is reset to its service default. Every ADT update path omitted
`protocolConfiguration`, so an MCP runtime silently reverted to the default
`HTTP` server protocol on its next update.

The failure is nasty because nothing looks broken:

* The container starts and reports healthy (uvicorn up in ~0.6 s), and the
  runtime reports `status: READY` — the config is *valid*, just *wrong*.
* The platform now treats an MCP streamable-HTTP server as an HTTP agent and
  **never forwards the MCP payload**: MCP `initialize` hangs until the client
  times out (`http=000` after 75 s direct; the orchestrator's 40 s session
  init loses 3/3 retries and reports the system unavailable).
* Runtime logs show **zero inbound HTTP requests**, so it reads as cold-start
  churn and sends the debugger toward latency, capacity, and AZs.
* It is persistent, not transient — it survives until the runtime is updated
  again with the field restored.

**The hypothesis that proved correct** (recorded so the next debugger does not
re-derive it): compare `get_agent_runtime` between a working and a hung
runtime. `adt_digital_thread` (never business-context-rolled) had
`protocolConfiguration: {"serverProtocol": "MCP"}` and answered `initialize`
with HTTP 200 in 1 s. `adt_lake_athena` and `adt_telem_sitewise` — the two
runtimes rolled by `refresh_business_context` (ADR-0054) — had the field
**missing** and both hung, on the same VPC, subnet, security group, image,
gateway, and token. Restoring the field on both fixed them immediately.

Severity: ADR-0055's live model swap (`refresh_agent_model` →
`_bump_runtime_version`) used the same omitted-field payload, so a fleet-wide
model swap would have bricked **every** system-agent runtime at once. The
"BOTH model roles swap LIVE" invariant was unsafe until this fix.

**Confirmed, not hypothetical (2026-07-29):** the model-switch path DID trigger
this defect in the field before the fix landed — a steward-initiated
`refresh_agent_model` fleet swap downgraded the system runtimes to HTTP and
produced the same READY-but-hung signature, **voiding a Sonnet 5 7/11 golden
benchmark** taken on the sick fleet (re-measured healthy: Sonnet 5 used FEWER
tool calls, not more). Two independent triggers of the same root cause
(business-context roll, model swap) are why the fix lives in the ONE shared
`runtime_update_kwargs` builder rather than at either call site — and why every
model comparison is gated on a same-session `wasted_calls == 0` baseline.

## Decision

1. **Round-trip, never hardcode.** Every update reads `protocolConfiguration`
   from the `get_agent_runtime` details and passes it through when present
   (key omitted when absent — no `None`/empty echo). Hardcoding `MCP` would be
   wrong: the orchestrator runtime is legitimately an HTTP agent.
2. **One shared kwargs builder.**
   `agents/runtime_provision_helpers.py::runtime_update_kwargs` is the ONE
   place update payloads are built. It also keeps the pre-existing behaviors:
   the immutable `requireServiceS3Endpoint` network key is dropped, and
   `authorizerConfiguration` is passed only when a `customJWTAuthorizer` is
   set (tagged union). Keyword overrides (`artifact`, `role_arn`,
   `authorizer_config`) serve the `_start_cloud` reuse path, which sends a
   fresh image/role/authorizer.
3. **All update sites route through it:**
   * `agent_manager._refresh_existing_runtime_image` (the `_start_cloud`
     reuse/redeploy path),
   * `agent_manager._resync_force_runtime_pickup` (connection resync),
   * `agent_manager._bump_runtime_version` (business-context refresh ADR-0054
     **and** live agent-model swap ADR-0055),
   * `admin_routes._update_orchestrator_runtime_model` (orchestrator model),
   * `admin_routes.restore_runtime_auth` (authorizer repair — this one
     previously built its payload from a partial `list_agent_runtimes` entry,
     which under full-replacement semantics also wiped the environment
     variables; it now reads `get_agent_runtime` details first),
   * `scripts/_query_mode_lib.build_runtime_update_kwargs` (query-mode CLI;
     kept standalone — the scripts lib does not import the app package — with
     the same three-line round-trip).
4. **Regression pins.**
   `tests/unit/test_runtime_update_protocol_roundtrip.py` asserts, per update
   path, that MCP details round-trip unchanged, that HTTP details round-trip
   as HTTP (proving no hardcode), that an absent field stays omitted, and that
   the S3-endpoint drop and tagged-union authorizer behaviors are unchanged.

## Consequences

* A business-context write or a live model swap deploys a new runtime version
  that keeps speaking MCP. The ADR-0055 live-swap invariant is safe again.
* Diagnostic signature worth remembering: MCP `initialize` hangs on a READY
  runtime with **no inbound request logs** ⇒ check `protocolConfiguration`
  first (`aws bedrock-agentcore-control get-agent-runtime`).
* `deploy.sh`'s orchestrator update also omits `--protocol-configuration`;
  that is benign today only because the orchestrator's correct protocol equals
  the service default (HTTP). The system-runtime repair block in `deploy.sh`
  already passes `{"serverProtocol":"MCP"}` explicitly.
* The create paths were never affected — both `agent_manager` and the
  provisioner set `{"serverProtocol": "MCP"}` at create time.
