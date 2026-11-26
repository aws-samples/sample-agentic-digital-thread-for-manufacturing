# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Deployment model allowlist (``ALLOWED_MODELS``): parsing, catalog flags,
default fallback, the PUT /llm-settings refusal, and the boot check.

Unset keeps today's behavior exactly; every test that sets the variable does
it through ``monkeypatch`` so nothing leaks into the rest of the lane.
"""
import asyncio
import json
import types
from unittest.mock import patch

import pytest

from agents import model_catalog as mc
from agents.model_catalog import AGENT, ORCHESTRATOR

SONNET_5 = "anthropic.claude-sonnet-5"
SONNET_55 = "anthropic.claude-sonnet-5-5"
SONNET_46 = "anthropic.claude-sonnet-4-6"
HAIKU = "anthropic.claude-haiku-4-5-20251001-v1:0"
HAIKU_55 = "anthropic.claude-haiku-5-5"
NOVA_PRO = "amazon.nova-pro-v1:0"
GPT_SOL = "openai.gpt-5.6-sol"
GPT_TERRA = "openai.gpt-5.6-terra"


@pytest.fixture
def allow(monkeypatch):
    """Set ALLOWED_MODELS for one test."""
    def _set(value: str):
        monkeypatch.setenv(mc.ALLOWED_MODELS_ENV, value)
    monkeypatch.delenv(mc.ALLOWED_MODELS_ENV, raising=False)
    return _set


# -- parsing -----------------------------------------------------------------

@pytest.mark.parametrize("raw", [None, "", "   ", "\n"])
def test_unset_or_blank_means_no_restriction(raw):
    assert mc.parse_allowed_models(raw) is None


def test_parses_families_tolerating_spaces_prefixes_and_empty_entries():
    raw = f" {SONNET_5} , us.{HAIKU},,eu.{SONNET_46} ,"
    assert mc.parse_allowed_models(raw) == frozenset({SONNET_5, HAIKU, SONNET_46})


def test_mantle_ids_parse_unchanged():
    assert mc.parse_allowed_models(GPT_SOL) == frozenset({GPT_SOL})


def test_unknown_id_fails_loudly_naming_it():
    with pytest.raises(ValueError) as err:
        mc.parse_allowed_models(f"{SONNET_5},anthropic.claude-sonet-5,made.up-model")
    message = str(err.value)
    assert "anthropic.claude-sonet-5" in message and "made.up-model" in message
    assert mc.ALLOWED_MODELS_ENV in message


def test_set_but_empty_list_fails_loudly():
    with pytest.raises(ValueError, match="names no models"):
        mc.parse_allowed_models(" , ,")


def test_allowed_families_reads_the_given_environ():
    assert mc.allowed_families({}) is None
    assert mc.allowed_families({mc.ALLOWED_MODELS_ENV: HAIKU}) == frozenset({HAIKU})


def test_is_allowed_accepts_prefixed_ids_and_none_allows_everything():
    allowed = frozenset({SONNET_5})
    assert mc.is_allowed(f"us.{SONNET_5}", allowed)
    assert mc.is_allowed(SONNET_5, allowed)
    assert not mc.is_allowed(f"us.{SONNET_46}", allowed)
    assert mc.is_allowed("anything.at-all", None)


# -- catalog flags -----------------------------------------------------------

#: The pre-allowlist catalog response contract: top-level keys and
#: per-entry keys. Unset/blank ALLOWED_MODELS must produce exactly these.
_PRE_FEATURE_TOP_KEYS = {ORCHESTRATOR, AGENT, "region"}
_PRE_FEATURE_ENTRY_KEYS = {"id", "family", "label", "role", "input_per_1k",
                           "output_per_1k", "default", "available"}


def _pre_feature_catalog(region: str, available_ids):
    """Verbatim re-statement of catalog() before the allowlist, the
    reference the unset/blank response must equal."""
    out = {ORCHESTRATOR: [], AGENT: [], "region": region}
    for role in (ORCHESTRATOR, AGENT):
        for m in mc.MODELS:
            rid = mc.resolve_id(m.family, region)
            available = True if (available_ids is None or m.mantle) else (rid in available_ids)
            out[role].append({
                "id": rid, "family": m.family, "label": m.label, "role": m.role,
                "input_per_1k": m.input_per_1k, "output_per_1k": m.output_per_1k,
                "default": m.default and m.role == role, "available": available,
            })
    return out


@pytest.mark.parametrize("raw", [None, "", "   "])
@pytest.mark.parametrize("region, available_ids", [
    ("us-east-1", None),
    ("eu-west-1", {f"eu.{SONNET_46}", f"eu.{HAIKU}"}),
])
def test_catalog_unset_or_blank_keeps_the_pre_feature_response_exactly(
        allow, raw, region, available_ids):
    if raw is not None:
        allow(raw)
    cat = mc.catalog(region, available_ids)
    # The memory list (test_memory_model_catalog.py) is the one addition.
    assert set(cat) == _PRE_FEATURE_TOP_KEYS | {mc.MEMORY}
    for role in (ORCHESTRATOR, AGENT, mc.MEMORY):
        for entry in cat[role]:
            assert set(entry) == _PRE_FEATURE_ENTRY_KEYS, entry
    assert {k: v for k, v in cat.items() if k != mc.MEMORY} == \
        _pre_feature_catalog(region, available_ids)


def test_catalog_flags_only_models_outside_the_allowlist(allow):
    allow(f"{SONNET_5},{HAIKU}")
    cat = mc.catalog("us-east-1")
    assert cat["allowlist_active"] is True
    assert set(cat) == _PRE_FEATURE_TOP_KEYS | {"allowlist_active", mc.MEMORY}
    for role, offered in ((ORCHESTRATOR, {SONNET_5}), (AGENT, {HAIKU})):
        # Every model is still listed (grey out, never hide).
        assert len(cat[role]) == len(mc.MODELS)
        assert all(set(e) == _PRE_FEATURE_ENTRY_KEYS | {"allowed"} for e in cat[role])
        allowed = {e["family"] for e in cat[role] if e["allowed"]}
        # Each role is offered the allowed models of its own catalog role.
        assert allowed == offered


def _defaults(cat: dict, role: str) -> list[dict]:
    return [e for e in cat[role] if e["default"]]


@pytest.mark.parametrize("raw", [
    f"{SONNET_5},{HAIKU}",            # the workshop pair
    f"{HAIKU},{NOVA_PRO}",            # agent-role models only
    f"{SONNET_5},{SONNET_46},{NOVA_PRO}",
    GPT_SOL,                          # one Mantle orchestrator model only
    f"{GPT_TERRA},{GPT_SOL}",
])
@pytest.mark.parametrize("region", ["us-east-1", "eu-west-1"])
def test_catalog_default_marker_is_the_effective_default(allow, raw, region):
    """Under an allowlist each list marks exactly one default: the allowed
    model default_model(role) returns, never a model the server refuses."""
    allow(raw)
    cat = mc.catalog(region)
    for role in (ORCHESTRATOR, AGENT):
        (default,) = _defaults(cat, role)
        assert default["allowed"] is True
        assert default["id"] == mc.default_model(role, region)


def test_catalog_default_marker_for_the_workshop_pair(allow):
    allow(f"{SONNET_5},{HAIKU}")
    cat = mc.catalog("us-east-1")
    assert [e["id"] for e in _defaults(cat, ORCHESTRATOR)] == [f"global.{SONNET_5}"]
    assert [e["id"] for e in _defaults(cat, AGENT)] == [f"global.{HAIKU}"]
    # The static orchestrator default (Sonnet 4.6) is not offered and not marked.
    by_family = {e["family"]: e for e in cat[ORCHESTRATOR]}
    assert by_family[SONNET_46]["default"] is False
    assert by_family[SONNET_46]["allowed"] is False


def test_catalog_default_marker_with_only_agent_role_models(allow):
    """No orchestrator-role model allowed: the orchestrator list's default is
    the cross-role fallback (first allowed model), the same one
    default_model(ORCHESTRATOR) returns."""
    allow(f"{HAIKU},{NOVA_PRO}")
    cat = mc.catalog("us-east-1")
    assert [e["id"] for e in _defaults(cat, ORCHESTRATOR)] == [f"global.{HAIKU}"]
    assert [e["id"] for e in _defaults(cat, AGENT)] == [f"global.{HAIKU}"]
    assert mc.default_model(ORCHESTRATOR, "us-east-1") == f"global.{HAIKU}"


def test_allowed_flag_is_independent_of_region_availability(allow):
    allow(f"{SONNET_5},{HAIKU}")
    available = {f"global.{SONNET_46}"}  # Sonnet 5 NOT enabled in the region
    by_family = {e["family"]: e for e in mc.catalog("us-east-1", available)[ORCHESTRATOR]}
    assert by_family[SONNET_5]["allowed"] is True
    assert by_family[SONNET_5]["available"] is False
    assert by_family[SONNET_46]["available"] is True
    assert by_family[SONNET_46]["allowed"] is False


def test_mantle_entries_follow_the_same_rule(allow):
    allow(f"{SONNET_5},{GPT_SOL}")
    by_family = {e["family"]: e for e in mc.catalog("us-east-1", set())[ORCHESTRATOR]}
    assert by_family[GPT_SOL]["allowed"] is True
    assert by_family[GPT_TERRA]["allowed"] is False
    # Mantle availability exemption unchanged.
    assert by_family[GPT_TERRA]["available"] is True


# -- default fallback ----------------------------------------------------------

def test_default_unset_is_unchanged(allow):
    assert mc.default_model(ORCHESTRATOR, "us-east-1") == f"global.{SONNET_55}"
    assert mc.default_model(AGENT, "us-east-1") == f"global.{HAIKU_55}"
    assert mc.default_model("no-such-role", "us-east-1") == ""


def test_default_never_leaves_the_allowlist(allow):
    """The workshop allowlist (Sonnet 5, Haiku 4.5) keeps the workshop on its
    two models even though neither is the catalog default any more."""
    allow(f"{SONNET_5},{HAIKU}")
    assert mc.default_model(ORCHESTRATOR, "us-east-1") == f"global.{SONNET_5}"
    assert mc.default_model(ORCHESTRATOR, "eu-west-1") == f"global.{SONNET_5}"
    assert mc.default_model(AGENT, "us-east-1") == f"global.{HAIKU}"


def test_workshop_allowlist_refuses_the_claude_5_5_models(allow):
    """Admin cannot pick a Claude 5.5 model in the workshop: the catalog
    lists them disabled and the save path's is_allowed check refuses them."""
    allow(f"{SONNET_5},{HAIKU}")
    allowed = mc.allowed_families()
    for family in ("anthropic.claude-opus-5-5", SONNET_55, HAIKU_55):
        assert not mc.is_allowed(f"global.{family}", allowed), family
    cat = mc.catalog("us-east-1")
    for role, expected in ((ORCHESTRATOR, {SONNET_5}), (AGENT, {HAIKU})):
        offered = {e["family"] for e in cat[role] if e["allowed"]}
        assert offered == expected, (role, offered)
    assert [e["id"] for e in cat[ORCHESTRATOR] if e["default"]] == [f"global.{SONNET_5}"]
    assert [e["id"] for e in cat[AGENT] if e["default"]] == [f"global.{HAIKU}"]


