<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# Operations

Day-two tasks—the routine operational work that follows the initial
deployment: refreshing the app, tearing the environment down, and
troubleshooting. Terminology follows [`glossary.md`](glossary.md).

---

## Invalidating the Amazon CloudFront cache

After changing any static file, invalidate the distribution:

```bash
aws cloudfront create-invalidation \
  --distribution-id <YOUR_DISTRIBUTION_ID> \
  --paths "/*" \
  --profile <YOUR_PROFILE> --region us-east-1
```

`agentic_digital_thread/deploy.sh` does this automatically at the end of a deploy.

---

## Post-deploy verification

Three gates verify a deployed environment end to end. Run all of them after
every deploy (and after any re-onboarding):

**Grounding gate**—asks every catalog prompt through the live chat path and
asserts grounded, cross-system answers:

```bash
python3 scripts/smoke_prompts.py --base-url <YOUR_URL> --password $ADT_PASSWORD [--strict]
```

Expected: exit 0 with every prompt grounded and the suite-level cross-system
proof satisfied. `--strict` additionally fails on RuntimeUnavailable and
fabrication-fallback. A JSON report is written by using `--report` (or
`SMOKE_REPORT`).

**Learning-health gate**—audits the knowledge graph's learned artifacts and
the proposal pipeline over HTTP:

```bash
python3 scripts/audit_learning.py --base-url <YOUR_URL> --password $ADT_PASSWORD [--strict] [--report /tmp/audit.json]
```

Expected: exit 0 with 0 duplicate-content groups (the census uses
table-qualified join keys), 0 legacy index-keyed live ids, 0 forbidden
pairings, and 0 auto-accept violations. `--strict` additionally fails on
cross-site pendings lacking a `review_reason` and on usage-ledger starvation
(unless the report marks it `expected-gap` for a report-only environment).
Quarantine growth and pending-age distribution are reported but never gate.
See [`adr/0052-learning-health-gate-evidence-ledger-and-authority-vouching.md`](adr/0052-learning-health-gate-evidence-ledger-and-authority-vouching.md)
for the probe and exit-code contract.

**Golden eval lane**—runs the golden questions through the live chat path
and scores each answer against pinned deterministic expectations (the LLM
never produces a verdict):

```bash
python3 scripts/eval_answers.py --base-url <YOUR_URL> --password $ADT_PASSWORD [--strict] [--report /tmp/eval.json] [--previous <YOUR_PRIOR_REPORT.json>]
```

Expected: exit 0 with every golden question's expectations met. `--previous`
diffs against a prior report as an informational trend (it never gates); keep
each deploy's `--report` JSON as the next deploy's baseline. The lane
complements the grounding and learning-health gates—it never replaces
either. See
[`adr/0053-preflight-drift-tombstones-and-cost-honesty.md`](adr/0053-preflight-drift-tombstones-and-cost-honesty.md).

**Manual drift check (optional)**—trigger a schema-drift re-discovery
across onboarded systems and poll the job to completion:

```bash
# POST {} for all onboarded systems, or {"system_id": "<id>"} for one
curl -s -X POST <YOUR_URL>/api/admin/learning/drift-check -d '{}' \
  -H 'Content-Type: application/json' --cookie "$SESSION_COOKIE"   # → {"job_id": ...}
curl -s <YOUR_URL>/api/admin/learning/drift-check/<YOUR_JOB_ID> --cookie "$SESSION_COOKIE"
```

Expected: every system reports `ok` or an honest `skipped:<reason>`—a
zero-drift result is a pass. Any drift found files a `schema_drift` proposal
into the steward queue; drift detection never mutates the graph, and
re-onboarding remains the steward's explicit fix (see
[`adr/0053-preflight-drift-tombstones-and-cost-honesty.md`](adr/0053-preflight-drift-tombstones-and-cost-honesty.md)).
A scheduled check also runs daily by default
(`DRIFT_CHECK_INTERVAL_SECONDS`, `0` disables).

> **Note:** Onboarding runbook—preflight. When onboarding (or
> re-onboarding) a system, the onboarding wizard now runs a connection
> preflight before discovery: `blocked` means the connection or AssumeRole
> probe failed (fix the connection before proceeding); `degraded` means the
> connection works but sample reads or Amazon Athena physical-column discovery
> would silently degrade—the warning lists the affected tables so the
> failure is visible *before* the discover and map steps, not after wrong answers. See
> [`adr/0053-preflight-drift-tombstones-and-cost-honesty.md`](adr/0053-preflight-drift-tombstones-and-cost-honesty.md)
> for the verdict semantics.

