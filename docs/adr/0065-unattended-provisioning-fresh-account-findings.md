<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# ADR-0065: Unattended provisioning in fresh accounts — asset staging, runtime discovery, first-invoke model access

## Status

**Accepted — 2026-09-10.** Every claim below was observed live in AWS Workshop
Studio deployments (four in a row on 2026-09-10), each in a
freshly vended event account, and the fixes were verified by the last two deployments
reaching `CREATE_COMPLETE` with the attendee path (sign-in → first chat question)
answering on the first try ((41 s to first reply)).

## Context

The engine deploys two ways: `deploy.sh` (a developer's own account, admin
credentials, humans in the loop) and the Workshop Studio packaging
(the Workshop Studio packager, an unattended CloudFormation root per attendee account).
Three defects were invisible on the `deploy.sh` path and only surfaced in the
unattended one, because they all depend on the account being **brand new** or
on **ordering** that `deploy.sh` happens to get right by hand.

## Decisions

### 1. Everything CloudFormation fetches is a Workshop Studio *S3 Asset*

Workshop Studio has two asset kinds. Files under the workshop repo's `static/`
("Repository Assets") are served to content but are **not copied into the
per-event assets bucket** that `{{.AssetsBucketName}}` / `{{.AssetsBucketPrefix}}`
resolve to. Only files under `assets/`, synced to the workshop's staging bucket,
reach it (the first 2026-09-10 build snapshotted an empty asset manifest; the first nested
stack 404'd).

Therefore: the root template stays a Repository Asset (Workshop Studio deploys
it itself via `contentspec.yaml`'s `templateLocation`), and the five child
templates plus every zip the root fetches are mirrored into
`assets/infrastructure/{templates,assets}/` and synced to the staging bucket
(`tools/stage-assets.sh` in the workshop repo). Builds snapshot S3 Assets at
push time, so a re-sync always needs a new build.

**Prefix semantics (verified):** `{{.AssetsBucketPrefix}}` resolves to
`<buildId>/assets/` — it already *contains* the `assets/` segment. A staged file
`assets/infrastructure/X` is therefore at `{{.AssetsBucketPrefix}}infrastructure/X`,
and `contentspec.yaml` passes `AssetsBucketPrefix = {{.AssetsBucketPrefix}}infrastructure/`.
Writing `…assets/infrastructure/` produced `<buildId>/assets/assets/…` → `NoSuchKey`
(first event).

### 2. The app re-attempts orchestrator discovery while unresolved

`agents/config.py` discovers the `adt_orchestrator` runtime ARN by name at
startup. In the workshop root the ECS task starts when `ADTComputeStack`
completes, ~20 min **before** the `EngineRuntimeBootstrap` custom resource
creates the runtime (its inputs are `GetAtt`s from Compute, and that edge also
orders the `adt_*` sweep before subnet teardown on DELETE, so it cannot be
reversed). The old `@lru_cache` pinned the miss for the life of the process and
every `/api/ask` was a 500 (second event, until a manual ECS redeploy).

Therefore: `get_config()` caches normally but, while the ARN is unresolved in
cloud mode, re-attempts discovery every 15 s and pins the config once an ARN
resolves. `get_config.cache_clear()` is preserved. `deploy.sh` §2d's forced ECS
restart existed for exactly this bug and is now unnecessary.

### 3. First-invoke Marketplace subscription is handled at provisioning time

The first invocation of an Anthropic model in an AWS account makes Bedrock open
an AWS Marketplace subscription on the caller's behalf. Two consequences, both
observed:

- The **calling role** needs `aws-marketplace:ViewSubscriptions` and
  `aws-marketplace:Subscribe`, or the invoke — and every one after it — fails
  with `AccessDeniedException` (second deployment: MCP gateway handshake fine, then
  `strands/models/bedrock.py stream → AccessDeniedException`). `deploy.sh`
  accounts never hit this because a human had subscribed Claude once, long ago.
- Even with the permission, the subscription takes up to ~2 minutes to
  complete, and invokes inside that window still fail. The attendee's first
  question races it.

Therefore: every role that invokes a model (AgentCore runtime role, ECS task
role, Memory execution role, and the bootstrap CodeBuild role) carries the
statement from `stacks/_bedrock_access.py`, conditioned on
`aws:CalledViaLast = bedrock.amazonaws.com` so it is only usable from inside a
Bedrock invoke. And the `EngineRuntimeBootstrap` CR, after the runtimes are
READY, calls `converse()` once per distinct engine model
(`runtime_env_contract.models_to_warm()`: the orchestrator model and the
system-agent model), retrying `AccessDenied`/throttling every 15 s for up to
5 min and failing the CR with context on anything else. The fourth event's CR log:
`bedrock warm-up: <model> OK after 1 attempt(s)` for both models.

## Consequences

- Provisioning is deterministic in a fresh account; the attendee's first
  question no longer depends on timing.
- The single env home (`runtime_env_contract.py`) now also spells the
  system-agent model. `stacks/adt_compute_stack.py` still carries the same
  literal; `test_runtime_env_cdk_lockstep` guards the pair.
- A wrong or unavailable model now fails the CR during provisioning with the
  model id and error code in the CFN reason, instead of failing attendees.
- Reference: <https://repost.aws/knowledge-center/bedrock-resolve-marketplace-permission>.

## Operational notes

- A Workshop Studio event whose scheduled start time is already in the past
  cannot be provisioned; provisioning starts only once the schedule is moved to
  a future time.
- The Workshop Studio event page reports stack progress with a delay of many
  minutes. The CloudFormation console in the provisioned account is the
  authoritative view of deployment state.
- Each Workshop Studio event provisions a fresh account and is not
  retried automatically. When you test the workshop, terminate an event as soon as its result is known.
- Build-time warnings and their meaning: the four "abnormal Service Linked
  Role requested" lines are the `serviceLinkedRoles` list in the workshop's
  `contentspec.yaml` (CloudFront VPC origin, Neptune Analytics, two AgentCore
  principals) and are cosmetic — the roles are created and provisioning
  succeeds; keep the list. The `deployableRegions` warning that recommends two
  or more Regions is deliberate until another Region is validated by a test
  event.
- The asset scanner's "Findings (High)" badge on ADTCompute, ADTAgentCore and
  ADTOnboarding is **expected and non-blocking** (builds, events and
  publishing all proceed). The Workshop Studio asset scanner (checkov-based)
  uses its own high-severity rule subset — not stock checkov's — and the
  full report was: IAM wildcard statements that are not resource-scopable
  (`CKV_AWS_109`/`111` on the task, runtime, provisioner and onboarding
  roles), S3 server-access logging on the two demo buckets (`CKV_AWS_18`) and
  the internal ALB's HTTP listener behind CloudFront (`CKV_AWS_2`/`103`).
  Every one is already suppressed with a reason via the repo's cdk-nag
  metadata; the scanner honors that (status "Suppressed", with the reason
  shown) but the badge counts suppressed findings, so it cannot be cleared without removing
  the underlying properties — which is not a safe or sensible change. The
  stock-checkov hygiene applied the same day (ALB drops invalid headers, ECR
  scan-on-push, S3 versioning with noncurrent expiry, the task role's
  `iam:CreateServiceLinkedRole` confined to AgentCore principals, and the
  documented skips in `stacks/_checkov_skips.py`) is worthwhile for the
  customer-shared templates but does not change the badge.
- **Amended 2026-10-19 — how the scanner counts, and the skips that were
  findings.** Scan report 8 (47 entries, 29 distinct findings: each child
  template is scanned twice, as the `static/infrastructure/templates` copy
  and as the staged S3 asset) matches this model exactly, with no extra or
  missing finding: stock checkov 3.x on the templates as shipped, reporting
  every FAILED **and every SKIPPED** result whose check id is in the
  scanner's rule subset (`CKV_AWS_2`, `59`, `76`, `103`, `107`, `109`,
  `110`, `111`, `161` so far). checkov records a listed skip without
  evaluating the check, so a skip in that subset is a finding even when the
  check passes. Conditions on an IAM statement are honoured (the statement
  is not counted), as stock checkov does. Four of the 22 skipped findings
  were checks that pass: the app task role's `CKV_AWS_109` (its one
  permissions-management grant on `*` is conditioned), the hub execution
  role's `CKV_AWS_107`, and `CKV_AWS_111` on the runtime-bootstrap project
  and the external-images trigger. Those skips are removed. The real
  failures fixed: IAM database authentication on the seed RDS instance
  (`CKV_AWS_161`); the AgentCore runtime and onboarding runtime roles' ENI,
  log and Mantle-inference writes scoped to the account (AgentCore attaches
  runtimes to the VPC with its network service-linked role, not the runtime
  role); the HighByte setup Lambda's `ecs:StopTask`/`DescribeTasks` scoped
  to its cluster's tasks; the workshop-seed `iotsitewise:CreateAsset` scoped
  to the account's asset models. Predicted result: 32 entries (20 distinct).
  What stays, and why: the four HTTP ALB listeners (`CKV_AWS_2`/`103`; HTTPS
  needs a certificate a vended account cannot get), the CodeBuild roles'
  ECR Public auth token (`CKV_AWS_107`), the provisioner Lambda's and the
  external-images CodeBuild project's VPC ENI writes (the Lambda and
  CodeBuild guides give `Resource: "*"`), the Quick subscription handler
  (`109`/`110`/`111`, Quick sign-up actions; scoping is the deferred
  follow-up) and a vendor API mock that was removed from this sample (`59`/`76`). A skip is kept only where stock checkov fails,
  so adopters still see the reason; `stacks/_checkov_skips.py` states the
  rule.
