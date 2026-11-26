#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Agentic Digital Thread — CDK Application Entry Point

Consolidates the ERP Bottling Simulator (with HighByte UNS) and the
ADT dashboard into a single CDK application.

Stack deployment order:
  ADTNetworkStack → ADTERPDatabaseStack → ADTERPLambdaStack → ADTERPDashboardStack
                    ADTDatabaseStack    → ADTComputeStack
                                        → ADTHighByteOpcuaStack (×3 OPC UA + 1 HighByte)

All stacks share a single VPC from ADTNetworkStack.
"""

import os
import sys

# `cdk synth` writes nothing into the checkout, bytecode included. Set before
# aws_cdk or any stacks module is imported: the stacks load app modules by
# path, and Python would otherwise cache their bytecode beside the sources.
sys.dont_write_bytecode = True

import aws_cdk as cdk
from cdk_nag import AwsSolutionsChecks

from stacks.network_stack import NetworkStack
from stacks.erp_database_stack import DatabaseStack as ERPDatabaseStack
from stacks.erp_lambda_stack import LambdaStack as ERPLambdaStack
from stacks.erp_dashboard_stack import DashboardStack as ERPDashboardStack
from stacks.adt_database_stack import ADTDatabaseStack
from stacks.adt_seed_data_stack import ADTSeedDataStack
from stacks.adt_compute_stack import ADTComputeStack
from stacks.agentcore_stack import AgentCoreStack
from stacks.onboarding_stack import OnboardingStack
from stacks.highbyte_opcua_stack import HighByteOpcuaStack
from stacks.sfc_s3tables_stack import SfcS3TablesStack
from stacks.sfc_stack import SfcStack
from stacks._sfc_watchdog import add_sfc_freshness_watchdog
from stacks._db_access import wire_database_clients
from stacks.quick_apigateway_stack import QuickApiGatewayStack
from stacks.adt_identity_center_stack import ADTIdentityCenterStack
from stacks._demo_credentials import create_demo_password_secret
from stacks import _sample_hardening
from stacks._sticky_context import resolve_sticky, resolve_sticky_bool
from stacks._observability import wire_observability_reader
from stacks import _quick_connector as quick_connector
from stacks._evaluations import wire_eval_config
from stacks._custom_domain import (
    resolve_custom_domain,
    CustomDomainCertificateStack,
    wire_custom_domain,
    wire_subdomain_alias,
)
from stacks._edge_auth import resolve_external_oidc
from stacks._quick_gateway_auth import resolve_quick_oidc
from stacks._checkov_skips import (
    apply_adt_compute_checkov_skips,
    apply_agentcore_checkov_skips,
    apply_onboarding_checkov_skips,
)
from stacks._nag_suppressions import (
    apply_sample_suppressions,
    apply_network_suppressions,
    apply_erp_database_suppressions,
    apply_erp_lambda_suppressions,
    apply_erp_dashboard_suppressions,
    apply_adt_database_suppressions,
    apply_adt_seed_data_suppressions,
    apply_agentcore_suppressions,
    apply_adt_compute_suppressions,
    apply_highbyte_opcua_suppressions,
    apply_onboarding_suppressions,
    apply_quick_api_suppressions,
    apply_sfc_suppressions,
)

app = cdk.App()

# Region and account from context or CDK CLI (--profile sets CDK_DEFAULT_ACCOUNT, --region sets CDK_DEFAULT_REGION)
region = (
    app.node.try_get_context("region")
    or os.environ.get("CDK_DEFAULT_REGION")
    or os.environ.get("AWS_DEFAULT_REGION")
)
account = os.environ.get("CDK_DEFAULT_ACCOUNT")
env = cdk.Environment(account=account, region=region) if (account and region) else None

# ---------------------------------------------------------------------------
# Sticky context values (SSM-backed) — mirrors the HighByte sticky-flag pattern.
#
# `system_query_mode` and `crossAccountDataRoleArn` are plain context values;
# a deploy that omits the flags would silently revert the ECS app to the
# default query mode and DROP the cross-account sts:AssumeRole grant (this
# happened live). deploy.sh persists explicit flag values in SSM; here we
# re-apply the persisted value whenever no explicit non-empty context value
# was provided. Precedence: explicit -c value > SSM sticky value > absent.
#
# Injection uses node.set_context() BEFORE any stacks are constructed (the
# only window where it is legal). We cannot rely on `App(context=...)`:
# cdk.json ships `"crossAccountDataRoleArn": ""` in context, and the CLI's
# merged context (CDK_CONTEXT_JSON) is applied AFTER the constructor prop —
# the empty string would always override the sticky value. set_context on an
# empty existing value preserves the intended precedence instead. The SSM
# read is fail-soft (stacks/_sticky_context.py): offline synth, missing
# credentials, or an absent parameter simply leave the key unset, so the
# stacks' `try_get_context(...) or <default>` logic keeps working.
# ---------------------------------------------------------------------------
for _ctx_key, _ssm_name in (
    ("system_query_mode", "/adt/system-query-mode"),
    ("crossAccountDataRoleArn", "/adt/cross-account-data-role-arn"),
):
    _explicit = app.node.try_get_context(_ctx_key)
    if _explicit is not None and str(_explicit).strip():
        continue  # explicit -c (or cdk.json non-empty) value wins — no SSM read
    _sticky = resolve_sticky(None, _ssm_name, region=region, default="")
    if _sticky:
        app.node.set_context(_ctx_key, _sticky)
        print(f"[sticky] {_ctx_key} = {_sticky!r} (from SSM {_ssm_name})")

# ---------------------------------------------------------------------------
# HighByte (OT/UNS layer) — opt-in, and sticky once enabled.
#
# The HighByte Intelligence Hub runs the public `highbyte/intelligence-hub`
# image from Docker Hub (no tar, no build pipeline). The container runs only
# with ACCEPT_EULA=Y, which agrees to the HighByte EULA, so the stack sets it
# only from the deployer's explicit -c highbyte_accept_eula=true
# (stacks/_highbyte_options.py::accept_eula_env). Whether the HighByte stack
# is part of this deployment is gated by `highbyte_enabled` (this also gates
# the Network public-subnet reference whose cross-stack export the OPC UA
# stack imports), so once enabled the stack must remain in the deployment.
# It is sticky:
#   1. -c highbyte_enabled=true                          (explicit), or
#   2. SSM /adt/highbyte-enabled == "true"  (set by deploy.sh
#      on the first --highbyte deploy; persists across later deploys).
#
# Enable once with deploy.sh --highbyte --accept-highbyte-eula. deploy.sh
# records both choices in SSM and passes -c highbyte_enabled=true and
# -c highbyte_accept_eula=true on every HighByte deploy. A raw `cdk deploy --all`
# reads the sticky enable from SSM but not the recorded acceptance, so it needs
# -c highbyte_accept_eula=true (and -c highbyte_enabled=true where the SSM
# value is absent); without the acceptance the synth fails with the EULA link.
# The 7-stack reference architecture (no OT/UNS layer) is the result when
# HighByte was never enabled.
# ---------------------------------------------------------------------------


def _highbyte_enabled_in_ssm() -> bool:
    """Read the sticky enable flag from SSM via the shared sticky reader.

    Absence means "not sticky-enabled here", and an explicit -c flag still
    enables HighByte independently. Outside a deploy any read failure (no
    credentials, no region, offline synth) also means False; during a deploy
    (ADT_STICKY_STRICT) a failure other than a missing parameter raises
    StickyReadError (stacks/_sticky_context.py).
    """
    return resolve_sticky_bool(None, "/adt/highbyte-enabled", region=region, default=False)


highbyte_enabled = (
    str(app.node.try_get_context("highbyte_enabled")).lower() == "true"
    or _highbyte_enabled_in_ssm()
)

# ---------------------------------------------------------------------------
# OPC UA servers without HighByte — opt-in, sticky, IMPLIED by highbyte_enabled
# and by sfc_enabled (below).
#
# The simulated per-site OPC UA servers are the telemetry source for more than
# HighByte (SFC reads them too), so they can be deployed on their own:
# -c opcua_enabled=true or SSM /adt/opcua-enabled == "true" (deploy.sh --opcua
# persists it). Sticky for the same reason as HighByte: the stack imports
# network exports, so it must stay in the deployment once enabled. With
# highbyte_enabled the flag is forced on, so nothing changes for HighByte
# deploys; without HighByte the same stack carries only the OPC UA servers.
# ---------------------------------------------------------------------------
# SFC (the SiteWise / S3 Tables writer, ADR-0082) — opt-in, sticky
# (-c sfc_enabled=true or SSM /adt/sfc-enabled; deploy.sh --sfc persists it).
# It IMPLIES opcua_enabled: the OPC UA servers are SFC's only source, so SFC
# can never be deployed with nothing to read. HighByte stays independent.
sfc_enabled = resolve_sticky_bool(
    app.node.try_get_context("sfc_enabled"),
    "/adt/sfc-enabled",
    region=region,
    default=False,
)
opcua_enabled = highbyte_enabled or sfc_enabled or resolve_sticky_bool(
    app.node.try_get_context("opcua_enabled"),
    "/adt/opcua-enabled",
    region=region,
    default=False,
)

# ---------------------------------------------------------------------------
# AWS Quick integration (opt-in) — same infra-flag convention as
# highbyte_enabled (NOT the with_* family: these are infrastructure layers,
# not demo-content layers — ADR-0030). Both default OFF and are NOT sticky:
#
#   quick_api_enabled   deploy.sh --quick-api  → QuickApiGatewayStack (the
#                       IAM/SigV4 REST front door + the query-bridge gateway
#                       target the app provisions when QUICK_API_ENABLED is
#                       on its task env).
# ---------------------------------------------------------------------------
quick_api_enabled = str(app.node.try_get_context("quick_api_enabled")).lower() == "true"
# Connector-only Quick path (-c quick_connector_enabled=true; NOT sticky,
# ADR-0093): the bridge-only adt-quick-gw gateway, the query-bridge env, the
# connector outputs and the client secret, wired by stacks/_quick_connector.py.
# Never QuickApiGatewayStack.
quick_connector_enabled = quick_connector.connector_enabled(app)

# ---------------------------------------------------------------------------
# Observability drill-down (opt-in, sticky) — same discipline as HighByte.
#
# Default OFF: CloudWatch Transaction Search span ingestion has an always-on
# trace-ingestion cost, so the flag is strictly opt-in. deploy.sh owns the
# SSM write (`/adt/observability-enabled`, set on the first
# `--observability` deploy) and the account-level Transaction Search
# enablement; here we only READ (fail-soft — offline synth / missing
# credentials / absent parameter all resolve to disabled). Precedence:
# explicit `-c observability=true` > SSM sticky value > off. Off-switch =
# delete the SSM parameter (same semantics as HighByte). This gates ONLY the
# ECS app's reader side (AGENTS_OBSERVABILITY_DRILLDOWN env + span-read IAM,
# wired post-construction by stacks/_observability.py below) and span
# EMISSION on the agent runtimes (AGENT_OBSERVABILITY_ENABLED).
# ---------------------------------------------------------------------------
observability_enabled = resolve_sticky_bool(
    app.node.try_get_context("observability"),
    "/adt/observability-enabled",
    region=region,
    default=False,
)
if observability_enabled:
    print("[sticky] observability drill-down ENABLED (flag or SSM /adt/observability-enabled)")

# ---------------------------------------------------------------------------
# Orchestrator runtime ARN (managed evaluations — CDK-native, additive).
#
# deploy.sh [2c-eval] persists the READY adt_orchestrator runtime ARN to SSM
# /adt/orchestrator-runtime-arn. Here we only READ it (fail-soft:
# offline synth / missing credentials / absent parameter resolve to "") and
# pass it to stacks/_evaluations.wire_eval_config below, which owns the
# AWS::BedrockAgentCore::OnlineEvaluationConfig resource + execution role and
# sets the EVAL_CONFIG_ORCHESTRATOR task env DIRECTLY from the resource's id
# attribute — the env agents/agents_routes.py::EVAL_CONFIG_MAP consumes for
# Agents-tab scores. Empty ⇒ nothing is added and the template is unchanged.
# First-deploy chicken-and-egg (narrowed): [2c-eval] runs AFTER deploy #1's
# synth, so config + env both land together on deploy #2 (documented in
# deploy.sh).
# ---------------------------------------------------------------------------
orchestrator_runtime_arn = resolve_sticky(
    app.node.try_get_context("orchestrator_runtime_arn"),
    "/adt/orchestrator-runtime-arn",
    region=region,
    default="",
)
if orchestrator_runtime_arn:
    print(f"[sticky] orchestrator runtime ARN = {orchestrator_runtime_arn!r} (evaluations enabled)")

# ---------------------------------------------------------------------------
# Optional custom domain (Route 53 + ACM) — opt-in, sticky.
#
# Serve the base ADT distribution on your own domain: provide the FQDN plus
# the Route 53 PUBLIC hosted zone (same account) and the app creates a
# DNS-validated ACM certificate (us-east-1, the CloudFront certificate region —
# its own small stack, wired cross-region), attaches the alias to the
# distribution, and creates the A/AAAA alias records. See
# stacks/_custom_domain.py. Absent ⇒ nothing is added and the templates are
# unchanged.
#
# Sticky (HighByte discipline): deploy.sh --domain/--hosted-zone-id/
# --hosted-zone-name persist to SSM /adt/custom-domain{,-zone-id,-zone-name};
# here we re-apply the persisted values fail-soft so a routine flag-less
# deploy cannot silently drop the domain (and its DNS records) from a live
# environment. Off-switch = delete the SSM parameters. Partial configuration
# fails LOUD in resolve_custom_domain.
# ---------------------------------------------------------------------------
custom_domain_cfg = resolve_custom_domain(
    resolve_sticky(app.node.try_get_context("custom_domain"),
                   "/adt/custom-domain", region=region, default=""),
    resolve_sticky(app.node.try_get_context("hosted_zone_id"),
                   "/adt/custom-domain-zone-id", region=region, default=""),
    resolve_sticky(app.node.try_get_context("hosted_zone_name"),
                   "/adt/custom-domain-zone-name", region=region, default=""),
)
if custom_domain_cfg and env is None:
    raise RuntimeError(
        "Custom-domain support needs an explicit account/region (the us-east-1 "
        "certificate stack is wired cross-region). Deploy with credentials so "
        "CDK_DEFAULT_ACCOUNT/CDK_DEFAULT_REGION are set (deploy.sh does this)."
    )
if custom_domain_cfg:
    print(f"[sticky] custom domain = {custom_domain_cfg.domain_name!r} "
          f"(zone {custom_domain_cfg.hosted_zone_id})")

# ---------------------------------------------------------------------------
# Demo-data deployment mode — opt-in ladder. Default is BASE (engine only):
#
#   base       (no flags)        engine stacks only (Network, ADTDatabase=Neptune+
#                                DynamoDB, AgentCore, ADTCompute). NO source-system
#                                RDS, no seed, no simulator — the engine has no
#                                dependency on or awareness of any demo data. You
#                                onboard your OWN data sources post-deploy.
#   --seed     with_seed=true    + ADTSeedDataStack (the adt_demo RDS) loaded
#                                with the static sample source-system schemas.
#   --simulator with_simulator=true   + the live ERP simulation (its own Aurora + the
#                                EventBridge sim Lambda + the Streamlit ERP dashboard).
#                                IMPLIES --seed: the simulator's cross-system records
#                                (e.g. MES downtime → CMMS work orders) are written
#                                against seed-defined schemas, so seed must be present.
#
# Gating is via CDK context (deploy.sh sets these); default false → base. NOT sticky:
# unlike HighByte, leaving a flag off removes the demo-data stacks on the next deploy,
# so an existing full-demo environment MUST keep passing the flags. See ADR-0030.
# ---------------------------------------------------------------------------
# HighByte is independent of the demo data. It deploys the OT/UNS layer
# (OPC UA → HighByte → AWS IoT SiteWise) on top of the base engine and needs only
# ADTNetworkStack. When --simulator/--seed are also set, the HighByte setup additionally
# wires the ERP Aurora + adt_demo historian (Postgres) connections; without
# them the stack + setup lambda leave those connections inactive (guarded on empty
# secret ARNs). So --highbyte no longer implies --simulator.
with_simulator = str(app.node.try_get_context("with_simulator")).lower() == "true"
# --simulator implies --seed (the simulator depends on seed-defined schemas).
with_seed = with_simulator or str(app.node.try_get_context("with_seed")).lower() == "true"

# ---------------------------------------------------------------------------
# Cognito Authorization@Edge on the app distribution — toggleable, sticky.
#
# Browser sign-in happens at the CloudFront edge: the first unauthenticated
# request is redirected to the Cognito HOSTED UI, so the application never
# serves its own credential form. One authentication boundary, shared with
# the ERP dashboard and the HighByte UI (SAR cloudfront-authorization-at-edge).
# Resolution: explicit `-c edge_auth=true|false` > sticky SSM
# /adt/edge-auth-enabled (written by deploy.sh --edge-auth on|off) > default =
# ON. /api/* and /health bypass the edge gate by design
# (APIs keep their own 401 auth), so the CLI eval/smoke lanes are unaffected.
# ---------------------------------------------------------------------------
edge_auth_enabled = resolve_sticky_bool(
    app.node.try_get_context("edge_auth"),
    "/adt/edge-auth-enabled",
    region=region,
    default=True,
)
print(f"[sticky] edge auth (Cognito hosted-UI redirect) "
      f"{'ENABLED' if edge_auth_enabled else 'disabled'} "
      f"(context/SSM /adt/edge-auth-enabled; default = on)")

# ---------------------------------------------------------------------------
# Optional external OIDC identity provider for the hosted UI — opt-in, sticky.
#
# Bring your own OpenID Connect provider (any vendor): every user pool that
# carries an Authorization@Edge client (ADT app, ERP dashboard, HighByte UI)
# gets ONE Cognito OIDC identity provider and its edge client lists it next
# to the local Cognito directory, so the hosted UI shows a second sign-in
# button. Local Cognito users keep working (customers still sign in that way
# by default). See stacks/_edge_auth.py and ADR-0072. Absent ⇒ nothing is
# added and every template is byte-identical.
#
# Sticky (custom-domain discipline): deploy.sh --oidc-issuer/--oidc-client-id/
# --oidc-secret-arn/--oidc-name persist to SSM /adt/oidc-{issuer-url,
# client-id,client-secret-arn,provider-name}; here we re-apply the persisted
# values fail-soft so a routine flag-less deploy cannot silently drop the
# corporate sign-in button. Off-switch = delete the SSM parameters. Partial
# configuration fails LOUD in resolve_external_oidc.
# ---------------------------------------------------------------------------
external_oidc_cfg = resolve_external_oidc(
    resolve_sticky(app.node.try_get_context("oidc_issuer_url"),
                   "/adt/oidc-issuer-url", region=region, default=""),
    resolve_sticky(app.node.try_get_context("oidc_client_id"),
                   "/adt/oidc-client-id", region=region, default=""),
    resolve_sticky(app.node.try_get_context("oidc_client_secret_arn"),
                   "/adt/oidc-client-secret-arn", region=region, default=""),
    resolve_sticky(app.node.try_get_context("oidc_provider_name"),
                   "/adt/oidc-provider-name", region=region, default=""),
    oidc_only=resolve_sticky(app.node.try_get_context("oidc_only"),
                             "/adt/oidc-only", region=region, default=""),
)
if external_oidc_cfg:
    print(f"[sticky] external OIDC identity provider = "
          f"{external_oidc_cfg.provider_name!r} ({external_oidc_cfg.issuer_url})"
          + (" — SOLE provider on every edge client (oidc_only)"
             if external_oidc_cfg.oidc_only else ""))

# ---------------------------------------------------------------------------
# Delegated user tokens on the Quick gateway (ADR-0079) — opt-in, sticky.
#
# The dedicated Quick gateway (adt-quick-gw, --quick-api) accepts delegated
# (on-behalf-of) user tokens from an external OIDC identity provider INSTEAD
# of the Cognito service-auth tokens: CUSTOM_JWT against the provider's
# discovery URL, allowed_audience = the gateway URL (incl. /mcp), and a
# REQUEST interceptor that mints the ADR-0049 identity claim per tool call.
# Sticky: deploy.sh --quick-oidc-discovery/--quick-oidc-audience persist to
# SSM /adt/quick-oidc-{discovery-url,audience}; a flag-less deploy re-applies
# them so the authorizer never silently flips back to Cognito. Off-switch =
# delete both parameters. Partial config, or config with neither --quick-api
# nor --quick-connector, fails LOUD in resolve_quick_oidc. Absent ⇒ every template unchanged.
# ---------------------------------------------------------------------------
quick_oidc_cfg = resolve_quick_oidc(
    resolve_sticky(app.node.try_get_context("quick_oidc_discovery_url"),
                   "/adt/quick-oidc-discovery-url", region=region, default=""),
    resolve_sticky(app.node.try_get_context("quick_oidc_audience"),
                   "/adt/quick-oidc-audience", region=region, default=""),
    # Either flag builds adt-quick-gw (see the AgentCoreStack call below).
    quick_api_enabled=quick_api_enabled or quick_connector_enabled,
)
if quick_oidc_cfg:
    print(f"[sticky] Quick gateway accepts delegated user tokens from "
          f"{quick_oidc_cfg.discovery_url} (audience "
          f"{quick_oidc_cfg.audience})")

# ---------------------------------------------------------------------------
# 1. Shared networking
# ---------------------------------------------------------------------------
network = NetworkStack(app, "ADTNetworkStack", env=env)

# Store the demo password in Secrets Manager for post-deploy retrieval.
# The password is either user-provided (-c demo_password=...) or auto-generated.
demo_secret = create_demo_password_secret(network)

# ---------------------------------------------------------------------------
# 2. Demo source-system data (opt-in: --seed, also implied by --simulator)
#    ADTSeedDataStack holds the adt_demo RDS + the static seed loader.
#    It is NOT part of the engine — base never instantiates it.
#    Instantiated BEFORE the simulator stacks: the simulation Lambda's
#    MES-downtime→CMMS bridge writes into the seed DB (cmms_maintenance), so
#    ERPLambdaStack needs the seed stack's secret at synth time.
# ---------------------------------------------------------------------------
seed_data = None
if with_seed:
    seed_data = ADTSeedDataStack(
        app, "ADTSeedDataStack", network_stack=network, env=env,
    )
    seed_data.add_dependency(network)

# ---------------------------------------------------------------------------
# 2b. ERP Simulator databases + Lambdas + dashboard (opt-in: --simulator)
#     The live, self-contained beverage-ERP simulation. Skipped in base/seed modes.
# ---------------------------------------------------------------------------
erp_db = None
erp_lambda = None
erp_dashboard = None
if with_simulator:
    erp_db = ERPDatabaseStack(
        app, "ADTERPDatabaseStack", network_stack=network, env=env,
    )
    erp_db.add_dependency(network)

    erp_lambda = ERPLambdaStack(
        app, "ADTERPLambdaStack",
        network_stack=network, database_stack=erp_db,
        seed_data_stack=seed_data,  # --simulator implies --seed; None-safe anyway
        env=env,
    )
    erp_lambda.add_dependency(erp_db)
    if seed_data is not None:
        erp_lambda.add_dependency(seed_data)

    erp_dashboard = ERPDashboardStack(
        app, "ADTERPDashboardStack",
        network_stack=network, database_stack=erp_db, env=env,
        # Optional custom domain: the callback updater registers Cognito
        # sign-in for erp.<domain> too; the alias/cert/DNS are wired
        # additively below (section 3c) via wire_subdomain_alias.
        ui_domain_name=(
            f"erp.{custom_domain_cfg.domain_name}" if custom_domain_cfg else None
        ),
        # Consume the us-east-1 certificate ARN (only relevant with a custom
        # domain; the flag changes nothing when no cross-region reference exists).
        cross_region_references=bool(custom_domain_cfg),
        # Optional external OIDC provider for the dashboard's hosted UI (None ⇒
        # template unchanged).
        external_oidc=external_oidc_cfg,
    )
    erp_dashboard.add_dependency(erp_db)
    erp_dashboard.add_dependency(erp_lambda)

# ---------------------------------------------------------------------------
# 3. ADT engine datastores + compute + AgentCore
# ---------------------------------------------------------------------------
adt_db = ADTDatabaseStack(
    app, "ADTDatabaseStack", network_stack=network, env=env,
)
adt_db.add_dependency(network)

agentcore = AgentCoreStack(app, "ADTAgentCoreStack", env=env,
                           # Second, bridge-only gateway (adt-quick-gw) for the
                           # Quick query-bridge: quick_api_enabled deploys and
                           # the connector-only path (ADR-0093).
                           quick_api_enabled=quick_api_enabled or quick_connector_enabled,
                           # Edge-auth OAuth client (+ optional external IdP) on
                           # the adt-app pool live HERE, next to the pool and the
                           # query gateway whose CUSTOM_JWT authorizer must allow
                           # the edge client (the forwarded end-user bearer per
                           # ADR-0049 is an edge-client token under edge auth —
                           # ADR-0070 amendment 2026-09-23). ADTComputeStack reads
                           # both handles via its agentcore handle.
                           edge_auth_enabled=edge_auth_enabled,
                           external_oidc=external_oidc_cfg,
                           # ADR-0079: delegated user tokens on adt-quick-gw
                           # (None ⇒ Cognito service auth, template unchanged).
                           quick_oidc=quick_oidc_cfg)
# ADTAgentCoreStack now owns the adt-app user pool + the ADT demo-user custom
# resource (relocated from ADTComputeStack to break the gateway<->pool cycle —
# F1 / ADR-0033). That custom resource reads the `adt-demo-password` secret
# (created in ADTNetworkStack) by name at runtime, so ADTAgentCoreStack must deploy
# after ADTNetworkStack. No reverse dependency exists, so this introduces no cycle.
agentcore.add_dependency(network)

# ---------------------------------------------------------------------------
# 3b. Onboarding control plane (always-on engine stack; ADR-0033)
#     The onboarding runtime role + JWT gateway (Cedar not attached).
#     Part of the engine (you onboard your OWN systems in base mode), so it is
#     instantiated in every data mode. Cross-stack refs go onboarding -> agentcore
#     (adt-app issuer/clients + m2m provider) and onboarding -> network (VPC);
#     ADTAgentCoreStack must NOT reference ADTOnboardingStack, keeping the graph
#     cycle-free. Deploy of the CfnRuntime is gated on the AZ-allowlist
#     prerequisite (see stacks/onboarding_stack.py) — synth is unaffected.
#
#     Created BEFORE ADTComputeStack so the ADT app task-def can read the
#     OnboardingGatewayUrl output (ONBOARDING_GATEWAY_URL env). The resulting
#     link is compute -> onboarding -> agentcore, which stays cycle-free.
# ---------------------------------------------------------------------------
onboarding = OnboardingStack(
    app, "ADTOnboardingStack",
    network_stack=network, agentcore_stack=agentcore, env=env,
)
onboarding.add_dependency(network)
onboarding.add_dependency(agentcore)

# ---------------------------------------------------------------------------
# 3c. Optional custom-domain certificate (us-east-1) — only when configured.
#     CloudFront requires ACM certificates from us-east-1, so the certificate
#     gets its own stack pinned there; cross_region_references carries the ARN
#     into ADTComputeStack's distribution below (same-region deploys degrade
#     to plain exports). See stacks/_custom_domain.py.
# ---------------------------------------------------------------------------
custom_domain_cert = None
if custom_domain_cfg:
    custom_domain_cert = CustomDomainCertificateStack(
        app, "ADTCertificateStack",
        config=custom_domain_cfg,
        # HighByte-enabled deploys also get the highbyte.<domain> certificate
        # for the HighByte UI distribution (consumed in section 4 below).
        include_highbyte=highbyte_enabled,
        # Simulator deploys also get the erp.<domain> certificate for the ERP
        # dashboard distribution (consumed by wire_subdomain_alias below).
        include_erp_dashboard=with_simulator,
        env=cdk.Environment(account=account, region="us-east-1"),
    )

adt_compute = ADTComputeStack(
    app, "ADTComputeStack",
    network_stack=network, adt_database_stack=adt_db,
    erp_database_stack=erp_db,     # None unless --simulator
    seed_data_stack=seed_data,     # None in base → app gets no POSTGRES_* (ADR-0030)
    agentcore_stack=agentcore,
    onboarding_stack=onboarding,   # for ONBOARDING_GATEWAY_URL task-def env
    # Cognito Authorization@Edge gate (resolved above: context > SSM > default
    # ON). OFF ⇒ the synthesized template is unchanged.
    edge_auth_enabled=edge_auth_enabled,
    # External OIDC config — kept for signature compatibility only; the
    # provider + edge client are created upstream in ADTAgentCoreStack
    # (ADR-0070 amendment 2026-09-23) and compute reads the handles.
    external_oidc=external_oidc_cfg,
    # AWS Quick integration opt-in: gates
    # the QUICK_* env vars + /adt/quick-* task-start SSM references so base
    # deploys are unchanged.
    quick_api_enabled=quick_api_enabled,
    # Custom-domain FQDN for the edge-auth callback updater (hosted-UI sign-in
    # must work through the alias too) — same approach as the ERP dashboard's
    # ui_domain_name. None when no custom domain is configured.
    ui_domain_name=(custom_domain_cfg.domain_name if custom_domain_cfg else None),
    env=env,
    # Consume the us-east-1 certificate ARN (only relevant with a custom
    # domain; the flag changes nothing when no cross-region reference exists).
    cross_region_references=bool(custom_domain_cfg),
)
# Opt-in observability drill-down reader (env + span read IAM) — additive
# post-construction wiring, gated by the sticky flag resolved above.
# Span emission on the agent runtimes follows the same flag.
wire_observability_reader(adt_compute, enabled=observability_enabled)
# Connector-only AWS Quick path (ADR-0093): additive post-construction
# wiring onto both engine stacks; a no-op (templates unchanged) when off.
quick_connector.wire_quick_connector(agentcore, adt_compute, enabled=quick_connector_enabled)
# Managed evaluations (CDK-owned CfnOnlineEvaluationConfig + execution role +
# EVAL_CONFIG_ORCHESTRATOR env from the resource's id attr) — additive
# post-construction wiring; no-op while the runtime-ARN SSM value is absent.
wire_eval_config(adt_compute, orchestrator_runtime_arn=orchestrator_runtime_arn)
# Optional custom domain — additive post-construction wiring (alias + viewer
# certificate on the distribution, Route 53 A/AAAA records); no-op when the
# custom-domain context is absent.
if custom_domain_cfg:
    wire_custom_domain(
        adt_compute,
        config=custom_domain_cfg,
        certificate=custom_domain_cert.certificate,
    )
    adt_compute.add_dependency(custom_domain_cert)
    # ERP dashboard on erp.<domain> — same additive seam; only with --simulator
    # (the dashboard stack does not exist otherwise).
    if erp_dashboard is not None and custom_domain_cert.erp_certificate is not None:
        wire_subdomain_alias(
            erp_dashboard,
            subdomain="erp",
            config=custom_domain_cfg,
            certificate=custom_domain_cert.erp_certificate,
            distribution_construct_id="ERPDashboardDistribution",
        )
        erp_dashboard.add_dependency(custom_domain_cert)
adt_compute.add_dependency(adt_db)
adt_compute.add_dependency(agentcore)
adt_compute.add_dependency(onboarding)
if erp_db is not None:
    adt_compute.add_dependency(erp_db)
if seed_data is not None:
    adt_compute.add_dependency(seed_data)

# ---------------------------------------------------------------------------
# 3d. AWS Quick integration (opt-in — quick_api_enabled)
#     QuickApiGatewayStack: IAM/SigV4 REST front door (VPC Link → internal ALB)
#     for an AWS Quick MCP client; it injects the x-quick-auth
#     gateway token (a Secrets Manager secret in ADTComputeStack) the app
#     validates.
#     Docs: docs/quick-web-connector.md.
# ---------------------------------------------------------------------------
quick_apigw = None
if quick_api_enabled:
    quick_apigw = QuickApiGatewayStack(
        app, "QuickApiGatewayStack",
        vpc=network.vpc,
        alb_dns_name=adt_compute.alb_dns_name,
        alb_arn=adt_compute.alb_arn,
        alb_security_group_id=adt_compute.node.find_child("AdtAlbSg").security_group_id,
        gateway_token_secret=adt_compute.quick_gateway_token_secret(),
        env=env,
    )
    quick_apigw.add_dependency(network)
    quick_apigw.add_dependency(adt_compute)   # needs the internal ALB
quick_stacks = [s for s in [quick_apigw] if s is not None]

# ---------------------------------------------------------------------------
# 4. HighByte + OPC UA servers (optional — gated on opcua_enabled; HighByte
#    itself on highbyte_enabled, which implies opcua_enabled)
# ---------------------------------------------------------------------------
highbyte_stacks = []
# The OPC UA servers' own stack (-c opcua_servers_stack=true). With
# HighByte on, the servers otherwise share ADTHighByteOpcuaStack with the hub, so SFC, which reads only
# the servers, waits for the hub's CloudFront distribution and Lambda@Edge
# (about 42 minutes on a fresh account). Not sticky and off by default:
# moving the servers between stacks replaces them, so a deployment keeps the
# shape it was created with. Without HighByte the servers already have the
# stack to themselves and this changes nothing.
opcua_servers = None
opcua_servers_stack = str(app.node.try_get_context("opcua_servers_stack")).lower() == "true"
if opcua_enabled:
    # OPC UA datasets are generated on demand (gzip CSVs, not committed) by
    # highbyte/generate_datasets.py — deploy.sh runs it BEFORE cdk when
    # --highbyte or --opcua is active. Synth itself is PURE: it never writes into the
    # checkout (2026-09 architecture review). A missing dataset is surfaced
    # here as a synth warning with the fix; the Docker build would otherwise
    # fail later on the COPY with a less helpful message.
    _datasets_dir = os.path.join(os.path.dirname(__file__), "highbyte", "datasets")
    _site_csvs = ["site1_milwaukee_bottling.csv.gz", "site2_detroit_automotive.csv.gz",
                  "site3_cedarrapids_food.csv.gz", "site4_indianapolis_machining.csv.gz"]
    _missing_datasets = [f for f in _site_csvs if not os.path.isfile(os.path.join(_datasets_dir, f))]

    # Optional custom domain for the HighByte UI (highbyte.<domain>): pass the
    # config + the us-east-1 certificate only when BOTH HighByte and the custom
    # domain are configured. cross_region_references mirrors ADTComputeStack —
    # the certificate ARN flows in from the us-east-1 ADTCertificateStack.
    _hb_domain_kwargs = {}
    if highbyte_enabled and custom_domain_cfg and custom_domain_cert is not None:
        _hb_domain_kwargs = dict(
            custom_domain_config=custom_domain_cfg,
            ui_certificate=custom_domain_cert.highbyte_certificate,
        )

    if highbyte_enabled and opcua_servers_stack:
        opcua_servers = HighByteOpcuaStack(
            app, "ADTOpcuaServersStack", network_stack=network, env=env,
            include_highbyte=False)
        opcua_servers.add_dependency(network)
        if _missing_datasets:
            cdk.Annotations.of(opcua_servers).add_warning(
                "OPC UA datasets missing: " + ", ".join(_missing_datasets)
                + " - run `python3 highbyte/generate_datasets.py` before deploying.")

    highbyte = HighByteOpcuaStack(
        app, "ADTHighByteOpcuaStack",
        network_stack=network, erp_database_stack=erp_db,  # erp_db=None unless --simulator
        # The adt_demo RDS (historian schema the HighByte setup writes to)
        # lives in ADTSeedDataStack post-Layer-1. seed_data=None unless --seed/--simulator;
        # the stack + setup lambda guard the ERP/historian wiring when these are absent,
        # so the HighByte OPC UA → SiteWise core deploys on base with no demo data.
        seed_data_stack=seed_data,
        env=env,
        cross_region_references=bool(_hb_domain_kwargs),
        # Optional external OIDC provider for the HighByte UI's hosted-UI
        # sign-in (None ⇒ template unchanged).
        external_oidc=external_oidc_cfg,
        **_hb_domain_kwargs,
        # opcua_enabled without highbyte_enabled: OPC UA servers only.
        include_highbyte=highbyte_enabled,
        # The servers' own stack, when split (the hub reads them from it).
        opcua_servers=opcua_servers,
    )
    highbyte.add_dependency(network)
    if opcua_servers is not None:
        highbyte.add_dependency(opcua_servers)
    if _hb_domain_kwargs:
        highbyte.add_dependency(custom_domain_cert)
    if erp_db is not None:
        highbyte.add_dependency(erp_db)
    # Fargate vCPU budget: a new account's default On-Demand quota can be as
    # low as 6 vCPU. With the hub and the four OPC UA servers + SFC sized
    # stop-first (opcua_stop_first), a redeploy adds no vCPU surge from the
    # telemetry children. No extra DependsOn edges: an update-only quota
    # concern must not serialize the fresh-create path.
    if _missing_datasets:
        cdk.Annotations.of(highbyte).add_warning(
            "OPC UA datasets missing: " + ", ".join(_missing_datasets)
            + " — run `python3 highbyte/generate_datasets.py` before deploying "
            "(deploy.sh --highbyte / --opcua does this); the OPC image build will fail without them."
        )

    highbyte_stacks = [highbyte]

#: The stack that holds the OPC UA servers (what SFC reads).
opcua_stack = opcua_servers or (highbyte_stacks[0] if highbyte_stacks else None)
opcua_servers_stacks = [opcua_servers] if opcua_servers is not None else []

# ---------------------------------------------------------------------------
# 4b. SFC — the continuous SiteWise / S3 Tables writer (optional — gated on
#     sfc_enabled, which implies opcua_enabled; ADR-0082). It reads the OPC UA
#     servers from section 4, so it never deploys without them, and HighByte is
#     independent. SfcS3TablesStack owns the CDK-owned S3 Tables
#     bucket/namespace/table + the narrow writer policy (SFC runs AutoCreate:
#     false); its table_bucket_name / namespace_name / table_name / table_arn /
#     sfc_writer_policy flow into SfcStack, which runs the SFC task in the OPC UA
#     servers' VPC and reads their endpoints. Kept in a SEPARATE list from
#     highbyte_stacks on purpose — apply_highbyte_opcua_suppressions below
#     indexes highbyte_stacks[0] expecting the OPC UA stack.
# ---------------------------------------------------------------------------
sfc_stacks = []
if sfc_enabled:
    sfc_s3tables = SfcS3TablesStack(app, "ADTSfcS3TablesStack", env=env)

    sfc = SfcStack(
        app, "ADTSfcStack",
        network_stack=network,
        s3tables_stack=sfc_s3tables,
        opc_ua_endpoints=opcua_stack.opc_ua_endpoints,
        # SFC restarts whenever a deploy replaces an OPC UA server task (the
        # servers' synth-time fingerprint changes this env var): its OPC UA
        # subscriptions do not survive a server restart (rehearsal 2026-10-02).
        # A plain string, never the task-definition ARN token: that becomes a
        # cross-stack export, which CloudFormation will not change while SFC
        # imports it.
        opc_ua_server_revisions=[opcua_stack.opc_ua_server_fingerprint],
        env=env,
    )
    # Transitional (remove after every account has deployed this once): an
    # account deployed with the ARN env var still imports these exports. Keep
    # them for one update so SFC can drop the import first; nothing imports
    # them afterwards, so they may change freely.
    for _svc in opcua_stack.opcua_services:
        opcua_stack.export_value(_svc.task_definition.task_definition_arn)
    sfc.add_dependency(network)
    sfc.add_dependency(sfc_s3tables)
    sfc.add_dependency(opcua_stack)
    # SFC freshness watchdog: a scheduled Lambda in the SFC stack that forces
    # a new SFC deployment when one source's sentinel stops updating while its
    # OPC UA server runs (a server task replaced outside a deploy, or SFC near
    # its memory limit for hours); the server-revision coupling above covers
    # forward deploys only. Not built when the hub's SiteWise pipeline also
    # writes the same properties (the synth says so).
    add_sfc_freshness_watchdog(
        sfc,
        sfc_service=sfc.service,
        opcua_services=[svc.service for svc in opcua_stack.opcua_services],
        highbyte_enabled=highbyte_enabled,
    )
    # No quota ordering edge here: SFC can deploy stop-first
    # (opcua_stop_first), so it surges 0 vCPU and never needs to be
    # kept off any other child's surge window.

    sfc_stacks = [sfc_s3tables, sfc]

# ---------------------------------------------------------------------------
# 4c. Postgres access to the demo databases by security group. Neither the
#     ERP Aurora cluster nor the seed RDS instance admits a CIDR on 5432: each
#     client group outside the database stacks gets its own rule in its own
#     stack (the app task, the AgentCore and onboarding runtimes, the HighByte
#     hub, the ERP Lambdas on the seed DB). See stacks/_db_access.py; a
#     deployment with neither database adds no rule, but still exports the
#     onboarding group id (a deployment that drops its databases needs it).
# ---------------------------------------------------------------------------
wire_database_clients(
    seed_data=seed_data, erp_db=erp_db, erp_lambda=erp_lambda, compute=adt_compute,
    onboarding=onboarding, highbyte=highbyte_stacks[0] if highbyte_stacks else None)

# ---------------------------------------------------------------------------
# 5. Identity Center — demo portal access (Bedrock + SiteWise read-only)
#    Only deployed in regions where IAM Identity Center is configured.
#    Set context: cdk deploy --context deploy_identity_center=true
# ---------------------------------------------------------------------------
deploy_identity_center = app.node.try_get_context("deploy_identity_center") == "true"
if deploy_identity_center:
    identity_center = ADTIdentityCenterStack(app, "ADTIdentityCenterStack", env=env)
    identity_center_stacks = [identity_center]
else:
    identity_center = None
    identity_center_stacks = []

# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------
# ERP simulator stacks are present only in --simulator mode; the seed-data stack only
# in --seed/--simulator. Filter out the None placeholders so base deploys tag/suppress
# only the stacks that exist.
erp_stacks = [s for s in [erp_db, erp_lambda, erp_dashboard] if s is not None]
seed_stacks = [seed_data] if seed_data is not None else []
custom_domain_stacks = [custom_domain_cert] if custom_domain_cert is not None else []
for stack in [network] + erp_stacks + seed_stacks + [adt_db, agentcore, adt_compute, onboarding] + opcua_servers_stacks + highbyte_stacks + sfc_stacks + identity_center_stacks + custom_domain_stacks + quick_stacks:
    cdk.Tags.of(stack).add("Project", "AgenticDigitalThread")
    cdk.Tags.of(stack).add("ManagedBy", "CDK")

# Non-workshop hardening (MFA OPTIONAL + TOTP on the human pools, ALBs drop
# invalid header fields, ADTNetworkStack owns the account's API Gateway
# CloudWatch role for every REST API stage), applied to the built stacks; a
# no-op on a workshop synth. See stacks/_sample_hardening.py for why it is not
# in the stack files.
_sample_hardening.harden(
    agentcore=agentcore, erp_dashboard=erp_dashboard,
    highbyte=highbyte_stacks[0] if highbyte_stacks else None,
    network=network)

# ---------------------------------------------------------------------------
# cdk-nag suppressions — sample-grade, documented in stacks/_nag_suppressions.py.
# Applied after all stacks are instantiated so that every child construct is
# in scope when the stack-level suppressions are registered. Each rule includes
# a `reason` describing why the trade-off is acceptable for a demo workload.
# ---------------------------------------------------------------------------
for stack in [network] + erp_stacks + seed_stacks + [adt_db, agentcore, adt_compute, onboarding] + opcua_servers_stacks + highbyte_stacks + sfc_stacks + identity_center_stacks + custom_domain_stacks + quick_stacks:
    apply_sample_suppressions(stack)

apply_network_suppressions(network)
if erp_db is not None:
    apply_erp_database_suppressions(erp_db)
    apply_erp_lambda_suppressions(erp_lambda)
    apply_erp_dashboard_suppressions(erp_dashboard)
if seed_data is not None:
    apply_adt_seed_data_suppressions(seed_data)
apply_adt_database_suppressions(adt_db)
apply_agentcore_suppressions(agentcore)
apply_adt_compute_suppressions(adt_compute)
apply_onboarding_suppressions(onboarding)
if quick_apigw is not None:
    apply_quick_api_suppressions(quick_apigw)

if sfc_enabled:
    apply_sfc_suppressions(sfc_stacks[1])

# checkov skips (template Metadata, honored by checkov) — documented per
# finding in stacks/_checkov_skips.py.
apply_agentcore_checkov_skips(agentcore, agentcore.agent_repo)
apply_adt_compute_checkov_skips(adt_compute, adt_compute.distribution, adt_compute.alb)
apply_onboarding_checkov_skips(onboarding)
if opcua_enabled:
    # highbyte_stacks == [opcua]
    apply_highbyte_opcua_suppressions(highbyte_stacks[0])
    for _servers in opcua_servers_stacks:
        apply_highbyte_opcua_suppressions(_servers)

# ---------------------------------------------------------------------------
# cdk-nag — AWS Solutions security checks
# Run with: cdk synth (warnings printed) or cdk deploy
# Disable temporarily by setting context: cdk synth --context cdk_nag_disabled=true
# ---------------------------------------------------------------------------
if not app.node.try_get_context("cdk_nag_disabled"):
    cdk.Aspects.of(app).add(AwsSolutionsChecks(verbose=True))

app.synth()
