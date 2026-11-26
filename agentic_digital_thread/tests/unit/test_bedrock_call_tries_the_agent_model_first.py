# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``bedrock_call`` tries the deployment's system-agent model first.

Onboarding, the knowledge-graph node analysis and the context interview all
reach Bedrock through ``db.bedrock_call``. Without an explicit ``model`` its
first attempt used to try Amazon Nova Pro, a choice made when the defaults
were Sonnet 4.x and Nova Pro was the cheaper model. Today the configured
system-agent model is cheaper (Claude Haiku 5.5 on a default deployment), the
workshop allowlist does not offer Nova Pro at all, and Nova Pro's 4096-token
output budget cut wide responses short. The first attempt now tries the
model ``model_config.configured_agent_model`` reports (the Admin > Settings
save, else ``SYSTEM_AGENT_MODEL``, else the catalog default), then the
orchestrator model; retries use the orchestrator model; an explicit
``model`` is the only model tried. A GPT-5.x id, which only Bedrock Mantle
serves, runs one tool-free turn there; every other id keeps the Converse
request. The saved model is read on every call, so an Admin save applies to
the next one.

Offline: fake Converse clients, a fake Mantle model in place of
``model_provider.build_model``, an in-memory SSM, no network.
"""
from __future__ import annotations

import importlib.util
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import boto3
import httpx
import openai
import pytest
from botocore.exceptions import ClientError
from strands.models import Model
from strands.types.exceptions import ModelThrottledException

import agents.aws_session as aws_session
import agents.db as db
import agents.model_config as model_config
import agents.model_provider as model_provider
from agents.model_catalog import (AGENT, ORCHESTRATOR, accepts_temperature,
                                  allowed_families, default_model, family_of,
                                  is_allowed_for, resolve_id)

HAIKU_45 = "anthropic.claude-haiku-4-5-20251001-v1:0"
HAIKU_55 = "anthropic.claude-haiku-5-5"
SONNET_55 = "anthropic.claude-sonnet-5-5"
NOVA_PRO = "amazon.nova-pro-v1:0"
GPT_LUNA = "openai.gpt-5.6-luna"
GPT_SOL = "openai.gpt-5.6-sol"
AGENT_PARAM = "/adt/llm-agent-model"
_REPO_ROOT = Path(__file__).resolve().parents[3]
_SSM_VALUE = model_config._ssm_value


def _error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, "Converse")


def _mantle_error(status: int) -> openai.APIStatusError:
    """The error the OpenAI client raises for an HTTP *status* from Mantle."""
    request = httpx.Request("POST", "https://bedrock-mantle.us-east-1.api.aws/openai/v1/responses")
    response = httpx.Response(status, request=request)
    kind = {400: openai.BadRequestError, 429: openai.RateLimitError,
            503: openai.InternalServerError}.get(status, openai.APIStatusError)
    return kind(f"status {status}", response=response, body=None)


class _Converse:
    """Records every Converse request; answers from a per-model script.

    ``script[model_id]`` is a list consumed in order: an exception is raised,
    a string is returned as the reply text. A model with no script answers
    ``"ok:<model_id>"``. A GPT-5.x id is refused the way bedrock-runtime
    refuses it: Converse cannot serve a Mantle-only model."""

    def __init__(self, script: dict | None = None):
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.requests: list[dict] = []

    @property
    def model_ids(self) -> list[str]:
        return [r["modelId"] for r in self.requests]

    def converse(self, **request):
        self.requests.append(request)
        if model_provider.is_mantle_only(request["modelId"]):
            raise _error("ValidationException")
        steps = self.script.get(request["modelId"])
        item = steps.pop(0) if steps else f"ok:{request['modelId']}"
        if isinstance(item, Exception):
            raise item
        return {"output": {"message": {"content": [{"text": item}]}}, "stopReason": "end_turn"}


@pytest.fixture
def bedrock(monkeypatch):
    """A clean env, no saved model in SSM, no sleeping, and a fake client."""
    for key in ("SYSTEM_AGENT_MODEL", "AGENT_MODEL", "BEDROCK_MODEL", "ALLOWED_MODELS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    saved: dict[str, str] = {}
    monkeypatch.setattr(model_config, "_ssm_value", lambda name, region: saved.get(name, ""))
    monkeypatch.setattr("time.sleep", lambda _s: None)
    holder: dict = {"client": _Converse()}
    monkeypatch.setattr(aws_session, "bedrock_runtime_client",
                        lambda *a, **k: holder["client"])

    def use(script: dict | None = None) -> _Converse:
        holder["client"] = _Converse(script)
        return holder["client"]

    use.saved = saved
    return use


class _MantleModel(Model):
    """Stands in for the ``OpenAIResponsesModel`` ``build_model`` returns for a
    GPT-5.x id. Answers from the shared per-model script like ``_Converse``
    (default reply ``"mantle:<model_id>"``), with a reasoning block ahead of
    the text as GPT-5.x replies carry one; a ``("cut", text)`` item stops at
    the output cap."""

    def __init__(self, model_id: str, mantle):
        self.model_id, self.mantle = model_id, mantle

    def update_config(self, **model_config):
        return None

    def get_config(self):
        return {"model_id": self.model_id}

    def structured_output(self, output_model, prompt, system_prompt=None, **kwargs):
        raise NotImplementedError

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        self.mantle.turns.append(SimpleNamespace(model_id=self.model_id, messages=messages,
                                                 tool_specs=tool_specs,
                                                 system_prompt=system_prompt))
        steps = self.mantle.script.get(self.model_id)
        item = steps.pop(0) if steps else f"mantle:{self.model_id}"
        if isinstance(item, Exception):
            raise item
        stop, text = item if isinstance(item, tuple) else ("end_turn", item)
        yield {"messageStart": {"role": "assistant"}}
        yield {"contentBlockDelta": {"delta": {"reasoningContent": {"text": "thinking"}}}}
        yield {"contentBlockStop": {}}
        yield {"contentBlockDelta": {"delta": {"text": text}}}
        yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "max_tokens" if stop == "cut" else stop}}


@pytest.fixture
def mantle(monkeypatch):
    """``model_provider.build_model`` replaced by a recorder that returns a
    ``_MantleModel``; Converse ids must never reach it."""
    state = SimpleNamespace(builds=[], turns=[], script={})

    def build_model(model_id, *, max_tokens, region=None):
        assert model_provider.is_mantle_only(model_id), model_id
        state.builds.append((model_id, max_tokens, region))
        return _MantleModel(model_id, state)

    monkeypatch.setattr(model_provider, "build_model", build_model)
    return state


def _mantle_calls(mantle) -> list[str]:
    return [t.model_id for t in mantle.turns]


class _SSMStore:
    """Parameter Store behind ``boto3.client("ssm")``: counts the clients built
    and the reads made. A client takes *build_delay* seconds to build, which
    widens the window two threads have to build one at the same time."""

    def __init__(self):
        self.values: dict[str, str] = {}
        self.clients = 0
        self.reads = 0
        self.build_delay = 0.0
        self._lock = threading.Lock()

    def client(self, service_name, region_name=None, config=None, **_kwargs):
        if service_name != "ssm":
            raise RuntimeError(f"no {service_name} client in this test")
        threading.Event().wait(self.build_delay)
        with self._lock:
            self.clients += 1
        return self

    def get_parameter(self, Name):
        with self._lock:
            self.reads += 1
        if Name not in self.values:
            raise RuntimeError(f"ParameterNotFound: {Name}")
        return {"Parameter": {"Value": self.values[Name]}}

    def put_parameter(self, Name, Value, Type="String", Overwrite=True):
        self.values[Name] = Value
        return {}


@pytest.fixture
def ssm_store(bedrock, monkeypatch):
    """The real ``model_config._ssm_value`` over an in-memory SSM (the
    ``bedrock`` fixture's stub is put back to the real reader)."""
    store = _SSMStore()
    monkeypatch.setattr(model_config, "_ssm_value", _SSM_VALUE)
    monkeypatch.setattr(boto3, "client", store.client)
    return store


def _workshop_models():
    spec = importlib.util.spec_from_file_location(
        "adt_test_workshop_models_bedrock_call", _REPO_ROOT / "stacks" / "_workshop_models.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------- #
# the first model tried
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("region", ["us-east-1", "eu-west-1"])
@pytest.mark.parametrize("agent_family", [HAIKU_45, HAIKU_55])
def test_first_model_tried_is_the_configured_agent_model(bedrock, monkeypatch, region,
                                                         agent_family):
    monkeypatch.setenv("AWS_REGION", region)
    agent_id = resolve_id(agent_family, region)
    monkeypatch.setenv("SYSTEM_AGENT_MODEL", agent_id)
    monkeypatch.setenv("BEDROCK_MODEL", resolve_id(SONNET_55, region))
    client = bedrock()
    assert db.bedrock_call("P") == f"ok:{agent_id}"
    assert client.model_ids == [agent_id]


@pytest.mark.parametrize("region", ["us-east-1", "eu-west-1"])
def test_without_any_model_env_the_first_try_is_the_catalog_agent_default(bedrock, monkeypatch,
                                                                          region):
    monkeypatch.setenv("AWS_REGION", region)
    client = bedrock({default_model(AGENT, region): [_error("ValidationException")]})
    db.bedrock_call("P")
    assert client.model_ids == [default_model(AGENT, region), default_model(ORCHESTRATOR, region)]


def test_an_admin_saved_agent_model_is_tried_first(bedrock, monkeypatch):
    monkeypatch.setenv("SYSTEM_AGENT_MODEL", resolve_id(HAIKU_55))
    bedrock.saved["/adt/llm-agent-model"] = resolve_id(HAIKU_45)
    client = bedrock()
    db.bedrock_call("P")
    assert client.model_ids == [resolve_id(HAIKU_45)]


# --------------------------------------------------------------------------- #
# fallback and retries
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("failure", [_error("ValidationException"),
                                     _error("AccessDeniedException"),
                                     TimeoutError("read timed out")])
def test_an_agent_model_error_falls_back_to_the_orchestrator_model(bedrock, monkeypatch, failure):
    agent_id, orchestrator_id = resolve_id(HAIKU_55), resolve_id(SONNET_55)
    monkeypatch.setenv("SYSTEM_AGENT_MODEL", agent_id)
    monkeypatch.setenv("BEDROCK_MODEL", orchestrator_id)
    client = bedrock({agent_id: [failure]})
    assert db.bedrock_call("P") == f"ok:{orchestrator_id}"
    assert client.model_ids == [agent_id, orchestrator_id]


def test_a_throttled_first_try_retries_on_the_orchestrator_model_only(bedrock, monkeypatch):
    agent_id, orchestrator_id = resolve_id(HAIKU_55), resolve_id(SONNET_55)
    monkeypatch.setenv("SYSTEM_AGENT_MODEL", agent_id)
    monkeypatch.setenv("BEDROCK_MODEL", orchestrator_id)
    client = bedrock({agent_id: [_error("ThrottlingException")],
                      orchestrator_id: [_error("ServiceUnavailableException")]})
    assert db.bedrock_call("P") == f"ok:{orchestrator_id}"
    assert client.model_ids == [agent_id, orchestrator_id, orchestrator_id]


def test_an_orchestrator_error_after_the_fallback_is_raised(bedrock, monkeypatch):
    agent_id, orchestrator_id = resolve_id(HAIKU_55), resolve_id(SONNET_55)
    monkeypatch.setenv("SYSTEM_AGENT_MODEL", agent_id)
    monkeypatch.setenv("BEDROCK_MODEL", orchestrator_id)
    client = bedrock({agent_id: [_error("ValidationException")],
                      orchestrator_id: [_error("ValidationException")]})
    with pytest.raises(ClientError):
        db.bedrock_call("P")
    assert client.model_ids == [agent_id, orchestrator_id]


def test_an_agent_model_equal_to_the_orchestrator_model_is_tried_once(bedrock, monkeypatch):
    same = resolve_id(SONNET_55)
    monkeypatch.setenv("SYSTEM_AGENT_MODEL", same)
    monkeypatch.setenv("BEDROCK_MODEL", same)
    client = bedrock({same: [_error("ValidationException")]})
    with pytest.raises(ClientError):
        db.bedrock_call("P")
    assert client.model_ids == [same]


@pytest.mark.parametrize("forced_family", [SONNET_55, NOVA_PRO])
def test_an_explicit_model_is_the_only_model_tried(bedrock, monkeypatch, forced_family):
    forced = resolve_id(forced_family, "us-east-1")
    monkeypatch.setenv("SYSTEM_AGENT_MODEL", resolve_id(HAIKU_55))
    client = bedrock()
    assert db.bedrock_call("P", model=forced) == f"ok:{forced}"
    client = bedrock({forced: [_error("ValidationException")]})
    with pytest.raises(ClientError):
        db.bedrock_call("P", model=forced)
    assert client.model_ids == [forced]


def test_an_explicit_mantle_model_is_the_only_model_tried(bedrock, mantle):
    client = bedrock()
    assert db.bedrock_call("P", model=GPT_SOL) == f"mantle:{GPT_SOL}"
    assert _mantle_calls(mantle) == [GPT_SOL]
    assert client.model_ids == []


# --------------------------------------------------------------------------- #
# GPT-5.x on Bedrock Mantle
# --------------------------------------------------------------------------- #

def test_a_mantle_agent_model_answers_through_mantle(bedrock, mantle, monkeypatch):
    """Agent GPT-5.6 Luna, orchestrator Claude: the first try is Luna, built
    by ``build_model`` with the 8192-token cap and run as one tool-free turn on
    the prompt; the reply is its text, without the reasoning block."""
    monkeypatch.setenv("SYSTEM_AGENT_MODEL", GPT_LUNA)
    monkeypatch.setenv("BEDROCK_MODEL", resolve_id(SONNET_55))
    client = bedrock()
    mantle.script[GPT_LUNA] = ['{"mapped": 3}']
    assert db.bedrock_call("map these") == '{"mapped": 3}'
    assert mantle.builds == [(GPT_LUNA, 8192, "us-east-1")]
    (turn,) = mantle.turns
    assert turn.messages == [{"role": "user", "content": [{"text": "map these"}]}]
    assert not turn.tool_specs and not turn.system_prompt
    assert client.model_ids == []


def test_a_failed_mantle_agent_model_falls_back_to_a_converse_orchestrator(bedrock, mantle,
                                                                          monkeypatch):
    orchestrator_id = resolve_id(SONNET_55)
    monkeypatch.setenv("SYSTEM_AGENT_MODEL", GPT_LUNA)
    monkeypatch.setenv("BEDROCK_MODEL", orchestrator_id)
    client = bedrock()
    mantle.script[GPT_LUNA] = [_mantle_error(400)]
    assert db.bedrock_call("P") == f"ok:{orchestrator_id}"
    assert _mantle_calls(mantle) == [GPT_LUNA]
    assert client.model_ids == [orchestrator_id]


@pytest.mark.parametrize("which", ["agent", "explicit"])
def test_a_mantle_model_that_cannot_be_built_raises_and_is_never_substituted(bedrock, monkeypatch,
                                                                            which):
    """A model that cannot be constructed (no token, a missing dependency)
    raises its own error: no other model answers in its place (ADR-0055 (d)).
    Only a failure of a model that was built falls back."""
    orchestrator_id = resolve_id(SONNET_55)
    monkeypatch.setenv("SYSTEM_AGENT_MODEL", GPT_LUNA)
    monkeypatch.setenv("BEDROCK_MODEL", orchestrator_id)
    client = bedrock()

    def build_model(model_id, *, max_tokens, region=None):
        raise RuntimeError("token mint failed")

    monkeypatch.setattr(model_provider, "build_model", build_model)
    with pytest.raises(db.ModelConstructionError, match="token mint failed") as raised:
        db.bedrock_call("P", model=GPT_LUNA if which == "explicit" else None)
    assert GPT_LUNA in str(raised.value)
    assert isinstance(raised.value.__cause__, RuntimeError)
    assert client.model_ids == []


def test_with_both_models_on_mantle_the_fallback_runs_on_mantle(bedrock, mantle, monkeypatch):
    """Luna as the agent model and Sol as the orchestrator model, a pair the
    Admin page offers: neither call goes to Converse."""
    monkeypatch.setenv("SYSTEM_AGENT_MODEL", GPT_LUNA)
    monkeypatch.setenv("BEDROCK_MODEL", GPT_SOL)
    client = bedrock()
    assert db.bedrock_call("P") == f"mantle:{GPT_LUNA}"
    mantle.script[GPT_LUNA] = [_mantle_error(400)]
    assert db.bedrock_call("P") == f"mantle:{GPT_SOL}"
    assert _mantle_calls(mantle) == [GPT_LUNA, GPT_LUNA, GPT_SOL]
    assert client.model_ids == []


def test_a_claude_agent_model_falls_back_to_a_mantle_orchestrator(bedrock, mantle, monkeypatch):
    agent_id = resolve_id(HAIKU_55)
    monkeypatch.setenv("SYSTEM_AGENT_MODEL", agent_id)
    monkeypatch.setenv("BEDROCK_MODEL", GPT_SOL)
    client = bedrock({agent_id: [_error("ValidationException")]})
    assert db.bedrock_call("P") == f"mantle:{GPT_SOL}"
    assert client.model_ids == [agent_id]
    assert _mantle_calls(mantle) == [GPT_SOL]


@pytest.mark.parametrize("transient", [
    ModelThrottledException("rate limited"), _mantle_error(429), _mantle_error(503),
    _mantle_error(408),
], ids=["strands-throttle", "429", "503", "408"])
def test_a_transient_mantle_error_retries_on_the_orchestrator_model(bedrock, mantle, monkeypatch,
                                                                    transient):
    """A Mantle throttle (429, which strands raises as ModelThrottledException),
    a 5xx or a 408 model timeout follows the Converse rule for Throttling,
    ServiceUnavailable and ModelTimeout: back off, then retry on the
    orchestrator model only."""
    sleeps: list[float] = []
    monkeypatch.setattr("time.sleep", sleeps.append)
    monkeypatch.setenv("SYSTEM_AGENT_MODEL", GPT_LUNA)
    monkeypatch.setenv("BEDROCK_MODEL", GPT_SOL)
    bedrock()
    mantle.script[GPT_LUNA] = [transient]
    mantle.script[GPT_SOL] = [transient]
    assert db.bedrock_call("P") == f"mantle:{GPT_SOL}"
    assert _mantle_calls(mantle) == [GPT_LUNA, GPT_SOL, GPT_SOL]
    assert sleeps == [1, 2]


def test_a_mantle_orchestrator_error_after_the_fallback_is_raised(bedrock, mantle, monkeypatch):
    monkeypatch.setenv("SYSTEM_AGENT_MODEL", GPT_LUNA)
    monkeypatch.setenv("BEDROCK_MODEL", GPT_SOL)
    bedrock()
    mantle.script[GPT_LUNA] = [_mantle_error(400)]
    mantle.script[GPT_SOL] = [_mantle_error(400)]
    with pytest.raises(openai.BadRequestError):
        db.bedrock_call("P")
    assert _mantle_calls(mantle) == [GPT_LUNA, GPT_SOL]


def test_a_mantle_reply_cut_at_its_output_cap_is_a_failed_try(bedrock, mantle, monkeypatch):
    """strands raises on a reply stopped at the cap rather than return half
    of it, so the first try falls back to the orchestrator model."""
    orchestrator_id = resolve_id(SONNET_55)
    monkeypatch.setenv("SYSTEM_AGENT_MODEL", GPT_LUNA)
    monkeypatch.setenv("BEDROCK_MODEL", orchestrator_id)
    client = bedrock()
    mantle.script[GPT_LUNA] = [("cut", '[{"field": ')]
    assert db.bedrock_call("P") == f"ok:{orchestrator_id}"
    assert _mantle_calls(mantle) == [GPT_LUNA]
    assert client.model_ids == [orchestrator_id]


# --------------------------------------------------------------------------- #
# the workshop allowlist
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("pinned", [True, False])
def test_under_the_workshop_allowlist_only_allowlisted_models_are_called(bedrock, monkeypatch,
                                                                       pinned):
    """The workshop allows exactly Sonnet 5 and Haiku 4.5. Whether the runtime
    carries the pinned model env or falls back to the catalog default, and
    even with a disallowed model saved in SSM, every call stays inside it."""
    ws = _workshop_models()
    monkeypatch.setenv("ALLOWED_MODELS", ws.WORKSHOP_ALLOWED_MODELS)
    if pinned:
        monkeypatch.setenv("SYSTEM_AGENT_MODEL", ws.WORKSHOP_SYSTEM_AGENT_MODEL)
        monkeypatch.setenv("BEDROCK_MODEL", ws.WORKSHOP_BEDROCK_MODEL)
    bedrock.saved["/adt/llm-agent-model"] = resolve_id(NOVA_PRO)
    agent_id = default_model(AGENT)
    client = bedrock({agent_id: [_error("ValidationException")]})
    db.bedrock_call("P")
    allowed = allowed_families()
    assert family_of(client.model_ids[0]) == ws.WORKSHOP_SYSTEM_AGENT_FAMILY
    assert family_of(client.model_ids[1]) == ws.WORKSHOP_BEDROCK_FAMILY
    roles = (AGENT, ORCHESTRATOR)
    assert all(is_allowed_for(m, role, allowed) for m, role in zip(client.model_ids, roles))


# --------------------------------------------------------------------------- #
# request shape: output budget and temperature
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("agent_family", [HAIKU_45, HAIKU_55])
def test_the_agent_model_request_keeps_the_full_output_budget(bedrock, monkeypatch, agent_family):
    """No site passes ``max_tokens``: the agent model gets the catalog-family
    budget, never less than the 4096 tokens the first try had before, and a
    model that rejects a temperature gets none."""
    agent_id = resolve_id(agent_family)
    monkeypatch.setenv("SYSTEM_AGENT_MODEL", agent_id)
    client = bedrock()
    db.bedrock_call("P")
    config = client.requests[0]["inferenceConfig"]
    assert config["maxTokens"] == db._CONVERSE_MAX_TOKENS["default"]
    assert config["maxTokens"] > db._CONVERSE_MAX_TOKENS["nova"]
    assert ("temperature" in config) is accepts_temperature(agent_id)


# --------------------------------------------------------------------------- #
# a model swap applies to the next call
# --------------------------------------------------------------------------- #

def test_an_admin_save_applies_to_the_next_call_in_the_same_process(ssm_store, bedrock,
                                                                   monkeypatch):
    """The Settings save (``admin_routes._apply_llm_settings``) writes SSM; the
    very next ``bedrock_call`` in the process that saved runs the new model
    (ADR-0055: both roles swap live)."""
    from agents import admin_routes
    monkeypatch.setattr(admin_routes, "_refresh_agent_model_fleet",
                        lambda _model: {"ok": True})
    ssm_store.values[AGENT_PARAM] = resolve_id(HAIKU_45)
    client = bedrock()
    db.bedrock_call("P")
    saved = admin_routes._apply_llm_settings("", resolve_id(HAIKU_55))
    assert saved["status"] == "ok", saved
    db.bedrock_call("P")
    assert client.model_ids == [resolve_id(HAIKU_45), resolve_id(HAIKU_55)]


def test_every_call_reads_the_saved_model_through_one_ssm_client(ssm_store, bedrock):
    ssm_store.values[AGENT_PARAM] = resolve_id(HAIKU_45)
    client = bedrock()
    for _ in range(3):
        db.bedrock_call("P")
    assert (ssm_store.reads, ssm_store.clients) == (3, 1)
    assert client.model_ids == [resolve_id(HAIKU_45)] * 3


def test_concurrent_first_calls_share_one_ssm_client_and_cache_no_model(ssm_store, bedrock):
    """Eight threads make the process's first calls together: none fails, one
    SSM client is built, each runs the saved model, and a later save still
    applies to the next call."""
    ssm_store.values[AGENT_PARAM] = resolve_id(HAIKU_45)
    ssm_store.build_delay = 0.05
    client = bedrock()
    workers = 8
    start = threading.Barrier(workers)

    def first_call(_index):
        start.wait()
        return db.bedrock_call("P")

    with ThreadPoolExecutor(workers) as pool:
        replies = list(pool.map(first_call, range(workers)))
    assert replies == [f"ok:{resolve_id(HAIKU_45)}"] * workers
    assert ssm_store.clients == 1
    ssm_store.values[AGENT_PARAM] = resolve_id(HAIKU_55)
    assert db.bedrock_call("P") == f"ok:{resolve_id(HAIKU_55)}"
    assert len(client.model_ids) == workers + 1


def test_a_changed_agent_model_env_applies_at_once(bedrock, monkeypatch):
    client = bedrock()
    monkeypatch.setenv("SYSTEM_AGENT_MODEL", resolve_id(HAIKU_45))
    db.bedrock_call("P")
    monkeypatch.setenv("SYSTEM_AGENT_MODEL", resolve_id(HAIKU_55))
    db.bedrock_call("P")
    assert client.model_ids == [resolve_id(HAIKU_45), resolve_id(HAIKU_55)]