- **Amended 2026-10-20: the prediction held; two roles drop ECR Public.** A
  security scan of the packaged workshop content reported exactly the
  predicted 20 distinct findings, with no malware. Two of them were grants nothing used:
  - The load project (`EngineImageLoadProject`) fetches its tarballs from the
    account's own S3 and pushes to the account's own ECR; `handle_load_mode`
    never calls ECR Public. Its role now holds `ecr:GetAuthorizationToken`
    only (which stock checkov's `CKV_AWS_107` excludes) and no skip.
  - The external-images project asks ECR Public for a token only when it
    copies an image pinned from `public.ecr.aws` (the busybox settings seed).
    With the default hub-image seed every copied image is on Docker Hub,
    which is pulled anonymously, so the role gets the ECR Public pair (and
    its skip) only when `external_images_root.pulls_from_ecr_public` says a
    copied image needs it.
  The copy and build projects pull base images from `public.ecr.aws` and keep
  the token. Predicted result: 18 distinct findings.
- **Amended 2026-10-20 (later): 18 confirmed, ten more fixed.** The rescan of
  the rebuilt package reported the predicted 18. Ten of
  them are now fixed in the resource, with no skip:
  - The copy and build projects' ECR Public token sits in a policy statement
    behind `Fn::If` on `ImageSourceMode`: the copy project gets it only in
    copy mode, the build project only in build mode. The image trigger
    starts only the project for the current mode, so the other one never
    runs. Stock checkov does not evaluate a statement behind `Fn::If`, so it
    counts neither.
  - The external-images project's VPC policy (generated by CDK) has its
    network-interface writes scoped to the account and Region's EC2
    resources; the describes stay on `*`.
  - The provisioner Lambda's VPC ENI writes are scoped to the account and
    Region's EC2 resources (the Lambda guide gives `*`). A probe on a
    deployed account confirmed that a VPC function whose role has only the
    scoped writes attaches to its VPC, goes Active and runs. The describe
    stays on `*`.
  - The Quick subscription handler grants every action of the guide's
    sign-up example, but each action that takes a resource is scoped to the
    account's Quick resources, its directories and the Quick service roles
    and policies. CloudTrail of a test sign-up showed Directory Service
    and Quick calls only, no IAM writes. A probe confirmed that Quick
    authorizes `DescribeAccountSubscription`, `DeleteAccountSubscription`
    and `RegisterUser` against the account's Quick ARNs (a role with only the
    scoped grant gets past authorization; a role without it is denied).
  - The vendor API mock's `api_key` is generated (it was a fixed string in the
    template), every method requires it as an API Gateway API key, and the
    stage writes access logs. A custom resource sets the account's API
    Gateway CloudWatch role only when the account has none usable (the role
    is missing, API Gateway cannot assume it, or the IAM policy simulator
    does not allow it the CloudWatch Logs writes), records in its physical id
    that it owns the setting and what it replaced, and on delete puts that
    back (or clears the setting if the old role is gone) while the setting
    still names its own role. If that fails, the delete fails and the role
    stays; otherwise the role goes with the stack.
  - In-place update of an older stack: the credentials secret's generated
    value changes, so `client_secret` and `api_key` are regenerated. The
    mock rereads the secret within two minutes. A system connected
    before the update holds the old values and must be connected again
    (module 8b) on that account. A Workshop Studio event always creates.
  What stays: the four HTTP ALB listeners (`CKV_AWS_2`/`103`, eight
  findings), and the external-images token in a busybox-seed package only.
  Predicted result: 8 distinct findings.