> **Note:** Data flags are not sticky on redeploys. An environment that
> should keep its demo data must re-pass its `--seed` / `--simulator` flags
> on **every** `deploy.sh` run; omitting them removes the simulator stacks,
> including the Amazon Aurora data. `--highbyte` is sticky: the deployment
> records it in AWS Systems Manager (`/adt/highbyte-enabled`), and HighByte
> stays deployed until that parameter is removed.

---

## Tearing down the environment

To destroy the environment—for example before a clean redeploy:

```bash
./cleanup.sh --profile <YOUR_PROFILE> --region <YOUR_REGION>
```

The CDK bootstrap stack (`CDKToolkit`) is intentionally left in place so the
next deploy doesn't need to re-bootstrap. To remove it as well, delete it
manually (`aws cloudformation delete-stack --stack-name CDKToolkit`) after
emptying its staging bucket.

`cleanup.sh` destroys resources in the correct order:

1. **Amazon Bedrock AgentCore Runtimes**—deleted first; they create VPC elastic network
   interfaces that block security-group deletion.
2. **AgentCore Gateway targets**—removed so the gateway can be deleted.
3. **AWS IoT SiteWise assets and models**—with `cleanup_sitewise.py`.
4. **CDK stacks**—deleted in reverse dependency order.
5. **Orphaned resources**—retained security groups and unassociated
   Elastic IPs.
6. **AWS Systems Manager parameters**—the `/adt/*` parameters
   created by `deploy.sh`—and the agent-config / MCP-token secrets.

> **Important:** Do not use `cdk destroy --all`. CDK is unaware of the AgentCore Runtimes
> and gateway targets that `deploy.sh` creates at runtime. Those hold elastic
> network interfaces in the VPC security groups, so `cdk destroy` hangs on
> security-group deletion. Use `cleanup.sh`.

### What to expect

A full teardown takes roughly **40–60 minutes**, dominated by three slow
deletions that `cleanup.sh` waits through:

- **HighByte Amazon CloudFront distribution + VPC origins** (~20–30 min). Because the
  HighByte stack imports the database endpoint, the script fully waits for it
  before deleting the databases; otherwise AWS CloudFormation cancels the database
  delete (*"Cannot delete export … as it is in use by ADTHighByteOpcuaStack"*). If
  a delete is canceled this way, the script re-issues it automatically once the
  importing stack is gone.
- **Amazon Neptune Analytics graph** in `ADTDatabaseStack` (~15–25 min).
- **Amazon Aurora cluster** in `ADTERPDatabaseStack` (~10 min).

One leftover class is **expected and harmless**: each enabled
Authorization@Edge distribution owns replicated AWS Lambda@Edge functions
(`CheckAuth`, `ParseAuth`, `RefreshAuth`, `SignOut`, `HttpHeaders`) that Lambda
refuses to delete until CloudFront removes their replicas—typically a few
hours. This can leave `ADTERPDashboardStack-LambdaEdgeAuth-*`,
`ADTComputeStack-LambdaEdgeAuth-*` (when ADT edge auth is on), and
`ADTHighByteOpcuaStack-LambdaEdgeAuth-*` nested stacks in `DELETE_FAILED`.
`cleanup.sh` handles this state generically by retaining the `LambdaEdgeAuth`
resource so each parent deletes cleanly. The leftovers do not block a redeploy
(a new deployment creates freshly named functions) and become deletable after
the replicas clear.

---

## Edge auth

Amazon Cognito Authorization@Edge is on by default for the ADT browser distribution.
Change the sticky setting only through the supported deploy path:

```bash
cd agentic_digital_thread
./deploy.sh --profile <YOUR_PROFILE> --region <YOUR_REGION> --edge-auth off
./deploy.sh --profile <YOUR_PROFILE> --region <YOUR_REGION> --edge-auth on
```

After an edge-on deployment, verify the browser/API boundary independently:

```bash
curl -sI https://<domain>/          # 307; Location host ends in .amazoncognito.com
curl -sI https://<domain>/api/ask  # 401 from application API auth, no hosted-UI redirect
curl -sI https://<domain>/health   # 200
curl -sI https://<domain>/parseauth   # 302; Location: /  (same for /refreshauth, /signout)
```