def test_default_keeps_the_role_default_when_it_is_allowed(allow, monkeypatch):
    # Put a non-default orchestrator model FIRST so "the role default" and
    # "the first allowed model of the role" are different answers.
    sonnet_46 = next(m for m in mc.MODELS if m.family == SONNET_46)
    monkeypatch.setattr(mc, "MODELS",
                        [sonnet_46] + [m for m in mc.MODELS if m is not sonnet_46])
    allow(f"{SONNET_55},{SONNET_46},{NOVA_PRO}")
    assert mc.default_model(ORCHESTRATOR, "us-east-1") == f"global.{SONNET_55}"
    # Haiku 5.5 (agent default) is not allowed: first allowed agent-role model.
    assert mc.default_model(AGENT, "us-east-1") == f"us.{NOVA_PRO}"


def test_default_falls_back_to_first_allowed_of_any_role(allow):
    allow(HAIKU)  # no orchestrator-role model allowed at all
    assert mc.default_model(ORCHESTRATOR, "us-east-1") == f"global.{HAIKU}"
    allow(GPT_SOL)  # no agent-role model allowed at all
    assert mc.default_model(AGENT, "us-east-1") == GPT_SOL


def test_configured_models_default_respects_the_allowlist(allow, monkeypatch):
    """model_config falls back to default_model when SSM and env are empty."""
    from agents import model_config
    allow(f"{SONNET_5},{HAIKU}")
    for name in ("BEDROCK_MODEL", "SYSTEM_AGENT_MODEL", "AGENT_MODEL"):
        monkeypatch.delenv(name, raising=False)
    with patch.object(model_config, "_ssm_value", return_value=""):
        models = model_config.get_configured_models("us-east-1")
    assert models["orchestrator"] == f"global.{SONNET_5}"
    assert models["agent"] == f"global.{HAIKU}"


