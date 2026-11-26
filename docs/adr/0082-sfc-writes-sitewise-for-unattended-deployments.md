<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# ADR-0082: SFC writes AWS IoT SiteWise for unattended deployments

## Status

Proposed — 2026-09-21. Verified in a sandbox account (us-east-1) on
2026-09-26 to 2026-09-29: SFC wrote all four sites (Cedar Rapids, Detroit,
Indianapolis, Milwaukee) to AWS IoT SiteWise and to the S3 Tables
`sfc.readings` table (3.8M rows). The first run stalled after about 8 hours
because the JVM defaulted to 25% of the task's memory (494 MB): target writers
timed out, S3 Tables appends stopped, and sites went stale in SiteWise one by
one. With `JAVA_TOOL_OPTIONS=-XX:MaxRAMPercentage=75.0` (1484 MB heap), a
2-hour re-run showed no writer timeouts, steady S3 Tables appends and fresh
SiteWise values for all four sites. On 2026-10-04 one deployment's task
was killed by ECS for memory (exit 137) about 70 minutes after start, and a
second account sat at 94-95% memory. Exit 137 is the container limit, not a
Java heap error, so the memory outside the heap (metaspace, threads, direct
buffers, and probably the Parquet and Iceberg writers behind the S3 Tables
target; not yet measured) outgrew the ~512 MB the 2 GB limit left over. The
task is now 1 vCPU / 4 GB with `-XX:MaxRAMPercentage=37.5`: the same ~1.5 GB
heap as before, and ~2.5 GB outside it. Keeping the heap at its old size also
keeps the JVM's default direct-buffer limit, which follows the maximum heap,
where it was. A 4-hour memory soak on two deployments with no exit 137
and a flat memory slope is the acceptance test.
Related: [ADR-0030](0030-demo-data-is-opt-in-base-default.md) (opt-in
infrastructure defaults) and [ADR-0044](0044-connection-state-on-records-demo-routing-in-demo-layer.md)
(demo literals live under `agents/demo/`).

## Context

With `deploy.sh --highbyte`, the OT/UNS layer moves shop-floor telemetry into
AWS IoT SiteWise with a HighByte Intelligence Hub pipeline. The
`NameSpace_to_SiteWise` pipeline in the project the hub imports
(`config/highbyte/intelligencehub-configuration.json`) polls the whole Unified
Namespace every five seconds, reshapes it into an `AnyCompany → Sites → Areas →
Lines → Processes_<Model>` payload, and writes to AWS IoT SiteWise with
`create: true` against `writePath: "realtime"`. The same pipeline is also kept
as a standalone file, `highbyte/pipeline_sitewise.json`, with its trigger
disabled; the hub does not import that file.