The last check covers the `EdgeAuthBareCallbackRedirect` rule, which each
edge-auth distribution's AWS WAF web ACL evaluates after its three managed
rule groups (ADR-0070). It redirects a bare request (no query string, no
cookies) to one of the three auth endpoints, which the edge handlers would
otherwise answer with an HTTP 200 error page. The 302 carries the same
security headers as the rest of the site. Its CloudWatch metric has the
same name. On HighByte, `curl -sI https://<highbyte-domain>/data/doc` should
also give a 307 to the Cognito hosted UI: the REST Data Server's Swagger page
is behind sign-in, while the other `/data/*` paths stay bearer-token only.

Signing out from the app sends the browser to `/signout` with its edge
cookies still set, and the edge sign-out handler logs out of Cognito and
clears them. A `/signout` request with no cookies is redirected to `/`
(sign-in) instead.

A Cognito callback-URL mismatch means the app client did not receive the current
CloudFront hostname or custom-domain alias. Re-run `deploy.sh`; if it persists,
inspect the stack's `CallbackUpdater` custom resource and its Amazon CloudWatch
logs. If users see the app's `/login.html` form instead of the Cognito hosted
UI, edge auth is off (explicitly, from sticky SSM `/adt/edge-auth-enabled`, or
by the workshop default); check the deploy summary and redeploy with
`--edge-auth on` if that was not intended.

---

## Resetting AWS IoT SiteWise

To start fresh—for example when redeploying to a new Region or after a
corrupted asset hierarchy:

> **Warning:** Resetting AWS IoT SiteWise permanently deletes data. All AWS
> IoT SiteWise assets, asset models, and time-series data are permanently
> deleted by the following command.

```bash
python3 highbyte/cleanup_sitewise.py --profile <YOUR_PROFILE> --region <YOUR_REGION>
```

The script disassociates asset hierarchies (top-down), deletes assets
(bottom-up), then deletes asset models. It takes 5–10 minutes depending on the
asset count. Afterward, re-run `DeploySitewiseModels.py` and re-enable the
HighByte pipeline (see [`post-deploy.md`](post-deploy.md#5-enable-highbyte-aws-iot-sitewise-publishing)).

---

## Troubleshooting

Common failure modes during and after deployment, and how to diagnose them.

### AgentCore Runtime not READY

```bash
aws bedrock-agentcore-control get-agent-runtime \
  --agent-runtime-name adt_orchestrator \
  --region <YOUR_REGION> --profile <YOUR_PROFILE>
```

### Amazon ECS task failing to start

```bash
aws ecs describe-services \
  --cluster <YOUR_CLUSTER_ARN> --services <YOUR_SERVICE_ARN> \
  --region <YOUR_REGION> --profile <YOUR_PROFILE>
```

Then check the task's Amazon CloudWatch logs for the application error.

### Finch VM issues (macOS)

```bash
pkill -9 -f lima && finch vm start
```

### Disk space during image build

```bash
docker system prune -af --volumes
```

### "Maximum number of rules per security group has been reached"

The HighByte ALB is locked to the **Amazon CloudFront origin-facing managed
prefix list** (`com.amazonaws.global.cloudfront.origin-facing`) so that only
Amazon CloudFront can reach it. A security-group rule that references a managed
prefix list counts as the prefix list's **MaxEntries** (roughly 50) against the
default **60 inbound rules per security group** quota—not as a single rule.

The stack already mitigates this by splitting the UI-port and MCP-port rules
across **two ALB security groups** (`HighByteALBSG` and `HighByteALBMcpSG`), so
each holds one prefix-list reference. If you still hit the error—typically
because the Amazon CloudFront prefix list has grown past ~60 entries—request a
Service Quotas increase:

- Quota: **Inbound or outbound rules per security group** (`L-0EA8095F`),
  service `vpc`.
- Raise the default from `60` to, for example, `150`:

```bash
aws service-quotas request-service-quota-increase \
  --service-code vpc --quota-code L-0EA8095F --desired-value 150 --region <YOUR_REGION>
```

> **Note:** The hard ceiling is `rules-per-SG × SGs-per-ENI ≤ 1000`.

For HighByte-specific issues (AWS IoT SiteWise model errors, MCP connectivity),
see [`highbyte.md`](highbyte.md#troubleshooting).