def test_settings_ignore_a_saved_model_the_allowlist_excludes(allow, monkeypatch):
    """A model saved before a deploy tightened the allowlist (a demo account
    with Sonnet 5.5 saved, redeployed as a workshop) is not what runs:
    deploy.sh does not deploy it and the save path refuses it. The Settings
    readback must report the model that runs, not the stale saved value."""
    from agents import model_config
    allow(f"{SONNET_5},{HAIKU}")
    monkeypatch.setenv("BEDROCK_MODEL", f"global.{SONNET_5}")
    monkeypatch.setenv("SYSTEM_AGENT_MODEL", f"us.{HAIKU}")
    saved = {"/adt/llm-orchestrator-model": f"global.{SONNET_55}",
             "/adt/llm-agent-model": f"global.{HAIKU_55}"}
    with patch.object(model_config, "_ssm_value",
                      side_effect=lambda name, region: saved.get(name, "")):
        models = model_config.get_configured_models("us-east-1")
    assert models["orchestrator"] == f"global.{SONNET_5}"
    assert models["agent"] == f"us.{HAIKU}"


def test_settings_report_an_allowed_saved_model(allow, monkeypatch):
    from agents import model_config
    allow(f"{SONNET_5},{SONNET_55},{HAIKU}")
    monkeypatch.setenv("BEDROCK_MODEL", f"global.{SONNET_5}")
    with patch.object(model_config, "_ssm_value",
                      side_effect=lambda name, region: (
                          f"global.{SONNET_55}" if name == "/adt/llm-orchestrator-model" else "")):
        assert model_config.get_configured_models("us-east-1")["orchestrator"] == \
            f"global.{SONNET_55}"


