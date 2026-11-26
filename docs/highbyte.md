<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# Integrate HighByte (OT/UNS layer)

The optional OT/UNS layer: the HighByte Intelligence Hub, four simulated OPC
UA servers, the Unified Namespace, and the AWS IoT SiteWise pipeline.
Terminology follows [`glossary.md`](glossary.md). To deploy it, see
[`deployment.md`](deployment.md#adding-highbyte-intelligence-hub); for the
required first-run configuration, see [`post-deploy.md`](post-deploy.md).

> HighByte is licensed third-party software. The container image is the public
> `highbyte/intelligence-hub` image from
> [Docker Hub](https://hub.docker.com/r/highbyte/intelligence-hub); this repository
> does **not** redistribute the HighByte image or include a HighByte license file.
> The container runs only with `ACCEPT_EULA=Y`, and setting it agrees to the
> [HighByte Intelligence Hub End User License Agreement](https://www.highbyte.com/hubfs/highbyte_intelligence_hub_eula.pdf),
> so the first HighByte deploy needs your explicit `--accept-highbyte-eula`
> (recorded once; a HighByte deploy without it stops with the link). You are
> responsible for obtaining any required HighByte license. An administrator
> uploads the license through the HighByte UI after startup (the hub boots in
> trial mode without one).

---

## What this layer deploys

`ADTHighByteOpcuaStack` provisions the items below. `deploy.sh` keeps them all
in this one stack. A direct CDK deploy that passes both
`-c highbyte_enabled=true` and `-c opcua_servers_stack=true` splits them the
same way (see [Architecture](architecture.md)).

- **HighByte Intelligence Hub**—an industrial data-integration platform on
  Amazon ECS on AWS Fargate, reachable through Amazon CloudFront over HTTPS. It
  runs the unmodified public `highbyte/intelligence-hub` image (with
  `ACCEPT_EULA=Y` once you accept the HighByte EULA); a small init container
  seeds the HighByte settings to
  Amazon EFS on first boot so the MQTT broker, REST Data Server, and MCP server
  are enabled (the image's defaults leave REST/MCP disabled).
- **Four OPC UA servers**—simulated industrial telemetry for Milwaukee,
  Detroit, Cedar Rapids, and Indianapolis. The Indianapolis server carries the
  `5-Axis-Milling-CMX-441` degradation cycle live
  ([ADR-0083](adr/0083-indianapolis-fourth-site.md)).
- **MCP server**—a Model Context Protocol endpoint for AI integration.
- **Amazon EFS**—persistent storage for HighByte configuration.
- **AWS Lambda function**—configures HighByte through its REST API (admin password,
  project import, per-Region substitution).
- **AWS IoT SiteWise**—the asset hierarchy that stores telemetry
  (Enterprise → Sites → Areas → Lines → Processes).

---

## Ports

| Port | Service | Access path |
|------|---------|-------------|
| 45245 | HighByte UI | Amazon CloudFront (HTTPS, AWS WAF + Amazon Cognito Authorization@Edge) → VPC origin → internal ALB |
| 8885 | MCP server | Amazon CloudFront (HTTPS, AWS WAF; no edge auth—machine path) → VPC origin → internal ALB; unauthenticated until a bearer token is configured (see [post-deploy](post-deploy.md#4-configure-the-highbyte-mcp-bearer-token)) |
| 26543 | OPC UA | Internal network load balancer (not internet-facing) |

> The HighByte ALB is **internal** (no public IP) and fronted by Amazon
> CloudFront **VPC origins**—one per port (UI 45245, MCP 8885). Amazon
> CloudFront is the only entry point: the ALB accepts traffic only from the
> CloudFront service-managed security group (`CloudFront-VPCOrigins-Service-SG`,
> looked up at deploy time) and, for first-run configuration, the in-VPC setup
> Lambda function. Because the ALB is inside the VPC, the setup function can reach it to
> set the admin password and import the project on deploy—no public exposure
> of the load balancer at any point.
>
> The distribution carries an AWS WAF web ACL, and the **UI paths require
> Amazon Cognito sign-in at the edge** (Authorization@Edge; user `ADT` with the
> shared `adt-demo-password`). The machine paths (`/mcp*`, `/i3x*`, `/i3X*`,
> `/data/*`) deliberately carry **no edge authentication**—the Amazon Bedrock AgentCore
> gateway and other machine clients authenticate with their bearer token at
> the origin.

---

## Stack outputs

| Output | Description |
|--------|-------------|
| `HighByteDistributionURL` | Amazon CloudFront URL for the HighByte UI. |
| `HighByteCustomDomainUrl` | Preferred HighByte UI URL when a custom ADT domain is configured (`https://highbyte.<custom-domain>`); absent without custom-domain configuration. |
| `HighByteUiUserPoolId` | Amazon Cognito user pool id used by HighByte Authorization@Edge. |
| `HighByteUiUserPoolDomain` | Amazon Cognito hosted-UI domain used by HighByte Authorization@Edge. |
| `HighByteEndpoint` | HighByte endpoint through Amazon CloudFront. |
| `MCPEndpoint` | MCP server endpoint (use with a bearer token). |
| `OpcUaEndpoint`, `OpcUaEndpoint2`, `OpcUaEndpoint3`, `OpcUaEndpoint4` | OPC UA endpoints—Milwaukee, Detroit, Cedar Rapids, Indianapolis. |
| `OpcUaServerHost` | OPC UA server hostname (for manual connection). |
| `HighBytePasswordSecretArn` | AWS Secrets Manager **ARN** for the HighByte admin password (username `administrator`). The plaintext password is never emitted as an output. |
| `SetupLambdaArn` | ARN of the setup function—invoke it to reconfigure HighByte (see [Reconfiguring HighByte](#reconfiguring-highbyte)). |
| `MCPInspectorCommand` | Convenience command for launching MCP Inspector to test the MCP endpoint. |

The HighByte outputs are always on `ADTHighByteOpcuaStack`. When the servers
run in `ADTOpcuaServersStack`, `OpcUaEndpoint*` and `OpcUaServerHost` are
outputs of that stack instead.

Retrieve the admin password from Secrets Manager (see
[`post-deploy.md`](post-deploy.md#2-retrieve-sign-in-credentials))—it is the same
`adt-demo-password` value used across the demo. When custom-domain support is
enabled, use the `HighByteCustomDomainUrl` output
(`https://highbyte.<custom-domain>`) for human access; the raw CloudFront URL
remains valid and is registered with Cognito as well.

---

## MCP server

The HighByte MCP server exposes API-triggered HighByte pipelines as MCP tools,
letting AI applications query industrial data directly. The setup function
**auto-creates an MCP API key on deploy** and stores it in the
`adt/conn/highbyte` secret (JSON `{"token": ...}`, which the ADT app
reads), so the
endpoint is authenticated out of the box. To rotate or recreate the key
manually, see [`post-deploy.md`](post-deploy.md#4-configure-the-highbyte-mcp-bearer-token).

### Unified Namespace (UNS)

The Unified Namespace in HighByte Intelligence Hub consolidates:

- **Telemetry**—real-time sensor and equipment data from OPC UA. Always
  present; the four HighByte OPC UA servers are self-contained (synthetic data).
- **ERP data**—products and inventory, read live from the ERP simulator's
  PostgreSQL (the `Products` input). Present **only when `--simulator` is deployed**.
- **CMMS data**—a maintenance work-order count, read live from the seed
  PostgreSQL (the `WorkOrderCount` input). Present **only when `--seed`/`--simulator`
  is deployed**.

> **The demo ERP/CMMS connections are conditional.** Those two inputs are JDBC
> reads against the demo PostgreSQL databases, wired automatically by the setup
> Lambda function **only when the backing seed/simulator stack is deployed**. On a
> `--highbyte` deployment *without* `--seed`/`--simulator`, the setup function omits
> those connections and inputs entirely, leaving a clean OPC UA → SiteWise/MCP
> namespace; add your own ERP/CMMS connection (with its query) through the HighByte
> UI to restore that correlation. The OPC UA → SiteWise pipeline is unaffected.

The Namespace is itself an MCP tool, so AI applications query current OT state
through a single call. The ADT orchestrator uses it for **current-state**
questions; for history and trends it uses AWS IoT SiteWise.

### Testing with MCP Inspector

```bash
npx @modelcontextprotocol/inspector@1.0.5
```

1. Select **Streamable HTTP** as the transport.
2. Paste the `MCPEndpoint` output value into the **URL** field.
3. Under **Authentication**, add a header—name `Authorization`, value your
   HighByte API token.
4. Choose **Connect**.

Get the endpoint:

```bash
aws cloudformation describe-stacks --stack-name ADTHighByteOpcuaStack \
  --query "Stacks[0].Outputs[?OutputKey=='MCPEndpoint'].OutputValue" \
  --output text --profile <YOUR_PROFILE> --region <YOUR_REGION>
```

---

## AWS IoT SiteWise pipeline

The asset model schema is deployed before the pipeline can write telemetry.
Models are pre-defined with full measurement schemas and correct data types
(DOUBLE for pump sensors, INTEGER for counts, BOOLEAN for state). The
`NameSpace_to_SiteWise` pipeline runs with `create: true` and creates all
roughly 170 assets itself on the first cycle, then writes telemetry every five
seconds. No separate asset-creation script is needed.

For the enablement procedure (deploy models → enable trigger), see
[`post-deploy.md`](post-deploy.md#5-enable-highbyte-aws-iot-sitewise-publishing).

> **A non-HighByte path into SiteWise also exists.** Every `--simulator`
> deploy (no `--highbyte` needed) includes the `erp-sitewise-degradation-feed`
> Lambda function, triggered by the ERP dashboard's "Start machine degradation feed"
> button: it creates the `5-Axis-Milling-CMX-441` asset from the `CNC` model
> (the models are deployed unconditionally by `deploy.sh`) and backfills a
> 72-hour degrading vibration + coolant-temperature trend. The HighByte
> OPC UA → UNS → SiteWise pipeline remains the production-shaped path, and
> since [ADR-0083](adr/0083-indianapolis-fourth-site.md) the Indianapolis
> OPC UA server emits that same CMX-441 cycle live; the button is a fallback
> slated for removal once the live path is validated. The button is not live
> telemetry: it writes that one asset once per click, so a plain `--simulator`
> deploy has no live plant-line telemetry in SiteWise. That needs `--highbyte`
> or `--sfc` (SFC reads the same OPC UA servers without HighByte). See
> [`architecture.md`](architecture.md#sitewise-degradation-feed).

### Asset hierarchy

```
Enterprise (AnyCompany)
  └─ Sites (Milwaukee, Detroit, Cedar Rapids, Indianapolis)
       └─ Areas (Bottling, AutoParts, FoodProcessing, Machining)
            └─ Lines (Line1, Line2, Line3)
                 └─ Processes (per model type)
                      Washer, WasherPump, FillerPump, BottlingMachine,
                      CNC, Assembly, Mixer, Extruder, Oven, ...
```

### Model-type assignment by site

Model types are determined by OPC UA data content (the transform's
`getModel()` function):

| Site | Equipment models |
|------|------------------|
| Milwaukee (Bottling) | Washer, FillerPump, BottlingMachine, and so on |
| Detroit (AutoParts) | CNC, Assembly, HeatTreat, and so on |
| Cedar Rapids (Food) | Mixer, Extruder, Oven, Cooler, and so on |
| Indianapolis (Machining) | CNC (`Line3CNC1` is `5-Axis-Milling-CMX-441`), QualityInspect, Packaging |

### Key implementation details

- **Models pre-define every measurement** (counters, pump fields, environment
  sensors, OEE), so HighByte never mutates the schema at runtime.
- **The transform uses hierarchy-name keys plus arrays** (`Sites: [...]`,
  `Lines: [...]`, `Processes_Washer: [...]`). The keys match existing model
  hierarchies, so HighByte associates assets without creating new hierarchies
  (which would hit the per-model hierarchy limit).
- **The pipeline runs with `create: true` continuously**—it creates assets on
  the first cycle, then only writes telemetry.

---

## Reconfiguring HighByte

Re-run the setup function to reapply the admin password, project import, and
Region substitution:

```bash
aws lambda invoke --function-name highbyte-setup /dev/stdout \
  --profile <YOUR_PROFILE> --region <YOUR_REGION>
```

The setup function re-applies the admin password and project import, and on a
fresh install auto-creates the MCP API key and stores it in Secrets Manager
([`post-deploy.md`](post-deploy.md#4-configure-the-highbyte-mcp-bearer-token)).

Configuration knobs:

- The HighByte image is pinned by digest in `stacks/_external_images.py`
  (currently the linux/amd64 manifest of `highbyte/intelligence-hub:4.5.3`),
  together with the settings-seed BusyBox image. The hub task runs
  `<repository>@<digest>`, never a tag, because a tag can move and ECS pulls
  the image at every task start. To track a different published tag, read its
  digests from the registry and change them there.
- The settings-seed init container runs on BusyBox from Amazon Elastic
  Container Registry Public (Amazon ECR Public), which your own account pulls
  by digest at every task start. This project does not ship, copy or
  redistribute BusyBox.
- `-c config_url=<url>` CDK context (not set in `cdk.json` by default)—optional URL
  to a HighByte project config to fetch at deploy time; otherwise the bundled
  `config/highbyte/intelligencehub-configuration.json` is used.

---

## Troubleshooting

Issues specific to the HighByte OT/UNS layer.

### HighByte and ECS

Tail the HighByte container logs. The task definition uses the `awslogs` driver
with stream prefix `HighByteService` and no explicit log group, so CDK generates a
stack-scoped log-group name—look it up by the stack prefix first:

```bash
aws logs describe-log-groups --log-group-name-prefix ADTHighByteOpcuaStack \
  --query 'logGroups[].logGroupName' --output text --profile <YOUR_PROFILE> --region <YOUR_REGION>
aws logs tail <log-group-name-from-above> --follow --profile <YOUR_PROFILE> --region <YOUR_REGION>
```

Open a shell in the running container:

```bash
TASK_ID=$(aws ecs list-tasks --cluster <YOUR_CLUSTER_ARN> --service-name <YOUR_HIGHBYTE_SERVICE> \
  --query 'taskArns[0]' --output text | cut -d'/' -f3)
aws ecs execute-command --cluster <YOUR_CLUSTER_ARN> --task "$TASK_ID" \
  --container highbyte --interactive --command "/bin/bash"
```

### AWS IoT SiteWise pipeline errors

| Error | Cause | Fix |
|-------|-------|-----|
| `Cannot create property X` | HighByte is adding a property that already exists with a different ID. | Verify the transform payload omits `_path` and `_hierarchyName`. |
| `LimitExceededException: too many hierarchies` | The transform is adding per-asset-name hierarchies to a model. | Verify it uses hierarchy-name keys (`Sites`, `Areas`, `Lines`, `Processes_<Model>`), not asset-name keys. |
| `does not match model type` | An asset references a model whose ID changed (for example after the models were redeployed). | Re-run `highbyte/DeploySitewiseModels.py` (idempotent). If the asset hierarchy is corrupted, reset it with `highbyte/cleanup_sitewise.py` and then re-run `DeploySitewiseModels.py` (see [operations.md](operations.md#resetting-aws-iot-sitewise)). |
| Assets created without a parent | Transform output is not nested under a parent key. | Use the validated transform from `highbyte/pipeline_sitewise.json`. |