- **Amended 2026-10-22: the busybox settings seed is gone from the workshop
  package.** BusyBox is licensed GPL-2.0-only, and copying it into an
  attendee account's Amazon Elastic Container Registry (Amazon ECR)
  repository would make this project a distributor of it. `package.py
  --settings-seed-image busybox` is now refused with the reason (`hub`, the
  default, is the only value), the workshop's image pins leave BusyBox out,
  `Custom::ADTExternalImages` refuses a settings-seed image other than the hub
  image, and the external-images project role no longer has the Amazon ECR
  Public token pair or its `CKV_AWS_107` skip, which only a busybox-seed
  package carried. No workshop package can carry, copy or load BusyBox.
  `deploy.sh --highbyte` is unchanged: the customer's own account pulls
  BusyBox by digest from Amazon ECR Public for its settings seed.
- **Workshop profile accepted live (2026-09-12).** `package.py --profile
  workshop` (nine nested stacks + engine bootstrap CR + workshop-seed CR)
  reached `Deployed` in 79 minutes in a fresh Workshop Studio account: all
  nested stacks complete by ~60 min, the two custom resources take the rest.
  `WorkshopSeededSystems` read `erp,mes-mil,mes-det,mes-cr,plm`; the app opened
  with those five onboarded and the CMMS (plus the workshop's Indianapolis
  CMMS) registered-only; a cross-plant question was answered in 69 s citing
  all three MES systems. Budget the provisioning window at 75–90 minutes.
