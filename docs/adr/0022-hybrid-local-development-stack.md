<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# Hybrid local development stack

---
Status: accepted
---

To shorten the local feedback loop, the local development stack runs the FastAPI
app, the orchestrator, the system agents, and PostgreSQL **locally**, while
calling **real** Amazon Bedrock (model inference) and Amazon Neptune Analytics
(the knowledge graph) in the cloud using the developer's AWS credentials. The
app already supports this: with no `AGENTCORE_GATEWAY_ID`/`AGENTCORE_ECR_IMAGE`
set, `agents/server.py` selects `AGENT_MODE=local`, and `get_orchestrator()`
builds an agent with the Strands Agents framework against `BEDROCK_MODEL` with `query_neptune_graph` as a
tool — so the AgentCore Gateway/Runtime layer is bypassed entirely.

## Considered options

- **Hybrid (chosen)** — fast iteration on ~90% of the code (UI, API, SQL,
  agent/orchestration, onboarding) with zero divergence in model/graph
  behavior; cost is Bedrock tokens plus one small Neptune graph.
- **Fully local** — also replace Bedrock with a local LLM and Neptune with a
  local openCypher store. Rejected for now: real work to build and it diverges
  model/graph behavior from the deployed demo, weakening the test signal.

## Consequences

- A local run still requires AWS credentials and network access to Bedrock; it
  is not fully offline.
- **Verified limitation:** the real Neptune Analytics graph is private and is
  **not reachable from a local machine**, so graph-backed features are degraded
  locally — see [`0024-neptune-private-graph-not-reachable-locally.md`](0024-neptune-private-graph-not-reachable-locally.md).
- A local openCypher substitute for Neptune is a possible *offline* path but is
  **deliberately deferred** in favor of in-VPC access to the real graph — see
  [`0024-neptune-private-graph-not-reachable-locally.md`](0024-neptune-private-graph-not-reachable-locally.md).
