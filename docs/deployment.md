<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# Deployment

> **Note:** Terminology in this document follows [`glossary.md`](glossary.md).
> For what gets deployed, see [`architecture.md`](architecture.md).

## Prerequisites

The following tools must be installed before you deploy:

| Tool | Version | Notes |
|------|---------|-------|
| Python | 3.12 (tested; `pyproject.toml` nominally allows 3.11+) | Runs the CDK construct code. The hash-locked `requirements.txt` and every CI/container image target 3.12. |
| Node.js | 20+ | The AWS CDK CLI is Node-based even though the stacks are written in Python. |
| AWS CDK CLI | pinned by `package.json` | Installed with `npm install`. |
| AWS CLI | 2.x | For credentials and post-deploy commands. |
| Docker | any | Required to build container images locally. `deploy.sh` checks for the `docker` command. |

**AWS permissions:** an administrator role on the target account. CDK bootstrap
and the stacks provision AWS Identity and Access Management (IAM), Amazon ECR,
Amazon ECS, Amazon RDS, Amazon Neptune Analytics, Amazon Bedrock, and Amazon
Bedrock AgentCore resources. Administrator access is a
convenience for deploying this demo; scope deploy-time permissions down for any
production use.

**Region:** this version of the sample deploys to US East (N. Virginia),
`us-east-1`; use it wherever this guide says `<YOUR_REGION>`. Other AWS
Regions are not supported yet: the CloudFront-scope AWS WAF web ACLs and the
Lambda@Edge sign-in functions can only be created in `us-east-1`, and the
stacks create them alongside everything else.

---

## One-time setup

### 1. Configure credentials

```bash
# Any standard method works — profiles, environment variables, or IAM Identity Center
aws configure --profile <YOUR_PROFILE>
# or:
aws configure sso --profile <YOUR_PROFILE>
aws configure set region <YOUR_REGION> --profile <YOUR_PROFILE>

aws sts get-caller-identity --profile <YOUR_PROFILE>   # verify
```

### 2. Install dependencies

```bash
# From the repository root
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt   # Python CDK construct library

npm install                       # AWS CDK CLI (pinned in package.json)
```

> **Note:** `agentic_digital_thread/deploy.sh` runs `npm install` automatically
> if `node_modules/` is missing, but doing it once up front is faster.

### 3. Bootstrap CDK (once per account and Region)

```bash
npx cdk bootstrap --profile <YOUR_PROFILE>
```

---

## Deploy modes

Each mode is a `deploy.sh` invocation; the flags decide which stacks deploy:

| Mode | Command | What deploys |
|------|---------|--------------|
| **Base** (default, 5 stacks) | `./deploy.sh --profile <YOUR_PROFILE> --region <YOUR_REGION>` | Engine only—Network, ADT database (Amazon Neptune Analytics + Amazon DynamoDB), AgentCore, Onboarding, and the Amazon ECS app + agents. **No source-system RDS and no pre-connected data**—you onboard your own systems post-deploy. |
| **Seed** (6 stacks) | `./deploy.sh --profile <YOUR_PROFILE> --region <YOUR_REGION> --seed` | Adds **`ADTSeedDataStack`**—an Amazon RDS for PostgreSQL database (`adt_demo`) loaded with the bundled **static** sample data (a frozen, coherent multi-plant ERP/MES/CMMS/PLM dataset). |
| **Full demo** (9 stacks) | `./deploy.sh --profile <YOUR_PROFILE> --region <YOUR_REGION> --simulator` | Adds the **live** ERP simulation: its own Amazon Aurora cluster, an AWS Lambda function driven by Amazon EventBridge that runs the simulation, and the Streamlit ERP dashboard. Implies `--seed`. **No live SiteWise telemetry**: see the `--simulator` telemetry note below the table. |
| **Full + HighByte** (10 stacks) | `./deploy.sh ... --simulator --highbyte --accept-highbyte-eula` | Adds the HighByte Intelligence Hub and four OPC UA simulators (Milwaukee, Detroit, Cedar Rapids, Indianapolis) feeding AWS IoT SiteWise. `--accept-highbyte-eula` is needed on the first HighByte deploy only. See the `--highbyte` notes below the table. |

