<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# ADR-0069: Runtime env parity — the contract carries the graph, roles get one IAM home, and boot fails closed

## Status

Accepted — 2026-09-11. Builds on [ADR-0056](0056-update-agent-runtime-full-replacement-protocol-roundtrip.md)
(full-replacement runtime updates through one builder), the runtime-env
contract (`agents/runtime_env_contract.py`, an engine invariant), and
[ADR-0053](0053-preflight-drift-tombstones-and-cost-honesty.md) posture
(computed verdicts, honest degrades).

## Context

The `adt_onboarding` AgentCore runtime executes the SAME `ONBOARD_TOOLS` as
the in-process wizard path when a steward uses the onboarding CHAT — and it
has **never been able to read the knowledge graph**. (Later change, ADR-0068
amendment: the runtime now exports only `RUNTIME_TOOLS`; the app runs
`get_catalog`, `provision_system`, `reset_system` and `connect_new_system`
itself. The image still carries their code.) Verified live on a
deployed environment (pre-existing, not introduced by any recent branch):

1. **Env starvation.** `runtime_env_contract._onboarding_env` built a 5-key
   payload WITHOUT `GRAPH_IDENTIFIER`. `agents/db.py::_neptune_execute` then
   fell back to the literal `"adt"`, which Neptune Analytics uses as the
   endpoint **hostname** (`https://adt.<region>.neptune-graph.amazonaws.com`)
   — a non-existent host. Every graph call failed at DNS.
2. **IAM starvation.** Three roles reach the graph, and each spelled its own
   `neptune-graph:*` statement: the ECS app task role
   (`stacks/adt_compute_stack.py`), the shared AgentCore runtime role
   (`stacks/agentcore_stack.py`) — and the onboarding runtime role
   (`stacks/onboarding_stack.py`), which had **no** `neptune-graph` statement
   at all. Even with the env fixed, the calls would have been denied.
3. **The hiding path.** `neptune_query` returns `[]` on failure (a deliberate,
   load-bearing degrade contract shared by many readers), so
   `catalog.hydrate_from_graph` returned 0 and the agent told the steward
   "the catalog is empty / the system is not registered" — false, and
   customer-facing. No runtime validated its env at boot, so nothing ever
   surfaced the real cause.

The tool CODE was DRY (one `ONBOARD_TOOLS` list, one envelope home, one
routing home) but the ENVIRONMENT was not: env keys and IAM statements were
per-consumer hand copies, and the copy for the newest consumer was simply
incomplete. DRY tool code cannot save a runtime whose process env and role
policy are assembled somewhere else, by hand, three times.

## Decision

### 1. `GRAPH_IDENTIFIER` joins the onboarding contract payload

`_onboarding_env` now emits `GRAPH_IDENTIFIER` (from `Sources.graph_id`) — a
6-key payload that is a strict SUPERSET of the legacy deploy.sh §2e `ONB_ENV`
blob. The golden-parity pin was updated honestly: legacy keys/values are
preserved byte-for-byte and the ONLY addition is `GRAPH_IDENTIFIER` (a
byte-equality pin would have enshrined the defect). The key is also
stale-checked (`STALE_CHECKED_KEYS["onboarding"]`), so the existing drift lane
(`scripts/audit_runtime_env.py`) flags a stale value with no script change.
deploy.sh §2e now exports `GRAPH_IDENTIFIER="$GRAPH_ID"` into the provisioning
driver env (the workshop CR already forwarded `GraphIdentifier`).

### 2. One IAM home: `stacks/_graph_access.py::graph_data_plane_statement`

Three tiers, each a flag: read (`ExecuteQuery`, `ReadDataViaQuery`,
`GetGraph`, `ListGraphs`), `write=True` adds `WriteDataViaQuery`
(CREATE/MERGE/SET), `delete=True` adds `DeleteDataViaQuery` (DELETE/DETACH
DELETE; implies write). Neptune Analytics authorizes each openCypher clause
kind against its matching action, so a role gets exactly the tier its code
runs — no more, no less. Resource scope preserves each call site's posture:
the app task role keeps the graph ARN; the runtime roles keep
account/Region-bounded `graph/*` (their stacks have no dependency edge to the
database stack).

Per-role tiers, grounded in the code each role executes:

| Role | Tier | Evidence |
|---|---|---|
| App task role (`adt_compute_stack`) | delete | offboard/reset, replace-not-accrete edge deletes, vocabulary retirement (`admin_routes`, `self_learning`, `graph_snapshot`) — unchanged from before |
| Onboarding runtime (`onboarding_stack`) | delete | `discover`/`store_credentials` reach `connection_resolver._neptune_write` (System-node `secret_arn` upsert); explore tools read — **new; previously NO grant**. The delete tier was granted for `reset_system`'s `DETACH DELETE`. Later change (ADR-0068 amendment): `reset_system` runs in the app, so the tier is kept until a follow-up checks whether any `RUNTIME_TOOLS` path still deletes. Later change (ADR-0068 amendment 2026-10-21): `store_credentials` runs in the app too, and a static call inventory of the eight exported tools found no graph write or delete, so the role now has the read tier only |
| Orchestrator/system runtime (`agentcore_stack`) | write | `log_proposed_mapping` / `record_rule_usage` CREATE/MERGE/SET only; accept/replace deletes run in the app process — unchanged posture (gains only the two describe actions the read tier carries) |

The orchestrator/system runtime role deliberately does NOT gain
`DeleteDataViaQuery`: least privilege is the point of one helper with explicit
tiers, and a role that never deletes must not be able to.