# -- PUT /llm-settings refusal --------------------------------------------------

class _FakeSSM:
    def __init__(self):
        self.store: dict[str, str] = {}

    def get_parameter(self, Name):  # noqa: N803 - boto3 API casing
        if Name not in self.store:
            raise Exception(f"ParameterNotFound: {Name}")
        return {"Parameter": {"Value": self.store[Name]}}

    def put_parameter(self, Name, Value, Type="String", Overwrite=True):  # noqa: N803
        self.store[Name] = Value
        return {}


class _FakeControl:
    def list_agent_runtimes(self, **_kwargs):
        return {"agentRuntimes": []}


def _request(body: dict):
    req = types.SimpleNamespace(state=types.SimpleNamespace(is_admin=True))

    async def _json():
        return body
    req.json = _json
    return req


def _put(body: dict, ssm: _FakeSSM, clients: list):
    from agents.admin_routes import update_llm_settings_route

    def fake_client(service_name, **_kwargs):
        clients.append(service_name)
        if service_name == "ssm":
            return ssm
        if service_name == "bedrock-agentcore-control":
            return _FakeControl()
        raise RuntimeError(f"unexpected boto3 client in test: {service_name}")

    with patch("boto3.client", fake_client):
        return asyncio.run(update_llm_settings_route(_request(body)))