- **Fresh deployment, 2026-09-13: both provisioning fixes hold; the content
  was not runnable.** With the target-CREATING wait (`provision_runtimes.
  _wait_target_created`) and the image-replication DependsOn on the ERP
  children, the workshop root reached `Deployed` in **65 min 20 s** (no wait
  needed — the target was already active — both models warmed on the first
  attempt; the seed CR took 12 min with one transient HTTP 502 on an ERP
  status poll that retried cleanly). Per-system field mappings from the seed
  log: ERP 25 tables → 321, each MES 7 tables → 93, PLM 3 tables → 27.
  The in-app dry run then found two app-side gaps the deployment could not
  show: (1) the planted wrong mapping never appeared in the wizard —
  `onboard._run_map_job` never consulted `_apply_mapping_overrides` (only the
  agent's `map_fields` did); (2) the B-2847 storyline lives in
  `erp_indianapolis` / `mes_line3` / `iot_historian`, which the demo
  manifest never registered, so the orchestrator reported an identifier miss
  from every connected system. Both are fixed by the workshop manifest and
  seam changes recorded in `docs/workshop-mode.md` (levers 1 and 2); the
  seed now pre-onboards nine systems (`WorkshopSeededSystems` =
  `erp,mes-mil,mes-det,mes-cr,plm,erp-ind,mes-ind,historian,sitewise-direct`)
  and creates the SiteWise `CNC` model + `5-Axis-Milling-CMX-441` asset that
  the module-6 degradation feed resolves by name. Lesson for the runbook:
  a `Deployed` event is the *start* of acceptance, not the end — the
  module-by-module dry run is what validates the workshop.
- **Observation (recorded 2026-09-13), resolved 2026-10-19.** At the
  time, `/api/onboard/status` and `/api/onboard/catalog` returned the demo
  systems' PostgreSQL connection strings with their inline credentials to the
  signed-in integrator/Admin browser, and the Onboard wizard used that DSN to
  run discover. *Resolved state:* `/api/onboard/catalog` serves each
  connection string masked and its connection settings allowlist-sanitized
  (`onboard._served_catalog_entry`); `/api/onboard/status` serves only an
  allowlisted projection of each onboarded system (id, name, plant, type,
  connection type, status) with no connection fields (`onboard._status_system`,
  `tests/unit/test_onboard_status_serves_no_connection.py`); and a provision
  result's `mcp_url`, held in the job record and returned by
  `/api/onboard/provision/status/{job_id}` and `/api/onboard/provision/confirm`,
  is masked the same way (`onboard._dp_result`,
  `tests/unit/test_provision_result_masks_mcp_url.py`). A credential-free
  registration path remains worth having before the templates are shared more
  widely.
- **Second fresh deployment, 2026-09-13: artifact failure from the storyline change
  itself.** The root's `WorkshopSeedProject` CodeBuild project failed to
  create at minute 2 — `Max description length is 255
  (InvalidInputException)`, because the storyline change lengthened its
  description to 330 characters; every nested stack and the engine bootstrap
  CR still completed, and the root reported `CREATE_FAILED` at 55:49. cfn-lint
  does not check this property. Fixed by shortening the description and by a
  new `package.py` structural gate (`check_codebuild_descriptions`, proven
  against the failing template offline); runbook §6 carries the signature.
  Retry would have failed identically — artifact class, rebuild and new event.
- **Fresh deployment, 2026-09-14: a second account-agnostic Cognito domain.**
  `NestedADTERPDashboardStack` failed at minute 20: `User pool domain
  erp-dashboard-ccountid6 does not exist in this account … AlreadyExists`.
  The ERP dashboard derived its hosted-UI domain prefix in Python from
  `self.account[-12:]` — an unresolved CDK token at the credential-blind
  packaging synth — so one literal was baked into every attendee account; the
  account deployed the day before (still live) owned it and the next deploy
  failed. Same class as the `adt-m2m-00000000` finding (2026-08-24), which
  had been fixed only for the engine stacks. Fix: the shared helper
  `stacks/_cognito_domain.py::pin_domain_to_account` now serves both stacks
  (deploy-time `Fn::Join` on `AWS::AccountId`), and
  `tests/test_cognito_domain_pinning.py` guards every Cognito domain in the
  packaged dist. Lesson: any per-account literal computed in Python from
  `self.account` is wrong for the workshop package; only intrinsics are.
- **Second fresh deployment, 2026-09-14: eight of nine systems seeded; the SiteWise
  discover was mis-routed.** The seed created the SiteWise `CNC` model and
  `5-Axis-Milling-CMX-441` asset and onboarded erp (321 mappings), the three
  simulator MES (93 each), plm (27), erp-ind (38), mes-ind (30) and historian
  (25) — the storyline systems work. `sitewise-direct` failed at discover with
  `Provide system_id or both connection_string and schema`: the route computed
  `body.connection_type or entry[...]`, but `DiscoverRequest.connection_type`
  defaults to `"PostgreSQL"`, so an undeclared type always won and the
  catalog identity was never consulted; only headless callers (the seed, the
  smoketest) ever omit the type. Fix: the route treats the body type as
  authoritative only when the client set it (`model_fields_set`), and the seed
  declares each system's type on discover. The root failed at 72 min because
  the seed CR is last; everything before it was green.
- **Third fresh deployment, 2026-09-14: first fully green account-agnostic deploy
  (Deployed at 79:10; seed CR ran 07:36→08:00, all nine systems onboarded,
  `sitewise-direct` 2 tables / 29 mappings / 6 rules).** Module 1 starting
  state matched the content verbatim. Module 2 exposed two content-level gaps
  (healthy stack): (a) *"Which orders shipped from batch B-2847?"* routed to
  the corporate `erp` (`RUN-YYYYMMDD-#####` vocabulary → "no records") instead
  of the plant `erp-ind`; the plant ERP answered the three orders ($200,475)
  the moment it was asked by name or carried a guidance note (validated live
  through `/api/admin/vocabulary/guidance/erp-ind/propose` + accept). (b) No
  IdentityRule linked `mes-ind` to `erp-ind` (183 onboarding rules, 5 of 36
  pairs), so the trace could not show the join the module teaches; a rule
  taught through `/api/thread/learn` carried no `method`/`provenance`, ranked
  tier 2 and was trimmed out of the 8 KB route seed. Fixes: manifest
  `capabilities`/`query_guidance` forwarded by the registration hook (lever 4);
  the seed CR teaches the storyline rule after onboarding (lever 5); `/learn`
  stamps `user_confirmed`/`human-asserted`. Also observed: chat MEMORY facts
  from a failed session ("ERP uses WO-YYYYMMDD") steer later sessions — clear
  memory (↻) between dry-run attempts. Question 2 (line/plant) answered
  correctly from `mes-ind · production_runs` unaided.
- **Same deployment, modules 4 to 7:** (c) the Confirm Mappings
  checkpoint correction was DISCARDED at provision — the map row carries the
  mapper's nested `mappings` role list, which `_dp_field_row` preferred over
  the confirmed `concept`/`concepts`; the planted `assets.asset_num ->
  work-order 0.82` was written back after the attendee set `equipment-id`
  (visible in module 7's Query 3 and `GET /api/onboard/mappings/cmms-ind`).
  Not workshop-specific: every steward edit at that checkpoint was lost. Fix:
  `_confirmed_role_mappings` (confirmed concepts win; roles inherited).
  (d) The degradation feed failed with `ThrottlingException … maximum rate of
  BatchPutAssetPropertyValue entries ingested per asset-property` after 500
  values: entries were grouped by property, 10 per request, back-to-back
  (SiteWise allows 10 entries/s per data stream). Fix: interleave streams,
  pace requests (35 requests ≈ 14 s), retry throttled entries with backoff.
  (e) Asked bare, the module 6 question routed to the Plant Historian
  (February archive, "7-month data gap"); anchored on "live AWS IoT SiteWise
  telemetry … last 72 hours" it routed to `sitewise-direct` and read the
  partial feed correctly (1.80 → 2.50 mm/s, X only). Content anchors the
  question; manifest guidance marks the historian archived and SiteWise live.
  (f) Briefing showed `--VALUE-MAPPING-PROPOSAL---` / `--END-PROPOSAL---`
  bullets: `cleanBriefingText` stripped FOLLOWUPS/CORRELATION blocks but not
  VALUE-MAPPING blocks. Fixed. (g) Fresh-install dashboard: "This chart is
  unavailable — ambiguous authority for concept actual-quantity: 3 active
  AuthorityRules name different systems ([mes-cr, mes-det, mes-mil])" (the
  per-plant MES authority rules collide in the concept-global resolver);
  "Sensor heatmap unavailable — no MAPS_TO field for heatmap dimension asset
  in system sitewise-direct"; 185 pending proposals after the seed (82
  cross-system-mining crosswalks, 98 auto-analysis insights, 50 join paths —
  all agent-sourced onboarding by-products). Recorded as follow-ups, not
  fixed here.