**`--highbyte` notes.** HighByte runs the public `highbyte/intelligence-hub`
image from Docker Hub, and the container runs only with
`ACCEPT_EULA=Y`. The flag is **independent of the data flags**—it can also be
added to base (**Base + HighByte = 6 stacks**) or seed. When `--simulator` is
set, the setup additionally wires the ERP/historian PostgreSQL connections;
when those databases are absent the setup omits them—add your own in the
HighByte UI. Setting `ACCEPT_EULA=Y` agrees to the HighByte EULA, so the first
HighByte deploy also needs `--accept-highbyte-eula` (see
[Accepting the HighByte EULA](#accepting-the-highbyte-eula)).

**`--simulator` telemetry note.** "Live" in the Full demo row means the ERP,
MES and CMMS records the simulator writes. A plain `--simulator` deploy writes
no live telemetry into AWS IoT SiteWise. Its only SiteWise writer is the
[SiteWise degradation feed](architecture.md#sitewise-degradation-feed), which
backfills 72 hours for one CNC asset (`5-Axis-Milling-CMX-441`: vibration on
three axes and coolant temperature) and runs only when someone clicks
**Start machine degradation feed** on the ERP dashboard. Live telemetry for the
plant lines comes only from the OPC UA servers, read into SiteWise by
[`--sfc`](#optional---sfc-flag-sticky-default-off) or by `--highbyte`. For a
demo that asks live telemetry questions, deploy with `--simulator --sfc`.

**The default is `base`**—a clean engine with no sample systems, intended for
[connecting your own data sources](#connecting-your-own-data-sources). `--seed`
and `--simulator` are **opt-in** sample data. `--simulator` implies `--seed`
because the live simulator writes cross-system records (for example, an MES downtime
event creating a CMMS work order) into seed-defined schemas, so the seed must be
present. See [ADR-0030](adr/0030-demo-data-is-opt-in-base-default.md) for the
rationale and the schema-to-database routing.

> **Warning:** Omitting data flags removes sample data. The `--seed` and
> `--simulator` flags are not sticky. Repeat `--seed` on every deployment that
> should retain the static sample data, or repeat `--simulator` on every
> deployment that should retain the live simulator and its implied seed data.
> Deploying without the required flag removes the corresponding stacks; omitting
> `--simulator` removes the ERP simulator stacks, including the Aurora database
> and its data.

The ERP database initializer (`lambda-db-init`) runs on the first deploy
and re-runs only when its custom resource changes (the `DB_INIT_TIMESTAMP`
token in `stacks/erp_database_stack.py`). Bumping `DB_INIT_TIMESTAMP`
re-runs it, and that run **drops and reseeds every ERP table**, so all
simulated ERP data is lost. If that run fails (an error in any step, or a
table or a site's APPROVED lots missing afterwards), the custom resource
fails with the step and the problem in its status reason, so a first deploy
fails instead of finishing over an empty database. An update rolls back. When
the rollback's own request reaches the new function code, whichever property
the update changed (the timestamp, the database name, the seed, the cluster
or the secret), it logs `Database reset skipped` and leaves the database as
the failed run left it, so deploy again once the cause is fixed.

> **Important:** HighByte enablement is sticky. Once you deploy with `--highbyte`, the
> deployment records the choice in AWS Systems Manager
> (`/adt/highbyte-enabled`). Later deployments keep the HighByte stack even
> if you omit `--highbyte`. See `app.py` for the full gating logic. HighByte
> also needs your one-time acceptance of its EULA
> ([Accepting the HighByte EULA](#accepting-the-highbyte-eula)).

### Optional `--opcua` flag (sticky, default off)

`--opcua` deploys only the four simulated OPC UA servers (one per site), in
the same `ADTHighByteOpcuaStack`, without the HighByte Intelligence Hub. Use it
when another consumer reads the servers and HighByte is not needed.
`--highbyte` implies it. Like `--highbyte`, it is **sticky**
(`/adt/opcua-enabled`); adding `--highbyte` later adds HighByte to the same
stack without recreating the servers. `deploy.sh` always keeps the servers in
`ADTHighByteOpcuaStack`. A direct CDK deploy that passes both
`-c highbyte_enabled=true` and `-c opcua_servers_stack=true` puts them in
their own `ADTOpcuaServersStack` (see
[`architecture.md`](architecture.md)).

### Optional `--sfc` flag (sticky, default off)

`--sfc` adds Shop Floor Connectivity (SFC), which reads the OPC UA servers and
writes their telemetry to AWS IoT SiteWise and Amazon S3 Tables ([ADR-0082](adr/0082-sfc-writes-sitewise-for-unattended-deployments.md)). It
implies `--opcua` and is independent of `--highbyte`. Nothing else is needed:
the SFC image build downloads and SHA-512-verifies the pinned SFC release, the
stack generates the SFC configuration, and a custom resource creates the
SiteWise models and asset tree before SFC starts. Sticky via `/adt/sfc-enabled`.

### Optional `--highbyte-sitewise-pipeline enabled|disabled` flag (sticky)

Switches the HighByte hub's `NameSpace_to_SiteWise` pipeline (CDK context
`highbyte_sitewise_pipeline`, [ADR-0082](adr/0082-sfc-writes-sitewise-for-unattended-deployments.md)).
`disabled` imports the pipeline with every trigger off and gives the hub's
task role no SiteWise permission, and HighByte stays the UNS. SFC is then the
only SiteWise writer, and the SFC freshness watchdog is built, only when SFC is
enabled (`--sfc`). With `disabled` and without SFC there is no continuous
SiteWise writer and no watchdog. The flag needs a deployment that includes
HighByte. Without the flag, `deploy.sh` picks:

| Deployment | Pipeline | Continuous SiteWise writers |
|---|---|---|
| HighByte and SFC (`--highbyte --sfc`) | `disabled` | SFC (watchdog built) |
| HighByte without SFC (`--highbyte`) | `enabled` | HighByte |
| `--highbyte --sfc --highbyte-sitewise-pipeline enabled` | `enabled` | HighByte and SFC on the same properties (no watchdog) |
| `--highbyte --highbyte-sitewise-pipeline disabled` (no SFC) | `disabled` | none (no watchdog; `deploy.sh` warns) |

An explicit value always wins and is **sticky** (`/adt/highbyte-sitewise-pipeline`);
delete the parameter to return to the default. The default itself is never
recorded. `deploy.sh` prints the chosen value and why.

> **Existing `--highbyte --sfc` accounts:** before this option, the pipeline
> was always on, so HighByte and SFC both wrote SiteWise. Every deploy that
> applies the default prints, before CDK runs: "HighByte SiteWise pipeline:
> disabled by default because SFC is on (SFC is the only SiteWise writer).
> Pass --highbyte-sitewise-pipeline enabled to have HighByte write SiteWise
> too." Pass `--highbyte-sitewise-pipeline enabled` to keep both writers.

Before it writes anything, `deploy.sh` reads the values this choice needs:
`/adt/highbyte-enabled` (unless `--highbyte` is passed), `/adt/sfc-enabled`
(unless `--sfc` is passed), and, when HighByte is in the deploy and no
explicit value is given, `/adt/highbyte-sitewise-pipeline`.
Only a missing parameter counts as "not set": any other SSM error (access
denied, throttling, expired credentials, network) stops the deploy and names
the parameter.

### Optional `--observability` flag (sticky, default off)

`--observability` enables the in-app **per-session agent-observability
drill-down** on the Agents page. It is independent of the data flags and works
with any mode. It is **off by default because it has real, continuous cost**:
the flag enables Amazon CloudWatch Transaction Search span ingestion
(account/Region-level `aws xray update-trace-segment-destination --destination
CloudWatchLogs`, plus an indexing rule at a conservative sampling percentage—`OBSERVABILITY_SAMPLING_PCT`,
default `10`), which bills for span ingestion,
storage, and indexing in CloudWatch Logs while enabled.

```bash
./deploy.sh --profile <YOUR_PROFILE> --region <YOUR_REGION> --observability
```

Like `--highbyte`, the flag is **sticky**: the choice is recorded in AWS
Systems Manager (`/adt/observability-enabled`) and later deployments keep
the drill-down even if you omit the flag. To turn it off,
delete the SSM parameter, redeploy, and revert the trace destination with
`aws xray update-trace-segment-destination --destination XRay`.

Per-session drill-down data appears only after a `--observability` deploy has
enabled ingestion **and** live traffic has produced spans; until then the
panel reports no data rather than an estimate. Details—what the drill-down shows, the reader
routes, and the deployed-environment attribute-name confirmation note—are in
[`agents-fleet-metrics.md`](agents-fleet-metrics.md#per-session-drill-down-opt-in).

### Optional `--edge-auth` flag (sticky, default `on`)

`--edge-auth on|off` toggles **Amazon Cognito Authorization@Edge** on the ADT app's
Amazon CloudFront distribution, the same serverless application repository (SAR)
`cloudfront-authorization-at-edge` gate the ERP dashboard and HighByte UI use.
When on, the first unauthenticated browser request is **redirected to the
Amazon Cognito hosted UI** to sign in (users and passwords are the same
`adt-app` user pool the login form uses), and edge-authenticated users are
signed straight into the app without seeing `/login.html` a second time.

Why it exists: sign-in belongs at the edge, in front of every browser surface
(app, ERP dashboard, HighByte UI), with one Cognito hosted UI rather than an
application-served credential form. The application itself never handles
passwords, and `/api/*` keeps its own bearer authentication.

`/api/*` and `/health` deliberately **bypass** the edge gate: APIs keep their
own 401 bearer/session auth, so the CLI lanes (`scripts/smoke_prompts.py`,
`scripts/eval_answers.py`, `scripts/audit_learning.py`—all of which POST
`/api/auth/login`) and the `/api/ask` SSE stream work unchanged.

```bash
./deploy.sh --profile <YOUR_PROFILE> --region <YOUR_REGION> --edge-auth off   # keep the login form
```

The flag is **sticky** (SSM `/adt/edge-auth-enabled`, storing `on` and `off`
alike since the default is on). Resolution precedence is explicit CDK context
(`-c edge_auth=...`, including `deploy.sh --edge-auth`) over the sticky SSM
value over the default, which is **on**.

> **Important:** The edge OAuth client (and the optional external IdP) lives
> in `ADTAgentCoreStack` so both gateway `CUSTOM_JWT` authorizers can allow the
> edge client (see the ADR-0070 amendment). Upgrading a deployment whose client
> was created in `ADTComputeStack` **replaces** the client — it gets a **new
> client id** — so currently signed-in browser sessions become invalid and every
> user re-authenticates once through the hosted UI. No data or user records are
> affected.

### External OIDC identity provider (optional, sticky, default off)

Bring your own **OpenID Connect identity provider** — any vendor — for the
hosted-UI sign-in. When configured, every Amazon Cognito user pool that
carries an Authorization@Edge client (the ADT app, and the ERP dashboard and
HighByte UI when those stacks are deployed) gets one Cognito OIDC identity
provider, and the pool's hosted UI shows a **second sign-in button** labelled
with the provider name next to the local Cognito username/password form.
Local Cognito users keep working unchanged — customers who only want the
built-in directory never touch this option.

```bash
# 1. Register a confidential OIDC client at your provider (see redirect URIs below),
#    then store its client secret in AWS Secrets Manager (plain string, same account/Region):
aws secretsmanager create-secret --name adt/oidc-client-secret \
  --secret-string "<client secret from your provider>" \
  --query ARN --output text
# 2. Deploy with the trio (+ optional display name; default "CorporateIdP"):
./deploy.sh --profile <YOUR_PROFILE> --region <YOUR_REGION> \
  --oidc-issuer https://login.example.com \
  --oidc-client-id <client id from your provider> \
  --oidc-secret-arn arn:aws:secretsmanager:<region>:<account>:secret:adt/oidc-client-secret-AbCdEf \
  --oidc-name CorporateIdP
```

What the deploy creates (all in `stacks/_edge_auth.py`, the one
Authorization@Edge home — [ADR-0072](adr/0072-external-oidc-identity-provider-option.md)):

- an `AWS::Cognito::UserPoolIdentityProvider` of type `OIDC` per edge pool,
  requesting scopes `openid email` with the `GET` attribute-request method,
  mapping `email`, `given_name` and `family_name` to the matching Cognito
  attributes;
- the edge OAuth client lists `COGNITO` **and** the provider, so both
  choices appear on the hosted UI;
- the client secret reaches Cognito through a CloudFormation **dynamic
  reference** (`{{resolve:secretsmanager:<arn>:SecretString:::}}`) — the
  synthesized template, the deploy arguments and the logs never carry the
  literal. Rotate it at the provider, update the secret, redeploy.

**Redirect URIs.** Register `https://<hosted-ui-domain>/oauth2/idpresponse`
at your provider for **every** pool that receives the provider. The
hosted-UI domain prefixes are deterministic per account
(`stacks/_cognito_domain.py` / the owning stacks), so you can compute them
from the 12-digit account id **before** the first deploy — `<last8>` is the
last eight digits, `<last12>` the full account id:

| Surface | Hosted-UI domain | Deployed when |
|---|---|---|
| ADT app (pool `adt-app`, owned by `ADTAgentCoreStack`) | `https://adt-app-<last8>.auth.<region>.amazoncognito.com` | always |
| ERP dashboard | `https://erp-dashboard-<last12>.auth.<region>.amazoncognito.com` | `--simulator` |
| HighByte UI | `https://highbyte-ui-<last8>.auth.<region>.amazoncognito.com` | `--highbyte` |

For example, account `111122223333` in `us-east-1` (`<last8>` = `22223333`) registers
`https://adt-app-22223333.auth.us-east-1.amazoncognito.com/oauth2/idpresponse`,
`https://erp-dashboard-111122223333.auth.us-east-1.amazoncognito.com/oauth2/idpresponse`
and `https://highbyte-ui-22223333.auth.us-east-1.amazoncognito.com/oauth2/idpresponse`.
After the deploy, `ADTERPDashboardStack` (`CognitoUserPoolDomain`) and
`ADTHighByteOpcuaStack` (`HighByteUiUserPoolDomain`) also output their
effective domain prefixes; the ADT app pool's domain is visible in the
Amazon Cognito console under the `adt-app` user pool.

**Token lifetimes.** All edge OAuth clients (with or without an external
provider) issue a 60-minute access token, a 60-minute id token and a
10-hour refresh token; the edge `RefreshAuth` function renews the short
tokens silently, so users re-authenticate about once per working day.
The access and id lifetimes must match because the edge `CheckAuth`
function refreshes on id-token expiry only, while the origin validates
the access token — a shorter access token would 401 every API call
before the edge ever refreshes.

**Two consequences to plan for:**

1. **Usernames are `<ProviderName>_<subject>`.** Cognito creates a linked
   pool user for each person who signs in through the provider, named from
   the provider and the provider's stable subject claim (for example
   `CorporateIdP_a1b2c3`). That name is what the app sees as the session
   username, and each such user is a distinct Cognito user with its own
   `sub` — so AgentCore conversation memory stays **per person**, exactly as
   for local users. Nothing is shared between external users.
2. **No `cognito:groups` by default.** Externally provisioned users belong to
   no pool groups, so they sign in as plain users (no Admin/integrator
   roles) and any system runtime with `SYSTEM_REQUIRED_GROUP` set denies
   them — **fails closed by design**
   ([ADR-0049](adr/0049-user-identity-propagation-app-signed-claim.md)).
   To grant roles, add the linked pool user to the group after their first
   sign-in (`aws cognito-idp admin-add-user-to-group`).

The configuration is **sticky** (SSM `/adt/oidc-issuer-url`,
`/adt/oidc-client-id`, `/adt/oidc-client-secret-arn`, and
`/adt/oidc-provider-name` when `--oidc-name` was given): later deploys keep
the provider without the flags; an explicit flag always wins over the stored
value. Providing only part of the trio fails the deploy with a message
naming the missing values. To remove the provider, delete the SSM parameters
and redeploy — the second button disappears and local sign-in is unaffected.
For the ADT app the option is inert while its edge gate is off (`--edge-auth
off`) because the login form has no provider
choice; the ERP dashboard and HighByte UI always sign in through their hosted
UIs, so they show the button whenever the provider is configured.

#### External provider as the only sign-in (`--oidc-only on`)

By default the hosted UI offers **both** the local Cognito directory and the
external provider. For an environment whose users all come from the
corporate provider — an internal staging site, an evaluation environment
reached only by employees — add `--oidc-only on`:

```bash
./deploy.sh --profile <YOUR_PROFILE> --region <YOUR_REGION> --oidc-only on
```

This removes `COGNITO` from **every** edge OAuth client, leaving the external
provider as its sole identity provider. Cognito then **skips the hosted-UI
chooser**: `/oauth2/authorize` answers with an immediate redirect to the
provider, so an unauthenticated browser lands on the corporate sign-in page
and no local-password form is reachable on the surface. Everything else — the provider resource, the
secret reference, the token lifetimes, the callback updater — is byte-identical
to the two-button configuration. The flag is sticky (SSM `/adt/oidc-only`,
`true`/`false`); `--oidc-only off` restores the two-button hosted UI. It
requires the provider trio (given now or already sticky) and fails the deploy
otherwise, because a client with no identity provider at all would lock
every user out.

Two things to keep in mind:

- **The local demo credentials stop working on the hosted UI.** The
  `adt-demo-password` users (including the `ADT` user) cannot sign in through
  the browser gate; `/api/*` keeps its own bearer/basic auth, so the
  CLI eval and smoke lanes are unaffected.
- **Pick one audience per surface.** If the same deployment must serve both
  corporate users and people who only have a local Cognito account, keep the
  default two-button mode, or deploy a separate surface for each audience.

### Optional `--quick-connector` flag (not sticky, default off)

`--quick-connector` adds the AWS Quick MCP connector path and nothing
else: the bridge-only AgentCore gateway `adt-quick-gw` with its
`query-bridge` target, and a copy of the `adt-quick-service` client secret in
the Secrets Manager secret `adt-quick-client-secret`. That client is Quick's
own and only `adt-quick-gw` accepts it. It does not deploy
`QuickApiGatewayStack`; `--quick-api` does, and it can be combined with it
([ADR-0093](adr/0093-quick-connector-in-the-workshop-package.md)):

```bash
./deploy.sh --profile <YOUR_PROFILE> --region <YOUR_REGION> --quick-connector
```

At the end of the deploy, `deploy.sh` waits for the `query-bridge` target to
be READY and prints what the Quick connector form asks for. With Service
authentication (the default) that is the MCP server URL, the token URL, the
client id, the scope and the command that reads the client secret (never the
secret itself); with delegated user tokens (below) it prints the MCP server
URL and a note that the external provider's values apply. The console steps are in
[quick-web-connector.md](quick-web-connector.md).

- **The Quick subscription is yours to set up.** The account needs an Amazon
  Quick Enterprise subscription, and you need a Quick user with the Author Pro
  or Admin Pro role, before you can create the connector. `deploy.sh` never creates either;
  subscribe the account in the Quick console first.
- **Not sticky.** Pass the flag on every deploy that should keep the
  connector. A deploy without `--quick-connector` or `--quick-api` stops
  before CDK while `adt-quick-gw` still has targets, and prints the commands
  that delete them. To remove the connector on purpose, follow the
  [rollback](quick-web-connector.md#rollback-fully-additive--clean-removal):
  delete the `query-bridge` target and the `adt_query_bridge` runtime, then
  deploy without the flag. `cleanup.sh` deletes the targets itself.

### Optional delegated user tokens on the AWS Quick gateway (sticky, needs `--quick-api` or `--quick-connector`)

The external-OIDC option above covers browser sign-in. A separate option
covers the dedicated Quick gateway: it can accept delegated (on-behalf-of)
user tokens from an external OIDC identity provider instead of the Cognito
service credential, so each assistant user's questions run as that user
([ADR-0079](adr/0079-quick-gateway-external-oidc-delegated-user-tokens.md)):

```bash
./deploy.sh ... --quick-api \
  --quick-oidc-discovery https://<issuer>/.well-known/openid-configuration \
  --quick-oidc-audience <QuickGatewayUrl>
```

Enabling it takes two steps, because the audience is the existing
`QuickGatewayUrl`. deploy.sh refuses to proceed before the gateway exists or
while the audience does not match it. Both values are sticky (SSM
`/adt/quick-oidc-*`); to switch the option off, delete them. The connector-form
values and the operator steps are in
[quick-web-connector.md](quick-web-connector.md#external-oidc-identity-provider-delegated-user-tokens).

### Optional `--query-mode` flag (sticky, default `agent`)

Controls how cross-system SQL is composed
([ADR-0035](adr/0035-system-agents-own-their-query.md) /
[ADR-0038](adr/0038-system-query-mode-default.md)):

```bash
./deploy.sh ... --query-mode agent          # Mode A (default): each system agent composes its own query
./deploy.sh ... --query-mode orchestrator   # Mode B: the orchestrator composes SQL centrally
```

One flag flips the whole stack (app, agent provisioning, and orchestrator
runtime all read the same value). Unlike the data flags, this one **is
sticky**: an explicit `--query-mode` persists to SSM, and later deploys
without the flag re-apply the persisted choice.

### Optional `--integrity-mode` flag (sticky, default `report-only`)

Sets the response-integrity posture
([ADR-0047](adr/0047-response-integrity-enforcement-is-opt-in.md)) by writing
`RESPONSE_INTEGRITY_MODE` into the app and every runtime: `annotate`
(recommended—model-authored answers pass through with a grounding sidecar and a
zero-evidence fabrication block) or `enforce` (the strict typed-evidence gate
replaces the answer). Any other value, including unset, resolves to the code
default `report-only`. Like `--query-mode`, an explicit value persists to SSM and
later deploys without the flag re-apply it.

### Optional `--auto-accept` flag (default `off`)

Self-learning: `--auto-accept on` lets high-confidence value-mapping
proposals apply without manual review. Off by default so a human reviews
every learned mapping; leave it off unless you are iterating on demo
content.

### Optional custom domain (sticky, default off)

By default the ADT app is served on the `*.cloudfront.net` name that Amazon
CloudFront assigns. To serve it on your own domain instead, provide the
domain plus the **Amazon Route 53 public hosted zone** (in the same account)
that is authoritative for it—all three flags together on the first enabling
deploy:

```bash
./deploy.sh --profile <YOUR_PROFILE> --region <YOUR_REGION> \
  --domain adt.example.com \
  --hosted-zone-id Z0123456789ABC \
  --hosted-zone-name example.com
```

This automatically:

- creates a **DNS-validated AWS Certificate Manager certificate** in
  `us-east-1` (the certificate Region CloudFront requires) in its own small
  `ADTCertificateStack`, wired into the app stack (the
  [one-time setup](#3-bootstrap-cdk-once-per-account-and-region) bootstraps
  `us-east-1`),
- attaches the domain as a CloudFront alternate domain name (SNI, TLS 1.2
  minimum), and
- creates the Route 53 **A/AAAA alias records** pointing at the distribution.

The first enabling deploy blocks a few minutes while ACM validates the
certificate through the zone. Afterwards the stack outputs
`ADTCustomDomainUrl` next to the existing `ADTCloudFrontUrl`—both serve the
app. The domain may be a subdomain of the zone or the zone apex itself;
partial configuration (for example a domain without the zone) fails the
deploy with a clear error rather than being silently ignored.

Like `--highbyte`, the configuration is **sticky**: it is recorded in AWS
Systems Manager (`/adt/custom-domain`, `/adt/custom-domain-zone-id`,
`/adt/custom-domain-zone-name`) and later deployments keep the domain even
if you omit the flags. To remove it, delete the three SSM parameters and
redeploy (this removes the alias and DNS records); then delete the orphaned
`ADTCertificateStack` in `us-east-1` manually.

#### Simulator and HighByte sibling custom domains

When a custom domain is configured, the optional human-facing distributions
receive CDK-managed sibling aliases as their stacks are enabled:

- `--simulator` provisions `erp.<custom-domain>` for the ERP dashboard.
- `--highbyte` provisions `highbyte.<custom-domain>` for the HighByte UI.

For each enabled sibling, CDK provisions the `us-east-1` certificate, attaches
the CloudFront alternate domain, and creates Route 53 A/AAAA aliases. The
Cognito callback updater registers both the raw `*.cloudfront.net` hostname and
the sibling alias as allowed callback and logout URLs, so sign-in works through
either URL. No console-side ACM, CloudFront, Route 53, or Cognito steps are
required, and the configuration does not introduce deployment drift. Base mode
does not deploy the ERP dashboard, so it has no `erp.<custom-domain>` alias.

### Why use `deploy.sh` rather than `cdk deploy`

`deploy.sh` is the supported path. It performs steps that `cdk deploy --all`
does **not**:

- builds and pushes the agent container image,
- creates the AgentCore Runtime,
- persists the READY orchestrator runtime ARN for the CDK-owned managed
  evaluations (section `[2c-eval]`—automatic, no flag, **non-fatal** on
  failure; the ARN persists to SSM `/adt/orchestrator-runtime-arn`
  and the *next* deploy's synth creates the
  `AWS::BedrockAgentCore::OnlineEvaluationConfig` resource together with the
  code-based figure-check evaluator and its Lambda function and the
  `EVAL_CONFIG_ORCHESTRATOR` app env—see
  [`agents-fleet-metrics.md`](agents-fleet-metrics.md#evaluation-scores-managed-agentcore-evaluations)),
- seeds the Neptune Analytics graph,
- deploys AWS IoT SiteWise models,
- invalidates the Amazon CloudFront cache.

Use raw `cdk deploy --all` only for iterating on CDK stack changes when you
know the runtime steps above are already in place:

```bash
cdk deploy --all -c region=<YOUR_REGION> --profile <YOUR_PROFILE> --require-approval never
```

## Connecting your own data sources

The default **base** deployment brings up the engine with no systems attached.
Connecting a real system is a **runtime onboarding** step, not a redeploy—the
knowledge graph stores *metadata* about each system (how to reach it, what its
fields mean, how identifiers translate across systems), never the data itself.

1. Open the app (the Amazon CloudFront URL printed by `deploy.sh`) and sign in.
2. Go to **Onboard** and choose **Connect New System**, then pick the connection
   type and enter its connection details (a PostgreSQL/Aurora connection string,
   an MCP server URL and optional token, an Amazon Athena workgroup and
   database with a read-only role, an AWS IoT SiteWise source (Direct or i3X),
   or a Tulip Tables API).
3. The onboarding wizard discovers the schema, proposes concept mappings, and—on
   accept—writes them to the graph. The orchestrator can then answer
   cross-system questions against your data.

**PostgreSQL needs a dedicated read-only login.** A system agent runs SQL that
its model writes, and that SQL can read whatever the login in your connection
string can read. Register a login made for this system and nothing else, with
CONNECT on the database, USAGE on the registered schema and SELECT on that
schema's tables, and no other rights: not a superuser, not the schema's owner,
not a member of another role, and no CREATE, INSERT, UPDATE, DELETE or
TRUNCATE anywhere. For example, as the schema's owner:

```sql
CREATE ROLE adt_reader_plant LOGIN PASSWORD '<password>';
GRANT CONNECT ON DATABASE <database> TO adt_reader_plant;
GRANT USAGE ON SCHEMA plant TO adt_reader_plant;
GRANT SELECT ON ALL TABLES IN SCHEMA plant TO adt_reader_plant;
ALTER DEFAULT PRIVILEGES IN SCHEMA plant GRANT SELECT ON TABLES TO adt_reader_plant;
```

Every login also gets what is granted to `PUBLIC`, so check that `PUBLIC` has
no SELECT on tables this login must not read. Onboarding refuses a login that
is a superuser or can create objects or change data in the registered schema:
preflight reports it as blocked, the onboarding chat's connection test fails,
provisioning stops before the system agent starts, and an Edit of the
connection to such a login is refused before it is saved, each with the grants
to use instead. When the login's privileges cannot be read, onboarding and the
Edit go ahead and warn that the login was not checked. It does not check what the login can read
outside the registered schema. The bundled demo systems need none of
this: their system agents already use a generated read-only login per schema
([ADR-0096](adr/0096-system-agents-read-with-their-own-login.md)).

**Amazon Athena needs a read-only role**, even for data in the app's own
account: no role of a deployed app can query Athena itself, so a registration
without a Role ARN is refused. Deploy
`stacks/cross-account/adt-data-readonly-role.yaml` in the account that holds the
data. `TrustedAccountId` (the ADT account) is required. `DataAccountId`
defaults to the account you deploy the role in. Set `S3TablesAccountId` (and the
other S3 Tables parameters) only when your tables are in Amazon S3 Tables, to the
account that owns the S3 Tables catalog, which is this account's ID when they
are here. Left empty, the role gets no S3 Tables access. For tables over a plain
Amazon S3 bucket, set `DataBucketName` to that bucket's name instead of editing the
role; left empty, the role gets no data-bucket access. For data in the app's
own account, deploy the role in that account with `TrustedAccountId` set to it.
Before redeploying over an existing role stack, read
[Updating an existing role stack](cross-account-onboarding.md#updating-an-existing-role-stack).
Then deploy the app with
`--cross-account-role convention` (or that role's ARN), and enter the role ARN
and its External ID in the form.

**Data in another AWS account** (Amazon Athena / AWS IoT SiteWise through an assumed
read-only role): onboarding stays a runtime step, but there is a **one-time
deploy prerequisite**—the app's roles need permission to assume the data
account's role. See the deploy-once checklist in
[docs/cross-account-onboarding.md](cross-account-onboarding.md#dynamic-onboarding-what-must-exist-ahead-of-time-deploy-once-checklist)
(In short: deploy once with `--cross-account-role convention`, have each data
account deploy the standard role template, then onboard any number of systems
through the UI with no further deploys).

**Network reachability:** your sources must be reachable from the app's VPC (the
Amazon ECS task and the AgentCore runtimes run in private subnets). For a source
in another VPC or on-prem, add the appropriate routing/peering/VPN and open the
relevant security group; for a managed database, place it where the app's
security group can reach it, and store credentials in AWS Secrets Manager.

**Connector types (current):** PostgreSQL/Aurora (SQL), MCP servers, Amazon
Athena, AWS IoT SiteWise (Direct, IAM-role), AWS IoT SiteWise (i3X, endpoint +
bearer token) and Tulip (Tables API, API key + secret)—all onboardable from **Onboard > Connect New
System**. A generic REST API (OpenAPI) source is listed in the form but cannot
be registered yet: this build cannot provision a generic REST connector. Adding
new *types* of systems is a planned extension of the
onboarding/registration flow.

---

## Deploying from a fresh clone

1. Clone the repository and enter its directory:

   ```bash
   git clone <REPO_URL>
   cd AgenticDigitalThread
   ```

2. Create a virtual environment and install the dependencies:

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   npm install
   ```

3. Configure the AWS CLI profile and Region:

   ```bash
   aws configure --profile <YOUR_PROFILE>          # or: aws configure sso --profile <YOUR_PROFILE>
   aws configure set region <YOUR_REGION> --profile <YOUR_PROFILE>
   ```

4. Enter the application directory and deploy the required mode:

   ```bash
   cd agentic_digital_thread
   ./deploy.sh --profile <YOUR_PROFILE> --region <YOUR_REGION>
   ```

   To include HighByte, add `--highbyte`. The first HighByte deploy also
   needs `--accept-highbyte-eula` (see
   [Accepting the HighByte EULA](#accepting-the-highbyte-eula)):

   ```bash
   ./deploy.sh --profile <YOUR_PROFILE> --region <YOUR_REGION> --highbyte --accept-highbyte-eula
   ```

The first deployment takes well over an hour (about 80 minutes). `deploy.sh` handles CDK bootstrap,
Systems Manager placeholders, stack deployment, AWS IoT SiteWise model
creation, agent image build and push, AgentCore Runtime creation, the ECS
update, and Amazon CloudFront invalidation.

After it finishes, complete the [post-deploy steps](post-deploy.md).
**Onboarding is required** before the platform can answer any question; the
HighByte MCP bearer token and AWS IoT SiteWise publishing are configured
automatically on a fresh deploy.

---

## Adding HighByte Intelligence Hub

Enable the optional OT/UNS layer with `--highbyte`:

```bash
./deploy.sh --profile <YOUR_PROFILE> --region <YOUR_REGION> --highbyte --accept-highbyte-eula
```

The HighByte container runs the **public** `highbyte/intelligence-hub` image
from [Docker Hub](https://hub.docker.com/r/highbyte/intelligence-hub)—no tar,
no build pipeline, and nothing to stage. The image is pinned by digest in
`stacks/_external_images.py`, so every task start runs the same image. This
repository does not redistribute the HighByte image or include a HighByte
license file.

### Accepting the HighByte EULA

The container runs only with the `ACCEPT_EULA=Y` environment variable (it
exits immediately without it). As HighByte's
[Docker guide](https://guide.highbyte.com/release/setup/docker/) states,
setting it means you agree to the
[HighByte Intelligence Hub End User License Agreement](https://www.highbyte.com/hubfs/highbyte_intelligence_hub_eula.pdf).
Read the agreement before you deploy HighByte. `deploy.sh` never accepts it
for you:

- `--accept-highbyte-eula` records your acceptance once, in AWS Systems
  Manager Parameter Store (`/adt/highbyte-eula-accepted`), and prints the
  agreement's link. Later deployments read the recorded acceptance and need
  no flag.
- A deployment that includes HighByte (`--highbyte`, or an account where
  HighByte is already enabled) without a recorded acceptance stops right
  after the credential check, before it installs or builds anything, writes
  to AWS or runs CDK, and prints the link.
- An account first deployed with `--highbyte` before this check existed has
  no recorded acceptance, so its next deployment stops until you pass
  `--accept-highbyte-eula` once.
- The CDK app sets `ACCEPT_EULA=Y` only from the explicit context value
  `highbyte_accept_eula=true` (`deploy.sh` passes it). A raw
  `cdk synth` or `cdk deploy` with HighByte enabled and without that value
  fails with the agreement's link.
- The recorded acceptance does not follow later changes to HighByte's terms.
  When HighByte publishes a new EULA, review it. To withdraw your acceptance,
  delete the parameter; the next HighByte deployment stops until you accept
  again.

You are also responsible for obtaining any HighByte license you need (see
[Licensing](#licensing)).

Enablement is **sticky**—once enabled, the choice is recorded in AWS Systems
Manager (`/adt/highbyte-enabled`) and later deployments keep the HighByte
stack even if you omit `--highbyte`.

### Licensing

HighByte boots in **trial mode** without a license—fine for the first-deploy
demo experience. To use the licensed features, import your license through the
HighByte UI after deployment (**Settings → License → Choose File**). See the
[HighByte licensing guide](https://guide.highbyte.com/release/setup/installation/licensing/)
and [`highbyte.md`](highbyte.md).

---

## Tuning the sample database

The Aurora Serverless v2 cluster that backs the simulated ERP / MES / CMMS /
PLM databases (deployed with `--seed` or `--simulator`) is configured through
the `database` context block in `cdk.json`:

```json
"database": {
  "database_name": "erp_simulation",
  "engine_version": "15.15",
  "min_capacity": 0.5,
  "max_capacity": 2,
  "backup_retention_days": 7,
  "deletion_protection": false
}
```

| Key | Default | Notes |
|-----|---------|-------|
| `database_name` | `erp_simulation` | Name of the initial database on the cluster. |
| `engine_version` | `15.15` | Aurora PostgreSQL engine version. |
| `min_capacity` | `0.5` | Aurora Serverless v2 floor, in ACUs (1 ACU ≈ 2 GiB memory). |
| `max_capacity` | `2` | Aurora Serverless v2 ceiling, in ACUs. |
| `backup_retention_days` | `7` | Automated backup retention. |
| `deletion_protection` | `false` | Off by default so `cleanup.sh` can tear the cluster down. Enable for any environment you want to protect from accidental deletion. |

> **Note:** `max_capacity` is a ceiling, not a floor—you pay only for the
> capacity actually consumed. The default of 2 ACUs is
> deliberate for a demo: it fits the bundled simulation comfortably and caps
> the cost blast radius of a runaway workload in an environment someone forgot
> to tear down. If your own onboarded data or query load hits the ceiling
> (sustained CPU or connection pressure on the cluster), raise `max_capacity`
> and redeploy—capacity changes are an in-place update, not a replacement.

Changes take effect on the next `./deploy.sh` run.

---

## Model and Region configuration

The default models are defined once in
`agentic_digital_thread/agents/runtime_env_contract.py` (the runtime env
contract), match the catalog defaults in `agents/model_catalog.py`, and are
read by `stacks/adt_compute_stack.py` for the ECS task. Both use Amazon
Bedrock global cross-Region inference profiles, the same id in every Region.

| Role | Environment variable | Default model |
|------|----------------------|---------------|
| Orchestrator | `BEDROCK_MODEL` | `global.anthropic.claude-sonnet-5-5` (Claude Sonnet 5.5) |
| System agents | `SYSTEM_AGENT_MODEL` | `global.anthropic.claude-haiku-5-5` (Claude Haiku 5.5) |

Claude Opus 5.5, Claude Sonnet 5 and Claude Haiku 4.5 are also selectable.
`SYSTEM_AGENT_MODEL=<profile-id> ./deploy.sh …` overrides the system-agent
default at deploy time (CDK context `system_agent_model`).

> **Note:** Every catalog model that has a global cross-Region inference
> profile resolves to it (`global.<model>`): every Claude model and Amazon
> Nova 2 Lite. Amazon Nova Lite and Nova Pro have none and use the Region's
> geo profile. To list the profiles your Region offers, run
> `aws bedrock list-inference-profiles --region <YOUR_REGION>` or see
> [Supported Regions and models for inference profiles](https://docs.aws.amazon.com/bedrock/latest/userguide/inference-profiles-support.html).

To change models, use the **LLM settings** page in the Admin panel
(`PUT /api/admin/llm-settings`). Both roles switch live — no redeploy and no
re-onboard is required; the orchestrator and every system-agent runtime pick up
the new model on their next invocation
([ADR-0055](adr/0055-bedrock-mantle-models-and-live-model-switching.md)). The
selectable models and their Amazon Bedrock list prices (estimates; see
[Amazon Bedrock pricing](https://aws.amazon.com/bedrock/pricing/) for current
rates) are catalogued in `agentic_digital_thread/agents/model_catalog.py`. The orchestrator default can
also be overridden at deploy time with `BEDROCK_MODEL=<model-id> ./deploy.sh …`
(passed through as CDK context `bedrock_model`).

> **Note:** Confirm the model is enabled in Amazon Bedrock for your account and
> Region before deploying. Using an inference profile from the wrong partition
> fails at runtime.

### Limiting the selectable models (optional, not sticky)

A deployment can limit which catalog models the **LLM settings** page offers,
for example to the models your organization has approved. Set
`ALLOWED_MODELS` to a comma-separated list of catalog family ids (the
`family` values in `model_catalog.py`, without the `us.`/`eu.`/`ap.` prefix
and with no spaces):

```bash
ALLOWED_MODELS=anthropic.claude-sonnet-5,anthropic.claude-haiku-4-5-20251001-v1:0 ./deploy.sh …
```

`deploy.sh` passes it through as CDK context `allowed_models`, which sets
`ALLOWED_MODELS` on the app's ECS task. With it set, each role is offered the
allowed models of its own catalog role (orchestrator or system agent), or
every allowed model when the list names none of that role. The models a role
may not run stay visible on the page but are greyed out as "not offered in
this deployment", and `PUT /api/admin/llm-settings` refuses them with HTTP 400
before anything is saved. With the example above, the orchestrator can run
only Claude Sonnet 5 and the system agents only Claude Haiku 4.5. When a
role's catalog default is not allowed, the default falls back to the first
allowed model. An id that is not in the
catalog fails the synth, and fails the app at startup if set by hand; so does
an allowlist that does not offer the deployment's own `BEDROCK_MODEL` or
`SYSTEM_AGENT_MODEL` for its role. Leave it unset for no restriction. Pass it on every
deploy that should keep it. When it is set, it is also written onto the
orchestrator, onboarding and system-agent runtimes, so a runtime that falls
back to the catalog default stays inside it.

The allowlist governs the models selectable on the Admin page (the
orchestrator and system-agent roles). Onboarding's helper model calls use the
system-agent model first and the orchestrator model as their fallback, so they
stay inside it too. The memory extraction model below is offered only where
the allowlist also names it.

### AgentCore Memory extraction model

AgentCore Memory turns conversations into the long-term Facts, Summaries and
Preferences records with its own model, set on the memory's strategies
(`stacks/agentcore_stack.py`). It is the third model on the Admin LLM settings
page.

| Deployment | Extraction model |
|------------|------------------|
| Base and demo (`deploy.sh`) | `global.anthropic.claude-haiku-5-5` (Claude Haiku 5.5) by default; Admin can change it |

On a base or demo deployment, Admin can pick one of the five models
AgentCore Memory accepted in a `us-east-1` comparison on 2026-10-26 (Claude
Haiku 5.5, Claude Sonnet 5.5, Amazon Nova Pro, Claude Sonnet 5 and Claude
Haiku 4.5; `MEMORY_EXTRACTION_FAMILIES` in `agents/model_catalog.py`).
Acceptance in other Regions has not been checked, and in that comparison Nova
Pro, Sonnet 5 and Haiku 4.5 sometimes wrote records in another language
despite the English instruction. Under `ALLOWED_MODELS` only the ones it names
are offered. The memory model has its own Save on the card, apart from the
orchestrator and agent models, because the switch takes about two minutes
(AgentCore settled one in about 131 s on 2026-10-27), longer than one request
through CloudFront may last (120 s). Saving starts a background switch
(`POST /api/admin/llm-settings/memory-model`, answered with a job id at once);
the card shows "Switching the memory to ..." while it polls the switch
(`GET /api/admin/llm-settings/memory-model/job/<job_id>`), then shows its
outcome, and you can leave the page meanwhile: a page opened while a switch
runs shows it running. `PUT /api/admin/llm-settings` refuses a
`memory_model` with 400. The page never waits for the app to load its own
configuration. Until the app has loaded it (just after a task starts), the
card says "Memory settings are still loading; reload the page in a moment."
while the app loads it in the background. The switch request checks only
the model, the allowlist and the workshop before it starts; the switch
itself finds the memory, and without one it ends with "AgentCore Memory is
not configured in this deployment". The switch changes the memory's strategies in place
and waits, up to 10 minutes, until the memory is active on the new model;
only the model changes, and it applies to new extractions while the records
already extracted stay. It then writes `/adt/llm-memory-model` in SSM, and
`deploy.sh` passes it back as `-c memory_extraction_model=<id>`, so the
choice persists across deploys. If the memory update fails, nothing is saved.
A retry on a memory that is FAILED or still updating is refused rather than
reported as unchanged, and one switch runs at a time across the app's tasks
(a second gets "another save is in progress"). When the app's shared
DynamoDB table cannot answer, the switch is refused ("could not be
coordinated; try again") rather than decided by one task alone: before it
starts (503), or, when the table will not take the job's record, as the
job's first step, before it reads or changes the memory. Right before it
sends UpdateMemory, and again right before it writes SSM, the switch shows it
still holds its claim; a switch that has lost it (another switch took it
over) sends neither and says so. The switch records how it
ended twice, in its job record and in the release of its claim, so every
app task reads the outcome even when one of the two writes did not land. A
switch with neither (its app task was replaced, or neither write landed) is
reported as unknown once its claim has been quiet for 5 minutes (a running
switch writes it at least every two minutes), never as running
or done; another switch can take the claim over 15 minutes after its last
write. When the card cannot read a switch's status it says so, keeps the
select locked while the switch was last seen running, and reads the
settings again so the select shows the model the memory runs. Every call
the switch makes to the shared table uses a bounded client (1 s connect,
3 s read, two attempts), and each of its requests makes at most two such
calls one after the other, so it answers within 30 s; the AgentCore and SSM
calls use 3 s / 10 s and two attempts, so a switch runs at most about 14
minutes. If the memory is not active on the saved model when the switch
reads it back, it reports an error and says SSM now holds that model.

What those bounds cover: time spent connecting, waiting for each part of
an answer, and retrying. What they do not: DNS resolution, a refresh of the
app task's credentials (or a wait for another thread's refresh), and an
answer that keeps arriving in small parts; nor the app's configuration
discovery, which the switch runs only if the app had not loaded its
configuration yet. These gaps are shared by every AWS call the app makes
(chat, onboarding, every admin page), so the 30 s and 14 minutes are bounds
under the failures the clients handle, not wall-clock limits. The switch's
safety does not rest on them: it shows it holds its claim immediately before
each change. The remaining window is a change already sent (UpdateMemory or
the SSM write) that stalls for more than the 15-minute takeover bound after
that check: another switch can then start while the stalled call is still in
flight, and the stalled call's late result is neither undone nor reported.

The card compares the model the memory runs with what the next deploy would
put on it. If the saved model, as the next deploy resolves it for this
Region, differs (for example after a change outside the page), the card says
so under the select; a saved value that is not a memory model this
deployment offers is named as one the next deploy ignores. With nothing valid
saved, the next deploy that updates the memory puts the default back, so a
memory running another model is named with that default ("nothing is saved
for the next deploy, which would put ... back"). Saving the model the memory
already runs is the repair: it writes only the saved value, with no switch.
`deploy.sh` stops before CDK when it cannot read `/adt/llm-memory-model`
(AccessDenied, expired credentials) rather than put the default back; a
parameter that does not exist keeps the default, and setting
`MEMORY_EXTRACTION_MODEL` deploys without reading it. The workshop shows its model
read-only, and a workshop synth refuses the context. Without a memory (local
mode), the select is read-only too.

`MEMORY_EXTRACTION_MODEL=<model-id>` set when you run `deploy.sh` still wins
over the saved model on every deployment type. The model must be one
AgentCore Memory accepts in your Region.

---

## Upgrading an existing deployment

Run `deploy.sh` with the same data flags as before (data flags are not
sticky). Steps that apply once, to a deployment created before the change
they describe, follow.

### Memory extraction model (nothing to do)

From this release a base or demo deployment extracts memory records with
Claude Haiku 5.5 instead of Nova Pro
([AgentCore Memory extraction model](#agentcore-memory-extraction-model)).
The next deploy updates the memory's strategies in place: the memory and the
records already extracted stay, and conversations from then on are extracted
by the new model. A workshop deployment is unchanged.

### System agents' read-only database logins (one time)

From this release a system agent on the bundled demo databases connects with
its schema's read-only login, `adt_reader_<schema>`, instead of the database
owner ([ADR-0096](adr/0096-system-agents-read-with-their-own-login.md)). A
fresh deployment needs nothing. On an existing one:

1. **Deploy the update** (`./deploy.sh --profile <P> --region <R> --simulator`,
   or your usual flags). The stack update creates the logins; it does not
   re-run the seed or the ERP initialisation and changes no data. Systems
   keep answering on the login they started with.
2. **Move each onboarded demo PostgreSQL system to its login.** For each
   system, open **Onboard**, select the system, choose **Edit connection**,
   leave the host and port as shown and the password blank, and choose
   **Save & Reconnect** (as an Admin, the same as
   `PUT /api/admin/connection/<system-id>` with the body `{}`). An Edit that
   changes nothing writes nothing to the system's stored connection; it
   rewrites the system's `adt/agent-config/<system-id>` secret with the
   schema's read-only login, keeps the generated prompt, table list, concept
   mappings and business context, and deploys a new version of the system's
   runtime, which reads the login when it starts. No separate restart is
   needed. The new version takes about one to two minutes to start; move one
   system at a time. Nothing is re-onboarded, so mappings, rules and
   proposals stay as they are.
3. **Check:** `PYTHONPATH=. python3 scripts/audit_runtime_env.py --region <R>
   --reader-logins` (from `agentic_digital_thread/`) lists every PostgreSQL
   system with its login; a system still marked "the deployment's database
   login" has not been moved. The UI's "Reconnected" means the Edit was
   accepted; the API answer's `reconnected` field and this check say whether
   the runtime was moved.

A system that does not move is logged by the app as
`resync_cloud_connection: <system-id> not refreshed: ...`. The usual cause is a
schema the stacks create no login for (add it to `SEED_READER_SCHEMAS` in
`stacks/adt_seed_data_stack.py` or `ERP_READER_SCHEMAS` in
`stacks/erp_database_stack.py` and deploy again). `scripts/reonboard.py
--systems <system-id>` also moves a system, but it re-provisions it from
scratch and regenerates its generated mappings, so prefer the Edit.

### Your own PostgreSQL systems on a privileged login (one time)

From this release onboarding refuses a PostgreSQL login you registered that is
a superuser or can create objects or change data in its schema (see
[Connecting your own data sources](#connecting-your-own-data-sources)). A
fresh deployment needs no upgrade step, but must use a dedicated read-only
login when it onboards your own databases. On an existing one, a system you
onboarded before this release on such a login keeps its system agent runtime
and keeps answering on that login: the check runs in onboarding preflight and
the chat's connection test, on an Edit of the connection, at provisioning and
agent start, and when the app restores a system after a restart, not on a
runtime that is already serving. To move it:

1. Create a read-only login for the system's schema (CONNECT on the database,
   USAGE on the schema, SELECT on its tables, nothing else).
2. Open **Onboard**, select the system, choose **Edit connection**, enter the
   new login and password, and choose **Save & Reconnect** (as an Admin, the
   same as `PUT /api/admin/connection/<system-id>`). The Edit checks the new
   login before it saves it, then tries to deploy a new version of the
   system's runtime with it.
3. **Check that the runtime moved.** The UI's "Reconnected" means the Edit was
   accepted, not that the runtime was replaced. The API answer's `reconnected`
   field must be `true`, or `PYTHONPATH=. python3 scripts/audit_runtime_env.py
   --region <R> --reader-logins` (from `agentic_digital_thread/`) must show the
   system on the new login. If neither does, run the Edit again or remove the
   system.

If you would rather not keep the system, remove it from **Onboard** instead.

### Demo databases admit PostgreSQL from their clients only (automatic)

The ERP Aurora cluster and the seed database now admit port 5432 only from
the security groups of the resources that connect to them, not from the
whole VPC (`stacks/_db_access.py`). Most of those resources' rules live in
stacks that update after the database stacks, so a deployment created before
this change is updated in two phases on the database stacks'
`DatabaseCidrIngress` parameter (`stacks/_db_cidr_ingress.py`), decided per
database stack:

1. every stack. A database stack from before this change (no
   `DatabaseCidrIngressState` output) or one that reports `keep` is deployed
   with `keep`: it keeps the VPC-wide rule it already has while each client
   rule is created. A database stack at `remove`, or a new one, is deployed
   with `remove` and never gets the rule;
2. only the database stacks deployed with `keep`, now with `remove`: the
   VPC-wide rule goes. Nothing else changes; a failed update rolls back to
   the rule.

`deploy.sh` reads each database stack before it changes anything and runs
phase 2 only when phase 1 kept a stack. It stops, with nothing changed, when
it cannot read a database stack's state (an access error, a stack
mid-operation or in a failed state, an unexpected value): there is no
override, since `cdk deploy` needs the same access and cannot update such a
stack either. It fails at its end while a database stack reports anything
but `remove`. A fresh deployment needs one deploy. With raw `cdk deploy`,
pass `--parameters <stack>:DatabaseCidrIngress=keep` only for a database
stack that has no `DatabaseCidrIngressState` output or reports `keep` (and
`remove` for any other), then deploy those stacks alone with `remove`:
`cdk deploy` keeps a stack's previous parameter value when none is given.

---

## Deploying to a new Region or account

The repository is self-contained. To target a different Region or account:

1. Confirm the default models (see the table above) are enabled in Amazon
   Bedrock for the target Region, or choose different ones from the Admin
   panel's LLM settings after the first deploy.
2. Bootstrap CDK in the new account/Region (`npx cdk bootstrap`).
3. Run `deploy.sh` as shown above.

### External dependencies summary

| Asset | Source | Required? |
|-------|--------|-----------|
| HighByte image | Public `highbyte/intelligence-hub` (Docker Hub); runs with `ACCEPT_EULA=Y` once you accept the HighByte EULA (`--accept-highbyte-eula`) | Pulled automatically when HighByte is enabled (`--highbyte`). |
| HighByte license | Imported through the HighByte UI post-deploy | Optional (trial mode otherwise). |
| HighByte config | `config/highbyte/intelligencehub-configuration.json` (in repo) | Included. The setup Lambda function substitutes per-Region values. |
| pg8000 layer (PostgreSQL client) for the seed Lambda function | First-party CDK file asset built from the source-controlled Python 3.12 hash lock and `pgdriver.py` in `lambda-layers/pg8000/` | Built and staged automatically when seed data is enabled. |
| CloudFront authorization SAR app | AWS Serverless Application Repository app from account `520945424137`—the AWS-published Serverless Application Repository publisher account | Used by the ERP dashboard, HighByte UI, and the ADT app when edge auth is enabled; version `2.3.2`, centralized through `stacks/_edge_auth.py`. |
| OPC UA datasets | `highbyte/datasets/site{1,2,3,4}_*.csv.gz` | Generated deterministically (gzip) by `highbyte/generate_datasets.py`—`deploy.sh --highbyte` runs it before `cdk` when missing; `cdk synth` itself never writes into the checkout. Baked into the OPC UA server image (context `highbyte/`, allowlist `highbyte/.dockerignore`). |

---

## Next steps

- [Post-deploy steps](post-deploy.md)—MCP token, SiteWise pipeline,
  onboarding, and sign-in credentials (**required**).
- [Operations](operations.md)—cache invalidation, teardown, and
  troubleshooting.
- [HighByte integration](highbyte.md)—deep reference for the OT/UNS layer.