### 3. Boot fails CLOSED: `missing_required(role, environ)`

The contract grows one pure, stdlib-only validator: the required set IS
`STALE_CHECKED_KEYS[role]` — the deterministic CFN-derived values every cloud
deployment provisions (`ONBOARDING_PROVISIONER_SFN_ARN` included: it comes
from the always-on engine stack, so nothing in the set is optional in cloud;
live-swap keys are deliberately NOT required). Entry points call it in cloud
mode and RAISE naming the missing keys: `create_onboarding_server` at
construction, and `runtime_app`'s `__main__` dispatch for the
orchestrator/system/digital-thread modes. Local/DEV_MODE
(`config.local_mode_env`, the pre-existing ADR-0023 gate, now module-level)
never raises — the local env is intentionally minimal.

### 4. `neptune_query`'s return-`[]`-on-failure contract is deliberately UNCHANGED

That degrade path is load-bearing across many readers (dashboards, learning
hooks, catalog fallbacks) where "graph briefly unavailable" must not fail a
turn. The defect was not the degrade — it was that a PERMANENTLY
misconfigured runtime could hide behind it forever. The boot check removes
the hiding path at the only moment the distinction is knowable (a missing
key at startup is configuration, not weather), so the runtime dies loudly in
CloudWatch instead of answering falsely.

## Consequences

- **Deployed environments need a `deploy.sh` re-run.** The contract change
  reaches the onboarding runtime through §2e's full-replacement env update,
  and the IAM change through `cdk deploy` of the onboarding/agentcore/compute
  stacks — both of which `deploy.sh` performs. Until then the runtime keeps
  its old env; after the image update lands WITHOUT the env/IAM update, the
  boot check will fail the onboarding runtime closed — which is the designed
  behavior (visible, actionable) rather than a regression.
- Golden pins changed shape: onboarding parity is now
  "legacy-superset + exactly `{GRAPH_IDENTIFIER}`" instead of byte equality.
- Any future graph-touching role gets its statement from
  `stacks/_graph_access.py` and its required keys from the contract; a new
  runtime role that skips either now fails a pinned test or fails closed at
  boot, instead of shipping a silent husk.

## Follow-up 3: discover dispatch parity (2026-09-11)

The env/IAM starvation above had a sibling defect in the TOOL layer: the same
`ONBOARD_TOOLS.discover` had TWO dispatchers with different capabilities. The
onboarding CHAT (the `adt_onboarding` runtime) routes through
`onboard_agent._discover_backend_result`, which supported PostgreSQL,
SiteWise (i3X), REST API, and MCP Server; the WIZARD routes through
`onboard.py::_onboard_discover_base`, which additionally implemented
'Athena' and 'SiteWise (Direct)' — the two connection types the deployed
customer environment actually uses. REST/MCP had been DRY-ed into
`onboard_agent` backends consumed by both; Athena and SiteWise (Direct) never
were, so the chat path answered "discover does not yet support the Athena
connection type in this environment" for an onboarded Athena system (verified
live, pre-existing).

Fix (replace-not-accrete): the ONE implementation per connection type now
lives in `onboard_agent` — `_discover_athena_schema` and
`_discover_sitewise_direct_model` (decomposed into ≤50-line helpers), called
by `_discover_backend_result` for the runtime and by `onboard.py`'s wizard
branches, which are now THIN wrappers that add only what is inherently
wizard-scoped: deriving the config from the HTTP body and the app-only
catalog persistence (`_persist_catalog_config_from_body`). Result and error
shapes are unchanged on both paths (`{"error": str(AthenaConfigError)}`,
`{"error": "Athena discovery failed: ..."}`, the SiteWise static-table
fail-soft). The dispatch sets are pinned equal — including 'Athena' and
'SiteWise (Direct)' — by `tests/unit/test_discover_dispatch_parity.py`.

## Follow-up 4: cross-account AssumeRole parity (2026-09-11, measured live)

With graph access, catalog hydration and discover dispatch fixed, the chat
path's Athena discovery on a cross-account system failed one hop later:
`sts:AssumeRole` on the data account's read-only role was **denied for the
onboarding runtime role**. The caller-side grant existed — hand-rolled twice,
in `adt_compute_stack.py` (app task role) and `agentcore_stack.py` (query
runtime role) — and the onboarding runtime role had none, because until
ADR-0068 it never ran data-plane discovery. Same defect class as §2.

Fix: `stacks/_cross_account.py::grant_cross_account_assume(stack, role)` is
the ONE caller-side grant (reads the same `crossAccountDataRoleArn` context,
no-op when unset, carries the convention-mode cdk-nag suppression); all three
roles call it. Pinned by `tests/test_cross_account_grant.py::
test_all_three_assuming_roles_carry_the_same_grant`. The far-side trust
policy (account-pinned + `sts:ExternalId`) is unchanged and remains the real
gate.

Rule of thumb this ADR now states outright: **any grant a runtime role needs
to run a tool the app process also runs must come from a shared helper the
app task role also uses.** A hand-rolled grant in one stack is the bug.

## Update 2026-10-29

`ONBOARDING_PROVISIONER_SFN_ARN` is no longer part of the contract: the Step Functions provisioner it pointed at had no caller and was removed. The onboarding runtime's required set is `ONBOARDING_SESSIONS_TABLE`, `AWS_REGION` and `GRAPH_IDENTIFIER`. A runtime deployed before this change keeps the stale key until its next full-replacement env write; the drift lane reports it as foreign, which is informational only.
