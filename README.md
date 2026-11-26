<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# Agentic Digital Thread for Manufacturing

An AWS Cloud Development Kit (AWS CDK) application that deploys a
conversational AI platform for manufacturing. A natural-language orchestrator
queries live manufacturing systems: enterprise resource planning (ERP),
manufacturing execution systems (MES), computerized maintenance management
systems (CMMS), product lifecycle management (PLM), AWS IoT SiteWise, and a
HighByte Unified Namespace. It uses an Amazon Neptune Analytics knowledge graph
to answer cross-system questions, and the graph accumulates query patterns to make
answers more consistent over time.

This sample is the companion code for the AWS re:Invent 2026 workshop IND402, "Agentic AI for manufacturing: Build a cross-domain digital thread".

The platform runs on **Amazon Bedrock AgentCore**: the orchestrator and system
agents run as AgentCore Runtimes connected through an AgentCore Gateway over the
Model Context Protocol (MCP). The web UI is served by Amazon ECS on AWS Fargate
behind Amazon CloudFront. The agents use the open-source
[Strands Agents](https://github.com/strands-agents/sdk-python) framework.

> **Note:** The knowledge graph is a rules engine, not a data store. It holds metadata (which
> systems exist, what their fields mean, and how to translate
> identifiers between them) and never stores production data. Architecturally
> it plays the role of a *semantic layer*: the meaning-and-translation tier
> between the agents and the systems ("rules engine" describes what is inside
> it; "semantic layer" describes where it sits; see
> [docs/adr/0057](docs/adr/0057-semantic-layer-export-boundary.md)). See
> [docs/architecture.md](docs/architecture.md).

> **Important:** This is sample code, for non-production usage. You should work
> with your security and legal teams to meet your organizational security,
> regulatory and compliance requirements before deployment. This repository
> demonstrates the pattern and is not production-hardened. Review and adapt the
> security, networking, IAM, and data-handling configuration before any
> non-demonstration use.

> **Regions:** this version of the sample deploys to US East (N. Virginia),
> `us-east-1`. Other AWS Regions are not supported yet: the CloudFront-scope
> AWS WAF web ACLs and the Lambda@Edge sign-in functions can only be created in
> `us-east-1`, and the stacks create them alongside everything else.

---

## Architecture at a glance

The application path includes an Application Load Balancer (ALB):

![Main architecture diagram. Users reach Amazon CloudFront (with an AWS WAF
web ACL), then an internal Application Load Balancer, then the ADT app on
Amazon ECS Fargate. The app uses Amazon DynamoDB and Amazon Neptune Analytics
(the VPC-private knowledge-graph rules engine), and invokes the orchestrator
on Amazon Bedrock AgentCore. The orchestrator reaches per-system agents
through the AgentCore Gateway over MCP; the agents query the onboarded
systems: ERP and MES on Amazon Aurora PostgreSQL, CMMS and PLM on Amazon RDS
for PostgreSQL, AWS IoT SiteWise, and HighByte UNS. AgentCore Memory and
Evaluations, plus Amazon Cognito, Amazon CloudWatch, AWS Secrets Manager, and
AWS Systems Manager Parameter Store round out the platform
services.](docs/diagrams/adt-arch-slide-1-main.png)

More views (telemetry path, ERP simulation) are in
[docs/diagrams/](docs/diagrams/), exported from
[docs/architecture-slides.html](docs/architecture-slides.html).

The default deployment is **`base`**: **5 engine stacks** with no
pre-connected data sources, intended for connecting your own systems. Opt-in
flags add bundled sample data: `--seed` adds a static dataset (a 6th
`ADTSeedDataStack`), `--simulator` adds the live ERP simulation (**9 stacks**,
implies `--seed`), and `--highbyte` adds the optional HighByte OT/UNS layer on top
of any data mode (base → **6 stacks**, full → **10 stacks**; independent of
`--seed`/`--simulator`). HighByte Intelligence Hub runs the public
`highbyte/intelligence-hub` image from Docker Hub. The container runs only with
`ACCEPT_EULA=Y`, which agrees to the
[HighByte Intelligence Hub End User License Agreement](https://www.highbyte.com/hubfs/highbyte_intelligence_hub_eula.pdf),
so read it first: the first HighByte deploy needs
`--highbyte --accept-highbyte-eula`, and without a recorded acceptance
`deploy.sh` stops and prints the link. You are responsible for obtaining any
required HighByte license, which is uploaded through the HighByte UI after
startup. Full stack list,
deploy modes, and data flow:
[docs/architecture.md](docs/architecture.md) and
[docs/deployment.md](docs/deployment.md#deploy-modes).

---

## Prerequisites

Before deploying, review the
[prerequisites](docs/deployment.md#prerequisites). In particular, the required
Amazon Bedrock models must be enabled for your account and Region first
(Amazon Bedrock model access). Otherwise the deployment will fail at runtime.

---

## Quickstart

```bash
# From the repository root
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
npm install                       # AWS CDK CLI (Node-based), installed locally
npx cdk bootstrap --profile <YOUR_PROFILE>

cd agentic_digital_thread
./deploy.sh --profile <YOUR_PROFILE> --region <YOUR_REGION>
```

This deploys **`base`** (engine only, no sample data). To explore with bundled
data instead, add `--seed` (static sample data) or `--simulator` (the live ERP
simulation; implies `--seed`). See [deploy modes](docs/deployment.md#deploy-modes).

The first deployment can take well over an hour (about 80 minutes). Creating
an AgentCore Runtime can take several minutes, and `deploy.sh` waits up to 15
minutes for each one to report ready.
`deploy.sh` is the supported path: it
performs runtime steps that `cdk deploy --all` does not (agent image build,
AgentCore Runtime, Neptune seeding, AWS IoT SiteWise models, cache
invalidation).

After it finishes, complete the **required** [post-deploy steps](docs/post-deploy.md): onboarding
your systems. On a fresh deployment the HighByte MCP bearer token
and AWS IoT SiteWise publishing are configured automatically.

Full instructions: [docs/deployment.md](docs/deployment.md).

---

## Cost

Deploying this sample provisions billable, always-on AWS resources, and charges
accrue until you tear the environment down. The main cost-driving resources are:

- **Amazon Neptune Analytics**: the knowledge graph.
- **Amazon Aurora Serverless v2**, **Amazon RDS for PostgreSQL**, or both: the
  simulated ERP / MES / CMMS / PLM databases.
- **Amazon ECS on AWS Fargate**: the ADT app, the agents, the ERP dashboard,
  and (optionally) the HighByte tasks.
- A **NAT gateway** for private-subnet egress.
- **Amazon CloudFront distributions**, each fronted by **AWS WAF**. Base (and
  seed) deploys one; `--simulator` deploys two; base + `--highbyte` deploys two;
  `--simulator --highbyte` deploys three. Human browser surfaces use Amazon
  Cognito at the edge as described under Security.

The optional HighByte and AWS IoT SiteWise path adds more (additional Amazon ECS
on AWS Fargate tasks plus AWS IoT SiteWise ingestion and storage). The optional
`--observability` flag adds Amazon CloudWatch trace-span ingestion, storage,
and indexing charges (CloudWatch Transaction Search) that accrue continuously
while enabled.

Use the [AWS Pricing Calculator](https://calculator.aws/) to estimate cost for
your Region and usage. **To stop charges, tear everything down** with the
[Cleanup](#cleanup) step (`./cleanup.sh`, run from the repository root; see
[docs/operations.md](docs/operations.md)).

---

## Cleanup

To remove everything this sample deploys, run the following **from the
repository root**:

```bash
./cleanup.sh --profile <YOUR_PROFILE> --region <YOUR_REGION>
```

`cleanup.sh` works on the whole account and Region, not only on what one
deployment created. It deletes every `/adt/*` SSM parameter and every
`adt/agent-config/*` and `adt/conn/*` secret there, with no recovery window,
and every AWS IoT SiteWise asset and asset model that has one of the sample's
model names. Deploy the sample into a dedicated account, so cleanup deletes
nothing you want to keep.

See [docs/operations.md](docs/operations.md) for details and expected teardown
time.

---

## Documentation

Use these guides to understand, deploy, operate, and extend the sample.

| Document | Contents |
|----------|----------|
| [docs/architecture.md](docs/architecture.md) | Stacks, data flow, the rules-engine concept, the data model. |
| [docs/onboarding.md](docs/onboarding.md) | Onboarding deep-dive: preflight, discover → map → resolve → provision, the guided flow, re-onboarding and schema drift. |
| [docs/domain-context-intake.md](docs/domain-context-intake.md) | The ten domain-context intake questions, why each matters, and the engine surface each answer lands on. |
| [docs/self-learning.md](docs/self-learning.md) | Self-learning deep-dive: the proposal lifecycle, crosswalk mining, safety rails, and feedback-triggered exploration. |
| [docs/metric-layer.md](docs/metric-layer.md) | How KPIs are defined, resolved deterministically, and explained (lineage, cards, honest statuses). |
| [docs/deployment.md](docs/deployment.md) | Prerequisites, deploy modes, new Region/account, model config. |
| [docs/local-development.md](docs/local-development.md) | Hybrid local stack (local app + PostgreSQL + knowledge graph; cloud Bedrock): onboarding, transactional simulation, and cross-system chat all run locally for fast iteration. |
| [docs/post-deploy.md](docs/post-deploy.md) | MCP token, AWS IoT SiteWise pipeline, onboarding, credentials. |
| [docs/operations.md](docs/operations.md) | Cache invalidation, teardown, troubleshooting. |
| [docs/agents-fleet-metrics.md](docs/agents-fleet-metrics.md) | How the Agents management page is served from a warmed snapshot cache of native metrics, never from live Logs Insights queries. |
| [docs/cross-account-onboarding.md](docs/cross-account-onboarding.md) | Onboarding real data from a separate account through an assumed read-only role (Amazon Athena + AWS IoT SiteWise). |
| [docs/highbyte.md](docs/highbyte.md) | The optional HighByte OT/UNS layer in depth. |
| [docs/quick-integration.md](docs/quick-integration.md) | Connecting AWS Quick: the decision table over the two integration paths (entry point for the Quick web-connector and skill docs). |
| [docs/experiments.md](docs/experiments.md) | *Maintainer.* Running a variant A/B with the golden eval lane and reading its informational-by-default diff. |
| [docs/provenance-citations-design.md](docs/provenance-citations-design.md) | *Maintainer.* Design doc for per-claim provenance: how an answer citation becomes a three-tier, inspectable evidence trail. |
| [docs/graph-shape-contract.md](docs/graph-shape-contract.md) | *Maintainer.* The verified knowledge-graph shape (labels, edges, properties) that the deterministic resolver may depend on. |
| [docs/glossary.md](docs/glossary.md) | Canonical terminology. |

---

## Connect your own Tulip instance

The sample ships a first-class **Tulip (Tables API)** connector. It reads a
Tulip workspace over Tulip's public Tables REST API and joins the equipment
identifier into the cross-system digital thread, the same way the other
connectors do. It talks to **your own** Tulip instance; the sample deploys no
Tulip server of its own.

To onboard a Tulip system, choose **Tulip (Tables API)** in the onboarding UI
(or call the onboarding API) and give the connector your Tulip instance details
and a Tables API key pair:

- **`base_url`** is your Tulip instance root over HTTPS, for example
  `https://<your-workspace>.tulip.co`.
- **`api_key`** and **`api_secret`** are a Tulip Tables API key pair. The
  connector derives the HTTP Basic token itself and never logs the raw values.
- **`key_scope`** is `instance` or `workspace`; **`workspace_id`** names the
  workspace the key reads.
- **`table_label_prefix`** is optional. Set it to scope the connection to the
  tables whose label starts with that prefix (useful when one workspace holds
  several sites' tables); leave it empty to read every table.

Onboarding stores these credentials in the application's own connection secret
(`adt/conn/<system_id>`) and keeps them off the system node, out of the catalog
and out of every response. The application and agent-runtime roles already read
`adt/conn/*`, so no extra IAM is needed. See
[docs/onboarding.md](docs/onboarding.md) for the onboarding flow.

> **42Q CMMS:** a 42Q connector is not included.

---

## Connect Amazon S3 Tables (DuckDB)

The **S3 Tables** connector reads an
[Amazon S3 Tables](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-tables.html)
table bucket with [DuckDB](https://duckdb.org/) running inside the system's
agent, through DuckDB's Iceberg extension. It is read-only (the catalog is
attached `READ_ONLY`, and only `SELECT`/`WITH` queries run), needs no stored
password, and installs nothing at run time: the four DuckDB extensions it loads
(`aws`, `httpfs`, `avro`, `iceberg`) are built into the images.

The onboarding form does not offer this type yet. Register the system through
`POST /api/onboard/connect` with `connection_type` set to `S3 Tables` and a
`connection_config` such as:

```json
{
  "table_bucket_arn": "arn:aws:s3tables:us-east-1:111122223333:bucket/<YOUR_BUCKET>",
  "catalog_endpoint": "s3_tables",
  "role_arn": "arn:aws:iam::111122223333:role/<YOUR_READ_ONLY_ROLE>",
  "external_id": "<YOUR_EXTERNAL_ID>",
  "namespaces": ["<YOUR_NAMESPACE>"]
}
```

- **`table_bucket_arn`** is required and must be an S3 Tables bucket ARN.
- **`catalog_endpoint`** is `s3_tables` (the default; attaches the bucket
  itself) or `glue` (attaches the bucket's federated AWS Glue catalog, for a
  bucket that AWS Lake Formation governs or shares from another account).
- **`role_arn`** (and its **`external_id`**, when the role requires one) is the
  read-only role to read through; see below for why you need one. The connector
  assumes it and passes the temporary keys to DuckDB as bound parameters, never
  in SQL text. The `external_id` is never logged or put in SQL.
- **`namespaces`** and **`tables`** (`namespace.table` entries) are optional and
  limit what the system can read.
- **`query_timeout_s`**, **`max_rows`** and **`duckdb_threads`** (default 2)
  are optional limits, each held under a fixed ceiling. **`duckdb_memory_limit`**
  (default `2GB`) is checked for its form only, so keep it below the memory of
  the agent runtime.

Give a `role_arn`. Onboarding discovery runs in the ADT app, and the app's
task role has no S3 Tables permissions of its own, so it must assume the
read-only role. Two things set that up:

1. Deploy the read-only role in the account that owns the bucket, trusting this
   app's account and requiring your External ID, as for Athena
   ([docs/cross-account-onboarding.md](docs/cross-account-onboarding.md)).
2. Deploy the app with `--cross-account-role convention` (or the role's ARN).
   That grants the app and agent roles `sts:AssumeRole` on it; the setting is
   sticky across later deploys.

Setting `"s3TablesBucketArn": "<YOUR_BUCKET_ARN>"` in the `context` block of
`cdk.json` grants the per-system agent runtimes the S3 Tables read actions on
that one bucket and its tables. It does not cover onboarding discovery, so it
is not a substitute for the role. A bucket governed by AWS Lake Formation also
needs a Lake Formation grant to the role that reads it.

---

## Security

This sample is subject to the
[AWS Shared Responsibility Model](https://aws.amazon.com/compliance/shared-responsibility-model/).
AWS is responsible for security *of* the cloud; you are responsible for security
*in* the cloud, including the configuration of the resources this sample
deploys.

The reference architecture implements defense-in-depth controls:

| Layer | Control | Implementation |
|-------|---------|----------------|
| **Edge** | AWS WAF | The ADT application, ERP dashboard, and HighByte UI CloudFront distributions use an AWS WAF web ACL with AWS Managed Rules (Common, KnownBadInputs, IpReputation). |
| **Authentication** | Amazon Cognito + OAuth2 | Human browser surfaces use Cognito Authorization@Edge: ERP and HighByte when deployed, and ADT by default (unless explicit/sticky configuration turns it off). Unauthenticated browsers are redirected to the hosted UI; ADT `/api/*` and `/health`, plus HighByte machine paths (`/mcp*`, `/i3x*`, `/i3X*`, `/data/*`), bypass the browser gate and retain their own 401/bearer auth. Machine-to-machine AgentCore traffic uses OAuth. See [ADR-0070](docs/adr/0070-cognito-authorization-at-edge-default.md). |
| **External identity provider** | Cognito OIDC identity provider (optional) | Bring your own OpenID Connect provider: `deploy.sh --oidc-issuer/--oidc-client-id/--oidc-secret-arn` adds one OIDC provider to every hosted-UI pool (ADT, ERP, HighByte) as a second sign-in button next to the local Cognito directory (`--oidc-only on` makes it the sole sign-in and skips the hosted-UI chooser). The client secret is a Secrets Manager dynamic reference (never in a template); external users arrive as `<ProviderName>_<subject>` with no pool groups, so per-system group enforcement fails closed until a group is granted. Sticky via SSM `/adt/oidc-*`. See [ADR-0072](docs/adr/0072-external-oidc-identity-provider-option.md) and [docs/deployment.md](docs/deployment.md#external-oidc-identity-provider-optional-sticky-default-off). |
| **Identity propagation** | App-signed identity claim | Interactive turns carry a short-lived (≤5 min) app-signed user-identity claim to system agents, enabling optional per-system group enforcement (`SYSTEM_REQUIRED_GROUP`; unset = allow-all, and a half-configured policy fails closed). Demo-grade: the signing key is app-managed in AWS Secrets Manager and the gateway transport hop remains machine-to-machine OAuth. |
| **AI safety** | Amazon Bedrock Guardrails | Default guardrail with content filtering (sexual, violence, hate, insults, misconduct) and prompt-attack detection; bring your own guardrail through `GUARDRAIL_ID` for PII anonymization or topic denial. |
| **Network** | VPC + flow logs | Private subnets for all data and compute; flow logs to Amazon CloudWatch. |
| **Encryption** | In transit and at rest | HTTPS for all public web traffic through Amazon CloudFront; Amazon RDS `storage_encrypted=True`; secrets in AWS Secrets Manager. Inside the VPC, the hop from CloudFront (a VPC origin) to each internal Application Load Balancer (the ADT app, the ERP dashboard, HighByte), and from in-VPC callers such as the HighByte setup AWS Lambda function and the opt-in AWS Quick bridge runtime to those load balancers, is plain HTTP: the load balancers are not internet-facing and accept traffic only from the CloudFront VPC origin and named in-VPC security groups. The ALBs have no ALB-specific domain or certificate; optional custom-domain certificates terminate at CloudFront. For production, add an HTTPS listener with an AWS Certificate Manager certificate on each ALB, set the VPC origins to HTTPS only, and point the in-VPC callers at the HTTPS listener. |
| **Secrets** | AWS Secrets Manager | Demo password generated or supplied at deploy time; overridable. |
| **IAM** | Least privilege | Resource-scoped IAM policies (account/Region-bounded ARNs) for AWS Secrets Manager, Amazon RDS, Amazon DynamoDB, and AWS Lambda; remaining wildcards are documented with justification through cdk-nag suppressions. |
| **Database access** | Read-only logins for model-written SQL | System agents on the bundled demo databases connect with a per-schema login that can only read their own schema (no owner login, no other schema, no writes); the SQL guard also refuses functions with side effects such as ending sessions or reading server files. Connections you register keep the login you supply; onboarding preflight warns when that login can write. See [ADR-0096](docs/adr/0096-system-agents-read-with-their-own-login.md). |
| **OT network isolation** | Private OPC UA | The simulated OPC UA servers run in private subnets behind an internal Network Load Balancer (not internet-facing). Demo connections are unencrypted and anonymous for simplicity. Enable OPC UA message security (`SignAndEncrypt`) and authentication for production OT. |
| **Monitoring** | Amazon CloudWatch + Container Insights | ECS Container Insights, VPC flow logs, log groups. |
| **Conversation memory** | Amazon Bedrock AgentCore Memory | When enabled, interactive chat turns persist to a managed memory store with a 7-day event TTL, namespaced per user; only interactive chat writes, and the resource is removed on stack teardown. See the **Conversation memory (when enabled)** note below. |

**Credentials (demo only):** the sample ships no hardcoded password. The
password is set **once at the first deploy** and is **stable across subsequent
deploys**. By default CloudFormation generates it once
(`GenerateSecretString`) and stores it in the `adt-demo-password` AWS Secrets
Manager secret, from which it is propagated to the HighByte admin secret and
the demo Amazon Cognito users (logic in
[`stacks/_demo_credentials.py`](stacks/_demo_credentials.py)). **This
generated path is the recommended one for every deployment**: the value never
appears in a template, a deploy argument, or a log. Retrieve it after
deployment:

```bash
aws secretsmanager get-secret-value --secret-id adt-demo-password \
  --query SecretString --output text
```

> **Warning:** Use the override for throwaway deployments only: it is embedded
> in the template. `cdk deploy -c demo_password='<YOUR_PASSWORD>'` stores that literal as the secret's
> value in the synthesized CloudFormation template in plain text
> (`SecretValue.unsafe_plain_text`), so it is visible to anyone who can read
> the template (in `cdk.out/`, the CloudFormation console, or
> `DescribeStacks`/`GetTemplate` API calls), and it appears in your shell
> history. The change-trigger property the custom resources see is a truncated
> SHA-256 digest of the override, never the value itself. Do not use the
> override for anything you would mind being read from the template.

**Conversation memory (when enabled):** when Amazon Bedrock AgentCore Memory is
enabled, interactive chat conversation turns are persisted to a managed
AgentCore Memory store to provide the agent's short- and long-term memory
(Events, Facts, Preferences, and Summaries), with a 7-day event TTL
(`eventExpiryDuration`). Only interactive chat writes to memory; synthetic and
automated flows (warmup, scheduler, briefings, onboarding checks) do not.
Records are namespaced per user (actor), and the memory resource is removed on
stack teardown (removal policy `DESTROY`). As with the rest of this
demonstration sample, this is not production-hardened: review retention,
encryption, and access controls before any non-demonstration use.

> **Important:** Memory and PII. Amazon Bedrock Guardrails apply to the model's
> **responses**, not to what is written to memory (and PII anonymization in
> particular requires configuring it on your own guardrail, because the bundled
> default guardrail does not include it). To keep
> raw identifiers out of the persisted store, the sample applies a **best-effort
> redaction on the persisted copy of each turn** before it becomes a memory event. Structured
> identifiers (SSN, email, phone, credit-card-shaped numbers) are
> replaced with `[REDACTED_*]` tokens, so they do not reach the events or the
> extracted Facts/Summaries (the live conversation the model reads is untouched).
> This is deterministic and **best-effort only**: it does **not** cover names or
> other unstructured PII, which the extraction strategies can still derive (for
> example a name into Facts). Before any non-demonstration use, add a
> guardrail-based (NER) scrub on the inbound turn for full coverage, and review
> the extraction prompts and record retention (7-day event TTL). See
[`docs/adr/0026-memory-keyed-to-conversation-session.md`](docs/adr/0026-memory-keyed-to-conversation-session.md),
[`docs/adr/0027-local-mode-uses-cloud-agentcore-memory.md`](docs/adr/0027-local-mode-uses-cloud-agentcore-memory.md),
and [`docs/adr/0028-memory-writes-use-strands-session-manager.md`](docs/adr/0028-memory-writes-use-strands-session-manager.md)
for the design.

### Security choices to review before you deploy

These are deliberate trade-offs in the sample. Review each one against your
own requirements.

- **Multi-factor authentication is optional, not required.** The human sign-in
  pools (`adt-app`, `erp-dashboard-users`, `highbyte-ui-users`) are set to MFA
  `OPTIONAL` with authenticator-app (TOTP) codes as the only second factor
  (SMS is off). A user who registers an authenticator app is always asked for
  a code; a user who has not signs in with a password only, which is how the
  generated demo user signs in. To require MFA for everyone, set
  `mfa_configuration` to `"ON"` in `offer_totp_mfa`
  ([`stacks/_sample_hardening.py`](stacks/_sample_hardening.py)). Every user
  then has to enroll a device at first sign-in through the Cognito hosted
  UI. cdk-nag rule `AwsSolutions-COG2` stays suppressed because it passes
  only on `REQUIRED`.
- **Known limitation: the app's password sign-in API does not complete an
  MFA challenge yet.** Once a user registers an authenticator app, Cognito
  answers the app's own password sign-in (`/api/auth/login`, the form shown
  when edge sign-in is off) with a code challenge, which that API cannot
  complete: the sign-in fails with a server error. The Cognito hosted UI
  (the default edge sign-in) asks for the code and works. Until a later
  version adds the code step to the app's sign-in, keep edge sign-in on for
  any user who enrolls a device, and do not enroll one on the demo user.
- **The Content-Security-Policy is report-only.** CloudFront sends the CSP in
  a `Content-Security-Policy-Report-Only` header
  ([`stacks/_edge_auth.py`](stacks/_edge_auth.py)), so a browser reports
  violations but does not block them. The same policy serves several
  different web apps (the ADT app, the Streamlit ERP dashboard and the
  HighByte UI, some over WebSockets), and it has not been validated against
  every asset each of them loads; enforced, a missing source breaks that
  page. Watch the violation reports on your own deployment, adjust the
  policy, then rename the header to `Content-Security-Policy` to enforce it.
- **The AWS Quick API (opt-in) has no AWS WAF web ACL.** Every method of
  the Quick REST API uses AWS IAM (SigV4) authorization, so only callers
  with credentials in your account can invoke it; there is no anonymous
  surface for a web ACL to filter (cdk-nag `AwsSolutions-APIG3`). Its stage
  writes access logs to its own CloudWatch log group (30-day retention). The
  Amazon API Gateway CloudWatch role that access logging needs is one setting per
  account and Region. If your account already has a working one, the
  network stack (`ADTNetworkStack`) keeps it and never changes it. Only
  when none is set, or the one set cannot be used, does it set a role of its
  own. It never clears or replaces the setting, also not when it is deleted
  ([`stacks/_apigw_log_role.py`](stacks/_apigw_log_role.py)).
- **One IAM role is left in your account on purpose.** The role the network
  stack creates for that setting (IAM path `/adt/apigateway-cloudwatch/`,
  name starting `ADTNetworkStack-ApiGatewayAccountLogRole`) is retained when
  the stack is deleted, so an account setting that names it never points at
  a missing role; CDK's own `RestApi` makes the same choice by default.
  `cleanup.sh` prints its name at the end. Delete it only once no API
  Gateway API in that account and Region logs to CloudWatch: check
  `aws apigateway get-account`, then detach
  `AmazonAPIGatewayPushToCloudWatchLogs` from the role and delete the role.
- **The HighByte hub can create AWS IoT SiteWise asset models and assets on
  `*`.** When the hub's SiteWise pipeline is on (the default with
  `--highbyte`), the hub creates SiteWise models and assets at run time, so
  its task role allows `iotsitewise:CreateAssetModel` and
  `iotsitewise:CreateAsset` without a resource constraint (checkov
  `CKV_AWS_111`). `CreateAsset` could be scoped to this account and Region's
  asset models; `CreateAssetModel` has no resource type, so the only
  available constraint is a condition such as `aws:RequestedRegion`. Updates,
  deletes and property writes are already scoped to this account and Region.
- **HighByte machine paths rely on the hub's own authentication.** The
  HighByte CloudFront distribution sends `/mcp*`, `/i3x*`, `/i3X*` and
  `/data/*` past the browser sign-in gate, because machine clients cannot
  follow a hosted-UI redirect. On those paths the hub itself checks the
  caller's bearer token, not Cognito. On a tested deployment, anonymous
  requests through CloudFront to `/data/*` and `/i3x*` returned no data
  (HTTP 404). Verify the same on your own deployment, for every one of these
  paths, before you rely on it.

To report a security issue, see [CONTRIBUTING.md](CONTRIBUTING.md#security-issue-notifications).

---

## Repository layout

| Path | Contents |
|------|----------|
| `app.py`, `stacks/` | CDK app and stack definitions. |
| `agentic_digital_thread/` | The ADT application: container image, agents, web UI, `deploy.sh`. |
| `lambda-adt-seed/`, `lambda-db-init/`, `erp_simulation/` | Database schemas, seed data, and simulation. |
| `highbyte/` | HighByte assets, OPC UA dataset generation, AWS IoT SiteWise models. |
| `docker/` | Additional Dockerfiles. |
| `docs/` | This documentation set. (`docs/adr/` holds maintainer design records: useful background, but not part of the user-facing guides. [`docs/adr/README.md`](docs/adr/README.md) indexes the ADRs.) |

---

## License and contributing

Licensed under the [MIT-0 License](LICENSE). Third-party attributions are in
[NOTICE](NOTICE). See [CONTRIBUTING.md](CONTRIBUTING.md) and
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

## Third-party dependencies and services

Although this repository is released under the MIT-0 license, it depends on
third-party packages that are installed or retrieved at install time, build
time, or run time. These packages are licensed under their own terms, which
you must accept to use them. Some of these licenses may be restricted by your
company's open source policy, so please review them before proceeding.

The list below reflects the licenses identified in each package's
documentation as of the latest license review. It is provided for
convenience only and may not be complete or current; please check each
package's download site for the latest licensing information.

- Memgraph, local development only (https://memgraph.com): BUSL-1.1

This code interacts with HighByte Intelligence Hub which has terms published
at https://www.highbyte.com/hubfs/highbyte_intelligence_hub_eula.pdf and
pricing described at https://www.highbyte.com/pricing. You should be familiar
with the pricing and confirm that your use case complies with the terms before
proceeding.

This code interacts with Tulip which has terms published at
https://tulip.co/legal/terms-of-service/ and pricing described at
https://tulip.co/plans/. You should be familiar with the pricing and confirm
that your use case complies with the terms before proceeding.

This code interacts with Docker Hub which has terms published at
https://www.docker.com/legal/docker-terms-use/ and pricing described at
https://www.docker.com/pricing/. You should be familiar with the pricing and
confirm that your use case complies with the terms before proceeding.

## Trademarks and disclaimer

The demo companies, plants, suppliers, equipment, people and operational data in
this repository are fictitious and were generated for demonstration purposes
only.

The repository also names real third-party products that the sample integrates
with or that it uses: HighByte Intelligence Hub (HighByte, Inc.), Tulip (Tulip
Interfaces, Inc.), the i3X API (CESMII), and Memgraph (Memgraph
Ltd., local development only). It also names 42Q (Sanmina Corporation). These
names are trademarks of their respective owners. They are used only to identify
the products they refer to; their use does not imply any affiliation with or
endorsement by those owners.

Claude is a trademark of Anthropic, PBC. Other product and company names
mentioned in this repository may be trademarks of their respective owners.

HighByte Intelligence Hub is commercial software. It is optional (`--highbyte`),
is not included in this repository, and is pulled by you from Docker Hub only
after you accept the HighByte EULA (`--accept-highbyte-eula`); you need your own
HighByte license to use it.
