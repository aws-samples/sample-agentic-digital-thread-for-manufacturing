<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# ADT Application (`agentic_digital_thread/`)

This directory holds the Agentic Digital Thread (ADT) application — the
container image, agents built with the Strands Agents framework, and the web UI — plus `deploy.sh`, the
supported deployment entry point for the whole sample.

> See [`../docs/glossary.md`](../docs/glossary.md) for the `adt`
> resource-name prefix and other terminology.

## What it does

A conversational orchestrator answers questions that span multiple
manufacturing systems by consulting an Amazon Neptune Analytics knowledge graph
(used as a **rules engine**, not a data store) and querying the source systems
directly. The full concept, data model, and API surface are documented in
[`../docs/architecture.md`](../docs/architecture.md).

## Layout

```
agents/        # FastAPI server, orchestrator, system agents, onboarding, catalog
frontend/      # React + Vite web UI; the server serves the build from frontend/dist
static/        # Only the deploy.sh-built i3x-explorer bundle
data/neptune/  # Cypher graph seeds (concepts + identity rules)
Dockerfile     # App container image
deploy.sh      # Supported deploy entry point (see ../docs/deployment.md)
```

## Deploy

Use `deploy.sh` from this directory — it performs runtime steps that
`cdk deploy --all` does not (agent image build, AgentCore Runtime, Neptune
seeding, AWS IoT SiteWise models, Amazon CloudFront invalidation):

```bash
./deploy.sh --profile <YOUR_PROFILE> --region <YOUR_REGION>
```

After deployment, open the URL from the `ADTComputeStack` output
`ADTCloudFrontUrl` and complete the [post-deploy steps](../docs/post-deploy.md).

## More

- [Architecture](../docs/architecture.md) — stacks, data flow, data model, APIs.
- [Deployment](../docs/deployment.md) — prerequisites and deploy modes.
- [Post-deploy](../docs/post-deploy.md) — onboarding and credentials.
