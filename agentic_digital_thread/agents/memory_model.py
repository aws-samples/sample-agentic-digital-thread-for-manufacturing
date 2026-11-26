# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The AgentCore Memory extraction model, read and switched in place.

The memory's three custom strategies (``stacks/agentcore_stack.py``: facts,
summaries, preferences) each name the model that turns conversations into
long-term records, on every extraction and consolidation override. The Admin
LLM settings page shows that model and, on a base or demo deploy, changes it:

* read: every override ``modelId`` from GetMemory (``mixed`` when they
  differ) and the memory's status;
* switch: ONE UpdateMemory whose ``modifyMemoryStrategies`` is built from the
  CURRENT GetMemory configuration with only each ``modelId`` replaced
  (``appendToPrompt`` and everything else unchanged), then GetMemory polled
  (bounded) until the memory and every strategy are ACTIVE on the new model.
  Nothing is sent when the memory is already settled on the model; a strategy
  that is not a custom override with model ids is refused before any call,
  even when the ids it does show match; a FAILED memory or strategy is
  refused as ``failed`` and one still changing as ``busy``;
* save (:func:`run_save`, the body of the background switch job in
  ``agents/memory_model_job.py``): one at a time across every app task (the
  JobStore slot :data:`SAVE_SLOT_KEY`, heartbeated while the save waits and
  right before UpdateMemory and the SSM write, and released with the
  switch's outcome);
  the switch first, then SSM ``/adt/llm-memory-model``, which deploy.sh
  passes back as the ``memory_extraction_model`` context, so SSM never names
  a model the memory does not run; then one more read, which reports (and
  never repairs) a memory another writer moved in the meantime.

The settings view also says when the next deploy would not keep the model
the memory runs: a saved model that differs, or nothing valid saved while
the memory runs another model than the stack default.

The memory is the one the app uses at runtime (``config``'s ``memory_id``).
A request never loads the app's config: the settings view reads it only once
it is loaded (:func:`loaded_memory_id`; before that it says so and starts one
background load), and the switch request checks only the model, the
allowlist and the workshop; the worker resolves the memory (:func:`run_save`,
which may run the config's discovery) and ends with an error when there is
none. Without a memory (local or disabled memory) nothing here is
selectable. The workshop pins its model (``stacks/_workshop_models.py``), so
its page shows it read-only. All functions are synchronous; routes offload
them.

What the time bounds cover, and what they do not. Every AWS call here goes
through a client with a connect timeout, a read timeout and a number of
attempts (:func:`store_client_config`, :func:`control_client`); the call,
route and job bounds below (``STORE_CALL_SECONDS``, ``CONTROL_CALL_SECONDS``,
``JOB_MAX_SECONDS``, ``JOB_SILENCE_MAX_SECONDS``, and the routes'
``POST_MAX_SECONDS`` and ``STATUS_MAX_SECONDS`` in ``memory_model_job``) add
those up, so they hold when the time goes to connecting, waiting for an
answer, and retrying. They are not wall-clock limits: botocore bounds
neither DNS resolution, nor a refresh of the task's credentials (or a wait
for another thread's refresh), nor an answer that keeps arriving in small
parts, and neither does the config's discovery when the worker runs it on a
config that was never loaded. Every AWS call the app makes shares these
gaps (chat, onboarding, every admin route); nothing here adds a deadline
around SDK calls. What protects the switch is ownership: the worker shows it
still holds the save slot (a strict heartbeat) immediately before
UpdateMemory and again before the SSM write, so a worker that lost the slot
never starts either. The window left is a mutating call that stalls, after
its ownership check, for longer than the slot's stale bound
(:data:`SAVE_SLOT_STALE_SECONDS`, 900 s): another switch can then take the
slot over and run while the stalled call is still in flight, and that
call's late result is neither undone nor reported.
"""
from __future__ import annotations

import copy
import logging
import threading
import time
from dataclasses import dataclass

from . import model_catalog
from .public_errors import public_error_text

logger = logging.getLogger(__name__)

#: Where a saved model lives for the next deploy (deploy.sh reads it).
SSM_PARAMETER = "/adt/llm-memory-model"
#: ``memory_model`` when the strategies run different models.
MIXED = "mixed"
#: How long a switch waits for the memory to settle on the new model.
#: AgentCore took about 131 s to settle a switch on adt-demo (2026-10-27,
#: us-east-1: the memory and its three strategies ACTIVE on the new model),
#: longer than an origin request may last (120 s), which is why the switch
#: is a background job; the wait allows several times that.
POLL_TIMEOUT_SECONDS = 600.0
POLL_INTERVAL_SECONDS = 5.0
#: The AgentCore control plane and SSM are reached through clients that
#: connect within this long, wait this long for each read of an answer, and
#: make this many attempts in all (one retry, botocore standard mode;
#: ``total_max_attempts``, since botocore reads a bare ``max_attempts`` as
#: retries and makes one attempt more).
CONTROL_CONNECT_TIMEOUT_SECONDS = 3.0
CONTROL_READ_TIMEOUT_SECONDS = 10.0
CONTROL_MAX_ATTEMPTS = 2
#: The app's shared DynamoDB table (the save slot and the switch job records)
#: is reached through a client that connects within this long, waits this
#: long for each read, and makes this many attempts in all. A table in the
#: app's own Region answers a one-item read or write in milliseconds; a 1 s
#: connect keeps one call within :data:`STORE_CALL_SECONDS`, so a switch
#: route's two calls stay under 30 s (``memory_model_job``).
STORE_CONNECT_TIMEOUT_SECONDS = 1.0
STORE_READ_TIMEOUT_SECONDS = 3.0
STORE_MAX_ATTEMPTS = 2
#: The longest botocore standard mode waits before a second attempt: its
#: jittered backoff (at most 1 s, a throttling error's scale) and, on its
#: newer retry path (behind botocore's ``AWS_NEW_RETRIES_2026`` gate in
#: 1.43), up to 5 s more when the service asks for it (``x-amz-retry-after``).
RETRY_WAIT_SECONDS = 1.0 + 5.0


def _call_seconds(connect: float, read: float, attempts: int) -> float:
    """The longest one call takes on a client with these bounds, when its
    time goes to connecting, waiting for each read and retrying (see the
    module docstring for what that leaves out)."""
    return attempts * (connect + read) + (attempts - 1) * RETRY_WAIT_SECONDS


#: One AgentCore or SSM call (:func:`control_client`, :func:`_ssm_client`).
CONTROL_CALL_SECONDS = _call_seconds(CONTROL_CONNECT_TIMEOUT_SECONDS,
                                     CONTROL_READ_TIMEOUT_SECONDS, CONTROL_MAX_ATTEMPTS)
#: One call to the shared table (:func:`store_client_config`).
STORE_CALL_SECONDS = _call_seconds(STORE_CONNECT_TIMEOUT_SECONDS,
                                   STORE_READ_TIMEOUT_SECONDS, STORE_MAX_ATTEMPTS)
#: The calls a switch job makes outside its wait: GetMemory and UpdateMemory
#: before it, the read that ends it, the SSM write and the read after it ...
JOB_CONTROL_CALLS = 5
#: ... and the claim (the POST's, which the job's ``started_at`` precedes),
#: its job record, the ownership check right before UpdateMemory, the last
#: heartbeat inside the wait, the final heartbeat (the ownership check right
#: before the SSM write), the terminal record and the slot's release.
JOB_STORE_CALLS = 7
#: The longest a switch job runs, from its ``started_at`` (read just before
#: its claim): the wait, one more poll interval, and every call it makes
#: outside the wait, the claim included. Below
#: :data:`SAVE_SLOT_STALE_SECONDS`, under the failure modes the client bounds
#: cover (the module docstring); it is not a wall-clock limit.
JOB_MAX_SECONDS = (POLL_TIMEOUT_SECONDS + POLL_INTERVAL_SECONDS
                   + JOB_CONTROL_CALLS * CONTROL_CALL_SECONDS
                   + JOB_STORE_CALLS * STORE_CALL_SECONDS)
#: The longest a running switch job goes between two writes of its slot (the
#: claim, a heartbeat, the release), under the same failure modes: from the
#: final heartbeat to the release (that heartbeat, the SSM write, the read
#: after it, the terminal record and the release). Longer than from the claim
#: to the ownership check before UpdateMemory (the claim, the job record,
#: GetMemory and that check), from that check to the first heartbeat in the
#: wait (the check, UpdateMemory, the first poll and that heartbeat), and
#: between two heartbeats (one, a poll interval, a read and the next).
JOB_SILENCE_MAX_SECONDS = 3 * STORE_CALL_SECONDS + 2 * CONTROL_CALL_SECONDS
# Seams for the wait (tests drive a fake clock); looked up at each call.
_sleep = time.sleep
_clock = time.monotonic

_ACTIVE = "ACTIVE"
_FAILED = "FAILED"
#: The configuration parts an override sits in, with their union key. The
#: GetMemory and UpdateMemory shapes use the same names.
_PARTS = (("extraction", "customExtractionConfiguration"),
          ("consolidation", "customConsolidationConfiguration"))
#: Configuration keys a modify may leave out without changing them.
_UNSENT_KEYS = frozenset({"type"})

#: The ONE slot a save holds (``JobStore.claim_slot``, a DynamoDB conditional
#: write on a deployed app, so every app task shares it; a locked
#: check-then-set in one process without a table): two saves interleaving
#: could leave SSM naming one model while the memory runs the other. A save
#: holds it across the update, the wait, the SSM write and the read after it,
#: and a second save is refused while it is held. It also covers two saves in
#: one process, so there is no process lock beside it. Not the runtime-config
#: lock: that one is never held across a wait
#: (runtime_provision_helpers.RUNTIME_CONFIG_LOCK), and no runtime is read or
#: written here.
SAVE_SLOT_KEY = "memory-model-save"
#: A claim older than this is taken over. Above the longest switch job
#: (:data:`JOB_MAX_SECONDS`); the job also heartbeats its claim on every poll,
#: so a claim is only this old once its job has written nothing for this
#: long.
SAVE_SLOT_STALE_SECONDS = 900.0
#: ``reason`` of a save refused because another holds the slot.
SAVE_IN_PROGRESS = "save_in_progress"
#: ``reason`` of a save refused because the shared slot store could not
#: decide (a backend error, or a backend the fail-soft path disabled).
SAVE_NOT_COORDINATED = "save_not_coordinated"
CLAIM_REFUSED = {
    SAVE_IN_PROGRESS: ("Another memory extraction model save is in progress; try again when "
                       "it finishes. Nothing was changed."),
    SAVE_NOT_COORDINATED: ("The memory extraction model save could not be coordinated with the "
                           "other app tasks (the shared store did not answer); try again. "
                           "Nothing was changed."),
}
#: The HTTP status of each refusal.
CLAIM_REFUSED_STATUS = {SAVE_IN_PROGRESS: 409, SAVE_NOT_COORDINATED: 503}
_SAVE_SLOTS = None  # lazy JobStore("memory-model"); tests inject an in-memory one


def _save_slots():
    """The shared save-slot store (lazy, so the module imports without boto3),
    on the bounded store client (:func:`store_client_config`)."""
    global _SAVE_SLOTS
    if _SAVE_SLOTS is None:
        from .job_store import JobStore
        _SAVE_SLOTS = JobStore("memory-model", client_config=store_client_config())
    return _SAVE_SLOTS


def store_client_config():
    """The botocore config of the shared table's client for the save slot
    and the switch job records (:data:`STORE_CALL_SECONDS`)."""
    from botocore.config import Config
    return Config(connect_timeout=STORE_CONNECT_TIMEOUT_SECONDS,
                  read_timeout=STORE_READ_TIMEOUT_SECONDS,
                  retries={"total_max_attempts": STORE_MAX_ATTEMPTS, "mode": "standard"})


def _control_config():
    """The botocore config of the AgentCore and SSM clients
    (:data:`CONTROL_CALL_SECONDS`)."""
    from botocore.config import Config
    return Config(connect_timeout=CONTROL_CONNECT_TIMEOUT_SECONDS,
                  read_timeout=CONTROL_READ_TIMEOUT_SECONDS,
                  retries={"total_max_attempts": CONTROL_MAX_ATTEMPTS, "mode": "standard"})


class MemoryModelError(Exception):
    """A switch that was refused or did not finish. The message is fixed text
    plus model ids, strategy names and AgentCore status words read from
    GetMemory (``agents.public_errors.SAFE_MESSAGE_TYPES``).

    ``code``: ``unsupported``, ``busy``, ``timeout``, ``mismatch``,
    ``failed`` or ``slot_lost`` (the save could not show it still held its
    slot, right before UpdateMemory or while it waited). ``update_sent``:
    UpdateMemory was called."""

    def __init__(self, message: str, *, code: str, update_sent: bool = False):
        super().__init__(message)
        self.code = code
        self.update_sent = update_sent


@dataclass(frozen=True)
class MemoryModelState:
    """What GetMemory says about the extraction model."""

    status: str
    model_ids: tuple[str, ...]                 # every override modelId, in order
    strategies: tuple[tuple[str, str], ...]    # (strategy name, status)

    @property
    def model_id(self) -> str:
        """The one model, :data:`MIXED` when they differ, ``""`` for none."""
        distinct = set(self.model_ids)
        if len(distinct) > 1:
            return MIXED
        return next(iter(distinct), "")

    def runs(self, model_id: str) -> bool:
        return bool(self.model_ids) and all(m == model_id for m in self.model_ids)

    def settled_on(self, model_id: str) -> bool:
        return self.settled() and self.runs(model_id)

    def failed(self) -> bool:
        return self.status == _FAILED or any(s == _FAILED for _, s in self.strategies)

    def settled(self) -> bool:
        """The memory and every strategy are ACTIVE."""
        return self.status == _ACTIVE and all(s == _ACTIVE for _, s in self.strategies)

    def describe(self) -> str:
        strategies = ", ".join(f"{name} {status}" for name, status in self.strategies)
        models = ", ".join(sorted(set(self.model_ids))) or "none"
        return (f"memory {self.status or 'UNKNOWN'}; strategies "
                f"{strategies or 'none'}; models {models}")


def _name(strategy: dict) -> str:
    return str(strategy.get("name") or strategy.get("strategyId") or "unnamed")


def _overrides(strategy: dict):
    """``(part, union key, override key, override)`` for each override."""
    configuration = strategy.get("configuration") or {}
    for part, custom in _PARTS:
        union = (configuration.get(part) or {}).get(custom) or {}
        for key, override in union.items():
            yield part, custom, key, override or {}


def _state(memory: dict) -> MemoryModelState:
    strategies = memory.get("strategies") or []
    ids = tuple(str(o["modelId"]) for s in strategies for *_, o in _overrides(s)
                if o.get("modelId"))
    return MemoryModelState(
        status=str(memory.get("status") or ""), model_ids=ids,
        strategies=tuple((_name(s), str(s.get("status") or "")) for s in strategies))


def _get_memory(client, memory_id: str) -> dict:
    return client.get_memory(memoryId=memory_id)["memory"]


def read_state(client, memory_id: str) -> MemoryModelState:
    """The memory's extraction model and status (one GetMemory)."""
    return _state(_get_memory(client, memory_id))


def _unsupported(name: str, why: str) -> MemoryModelError:
    return MemoryModelError(
        f"the memory's {name} strategy {why}, so its model cannot be switched here.",
        code="unsupported")


def _modify_one(strategy: dict, model_id: str) -> dict:
    name = _name(strategy)
    if strategy.get("type") != "CUSTOM" or not strategy.get("strategyId"):
        raise _unsupported(name, "is not a custom strategy")
    configuration = strategy.get("configuration") or {}
    sent = _UNSENT_KEYS | {part for part, _ in _PARTS}
    extra = sorted(k for k, v in configuration.items() if v and k not in sent)
    if extra:
        raise _unsupported(name, "has settings this update cannot carry back ("
                           + ", ".join(extra) + ")")
    out: dict = {}
    for part, custom, key, override in _overrides(strategy):
        if not override.get("modelId") or "appendToPrompt" not in override:
            raise _unsupported(name, f"has an override without a model id or prompt ({key})")
        out.setdefault(part, {}).setdefault(custom, {})[key] = {
            **copy.deepcopy(override), "modelId": model_id}
    if not out:
        raise _unsupported(name, "has no override with a model id")
    return {"memoryStrategyId": strategy["strategyId"], "configuration": out}


def modify_strategies(memory: dict, model_id: str) -> list[dict]:
    """UpdateMemory's ``modifyMemoryStrategies`` for a GetMemory ``memory``:
    its own configuration with each override's ``modelId`` set to
    *model_id*. Raises :class:`MemoryModelError` (``unsupported``) when any
    strategy is not a custom override with model ids."""
    strategies = memory.get("strategies") or []
    if not strategies:
        raise MemoryModelError("the memory has no strategies to switch.", code="unsupported")
    return [_modify_one(strategy, model_id) for strategy in strategies]


def _not_settled(state: MemoryModelState, model_id: str, timeout: float) -> MemoryModelError:
    if state.status == _ACTIVE and not state.runs(model_id):
        return MemoryModelError(
            f"UpdateMemory was sent, but after {timeout:g} s the memory still reads "
            f"{state.describe()}, not {model_id}.", code="mismatch", update_sent=True)
    return MemoryModelError(
        f"UpdateMemory was sent, but the memory did not settle on {model_id} within "
        f"{timeout:g} s; last read: {state.describe()}.", code="timeout", update_sent=True)


def _wait_until_settled(client, memory_id: str, model_id: str, timeout: float,
                        interval: float, sleep, clock, heartbeat) -> MemoryModelState:
    deadline = clock() + timeout
    while True:
        state = read_state(client, memory_id)
        if state.settled_on(model_id):
            return state
        if state.failed():
            raise MemoryModelError(
                f"UpdateMemory was sent, but the memory reports a failure: {state.describe()}.",
                code="failed", update_sent=True)
        if clock() >= deadline:
            raise _not_settled(state, model_id, timeout)
        if heartbeat is not None and not heartbeat():
            raise MemoryModelError(
                f"UpdateMemory was sent to switch the memory to {model_id}, but this save "
                "could no longer show it holds the save slot while it waited (another save "
                "took over, or the shared store could not be reached), so it stopped; last "
                f"read: {state.describe()}.", code="slot_lost", update_sent=True)
        sleep(interval)


def _refuse_unsettled(state: MemoryModelState) -> None:
    """A memory or strategy that is not ACTIVE is never switched (and never
    "unchanged"): FAILED is ``failed``, anything else still changing ``busy``."""
    if state.failed():
        raise MemoryModelError(
            f"the memory reports a failure ({state.describe()}); check it in AgentCore "
            "before saving again.", code="failed")
    if not state.settled():
        raise MemoryModelError(f"the memory is busy ({state.describe()}); try again when it "
                               "and every strategy are ACTIVE.", code="busy")


def set_model(client, memory_id: str, model_id: str, *, timeout: float | None = None,
              interval: float | None = None, sleep=None, clock=None,
              heartbeat=None) -> bool:
    """Switch every strategy of the memory to *model_id* and wait until the
    memory and every strategy are ACTIVE on it. Every strategy is checked
    first (``unsupported``), then the status (``failed``, ``busy``); False
    (nothing sent) only when the memory is settled on *model_id*. Raises
    :class:`MemoryModelError` on a refusal, a failure, a timeout or a model
    that did not take. *heartbeat* (no arguments) runs right before
    UpdateMemory, which is not sent when it fails (``slot_lost``, nothing
    sent), and on every poll."""
    timeout = POLL_TIMEOUT_SECONDS if timeout is None else timeout
    interval = POLL_INTERVAL_SECONDS if interval is None else interval
    sleep = _sleep if sleep is None else sleep
    clock = _clock if clock is None else clock
    memory = _get_memory(client, memory_id)
    state = _state(memory)
    modify = modify_strategies(memory, model_id)
    if state.settled_on(model_id):
        return False
    _refuse_unsettled(state)
    if heartbeat is not None and not heartbeat():
        raise MemoryModelError(
            f"this save could no longer show it holds the save slot (another save took "
            f"over, or the shared store could not be reached), so it did not send "
            f"UpdateMemory to switch the memory to {model_id}; last read: "
            f"{state.describe()}.", code="slot_lost")
    client.update_memory(memoryId=memory_id,
                         memoryStrategies={"modifyMemoryStrategies": modify})
    logger.info("memory %s: UpdateMemory sent for extraction model %s", memory_id, model_id)
    _wait_until_settled(client, memory_id, model_id, timeout, interval, sleep, clock, heartbeat)
    return True


def resolve(model_id: str, region: str) -> str:
    """*model_id* (any profile spelling, or a family) as the model a deploy in
    *region* puts on the memory (``model_catalog.effective_memory_model``
    under the deployment allowlist, the rule deploy.sh and the stack apply),
    ``""`` when that deploy would ignore it."""
    return model_catalog.effective_memory_model(model_id, region,
                                                model_catalog.allowed_families())


def configured_memory_id() -> str | None:
    """The memory the app uses (``MEMORY_ID`` or discovery by name), or None.
    Loads the app's config when it is not loaded yet, which may run its
    discovery: only the switch's worker calls it, never a request."""
    try:
        from .config import get_config
        return get_config().memory_id or None
    except Exception as exc:  # noqa: BLE001 - no memory means not selectable
        logger.warning("memory model: no memory id (%s)", type(exc).__name__)
        return None


def loaded_memory_id() -> "tuple[bool, str | None]":
    """``(loaded, memory id)`` from the app's config only when it is already
    loaded (``config.peek_config``): never a load, a discovery or any other
    AWS call. ``(False, None)`` until something loads it."""
    from .config import peek_config
    cfg = peek_config()
    if cfg is None:
        return False, None
    return True, cfg.memory_id or None


#: Why the settings view offers nothing yet: the app's config has not loaded.
LOADING_REASON = "Memory settings are still loading; reload the page in a moment."
_WARMUP_LOCK = threading.Lock()
_WARMUP: "threading.Thread | None" = None


def _load_config() -> None:
    """The background load the settings view starts (``config.get_config``,
    which may run discovery); a failure is logged, never raised."""
    try:
        from .config import get_config
        get_config()
    except Exception as exc:  # noqa: BLE001 - a later view starts another
        logger.warning("memory model: config load failed (%s)", type(exc).__name__)


def _start_warmup(target) -> threading.Thread:
    """Run *target* in a daemon thread (a seam for tests)."""
    thread = threading.Thread(target=target, name="memory-model-config-load", daemon=True)
    thread.start()
    return thread


def warm_config() -> bool:
    """Start ONE background load of the app's config; ``False`` when one is
    already running (or none could be started). Never raises."""
    global _WARMUP
    with _WARMUP_LOCK:
        if _WARMUP is not None and _WARMUP.is_alive():
            return False
        try:
            _WARMUP = _start_warmup(_load_config)
        except Exception as exc:  # noqa: BLE001 - the view still answers
            logger.warning("memory model: config load not started (%s)", type(exc).__name__)
            return False
        return True


def control_client(region: str):
    """A bounded AgentCore control-plane client (settings reads and saves)."""
    import boto3
    return boto3.client("bedrock-agentcore-control", region_name=region,
                        config=_control_config())


def _label(model_id: str) -> str:
    family = model_catalog.family_of(model_id)
    model = next((m for m in model_catalog.MODELS if m.family == family), None)
    return model.label if model else ""


def _locked_without_memory(current_model: str = "") -> str:
    """Why the model may not be changed here, from the deployment alone (no
    memory lookup, no AWS call): the workshop pins it, or the allowlist
    offers no memory model; ``""`` otherwise."""
    from .demo import workshop
    if workshop.is_enabled():
        label = _label(current_model)
        return "Pinned by the workshop" + (f" to {label}." if label else ".")
    if not model_catalog.memory_families(model_catalog.allowed_families()):
        return (f"{model_catalog.ALLOWED_MODELS_ENV} offers no memory extraction model "
                f"({', '.join(model_catalog.MEMORY_EXTRACTION_FAMILIES)}).")
    return ""


_LOOK_UP = object()


def selectable(current_model: str = "", memory_id=_LOOK_UP) -> tuple[bool, str]:
    """Whether the Admin page may change the model, and the reason when not:
    the workshop pins it, no memory resolves, the allowlist offers no memory
    model, or (*memory_id* not given) the app's config has not loaded yet
    (:func:`loaded_memory_id`, never a load). *current_model* names the
    pinned model in the reason."""
    from .demo import workshop
    if workshop.is_enabled():
        return False, _locked_without_memory(current_model)
    if memory_id is _LOOK_UP:
        loaded, memory_id = loaded_memory_id()
        if not loaded:
            return False, LOADING_REASON
    if not memory_id:
        return False, "AgentCore Memory is not configured in this deployment."
    reason = _locked_without_memory(current_model)
    return (False, reason) if reason else (True, "")


def _saved_model(region: str) -> str:
    """SSM :data:`SSM_PARAMETER` (what the next deploy puts on the memory),
    ``""`` when the parameter does not exist. ``get_parameters`` lists a
    missing name in ``InvalidParameters`` instead of raising, so any
    exception here is a failed read."""
    found = _ssm_client(region).get_parameters(Names=[SSM_PARAMETER]).get("Parameters") or []
    return str(found[0].get("Value") or "") if found else ""


#: The most of a saved value the GET repeats when it is not a model the next
#: deploy would use (SSM can hold any hand-written text).
SAVED_INVALID_MAX_CHARS = 200


def _next_deploy_default(region: str) -> str:
    """The model a base or demo deploy puts on the memory with nothing valid
    saved: the stack default (``MEMORY_EXTRACTION_FAMILY``, the catalog's
    :data:`model_catalog.MEMORY_DEFAULT_FAMILY`) resolved for *region*."""
    return model_catalog.resolve_id(model_catalog.MEMORY_DEFAULT_FAMILY, region)


def _add_saved_model(view: dict, region: str) -> None:
    """Compare what the NEXT DEPLOY would put on the memory with the model it
    runs: the saved SSM :data:`SSM_PARAMETER` as that deploy reads it
    (:func:`resolve`, the rule deploy.sh and the stack apply), reported as
    ``memory_model_saved`` when it differs; with nothing valid saved, the
    stack default, reported as ``memory_model_unsaved`` with
    ``memory_model_next_deploy``. A saved value the next deploy ignores is
    ``memory_model_saved_invalid`` (the text, bounded). Only on a deploy that
    reads it (not the workshop) and only against a model that was read; a
    failed read is reported."""
    from .demo import workshop
    live = view["memory_model"]
    if not live or workshop.is_enabled():
        return
    try:
        saved = _saved_model(region).strip()
        effective = resolve(saved, region) if saved else ""
    except Exception as e:  # noqa: BLE001 - reported, never a 500
        view["memory_model_error"] = public_error_text(
            e, "admin.llm_settings.memory_saved_read", default="upstream_error")
        return
    if saved and not effective:
        view["memory_model_saved_invalid"] = saved[:SAVED_INVALID_MAX_CHARS]
    if effective:
        if effective != live:
            view["memory_model_saved"] = effective
        return
    default = _next_deploy_default(region)
    if default != live:
        view["memory_model_unsaved"] = True
        view["memory_model_next_deploy"] = default


def settings_view(region: str) -> dict:
    """The memory fields of GET /api/admin/llm-settings. Never raises: a
    failed read gives ``memory_model: ""`` (or no ``memory_model_saved``)
    and ``memory_model_error``. Never loads the app's config: until it is
    loaded the view reads nothing, says so (:data:`LOADING_REASON`) and
    starts one background load (:func:`warm_config`)."""
    loaded, memory_id = loaded_memory_id()
    if not loaded:
        warm_config()
        return {"memory_model": "", "memory_model_selectable": False,
                "memory_model_locked_reason": LOADING_REASON}
    view: dict = {"memory_model": ""}
    if memory_id:
        try:
            view["memory_model"] = read_state(control_client(region), memory_id).model_id
        except Exception as e:  # noqa: BLE001 - reported, never a 500
            view["memory_model_error"] = public_error_text(
                e, "admin.llm_settings.memory_read", default="upstream_error")
        _add_saved_model(view, region)
    ok, reason = selectable(view["memory_model"], memory_id)
    view["memory_model_selectable"] = ok
    view["memory_model_locked_reason"] = reason
    return view


def refusal(model_id: str) -> str:
    """Why a switch naming *model_id* is refused (``""``: it may go ahead),
    from the deployment alone: a model this deployment does not offer, the
    workshop, or an allowlist with no memory model. No memory lookup and no
    AWS call, so the switch request never waits on the app's config; the
    worker finds the memory (:func:`run_save`)."""
    allowed = model_catalog.allowed_families()
    if not model_catalog.effective_memory_model(model_id, None, allowed):
        offered = ", ".join(sorted(model_catalog.memory_families(allowed))) or "none"
        source = (f"{model_catalog.ALLOWED_MODELS_ENV} offers" if allowed is not None
                  else "memory extraction models")
        return (f"memory_model {model_id!r} is not a memory extraction model offered in "
                f"this deployment ({source}: {offered})")
    reason = _locked_without_memory()
    return f"memory_model cannot be changed here: {reason}" if reason else ""


def _ssm_client(region: str):
    """A bounded SSM client, like :func:`control_client`."""
    import boto3
    return boto3.client("ssm", region_name=region, config=_control_config())


def _switch(target: str, region: str, memory_id: str, heartbeat=None) -> dict:
    """Switch the memory to *target*; the save's result or its error."""
    try:
        changed = set_model(control_client(region), memory_id, target, heartbeat=heartbeat)
    except MemoryModelError as e:
        if e.update_sent:
            message = (f"The memory extraction model change did not finish: {e} Nothing was "
                       "saved for the next deploy; check the memory before saving again.")
        else:
            message = f"The memory extraction model was not changed: {e} Nothing was saved."
        return {"status": "error", "memory_model_changed": False, "message": message}
    except Exception as e:  # noqa: BLE001 - public text only
        detail = public_error_text(e, "admin.llm_settings.memory_update", default="upstream_error")
        return {"status": "error", "memory_model_changed": False, "message": (
            f"The memory extraction model could not be changed: {detail} Nothing was saved.")}
    return {"status": "ok", "memory_model": target, "memory_model_changed": changed}


def _persist(target: str, region: str, result: dict) -> dict:
    """Write SSM after the memory runs *target*; say so honestly if it fails."""
    try:
        _ssm_client(region).put_parameter(Name=SSM_PARAMETER, Value=target,
                                          Type="String", Overwrite=True)
    except Exception as e:  # noqa: BLE001 - public text only
        detail = public_error_text(e, "admin.llm_settings.memory_ssm_write",
                                   default="upstream_error")
        return {**result, "status": "error", "message": (
            f"The memory now runs {target}, but saving it for the next deploy failed "
            f"({detail}), so SSM still holds what it held before and the next deploy would "
            f"not keep {target}. Save {target} again to keep it.")}
    return result


def _check_after_save(target: str, region: str, memory_id: str, result: dict) -> dict:
    """One more read after SSM was written: a memory that is not settled on
    *target* (FAILED, still changing, or moved by another writer such as a
    deploy or a change outside this page) is an error that says SSM now
    holds *target* and what the memory read. SSM is left as saved: which
    model is wanted is the steward's call."""
    try:
        state = read_state(control_client(region), memory_id)
    except Exception as e:  # noqa: BLE001 - public text only
        detail = public_error_text(e, "admin.llm_settings.memory_recheck",
                                   default="upstream_error")
        return {**result, "status": "error", "message": (
            f"The memory was switched to {target} and {target} was saved for the next "
            f"deploy, but reading the memory back after saving failed ({detail}), so it is "
            "not known whether it still runs that model. Reload the page to check.")}
    if state.settled_on(target):
        return result
    held = f"{target} is now saved for the next deploy (SSM holds it), but"
    if state.failed():
        found = (f"reading the memory back after saving shows a failure: {state.describe()}. "
                 "Check the memory in AgentCore.")
    elif not state.settled():
        found = (f"reading the memory back after saving shows it busy, not settled: "
                 f"{state.describe()}; something changed it after this save switched it. "
                 "Reload the page when it and every strategy are ACTIVE.")
    else:
        found = (f"the memory now reads {state.describe()}: something else changed it after "
                 "this save switched it. The saved value was left as it is.")
    return {**result, "status": "error", "message": f"{held} {found}"}


def done_message(target: str, changed: bool) -> str:
    """The message of a save that finished."""
    if changed:
        return (f"The memory extraction model was switched to {target} and saved for the "
                "next deploy. New extractions use it; records already extracted stay.")
    return (f"The memory already ran {target}; it is now saved for the next deploy, so a "
            "deploy keeps it.")


def claim_save_slot(owner: str, memory_model: str = "", *, now: "float | None" = None) -> str:
    """Take the save slot for *owner*: ``""`` when taken, else the refusal's
    ``reason``, :data:`SAVE_IN_PROGRESS` (another save holds it) or
    :data:`SAVE_NOT_COORDINATED` (the shared store could not decide). Strict
    (``JobStore.claim_slot``): a configured table that errors, or one the
    fail-soft path disabled, refuses rather than deciding from this task's
    memory. The claim is the running switch's record every task reads: its
    owner (the job id), *memory_model*, and ``claimed_at`` (``started_at``
    moves with each heartbeat), at *now* (the clock read when ``None``)."""
    now = time.time() if now is None else now
    store = _save_slots()
    state = {"status": "running", "job_id": owner, "started_at": now, "claimed_at": now}
    if memory_model:
        state["memory_model"] = memory_model
    if store.claim_slot(SAVE_SLOT_KEY, state, stale_seconds=SAVE_SLOT_STALE_SECONDS,
                        now=now, strict=True):
        return ""
    return SAVE_IN_PROGRESS if _held_by_another(store, now) else SAVE_NOT_COORDINATED


def _held_by_another(store, now: float) -> bool:
    """Why a strict claim was refused: a fresh running holder (``True``), or
    a store that could not decide (``False``). Without a shared table the
    claim is the in-memory CAS, which refuses only for a holder."""
    if not store.has_shared_table():
        return True
    from .job_store import JobStoreUnavailableError
    try:
        record = store.get_strict(SAVE_SLOT_KEY) or {}
    except JobStoreUnavailableError:
        return False
    try:
        started = float(record.get("started_at"))
    except (TypeError, ValueError):
        return False
    return record.get("status") == "running" and started >= now - SAVE_SLOT_STALE_SECONDS


def read_save_slot() -> "dict | None":
    """The slot's record (its ``job_id`` is the last switch job): a
    consistent read of the shared table when one is configured (raises
    ``JobStoreUnavailableError`` when it cannot answer), else this process's
    copy."""
    store = _save_slots()
    if store.has_shared_table():
        return store.get_strict(SAVE_SLOT_KEY)
    return store.get(SAVE_SLOT_KEY)


def release_save_slot(owner: str, result: "dict | None" = None) -> bool:
    """Exact-owner release; ``True`` when it landed. *result* (the switch's
    terminal record) is kept in the released slot as ``result``, so every
    task can read how the switch ended even when its job record was not
    written. A slot taken over or unreachable is not released and goes
    stale."""
    state = {"status": "idle", "job_id": owner, "finished_at": time.time()}
    if result is not None:
        state["result"] = result
    released = _save_slots().release_slot(SAVE_SLOT_KEY, owner, state, strict=True)
    if not released:
        logger.warning("memory model save: slot not released (taken over by another save, "
                       "or the shared store could not be reached); it goes stale after %ss",
                       int(SAVE_SLOT_STALE_SECONDS))
    return released


#: The switch's error when the app has no memory to switch.
NO_MEMORY = ("AgentCore Memory is not configured in this deployment, so there is no memory "
             "to switch. Nothing was changed.")


def run_save(target: str, region: str, owner: str) -> dict:
    """The save, with this task holding the slot as *owner* (the switch
    job's worker): find the memory (:func:`configured_memory_id`, once; it
    may load the app's config), switch it and read it back FIRST, then write
    :data:`SSM_PARAMETER`, then read the memory once more. Each mutating
    call (UpdateMemory, the SSM write) comes right after a strict heartbeat
    shows this save still holds the slot. ``status`` is ``ok`` or ``error``
    with a ``message`` (:data:`NO_MEMORY` before any call when there is no
    memory). *target* is a resolved id the caller checked with
    :func:`refusal`."""
    def heartbeat() -> bool:
        return _save_slots().touch_slot(SAVE_SLOT_KEY, owner, strict=True)

    memory_id = configured_memory_id()
    if not memory_id:
        return {"status": "error", "memory_model_changed": False, "message": NO_MEMORY}
    result = _switch(target, region, memory_id, heartbeat)
    if result["status"] != "ok":
        return result
    if not heartbeat():
        return {**result, "status": "error", "message": (
            f"The memory now runs {target}, but this save could no longer show it holds the "
            "save slot (another save took over while it waited, or the shared store could not "
            "be reached), so it saved nothing for the next deploy. Reload the page to see what "
            "the memory runs and what is saved.")}
    result = _persist(target, region, result)
    if result["status"] != "ok":
        return result
    return _check_after_save(target, region, memory_id, result)
