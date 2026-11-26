# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
HighByte OPC UA Integration Stack

This stack creates the HighByte Intelligence Hub and OPC UA server infrastructure:
- ECS Fargate service for HighByte Intelligence Hub (ApplicationLoadBalancedFargateService)
- ECS Fargate services for OPC UA servers — 4 simulated sites
  (Milwaukee, Detroit, CedarRapids, Indianapolis — ADR-0083)
- CloudFront distribution for secure external access to HighByte
- EFS file system for HighByte persistent storage
- Setup Lambda to configure HighByte via REST API
- Secrets Manager for HighByte admin password

This is a standalone stack that imports VPC from the ERP Beverage Network stack.
The HighByte container runs the unmodified public `highbyte/intelligence-hub`
image from Docker Hub. It runs only with ACCEPT_EULA=Y, which agrees to the
HighByte EULA, so the stack sets it only from the deployer's explicit
acceptance (context highbyte_accept_eula=true; see _highbyte_options.py). A small
init container seeds the HighByte application-settings file to EFS on first boot
so the MQTT broker, REST Data Server, and MCP server are enabled — the public
image's defaults leave REST/MCP disabled. The HighByte license is uploaded by
an administrator via the HighByte UI post-startup (the HighByte documented
licensing workflow).
"""

import hashlib
import json
import os
import re
from typing import Optional

from aws_cdk import (
    Stack,
    Fn,
    CfnOutput,
    CfnParameter,
    Duration,
    RemovalPolicy,
    SecretValue,
    CustomResource,
    custom_resources as cr,
    aws_certificatemanager as acm,
    aws_cognito as cognito,
    aws_ec2 as ec2,
    aws_ecr_assets as ecr_assets,
    aws_ecs as ecs,
    aws_ecs_patterns as ecs_patterns,
    aws_efs as efs,
    aws_iam as iam,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_secretsmanager as secretsmanager,
    aws_lambda as lambda_,
    aws_elasticloadbalancingv2 as elbv2,
)
from constructs import Construct

from stacks import _external_images, _highbyte_options, _workshop_story
from stacks._checkov_skips import checkov_skip
from stacks._cognito_domain import domain_suffix, pin_domain_to_account
from stacks._custom_domain import CustomDomainConfig, wire_subdomain_alias
from stacks._edge_auth import (
    ExternalOidcConfig,
    add_demo_user,
    create_callback_updater,
    create_demo_user_provider,
    create_edge_oauth_client,
    create_external_oidc_provider,
    create_standard_web_acl,
    deploy_edge_auth,
    edge_behavior_parts,
    guarded_behavior,
    edge_http_headers,
)
from stacks._eni_drain import detach_before_delete, drain_on_delete
from stacks._os_patch import patched_image_options
from stacks._staging import stage_tree

# OPC UA + HighByte port assignments
HIGHBYTE_UI_PORT = 45245
HIGHBYTE_MCP_PORT = 8885   # MCP moved to shared HTTP port in 4.4.0 (was 45345 in 4.3.x)
HIGHBYTE_DOCS_PORT = 8885  # Swagger API docs port
OPC_UA_PORT = 26543

# Public HighByte Intelligence Hub image (Docker Hub), run by DIGEST: a tag can
# move, and ECS pulls the image again at every task start. The digest is the
# linux/amd64 manifest of HIGHBYTE_IMAGE_SOURCE, pinned once in
# stacks/_external_images.py (the Workshop Studio package reads the same pins).
# The release must stay at 4.4.2 or later for i3X v1.0 compatibility, and
# 4.5.3 (pinned 2026-10-12) passed the setup contract on a local hub with the
# setup Lambda unchanged. The container needs ACCEPT_EULA=Y, set only from the
# deployer's explicit acceptance (_highbyte_options.accept_eula_env).
# https://hub.docker.com/r/highbyte/intelligence-hub
HIGHBYTE_IMAGE_SOURCE = _external_images.EXTERNAL_IMAGES["highbyte-hub"]["source"]
HIGHBYTE_IMAGE = _external_images.task_image_ref("highbyte-hub")
#: Where the hub keeps its 4.5 knowledge graph (HIGHBYTE_GRAPHDB_DIR): a
#: directory in the container's writable layer (the image's /tmp is 1777 and
#: the task's root filesystem is not read-only), so the files it rewrites at
#: every start and project import stay off the EFS appData mount.
HIGHBYTE_GRAPHDB_DIR = "/tmp/graph-db"  # nosec B108 - container-local path inside the hub task, not the host's /tmp
# Minimal stock image the demo's settings-seed init container runs on (no build
# needed), by its linux/amd64 digest from stacks/_external_images.py (BusyBox
# 1.36 on Amazon ECR Public, pulled by the deploying account itself). The seed
# can also run on the hub image (4.5.3 is Ubuntu-based: dash for sh, mawk for
# awk; the seed script and awk program use POSIX features only).
SETTINGS_SEED_IMAGE = _external_images.task_image_ref("settings-seed")

#: POSIX awk program the settings-seed init container runs on an EXISTING
#: EFS settings file. It enforces two security settings, preserving every
#: other persisted UI change byte for byte, and FAILS CLOSED on anything it
#: does not recognise:
#:
#: 1. the REST/MCP server's allowAnonymous=false, and
#: 2. MCP Configuration Tools off. From HighByte 4.5 on, with them enabled the
#:    MCP server lists config_* tools that create and edit connections,
#:    models, instances and pipelines. The shipped settings file turns them
#:    off, but only a FIRST boot writes it; a hub that already booted keeps
#:    the persisted value (a 4.4.2 hub persisted "enabled", and 4.5.3 kept it
#:    across the upgrade: local probe, 2026-10-11).
#:
#: It tracks brace depth (quoted strings stripped first, so braces inside values
#: do not count) and the key that opened each object, so every target is
#: located by structure, not by a fixed indent, and every key is matched by
#: its property NAME at the start of the line, never by a word inside a string
#: value.
#:
#: allowAnonymous: the direct children `settings.httpServer` (the name 4.4.2
#: and 4.5.3 serialize our `restDataServer` block under) and
#: `settings.restDataServer`. Inside a target section an
#: `"allowAnonymous" : true|false` line is accepted -- an existing false is
#: kept, a true is replaced -- and `hbMqttBroker.allowAnonymous` (the MQTT
#: broker, not internet-reachable) is never touched.
#:
#: Configuration Tools, in both shapes the hub reads:
#: `settings.restDataServer.mcpConfigTools` (the shipped input shape, what a
#: first boot writes) and `settings.serversConfig.mcpServer.configTools` (what
#: 4.4.2 and 4.5.3 persist). The value is the hub's two-value enum: an
#: `"enabled"` line is replaced with `"disabled"`, an existing `"disabled"` is
#: kept.
#:
#: It exits NONZERO, naming the problem on stderr, when: no allowAnonymous
#: target section is found in the one-key-per-line format (e.g. minified JSON),
#: an allowAnonymous target section has no allowAnonymous key, an
#: allowAnonymous line is not a plain boolean, no Configuration Tools section
#: of either shape is found, a Configuration Tools section has no key of its
#: shape, a Configuration Tools line is not "enabled" or "disabled", or the
#: braces do not balance. The caller (_SETTINGS_SEED_SCRIPT) then discards the
#: output and leaves the original file untouched.
#:
#: The output ends with a newline only when the input did (the caller passes
#: ``-v final_nl=0`` when it did not; default 1), so an already-reconciled
#: file comes out byte-identical. POSIX awk only: it runs on the demo's
#: busybox awk and on the workshop hub image's awk (mawk on the Ubuntu-based
#: 4.5.3 image); tests/test_highbyte_settings_seed_images.py runs it in both
#: images.
_SETTINGS_PATCH_AWK = r'''
function fail(msg) {
    printf "settings-seed: REFUSING to install %s: %s\n", FILENAME, msg > "/dev/stderr"
    failed = 1
    exit 1
}
function target(d) {
    return d == 3 && name[2] == "settings" && (name[3] == "httpServer" || name[3] == "restDataServer")
}
# The Configuration Tools section open at depth d ("" when none).
function tools_target(d) {
    if (d == 3 && name[2] == "settings" && name[3] == "restDataServer")
        return "settings.restDataServer"
    if (d == 4 && name[2] == "settings" && name[3] == "serversConfig" && name[4] == "mcpServer")
        return "settings.serversConfig.mcpServer"
    return ""
}
# The key the hub reads in that section.
function tools_key(sec) {
    if (sec == "settings.restDataServer")
        return "mcpConfigTools"
    return "configTools"
}
BEGIN {
    depth = 0; nseen = 0; ntools = 0; failed = 0
    if (final_nl == "") final_nl = 1
}
{
    raw = $0
    # Anchored to the property NAME: the word in a string value
    # (e.g. "schemeCertificate" : "allowAnonymous") is not the key.
    if (raw ~ /^[ \t]*"allowAnonymous"[ \t]*:/ && target(depth)) {
        if (raw !~ /^[ \t]*"allowAnonymous"[ \t]*:[ \t]*(true|false)[ \t]*,?[ \t]*$/)
            fail("unrecognised allowAnonymous line in settings." name[3] ": " raw)
        if (raw ~ /true/) sub(/true/, "false", raw)
        found[name[3]] = 1
    }
    # The same for Configuration Tools: the property name at the start of the
    # line, inside a Configuration Tools section, is its shape's key.
    tsec = tools_target(depth)
    if (tsec != "" && raw ~ /^[ \t]*"[A-Za-z0-9_]+"[ \t]*:/) {
        tkey = raw
        sub(/^[ \t]*"/, "", tkey)
        sub(/".*$/, "", tkey)
        if (tkey == tools_key(tsec)) {
            if (raw !~ /^[ \t]*"[A-Za-z0-9_]+"[ \t]*:[ \t]*"(enabled|disabled)"[ \t]*,?[ \t]*$/)
                fail("unrecognised " tkey " line in " tsec ": " raw)
            sub(/"enabled"/, "\"disabled\"", raw)
            tools_found[tsec] = 1
        }
    }
    key = ""
    if (raw ~ /^[ \t]*"[A-Za-z0-9_]+"[ \t]*:[ \t]*[{][ \t]*$/) {
        key = raw
        sub(/^[ \t]*"/, "", key)
        sub(/".*$/, "", key)
    }
    s = raw
    gsub(/"([^"\\]|\\.)*"/, "\"\"", s)
    n = length(s)
    for (i = 1; i <= n; i++) {
        c = substr(s, i, 1)
        if (c == "{") {
            depth++
            name[depth] = key
            key = ""
            if (target(depth) && !(name[3] in seen)) { seen[name[3]] = 1; nseen++ }
            tsec = tools_target(depth)
            if (tsec != "" && !(tsec in tools_seen)) { tools_seen[tsec] = 1; ntools++ }
        } else if (c == "}") {
            if (depth <= 0) fail("unbalanced braces")
            name[depth] = ""
            depth--
        }
    }
    if (NR > 1) printf "\n"
    printf "%s", raw
}
END {
    if (failed) exit 1
    if (depth != 0) fail("unbalanced braces (truncated or unrecognised format)")
    if (nseen == 0) fail("no settings.httpServer or settings.restDataServer section in the recognised one-key-per-line format")
    for (sec in seen) if (!(sec in found)) fail("settings." sec " has no allowAnonymous key")
    if (ntools == 0) fail("no settings.restDataServer or settings.serversConfig.mcpServer section to turn MCP Configuration Tools off in")
    for (sec in tools_seen) if (!(sec in tools_found)) fail(sec " has no " tools_key(sec) " key")
    if (NR > 0 && final_nl + 0 == 1) printf "\n"
}
'''

#: The settings-seed init container's script (``/bin/sh -c``). First boot:
#: write the seeded settings. Existing file: run _SETTINGS_PATCH_AWK into a
#: temp file (telling it whether the file ends with a newline, so unrelated
#: bytes stay as they were) and atomically rename it over the original ONLY
#: if the patch succeeded; on any awk or mv failure remove the temp file,
#: leave the original byte-for-byte untouched, log why, and exit nonzero. The
#: hub container depends on this container's SUCCESS, so a refusal keeps the
#: hub down rather than letting it start on an un-reconciled (possibly
#: anonymous, or Configuration Tools on) file.
_SETTINGS_SEED_SCRIPT = r'''set -u
mkdir -p "$(dirname "$SETTINGS_FILE")/license" || exit 1
if [ -f "$SETTINGS_FILE" ]; then
    echo "Settings present on EFS -- enforcing the REST/MCP allowAnonymous=false and MCP Configuration Tools off, keeping all other saved settings"
    rm -f "$SETTINGS_FILE.tmp"
    final_nl=1
    [ -n "$(tail -c 1 "$SETTINGS_FILE")" ] && final_nl=0
    if ! awk -v final_nl="$final_nl" "$SETTINGS_PATCH_AWK" "$SETTINGS_FILE" > "$SETTINGS_FILE.tmp"; then
        rm -f "$SETTINGS_FILE.tmp"
        echo "settings-seed: FAILED CLOSED -- $SETTINGS_FILE was not recognised and is left unchanged; the hub will not start until it is fixed or removed" >&2
        exit 1
    fi
    if ! mv "$SETTINGS_FILE.tmp" "$SETTINGS_FILE"; then
        rm -f "$SETTINGS_FILE.tmp"
        echo "settings-seed: FAILED CLOSED -- could not install the patched settings; $SETTINGS_FILE is left unchanged and the hub will not start" >&2
        exit 1
    fi
    echo "settings-seed: allowAnonymous=false enforced and MCP Configuration Tools disabled in $SETTINGS_FILE"
else
    echo "First boot -- seeding settings to $SETTINGS_FILE"
    printf "%s" "$HB_SETTINGS_JSON" > "$SETTINGS_FILE" || exit 1
fi
'''


def _registry_image(ref: str):
    """A ContainerImage for a registry ref. When the ref carries CloudFormation
    intrinsics (``${AWS::AccountId}`` for an account-ECR ref) it is wrapped in
    Fn.sub so CloudFormation resolves them at deploy time; a plain public ref
    is used verbatim."""
    name = Fn.sub(ref) if "${" in ref else ref
    return ecs.ContainerImage.from_registry(name)


#: What marks an image ref in this account's own ECR (the CDK bootstrap
#: container-assets repository).
_ACCOUNT_ECR_MARKER = "${AWS::AccountId}.dkr.ecr."


def _account_ecr_repository_arn(ref: str) -> Optional[str]:
    """The ECR repository ARN, as an Fn::Sub template, behind an account-ECR
    image ref ``<account>.dkr.ecr.<region>.<suffix>/<repo>[:tag][@digest]``.
    None for any other ref: a public registry pull needs no IAM."""
    if _ACCOUNT_ECR_MARKER not in ref:
        return None
    host, _, path = ref.partition("/")
    region = host.split(".dkr.ecr.", 1)[1].split(".", 1)[0]
    repo = re.sub(r":[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$", "", re.sub(r"@sha256:[0-9a-f]{64}$", "", path))
    return f"arn:${{AWS::Partition}}:ecr:{region}:${{AWS::AccountId}}:repository/{repo}"



def server_fingerprint(highbyte_root: str, synth_inputs: list) -> str:
    """A synth-time digest of what the OPC UA server tasks run: the image's
    build inputs (``highbyte/.dockerignore`` allowlist: the server sources and
    the site datasets), this stack's own source and the story context it
    reads. A plain string, so a stack that depends on it (SFC) gets no
    cross-stack export. An export of the task-definition ARN cannot change
    while another stack imports it, which failed every update that replaced
    a server task (rehearsal 2026-10-02). Over-inclusive on purpose: an extra
    SFC restart is harmless, a missed one leaves SiteWise empty."""
    digest = hashlib.sha256(json.dumps(synth_inputs, default=str).encode())
    server_dir = os.path.join(highbyte_root, "components", "opc-server")
    paths = [os.path.abspath(__file__)]
    for root, dirs, files in os.walk(server_dir):
        dirs[:] = sorted(d for d in dirs if d not in ("node_modules", "datasets"))
        paths += [os.path.join(root, f) for f in sorted(files)]
    datasets = os.path.join(highbyte_root, "datasets")
    if os.path.isdir(datasets):
        paths += [os.path.join(datasets, f) for f in sorted(os.listdir(datasets)) if f.endswith(".csv.gz")]
    for path in paths:
        digest.update(os.path.relpath(path, highbyte_root).encode())
        with open(path, "rb") as fh:
            digest.update(hashlib.sha256(fh.read()).digest())
    return digest.hexdigest()[:16]


def setup_revision(setup_lambda_src: str, config_file_src: str,
                   settings_path: str, synth_inputs: dict) -> str:
    """A deterministic digest of everything that changes what the HighByte
    setup custom resource DOES: the setup Lambda sources (index.mjs,
    project-transform.mjs, mcp-credential.mjs — the OPC/Bedrock/SiteWise
    transform + MCP reconcile logic), the bundled project JSON it imports, the
    seeded settings JSON, and the behaviour-affecting literal inputs in
    *synth_inputs* (the SiteWise pipeline mode, and the hub image ref when it
    is known at synth). Same inputs give the same hash; any of them changing
    changes the hash, so CloudFormation sends the CR an Update (the old fixed
    Timestamp never did). Token-valued inputs (endpoints, the workshop's hub
    image parameter) are carried as separate CR properties, not hashed, since
    their value is not known at synth."""
    digest = hashlib.sha256(json.dumps(synth_inputs, sort_keys=True, default=str).encode())
    paths = []
    for root, dirs, files in os.walk(setup_lambda_src):
        dirs[:] = sorted(d for d in dirs if d not in ("node_modules", "__pycache__"))
        paths += [os.path.join(root, f) for f in sorted(files)]
    paths += [config_file_src, settings_path]
    for path in paths:
        if not os.path.isfile(path):
            continue
        digest.update(os.path.basename(path).encode())
        with open(path, "rb") as fh:
            digest.update(hashlib.sha256(fh.read()).digest())
    return digest.hexdigest()


class HighByteOpcuaStack(Stack):
    """
    HighByte OPC UA Stack creating HighByte Intelligence Hub and OPC UA server on ECS Fargate.

    - Uses ApplicationLoadBalancedFargateService for HighByte
    - Uses NetworkLoadBalancedFargateService for OPC UA
    - Configures HighByte via REST API using a setup Lambda
    - Loads large files from S3
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        network_stack=None,
        erp_database_stack=None,
        seed_data_stack=None,
        custom_domain_config: Optional[CustomDomainConfig] = None,
        ui_certificate: Optional[acm.ICertificate] = None,
        external_oidc: Optional[ExternalOidcConfig] = None,
        include_highbyte: bool = True,
        opcua_servers: Optional["HighByteOpcuaStack"] = None,
        **kwargs,
    ) -> None:
        # include_highbyte=False deploys only the OPC UA servers and their
        # endpoints (for SFC). Same stack id, so enabling HighByte later adds
        # to this stack instead of moving the servers. opcua_servers (the
        # workshop's -c opcua_servers_stack=true, app.py) is such a
        # servers-only stack: the hub then reads its servers instead of
        # creating them here, so SFC need not wait for the hub's CloudFront.
        description = (
            "ADT - OPC UA servers (x4 sites) + HighByte instance (ECS)"
            if include_highbyte else "ADT - OPC UA servers (ECS)"
        )
        super().__init__(scope, construct_id, description=description, **kwargs)
        self.include_highbyte = include_highbyte
        self._network_stack = network_stack
        self._erp_database_stack = erp_database_stack
        self._seed_data_stack = seed_data_stack
        # Optional custom domain for the HighByte UI (highbyte.<domain>). Both
        # values arrive together from app.py (config + the us-east-1 ACM
        # certificate from CustomDomainCertificateStack) or not at all. When
        # absent, the distribution keeps its *.cloudfront.net name only.
        self._custom_domain_config = custom_domain_config
        self._ui_certificate = ui_certificate
        # Optional external OIDC identity provider for the HighByte UI's
        # hosted-UI sign-in (app.py resolves the sticky oidc_* trio; None ⇒
        # template unchanged).
        self._external_oidc = external_oidc
        if opcua_servers is not None and (not include_highbyte or opcua_servers.include_highbyte):
            raise ValueError("opcua_servers is a servers-only stack for a hub stack")
        self._opcua_servers = opcua_servers

        self._create_resources(include_highbyte)

    def _create_resources(self, include_highbyte: bool) -> None:
        """Load settings, then create the OPC UA (and optionally HighByte) resources."""
        # Get configuration from context (cdk.json)
        self.config_url = self.node.try_get_context("config_url")

        # Resource sizing and the SiteWise pipeline switch: synth context with
        # the demo's values as defaults (stacks/_highbyte_options.py; the
        # workshop live package sets them). Demo: hub 8 vCPU / 16 GB, OPC UA
        # servers 1 vCPU / 2 GB, SiteWise pipeline enabled.
        self.sizing = _highbyte_options.sizing_from_node(self.node)
        self.sitewise_pipeline = _highbyte_options.sitewise_pipeline_from_node(self.node)
        # Workshop: the images are deploy-time parameters (the root passes the
        # route its Custom::ADTExternalImages resource picked). Read first, so
        # an image key given with the switch gets the switch's own message.
        self.image_parameters = _highbyte_options.image_parameters_from_node(self.node)
        # Container images: default to the demo's registry refs (digest refs);
        # a deployer override must be pinned by digest too.
        self.hub_image, self.settings_seed_image = _highbyte_options.images_from_node(
            self.node, HIGHBYTE_IMAGE, SETTINGS_SEED_IMAGE)
        # The hub runs only with ACCEPT_EULA=Y, and setting it agrees to the
        # HighByte EULA, so it comes only from the deployer's explicit
        # acceptance (context highbyte_accept_eula=true); a synth that
        # includes the hub without it fails loud with the EULA link. The OPC
        # UA servers alone run no HighByte software and need no acceptance.
        self.accept_eula = (_highbyte_options.accept_eula_env(self.node)
                            if include_highbyte else None)
        self.highbyte_cpu = self.sizing.hub_cpu
        self.highbyte_memory = self.sizing.hub_memory
        self.opcua_cpu = self.sizing.opcua_cpu
        self.opcua_memory = self.sizing.opcua_memory

        self._import_vpc()
        if not include_highbyte:
            self._create_opcua_only()
            return
        self._create_highbyte_resources()

    def _create_opcua_only(self) -> None:
        """OPC UA servers and their endpoint exports only (include_highbyte=False)."""
        self._create_opcua_service()
        self._create_opcua_exports()

    def _create_highbyte_resources(self) -> None:
        """OPC UA servers plus HighByte, its edge auth, CloudFront and setup Lambda."""
        self._create_highbyte_efs()
        self._create_password_secret()
        if self._opcua_servers is None:
            self._create_opcua_service()
        else:
            self._adopt_opcua_servers(self._opcua_servers)
        self._create_highbyte_service()
        # Cognito user pool + hosted UI for the HighByte UI edge auth. The SAR
        # Authorization@Edge app must exist before the distribution so its
        # Lambda@Edge version ARNs can be wired into the L2 behaviors.
        self._create_cognito_user_pool()
        self._create_edge_auth()
        self._create_cloudfront_distribution()
        # Patch the Cognito app client callback URLs to the real domain(s),
        # then pre-create the ADT demo user in the HighByte UI user pool.
        self._create_callback_updater()
        self._create_default_user()
        self._create_setup_lambda_and_exports()
        self._create_auth_exports()

    def _import_vpc(self) -> None:
        """Use the VPC from the shared ADTNetworkStack (or CFN exports when
        standalone) and create the HighByte / OPC UA security groups this stack
        owns.

        The highbyte_sg / opcua_sg were moved out of ADTNetworkStack (which used
        to define every demo/OT SG) so a base engine-only deploy carries no OT
        networking. This stack now ALWAYS creates its own, regardless of how the
        VPC was obtained.
        """
        if self._network_stack:
            self.vpc = self._network_stack.vpc
            self.aurora_secret_arn = self._erp_database_stack.db_secret.secret_arn if self._erp_database_stack else ""
        else:
            # Fallback: import VPC from CloudFormation exports (standalone deployment)
            vpc_id = Fn.import_value("ERPNetworkStack-VpcId")
            private_subnet_ids = Fn.split(",", Fn.import_value("ERPNetworkStack-PrivateSubnetIds"))
            public_subnet_ids = Fn.split(",", Fn.import_value("ERPNetworkStack-PublicSubnetIds"))
            self.vpc = ec2.Vpc.from_vpc_attributes(
                self, "ImportedVpc", vpc_id=vpc_id,
                availability_zones=Stack.of(self).availability_zones[:2],
                private_subnet_ids=[Fn.select(0, private_subnet_ids), Fn.select(1, private_subnet_ids)],
                public_subnet_ids=[Fn.select(0, public_subnet_ids), Fn.select(1, public_subnet_ids)],
            )
            self.aurora_secret_arn = Fn.import_value("ADTERPDatabaseStack-SecretArn")

        # These SGs are HighByte's only; each OPC UA service has its own.
        if self.include_highbyte:
            self._create_security_groups()

    def _create_security_groups(self) -> None:
        """Create the HighByte / OPC UA security groups (moved out of
        ADTNetworkStack). Preserves the original ports/peers/allow_all_outbound.

        - highbyte_sg: HighByte Intelligence Hub SG (all-outbound for package
          downloads); ingress on 8080 from the VPC CIDR.
        - opcua_sg: OPC UA server SG (all-outbound); ingress on 4840 from
          highbyte_sg.
        """
        # HighByte Security Group
        self.highbyte_sg = ec2.SecurityGroup(
            self,
            "HighByteSG",
            vpc=self.vpc,
            description="Security group for HighByte Intelligence Hub",
            allow_all_outbound=True,  # Needs internet for package downloads
        )

        # OPC UA Security Group
        self.opcua_sg = ec2.SecurityGroup(
            self,
            "OPCUASG",
            vpc=self.vpc,
            description="Security group for OPC UA server",
            allow_all_outbound=True,  # Needs internet for package downloads
        )

        # Allow OPC UA traffic from HighByte on port 4840
        self.opcua_sg.add_ingress_rule(
            peer=self.highbyte_sg,
            connection=ec2.Port.tcp(4840),
            description="Allow OPC UA from HighByte",
        )

        # Allow HighByte web interface access on port 8080 from the VPC CIDR
        self.highbyte_sg.add_ingress_rule(
            peer=ec2.Peer.ipv4(self.vpc.vpc_cidr_block),
            connection=ec2.Port.tcp(8080),
            description="Allow web interface access from VPC",
        )

    def _create_highbyte_efs(self) -> None:
        """Create EFS file system for HighByte persistent storage."""
        self.highbyte_efs = efs.FileSystem(
            self,
            "HighByteFileSystem",
            file_system_name="highbyte-file-system",
            vpc=self.vpc,
            encrypted=True,  # AWS-managed key; no extra KMS setup required
            removal_policy=RemovalPolicy.DESTROY,
        )

        # Create access point
        self.highbyte_access_point = self.highbyte_efs.add_access_point(
            "HighByteAccessPoint",
            path="/highbyte",
            posix_user=efs.PosixUser(uid="1000", gid="1000"),
            create_acl=efs.Acl(owner_uid="1000", owner_gid="1000", permissions="777"),
        )

    def _create_password_secret(self) -> None:
        """Create Secrets Manager secret for HighByte admin password.

        The value mirrors the shared demo password (adt-demo-password, owned by
        ADTNetworkStack) via a CloudFormation dynamic reference rather than baking
        a plaintext value in at synth time. This keeps the HighByte admin
        password in sync with the demo password — whether that was supplied via
        `-c demo_password=...` or generated once by CloudFormation — and means
        redeploys do not rotate it. ADTHighByteOpcuaStack depends on ADTNetworkStack,
        so the referenced secret always exists first.
        """
        self.password_secret = secretsmanager.Secret(
            self,
            "HighBytePassword",
            secret_name="HighBytePassword",  # nosec B106 - Secrets Manager construct name or JSON template, not credential value
            description="Administrator password for HighByte IntelligenceHub",
            secret_string_value=SecretValue.secrets_manager("adt-demo-password"),
        )

    # (site_name, construct_id) per bundled OPC UA server. SITE_NAME selects the
    # dataset baked into the image (/app/datasets/<SITE_NAME>.csv.gz, see
    # server.sh and components/opc-server/Dockerfile). Order matters: the
    # setup Lambda receives OPC_UA_ENDPOINT[_2.._N] by position and the first
    # site stays the backward-compatible single-endpoint reference.
    OPC_UA_SITES = (
        ("Milwaukee", "OpcMilwaukee"),
        ("Detroit", "OpcDetroit"),
        ("CedarRapids", "OpcCedarRapids"),
        ("Indianapolis", "OpcIndianapolis"),  # ADR-0083: carries CMX-441 live
    )

    def _create_opcua_service(self) -> None:
        """Create the OPC UA servers, one NetworkLoadBalancedFargateService per
        site in ``OPC_UA_SITES`` (Milwaukee, Detroit, CedarRapids, Indianapolis).

        The image context is ``highbyte/`` (allowlist ``highbyte/.dockerignore``)
        with ``file=components/opc-server/Dockerfile``, so the Dockerfile COPYs
        the generated ``highbyte/datasets/*.csv.gz`` directly. Synth never
        copies datasets into the source tree (``stacks/_staging.py``).
        """
        highbyte_root = os.path.join(os.path.dirname(__file__), "..", "highbyte")
        opc_image = ecs.ContainerImage.from_asset(
            highbyte_root,
            file=os.path.join("components", "opc-server", "Dockerfile"),
            platform=ecr_assets.Platform.LINUX_ARM64,
            **patched_image_options(),
        )

        self.opc_ua_endpoints = []
        self.opc_ua_hosts = []
        self.opcua_services = []
        self.opc_ua_server_fingerprint = server_fingerprint(highbyte_root, [
            _workshop_story.from_node(self.node), _workshop_story.loop_from_node(self.node)])

        for site_name, construct_id in self.OPC_UA_SITES:
            svc = self._create_one_opcua_site(site_name, construct_id, opc_image)
            self._attach_story_clock(site_name, svc)
            host = svc.load_balancer.load_balancer_dns_name
            self.opc_ua_hosts.append(host)
            self.opc_ua_endpoints.append(f"opc.tcp://{host}:{OPC_UA_PORT}/UA/ManufacturingServer")
            self.opcua_services.append(svc)

        # Keep backward-compatible single-endpoint references (Milwaukee = primary)
        self.opcua_service = self.opcua_services[0]
        self.opc_ua_host = self.opc_ua_hosts[0]
        self.opc_ua_endpoint = self.opc_ua_endpoints[0]

    def _adopt_opcua_servers(self, servers: "HighByteOpcuaStack") -> None:
        """Read the OPC UA servers of the servers-only stack *servers* (the
        same attributes :meth:`_create_opcua_service` sets): the setup
        Lambda's endpoints and the setup resource's wait on the servers become
        cross-stack references, so this stack deploys after *servers*."""
        self.opc_ua_endpoints = list(servers.opc_ua_endpoints)
        self.opc_ua_hosts = list(servers.opc_ua_hosts)
        self.opcua_services = list(servers.opcua_services)
        self.opc_ua_server_fingerprint = servers.opc_ua_server_fingerprint
        self.opcua_service = servers.opcua_service
        self.opc_ua_host = servers.opc_ua_host
        self.opc_ua_endpoint = servers.opc_ua_endpoint

    def _attach_story_clock(self, site_name: str, svc) -> None:
        """Live workshop story only (ADR-0092): the story site's server plays
        its dataset from the stored story anchor instead of the wall clock.

        It polls the SSM copy of ``simulation_config.story_anchor_epoch``
        (``stacks/erp_lambda_stack.py::_create_story_anchor_mirror``) and
        plays the packaged loop length. Every other site, and every demo or
        frozen synth, keeps the wall clock and an unchanged template.
        """
        if (_workshop_story.from_node(self.node) != "live"
                or site_name != _workshop_story.ANCHOR_STORY_SITE):
            return
        container = svc.task_definition.default_container
        container.add_environment(
            _workshop_story.ANCHOR_PARAMETER_ENV_NAME, _workshop_story.ANCHOR_PARAMETER_NAME)
        container.add_environment("AWS_REGION", self.region)
        loop = _workshop_story.loop_from_node(self.node)
        if loop is not None:
            container.add_environment(_workshop_story.LOOP_ENV_NAME, str(loop))
        name = _workshop_story.ANCHOR_PARAMETER_NAME.lstrip("/")
        svc.task_definition.add_to_task_role_policy(iam.PolicyStatement(
            actions=["ssm:GetParameter"],
            resources=[f"arn:{self.partition}:ssm:{self.region}:{self.account}:parameter/{name}"],
        ))
        # One-time history replay (the replay ADR): the SECOND parameter the
        # server polls to replay the history once on its history nodes, and
        # writes "<value>;done" into when finished (so a restart does not
        # replay it again). Read and write on exactly this one parameter.
        # Live story with the switch on only; off = unchanged template.
        if _workshop_story.backfill_from_node(self.node):
            container.add_environment(
                _workshop_story.BACKFILL_PARAMETER_ENV_NAME, _workshop_story.BACKFILL_PARAMETER_NAME)
            backfill_name = _workshop_story.BACKFILL_PARAMETER_NAME.lstrip("/")
            svc.task_definition.add_to_task_role_policy(iam.PolicyStatement(
                actions=["ssm:GetParameter", "ssm:PutParameter"],
                resources=[f"arn:{self.partition}:ssm:{self.region}:{self.account}:parameter/{backfill_name}"],
            ))

    def _create_one_opcua_site(self, site_name: str, construct_id: str, opc_image):
        """One internal-NLB Fargate service serving one site's OPC UA dataset."""
        svc = ecs_patterns.NetworkLoadBalancedFargateService(
            self,
            f"{construct_id}Service",
            service_name=f"IDO-OPC-Server-{site_name}",
            task_image_options=ecs_patterns.NetworkLoadBalancedTaskImageOptions(
                image=opc_image,
                container_port=OPC_UA_PORT,
                enable_logging=True,
                container_name="opcua",
                environment={
                    # SITE_NAME picks the per-site dataset baked into the image
                    # at /app/datasets/${SITE_NAME}.csv.gz (see server.sh).
                    "SITE_NAME": site_name,
                },
            ),
            memory_limit_mib=self.opcua_memory,
            cpu=self.opcua_cpu,
            desired_count=1,
            public_load_balancer=False,
            listener_port=OPC_UA_PORT,
            vpc=self.vpc,
            runtime_platform=ecs.RuntimePlatform(
                operating_system_family=ecs.OperatingSystemFamily.LINUX,
                cpu_architecture=ecs.CpuArchitecture.ARM64,
            ),
            **self._opcua_grace_kwargs(),
        )

        cfn_svc = svc.service.node.default_child
        # Per-site OPC servers keep their explicit (disabled) circuit breaker as
        # before; only the rolling percentages change (stop-first in
        # the workshop) via the shared helper.
        _highbyte_options.set_deployment_configuration(cfn_svc, {
            **_highbyte_options.opcua_deployment_configuration(self.sizing),
            "DeploymentCircuitBreaker": {"Enable": False, "Rollback": False},
        })

        svc.target_group.configure_health_check(
            healthy_threshold_count=2,
            interval=Duration.seconds(10),
        )
        svc.target_group.set_attribute("deregistration_delay.timeout_seconds", "5")
        self._allow_in_vpc_opcua_ingress(svc, site_name)
        return svc

    def _opcua_grace_kwargs(self) -> dict:
        """``health_check_grace_period`` for the OPC UA services when the
        sizing sets one (workshop: 300 s, so a 0.25 vCPU server's slower
        startup is not killed by the NLB health check); otherwise CDK's
        default (60 s) and an unchanged template."""
        seconds = self.sizing.opcua_grace_seconds
        return {} if seconds is None else {"health_check_grace_period": Duration.seconds(seconds)}

    def _allow_in_vpc_opcua_ingress(self, svc, site_name: str) -> None:
        """Restrict OPC UA ingress to the VPC CIDR.

        The in-VPC HighByte Intelligence Hub ECS service is the only consumer
        of these OPC UA endpoints (it reaches them via the internal NLB DNS
        names passed to the setup Lambda). The NLB is internal
        (public_load_balancer=False), and because the Fargate IP targets sit
        behind the NLB without client-IP preservation, traffic arrives at the
        task from the NLB nodes' private addresses (VPC CIDR) rather than from
        the HighByte task SG — so an SG-to-SG rule would not match. The VPC
        CIDR covers HighByte tasks + NLB health checks with no internet
        exposure.
        """
        svc.service.connections.allow_from(
            ec2.Peer.ipv4(self.vpc.vpc_cidr_block),
            ec2.Port.tcp(OPC_UA_PORT),
            f"Allow in-VPC OPC UA traffic ({site_name})",
        )

    def _create_highbyte_service(self) -> None:
        """Create HighByte Intelligence Hub using ApplicationLoadBalancedFargateService.

        The HighByte container runs the unmodified HighByte image (see
        HIGHBYTE_IMAGE): from Docker Hub in the demo, from the account's own
        ECR in the workshop (_grant_hub_ecr_pull). No build pipeline is needed.
        Split into helpers in construct order (the synthesized template is
        unchanged by the split)."""
        self._create_highbyte_task_definition()
        self._add_image_parameters()
        self._grant_hub_ecr_pull()
        self._grant_hub_bedrock()
        self._grant_hub_sitewise()
        seed_container, settings_file_path = self._add_settings_seed_container()
        self._add_hub_container(seed_container, settings_file_path)
        self._create_hub_alb_security_groups()
        self._create_hub_fargate_service()
        self._create_mcp_listener()
        self._finish_hub_service()

    def _create_highbyte_task_definition(self) -> None:
        """The hub's Fargate task definition with its EFS volume and EFS grant."""
        self.highbyte_task_definition = ecs.FargateTaskDefinition(
            self,
            "HighByteTaskDefinition",
            cpu=self.highbyte_cpu,
            memory_limit_mib=self.highbyte_memory,
        )

        # Add EFS volume
        self.highbyte_task_definition.add_volume(
            name="highbyte-config",
            efs_volume_configuration=ecs.EfsVolumeConfiguration(
                file_system_id=self.highbyte_efs.file_system_id,
                transit_encryption="ENABLED",
                authorization_config=ecs.AuthorizationConfig(
                    access_point_id=self.highbyte_access_point.access_point_id,
                ),
            ),
        )

        # Grant EFS access to task role
        self.highbyte_task_definition.add_to_task_role_policy(
            iam.PolicyStatement(
                actions=[
                    "elasticfilesystem:ClientMount",
                    "elasticfilesystem:ClientWrite",
                    "elasticfilesystem:ClientRootAccess",
                ],
                resources=[
                    self.highbyte_access_point.access_point_arn,
                    self.highbyte_efs.file_system_arn,
                ],
            )
        )

    def _add_image_parameters(self) -> None:
        """Under ``highbyte_image_parameters`` (the workshop), declare the two
        deploy-time image parameters the hub task's containers run. The root
        passes the refs its Custom::ADTExternalImages resource picked: always
        a digest ref, so a moved tag can never change what ECS runs, and a
        content change is a new parameter value and so a new task definition.
        Nothing is declared otherwise (the demo template is unchanged)."""
        self.image_parameter_refs: dict[str, str] = {}
        if not self.image_parameters:
            return
        for name, what in ((_highbyte_options.HUB_IMAGE_PARAMETER, "hub container"),
                           (_highbyte_options.SETTINGS_SEED_IMAGE_PARAMETER,
                            "settings-seed init container")):
            parameter = CfnParameter(
                self, name, type="String",
                allowed_pattern=_highbyte_options.IMAGE_PARAMETER_PATTERN,
                description=(
                    f"Image of the HighByte {what}, as a digest ref: this account's "
                    "bootstrap container-assets repo, or Docker Hub / ECR Public. "
                    "Passed by the Workshop Studio root."),
            )
            self.image_parameter_refs[name] = parameter.value_as_string

    def _container_image(self, parameter: str, ref: str):
        """The image a hub task container runs: its deploy-time parameter
        under the switch, else the synth-time *ref* (the demo)."""
        if self.image_parameters:
            return ecs.ContainerImage.from_registry(self.image_parameter_refs[parameter])
        return _registry_image(ref)

    def _grant_hub_ecr_pull(self) -> None:
        """Let the hub task's execution role pull its account-ECR images.

        ECS pulls a task's images with its EXECUTION role, and from_registry
        grants that role nothing. With the workshop's account-ECR refs every
        hub task stopped with ecr:GetAuthorizationToken AccessDenied (seen
        2026-10-10). Grant what CDK grants the OPC UA servers' asset images:
        layer and image reads on each image's repository, and the auth token
        on *. A public ref (the demo) needs no IAM, so it gets nothing. Under
        the image parameters the repo is the bootstrap container-assets repo
        the copy and load routes write (the direct route needs no grant)."""
        if self.image_parameters:
            repos = [_highbyte_options.ACCOUNT_ECR_REPOSITORY_ARN]
        else:
            repos = sorted({arn for arn in map(_account_ecr_repository_arn,
                                               (self.hub_image, self.settings_seed_image)) if arn})
        if not repos:
            return
        task = self.highbyte_task_definition
        task.add_to_execution_role_policy(iam.PolicyStatement(
            actions=["ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage",
                     "ecr:GetDownloadUrlForLayer"],
            resources=[Fn.sub(arn) for arn in repos],
        ))
        task.add_to_execution_role_policy(iam.PolicyStatement(
            actions=["ecr:GetAuthorizationToken"], resources=["*"]))
        # No checkov skip: checkov passes CKV_AWS_107 on this policy (the same
        # grant CDK gives every ECS execution role), and the Workshop Studio
        # scanner counts every skip in its rule set as a finding
        # (stacks/_checkov_skips.py).

    def _grant_hub_bedrock(self) -> None:
        """Grant Bedrock access for HighByte MCP/AI features."""
        self.highbyte_task_definition.add_to_task_role_policy(
            iam.PolicyStatement(
                # Scope InvokeModel to foundation models plus this account/region's
                # inference profiles (cross-region inference profiles route to
                # foundation models, which carry no account in their ARN).
                actions=[
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                ],
                resources=[
                    "arn:aws:bedrock:*::foundation-model/*",
                    f"arn:aws:bedrock:{self.region}:{self.account}:inference-profile/*",
                ],
            )
        )

    def _grant_hub_sitewise(self) -> None:
        """Grant IoT SiteWise access for HighByte data publishing.

        Only the hub's NameSpace_to_SiteWise pipeline uses SiteWise. With the
        pipeline disabled (``highbyte_sitewise_pipeline=disabled``, the
        workshop: SFC is the only SiteWise writer, ADR-0082) nothing in the
        hub reads or writes SiteWise, so the task role gets no SiteWise grant
        at all (the "read" statement below also creates models and assets)."""
        if self.sitewise_pipeline == "disabled":
            return
        # CreateAssetModel makes a resource that does not exist yet, and
        # ListAssetModels has no resource-level permission, so both stay on "*".
        self.highbyte_task_definition.add_to_task_role_policy(
            iam.PolicyStatement(
                actions=[
                    "iotsitewise:ListAssetModels",
                    "iotsitewise:CreateAssetModel",
                ],
                resources=["*"],
            )
        )
        # Resource-addressable actions scoped to this account and region's asset
        # and asset-model namespaces (same bound as the write statement).
        # CreateAsset authorizes against the existing asset-model it builds from,
        # so it belongs here rather than on "*". Individual IDs are unknown at
        # synth time, hence the trailing wildcards.
        self.highbyte_task_definition.add_to_task_role_policy(
            iam.PolicyStatement(
                actions=[
                    "iotsitewise:CreateAsset",
                    "iotsitewise:DescribeAssetModel",
                    "iotsitewise:DescribeAsset",
                    "iotsitewise:ListAssets",
                    "iotsitewise:ListAssociatedAssets",
                ],
                resources=[
                    Stack.of(self).format_arn(
                        service="iotsitewise",
                        resource="asset",
                        resource_name="*",
                    ),
                    Stack.of(self).format_arn(
                        service="iotsitewise",
                        resource="asset-model",
                        resource_name="*",
                    ),
                ],
            )
        )
        self._grant_hub_sitewise_writes()

    def _grant_hub_sitewise_writes(self) -> None:
        """The mutating SiteWise verbs, scoped to this account and region."""
        self.highbyte_task_definition.add_to_task_role_policy(
            iam.PolicyStatement(
                # Mutating verbs (update/delete/associate/property writes) are
                # kept off "*": they are scoped to the asset and asset-model
                # namespaces of THIS account and region so the task cannot
                # alter SiteWise resources in another region of a shared
                # account. Individual IDs are still unknown at synth time,
                # hence the trailing wildcards.
                actions=[
                    "iotsitewise:UpdateAssetModel",
                    "iotsitewise:DeleteAssetModel",
                    "iotsitewise:UpdateAsset",
                    "iotsitewise:DeleteAsset",
                    "iotsitewise:BatchPutAssetPropertyValue",
                    "iotsitewise:AssociateAssets",
                    "iotsitewise:DisassociateAssets",
                ],
                resources=[
                    Stack.of(self).format_arn(
                        service="iotsitewise",
                        resource="asset",
                        resource_name="*",
                    ),
                    Stack.of(self).format_arn(
                        service="iotsitewise",
                        resource="asset-model",
                        resource_name="*",
                    ),
                ],
            )
        )

    def _add_settings_seed_container(self):
        """Settings-seed init container; returns (container, settings file path).

        The public HighByte image boots from default application settings,
        which leave the REST Data Server and MCP server DISABLED. This tiny
        init container writes the HighByte settings file to the shared EFS
        volume on FIRST boot only (preserving UI-saved changes on later
        boots), enabling the MQTT broker, REST Data Server, and MCP server.
        The JSON is read from the repo at synth time and passed as an env var
        — no custom image or build pipeline required."""
        settings_path = os.path.join(
            os.path.dirname(__file__), "..", "highbyte", "components",
            "highbyte-settings", "intelligencehub-settings.json",
        )
        # Remembered for the setup custom resource's SetupRevision hash (the
        # seeded settings are a behaviour-affecting setup input).
        self._settings_path = settings_path
        with open(settings_path, "r", encoding="utf-8") as f:
            settings_json = f.read()
        settings_file_path = "/usr/local/highbyte/appData/intelligencehub-settings.json"
        seed_container = self.highbyte_task_definition.add_container(
            "HighByteSettingsSeed",
            image=self._container_image(_highbyte_options.SETTINGS_SEED_IMAGE_PARAMETER,
                                        self.settings_seed_image),
            essential=False,  # runs once and exits; must not keep the task alive
            container_name="settings-seed",
            logging=ecs.LogDrivers.aws_logs(stream_prefix="HighByteSettingsSeed"),
            environment={
                "SETTINGS_FILE": settings_file_path,
                "HB_SETTINGS_JSON": settings_json,
                # On an UPDATE of an existing
                # EFS-backed hub the settings file already exists, so a
                # first-boot-only seed left the old (vulnerable)
                # allowAnonymous:true in place and anonymous REST reads stayed
                # open, and kept MCP Configuration Tools on. HighByte 4.4.2 has
                # no supported settings API (every /v1/settings probe 404s on
                # the local container), so this POSIX awk reconciles ONLY the
                # REST/MCP server's allowAnonymous flag and the Configuration
                # Tools setting in the persisted file, preserving every other
                # UI-saved change.
                #
                # 4.4.2 re-serializes our input `restDataServer` block into a
                # top-level `httpServer` block (verified on a local 4.4.2
                # upgrade; 4.5.3 persists the same block), so the field that
                # actually controls anonymous /data
                # reads is `httpServer.allowAnonymous`. We set allowAnonymous to
                # false ONLY inside the `httpServer` or `restDataServer` section
                # (located by brace depth under `settings`), and never
                # touch `hbMqttBroker.allowAnonymous` (the MQTT broker, a
                # different concern and not internet-reachable). The patch
                # fails closed on any unrecognised file.
                "SETTINGS_PATCH_AWK": _SETTINGS_PATCH_AWK,
            },
            entry_point=["/bin/sh", "-c"],
            command=[_SETTINGS_SEED_SCRIPT],
        )
        seed_container.add_mount_points(
            ecs.MountPoint(
                container_path="/usr/local/highbyte/appData",
                source_volume="highbyte-config",
                read_only=False,
            )
        )
        return seed_container, settings_file_path

    def _add_hub_container(self, seed_container, settings_file_path: str) -> None:
        """The hub container, its EFS mount and its start-after-seed dependency."""
        highbyte_container = self.highbyte_task_definition.add_container(
            "HighByteContainer",
            image=self._container_image(_highbyte_options.HUB_IMAGE_PARAMETER, self.hub_image),
            logging=ecs.LogDrivers.aws_logs(stream_prefix="HighByteService"),
            container_name="highbyte",
            port_mappings=[
                ecs.PortMapping(container_port=HIGHBYTE_UI_PORT),
                ecs.PortMapping(container_port=HIGHBYTE_MCP_PORT),  # Shared HTTP: MCP + Docs + REST (port 8885 in 4.4.0)
            ],
            environment={
                # ACCEPT_EULA=Y is REQUIRED to run the HighByte image (the
                # container exits immediately without it), and setting it
                # agrees to the HighByte Intelligence Hub EULA. The value
                # comes only from the deployer's explicit acceptance
                # (_highbyte_options.accept_eula_env).
                # https://guide.highbyte.com/release/setup/docker/
                "ACCEPT_EULA": self.accept_eula,
                # The HighByte license is uploaded by an administrator via the
                # HighByte UI post-startup (Settings → License → Choose File).
                # See https://guide.highbyte.com/release/setup/installation/licensing/
                # The runtime boots in trial mode without a license, fine for
                # this sample's first-deploy experience.
                "HIGHBYTE_SETTINGS_FILE": settings_file_path,
                # 4.5 keeps a knowledge-graph directory it rebuilds at every
                # start and project import; its default is under appData,
                # which is EFS here. Keep it on the container's own disk.
                "HIGHBYTE_GRAPHDB_DIR": HIGHBYTE_GRAPHDB_DIR,
            },
        )
        # EFS mount point: the hub's appData (settings, project, license)
        highbyte_container.add_mount_points(
            ecs.MountPoint(
                container_path="/usr/local/highbyte/appData",
                source_volume="highbyte-config",
                read_only=False,
            )
        )
        # HighByte starts only after the settings file has been seeded to EFS.
        highbyte_container.add_container_dependencies(
            ecs.ContainerDependency(
                container=seed_container,
                # SUCCESS (exit 0), not COMPLETE (any exit): the seed fails
                # closed on an unrecognised settings file, and the hub must not
                # start on it.
                condition=ecs.ContainerDependencyCondition.SUCCESS,
            )
        )

    def _create_hub_alb_security_groups(self) -> None:
        """The two ALB security groups (UI port and MCP port CloudFront ingress).

        The ALB is internal (public_load_balancer=False) and fronted by a CloudFront
        VPC origin. open_listener=False keeps the construct from adding a 0.0.0.0/0 rule;
        ingress is granted to the CloudFront VPC-origin service-managed SG after the
        distribution is created (see _create_cloudfront_distribution).

        WHY two: a security-group rule that references a managed prefix list counts as
        the prefix list's MaxEntries (~50 for the CloudFront origin-facing list) against
        the 60-rules-per-SG quota, NOT as a single rule. Referencing it on both the UI and
        MCP ports in ONE SG therefore costs ~100 and exceeds the limit. Splitting the two
        prefix-list rules across two SGs (both attached to the ALB) keeps each SG to a
        single ~50-entry reference, well under 60. If the CloudFront prefix list itself
        ever grows past ~60 entries, raise the "Inbound rules per security group" quota
        (L-0EA8095F). See highbyte/README.md."""
        self.alb_sg = ec2.SecurityGroup(
            self,
            "HighByteALBSG",
            vpc=self.vpc,
            description="HighByte ALB (UI port CloudFront ingress)",
            allow_all_outbound=True,
            disable_inline_rules=True,
        )
        self.alb_sg_mcp = ec2.SecurityGroup(
            self,
            "HighByteALBMcpSG",
            vpc=self.vpc,
            description="HighByte ALB (MCP port CloudFront ingress)",
            allow_all_outbound=True,
            disable_inline_rules=True,
        )

    def _create_hub_fargate_service(self) -> None:
        """The internal-ALB hub service, ECS Exec and its ALB security groups."""
        self.highbyte_service = ecs_patterns.ApplicationLoadBalancedFargateService(
            self,
            "HighByteService",
            service_name="IDO-HighByteIntelligenceHub",
            task_definition=self.highbyte_task_definition,
            desired_count=1,
            listener_port=HIGHBYTE_UI_PORT,
            open_listener=False,  # no 0.0.0.0/0; reached only via CloudFront VPC origin
            public_load_balancer=False,  # internal ALB — CloudFront reaches it via VPC origin
            # NOTE: do NOT pass `security_groups` here. That prop attaches SGs to the Fargate
            # SERVICE, and attaching alb_sg/alb_sg_mcp to BOTH the service and the ALB makes
            # CDK auto-generate an SG-to-SG ingress rule for every (LB SG x service SG x port)
            # combination — which exceeds the 60-rules-per-SG quota. The service gets its own
            # construct-managed SG; the ALB SGs are attached to the load balancer below.
            vpc=self.vpc,
        )
        # Enable ECS Exec for debugging (allows aws ecs execute-command)
        cfn_highbyte_svc = self.highbyte_service.service.node.default_child
        cfn_highbyte_svc.add_property_override("EnableExecuteCommand", True)
        # CRITICAL: Disable circuit breaker - HighByte has high CPU on startup
        # and the MCP health check fails until MCP is configured via setup Lambda.
        # Without this, ECS kills the task after a few failed health checks.
        cfn_highbyte_svc.add_property_deletion_override("DeploymentConfiguration")
        cfn_highbyte_svc.add_property_override("DeploymentConfiguration", {
            "MaximumPercent": 200,
            "MinimumHealthyPercent": 50,
            "DeploymentCircuitBreaker": {"Enable": False, "Rollback": False},
        })
        # Grant SSM permissions for ECS Exec
        self.highbyte_task_definition.task_role.add_managed_policy(
            iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSSMManagedInstanceCore")
        )
        # The `security_groups=[...]` prop would associate SGs with the Fargate SERVICE
        # (the tasks), NOT the load balancer, so they are attached to the ALB here.
        # Ingress (CloudFront VPC-origin service SG -> UI and MCP ports, setup Lambda
        # SG -> UI port) is added in _create_cloudfront_distribution and
        # _create_setup_lambda.
        self.highbyte_service.load_balancer.add_security_group(self.alb_sg)
        self.highbyte_service.load_balancer.add_security_group(self.alb_sg_mcp)

    def _create_mcp_listener(self) -> None:
        """One target group and listener on 8885 (MCP + Docs + REST share it in 4.4.0)."""
        self.mcp_target_group = elbv2.ApplicationTargetGroup(
            self,
            "MCPTargetGroup",
            vpc=self.vpc,
            port=HIGHBYTE_MCP_PORT,
            protocol=elbv2.ApplicationProtocol.HTTP,
            target_type=elbv2.TargetType.IP,
            health_check=elbv2.HealthCheck(
                path="/data/doc",
                port=str(HIGHBYTE_MCP_PORT),
                healthy_threshold_count=2,
                interval=Duration.seconds(20),
                healthy_http_codes="200-499",
            ),
        )
        self.mcp_target_group.set_attribute("deregistration_delay.timeout_seconds", "5")
        self.mcp_target_group.add_target(
            self.highbyte_service.service.load_balancer_target(
                container_name="highbyte",
                container_port=HIGHBYTE_MCP_PORT,
            )
        )
        self.mcp_listener = self.highbyte_service.load_balancer.add_listener(
            "MCPListener",
            port=HIGHBYTE_MCP_PORT,
            protocol=elbv2.ApplicationProtocol.HTTP,
            open=False,  # ingress restricted to CloudFront prefix list (see alb_sg rules)
            default_target_groups=[self.mcp_target_group],
        )
        _origin_hop = (
            "The ALB is internal and its security groups admit only the "
            "CloudFront VPC origin and this stack's in-VPC setup Lambda. Viewers "
            "connect to CloudFront over HTTPS; the hop from CloudFront to this "
            "in-VPC origin is HTTP by design, the same as the ADT app's load "
            "balancer."
        )
        for listener in (self.highbyte_service.listener, self.mcp_listener):
            checkov_skip(listener, [("CKV_AWS_2", _origin_hop), ("CKV_AWS_103", _origin_hop)])
        # MCP and Docs endpoints set in _create_cloudfront_distribution after CloudFront is created

    def _finish_hub_service(self) -> None:
        """Deployment configuration, target-group settings, EFS access, endpoints."""
        # CRITICAL: Disable circuit breaker - HighByte has high CPU on startup.
        # The rollout itself is start-first (demo) or stop-first (the workshop's
        # sizing, stacks/_highbyte_options.py).
        cfn_svc = self.highbyte_service.service.node.default_child
        _highbyte_options.set_deployment_configuration(
            cfn_svc, _highbyte_options.hub_deployment_configuration(self.sizing))
        # Store cluster ARN for setup Lambda
        self.cluster_arn = self.highbyte_service.cluster.cluster_arn
        self.highbyte_service.target_group.set_attribute(
            "deregistration_delay.timeout_seconds", "5"
        )
        # Disable circuit breaker on OPC UA service (stop-first in the workshop)
        cfn_opcua = self.opcua_service.service.node.default_child
        _highbyte_options.set_deployment_configuration(
            cfn_opcua, _highbyte_options.opcua_deployment_configuration(self.sizing))
        self.highbyte_service.target_group.configure_health_check(
            port=str(HIGHBYTE_UI_PORT),
            path="/ui/",
            healthy_threshold_count=2,
            interval=Duration.seconds(20),
            healthy_http_codes="200-499",
        )
        # Allow EFS connections from the service
        self.highbyte_efs.connections.allow_default_port_from(
            self.highbyte_service.service.connections
        )
        # The HighByte endpoint (via ALB), and the MCP endpoint (direct ALB
        # access, token-protected) until CloudFront replaces it.
        # In-transit note: these in-VPC URLs are plain HTTP. The ALB is
        # internal (not internet-facing) and its security groups admit only
        # the CloudFront VPC origin and the setup Lambda, so the bearer token
        # and UI traffic cross only this VPC unencrypted; every public path is
        # HTTPS through CloudFront. The sample has no domain or certificate
        # for an ALB HTTPS listener. For production, add an HTTPS listener
        # with an AWS Certificate Manager certificate and use https:// here.
        self.highbyte_alb_endpoint = f"http://{self.highbyte_service.load_balancer.load_balancer_dns_name}:{HIGHBYTE_UI_PORT}"
        self.mcp_endpoint = f"http://{self.highbyte_service.load_balancer.load_balancer_dns_name}:{HIGHBYTE_MCP_PORT}/mcp"
        # Stable in-VPC MCP endpoint for the setup Lambda's key verification.
        # self.mcp_endpoint is REPLACED by the public CloudFront
        # URL in _create_cloudfront_distribution; the Lambda must use the ALB
        # directly (reachable via the alb_sg_mcp Lambda ingress rule), so keep a
        # separate attribute CloudFront does not overwrite.
        self.mcp_alb_endpoint = self.mcp_endpoint

    def _create_cognito_user_pool(self) -> None:
        """Cognito user pool + hosted UI + OAuth app client for the HighByte UI
        Authorization@Edge flow (same pattern as the ERP dashboard)."""
        self.user_pool = cognito.UserPool(
            self,
            "HighByteUiUserPool",
            user_pool_name="highbyte-ui-users",
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(
                username=True,
                email=True,
            ),
            auto_verify=cognito.AutoVerifiedAttrs(
                email=False,
            ),
            password_policy=cognito.PasswordPolicy(
                min_length=8,
                require_lowercase=False,
                require_uppercase=False,
                require_digits=False,
                require_symbols=False,
            ),
            account_recovery=cognito.AccountRecovery.NONE,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # Domain prefix must be lowercase alphanumeric and hyphens only, and is
        # GLOBALLY unique per Region. Derive it through the shared helper: with
        # a concrete account (deploy.sh) it is the literal last-8 digits; in the
        # account-agnostic workshop synth it is a deploy-time Fn::Join on
        # AWS::AccountId (pin_domain_to_account below — MANDATORY for every
        # Cognito hosted-UI domain in this repo, see stacks/_cognito_domain.py).
        # Note: Cognito domains cannot be updated in-place — changing it needs
        # a manual domain delete (or stack replacement) first.
        domain_prefix = f"highbyte-ui-{domain_suffix(self.account)}"
        self.user_pool_domain = self.user_pool.add_domain(
            "HighByteUiUserPoolDomain",
            cognito_domain=cognito.CognitoDomainOptions(
                domain_prefix=domain_prefix,
            ),
        )
        self.user_pool_domain_value = pin_domain_to_account(
            self.user_pool_domain, domain_prefix, self.account)

        self._create_auth_client()

    def _create_auth_client(self) -> None:
        """OAuth app client for Authorization@Edge.

        Placeholder callback/logout URLs are intentional — the real CloudFront
        domain is a CFN token not resolvable here without creating a
        client → SAR → distribution → client cycle.  The callback-updater
        custom resource (see _create_callback_updater) rewrites these URLs
        post-distribution-creation at deploy time.
        """
        # Optional external OIDC provider on this pool (ADR-0072) — listed by
        # the client below so the hosted UI offers both sign-in choices.
        self.external_idp = (
            create_external_oidc_provider(
                self.user_pool,
                construct_id="HighByteUiExternalIdp",
                user_pool=self.user_pool,
                config=self._external_oidc,
            )
            if self._external_oidc
            else None
        )
        self.auth_client = create_edge_oauth_client(
            self.user_pool,
            construct_id="HighByteUiAuthClient",
            user_pool=self.user_pool,
            external_idp=self.external_idp,
            oidc_only=bool(self._external_oidc and self._external_oidc.oidc_only),
        )

    def _create_edge_auth(self) -> None:
        """Deploy the shared auth-only SAR application and edge versions."""
        self.edge_headers = edge_http_headers(allow_websockets=True)
        self.edge_auth, self.edge_versions = deploy_edge_auth(
            self,
            user_pool=self.user_pool,
            client=self.auth_client,
            http_headers=self.edge_headers,
        )

    def _create_cloudfront_distribution(self) -> None:
        """Create CloudFront access for HighByte: the distribution (AWS WAF +
        Cognito Authorization@Edge on the UI paths; bearer-token machine
        paths), optional custom-domain DNS records, and the ALB ingress
        restriction to the CloudFront VPC-origin service SG."""
        self._create_distribution()
        self._create_ui_alias_records()
        self.highbyte_endpoint = f"https://{self.cloudfront.distribution_domain_name}"
        # MCP and Docs are now accessible via CloudFront HTTPS
        self.mcp_endpoint = f"https://{self.cloudfront.distribution_domain_name}/mcp"
        self.docs_endpoint = f"https://{self.cloudfront.distribution_domain_name}/data/doc"
        self._restrict_alb_ingress_to_cloudfront()

    def _create_vpc_origin(self, port: int, name: str, origin_id: str) -> origins.VpcOrigin:
        """VPC origin over the internal ALB for one port.

        Each VpcOrigin provisions a service-managed ENI in the VPC and can
        take several minutes to deploy. MCP + Docs + REST share port 8885 in
        HighByte 4.4.0+.

        The origin hop is HTTP_ONLY: TLS ends at CloudFront, and the hop
        reaches the internal ALB through the CloudFront VPC origin's security
        group (the setup Lambda has its own named in-VPC ingress). The ALB
        has no domain or certificate of its own for an HTTPS listener. For
        production, add an HTTPS listener with an AWS Certificate Manager
        certificate on the ALB and use ``OriginProtocolPolicy.HTTPS_ONLY``.
        """
        return origins.VpcOrigin.with_application_load_balancer(
            self.highbyte_service.load_balancer,
            protocol_policy=cloudfront.OriginProtocolPolicy.HTTP_ONLY,
            http_port=port,
            read_timeout=Duration.seconds(60),
            vpc_origin_name=f"highbyte-{name}-{self.region}",
            origin_id=origin_id,
        )

    def _create_web_acl(self):
        """Create the shared three-managed-rule CloudFront web ACL."""
        return create_standard_web_acl(
            self,
            construct_id="HighByteWebAclCdk",
            metric_name="HighByteWebAclCdk",
            bare_callback_redirect_headers=self.edge_headers,
        )

    def _create_root_rewrite_fn(self) -> cloudfront.Function:
        """Root-path fix: hitting "/" makes HighByte emit an absolute redirect
        to http://<host>:45245/ui/index.html (its internal UI port, over http),
        which dead-ends behind CloudFront (443-only VPC origins). We must NOT
        rewrite the URI to /ui/index.html — that keeps the browser at "/", so
        the SPA's RELATIVE asset refs (./assets/*) resolve to /assets/* and
        404. Instead, return a 302 to /ui/ so the browser lands under /ui/ and
        relative assets resolve to /ui/assets/*. Only the exact "/" is
        handled; all other paths pass through.
        """
        return cloudfront.Function(
            self,
            "HighByteRootRewriteFn",
            comment="Redirect / -> /ui/ (avoid HighByte :45245 redirect; keep SPA base path)",
            code=cloudfront.FunctionCode.from_inline(
                "function handler(event) {\n"
                "  if (event.request.uri === '/') {\n"
                "    return {\n"
                "      statusCode: 302,\n"
                "      statusDescription: 'Found',\n"
                "      headers: { 'location': { value: '/ui/' } }\n"
                "    };\n"
                "  }\n"
                "  return event.request;\n"
                "}"
            ),
        )

    @staticmethod
    def _machine_behavior(mcp_origin: origins.VpcOrigin) -> cloudfront.BehaviorOptions:
        """The bearer-token machine-path behavior (/mcp*, /i3x*, /i3X*,
        /data/*): NO edge auth — the origin enforces its own token. The
        Swagger page under /data/doc is the exception (edge-auth behaviors
        in _create_distribution)."""
        return cloudfront.BehaviorOptions(
            origin=mcp_origin,
            allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
            viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
            origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER,
            cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
        )

    def _create_distribution(self) -> None:
        """Distribution with edge-auth UI paths and bearer-token machine paths."""
        ui_origin = self._create_vpc_origin(HIGHBYTE_UI_PORT, "ui", "HighByteUiOrigin")
        mcp_origin = self._create_vpc_origin(HIGHBYTE_MCP_PORT, "mcp", "HighByteMCPOrigin")
        shared_behavior = self._machine_behavior(mcp_origin)
        self.ui_domain_name = (
            f"highbyte.{self._custom_domain_config.domain_name}"
            if self._custom_domain_config and self._ui_certificate else None
        )
        common, default, auth_paths = edge_behavior_parts(
            origin=ui_origin,
            versions=self.edge_versions,
            origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER,
        )
        web_acl = self._create_web_acl()
        docs_behavior = guarded_behavior(
            origin=mcp_origin,
            versions=self.edge_versions,
            origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER,
        )
        root = cloudfront.BehaviorOptions(
            **common,
            function_associations=[cloudfront.FunctionAssociation(
                function=self._create_root_rewrite_fn(),
                event_type=cloudfront.FunctionEventType.VIEWER_REQUEST,
            )],
        )
        self.cloudfront = cloudfront.Distribution(
            self,
            "HighByteDistribution",
            web_acl_id=web_acl.attr_arn,
            domain_names=[self.ui_domain_name] if self.ui_domain_name else None,
            certificate=self._ui_certificate if self.ui_domain_name else None,
            default_behavior=default,
            additional_behaviors={
                "/": root,
                **auth_paths,
                "/mcp*": shared_behavior,
                "/i3x*": shared_behavior,
                "/i3X*": shared_behavior,
                # The REST Data Server's Swagger UI is an HTML page the origin
                # serves without a token: it needs sign-in like the UI. These
                # must precede /data/* (CloudFront takes the first match).
                **dict.fromkeys(("/data/doc", "/data/doc/*"), docs_behavior),
                "/data/*": shared_behavior,
            },
        )
        self.cloudfront.node.add_dependency(self.edge_auth)

    def _create_ui_alias_records(self) -> None:
        """Create HighByte DNS aliases through the shared subdomain seam."""
        if not self.ui_domain_name:
            return
        wire_subdomain_alias(
            self,
            subdomain="highbyte",
            config=self._custom_domain_config,
            certificate=self._ui_certificate,
            distribution_construct_id="HighByteDistribution",
            construct_id_prefix="HighByte",
            attach_viewer_certificate=False,
        )

    def _restrict_alb_ingress_to_cloudfront(self) -> None:
        """Grant the internal ALB ingress from the CloudFront VPC-origin
        service-managed SG (`CloudFront-VPCOrigins-Service-SG`). AWS creates
        this SG once a VPC origin exists and its id is not stable, so look it
        up at deploy time. This is the tightest option (AWS docs "Restrict
        access with VPC origins", Option 2): only this account's CloudFront
        VPC-origin traffic can reach the ALB — nothing else in the VPC."""
        service_sg_call = cr.AwsSdkCall(
            service="EC2",
            action="describeSecurityGroups",
            parameters={
                "Filters": [
                    {"Name": "group-name", "Values": ["CloudFront-VPCOrigins-Service-SG"]},
                    {"Name": "vpc-id", "Values": [self.vpc.vpc_id]},
                ]
            },
            physical_resource_id=cr.PhysicalResourceId.of(
                "highbyte-cloudfront-vpcorigins-service-sg-lookup"
            ),
        )
        service_sg_lookup = cr.AwsCustomResource(
            self,
            "HighByteVpcOriginServiceSgLookup",
            on_create=service_sg_call,
            on_update=service_sg_call,
            policy=cr.AwsCustomResourcePolicy.from_sdk_calls(
                resources=cr.AwsCustomResourcePolicy.ANY_RESOURCE
            ),
        )
        # The service SG only exists after the VPC origins (distribution) are created.
        service_sg_lookup.node.add_dependency(self.cloudfront)
        service_sg_id = service_sg_lookup.get_response_field("SecurityGroups.0.GroupId")

        for port, sg, suffix in (
            (HIGHBYTE_UI_PORT, self.alb_sg, "Ui"),
            (HIGHBYTE_MCP_PORT, self.alb_sg_mcp, "Mcp"),
        ):
            ingress = ec2.CfnSecurityGroupIngress(
                self,
                f"HighByteAlbIngressFromCloudFront{suffix}",
                group_id=sg.security_group_id,
                ip_protocol="tcp",
                from_port=port,
                to_port=port,
                source_security_group_id=service_sg_id,
                description=f"Allow CloudFront VPC-origin service SG (port {port})",
            )
            ingress.node.add_dependency(self.cloudfront)
            ingress.node.add_dependency(service_sg_lookup)

    def _create_callback_updater(self) -> None:
        """Patch OAuth callbacks after the distribution token resolves."""
        create_callback_updater(
            self,
            function_name="highbyte-ui-callback-updater",
            user_pool=self.user_pool,
            client=self.auth_client,
            distribution=self.cloudfront,
            custom_domain=self.ui_domain_name,
        )

    def _create_default_user(self) -> None:
        """Create the ADT HighByte UI user through the shared provider."""
        secret_name = "adt-demo-password"  # nosec B105 - secret name, not value
        provider = create_demo_user_provider(
            self,
            function_name="highbyte-ui-create-user",
            user_pool=self.user_pool,
            secret_name=secret_name,
        )
        add_demo_user(
            self,
            construct_id="DefaultUser",
            provider=provider,
            user_pool=self.user_pool,
            username="ADT",
            secret_name=secret_name,
        )

    def _create_setup_lambda(self) -> None:
        """Create Lambda function to configure HighByte via REST API and write license to EFS."""
        # Create Lambda execution role
        setup_lambda_role = iam.Role(
            self,
            "SetupLambdaRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                ),
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaVPCAccessExecutionRole"
                ),
            ],
        )

        # Grant permissions
        self.password_secret.grant_read(setup_lambda_role)
        
        # Grant read access to the ERP Aurora database secret — only when the ERP
        # simulator is deployed. On a base/seed-less --highbyte deploy aurora_secret_arn
        # is "", which would otherwise produce an invalid empty-ARN policy resource.
        if self.aurora_secret_arn:
            setup_lambda_role.add_to_policy(
                iam.PolicyStatement(
                    actions=["secretsmanager:GetSecretValue"],
                    resources=[self.aurora_secret_arn],
                )
            )

        # Allow the Lambda to read AND store the auto-created HighByte MCP bearer
        # token so the ADT app can use it with no manual step. Scoped to the
        # canonical conn/highbyte JSON secret ({"token": "<jwt>"}) read by the
        # ADT app. GetSecretValue is REQUIRED: reconciliation begins by READING
        # the secret to decide whether the stored token still authenticates;
        # without it every run would read AccessDenied as "secret absent" and
        # mint another key, churning key slots.
        setup_lambda_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "secretsmanager:GetSecretValue",
                    "secretsmanager:PutSecretValue",
                    "secretsmanager:CreateSecret",
                    "secretsmanager:DescribeSecret",
                ],
                resources=[
                    f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:adt/conn/highbyte*"
                ],
            )
        )

        # Grant read access to ADT database secret
        if self._seed_data_stack:
            self._seed_data_stack.db_secret.grant_read(setup_lambda_role)
        
        # ECS task restarts. The setup Lambda's code makes no ECS call today;
        # the grant stays for its restart path. ListTasks has no resource
        # type and stays on "*"; StopTask and DescribeTasks reach only tasks
        # in this stack's HighByte cluster (the cluster is in this stack, its
        # task ids are assigned at launch).
        setup_lambda_role.add_to_policy(
            iam.PolicyStatement(
                actions=["ecs:ListTasks"],
                resources=["*"],
            )
        )
        setup_lambda_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "ecs:StopTask",
                    "ecs:DescribeTasks",
                ],
                resources=[self.format_arn(
                    service="ecs", resource="task",
                    resource_name=f"{self.highbyte_service.cluster.cluster_name}/*")],
            )
        )
        
        # Grant EFS permissions to Lambda
        setup_lambda_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "elasticfilesystem:ClientMount",
                    "elasticfilesystem:ClientWrite",
                    "elasticfilesystem:ClientRootAccess",
                ],
                resources=[
                    self.highbyte_access_point.access_point_arn,
                    self.highbyte_efs.file_system_arn,
                ],
            )
        )

        # Create Lambda function with EFS access. The Lambda code and the
        # HighByte project configuration live in two places
        # (highbyte/lambda/highbyte-setup and config/highbyte); they are
        # assembled into a STAGING directory under cdk.out (stacks/_staging.py)
        # — synth never writes into the source tree.
        setup_lambda_src = os.path.join(os.path.dirname(__file__), "..", "highbyte", "lambda", "highbyte-setup")
        config_file_src = os.path.join(os.path.dirname(__file__), "..", "config", "highbyte", "intelligencehub-configuration.json")
        setup_lambda_path = stage_tree(
            self, "highbyte-setup", setup_lambda_src,
            extra_files={"intelligencehub-configuration.json": config_file_src},
        )

        # NOTE: the HighByte license is intentionally NOT bundled with the
        # Lambda asset. Operators upload their license via the HighByte UI
        # post-startup (Settings → License → Choose File). HighByte boots in
        # trial mode without one. This avoids shipping any HighByte-licensed
        # bytes inside the repo.

        self.setup_lambda = lambda_.Function(
            self,
            "HighByteSetupLambda",
            function_name="highbyte-setup",
            runtime=lambda_.Runtime.NODEJS_22_X,
            handler="index.handler",
            code=lambda_.Code.from_asset(setup_lambda_path),
            timeout=Duration.minutes(5),
            memory_size=256,
            role=setup_lambda_role,
            environment={
                "HIGHBYTE_ENDPOINT": self.highbyte_endpoint,
                "HIGHBYTE_ALB_ENDPOINT": self.highbyte_alb_endpoint,
                "OPC_UA_ENDPOINT": self.opc_ua_endpoint,
                "OPC_UA_ENDPOINT_2": self.opc_ua_endpoints[1] if len(self.opc_ua_endpoints) > 1 else "",
                "OPC_UA_ENDPOINT_3": self.opc_ua_endpoints[2] if len(self.opc_ua_endpoints) > 2 else "",
                "SECRET_HIGHBYTE_PASSWORD": self.password_secret.secret_arn,
                "MCP_TOKEN_SECRET": "adt/conn/highbyte",
                # The hub MCP endpoint the setup Lambda calls to PROVE the MCP
                # bearer key authenticates (ALB :8885/mcp, reachable in-VPC via
                # the alb_sg_mcp ingress rule added below).
                "MCP_VERIFY_ENDPOINT": self.mcp_alb_endpoint,
                "CLUSTER_ARN": self.cluster_arn,
                "PROJECT_CONFIG_URL": self.config_url or "",
                "EFS_MOUNT_PATH": "/mnt/efs",
                "AURORA_SECRET_ARN": self.aurora_secret_arn,
                "CONFIG_FILE_PATH": "/var/task/intelligencehub-configuration.json",
                "ADT_POSTGRES_HOST": self._seed_data_stack.db_endpoint if self._seed_data_stack else "",
                "ADT_POSTGRES_PORT": "5432",
                # "adt_demo" here is the fictitious demo company's database — demo content, not engine identity (see rename plan)
                "ADT_POSTGRES_DB": "adt_demo",
                "ADT_POSTGRES_USER": "postgres",
                "ADT_SECRET_ARN": self._seed_data_stack.db_secret.secret_arn if self._seed_data_stack else "",
            },
            vpc=self.vpc,
            filesystem=lambda_.FileSystem.from_efs_access_point(
                self.highbyte_access_point,
                "/mnt/efs",
            ),
        )

        # Allow Lambda to connect to HighByte ALB on UI port
        self.alb_sg.add_ingress_rule(
            ec2.Peer.security_group_id(self.setup_lambda.connections.security_groups[0].security_group_id),
            ec2.Port.tcp(HIGHBYTE_UI_PORT),
            "Allow setup Lambda to reach ALB on UI port",
        )

        # Allow the setup Lambda to reach the ALB MCP port (8885) so it can
        # VERIFY the MCP bearer key it reconciles. The MCP listener
        # is otherwise CloudFront-prefix-list only (open=False); this adds just
        # the Lambda's SG, not 0.0.0.0/0.
        self.alb_sg_mcp.add_ingress_rule(
            ec2.Peer.security_group_id(self.setup_lambda.connections.security_groups[0].security_group_id),
            ec2.Port.tcp(HIGHBYTE_MCP_PORT),
            "Allow setup Lambda to reach ALB on MCP port (key verification)",
        )

        # Allow Lambda to connect to EFS
        self.highbyte_efs.connections.allow_default_port_from(
            self.setup_lambda
        )

        # Auto-invoke setup Lambda as a Custom Resource after deployment
        # This ensures HighByte is configured automatically without manual intervention
        setup_provider = cr.Provider(
            self,
            "SetupProvider",
            on_event_handler=self.setup_lambda,
        )

        # Deterministic revision: the setup CR re-runs on a stack update
        # whenever anything that changes what the setup does changes. The hash
        # covers the setup Lambda sources, the bundled project JSON, the seeded
        # settings JSON, and the behaviour-affecting literal flags; the
        # token-valued endpoints (unknown at synth) are carried as separate
        # properties so a resolved-endpoint change also sends an Update. The old
        # fixed "Timestamp" string never changed, so EFS-backed hubs never got
        # these setup fixes on a normal update.
        #
        # The hub image is a setup input too: 4.4.2 cannot read the project
        # file 4.5.3 rewrites and boots with an empty project until setup
        # re-imports it (local rollback probe, 2026-10-11), so an image-only
        # change, forward or back, must re-run setup. The demo's synth-time
        # ref is hashed; the workshop's is a deploy-time parameter (a token),
        # so it is carried as its own property, like the endpoints.
        revision_inputs = {"sitewise_pipeline": self.sitewise_pipeline}
        image_properties = {}
        if self.image_parameters:
            image_properties["HubImage"] = self.image_parameter_refs[_highbyte_options.HUB_IMAGE_PARAMETER]
        else:
            revision_inputs["hub_image"] = self.hub_image
        self.setup_resource = CustomResource(
            self,
            "HighByteSetupResource",
            service_token=setup_provider.service_token,
            properties={
                "SetupRevision": setup_revision(
                    setup_lambda_src, config_file_src, self._settings_path, revision_inputs),
                # Behaviour-affecting endpoints (CFN tokens): included so a
                # changed resolved value also triggers Update.
                "OpcUaEndpoints": self.opc_ua_endpoints,
                "SiteWisePipeline": self.sitewise_pipeline,
                **image_properties,
            },
        )

        # CRITICAL: Setup must wait for HighByte service and CloudFront to be ready
        self.setup_resource.node.add_dependency(self.highbyte_service)
        self.setup_resource.node.add_dependency(self.cloudfront)
        for svc in self.opcua_services:
            self.setup_resource.node.add_dependency(svc)

    def _create_setup_lambda_and_exports(self) -> None:
        """Setup Lambda + stack outputs, then the OPC UA sites beyond the third.

        ``_create_setup_lambda`` / ``_create_exports`` wire the first three
        sites positionally (``OPC_UA_ENDPOINT[_2|_3]``, ``OpcUaEndpoint[2|3]``).
        Every further site in ``OPC_UA_SITES`` (today Indianapolis, ADR-0083)
        gets ``OPC_UA_ENDPOINT_<n>`` on the setup Lambda — the HighByte project
        import substitutes it into connection ``opc_<site>`` — and an
        ``OpcUaEndpoint<n>`` output, from ONE place, so adding a site is one
        tuple entry plus its dataset.
        """
        self._create_setup_lambda()
        # A package without the hub (package.py --highbyte off) deletes the
        # setup function and its CDK-made group in place, in the retained
        # stack's update cleanup. The detach and the drain act there too: their
        # per-resource removal decision sees the function and the group gone
        # from the template, so the function leaves the VPC first and the drain
        # deletes the interfaces Lambda leaves on the group.
        detach_before_delete(self.setup_lambda)
        drain_on_delete(self.setup_lambda.connections.security_groups[0], self.setup_lambda)
        self._apply_sitewise_pipeline_switch()
        self._create_exports()
        if self._opcua_servers is None:  # else the servers' own stack has them
            self._create_opcua_exports()
        for index, _site in enumerate(self.OPC_UA_SITES[3:], start=4):
            self.setup_lambda.add_environment(
                f"OPC_UA_ENDPOINT_{index}", self.opc_ua_endpoints[index - 1])

    def _apply_sitewise_pipeline_switch(self) -> None:
        """``highbyte_sitewise_pipeline=disabled``: the setup Lambda imports
        NameSpace_to_SiteWise with every trigger off (index.mjs reads
        ``HIGHBYTE_SITEWISE_PIPELINE``). The default sets nothing, so the
        demo's Lambda and template are unchanged."""
        if self.sitewise_pipeline == "disabled":
            self.setup_lambda.add_environment(
                _highbyte_options.SITEWISE_PIPELINE_ENV, "disabled")

    def _create_opcua_exports(self) -> None:
        """OPC UA endpoint outputs — emitted with or without HighByte.

        The first three keep their historical ids/descriptions; every further
        site in ``OPC_UA_SITES`` gets ``OpcUaEndpoint<n>``.
        """
        # Output descriptions stay ASCII: some consoles render non-ASCII
        # as '?'.
        CfnOutput(
            self,
            "OpcUaEndpoint",
            value=self.opc_ua_endpoints[0],
            description="OPC UA server endpoint: Milwaukee Bottling",
        )

        CfnOutput(
            self,
            "OpcUaEndpoint2",
            value=self.opc_ua_endpoints[1] if len(self.opc_ua_endpoints) > 1 else "N/A",
            description="OPC UA server endpoint: Detroit AutoParts",
        )

        CfnOutput(
            self,
            "OpcUaEndpoint3",
            value=self.opc_ua_endpoints[2] if len(self.opc_ua_endpoints) > 2 else "N/A",
            description="OPC UA server endpoint: Cedar Rapids Food",
        )

        CfnOutput(
            self,
            "OpcUaServerHost",
            value=self.opc_ua_hosts[0],
            description="OPC UA server hostname: Milwaukee (for manual connection)",
        )

        for index, (site_name, _construct_id) in enumerate(self.OPC_UA_SITES[3:], start=4):
            CfnOutput(
                self,
                f"OpcUaEndpoint{index}",
                value=self.opc_ua_endpoints[index - 1],
                description=f"OPC UA server endpoint: {site_name}",
            )

    def _create_exports(self) -> None:
        """Add stack outputs."""
        CfnOutput(
            self,
            "HighByteDistributionURL",
            value=f"{self.cloudfront.distribution_domain_name}/ui/#/",
            description="HighByte Intelligence Hub UI",
        )

        CfnOutput(
            self,
            "HighByteEndpoint",
            value=self.highbyte_endpoint,
            description="HighByte Intelligence Hub endpoint (via Amazon CloudFront)",
        )

        CfnOutput(
            self,
            "MCPEndpoint",
            value=self.mcp_endpoint,
            description="HighByte MCP server endpoint (use with Bearer token)",
            export_name="ADTHighByteOpcuaStack-MCPEndpoint",
        )

        CfnOutput(
            self,
            "HighBytePasswordSecretArn",
            value=self.password_secret.secret_arn,
            description="AWS Secrets Manager ARN for HighByte admin password (username: administrator)",
        )

        self._create_ops_exports()

    def _create_ops_exports(self) -> None:
        """Operational outputs: ECS cluster, setup Lambda and the MCP Inspector hint."""
        CfnOutput(
            self,
            "ClusterArn",
            value=self.cluster_arn,
            description="Amazon ECS cluster ARN",
        )

        CfnOutput(
            self,
            "SetupLambdaArn",
            value=self.setup_lambda.function_arn,
            description="Setup AWS Lambda function ARN - invoke to configure HighByte",
        )

        # Convenience output for MCP Inspector command
        CfnOutput(
            self,
            "MCPInspectorCommand",
            value="npx @modelcontextprotocol/inspector",
            description="Run this command, then use MCPEndpoint with your API key to connect",
        )

    def _create_auth_exports(self) -> None:
        """Outputs for the HighByte UI edge auth (+ optional custom domain)."""
        if self.ui_domain_name:
            CfnOutput(
                self,
                "HighByteCustomDomainUrl",
                value=f"https://{self.ui_domain_name}",
                description="HighByte Intelligence Hub UI (custom domain)",
            )

        CfnOutput(
            self,
            "HighByteUiUserPoolId",
            value=self.user_pool.user_pool_id,
            description="Amazon Cognito User Pool ID for the HighByte UI edge auth",
        )

        CfnOutput(
            self,
            "HighByteUiUserPoolDomain",
            value=self.user_pool_domain_value,
            description="Amazon Cognito User Pool Domain for the HighByte UI edge auth",
        )