- **Fresh deployment, 2026-09-16: failed at 83 min in the seed CR's last step.**
  All nine systems onboarded and the phase-2 lap passed; then
  `ensure_story_identity_rules` raised `TypeError: 'int' object is not
  iterable` — it read `identity_rules` from `GET /api/thread/graph-summary`,
  which is the COUNT; the IdentityRule rows are under `rules`. The offline fake
  had been written to the wrong shape, so the tests passed. Fix:
  `GRAPH_SUMMARY_RULES_KEY = "rules"`, a text pin against `server.py`, and a
  fake that mirrors the real response (counts + detail lists). Lesson: a fake
  of an app route must be pinned to the route's real shape, not to the caller's
  expectation.
- **Fresh deployment, 2026-09-16: deployed green in 86 min; seed log shows `identity rule taught: mes-ind.batch_number B-{id} -> erp-ind.material_lot IND-2026-0{id}`. Dry run: modules 1, 3, 4 (correction PERSISTED after provision), 5, 6 (feed wrote 3456 values across 4 properties; anchored question reads 1.83→2.89 mm/s from Sitewise Direct and lists Indianapolis ERP orders at risk) and 7 all pass. Module 2 Q1 still routes to the corporate `erp` and reports "no cross-system identity mapping exists" even though the rule is in the graph (steward provenance, visible in Threads TOPOLOGY) and the plant ERP carries routing guidance. Root cause: `_serialize_route_seed` popped ALL rule detail (tier 0 included) before shrinking any prose; with ten systems the guidance prose alone exceeds the 8,000-char budget, so the confirmed rule never reached the model while generic connection-type boilerplate did. Fix: hold tier-0 rows, shrink prose first, drop confirmed rows only as a last resort (`keep_confirmed`).
- **Identifier-core affinity (2026-09-16, follow-up to that deployment):** the LLM resolver only pairs fields that share a concept; `mes-ind.batch_number` maps to `batch` and `erp-ind.orders.material_lot` to `lot-number`, so the B-{id} ↔ IND-2026-0{id} rule was never proposed and the seed had been teaching it through `/api/thread/learn`. New `agents/identity_affinity.py` (pure) reduces sampled identifier values to digit cores, pairs fields across systems on shared cores, and keeps the pair only when ONE inferred pattern transform explains ≥80% of the matches (a year shared with dates fails that check). `_run_resolve_job` runs it around `_do_resolve` (so it fires when no concept is shared); the rule carries `method=value_core_match` plus its evidence as `origin_context` (provision persists them; tier 1 in the route seed); the pattern sample check now uses the rule's own table; and the seed budget shrinks prose before dropping tier-0/1 rows. The workshop seed no longer teaches anything: `report_identity_rules` prints the rules discovery found between the seeded systems.
- **Fresh deployment, 2026-09-17 (the first package built with the `--require-main` guard): deployed green at ~107 min (nested stacks by ~88 min; seed phase 2 and the identity-rule report took the rest). Discovery found `mes-ind.batch_number B-{id} -> erp-ind.material_lot IND-2026-0{id} [value_core_match]` unaided, with no `OP-0{id}` / `PM-1001` false positives after the three-significant-digit and integer-coverage guards. Module 2 Q1 asked bare in a fresh chat routed to `erp-ind · orders` and the answer named the translation itself ("batch translated from MES B-2847 → ERP lot IND-2026-02847 via a known identity rule, value_core_match, confidence 0.85") — the deterministic `IDENTIFIER TRANSLATIONS` block doing what the model had been left to do. Modules 3–7 passed (module 4 correction persisted; module 6's bare question first went to the archived historian, and a guidance note proposed and delivered through the Vocabulary UI flipped it to `Sitewise Direct`, 1.83→2.82 mm/s). One UI defect found on the way: after **Propose note** the Guidance delivery queue kept reading ZERO until a page reload because the propose mutation never invalidated the proposals query (fixed here: it now invalidates `vocabularyGuidanceProposals`, mirroring accept). Variance worth knowing: the agent read "Which orders shipped" literally against `status` (one shipped, two released) — the workshop content now says the columns vary.
- **2026-09-18: SiteWise (Direct) provision wrote NO Asset / AssetModel / Property nodes.** Both provisions passed Verify, yet the live label census had Field 790, JoinPath 183, System 10 … and zero `Asset`, `AssetModel`, `Property`, although the account held the `CNC` model with its one story asset and four properties. Root cause (`_dp_sitewise_hierarchy`, `agents/onboard.py`): the walk was rooted ONLY at assets of a model literally named `Site` and returned silently when none existed — any account that models equipment directly (the workshop, and most customers) got nothing — and the Direct branch never wrote `:Property` nodes at all; only the i3X walker (`onboard_i3x._write_property_child`) did. With no `:Asset` the equipment ValueMapping bridge (`MAPS_TO_ASSET`) had nothing to anchor the CMMS asset number to. Fix, generic: without a `Site` model the walk roots at every model's `TOP_LEVEL` assets (`list_assets filter=TOP_LEVEL`), root level from the model name via `onboard_i3x._level_for`; `HAS_PARENT` / `INSTANCE_OF` / `FROM_SYSTEM` unchanged; each asset's `describe_asset` `assetProperties` are written as `:Property {source_id, name, data_type, unit, path, parent_id, system}` with `(Asset)-[:HAS_PROPERTY]->(Property)` and `(Property)-[:FROM_SYSTEM]->(System)` — the i3X node shape — and `Asset.property_count` is stamped as the i3X walker does; a walk that visits zero assets logs a WARNING instead of an info line. Split into `_dp_sitewise_client` / `_roots` / `_write_asset` / `_write_properties` / `_walk` (each under 50 lines). Covered by `tests/unit/test_sitewise_direct_graph_skeleton.py` (fake `iotsitewise` client: no Site model, one model, one asset, four properties). The seed needs no change: `workshop_seed/handler.py` onboards `sitewise-direct` through the same discover → map → resolve → provision REST pipeline a UI user drives.
- **2026-09-18: Identity Rules checkpoint proposed junk rules from value overlap on MEASURES.** The corporate maintenance system's resolve proposed six `cost` rules to the ERP (`spare_parts.unit_cost` ↔ `products.standard_cost` / `list_price` / `order_line_items.unit_price` / `inventory_items.unit_cost` …) at 7–27 % overlap sharing `120.00`, `45.00`, `420.00`; the plant maintenance system proposed `inspection` (`work_order_log.log_id` ↔ the plant MES `quality_checks.sample_number`, sharing `1 2 3 4 5`) and `schedule` (`pm_schedules.next_due_date` ↔ the plant ERP `orders.scheduled_start`, 14 %, one date). Root cause: `_do_resolve` measures overlap for EVERY field pair that shares a concept and tells the model to "strongly prefer measured overlapping values", so any coincidental overlap of prices, sequence numbers or dates read as licence. Fix, generic (new pure `agents/resolve_gates.py`, applied in `_run_resolve_job` to the LLM's rules BEFORE the affinity rules join — `identity_affinity` is untouched and never re-gated): never on measure/temporal concepts (cost, price, quantity, amount, date, time, schedule …); with measured evidence require ≥ 3 distinct shared values AND overlap ≥ 50 % of the smaller side, and reject shared values that all share a non-identifier shape (decimal measures, dates/timestamps, booleans, a small dense integer range); without measured evidence keep a rule only when BOTH field names are identifier-like (id, num, number, code, key, lot, batch, serial, tag …). Rejected rules ride on the result as `rejected_rules` with a `rejected_reason`, and each drop is logged. Frontend: the Identity Rules checkpoint table now shows the field pair and a per-row **✕ reject** (`flow-store.rejectRule`) that removes the rule from the table AND from the step data `Confirm & Provision` sends — the same edit the chat `remove_rule` action performs. Tests: `tests/unit/test_resolve_gates.py` (the three live shapes rejected, a string-identifier rule with strong overlap still proposed, the job wiring), `frontend/src/test/onboard-stores.test.ts`. Follow-up: render `rejected_rules` (with reasons) under the evidence table so the steward sees what discovery declined.
- **2026-09-18: every SUCCESSFUL provision ended "Provisioned — completion confirmed; statistics could not be recovered".** Two provisions (6 min and 1.5 min, both passed Verify) showed the degraded summary although `_do_provision` returned `neptune_nodes_created` / `identity_rules_created` (confirmed in code: the result dict carries both, and the knowledge post-pass adds to them). Root cause, deterministic: `GET /api/onboard/provision/status/{job_id}` returned `{"status": "done", **result}` and the provision result carries its OWN `"status": "provisioned"`, so the spread overrode `done`. The wizard keys on `status === 'done'`, never matched, and kept polling; the route had DELETED the job on that first read, so the next poll got `Unknown job_id`, the catalog said `onboarded`, and the wizard reported `stats_unavailable`. The seed harness polls the catalog for `onboarded` and never saw it; the resolve/map routes were unaffected because their results carry no `status`. Fix: `_finished_provision_payload` builds the body with the route's `done` winning (the result's status rides along as `provision_status`), and finished records are no longer deleted on first read — `_consume_finished_provision` stamps `consumed_at` and reaps after `PROVISION_DONE_GRACE_SECONDS` (120 s; the reaping read still answers), so a StrictMode double effect, duplicate poll or edge retry re-reads the real counters instead of falling back. `JobStore('provision')` uses the default 1 h DynamoDB TTL, so the remote copy is reaped regardless. Pinned by `tests/unit/test_onboard_provision_status.py`. No frontend change: the wizard's `done` branch and the existing `stats_unavailable` fallback are correct once the server answers `done`.
- **2026-09-18, finding 1: SiteWise Direct history truncated the tail:** the feed wrote 3456 values across four properties (864 per property, 5-min cadence, 72 h); eight minutes later the Direct system agent's `sitewise_get_asset_property_history` returned `partial` / `page_limit` and its 24 points covered only ~44 of the 72 h, so the answer reported a 1.78→2.14 mm/s rise instead of the ~2.8 peak that lived in the dropped tail (an earlier deployment answered 1.83→2.82 for the same question: same code, different call shape). Root cause: the chunk budget was `HISTORY_MAX_PAGES_PER_CHUNK` (8) × the MODEL-chosen `max_results` page size (default 100) = 800 raw points per ≤16-entry chunk, so coverage depended on how many entries the model batched and what page size it happened to pass; a second defect made the shortfall look worse — the accumulator kept `buffer[::2][:24]` then appended EVERY later point, clustering 23 of 24 downsampled points at the read's end. Fix (`agents/sitewise_read.py`): the service page size is always the API maximum (`HISTORY_FETCH_PAGE_SIZE = 20000`; `max_results` is validated for compatibility, never shrinks a page); the raw-point/byte budgets are PER CHUNK (`HISTORY_MAX_RAW_POINTS_PER_CHUNK = 131072`, bytes = ×64) so an oversized chunk cannot starve later ones and only the global call budget halts everything; the accumulator is stride-decimated (keep every k-th point, double k on overflow, newest point tracked separately) so retained points stay uniform over the whole window and the tail is never lost; `_downsample_points` picks 24 points evenly by TIME, always first and last; and every `partial` outcome now carries `covered_start`/`covered_end`/`requested_start`/`requested_end` plus a `note` that states the covered span, so a truncated head can no longer read as the full window. Pinned by `tests/unit/test_sitewise_history_coverage.py` (fake client serving the 3- and 4-property 72 h shape through service-style pagination). Inspection of `agents/system_agent.py`: `_summarize_vqt_series`/`_cap_history_payload` (stats + `dataset_ref` + `landmark_values`) run ONLY on the i3X paths (`_i3x_fetch_history_capped`, the composite telemetry tool); the Direct path hands the orchestrator the raw downsampled rows (≤24 per property, no `_stats`), so the content's "~40-token summary of 864 samples" describes i3X, not Direct. Left unchanged — the Direct list shape is consumed by result extraction and summary counting, so a shape change is not "small and safe" here.
- **2026-09-18, finding 2: stale pending list / "186 learning proposals awaiting review":** `accept_mapping` and `reject_mapping` DO flip the row's `status` (conditional `_update_status`, guidance goes through the CAS saga), so the list was not reading a dead flag. Two verifiable defects explain the stale view: (a) `get_all_mappings` — the ONE read behind `GET /api/proposed-mappings`, the review list, the guidance delivery queue and the bell badge — scanned with DynamoDB's default eventually-consistent read; the client re-fetches the list the instant an accept/reject mutation resolves, so a pre-write snapshot could come back and then sat in the react-query cache (30 s `staleTime`, no polling) as "still pending". Fix: `ConsistentRead=True` on the base-table scan (strong consistency is supported there, not on the GSIs; the queue is small). (b) The route accepted no `status` parameter, so `?status=pending` was silently ignored and every accepted/rejected/superseded row came back too. Fix: optional `status` query param (closed vocabulary, 422 otherwise) that filters server-side and echoes `status` in the payload; no parameter keeps the every-row contract. Pinned by `tests/unit/test_proposed_mappings_pending_freshness.py`. The 186: the badge counts `status == pending` rows other than `steward_question`, and on a fresh deployment those are the onboarding by-products the seed CR's nine-system onboarding files for steward review — cross-system-mining crosswalks, `join_discovery` join paths and auto-analysis `insight` findings (the first fully green deployment on 2026-09-14 counted 185 of the same shape), so the number is a genuine, honestly-counted backlog, not a stale-status artifact. Open question recorded, not decided here: `insight` rows are informational (accept writes no graph rule) and only leave review pressure through the 30-day lazy TTL, which `get_pending_mappings` applies but the badge's `get_all_mappings` scan does not; whether the bell should count insights at all is a product decision. No rows were deleted.
- **2026-09-18, finding 3 — `resolve.py` entity-picker demo-schema fallback removed (follow-up to the change that removed demo literals from `resolve.py`):** `thread_entities` still fell back to fixed demo schema names (`pg` reads against four seed-only schemas) whenever the graph had no `identifier` mappings, which is exactly the state of a fresh engine before onboarding — the last domain literal in `agents/resolve.py` (neutrality baseline 1). Now the picker is generic by construction: identifier values are sampled ONLY through graph `MAPS_TO {role: identifier}` fields on `onboarded` catalog systems (sampling extracted to `_sample_identifier_values`, identity-only catalog reads as before), and an empty graph yields a STRUCTURED EMPTY result (every picker key present, empty lists — the frontend `ThreadEntities` shape is unchanged). The ratchet entry for `resolve.py` is deleted (a zero entry is forbidden by the ratchet's own rule, so any future hit fails as an unlisted file). Pinned by `tests/unit/test_thread_entities_generic.py` (no schema read without a mapping; graph-mapped fields drive the picker; no fixed schema literals; function-length gate).
- **2026-09-18, finding 4, cosmetics:** (a) the Workshop Studio team page rendered every non-ASCII character in ROOT-stack Output/Parameter descriptions as `?` ("The ADT web application URL ? the one thing an attendee needs", "(audit ?2 ta..."). Six descriptions in the Workshop Studio packager carried an em dash or a section sign (Outputs `ADTCloudFrontUrl`, `EngineRootDeployed`; Parameters `ImageSourceRepository`, `ImageSourceArchiveChecksum`, `IdentityClaimSecretArn`, `AdtWorkshop`), plus two in the staged `ADTERPDashboardStack` child (`DashboardUrl`, `LoginCredentials`, in `stacks/erp_dashboard_stack.py`). All are ASCII now, and the Workshop Studio packager gained `check_ascii_descriptions` (wired into `structural_check`, so a packaging run fails on any non-ASCII Output/Parameter description in the root or a staged child — cfn-lint accepts any Unicode and would never catch this); the root synth is also pinned by `test_root_output_and_parameter_descriptions_are_ascii` for both profiles. Not touched: `stacks/adt_identity_center_stack.py` and `stacks/highbyte_opcua_stack.py` carry em dashes in output descriptions too, but neither is a workshop child template, so they never reach the team page. (b) `erp_simulation/streamlit_dashboard.py` titled the ERP simulator after a beverage company while the workshop story is precision machining. New pure `erp_simulation/branding.py`: default title `AnyCompany Manufacturing - ERP Simulator` (page title `... Dashboard`, factory icon), overridable per deployment through the `ERP_DASHBOARD_TITLE` container env var (blank/oversized values fall back to the default). No seed-data change. Pinned by `erp_simulation/tests/test_dashboard_branding.py`.
- **2026-09-18, finding 5: the three "unavailable" dashboard panels (follow-ups from item (g) of the first fully green deployment on 2026-09-14), root causes:** (1) *"ambiguous authority for concept `actual-quantity`: 3 active AuthorityRules name different systems"* (Output by Line and every other grouped bar that resolves a concept the plant MES systems all provide). Provisioning writes AuthorityRules PER PLANT: `_dp_provision_authority_rules` mints `ar-<system>-<concept>` ("… authoritative source for <concept> at <plant>") for each system, skipping only when another system in the SAME plant already covers the concept, while `GraphAuthorityResolver.authoritative_system(concept)` is concept-GLOBAL and, by design, fails loud when active rules name more than one system. KPI tiles never hit this because `metric_multisite.resolve_metric_multi` pins authority per covering system and combines; the grouped route (`POST /api/metrics/resolve-by-dimension`, called by the frontend with no `system_id` pin) has no such fan-out, so any multi-plant estate with a shared MES concept trips it. NOT fixed here: the honest fix is a grouped fan-out (per covering system, rows unioned with per-row lineage, SUM/COUNT merged on a dimension-value collision, AVG/LATEST left per-site) or a plant-scoped pin on the request: an extension of ADR-0045's fan-out contract ("fan-out only inside TTL-cached `_build_cards`"), so it needs its own decision, not a patch here. (2) *"Sensor heatmap unavailable: no MAPS_TO field for heatmap dimension `asset` in system `sitewise-direct`"*. The frontend asks for `sensor-reading-latest` over `asset` × `production-line` with all three co-located on the measure table. With SiteWise (Direct) as the `sensor-reading` authority the measure table is a per-asset-model property pseudo-table; `asset` is the CMMS *entity* concept (catalog: "the record type … distinct from the `equipment-id` identifier") and is never mapped on a telemetry field, and `production-line` lives on the separate `asset_hierarchy` pseudo-table, so the heatmap is structurally unsatisfiable for a direct-source authority (the SiteWise grouped executor buckets by a dimension PROPERTY; "the asset itself" as an axis is not modelled). Contained fix applied: the missing-dimension case is now a `CoverageGapError` → `status: "uncovered"` envelope (`heatmap_uncovered`, `missing_concepts: ["asset"]`), exactly how `resolve_metric_by_dimension` classifies the same condition, so the panel renders as needs-data instead of error styling with a Retry that can never succeed. Follow-up: model an implicit `equipment-id` (asset-name) axis for the direct source and switch the heatmap's x concept from `asset` to `equipment-id`. (3) *"Active issues unavailable"*. `ISSUE_SOURCES` are FIXED SQL over one seeded vocabulary: MES `quality_checks.pass_rate/fail_count/sample_size`, MES `work_orders.status/good_quantity/planned_quantity/line`, ERP `customer_orders.status/total_amount`, and `compose_active_issues` was all-or-nothing. The workshop onboards a plant MES (`production_runs`, and a `quality_checks` table WITHOUT `pass_rate`) and a plant ERP (`orders`, no `customer_orders`); the first `UndefinedTable`/`UndefinedColumn` raised and the route wrapped the whole panel as "temporarily unavailable", although the corporate ERP and the three site MES systems could report. Contained fix applied: a (source, system) pair whose schema lacks the source's table/column (SQLSTATE 42P01/42703, duck-typed via `pgcode`) is skipped with an INFO log (the same honesty rule as the existing non-SQL skip), and every other error still propagates (outage stays fail-loud). Pinned in `tests/unit/test_active_issues.py` and `tests/unit/test_heatmap_resolver.py`.
- **2026-09-27: one ERP; the plant ERP named in the deployment notes above is gone ([ADR-0084](0084-one-erp-and-the-workshop-story-setting.md)).** The workshop seed list is now story-dependent: `WorkshopSeededSystems` = `erp,mes-mil,mes-det,mes-cr,plm,mes-ind,historian,sitewise-direct` for the `frozen` story (default) and `erp,mes-mil,mes-det,mes-cr,plm,mes-ind,sitewise-direct` for `live`; the root also outputs `WorkshopStory`. The 2026-09-13 to 2026-09-17 notes above describe the nine-system list, the plant-ERP routing gap (2026-09-14) and the `B-{id}` ↔ plant-lot identity rule (2026-09-16 and 2026-09-17) as they were observed; they stay as history. Module 2's frozen translation is now MES `product_code` ↔ PLM `item_id` (`value_core_match`), which a fresh deployment must confirm in the seed log. The live story's anchor, history and Delete behaviour: [ADR-0085](0085-live-story-clock-history-and-lookup.md).
- **Amended 2026-09-27 (frozen-story deployment): [ADR-0086](0086-identity-rule-wildcard-for-one-to-many-cores.md).** Affinity learned `mes-ind.product_code {id} -> plm.item_id PN-{id}-REV-A` because it kept one value per digit core from an un-ordered sample (three PLM revisions share `7742`); the rule was wrong and row-order dependent. It now groups all values per core and marks the varying part `{*}` (`PN-{id}-REV-{*}`); one-to-one rules are unchanged.

## Update 2026-10-29

The provisioner Lambda named in the findings above (`adt-onboarding-provisioner`), its Step Functions state machine, image and roles were removed: they had no caller. The onboarding stack now synthesizes neither (`tests/test_onboarding_runtime_stack.py`). The findings stay as a record.
