<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# AgentCore runtimes self-heal around unsupported availability zones

---
Status: accepted
---

Per-system agent runtimes are created on **Bedrock AgentCore**, which only launches VPC
runtimes in the availability zones the service supports. The shared VPC (`ADTNetworkStack`,
`max_azs=2`) is *not* guaranteed to land in supported AZs — AZ **names** (`us-east-1a`) map
to account-specific AZ **IDs** (`use1-az*`), and for this demo account `us-east-1a` is
`use1-az6`, which AgentCore does not support (it allows `use1-az1`, `use1-az2`, `use1-az4`).

When `agent_manager` hands every private subnet to `create_agent_runtime`, the runtime goes
`CREATE_FAILED` with a `failureReason` naming the offending subnets. The provision job then
fails, so onboarding a system agent never completes even though the knowledge graph was
already written.

**Decision:** on a `CREATE_FAILED` whose `failureReason` mentions unsupported availability
zones, parse the named `subnet-*` ids, delete the failed runtime, drop those subnets, and
recreate with the remaining (supported-AZ) subnets — up to a small retry bound. If no subnets
remain, fail loudly (no silent fallback to `PUBLIC`, which would sever private RDS access).

## Context

Discovered by the onboarding smoke test: `cmms` reached
`status='onboarded'` with 62 `MAPS_TO` edges in Neptune, but the catalog card never flipped
to onboarded because the `vf_cmms` runtime CREATE_FAILED on `subnet-…` in `use1-az6`. This
affects every cloud-runtime system agent in any account whose VPC includes an unsupported AZ.

## Considered Options

- **Hardcode a per-Region supported-AZ allowlist and filter subnets up front:** rejected.
  The AgentCore supported-AZ set changes as the service expands, so the list would rot and
  silently misbehave; it also re-introduces the hardcoded-infra-assumption smell ADR 0009 warns
  against. The authoritative source of "what's unsupported" is the API's own failure reason.
- **Pin `ADTNetworkStack` to AgentCore-supported AZs:** rejected as the primary fix. It is the
  "most correct" long-term, but it is a high-blast-radius VPC change (subnet/NAT replacement
  forcing replacement of dependent resources) and still bakes in a static AZ assumption. Left
  as a possible future hardening, not the unblock.
- **Fall back to `networkMode: PUBLIC` when VPC AZs are unsupported:** rejected. System agents
  need VPC egress to private RDS; a public runtime would break data access silently.

## Consequences

- Onboarding self-heals across AZ layouts with no per-Region configuration; portable across
  accounts and Regions.
- Reactive: one `CREATE_FAILED` (and a runtime delete) is incurred before the retry succeeds,
  adding a couple of minutes to first-time provisioning in an affected account.
- If a future deploy wants to avoid the reactive cost, pinning supported AZs in `ADTNetworkStack`
  remains available as a complementary hardening.
- Recorded as an early-deployment finding.
