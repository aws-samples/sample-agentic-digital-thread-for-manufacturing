# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""agents/runtime_env_contract.py — golden parity + merge policies + hygiene.

Golden fixtures: the legacy deploy.sh env blobs (ORCH_ENV §2c, ONB_ENV §2e and
the workshop fragment, lifted VERBATIM from the pre-contract deploy.sh of
2026-08-04) are rendered with a documented bash-``${VAR:-default}`` renderer and
compared to ``build_env`` output by PARSED-JSON (value) equality — key order
is a json.dumps artifact, not a contract.

Merge policies: ONBOARDING_SESSIONS_TABLE is
fill-if-absent-or-empty; SYSTEM_IDENTITY_CLAIM_SECRET_ARN is
overwrite-when-source-nonempty (the ADR-0049 rotation path — a fill-only
policy would strand stale signing-secret ARNs forever).

Anti-mirror: the SFN builder and the in-app builder agree
on the FULL unified key set by construction — the 4-key SFN starvation
(GRAPH_IDENTIFIER / I3X_TLS_VERIFY / SYSTEM_IDENTITY_CLAIM_SECRET_ARN /
DATASET_REF_BUCKET) is closed, not laundered into "expected extras".
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from agents.runtime_env_contract import (
    FILL_IF_ABSENT,
    LIVE_SWAP_KEYS,
    ONBOARDING_SESSIONS_TABLE_DEFAULT,
    SYSTEM_GUIDANCE_TABLE_DEFAULT,
    OPERATOR_OWNED_KEYS,
    OVERWRITE_WHEN_SOURCE_NONEMPTY,
    REQUIRED_KEYS,
    STALE_CHECKED_KEYS,
    WORKSHOP_ENV,
    WORKSHOP_ORCHESTRATOR_ENV,
    RuntimeEnvSources,
    build_env,
    default_bedrock_model,
    known_keys,
    missing_required,
    reconcile_env,
)

_ADT_ROOT = Path(__file__).resolve().parents[2]  # agentic_digital_thread/

# --------------------------------------------------------------------------- #
# Legacy blobs — lifted VERBATIM from the pre-contract deploy.sh (2026-08-04).
# Raw bash lines; _blob_template() strips the VAR=" wrapper and unescapes \".
# --------------------------------------------------------------------------- #
_LEGACY_ORCH_LINE = r'''ORCH_ENV="{\"GRAPH_IDENTIFIER\":\"$GRAPH_ID\",\"AWS_REGION\":\"$REGION\",\"BEDROCK_MODEL\":\"$ORCH_BEDROCK_MODEL\",\"AGENTCORE_GATEWAY_URL\":\"$GW_URL\",\"AGENTCORE_GATEWAY_ID\":\"$GW_ID\",\"ADT_APP_SERVICE_CLIENT_ID\":\"$APP_SERVICE_CLIENT_ID\",\"ADT_APP_USER_POOL_ID\":\"$APP_USER_POOL_ID\",\"SYSTEM_QUERY_MODE\":\"${SYSTEM_QUERY_MODE:-agent}\",\"SELF_LEARNING_AUTO_ACCEPT\":\"${SELF_LEARNING_AUTO_ACCEPT:-off}\",\"ORCHESTRATOR_DELEGATION_CONCURRENCY\":\"${ORCHESTRATOR_DELEGATION_CONCURRENCY:-3}\",\"SYSTEM_IDENTITY_CLAIM_SECRET_ARN\":\"$IDENTITY_CLAIM_SECRET_ARN\",\"ONBOARDING_SESSIONS_TABLE\":\"${ONBOARDING_SESSIONS_TABLE:-adt-onboarding-sessions}\",\"AGENT_OBSERVABILITY_ENABLED\":\"true\"$WORKSHOP_ENV_FRAG}"'''

_LEGACY_ONB_LINE = r'''ONB_ENV="{\"SYSTEM_ID\":\"onboarding\",\"ONBOARDING_SESSIONS_TABLE\":\"$ONB_TABLE\",\"ONBOARDING_PROVISIONER_SFN_ARN\":\"$ONB_SFN_ARN\",\"AWS_REGION\":\"$REGION\",\"AGENT_OBSERVABILITY_ENABLED\":\"true\"$WORKSHOP_ENV_FRAG}"'''

_LEGACY_WORKSHOP_FRAG = (r'''\"ADT_WORKSHOP\":\"1\",\"ADT_MAPPING_OVERRIDES_FILE\"'''
                         r''':\"/app/agents/demo/fixtures/workshop_mapping_overrides.json\"''')

_BASH_VAR = re.compile(r"\$\{(\w+):-([^}]*)\}|\$\{(\w+)\}|\$(\w+)")


def _blob_template(line: str) -> str:
    """The JSON template inside a legacy ``VAR="..."`` bash assignment."""
    body = line.split("=", 1)[1]
    assert body.startswith('"') and body.endswith('"')
    return body[1:-1].replace('\\"', '"')


def _render(template: str, values: dict) -> str:
    """Documented rendering step: bash ``${VAR:-default}``
    (empty ⇒ default), ``${VAR}`` and ``$VAR`` expansion over *values*."""
    def sub(match):
        if match.group(1) is not None:  # ${VAR:-default}
            return values.get(match.group(1), "") or match.group(2)
        name = match.group(3) or match.group(4)
        return values.get(name, "")
    return _BASH_VAR.sub(sub, template)