@pytest.fixture
def aws_env(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("BEDROCK_MODEL", f"global.{SONNET_5}")
    monkeypatch.setenv("SYSTEM_AGENT_MODEL", f"global.{HAIKU}")


@pytest.mark.parametrize("body, refused", [
    ({"orchestrator_model": f"global.{SONNET_46}", "agent_model": f"global.{HAIKU}"},
     ["orchestrator_model"]),
    ({"orchestrator_model": "", "agent_model": f"us.{NOVA_PRO}"}, ["agent_model"]),
    ({"orchestrator_model": "eu.custom.model-v9", "agent_model": GPT_SOL},
     ["orchestrator_model", "agent_model"]),
])
def test_put_refuses_a_disallowed_model_before_any_write(allow, aws_env, body, refused):
    allow(f"{SONNET_5},{HAIKU}")
    ssm, clients = _FakeSSM(), []
    res = _put(body, ssm, clients)
    assert res.status_code == 400
    payload = json.loads(res.body)
    assert payload["status"] == "error"
    for field in refused:
        assert field in payload["message"]
    assert "not offered in this deployment" in payload["message"]
    assert SONNET_5 in payload["message"] and HAIKU in payload["message"]
    # Nothing was written, and no AWS client was even created.
    assert ssm.store == {}
    assert clients == []


def test_put_saves_an_allowed_model(allow, aws_env):
    allow(f"{SONNET_5},{HAIKU}")
    ssm, clients = _FakeSSM(), []
    res = _put({"orchestrator_model": f"global.{SONNET_5}", "agent_model": f"global.{HAIKU}"},
               ssm, clients)
    assert res["status"] == "ok", res
    assert ssm.store["/adt/llm-orchestrator-model"] == f"global.{SONNET_5}"
    assert ssm.store["/adt/llm-agent-model"] == f"global.{HAIKU}"


def test_put_empty_field_is_never_refused(allow, aws_env):
    """An empty field means "leave unchanged" (the page sends it for a
    configured model it shows greyed out)."""
    allow(f"{SONNET_5},{HAIKU}")
    ssm, clients = _FakeSSM(), []
    res = _put({"orchestrator_model": "", "agent_model": f"global.{HAIKU}"}, ssm, clients)
    assert res["status"] == "ok", res
    assert "/adt/llm-orchestrator-model" not in ssm.store


def test_put_unset_allowlist_accepts_any_model(allow, aws_env):
    ssm, clients = _FakeSSM(), []
    res = _put({"orchestrator_model": f"global.{SONNET_46}", "agent_model": "eu.custom.model-v9"},
               ssm, clients)
    assert res["status"] == "ok", res
    assert ssm.store["/adt/llm-agent-model"] == "eu.custom.model-v9"


# -- boot check ------------------------------------------------------------------

#: Every startup step lifespan runs after its config validators. Each is
#: replaced by a recorder, so the test never starts a real worker.
_STARTUP_WORKERS = (
    "_restore_graph", "_start_digital_thread_provisioner",
    "_start_query_bridge_provisioner", "_init_prompt_library",
    "_register_demo_systems", "seed_registry", "_start_prompt_scheduler",
    "_register_runtime_keepwarm", "register_auto_accept_job",
    "_start_live_workers", "_start_cache_workers",
)


class _WorkerReached(Exception):
    """Raised by a recorded startup worker: lifespan got past validation."""


@pytest.fixture
def startup_recorder(monkeypatch):
    from agents import server
    ran: list[str] = []

    def recorder(name):
        def _run(*_args, **_kwargs):
            ran.append(name)
            raise _WorkerReached(name)
        return _run

    for name in _STARTUP_WORKERS:
        monkeypatch.setattr(server, name, recorder(name))
    monkeypatch.setattr(server.mqtt_subscriber, "start_subscriber",
                        recorder("mqtt_subscriber.start_subscriber"))
    return ran


def _enter_lifespan():
    from agents import server

    async def _enter():
        async with server.lifespan(server.app):
            pass
    asyncio.run(_enter())


def test_app_refuses_to_boot_on_an_invalid_allowlist(allow, startup_recorder):
    """Entering the real FastAPI lifespan with an invalid ALLOWED_MODELS raises
    before ANY startup worker runs."""
    allow(f"{SONNET_5},not.a-catalog-model")
    with pytest.raises(ValueError, match="not.a-catalog-model"):
        _enter_lifespan()
    assert startup_recorder == []


@pytest.mark.parametrize("raw", [None, f"{SONNET_5},{HAIKU}"])
def test_app_boots_past_the_allowlist_check_when_unset_or_valid(allow, startup_recorder, raw):
    """Positive control: with no allowlist or a valid one, lifespan reaches its
    first startup worker (the recorder stops it there)."""
    if raw is not None:
        allow(raw)
    with pytest.raises(_WorkerReached):
        _enter_lifespan()
    assert startup_recorder == ["_restore_graph"]


# -- role-aware allowlist (the workshop pair is not swappable) ------------------

def test_role_families_limit_each_role_to_its_own_allowed_models():
    """An allowlist naming models of a role limits that role to them; a role
    with none falls back to every allowed model (default_model's fallback)."""
    workshop = frozenset({SONNET_5, HAIKU})
    assert mc.role_families(ORCHESTRATOR, workshop) == frozenset({SONNET_5})
    assert mc.role_families(AGENT, workshop) == frozenset({HAIKU})
    assert mc.role_families(ORCHESTRATOR, frozenset({HAIKU, NOVA_PRO})) == \
        frozenset({HAIKU, NOVA_PRO})
    assert mc.role_families(AGENT, None) is None
    assert mc.is_allowed_for(f"us.{SONNET_5}", ORCHESTRATOR, workshop)
    assert not mc.is_allowed_for(f"global.{HAIKU}", ORCHESTRATOR, workshop)
    assert not mc.is_allowed_for(f"global.{SONNET_5}", AGENT, workshop)
    assert mc.is_allowed_for("anything.at-all", AGENT, None)


@pytest.mark.parametrize("raw", [
    f"{SONNET_5},{HAIKU}", f"{HAIKU},{NOVA_PRO}", GPT_SOL,
    f"{SONNET_5},{SONNET_46},{NOVA_PRO}",
])
def test_role_default_is_always_allowed_for_its_role(allow, raw):
    allow(raw)
    allowed = mc.allowed_families()
    for role in (ORCHESTRATOR, AGENT):
        assert mc.is_allowed_for(mc.default_model(role, "us-east-1"), role, allowed)


def test_workshop_catalog_offers_one_model_per_role(allow):
    """Each Admin dropdown has exactly one enabled model: Sonnet 5 for the
    orchestrator, Haiku 4.5 for the system agents."""
    allow(f"{SONNET_5},{HAIKU}")
    cat = mc.catalog("us-east-1")
    assert [e["family"] for e in cat[ORCHESTRATOR] if e["allowed"]] == [SONNET_5]
    assert [e["family"] for e in cat[AGENT] if e["allowed"]] == [HAIKU]


def test_put_refuses_the_workshop_pair_swapped_between_roles(allow, aws_env):
    allow(f"{SONNET_5},{HAIKU}")
    ssm, clients = _FakeSSM(), []
    res = _put({"orchestrator_model": f"global.{HAIKU}", "agent_model": f"global.{SONNET_5}"},
               ssm, clients)
    assert res.status_code == 400
    message = json.loads(res.body)["message"]
    assert "orchestrator_model" in message and "agent_model" in message
    assert ssm.store == {}
    assert clients == []


def test_settings_ignore_saved_models_swapped_between_roles(allow, monkeypatch):
    """A saved pair inside the allowlist but in the wrong roles (saved before a
    workshop redeploy) is not what runs; the pinned env values are."""
    from agents import model_config
    allow(f"{SONNET_5},{HAIKU}")
    monkeypatch.setenv("BEDROCK_MODEL", f"global.{SONNET_5}")
    monkeypatch.setenv("SYSTEM_AGENT_MODEL", f"global.{HAIKU}")
    saved = {"/adt/llm-orchestrator-model": f"global.{HAIKU}",
             "/adt/llm-agent-model": f"global.{SONNET_5}"}
    with patch.object(model_config, "_ssm_value",
                      side_effect=lambda name, region: saved.get(name, "")):
        models = model_config.get_configured_models("us-east-1")
    assert models["orchestrator"] == f"global.{SONNET_5}"
    assert models["agent"] == f"global.{HAIKU}"