Some deployments run with nobody to operate them: a Workshop Studio account
is provisioned and used without an administrator watching the OT layer. Their
telemetry has to keep reaching SiteWise for the whole session with no manual
step. Without a license, the hub runs for two hours at a time; then its
runtime services stop until a user signs in again and resets the runtime
([HighByte licensing guide](https://guide.highbyte.com/release/setup/installation/licensing/)).
A SiteWise writer in such a deployment therefore must not depend on the hub's
runtime.

Shop Floor Connectivity (SFC,
[awslabs/industrial-shopfloor-connect](https://github.com/awslabs/industrial-shopfloor-connect))
is an AWS open-source project (Apache-2.0) that runs with no manual step. It
ships an OPC UA protocol adapter, an `AWS-SITEWISE` target and an
`AWS-S3-TABLES` target, configured by a single JSON file passed as
`sfc-main -config <file>`.

Three existing properties of this repository constrain the design:

1. **The 28 SiteWise asset models are pre-created and must not be mutated.**
   `highbyte/DeploySitewiseModels.py` creates them with complete measurement
   schemas precisely so that a writer only ever creates *assets*. SFC's
   `AssetCreation` mode creates and updates asset *models*, requiring
   `iotsitewise:CreateAssetModel` and `UpdateAssetModel`.
2. **`cdk synth` is pure** — it never writes into the checkout
   (`tests/test_docker_context_hygiene.py`). Configuration cannot be generated
   at synth time.
3. **Demo literals may not appear in `agents/*.py` outside `agents/demo/`**
   (ADR-0044), and a per-file ratchet test fails on any increase. Site names,
   tag paths and asset names are demo literals.

A fourth property makes the work tractable. The OPC UA servers address every
variable with a **string** NodeId whose value is the tag's full slash path —
`constructAddressSpace` in `highbyte/components/opc-server/server.mjs` sets
`identifierType: opc.NodeIdType.STRING, value: alias`, where `alias` is for
example `AnyCompany/Milwaukee/Bottling/Line1/Line1Filler/Filler_Good`. That
path is the same hierarchy the HighByte transform reshapes, and the alias set
is the tag map the datasets are generated from. Every identifier SFC needs is
therefore derivable from one existing source of truth rather than transcribed.

## Decision

1. **SFC is the continuous SiteWise writer of an unattended deployment; the
   HighByte hub keeps the UNS role.** Where the hub is deployed it keeps
   reading the OPC UA servers. Its `NameSpace_to_SiteWise` pipeline is
   switched by the synth context key `highbyte_sitewise_pipeline`
   (`stacks/_highbyte_options.py`): `enabled` (the default) imports the
   bundled project as it is, trigger on, and grants the hub's task role
   SiteWise access; `disabled` makes the setup Lambda import the pipeline with
   every trigger off (`highbyte/lambda/highbyte-setup/project-transform.mjs`)
   and gives the hub's task role no SiteWise permission. What each deployment
   path writes today:

   | Deployment | HighByte `NameSpace_to_SiteWise` | Continuous SiteWise writers |
   |---|---|---|
   | `deploy.sh --highbyte` | imported with its trigger on | HighByte |
   | `deploy.sh --sfc` | no hub | SFC |
   | `deploy.sh --highbyte --sfc` | imported with every trigger off (deploy.sh's default with both, `highbyte_sitewise_pipeline=disabled`) | SFC |
   | `deploy.sh --highbyte --sfc --highbyte-sitewise-pipeline enabled` | imported with its trigger on | HighByte and SFC, on the same asset properties |
   | Workshop Studio package, live story | imported with every trigger off (`highbyte_sitewise_pipeline=disabled`, the Workshop Studio packager `LIVE_TELEMETRY_CONTEXT`) | SFC |
   | Workshop Studio package, frozen story | no hub, no SFC | none |

   *Amendment 2026-11-04 (deploy.sh switch).* `deploy.sh
   --highbyte-sitewise-pipeline enabled|disabled` passes the key. Without it,
   a deploy with both HighByte and SFC passes `disabled` (HighByte stays the
   UNS, SFC is the only SiteWise writer) and a HighByte deploy without SFC
   passes `enabled`. An explicit value always wins and is sticky in SSM
   `/adt/highbyte-sitewise-pipeline`; the default is never recorded. Before
   this, `deploy.sh --highbyte --sfc` left the pipeline on. Every deploy
   that applies the default prints the same line before CDK, saying the
   pipeline is disabled because SFC is on and naming
   `--highbyte-sitewise-pipeline enabled`; no state is kept for it, so it
   never claims a past state. Pinned by
   `tests/test_highbyte_sitewise_pipeline_flag.py`.

   *Amendment 2026-10-13 (SFC freshness watchdog).* The SFC stack carries a
   watchdog (`stacks/_sfc_watchdog.py`) that forces a new SFC deployment when
   one SFC source's sentinel channel stops updating in SiteWise while its OPC
   UA server runs. It reads the newest stored value of a shared property, so
   it is built only where SFC is the only continuous SiteWise writer: every
   row of the table above except `--highbyte --sfc` with the pipeline on
   (`--highbyte-sitewise-pipeline enabled`), where the hub keeps the
   sentinels fresh while SFC is stalled. That synth builds no watchdog and
   prints a warning naming the switch. Pinned by `tests/test_sfc_watchdog_stack.py` and
   `tests/test_sfc_flag.py`.
   Real-time UNS queries route to the HighByte MCP server; historical queries
   route to SiteWise or S3 Tables.

2. **SFC writes to SiteWise through the `Assets` mapping mode, never
   `AssetCreation`.** Each asset is addressed by `AssetExternalId` (decision 9)
   with an explicit `Properties` list, each entry carrying the property name,
   the MODEL's `DataType`, and a
   `DataPath` JMESPath expression of the form
   `@.sources.<source>.values.<channel>.value`. This keeps
   `iotsitewise:CreateAssetModel` and `UpdateAssetModel` **out** of SFC's task
   role, so the 28 pre-created models cannot be mutated by the writer. SFC's
   IAM is `BatchPutAssetPropertyValue`, `DescribeAsset`, `DescribeAssetModel`,
   `DescribeEndpoint`, `ListAssets`, `ListAssetModels` and
   `ListAssetModelProperties`.

3. **The asset and property naming is a port of the existing transform, not a
   new scheme.** The model resolution in `pipeline_sitewise.json` —
   `Washer`/`WasherPump` and `Filler`/`FillerPump` by the presence of a `Pump`
   attribute, `BottlingMachine` for `Capper`/`CasePacker`/`WeighCheck`/
   `Labeller`, `FoodPalletizer` versus `BottlingMachine` for `Palletizer` by
   area, `Level` for buffers and conveyors, and `AutoEnvironment` versus
   `FoodEnvironment` by attribute — is reproduced exactly, and a test asserts
   agreement for every tag family on every site.

4. **S3 Tables infrastructure is CDK-owned and SFC runs with
   `AutoCreate: false`.** The target's `AutoCreate` defaults to `true` and
   would let SFC create the table bucket, namespace and table itself, but that
   requires `CreateTableBucket`, `CreateNamespace` and `CreateTable` on the
   task role. CDK creates all three so SFC's role narrows to `PutTableData`,
   `GetTable`, `GetTableData`, `GetTableMetadataLocation`,
   `UpdateTableMetadataLocation`, `ListNamespaces`, `ListTables` and
   `GetTableBucket` on its own
   bucket, plus the read-only, account-level `ListTableBuckets` (`Resource: *`),
   which SFC uses to resolve the bucket name to its ARN. This also
   makes the table a teardownable stack resource and gives Athena a known
   target.

5. **The table is narrow — one row per reading — partitioned by day.** Columns
   are `event_time` (`timestamptz`, not optional), `site`, `area`, `line`,
   `asset`, `channel` (`string`), `value` (`double`), and `quality`
   (`string`); `Partition` is `{"day": "event_time"}`. A narrow table answers
   "this property over this window" without a schema change per tag, which is
   what an Athena query path needs. The cost is one SFC `Mappings` entry per
   numeric reading. A `BOOLEAN` reading is read a second time through an SFC
   `BoolToNumber` channel so the table stores 0/1 while SiteWise still gets the
   raw boolean its `BOOLEAN` property needs; a `STRING` reading (`Status`) has
   no numeric form and goes to SiteWise only.

6. **The SFC configuration is GENERATED from the existing tag map, never
   hand-written.** Decisions 2 and 5 together require one property mapping and
   one table mapping per channel — several hundred entries across the four sites.
   The generator reads the same tag map that `highbyte/generate_datasets.py`
   uses, emits the `Channels` (`NodeId` of the form `ns=<index>;s=<alias>`),
   the SiteWise `Assets`/`Properties`, and the S3 Tables `Mappings`, and is
   covered by unit tests. It lives under `agents/demo/` because every value it
   handles is a demo literal (ADR-0044). `SfcStack` calls it at synth and
   writes the result into the image's staged build context under `cdk.out`
   (`stacks/_staging.py`). Constraint 2 is about the checkout, which is never
   written, so no deploy.sh step is needed, and `cdk deploy`, deploy.sh and a
   Workshop Studio build all produce the same image. (An earlier draft had
   deploy.sh generate the file and download the SFC bundles; both moved into
   the build. The Dockerfile now downloads the four release bundles itself,
   pinned by tag and by their published SHA-512, and fails on a mismatch.)

7. **SFC has its own flag, `sfc_enabled`, which implies `opcua_enabled`.**
   `opcua_enabled` (sticky, `/adt/opcua-enabled`, implied by
   `highbyte_enabled`) deploys the per-site OPC UA servers without HighByte.
   `sfc_enabled` (sticky, `/adt/sfc-enabled`, `deploy.sh --sfc`) adds the SFC
   stacks and forces `opcua_enabled` on, because the OPC UA servers are SFC's
   only source: the meaningless state — SFC deployed with nothing to read —
   stays unrepresentable. HighByte is independent: SFC with or without it are
   both valid. `--sfc` is only the flag: the stacks do the rest (decisions 6
   and 9). `tests/test_sfc_flag.py` pins the gating. The Workshop Studio
   package turns on `sfc_enabled` and `highbyte_enabled` for the live story
   only.

8. **SFC runs in the same VPC as the OPC UA servers and adds no
   security-group rule.** `highbyte_opcua_stack.py` grants OPC UA ingress to
   `vpc.vpc_cidr_block` on port 26543 rather than SG-to-SG, deliberately,
   because the NLB IP targets do not preserve the client IP. An in-VPC SFC task
   is already permitted. Each site is a separate `OpcuaServers` entry whose
   `Address`, `Port` and `Path` are split from the stack's existing
   `opc_ua_endpoints` values (`opc.tcp://<nlb-dns>:26543/UA/ManufacturingServer`).

9. **The asset tree is the one HighByte builds, created in-stack, addressed by external
   id.** Without the HighByte pipeline, nothing creates the assets it creates
   (`create: true`), and SFC's role cannot. A custom resource in `SfcStack`
   (`lambda-sfc-sitewise-tree`, on its own deploy-time role) creates the 28
   models (`DeploySitewiseModels.deploy_models`, the code deploy.sh step 1a
   runs) and then the tree the HighByte reshape stage builds: `AnyCompany`
   (Enterprise) → Site → Area → Line → `Processes_<Model>` children, with the
   same names and models. The specs come from `sfc_config.asset_tree()` at
   synth and travel as a resource property. Because it is part of the stack,
   it runs in every deployment path, including a Workshop Studio account
   that never runs deploy.sh. The SFC service depends on it, so SFC never
   starts before its assets exist. Every consumer already reads that shape:
   the SiteWise (Direct) onboarding walk roots at the `Site` assets and writes
   graph `Asset`/`Property` nodes by site/area/line path, and a flat tree
   would give it zero assets. Asset names repeat across sites (every site has
   a `Line1`), so every asset gets a unique path-derived external id
   (`adt-<site>-<line>-<node>`), and SFC addresses assets by it, never by
   name. The resource is idempotent and reuses instead of duplicating: an
   asset with its external id is kept; a HighByte-built asset of the right
   name under the right parent is kept and given its external id; the
   CMX-441 mill reuses the existing top-level `5-Axis-Milling-CMX-441` asset
   (created by the feed Lambda / workshop seed), keeping its id, and moves it
   under its Indianapolis Line3 slot. Nothing is ever deleted, renamed or
   remodelled, and stack deletion leaves the assets, as for the models
   (`cleanup.sh` removes both). Rejected: site-prefixed asset names (they
   diverge from a HighByte deployment's names and graph paths), and property
   aliases (one `UpdateAssetProperty` plus a wait per property, ~765 calls,
   where external ids cost one call per asset).

## Consequences

- A deployment with `sfc_enabled` grows by the per-site OPC UA Fargate
  services and the SFC service (plus the hub only if `highbyte_enabled` is
  also set). The live-story Workshop Studio package carries all three and sizes
  them to fit the default Fargate On-Demand vCPU quota
  (`stacks/_highbyte_options.py`, the Workshop Studio packager).
- SFC cannot write a property that does not already exist on one of the 28
  models. A tag with no modelled property is dropped rather than silently
  creating a model, which is the intended failure direction.
- SFC writes the fourth site's Line3 mill
  ([ADR-0083](0083-indianapolis-fourth-site.md)) to
  `5-Axis-Milling-CMX-441`, the asset `lambda-sitewise-feed` backfills, because
  the tag map names that node with a constant `AssetName` tag. Live SFC values
  and a backfill therefore land on the same properties; the backfill button
  stays until the live path has run on a fresh Workshop Studio deployment.
- The UNS route still depends on the hub's runtime. The licensing guide says
  every runtime service except configuration stops when the two hours run
  out; whether the hub's MCP server keeps answering namespace reads then is
  not verified here. This decision does not address that.
- The configuration is generated at synth, so it cannot be missing; the image
  build needs network access to GitHub releases (about 445 MB, SHA-512
  verified), like the OPC UA image's `npm ci` already needs npm.
- The asset-tree custom resource can take several minutes on a fresh account
  (28 models, ~135 assets, each polled to ACTIVE) against a 15-minute Lambda
  limit. It is idempotent, so a timed-out attempt resumes on retry. Not yet
  measured against a real account.

## Alternatives rejected

- **SFC `AssetCreation` mode.** Fewer configuration lines, but it grants
  `CreateAssetModel`/`UpdateAssetModel` to the writer and so puts the 28
  pre-created model schemas at risk. Rejected on decision 2's reasoning: the
  models being immutable to writers is an existing, deliberate property.
- **Using the hub's own `NameSpace_to_SiteWise` pipeline as the writer of an
  unattended deployment.** The pipeline already exists. Rejected because that
  writer stops whenever the hub's runtime stops, and nobody is there to reset
  it. It stays the writer of a `deploy.sh --highbyte` deployment, which is why
  the pipeline is kept.
- **A wide S3 Tables schema, one column per channel.** Fewer rows and one
  mapping, but a schema change per new tag and awkward cross-asset queries.
  Rejected for the Athena path's sake.
- **Letting SFC auto-create the S3 Tables resources.** Less infrastructure
  code, but a materially broader task role and resources CDK cannot tear down.
- **Reusing `highbyte_enabled` for SFC, with no flag of its own.** Chosen at
  first because the OPC UA servers existed only under that flag. Rejected
  2026-09-26: the workshop needs the servers and SFC whether or not the hub is
  deployed, so the servers got their own `opcua_enabled` and SFC its own
  `sfc_enabled` that implies it.

## Amendment 2026-10-02: the model owner adds missing properties

The models stay immutable to WRITERS (decision 2 is unchanged: SFC's task
role still has neither `CreateAssetModel` nor `UpdateAssetModel`). What
changed is the deploy-time model OWNER, `DeploySitewiseModels.deploy_models`,
shared by deploy.sh step 1a and the asset-tree custom resource.

Its find-by-name returned an existing model unchanged. An account
that had kept its SiteWise models after a stack delete still had a June
`CNC` model without the eight ADR-0073 analysis properties. The tree build reused that model, and SFC logged "property ...
not found" for every one of those channels and wrote none of them, so the
CMX-441 bearing story never reached SiteWise on that account. ADR-0073
decision 2 assumed the existing asset would receive the new properties;
find-by-name meant it never did.

`reconcile_model` now ADDS the declared properties and hierarchies an
existing model lacks, matched by name. Every existing property and
hierarchy is sent back with its id, so nothing is removed or redefined; a
model carrying composite models is left alone (none of the 28 has one). The
asset-tree custom resource's role gains `iotsitewise:UpdateAssetModel` for
this; it was already the role that creates models. A fresh account (a
Workshop Studio event) creates every model and never reaches the update
path.

## Note 2026-10-04: readings snapshots expire after 6 hours

The `sfc.readings` table carried no maintenance settings, so it ran the S3
Tables default snapshot management: keep at least 1 snapshot and expire
snapshots older than 120 hours. Every SFC append is an Iceberg commit that
writes a new `metadata.json`, and that file lists every retained snapshot.
Nothing expires for five days, so the file grows with every commit. On a
long-running deployment the table was 2.7 days old with 64,912 snapshots, all
retained, in a 49.5 MB `metadata.json` (about 760 bytes per snapshot); appends
of about 1,150 records took a median 18.9 s there against 4.1 s on an account
whose file was 32 MB, and SFC CPU sat near 90%.

The table now sets `SnapshotManagement` (`stacks/sfc_s3tables_stack.py`):
status enabled, `MinSnapshotsToKeep` 1, `MaxSnapshotAgeHours` 6. At about
three commits a minute (one write a minute since the S3 Tables batching
change) six hours keeps about 1,100 snapshots, under 1 MB of metadata. The
maintenance job's cadence is not documented; that account showed about 19
hours between snapshot-management runs, so the window actually retained is
the setting plus that cadence, and going below six hours would barely shorten
it. Nothing in the app reads an older snapshot: Athena queries the current
one, and no query uses time travel. Compaction stays at the service default.

CloudFormation updates `SnapshotManagement` in place ("No interruption"), so
an already-deployed table is kept with its data; its next maintenance run
expires the older snapshots. Data files referenced only by expired snapshots
become noncurrent and are removed under the bucket's unreferenced-file policy.
Pinned by `tests/test_sfc_s3tables_stack.py`.

## Amendment 2026-10-13: the SiteWise grants name this account's resources

Decision 2's action list no longer has `DescribeEndpoint`. The Service
Authorization Reference lists no such AWS IoT SiteWise action, so the grant
allowed nothing and the writer never needed it.

The writer's one write, `BatchPutAssetPropertyValue`, is now granted on this
account's and Region's `asset/*` and `time-series/*` ARNs instead of `*`.
SiteWise authorizes each entry of a batch on its own: the asset when the entry
names an asset id, the time series when it names a property alias. It drops an
entry that fails and still accepts the rest, so a scoping mistake here shows up
as missing values, not as a failed call. The reads stay on `*`.

The asset-tree custom resource's writes are scoped the same way: `CreateAsset`,
`UpdateAsset`, `AssociateAssets` and `UpdateAssetModel` on `asset/*` and
`asset-model/*`. `CreateAssetModel` has no resource type, so it stays on `*`.
Pinned by `tests/test_sfc_stack.py`.