def _legacy_orch(values: dict) -> dict:
    """Render the legacy ORCH_ENV blob, replicating deploy.sh's region-keyed
    ORCH_BEDROCK_MODEL branch (the spelling the contract adopted)."""
    values = dict(values)
    # The default is the global inference profile in every Region (Claude
    # Sonnet 5.5 since 2026-10-21; the workshop pins its own model).
    values["ORCH_BEDROCK_MODEL"] = (values.get("BEDROCK_MODEL")
                                    or "global.anthropic.claude-sonnet-5-5")
    return json.loads(_render(_blob_template(_LEGACY_ORCH_LINE), values))


def _legacy_onb(values: dict) -> dict:
    return json.loads(_render(_blob_template(_LEGACY_ONB_LINE), values))


#: The one legacy ONB_ENV key the contract no longer writes: the Step
#: Functions provisioner it pointed at had no caller and was removed
#: (2026-10-29).
_REMOVED_ONB_KEY = "ONBOARDING_PROVISIONER_SFN_ARN"


def _frag(workshop: bool) -> str:
    return ("," + _LEGACY_WORKSHOP_FRAG.replace('\\"', '"')) if workshop else ""


_FIXTURE = {
    "GRAPH_ID": "g-abc123",
    "REGION": "us-east-1",
    "GW_URL": "https://gw.example.com/mcp",
    "GW_ID": "GW123ABC",
    "APP_SERVICE_CLIENT_ID": "svc-client-1",
    "APP_USER_POOL_ID": "us-east-1_Pool1",
    "IDENTITY_CLAIM_SECRET_ARN":
        "arn:aws:secretsmanager:us-east-1:111122223333:secret:adt/identity-claim",
    "ONB_TABLE": "custom-onb-table",
    "ONB_SFN_ARN": "arn:aws:states:us-east-1:111122223333:stateMachine:adt-onb",
}


def _sources(region="us-east-1", workshop=False, observability=True,
             **kw) -> RuntimeEnvSources:
    """The golden sources. The legacy blobs always emitted spans, so these
    default to a deploy with --observability on."""
    return RuntimeEnvSources(
        observability=observability,
        region=region,
        graph_id=_FIXTURE["GRAPH_ID"],
        gateway_url=_FIXTURE["GW_URL"],
        gateway_id=_FIXTURE["GW_ID"],
        app_service_client_id=_FIXTURE["APP_SERVICE_CLIENT_ID"],
        app_user_pool_id=_FIXTURE["APP_USER_POOL_ID"],
        identity_claim_secret_arn=_FIXTURE["IDENTITY_CLAIM_SECRET_ARN"],
        workshop=workshop,
        **kw,
    )


# --------------------------------------------------------------------------- #
# Golden parity — deploy.sh-written roles (orchestrator/onboarding)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("workshop", [False, True], ids=["base", "workshop"])
def test_orchestrator_golden_parity(workshop):
    """Byte parity with the legacy §2c ORCH_ENV blob — plus, under the
    workshop profile ONLY, the orchestrator-only fragment (MEMORY_LTM_SCOPE=
    session: one shared login ⇒ one actorId, so actor-wide LTM retrieval
    leaked other attendees' chats — workshop R2b defect C)."""
    values = dict(_FIXTURE, WORKSHOP_ENV_FRAG=_frag(workshop))
    legacy = _legacy_orch(values)
    if workshop:
        legacy.update(WORKSHOP_ORCHESTRATOR_ENV)
    built = build_env("orchestrator", _sources(workshop=workshop))
    assert built == legacy  # parsed-JSON (value) equality


def test_workshop_orchestrator_fragment_is_session_scoped_memory_only():
    assert WORKSHOP_ORCHESTRATOR_ENV == {"MEMORY_LTM_SCOPE": "session"}


def test_memory_scope_rides_only_the_workshop_orchestrator_payload():
    assert "MEMORY_LTM_SCOPE" not in build_env("orchestrator", _sources())
    assert build_env("orchestrator", _sources(workshop=True))[
        "MEMORY_LTM_SCOPE"] == "session"
    # The onboarding runtime keeps no chat memory: never carries the knob.
    for workshop in (False, True):
        assert "MEMORY_LTM_SCOPE" not in build_env(
            "onboarding", _sources(workshop=workshop))
    assert "MEMORY_LTM_SCOPE" not in WORKSHOP_ENV


def test_memory_scope_is_a_known_orchestrator_key_never_foreign():
    assert "MEMORY_LTM_SCOPE" in known_keys("orchestrator")


def test_memory_scope_is_not_a_reconciled_or_boot_required_key():
    """Build-only (full-replacement §2c rebuilds it per deploy, NOT sticky —
    ADR-0030); a base deploy is ABSENT ⇒ actor default, so it is neither
    reconciled nor required at boot."""
    for role in REQUIRED_KEYS:
        assert "MEMORY_LTM_SCOPE" not in REQUIRED_KEYS[role]
        assert "MEMORY_LTM_SCOPE" not in STALE_CHECKED_KEYS[role]


@pytest.mark.parametrize("workshop", [False, True], ids=["base", "workshop"])
def test_onboarding_golden_parity(workshop):
    """The contract is a SUPERSET of the legacy §2e ONB_ENV blob: every legacy
    key/value survives byte-for-byte, and the ONLY additions are
    GRAPH_IDENTIFIER (ADR-0069 — the onboarding runtime exports graph-reading
    tools from RUNTIME_TOOLS; the legacy blob's omission is the pre-existing
    defect, so a byte-equality pin here would enshrine the bug) and
    BEDROCK_MODEL (the
    same tools call Bedrock; without it they used model ids hardcoded in
    code, 2026-10-01)."""
    values = dict(_FIXTURE, WORKSHOP_ENV_FRAG=_frag(workshop))
    legacy = _legacy_onb(values)
    # The Step Functions provisioner ARN left with the provisioner (no
    # caller; removed 2026-10-29). Every other legacy key survives.
    assert legacy.pop(_REMOVED_ONB_KEY)
    built = build_env(
        "onboarding",
        _sources(workshop=workshop,
                 onboarding_sessions_table=_FIXTURE["ONB_TABLE"]))
    assert _REMOVED_ONB_KEY not in built
    assert {k: built[k] for k in legacy} == legacy  # legacy keys preserved
    assert set(built) - set(legacy) == {"GRAPH_IDENTIFIER", "BEDROCK_MODEL"}
    assert built["GRAPH_IDENTIFIER"] == _FIXTURE["GRAPH_ID"]
    assert built["BEDROCK_MODEL"] == default_bedrock_model(_FIXTURE["REGION"])


def test_onboarding_runs_the_pinned_model():
    """A pinned deployment (the workshop package) runs ONE model in both
    deploy-time runtimes; the orchestrator's value is the onboarding's."""
    sources = _sources(bedrock_model="global.anthropic.claude-sonnet-5")
    assert build_env("onboarding", sources)["BEDROCK_MODEL"] == \
        build_env("orchestrator", sources)["BEDROCK_MODEL"] == \
        "global.anthropic.claude-sonnet-5"


def test_orchestrator_golden_parity_with_explicit_overrides():
    values = dict(
        _FIXTURE, WORKSHOP_ENV_FRAG="",
        BEDROCK_MODEL="global.anthropic.claude-sonnet-5",
        SYSTEM_QUERY_MODE="orchestrator", SELF_LEARNING_AUTO_ACCEPT="on",
        ORCHESTRATOR_DELEGATION_CONCURRENCY="1",
        ONBOARDING_SESSIONS_TABLE="custom-onb-table",
    )
    legacy = _legacy_orch(values)
    built = build_env("orchestrator", _sources(
        bedrock_model="global.anthropic.claude-sonnet-5",
        system_query_mode="orchestrator", self_learning_auto_accept="on",
        delegation_concurrency="1",
        onboarding_sessions_table="custom-onb-table"))
    assert built == legacy


def test_orchestrator_eu_region_pin():
    """An eu-* Region gets the same global inference profile as every other
    Region (both legacy and contract)."""
    values = dict(_FIXTURE, REGION="eu-west-1", WORKSHOP_ENV_FRAG="")
    legacy = _legacy_orch(values)
    built = build_env("orchestrator", _sources(region="eu-west-1"))
    assert built == legacy
    assert built["BEDROCK_MODEL"] == "global.anthropic.claude-sonnet-5-5"
    assert default_bedrock_model("us-east-1") == "global.anthropic.claude-sonnet-5-5"


def test_module_docstring_names_the_real_model_defaults():
    """The contract's own docstring states the defaults its functions return:
    both roles use global inference profiles, the same in every Region, so it
    must not still describe a Region-keyed us./eu. default."""
    import re

    from agents import runtime_env_contract as contract

    doc = re.sub(r"\s+", " ", contract.__doc__ or "")
    for region in ("us-east-1", "eu-west-1", "ap-southeast-2"):
        assert contract.default_bedrock_model(region) in doc
        assert contract.default_system_agent_model(region) in doc
    assert "region-keyed spelling WINS" not in doc
    assert "defaults to the ``eu.`` inference profile" not in doc


def test_onboarding_empty_cfn_output_corner_is_a_decided_value_change():
    """Empty-output corner, DECIDED: where the legacy ONB_ENV blob wrote ""
    for ONBOARDING_SESSIONS_TABLE on a failed CFN-output read, the contract
    fills the CDK-fixed default — a required key is never written empty.
    Pinned: the diff vs legacy is exactly that one key plus the ADR-0069
    GRAPH_IDENTIFIER and the 2026-10-01 BEDROCK_MODEL additions (both absent
    from the legacy blob entirely)."""
    values = dict(_FIXTURE, ONB_TABLE="", WORKSHOP_ENV_FRAG="")
    legacy = _legacy_onb(values)
    del legacy[_REMOVED_ONB_KEY]  # removed with the provisioner, 2026-10-29
    built = build_env("onboarding", _sources())  # no table configured
    assert legacy["ONBOARDING_SESSIONS_TABLE"] == ""
    assert built["ONBOARDING_SESSIONS_TABLE"] == ONBOARDING_SESSIONS_TABLE_DEFAULT
    diff = {k for k in set(legacy) | set(built) if legacy.get(k) != built.get(k)}
    assert diff == {"ONBOARDING_SESSIONS_TABLE", "GRAPH_IDENTIFIER", "BEDROCK_MODEL"}


# --------------------------------------------------------------------------- #
# Golden parity — the in-app system builder (byte-parity re-scoped: deploy.sh
# roles above are blob-pinned; the system role pins the pre-contract
# _base_env_vars + _prepare_runtime_env spelling)
# --------------------------------------------------------------------------- #

def test_system_role_matches_legacy_in_app_spelling(monkeypatch):
    for key in ("SYSTEM_QUERY_MODE", "BEDROCK_MODEL", "GRAPH_IDENTIFIER",
                "I3X_TLS_VERIFY", "SYSTEM_IDENTITY_CLAIM_SECRET_ARN",
                "DATASET_REF_BUCKET", "STREAM_CAPTURE_BUCKET",
                "ONBOARDING_SESSIONS_TABLE", "ADT_SYSTEM_GUIDANCE_TABLE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("GRAPH_IDENTIFIER", "g-abc123")
    monkeypatch.setenv("SYSTEM_IDENTITY_CLAIM_SECRET_ARN", "arn:idc")
    monkeypatch.setenv("STREAM_CAPTURE_BUCKET", "stream-bucket")
    monkeypatch.setenv("AGENT_SPAN_EMISSION", "true")  # legacy: always on
    built = build_env(
        "system", RuntimeEnvSources.from_ambient(region="us-east-1"),
        system_id="erp", system_name="N" * 150, schema="S" * 150,
        connection_type="PostgreSQL", system_agent_model="model-x")
    assert built == {
        "SYSTEM_ID": "erp",
        "SYSTEM_NAME": "N" * 100,          # legacy [:100] truncation
        "SCHEMA": "S" * 100,
        "CONNECTION_TYPE": "PostgreSQL",
        "AWS_REGION": "us-east-1",
        "SYSTEM_QUERY_MODE": "agent",
        "SYSTEM_AGENT_MODEL": "model-x",
        "BEDROCK_MODEL": "global.anthropic.claude-sonnet-5-5",
        "AGENT_OBSERVABILITY_ENABLED": "true",
        "GRAPH_IDENTIFIER": "g-abc123",
        "I3X_TLS_VERIFY": "true",
        "SYSTEM_IDENTITY_CLAIM_SECRET_ARN": "arn:idc",
        "DATASET_REF_BUCKET": "stream-bucket",  # legacy STREAM_CAPTURE fallback
        "ONBOARDING_SESSIONS_TABLE": ONBOARDING_SESSIONS_TABLE_DEFAULT,
        "ADT_SYSTEM_GUIDANCE_TABLE": SYSTEM_GUIDANCE_TABLE_DEFAULT,
    }


def test_anti_mirror_sfn_and_in_app_builders_agree_on_the_full_set(monkeypatch):
    """ONE spelling by construction. The SFN builder output
    equals the in-app base builder output + the connection key — including the
    four keys the old mirror starved (GRAPH_IDENTIFIER, I3X_TLS_VERIFY,
    SYSTEM_IDENTITY_CLAIM_SECRET_ARN, DATASET_REF_BUCKET)."""
    from agents.agent_manager import AgentManager
    from agents.provisioner.handlers import _runtime_env_vars
    from agents.provisioner.models import CreateRuntimeInput

    monkeypatch.setenv("SYSTEM_AGENT_MODEL", "model-x")
    monkeypatch.setenv("GRAPH_IDENTIFIER", "g-abc123")
    monkeypatch.setenv("SYSTEM_IDENTITY_CLAIM_SECRET_ARN", "arn:idc")
    monkeypatch.delenv("ONBOARDING_SESSIONS_TABLE", raising=False)
    monkeypatch.delenv("BEDROCK_MODEL", raising=False)

    in_app = AgentManager._base_env_vars(
        "erp", "Example Corp ERP", "public", "PostgreSQL", "us-east-1", {})
    inp = CreateRuntimeInput(
        system_id="erp", system_name="Example Corp ERP", schema="public",
        connection_type="PostgreSQL", region="us-east-1",
        system_agent_model="model-x",
        bedrock_model=in_app["BEDROCK_MODEL"])
    sfn = _runtime_env_vars(inp, "arn:secret", {"host": "db"})

    assert sfn.pop("CONNECTION_SECRET_ARN") == "arn:secret"
    assert sfn == in_app  # full-set agreement — no "expected extras" laundering
    for key in ("GRAPH_IDENTIFIER", "I3X_TLS_VERIFY",
                "SYSTEM_IDENTITY_CLAIM_SECRET_ARN", "DATASET_REF_BUCKET"):
        assert key in in_app, f"unified key {key} missing"


# --------------------------------------------------------------------------- #
# reconcile_env — one test per merge policy
# --------------------------------------------------------------------------- #

def test_registry_policies_are_the_reviewed_pair():
    assert REQUIRED_KEYS["system"]["ONBOARDING_SESSIONS_TABLE"] == FILL_IF_ABSENT
    assert (REQUIRED_KEYS["system"]["SYSTEM_IDENTITY_CLAIM_SECRET_ARN"]
            == OVERWRITE_WHEN_SOURCE_NONEMPTY)
    # RESPONSE_INTEGRITY_MODE (ADR-0047) joined the registry on EVERY role with
    # the overwrite-when-source-nonempty policy: the refresh paths reconcile
    # rather than rebuild, so without it an opted-in deploy left existing
    # runtimes on the old posture (observed live: app + orchestrator annotate,
    # system runtimes absent). Empty source ⇒ untouched, so opt-in still holds.
    for role in ("orchestrator", "onboarding", "system"):
        assert (REQUIRED_KEYS[role]["RESPONSE_INTEGRITY_MODE"]
                == OVERWRITE_WHEN_SOURCE_NONEMPTY)
    # AWS_ACCOUNT_ID (the deployment account) is reconciled onto the onboarding
    # and system runtimes (test_runtime_env_aws_account_id.py); never the
    # orchestrator, which reads no connection secret. ALLOWED_MODELS (the
    # deployment's model allowlist) follows RESPONSE_INTEGRITY_MODE's rule on
    # every role (test_runtime_env_allowed_models.py).
    assert REQUIRED_KEYS["orchestrator"] == {
        "ONBOARDING_SESSIONS_TABLE": FILL_IF_ABSENT,
        "RESPONSE_INTEGRITY_MODE": OVERWRITE_WHEN_SOURCE_NONEMPTY,
        "ALLOWED_MODELS": OVERWRITE_WHEN_SOURCE_NONEMPTY,
    }
    assert REQUIRED_KEYS["onboarding"] == {
        "ONBOARDING_SESSIONS_TABLE": FILL_IF_ABSENT,
        "RESPONSE_INTEGRITY_MODE": OVERWRITE_WHEN_SOURCE_NONEMPTY,
        "AWS_ACCOUNT_ID": OVERWRITE_WHEN_SOURCE_NONEMPTY,
        "ALLOWED_MODELS": OVERWRITE_WHEN_SOURCE_NONEMPTY,
    }


def test_reconcile_fill_policy_fills_absent_and_empty_only():
    src = RuntimeEnvSources(onboarding_sessions_table="cfn-table")
    assert reconcile_env("system", {}, src)["ONBOARDING_SESSIONS_TABLE"] == "cfn-table"
    assert (reconcile_env("system", {"ONBOARDING_SESSIONS_TABLE": "  "}, src)
            ["ONBOARDING_SESSIONS_TABLE"] == "cfn-table")
    assert (reconcile_env("system", {"ONBOARDING_SESSIONS_TABLE": "keep-me"}, src)
            ["ONBOARDING_SESSIONS_TABLE"] == "keep-me")


def test_reconcile_overwrite_policy_rotates_a_stale_identity_arn():
    """THE defect-1 pin: §2f's merge is OVERWRITE when the deploy resolved an
    ARN (ADR-0049 rotation/propagation) — fill-only would strand stale
    signing-secret ARNs on every existing runtime forever."""
    rotated_arn = "arn:NEW"
    stale_arn = "arn:OLD"
    src = RuntimeEnvSources(identity_claim_secret_arn=rotated_arn)
    out = reconcile_env("system", {"SYSTEM_IDENTITY_CLAIM_SECRET_ARN": stale_arn}, src)
    assert out["SYSTEM_IDENTITY_CLAIM_SECRET_ARN"] == rotated_arn


def test_reconcile_overwrite_policy_noops_on_empty_source():
    retained_arn = "arn:OLD"
    out = reconcile_env("system",
                        {"SYSTEM_IDENTITY_CLAIM_SECRET_ARN": retained_arn},
                        RuntimeEnvSources())
    assert out["SYSTEM_IDENTITY_CLAIM_SECRET_ARN"] == retained_arn


def test_reconcile_never_touches_operator_or_unknown_keys():
    existing = {"SYSTEM_REQUIRED_GROUP": "plant-ops",
                "CONNECTION_SECRET_ARN": "arn:conn",
                "SOME_FUTURE_KEY": "x",
                "ONBOARDING_SESSIONS_TABLE": "t"}
    identity_arn = "arn:idc"
    out = reconcile_env("system", existing,
                        RuntimeEnvSources(identity_claim_secret_arn=identity_arn))
    for key, val in existing.items():
        assert out[key] == val
    assert set(out) == set(existing) | {"SYSTEM_IDENTITY_CLAIM_SECRET_ARN"}


def test_reconcile_is_noop_on_a_contract_complete_env():
    src = _sources(onboarding_sessions_table="t")
    complete = build_env("system", src, system_id="erp", system_name="E",
                         schema="s", connection_type="PostgreSQL",
                         system_agent_model="m")
    assert reconcile_env("system", complete, src) == complete


def test_reconcile_never_deletes_and_does_not_mutate_input():
    existing = {"A": "1"}
    out = reconcile_env("orchestrator", existing, RuntimeEnvSources())
    assert existing == {"A": "1"}
    assert out["A"] == "1"


# --------------------------------------------------------------------------- #
# Hygiene: no os.environ mutation; import-light; audit registries coherent
# --------------------------------------------------------------------------- #

def test_no_process_env_mutation_ever(monkeypatch):
    monkeypatch.delenv("ONBOARDING_SESSIONS_TABLE", raising=False)
    before = dict(os.environ)
    src = RuntimeEnvSources.from_ambient()
    build_env("orchestrator", src)
    build_env("onboarding", src)
    build_env("system", src, system_id="x", system_name="x", schema="x",
              connection_type="x", system_agent_model="x")
    reconcile_env("system", {}, src)
    assert dict(os.environ) == before  # JobStore local-dev gate stays intact


def test_module_is_import_light_stdlib_only():
    """Importing the module must pull in NO third-party packages — the
    deploy.sh shim runs on machines without the app venv (subprocess check
    so this test's own imports don't pollute the measurement)."""
    code = (
        "import sys; import agents.runtime_env_contract; "
        "bad = sorted({m.split('.')[0] for m in sys.modules} & "
        "{'boto3', 'botocore', 'strands', 'requests', 'pydantic', 'flask'}); "
        "sys.exit(1 if bad else 0)"
    )
    proc = subprocess.run([sys.executable, "-c", code], cwd=_ADT_ROOT,
                          env={**_clean_env()}, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_live_swap_keys_are_never_stale_checked():
    """Model-swapped env is NOT stale."""
    for role, keys in STALE_CHECKED_KEYS.items():
        assert not (keys & LIVE_SWAP_KEYS), (role, keys & LIVE_SWAP_KEYS)


def test_operator_owned_keys_are_known_everywhere():
    for role in REQUIRED_KEYS:
        assert OPERATOR_OWNED_KEYS <= known_keys(role)
    assert "SYSTEM_REQUIRED_GROUP" in OPERATOR_OWNED_KEYS
    assert WORKSHOP_ENV.keys() <= known_keys("orchestrator")


# --------------------------------------------------------------------------- #
# missing_required — the ONE boot-time validator (ADR-0069)
# --------------------------------------------------------------------------- #

def _complete_env(role: str) -> dict:
    return {key: f"value-{key.lower()}" for key in STALE_CHECKED_KEYS[role]}


def test_the_contract_has_no_provisioner_state_machine_arn():
    """No role builds, requires, stale-checks or knows the removed
    provisioner's ARN, and the sources carry no field for it."""
    for role in STALE_CHECKED_KEYS:
        assert _REMOVED_ONB_KEY not in STALE_CHECKED_KEYS[role]
        assert _REMOVED_ONB_KEY not in REQUIRED_KEYS.get(role, ())
        assert _REMOVED_ONB_KEY not in known_keys(role)
    for workshop in (False, True):
        assert _REMOVED_ONB_KEY not in build_env("onboarding", _sources(workshop=workshop))
    assert "onboarding_sfn_arn" not in RuntimeEnvSources.__dataclass_fields__
    ambient = RuntimeEnvSources.from_ambient({_REMOVED_ONB_KEY: "arn:aws:states:x"})
    assert _REMOVED_ONB_KEY not in build_env("onboarding", ambient)


def test_onboarding_required_set_includes_graph_identifier():
    """THE ADR-0069 pin: the onboarding runtime exports graph-reading tools
    (RUNTIME_TOOLS), so GRAPH_IDENTIFIER is required (and stale-checked by the
    drift lane) — its absence was invisible for the runtime's whole life."""
    assert "GRAPH_IDENTIFIER" in STALE_CHECKED_KEYS["onboarding"]
    assert build_env("onboarding", _sources())["GRAPH_IDENTIFIER"] == \
        _FIXTURE["GRAPH_ID"]


@pytest.mark.parametrize("role", ["orchestrator", "onboarding", "system"])
def test_missing_required_empty_on_complete_env(role):
    assert missing_required(role, _complete_env(role)) == []


@pytest.mark.parametrize("absent", ["GRAPH_IDENTIFIER",
                                    "ONBOARDING_SESSIONS_TABLE"])
def test_missing_required_names_absent_and_empty_keys(absent):
    env = _complete_env("onboarding")
    del env[absent]
    assert missing_required("onboarding", env) == [absent]
    env[absent] = "   "  # whitespace-only counts as empty
    assert missing_required("onboarding", env) == [absent]


def test_missing_required_is_the_stale_checked_set_and_sorted():
    for role in STALE_CHECKED_KEYS:
        assert missing_required(role, {}) == sorted(STALE_CHECKED_KEYS[role])


def test_missing_required_unknown_role_raises():
    with pytest.raises(ValueError, match="unknown runtime env role"):
        missing_required("bogus", {})


def test_missing_required_never_reads_or_mutates_process_env(monkeypatch):
    """PURE: only the handed-in mapping is consulted; os.environ untouched."""
    monkeypatch.setenv("GRAPH_IDENTIFIER", "ambient-should-be-ignored")
    before = dict(os.environ)
    assert "GRAPH_IDENTIFIER" in missing_required("onboarding", {})
    assert dict(os.environ) == before


def test_missing_required_never_requires_live_swap_keys():
    """Live-swap keys are legitimately absent (ADR-0055 model swaps etc.) —
    the boot validator must never fail closed on them."""
    for role in STALE_CHECKED_KEYS:
        assert not set(missing_required(role, {})) & LIVE_SWAP_KEYS


# --------------------------------------------------------------------------- #
# The shim (python3 -m agents.runtime_env_contract) — its contract
# --------------------------------------------------------------------------- #

def _clean_env() -> dict:
    """Minimal subprocess env: PATH + PYTHONPATH=. only (no ambient leakage)."""
    return {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": "."}


def _run_shim(args, env=None, stdin=""):
    return subprocess.run(
        [sys.executable, "-m", "agents.runtime_env_contract", *args],
        cwd=_ADT_ROOT, env={**_clean_env(), **(env or {})},
        input=stdin, capture_output=True, text=True)


def test_shim_build_matches_the_api_and_golden_sources():
    env = {
        "GRAPH_IDENTIFIER": _FIXTURE["GRAPH_ID"], "AWS_REGION": "us-east-1",
        "AGENTCORE_GATEWAY_URL": _FIXTURE["GW_URL"],
        "AGENTCORE_GATEWAY_ID": _FIXTURE["GW_ID"],
        "ADT_APP_SERVICE_CLIENT_ID": _FIXTURE["APP_SERVICE_CLIENT_ID"],
        "ADT_APP_USER_POOL_ID": _FIXTURE["APP_USER_POOL_ID"],
        "SYSTEM_IDENTITY_CLAIM_SECRET_ARN": _FIXTURE["IDENTITY_CLAIM_SECRET_ARN"],
        "ADT_WORKSHOP": "1",
    }
    proc = _run_shim(["--role", "orchestrator"], env=env)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == build_env(
        "orchestrator", _sources(workshop=True, observability=False))


def test_shim_reconcile_applies_both_policies_and_preserves_the_rest():
    stale_arn = "arn:OLD"
    rotated_arn = "arn:NEW"
    starved = {"SYSTEM_ID": "erp", "SYSTEM_REQUIRED_GROUP": "ops",
               "SYSTEM_IDENTITY_CLAIM_SECRET_ARN": stale_arn}
    proc = _run_shim(
        ["--role", "system", "--reconcile"],
        env={"SYSTEM_IDENTITY_CLAIM_SECRET_ARN": rotated_arn,
             "ONBOARDING_SESSIONS_TABLE": "cfn-table"},
        stdin=json.dumps(starved))
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["ONBOARDING_SESSIONS_TABLE"] == "cfn-table"
    assert out["SYSTEM_IDENTITY_CLAIM_SECRET_ARN"] == rotated_arn
    assert out["SYSTEM_REQUIRED_GROUP"] == "ops"
    assert out["SYSTEM_ID"] == "erp"


@pytest.mark.parametrize("args,stdin", [
    (["--role", "bogus"], ""),                      # unknown role
    (["--role", "system"], ""),                     # system build unsupported
    (["--role", "system", "--reconcile"], "not-json"),
    (["--role", "system", "--reconcile"], "[1,2]"),  # non-object stdin
], ids=["bad-role", "system-build", "malformed-stdin", "non-object-stdin"])
def test_shim_fails_nonzero_with_empty_stdout(args, stdin):
    """Defect 6a: NOTHING on stdout unless valid JSON — a zero-exit empty
    result can never happen, and failures never write partial payloads."""
    proc = _run_shim(args, stdin=stdin)
    assert proc.returncode != 0
    assert proc.stdout == ""
    assert proc.stderr  # diagnostics on stderr


# --------------------------------------------------------------------------- #
# from_ambient — explicit-arg-wins + bash ${VAR:-default} semantics
# --------------------------------------------------------------------------- #

def test_from_ambient_defaults_and_overrides():
    src = RuntimeEnvSources.from_ambient(environ={}, region="eu-central-1")
    assert src.region == "eu-central-1"
    assert src.system_query_mode == "agent"
    assert src.self_learning_auto_accept == "off"
    assert src.delegation_concurrency == "3"
    assert src.i3x_tls_verify == "true"
    assert src.workshop is False


def test_from_ambient_empty_env_value_means_default():
    src = RuntimeEnvSources.from_ambient(environ={"SYSTEM_QUERY_MODE": ""})
    assert src.system_query_mode == "agent"  # bash ${VAR:-agent} semantics


def test_from_ambient_dataset_ref_bucket_fallback_chain():
    assert RuntimeEnvSources.from_ambient(
        environ={"STREAM_CAPTURE_BUCKET": "s"}).dataset_ref_bucket == "s"
    assert RuntimeEnvSources.from_ambient(
        environ={"DATASET_REF_BUCKET": "d",
                 "STREAM_CAPTURE_BUCKET": "s"}).dataset_ref_bucket == "d"


def test_system_guidance_table_is_required_and_built_only_by_contract():
    source = RuntimeEnvSources(system_guidance_table="custom-guidance")
    built = build_env(
        "system", source, system_id="system-a", system_name="A", schema="s",
        connection_type="PostgreSQL", system_agent_model="model")
    assert built["ADT_SYSTEM_GUIDANCE_TABLE"] == "custom-guidance"
    assert (REQUIRED_KEYS["system"]["ADT_SYSTEM_GUIDANCE_TABLE"]
            == OVERWRITE_WHEN_SOURCE_NONEMPTY)
    reconciled = reconcile_env(
        "system", {"ADT_SYSTEM_GUIDANCE_TABLE": "stale"}, source)
    assert reconciled["ADT_SYSTEM_GUIDANCE_TABLE"] == "custom-guidance"


# --------------------------------------------------------------------------- #
# Span emission follows the --observability deploy option.
# --------------------------------------------------------------------------- #
_SYSTEM_KW = dict(system_id="erp", system_name="ERP", schema="erp",
                  connection_type="PostgreSQL", system_agent_model="model-x")
_EXPORTERS_OFF = {"OTEL_TRACES_EXPORTER": "none", "OTEL_METRICS_EXPORTER": "none",
                  "OTEL_LOGS_EXPORTER": "none"}


@pytest.mark.parametrize("role", ["orchestrator", "onboarding", "system"])
def test_span_emission_is_off_without_the_observability_option(role):
    """Without --observability no span destination is set up, so the runtime
    exporters failed every batch (400/403). Off means no exporter at all."""
    kw = _SYSTEM_KW if role == "system" else {}
    env = build_env(role, RuntimeEnvSources(), **kw)
    assert env["AGENT_OBSERVABILITY_ENABLED"] == "false"
    assert {k: env.get(k) for k in _EXPORTERS_OFF} == _EXPORTERS_OFF


@pytest.mark.parametrize("role", ["orchestrator", "onboarding", "system"])
def test_span_emission_is_on_with_the_observability_option(role):
    kw = _SYSTEM_KW if role == "system" else {}
    env = build_env(role, RuntimeEnvSources(observability=True), **kw)
    assert env["AGENT_OBSERVABILITY_ENABLED"] == "true"
    assert not set(_EXPORTERS_OFF) & set(env)


@pytest.mark.parametrize("value,expected", [("true", True), ("false", False), ("", False)])
def test_ambient_observability_is_the_span_emission_flag(value, expected):
    """The ECS app carries AGENT_SPAN_EMISSION (stacks/_observability.py);
    deploy.sh passes it to the driver."""
    sources = RuntimeEnvSources.from_ambient({"AGENT_SPAN_EMISSION": value})
    assert sources.observability is expected


def test_deploy_sh_passes_the_observability_option_to_both_runtime_drivers():
    text = (_ADT_ROOT / "deploy.sh").read_text(encoding="utf-8")
    calls = re.findall(r"\$\(([^()]*?python3 scripts/provision_runtimes\.py --role (\w+))", text, re.S)
    assert {role for _, role in calls} == {"orchestrator", "onboarding"}
    for call, role in calls:
        assert 'AGENT_SPAN_EMISSION="$SPAN_EMISSION"' in call, role
    assert re.search(r'^SPAN_EMISSION=false$', text, re.M)
    assert text.count('  SPAN_EMISSION=true') == 2  # --observability, or its sticky SSM value


# --------------------------------------------------------------------------- #
# deploy.sh [2f] refresh: an existing runtime's span keys follow the option.
# --------------------------------------------------------------------------- #
_OLD_SYSTEM_ENV = {"SYSTEM_ID": "erp", "SYSTEM_REQUIRED_GROUP": "ops",
                   "CONNECTION_SECRET_ARN": "arn:conn", "AGENT_OBSERVABILITY_ENABLED": "true"}


def test_reconcile_turns_span_export_off_when_the_option_is_off():
    env = reconcile_env("system", _OLD_SYSTEM_ENV, RuntimeEnvSources(), span_emission=False)
    assert env["AGENT_OBSERVABILITY_ENABLED"] == "false"
    assert {k: env[k] for k in _EXPORTERS_OFF} == _EXPORTERS_OFF
    for key in ("SYSTEM_ID", "SYSTEM_REQUIRED_GROUP", "CONNECTION_SECRET_ARN"):
        assert env[key] == _OLD_SYSTEM_ENV[key], key


def test_reconcile_turns_span_export_back_on():
    old = {**_OLD_SYSTEM_ENV, "AGENT_OBSERVABILITY_ENABLED": "false", **_EXPORTERS_OFF}
    env = reconcile_env("system", old, RuntimeEnvSources(), span_emission=True)
    assert env["AGENT_OBSERVABILITY_ENABLED"] == "true"
    assert not set(_EXPORTERS_OFF) & set(env)


def test_reconcile_leaves_span_keys_alone_without_the_option():
    assert reconcile_env("system", _OLD_SYSTEM_ENV, RuntimeEnvSources()) == {
        **_OLD_SYSTEM_ENV, "ONBOARDING_SESSIONS_TABLE": ONBOARDING_SESSIONS_TABLE_DEFAULT}


@pytest.mark.parametrize("value,expected", [("false", "false"), ("true", "true")])
def test_shim_reconcile_applies_the_span_option(value, expected):
    proc = _run_shim(["--role", "system", "--reconcile"],
                     env={"AGENT_SPAN_EMISSION": value}, stdin=json.dumps(_OLD_SYSTEM_ENV))
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["AGENT_OBSERVABILITY_ENABLED"] == expected


def test_shim_reconcile_without_the_span_option_keeps_the_old_value():
    proc = _run_shim(["--role", "system", "--reconcile"], stdin=json.dumps(_OLD_SYSTEM_ENV))
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["AGENT_OBSERVABILITY_ENABLED"] == "true"


def test_deploy_sh_refresh_passes_the_span_option_to_the_reconcile():
    text = (_ADT_ROOT / "deploy.sh").read_text(encoding="utf-8")
    (call,) = re.findall(r"RT_ENV_NEW=\$\(printf '%s' \"\$RT_ENV\" \|(.*?)--role system --reconcile\)",
                         text, re.S)
    assert 'AGENT_SPAN_EMISSION="$SPAN_EMISSION"' in call
