# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The onboarding chat's provision and removal, run by the app itself.

The onboarding chat's agent loop runs in the app (``POST /api/onboard/chat``,
``onboard._run_onboard_agent``) under the user's verified integrator/Admin
session; its other tools come from the onboarding gateway
(``onboard_agent._get_onboard_tools``). Provisioning a system and removing it
are the app's own work: they write the graph, start or delete the system's
agent runtime and gateway target, and change the app's catalog, under the
app's per-system locks. So the chat's agent gets two tools of its own, with
the names and parameters of the gateway's:

* :func:`provision_system` runs the wizard's provision on what the chat's
  onboarding session holds. The wizard's provision takes only mappings joined
  to the schema provenance its own discover records, and the chat's discover
  records none, so the wizard's discover reads the system's schema again here
  and the session's mappings are joined to it with the wizard map job's own
  step; a mapped field or a source of this system's identity rules that is
  no longer in the schema refuses the provision (the schema the chat mapped,
  stored in the session, tells a field of the system from a row naming no
  column, and this system's rules from other systems', which are left out).
  That read and check run in the provision job, before it claims the session
  or writes anything; the call takes the system's one provision claim
  (shared with the Onboard view's provision), starts the job and answers at
  once, and a refusal the job finds is the next call's answer. Then the
  wizard's provision job runs on the body the wizard sends, under that
  claim, until the session says how it ended.
* :func:`reset_system` is the card's removal, run as the card's background
  removal, or starts a half-done chat onboarding over
  (``onboard_agent._reset_in_app``).

In a deployment the agent also gets :func:`connect_new_system`, which
registers nothing: the onboarding runtime keeps its own catalog, so a system
it registered is unknown to the app's provision, and a system the app
registered is invisible to the runtime's discover until it is onboarded. A new
system is added with Connect New System in the Onboard view and the wizard.

In a deployment the agent also gets the app's own :func:`get_catalog` and
:func:`discover`, which read the APP's in-process catalog instead of the
onboarding runtime's. The runtime's catalog lists only onboarded systems
(``catalog.SystemCatalog.hydrate`` skips a System node without the onboarding
marker), so the runtime's get_catalog/discover cannot see a registration: a
system the app holds as registered-but-not-onboarded (e.g. after a card
removal that kept the registration) looked unregistered, and the chat wrongly
sent the user to Connect New System for an id that already exists. The app's
get_catalog reports the app catalog's statuses, and the app's discover answers
a registered-but-not-onboarded id by sending it to the wizard's steps on its
card (an id with an onboarding session still gets the discover gate's own
answer; an onboarded system is never discovered again).

In a deployment the agent also gets the app's own :func:`store_credentials`,
which writes the system's ``adt/systems/<id>`` secret from the app and points
the system's graph node at it, so the onboarding runtime and the app's
provision both read it next (:func:`store_system_credentials`, also the
secure credential form's store). The
onboarding gateway accepts any app token and has no per-tool policy attached,
so the onboarding runtime does not export a tool that writes a secret; here
the write runs inside the chat request, which only an integrator or Admin
reaches.

The gateway's tools of those names are left out of the chat's tool list
(:data:`GATEWAY_TOOL_FILTERS`), and the onboarding runtime does not export
them at all except ``discover``, which the chat forwards a runtime-owned case
to (:data:`NOT_ON_GATEWAY`, :func:`gateway_tools`). Local mode, where the
in-process tools share the app's catalog, uses the app's provision_system and
reset_system (:func:`in_app_tools`) and keeps its own get_catalog, discover,
connect_new_system and store_credentials.
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import threading
import uuid
from contextvars import ContextVar

from strands import tool

from . import onboard_agent as oa
from .db import validate_system_id
from .onboarding_session import (STATUS_FAILED, STATUS_ONBOARDED, STATUS_PROVISIONING,
                                 STATUS_RESOLVED, DuplicateProvisionError,
                                 SessionNotFoundError)

logger = logging.getLogger(__name__)

#: The onboarding tools the app answers itself in a deployment; the gateway's
#: tools of these names are hidden there. ``get_catalog`` and ``discover`` are
#: answered from the APP's in-process catalog (not the runtime's, which lists
#: only onboarded systems), so a system the app holds as registered-but-not-
#: onboarded is reported as registered and sent to the wizard on its card
#: rather than mistaken for an unregistered one. ``store_credentials`` writes
#: the secret from the app, so the runtime exports no tool that writes one.
APP_TOOL_NAMES = frozenset({"get_catalog", "discover", "provision_system",
                            "reset_system", "connect_new_system", "store_credentials"})

#: The app's own tools whose gateway tool the app still calls: the chat's
#: discover sends a case the runtime owns to the gateway's
#: ``onboarding___discover`` (:func:`_forward_discover`).
FORWARDED_TO_GATEWAY = frozenset({"discover"})

#: The onboarding tools the runtime does not export to the gateway: the app
#: answers them itself in a deployment and never calls the gateway's. The
#: gateway accepts any app token and has no per-tool policy attached yet, so
#: an exported one could be called on the gateway directly, past the app's
#: integrator/Admin check. These are all the commit-tier tools, so the
#: gateway serves only the explore tier (``discover`` included, which the
#: chat forwards to). ``onboarding_runtime_server`` exports
#: ``ONBOARD_TOOLS`` without these (:func:`gateway_tools`); local mode, which
#: has no gateway, keeps them all.
NOT_ON_GATEWAY = APP_TOOL_NAMES - FORWARDED_TO_GATEWAY

#: Gateway tool names are ``<target>___<tool>`` (``onboarding___reset_system``).
_GATEWAY_TOOL_SEP = "___"

_NOT_RESOLVED = "No resolved mappings. Call resolve_identities first."

#: How long a check on a running provision waits for it to end.
CHECK_WAIT_SECONDS = 20.0
_CHECK_POLL_SECONDS = 0.5


def runs_in_app(agent_tool) -> bool:
    """Strands ``ToolFilters`` matcher: True for a gateway tool
    (``MCPAgentTool``) whose name, with or without the gateway's target
    prefix, is one the app runs itself."""
    name = str(getattr(getattr(agent_tool, "mcp_tool", None), "name", "") or "")
    return name.rsplit(_GATEWAY_TOOL_SEP, 1)[-1] in APP_TOOL_NAMES


#: The onboarding gateway client's filter: the app's own tools are left out.
GATEWAY_TOOL_FILTERS = {"rejected": [runs_in_app]}


def gateway_tools(tools) -> list:
    """``tools`` without the ones the onboarding runtime does not export to
    the gateway (:data:`NOT_ON_GATEWAY`), in order."""
    return [t for t in tools if getattr(t, "tool_name", None) not in NOT_ON_GATEWAY]


#: The onboarding runtime's ``discover`` the chat forwards a runtime-owned case
#: to (an onboarded system that has no onboarding session, or any status other
#: than 'registered'): the gateway MCPClient bound for THIS chat request
#: (``bind_runtime_discover``, from ``onboard_agent._get_onboard_tools``). A
#: ContextVar so two concurrent chat requests (each on its own worker thread,
#: ``onboard._run_onboard_agent``) never share a binding. None in local mode
#: and before a cloud request binds it.
_RUNTIME_DISCOVER: "ContextVar" = ContextVar("onboard_chat_runtime_discover", default=None)


def bind_runtime_discover(gateway_client) -> None:
    """Bind the gateway MCPClient the chat's discover forwards a runtime-owned
    case to for this request (``gateway_client`` None unbinds it). Called when
    the cloud tool set is built so the forward uses the SAME client the agent
    runs its gateway tools on (its session is live for the agent's turn)."""
    _RUNTIME_DISCOVER.set(gateway_client)


def _forward_discover(system_id: str) -> dict:
    """Run the onboarding runtime's ``discover`` for ``system_id`` through the
    bound gateway client and return its result unchanged. The gateway advertises
    the tool prefixed by its target (``onboarding___discover``). The result is
    an ``MCPToolResult`` (``toolUseId``/``status``/``content``), which Strands'
    @tool wrapper passes through as the tool result (it only re-stamps
    ``toolUseId``), so the model sees exactly what the runtime's discover
    returned, the same as on main. With nothing bound (a wiring bug), fail
    loudly rather than fabricating an answer."""
    client = _RUNTIME_DISCOVER.get()
    if client is None:
        raise RuntimeError(
            "The onboarding chat's discover has no gateway client bound to "
            "forward to the runtime; refusing to answer a runtime-owned discover "
            "in-app (it would not write the runtime's onboarding session).")
    return client.call_tool_sync(uuid.uuid4().hex, "onboarding___discover",
                                 {"system_id": system_id})


@tool
def get_catalog() -> dict:
    """List the systems in the catalog with their status, so the model reports
    only systems that exist and never calls Connect New System for one already
    registered.

    In a deployment this reads the APP's in-process catalog (the one the card
    reads), not the onboarding runtime's: the runtime lists only systems that
    are onboarded, so a system the app holds as registered-but-not-onboarded
    would be missing from the runtime's list. Each entry carries its id, name,
    type, plant and status: an 'onboarded' system is live; a 'registered' one
    is connected but not onboarded (onboard it with the wizard's steps on its
    card: Discover, Map, Resolve and Provision)."""
    return oa.get_catalog()


@tool
@oa._unless_removal_failed
def discover(system_id: str) -> dict:
    """Discover a system's schema/tables, or relay why it does not run.

    In a deployment the chat cannot onboard a system the app holds as
    registered-but-not-onboarded (status 'registered', no onboarding session):
    for such a system this answers that it is registered and must be onboarded
    with the wizard's steps on its card (Discover, Map, Resolve and Provision),
    not with Connect New System. A system the app does not know is sent to
    Connect New System in the Onboard view. Every other case runs the
    onboarding runtime's discover (a system with an onboarding session keeps
    the gate's own answer, naming the step to call next)."""
    return discover_for_chat(system_id)


def discover_for_chat(system_id: str) -> dict:
    """The chat's discover in a deployment. Reads only the app's in-process
    catalog and the onboarding session (no source read, no new query):

    * an id the app's catalog does not hold -> how a new system is added
      (:func:`new_system_in_onboard_view`), the same answer provision_system
      and connect_new_system give for an unknown id;
    * an id that already has an onboarding session -> the discover gate's
      answer for that session (``onboard_agent._discover_gate_refusal``): its
      status and the next step, unchanged;
    * an id whose catalog status is exactly 'registered' and that has NO
      onboarding session -> :func:`registered_not_onboarded`: it is registered
      here, onboard it with the wizard's steps on its card;
    * every other case (an onboarded system with no session -- seeded or
      provisioner-onboarded, or its session was cleared -- or any other
      status) -> the onboarding runtime's discover, forwarded unchanged
      (:func:`_forward_discover`).

    Only the 'registered'-and-no-session case is the app's to answer, because
    the chat cannot onboard a registered-only system in a deployment BY DESIGN
    (one catalog does not serve the whole flow, and the app's registration is
    invisible to the runtime's discover until onboarded). Discovering any
    system the runtime does know, including an onboarded one with no session,
    stays the runtime's job so its session is written exactly as before."""
    from . import onboard as ob
    from .catalog import REMOVAL_FAILED
    try:
        oa.validate_system_id(system_id)
    except ValueError as exc:
        return {"error": str(exc)}
    entry = ob.catalog.lookup(system_id)
    if entry is None:
        return new_system_in_onboard_view(system_id)
    refused = oa._existing_session_refusal(system_id)
    if refused is not None:
        return refused
    # No onboarding session. Only a plain 'registered' entry is the app's to
    # answer; anything else (onboarded with no session, or any other status
    # except the removal-failed one the decorator already refused) is the
    # runtime's discover.
    if entry.get("status") == "registered":
        return registered_not_onboarded(system_id)
    if entry.get("status") == REMOVAL_FAILED:  # defensive; the decorator refuses it first
        return oa._removal_failed_refusal(system_id) or _forward_discover(system_id)
    return _forward_discover(system_id)


def registered_not_onboarded(system_id: str) -> dict:
    """The answer when the chat is asked to discover/onboard a system the app
    holds as registered but not onboarded (no onboarding session): it is
    registered here, the chat cannot onboard it in this deployment, and it is
    onboarded with the wizard's steps on its card. Nothing is read or changed."""
    return {"error": f"'{system_id}' is registered in this deployment but not yet onboarded. "
                     "The chat cannot onboard it here: onboard it with the wizard's steps on "
                     "its card in the Onboard view: Discover, Map, Resolve and Provision. Do "
                     "not use Connect New System, it is already registered.",
            "reason": "registered_not_onboarded",
            "steps": ["Discover", "Map", "Resolve", "Provision"],
            "offer": "Offer to explain any of these steps.",
            "system_id": system_id}


@tool
@oa._unless_removal_failed
def provision_system(system_id: str) -> dict:
    """Provision the system: write its schema, field mappings and identity
    rules to the knowledge graph and start its agent, the same provision as
    Provision in the Onboard view. Must call resolve_identities first.

    For a system this app does not know it changes nothing and answers how a
    new system is added (Connect New System in the Onboard view, then the
    wizard); relay that. It refuses at once, and changes nothing, while the
    system is being provisioned or removed, and when its onboarding has not
    reached resolve. Otherwise it starts the provision and returns at once
    with {onboarding_id, status: 'provisioning'}. The provision first reads
    the system's schema again; it changes nothing, and the next call answers
    the refusal, when the system cannot be read or when any field it mapped
    or any identity rule's source field is no longer in the schema (the
    answer names them and how to start the system over and map it again).
    Identity rules sourced in other systems, and mapped fields that
    name no column of the system, are left out (the answers count them).
    Call it again with the same system_id to check on it (a check waits up
    to 20 seconds for it to finish): 'provisioning' while it runs,
    'onboarded' with what was written once it has finished, 'failed' with
    the reason and how to start the system over, or the refusal. For a
    system that is not onboarded that is reset_system; for an onboarded
    system that is not reset_system, which removes it completely: the
    answer says what to do instead."""
    return provision_for_chat(system_id)


@tool
def reset_system(system_id: str) -> dict:
    """Start a system over or remove it, and relay the answer.

    A system that is not onboarded is never removed here. A chat onboarding of
    it that stopped part-way is started over: only its onboarding session is
    cleared, so discover can run again; its registration and connection stay.
    A discover still in progress is left alone (try again shortly), and a
    provision that stopped before it finished is marked failed and cleared.
    With nothing to start over, the answer points to the system's card in the
    Onboard view.

    An onboarded system is removed completely: its knowledge-graph data,
    its agent runtime and gateway target, and its registration (a system the
    user connected has its stored credentials scheduled for deletion after 7
    days), the same removal as Remove system in the Onboard view with "Also
    forget the connection registration" checked. The removal runs in the
    app and takes a few minutes: the call answers at once with status
    'removing'; tell the user it takes a few minutes and call reset_system
    again with the same system_id to check on it ('removing' while it runs,
    'reset' once it finished, or why it did not finish). A check starts
    nothing. It is refused, with a reason to relay, while the system is
    being provisioned or its connection is being changed, while another
    removal or a reset of all systems runs, and when an earlier removal that
    kept the registration did not finish. A removal that did not finish
    answers that it should be removed again.

    It is not the way to run discover again on an onboarded system: the app
    removes an onboarded system completely, as above, so the system the user
    wanted re-onboarded would be gone. A re-onboarding goes on with the next
    step discover's answer names; to start an onboarded system's onboarding
    over, the user removes it with Remove system on its card in the Onboard
    view, keeping the connection registration (the default), and onboards it
    again."""
    return oa._reset_in_app(system_id)


@tool
def connect_new_system(name: str, system_type: str, plant: str, connection_type: str, connection_string: str = "", schema: str = "", connection_config: dict | None = None, query_guidance: str = "", capabilities: str = "", tool_prefix: str = "", system_id: str = "") -> dict:  # noqa: E501 - the gateway tool's exact signature
    """Say how a new system is added in this deployment. Registers nothing.

    In this deployment a new system is not registered from the chat. It is
    added with Connect New System in the Onboard view (its connection details
    and credentials go in that form), then onboarded with the wizard's steps
    on its card: Discover, Map, Resolve and Provision. Relay the answer and
    offer to explain any of those steps. Nothing passed here is kept."""
    return new_system_in_onboard_view()


def new_system_in_onboard_view(system_id: str = "") -> dict:
    """The answer when the chat is asked to register, or to provision, a system
    this app does not know: nothing was registered, claimed or started."""
    which = f"'{system_id}' is not registered in this app. " if system_id else ""
    return {"error": f"{which}In this deployment a new system is added in the Onboard view, not "
                     "from the chat: register it with Connect New System, then run the "
                     "wizard's steps on its card: Discover, Map, Resolve and Provision. Nothing "
                     "was registered.",
            "reason": "connect_in_onboard_view",
            "steps": ["Connect New System", "Discover", "Map", "Resolve", "Provision"],
            "offer": "Offer to explain any of these steps.",
            **({"system_id": system_id} if system_id else {})}


@tool
@oa._unless_removal_failed
def store_credentials(system_id: str, credentials: str) -> dict:
    """Store credentials securely in AWS Secrets Manager for a system.

    In this deployment the app stores them and points the system's
    knowledge-graph record at them, so its connection test, discover and
    provision read them from then on (the same store as the secure credential
    form). It stores nothing, and the answer says why and what to do instead,
    for a system this app does not know, a system registered with Connect New
    System or whose credentials are kept in its own connection secret (an
    Admin changes those with Edit connection on its card), an MCP Server, a
    system that connects with the deployment's own credentials (PostgreSQL,
    SiteWise (Direct), Athena), a system not yet provisioned, and while its
    connection is being changed or provisioned. A stored answer can carry a
    note (an agent already running keeps its old credentials until the system
    is provisioned again); relay it. Never echo the credentials.

    Args:
        system_id: The system to store credentials for.
        credentials: JSON string with credential fields (varies by connection type).
    """
    # While the app's startup restore still runs in the background, refused
    # like the secure credential form's 503 (startup_restore).
    from . import startup_restore
    return (startup_restore.refusal(system_id)
            or store_system_credentials(system_id, credentials))


def _refused_store(system_id: str, reason: str, why: str) -> dict:
    return {"error": f"'{system_id}' {why} Nothing was stored.", "reason": reason,
            "system_id": system_id}


_KEPT_IN_OWN_SECRET = (  # nosec B105 - user-facing sentence text, not a credential
    "keeps its credentials in its own connection secret, which its provision and its agent "
    "read. An Admin changes them with Edit connection on its card in the Onboard view.")


def _store_kept_elsewhere(system_id: str, entry: dict, node: dict) -> dict | None:
    """The refusal for a system whose next steps would never read credentials
    stored at ``adt/systems/<id>``, else ``None``. The connector is the checked
    System ``node``'s (``connection_resolver.node_connection_checked``), not
    the app's catalog entry, which an Edit connection may not have reached;
    a Connect New System origin on either refuses. Only the REST family (REST
    API, SiteWise (i3X), Tulip) resolves its credentials through the
    node's pointer in both the onboarding runtime
    (``connection_resolver.resolve_connection``) and the app's provision
    (``onboard._dp_i3x_connection_inputs``). An MCP Server's provision reads
    its token only from its own ``adt/conn/<id>`` secret, and the built-in
    HighByte's key is ``adt/conn/highbyte``. A PostgreSQL system the
    deployment registered is pointed back at the deployment database's secret
    by its provision, and its agent connects with a read-only login; SiteWise
    (Direct) and Athena connect with an AWS IAM role."""
    from . import builtin_highbyte
    from .connection_resolver import (ORIGIN_USER, is_own_connection_secret,
                                      is_user_registration)
    from .connection_types import MCP_SERVER, REST_API, SITEWISE_I3X, TULIP
    if is_user_registration(entry) or node["connection_origin"] == ORIGIN_USER:
        return _refused_store(system_id, "edit_connection", (
            "was registered with Connect New System, so its credentials are kept with its "
            "registration. An Admin changes them with Edit connection on its card in the "
            "Onboard view."))
    if not (node["exists"] and node["has_config"]):
        return _refused_store(system_id, "no_connection_record", (
            "has no connection record in the knowledge graph yet, so credentials stored now "
            "would not be used. Provision it from its card in the Onboard view first."))
    connection_type = node["connection_type"]
    if (builtin_highbyte.is_builtin(system_id) or connection_type == MCP_SERVER
            or is_own_connection_secret(node["secret_arn"], system_id)
            or is_own_connection_secret(entry.get("secret_arn"), system_id)):
        return _refused_store(system_id, "edit_connection", _KEPT_IN_OWN_SECRET)
    if connection_type not in (REST_API, SITEWISE_I3X, TULIP):
        return _refused_store(system_id, "deployment_credentials", (
            "connects with the deployment's own credentials (its database's secret or an "
            "AWS IAM role), so credentials stored here would never be used. To connect "
            "another source, register it with Connect New System in the Onboard view."))
    return None


def _busy_store(system_id: str, holder: str) -> dict:
    from .connection_lock import PROVISION
    why = ("is being provisioned, so its credentials were not changed. Try again once that "
           "has finished." if holder == PROVISION else
           "has a change of its connection in progress, so its credentials were not changed. "
           "Try again in a moment.")
    return _refused_store(system_id, "connection_busy", why)


def _written_but(system_id: str, reason: str, what_happened: str, effect: str,
                 then: str) -> dict:
    """The answer when the secret was written but the node was not pointed at
    it: what happened to the node, what the written secret now means
    (``effect``), and what to do."""
    return {"error": (f"The credentials were written to AWS Secrets Manager, but "
                      f"{what_happened} {effect} {then}"),
            "reason": reason, "system_id": system_id}


_NOT_IN_USE = "They are not in use; its current credentials still apply."


def _stored_note(system_id: str, node: dict, same_secret: bool) -> str:
    """What still uses the old credentials after a store: an agent the system
    already runs (it was started with the credentials it had then), and, for
    a rotation of the same secret, a process that read it in the last minute
    (the resolvers cache a secret's value for up to 45 seconds; the app's own
    copy was dropped)."""
    from .catalog import ONBOARDED_NODE_STATUS
    notes = []
    if node["status"] == ONBOARDED_NODE_STATUS:
        notes.append(f"'{system_id}' is provisioned, and its agent keeps the credentials it "
                     "was started with until it is provisioned again (Provision on its card in "
                     "the Onboard view).")
    if same_secret:
        notes.append("A connection test or discover in the next minute may still use the "
                     "previous credentials.")
    return " ".join(notes)


def store_system_credentials(system_id: str, credentials) -> dict:
    """Store a system's credentials: the ONE implementation behind the chat's
    ``store_credentials`` tool and the secure credential form
    (``POST /api/onboard/credentials``), run in the app process under the
    request's integrator/Admin session (the onboarding runtime does not
    export it, so no gateway token reaches it).

    ONE durable step, because the app and the onboarding runtime are separate
    processes and every later reader resolves a connection from the System
    node. Under the system's connection lock (the one Edit connection and
    provision take; it fails at once behind a provision), after the removal
    checks the lock orders: the node is read checked
    (``connection_resolver.node_connection_checked``) and the system classified
    from it (:func:`_store_kept_elsewhere`); the secret is written to
    ``adt/systems/<id>`` and this process's cached copy of it dropped; the
    node is pointed at it with ONE compare-and-set statement that lands only
    while its connection type and pointer are still the ones read
    (``connection_resolver.publish_secret_pointer``); only then is the app's
    catalog entry changed and the answer "stored", with a note when something
    keeps the old credentials (:func:`_stored_note`).

    Refused, with nothing written: an id the app's catalog does not hold
    (:func:`new_system_in_onboard_view`); credentials that are not a JSON
    object; a busy connection; a removal in progress or unfinished; an
    unreadable graph; and the systems :func:`_store_kept_elsewhere` refuses.
    After the secret was written: a node that changed meanwhile is a
    ``conflict`` and a pointer the graph did not take is
    ``pointer_not_saved``, each saying truthfully what the written secret now
    means (:func:`_written_but`), the catalog unchanged. An invalid
    identifier raises, as the onboarding runtime's tool did."""
    from . import onboard as ob
    validate_system_id(system_id)
    entry = ob.catalog.lookup(system_id)
    if entry is None:
        return new_system_in_onboard_view(system_id)
    try:
        creds = json.loads(credentials) if isinstance(credentials, str) else credentials
    except json.JSONDecodeError:
        creds = None
    if not isinstance(creds, dict) or not creds:
        return {"error": "credentials must be a JSON object of credential fields. Nothing was "
                         "stored.", "system_id": system_id}
    from .connection_lock import PROVISION, ConnectionBusyError, connection_lock
    try:
        with connection_lock(system_id, fail_fast_behind=(PROVISION,)):
            refused = oa._removal_running_refusal(system_id) or oa._removal_failed_refusal(
                system_id)
            if refused:
                return refused
            return _store_locked(system_id, entry, creds)
    except ConnectionBusyError as exc:
        return _busy_store(system_id, exc.holder)


def _publish_pointer(system_id: str, arn: str, node: dict, same_secret: bool) -> dict | None:
    """Point the system's node at the secret just written. None when it is
    saved; otherwise the answer for a secret that is written but not pointed at."""
    from .connection_resolver import (ConnectionChangedError, ConnectionNodeWriteError,
                                      publish_secret_pointer)
    try:
        publish_secret_pointer(system_id, arn, expected_arn=node["secret_arn"],
                               connection_type=node["connection_type"])
    except ConnectionChangedError:
        logger.warning("%s: the node changed meanwhile; store_credentials wrote the secret",
                       system_id)
        return _written_but(system_id, "conflict", (
            f"the connection of '{system_id}' changed while they were being stored (for "
            "example an Edit connection), so its knowledge-graph record was not changed."),
            ("The secret its record pointed at before now holds them." if same_secret
             else _NOT_IN_USE),
            "Check its connection on its card in the Onboard view, then store them again "
            "if it still needs them.")
    except ConnectionNodeWriteError as exc:
        logger.warning("%s: pointer not saved (%s); store_credentials wrote the secret",
                       system_id, type(exc).__name__)
        return _written_but(system_id, "pointer_not_saved", (
            f"the knowledge-graph record of '{system_id}' could not be updated to point at "
            "them."),
            ("Its record already points at that secret, so the new values are what is read "
             "from it from now on." if same_secret else _NOT_IN_USE),
            "Try again in a moment.")
    return None


def _store_locked(system_id: str, entry: dict, creds: dict) -> dict:
    """:func:`store_system_credentials` under the system's connection lock."""
    from .connection_resolver import invalidate_secret, node_connection_checked
    from .db import GraphQueryError
    from .public_errors import public_error_text
    try:
        node = node_connection_checked(system_id)
    except GraphQueryError as exc:
        logger.warning("%s: graph read failed (%s); store_credentials stored nothing",
                       system_id, exc.category)
        return _refused_store(system_id, "graph_unavailable", (
            "could not be checked in the knowledge graph, so its credentials were not "
            "changed. Try again in a moment."))
    refused = _store_kept_elsewhere(system_id, entry, node)
    if refused is not None:
        return refused
    try:
        arn = oa._store_secret(system_id, creds)
    except Exception as exc:  # noqa: BLE001 - answered as a fixed category, detail logged
        return {"error": "Failed to store credentials: " + public_error_text(
            exc, "onboard_chat_lifecycle.store_credentials", default="upstream_error"),
                "system_id": system_id}
    invalidate_secret(arn)   # a rotation keeps the ARN: never serve the old value here
    # A rotation overwrites the very secret the node already points at, so
    # its new value is what that secret holds from now on, whatever happens
    # to the node below.
    same_secret = node["secret_arn"] == arn
    unsaved = _publish_pointer(system_id, arn, node, same_secret)
    if unsaved is not None:
        return unsaved
    entry["secret_arn"] = arn
    answer = {"status": "stored", "secret_arn": arn, "system_id": system_id}
    note = _stored_note(system_id, node, same_secret)
    return {**answer, "note": note} if note else answer


#: The app's own onboarding tools that local mode puts in place of the
#: in-process ones (:func:`in_app_tools`). Local mode keeps the in-process
#: get_catalog / discover (one catalog serves the whole flow there), so only
#: provision_system and reset_system are replaced.
APP_TOOLS = [provision_system, reset_system]

#: The app's own onboarding tools a deployment's agent gets beside the
#: gateway's, in the order it gets them. get_catalog and discover are the
#: app's here (the gateway's are hidden) so they read the app's catalog, which
#: holds registrations the runtime's catalog does not; store_credentials is
#: the app's so the secret is written from the app, behind its gate.
CLOUD_APP_TOOLS = [get_catalog, discover, provision_system, reset_system,
                   connect_new_system, store_credentials]


def in_app_tools(tools) -> list:
    """``tools`` with each tool the app runs itself replaced, in place, by the
    app's own (local mode: the in-process ``ONBOARD_TOOLS``)."""
    own = {t.tool_name: t for t in APP_TOOLS}
    return [own.get(getattr(t, "tool_name", None), t) for t in tools]


# ── provision ───────────────────────────────────────────────────────────────

def provision_for_chat(system_id: str) -> dict:
    """The chat's provision. An id this app's catalog does not hold is
    answered first, whatever session the id may have: nothing here can
    provision it, so the answer is how a new system is added
    (:func:`new_system_in_onboard_view`) and nothing is read or changed.
    For a known system: answer a provision already claimed, refuse a session
    that is not resolved, answer the chat's job still reading the schema or
    a refusal it found (:func:`_check_on_the_chat_job`), else start one
    (:func:`_start`)."""
    from . import onboard as ob
    try:
        validate_system_id(system_id)
    except ValueError as exc:
        return {"error": str(exc)}
    if ob.catalog.lookup(system_id) is None:
        return new_system_in_onboard_view(system_id)
    try:
        session = oa._store().get_session(system_id)
    except SessionNotFoundError:
        return {"error": _NOT_RESOLVED}
    status = session.get("status")
    if status in (STATUS_PROVISIONING, STATUS_ONBOARDED, STATUS_FAILED):
        return provision_status(system_id, session)
    if status != STATUS_RESOLVED:
        return {"error": _NOT_RESOLVED}
    checked = _check_on_the_chat_job(system_id, session)
    if checked is not None:
        return checked
    return _start(system_id, session)


# The chat's provisions started in this process, by system id: the job id,
# its phase (PREPARING while the job reads the schema again and checks the
# session against it, then REFUSED, or STARTED once it claimed the session),
# the session's ``updated_at`` it started from, what it left out, and for a
# refusal its answer. A record leaves once it has nothing more to answer: a
# refusal when a call takes it (:func:`_take_refusal`) or the session it was
# found for has moved on, a job another request superseded at once, a
# started one when its final answer is given (:func:`_final_answer`). So it
# holds at most the live job of a system. Process-local, like the provision
# claim it goes with.
PREPARING, REFUSED, STARTED = "preparing", "refused", "started"
_CHAT_JOBS: dict = {}
_CHAT_JOBS_LOCK = threading.Lock()

#: How many times a check reads the registry again after the record it read
#: changed under it (a refusal taken by another call, a newer job started).
_CHECK_LOOKS = 3


def _chat_job(system_id: str) -> dict:
    with _CHAT_JOBS_LOCK:
        return dict(_CHAT_JOBS.get(system_id) or {})


def _update_chat_job(system_id: str, job_id: str, **fields) -> None:
    """Update the record of job ``job_id`` (another job's record is left
    alone)."""
    with _CHAT_JOBS_LOCK:
        record = _CHAT_JOBS.get(system_id)
        if record is not None and record.get("job_id") == job_id:
            record.update(fields)


def _retire_chat_job(system_id: str, job_id: str, phase: "str | None" = None) -> None:
    """Remove the record of job ``job_id`` (in ``phase``, when given);
    another job's record is left alone."""
    with _CHAT_JOBS_LOCK:
        record = _CHAT_JOBS.get(system_id)
        if (record is not None and record.get("job_id") == job_id
                and (phase is None or record.get("phase") == phase)):
            del _CHAT_JOBS[system_id]


def _take_refusal(system_id: str, job_id: str, session_stamp) -> "dict | None":
    """The refusal of job ``job_id``, taken from the registry in one step:
    only while that job's record is still the current one, REFUSED, and found
    for the session as it is now (``session_stamp``, its ``updated_at``).
    Taken means given and retired, so no other call gives it. A refusal found
    for a session that has moved on since is retired, not given. None when
    nothing was taken."""
    with _CHAT_JOBS_LOCK:
        record = _CHAT_JOBS.get(system_id)
        if (record is None or record.get("job_id") != job_id
                or record.get("phase") != REFUSED):
            return None
        del _CHAT_JOBS[system_id]
        if record.get("session_stamp") != session_stamp:
            return None
        return copy.deepcopy(record["answer"])


def _check_on_the_chat_job(system_id: str, session: dict) -> "dict | None":
    """A call on a ``resolved`` session while the chat's job of it is still
    reading the schema (its record PREPARING and the provision claim held):
    a check, which waits up to :data:`CHECK_WAIT_SECONDS` for the job to get
    past that and starts nothing; then how it went on (provision_status), a
    refusal it found, or still provisioning. With no job preparing: the
    refusal the job found, once, while the session is the one it started
    from (:func:`_take_refusal`). When the record read changed meanwhile (the
    refusal taken by another call, a newer job started), the current one is
    read again: a call never answers a job that is no longer the current
    one. Else None: start one."""
    import time
    from . import onboard as ob
    deadline = None
    for _look in range(_CHECK_LOOKS):
        job = _chat_job(system_id)
        if job.get("phase") == PREPARING and ob._provision_running(system_id):
            if deadline is None:
                deadline = time.monotonic() + CHECK_WAIT_SECONDS
            if _still_preparing(system_id, job["job_id"], deadline):
                return _still_provisioning(system_id, job["job_id"])
            session = _reread(system_id)
            if session.get("status") != STATUS_RESOLVED:
                return provision_status(system_id, session, deadline)
            continue
        if job.get("phase") != REFUSED:
            return None
        taken = _take_refusal(system_id, job["job_id"], session.get("updated_at"))
        if taken is not None:
            return taken
    return None


def _still_preparing(system_id: str, job_id: str, deadline: float) -> bool:
    """Wait (until ``deadline``) while job ``job_id`` is the current record,
    PREPARING, and the provision claim is held; True when it still is."""
    import time
    from . import onboard as ob

    def _preparing() -> bool:
        job = _chat_job(system_id)
        return (job.get("job_id") == job_id and job.get("phase") == PREPARING
                and ob._provision_running(system_id))
    while _preparing():
        if time.monotonic() >= deadline:
            return True
        time.sleep(_CHECK_POLL_SECONDS)
    return False


def _still_provisioning(system_id: str, job_id: str) -> dict:
    return _with_left_out(system_id, job_id, {
        "onboarding_id": system_id, "status": STATUS_PROVISIONING,
        **({"job_id": job_id} if job_id else {}),
        "message": "Still provisioning. Call provision_system again to check."})


def _with_left_out(system_id: str, job_id: str, answer: dict) -> dict:
    """``answer`` with what the chat's job ``job_id`` left out of the
    provision (:func:`_provision_plan`), when it left anything out."""
    job = _chat_job(system_id)
    left_out = job.get("left_out") or {} if job_id and job.get("job_id") == job_id else {}
    if not left_out:
        return answer
    return {**answer, "message": str(answer.get("message") or "") + _left_out_text(left_out),
            **left_out}


def _start(system_id: str, session: dict) -> dict:
    """Refused like the wizard's provision while a removal runs or did not
    finish. Then the system's one provision claim
    (``onboard._claim_provision``, shared with the Onboard view's provision)
    is taken: refused while another provision of the system runs. From here
    the system counts as being provisioned, so a removal is refused. The job
    is started and the call answers ``provisioning`` at once; the job reads
    the schema again and checks the session against it before it claims the
    session or writes anything (:func:`_prepare_and_run`), and releases the
    claim however it ends. While the startup restore still runs in the
    background it answers ``startup_restore.refusal`` and claims nothing,
    like the 503 of the Onboard view's provision."""
    from . import onboard as ob
    from . import startup_restore
    refused = startup_restore.refusal(system_id) or ob._reset_conflict_or_none(system_id)
    if refused:
        return refused
    job_id = uuid.uuid4().hex[:8]
    with _CHAT_JOBS_LOCK:
        # The claim and the job's record are taken together, so a call that
        # sees the claim held also sees the record (and answers this job).
        try:
            claim = ob._claim_provision(system_id)
        except ob.ProvisionBusyError:
            claim = None
        else:
            _CHAT_JOBS[system_id] = {"job_id": job_id, "phase": PREPARING,
                                     "session_stamp": session.get("updated_at")}
    if claim is None:
        return _provision_busy(system_id)
    started = False
    try:
        ob._provision_jobs[job_id] = {"status": "running"}
        threading.Thread(target=_prepare_and_run, args=(job_id, system_id, session, claim),
                         daemon=True, name=f"chat-provision-{job_id}").start()
        started = True
    finally:
        if not started:
            _retire_chat_job(system_id, job_id)
            claim.release()
    logger.info("chat provision of %s started (job %s)", system_id, job_id)
    return {"onboarding_id": system_id, "status": STATUS_PROVISIONING, "job_id": job_id,
            "message": "Provisioning started. Call provision_system again to check on it."}


def _prepare_and_run(job_id: str, system_id: str, session: dict, claim) -> None:
    """The chat's provision job. First the schema is read again and the
    session's mappings and rules are checked against it (:func:`_wizard_body`),
    with nothing claimed or written: a refusal ends the job there
    (:func:`_end_refused`). Then the session's ``resolved -> provisioning``
    claim (another request that claimed it in between provisions instead),
    and the wizard's provision job (:func:`_run_job`). The provision claim is
    released however it ends."""
    from . import onboard as ob
    try:
        built = _wizard_body(system_id, session)
    except Exception as exc:  # noqa: BLE001 - the thread's boundary: a refusal, nothing written
        built = _refused(system_id, {
            "error": f"The provision of '{system_id}' could not be prepared "
                     f"({type(exc).__name__}), so nothing was provisioned. Try again.",
            "reason": "provision_failed", "system_id": system_id})
    if isinstance(built, dict):
        _end_refused(system_id, job_id, built, claim)
        return
    body, left_out = built
    try:
        oa._store().claim_provisioning(system_id, provision_job_id=job_id)
    except DuplicateProvisionError:
        # Another request claimed it in between: that one provisions.
        logger.info("chat provision of %s (job %s): another request claimed the session",
                    system_id, job_id)
        try:
            _retire_chat_job(system_id, job_id)
            ob._provision_jobs[job_id] = {"status": "error", "error": "provision_superseded"}
        finally:
            claim.release()
        return
    except Exception as exc:  # noqa: BLE001 - the session is unchanged: a refusal
        _end_refused(system_id, job_id, _refused(system_id, {
            "error": f"The onboarding session of '{system_id}' could not be claimed for the "
                     f"provision ({type(exc).__name__}), so nothing was provisioned. Try again.",
            "reason": "session_unavailable", "system_id": system_id}), claim)
        return
    _update_chat_job(system_id, job_id, phase=STARTED, left_out=left_out)
    logger.info("chat provision of %s (job %s): the session is claimed; provisioning",
                system_id, job_id)
    _run_job(job_id, body, claim)


def _end_refused(system_id: str, job_id: str, answer: dict, claim) -> None:
    """End a chat job that found a refusal before it claimed or wrote
    anything: its record says ``refused`` (the next call gives the answer),
    and the provision claim is released."""
    from . import onboard as ob
    try:
        _update_chat_job(system_id, job_id, phase=REFUSED, answer=copy.deepcopy(answer))
        ob._provision_jobs[job_id] = {"status": "refused",
                                      "reason": str(answer.get("reason") or "")}
    finally:
        claim.release()


def _left_out_text(left_out: dict) -> str:
    parts = []
    if left_out.get("fields_left_out"):
        parts.append(f"{len(left_out['fields_left_out'])} mapped field(s) that name no column "
                     "of its schema")
    if left_out.get("rules_left_out"):
        parts.append(f"{left_out['rules_left_out']} identity rule(s) sourced in other systems")
    return (" Not provisioned with it (left out): " + "; ".join(parts) + ".") if parts else ""


def _provision_busy(system_id: str) -> dict:
    return {"error": f"'{system_id}' is already being provisioned (from the Onboard view or "
                     "an earlier request), so nothing was started. Check on it again once "
                     "that provision has finished.",
            "reason": "provisioning", "system_id": system_id}


def _wizard_body(system_id: str, session: dict):
    """``(ProvisionRequest, left_out)``: the body the wizard sends for this
    system and what of the session it leaves out, or an error dict (nothing
    claimed). The connection values are what ``GET /catalog`` gives the
    wizard; the schema is the wizard's discover's; the mappings and rules are
    the session's (:func:`_provision_plan`), joined to that discover."""
    from . import onboard as ob
    from .models import ProvisionRequest
    entry = ob.catalog.get(system_id)
    if not entry:
        return _refused(system_id, new_system_in_onboard_view(system_id))
    served = ob._served_catalog_entry(entry)
    conn_str = str(served.get("connection_string") or "")
    if "://***@" in conn_str:
        conn_str = ""  # the wizard never sends a masked value (frontend getConnString)
    cfg = dict(served.get("connection_config") or {})
    ctype = served.get("connection_type") or ""
    disc = _wizard_discover(system_id, conn_str, ctype, cfg)
    if disc.get("error") or not disc.get("tables"):
        return _refused(system_id, {
            "error": f"The schema of '{system_id}' could not be read again, so nothing was "
                     f"provisioned: {disc.get('error') or 'no tables were found'}",
            "reason": "discover_failed", "system_id": system_id})
    plan = _provision_plan(system_id, session, disc["tables"])
    if "error" in plan:
        return plan
    # The rules are the chat session's own (server-held), the ones its
    # Identity Rules checkpoint showed: stamped as approved there. They are
    # bound like a Resolve job's result, so provision checks them the one way.
    body = ProvisionRequest(
        system_id=system_id, system_name=served.get("name", ""), plant=served.get("plant", ""),
        schema=disc.get("schema_name") or served.get("schema", ""), rules=plan["rules"],
        rules_from_checkpoint=True,
        resolve_job_id=ob._bind_resolve_rules(system_id, plan["rules"]),
        mappings=plan["mappings"], connection_type=ctype or "PostgreSQL",
        connection_string=conn_str, connection_config=cfg)
    try:
        return ob._trusted_provision_body(body), plan["left_out"]
    except ValueError:
        return _refused(system_id, _schema_changed(system_id, [], []))


def _provision_plan(system_id: str, session: dict, tables: list) -> dict:
    """The session's mappings and identity rules that this provision writes,
    checked against the schema read again (``tables``), or the refusal.

    The schema the chat mapped is the one its discover stored in the session
    (``discovered_tables``). On it, a mapping row names the column it maps
    (:func:`_rows_on_schema`) and an identity rule is this system's or another
    system's (:func:`_rules_of`). A row that names no column of that schema
    was never a field of the system (the wizard's map step leaves such a row
    out too); it is left out and logged, and so is a rule sourced in another
    system: provision writes every rule it is given as sourced in this
    system. Every mapped column and every source of this system's rules must
    still be in the schema read again: a provision of only part of what was
    confirmed is refused."""
    as_mapped = [t for t in session.get("discovered_tables") or [] if isinstance(t, dict)]
    rows, not_columns = _rows_on_schema(session.get("mappings") or [], as_mapped or tables)
    rules, others = _rules_of(system_id, list(session.get("rules") or []), as_mapped or tables)
    mappings, missing = _joined_mappings(rows, tables)
    missing_rules = _rule_sources_missing(rules, tables)
    if missing or missing_rules:
        return _refused(system_id, _schema_changed(system_id, missing, missing_rules))
    if not mappings:
        return _refused(system_id, _no_mapped_columns(system_id, not_columns))
    if not_columns:
        logger.warning("chat provision of %s: %d mapped field(s) name no column of the schema "
                       "discover read, left out: %s", system_id, len(not_columns),
                       ", ".join(not_columns[:20]))
    if others:
        logger.info("chat provision of %s: %d identity rule(s) sourced in other systems left "
                    "out: %s", system_id, len(others),
                    ", ".join(_rule_label(r) for r in others[:20]))
    left_out = {**({"fields_left_out": not_columns[:20]} if not_columns else {}),
                **({"rules_left_out": len(others)} if others else {})}
    return {"mappings": mappings, "rules": rules, "left_out": left_out}


def _refused(system_id: str, answer: dict) -> dict:
    """Log a refused chat provision (nothing claimed or started) and return
    its answer."""
    logger.warning("chat provision of %s refused: reason=%s fields_not_found=%s "
                   "rules_not_found=%s", system_id, answer.get("reason"),
                   answer.get("fields_not_found") or [], answer.get("rules_not_found") or [])
    return answer


def _schema_changed(system_id: str, fields: list, rules: list) -> dict:
    """The refusal when the schema read again no longer has everything the
    chat confirmed at map and resolve (nothing was claimed or started)."""
    gone = []
    if fields:
        gone.append("mapped fields no longer in it: " + ", ".join(fields[:20]))
    if rules:
        gone.append("identity rules name source fields not in it: " + ", ".join(rules[:20]))
    what = "; ".join(gone) or "none of its mapped fields are in it"
    return {"error": f"The schema of '{system_id}' changed since it was mapped ({what}), so "
                     "nothing was provisioned. " + _start_over(system_id),
            "reason": "schema_changed", "system_id": system_id,
            **({"fields_not_found": fields[:20]} if fields else {}),
            **({"rules_not_found": rules[:20]} if rules else {})}


def _no_mapped_columns(system_id: str, fields: list) -> dict:
    """The refusal when no mapping row of the session names a column of the
    schema its discover read."""
    which = f" ({', '.join(fields[:20])})" if fields else ""
    return {"error": f"None of the fields mapped for '{system_id}'{which} name a column of the "
                     "schema its discover read, so nothing was provisioned. "
                     + _start_over(system_id),
            "reason": "no_mapped_columns", "system_id": system_id,
            **({"fields_not_found": fields[:20]} if fields else {})}


def _start_over(system_id: str) -> str:
    """How a refused or failed chat provision of ``system_id`` is started
    over (``onboard_agent._start_over_advice``): reset_system, then the
    onboarding steps again, for a system that is not onboarded; never
    reset_system for an onboarded one, which it removes completely."""
    return oa._start_over_advice(
        system_id, "Call reset_system to start it over, then run discover, map_fields and "
                   "resolve_identities again.")


def _wizard_discover(system_id: str, conn_str: str, ctype: str, cfg: dict) -> dict:
    """The wizard's ``POST /discover`` (``onboard.onboard_discover``: the reset
    check, the connector's discover and the provenance it records) with the
    body ``buildDiscoverBody`` sends, on a private event loop (this tool runs
    in the chat's worker thread)."""
    from . import onboard as ob
    from .models import DiscoverRequest
    fields = {"system_id": system_id, "connection_config": cfg}
    if conn_str:
        fields["connection_string"] = conn_str
    if ctype:
        fields["connection_type"] = ctype
    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(ob.onboard_discover(DiscoverRequest(**fields)))
    except Exception as exc:  # noqa: BLE001 - the connector's own failure: say it, change nothing
        logger.warning("chat provision of %s: discover failed: %s", system_id, type(exc).__name__)
        return {"error": f"the discover failed ({type(exc).__name__})"}
    finally:
        loop.close()
    return result if isinstance(result, dict) else {"error": "the discover returned nothing"}


def _columns_by_table(tables: list) -> dict:
    return {str(t.get("name") or ""): {str(c.get("name") or "")
                                       for c in t.get("columns") or [] if isinstance(c, dict)}
            for t in tables or [] if isinstance(t, dict)}


def _table_named(name: str, columns: dict) -> "str | None":
    """The discovered table ``name`` names: itself, or the one gateway tool
    whose name is ``<target>___<name>`` (a model may drop the gateway's
    target prefix). None when there is none, or more than one."""
    if name in columns:
        return name
    tools = [t for t in columns if t.endswith(_GATEWAY_TOOL_SEP + name)] if name else []
    return tools[0] if len(tools) == 1 else None


def _column_named(name: str, columns: set) -> "str | None":
    """The column ``name`` names: itself, or the one column that ends with
    it on a dotted segment boundary (a nested column named by its last
    segments: ``schedule`` for ``prompts.schedule``). None when there is
    none, or more than one."""
    if name in columns:
        return name
    nested = [c for c in columns if c.endswith("." + name)] if name else []
    return nested[0] if len(nested) == 1 else None


def _rows_on_schema(rows: list, tables: list) -> tuple:
    """``(rows, not_columns)``: copies of the mapping ``rows`` that name a
    column of ``tables``, each spelled ``<table>.<column>`` with the
    discovered names (:func:`_table_named`, :func:`_column_named`), and the
    fields of the rows that name none. A row that is not a dict is kept as it
    is (the join refuses it)."""
    columns = _columns_by_table(tables)
    kept, not_columns = [], []
    for row in copy.deepcopy(list(rows or [])):
        if not isinstance(row, dict):
            kept.append(row)
            continue
        field = str(row.get("field") or "")
        named = str(row.get("table") or "") or field.split(".", 1)[0]
        table = _table_named(named, columns)
        rest = field
        for prefix in (named, table):
            if prefix and rest.startswith(prefix + "."):
                rest = rest[len(prefix) + 1:]
                break
        column = _column_named(rest, columns[table]) if table else None
        if column is None:
            not_columns.append(field or str(row.get("column_name") or ""))
            continue
        row["table"], row["field"] = table, f"{table}.{column}"
        kept.append(row)
    return kept, not_columns


def _rules_of(system_id: str, rules: list, tables: list) -> tuple:
    """``(this system's rules, other systems' rules)``. A rule is this
    system's when it names this system as its ``source_system``; without one,
    when its ``source_table`` is a table of ``tables`` (without a table, when
    its ``source_field`` names a column of them, :func:`_rule_sources_missing`).
    Any other rule (another ``source_system``, another system's table or
    field) is another system's. Rules that are not dicts stay with this
    system's (provision skips them)."""
    columns = _columns_by_table(tables)
    own, others = [], []
    for rule in rules or []:
        if not isinstance(rule, dict):
            own.append(rule)
            continue
        source_system = str(rule.get("source_system") or "").strip()
        table = str(rule.get("source_table") or "").strip()
        if source_system:
            mine = source_system == system_id
        elif table:
            mine = _table_named(table, columns) is not None
        else:
            mine = not _rule_sources_missing([rule], tables)
        (own if mine else others).append(rule)
    return own, others


def _rule_label(rule) -> str:
    if not isinstance(rule, dict):
        return "(a rule that is not an object)"
    where = ".".join(p for p in (str(rule.get("source_system") or ""),
                                 str(rule.get("source_table") or "")) if p)
    return f"{where}:{rule.get('source_field') or ''}" if where else str(
        rule.get("source_field") or "")


def _joined_mappings(rows: list, tables: list) -> tuple:
    """The mapping ``rows`` joined to the discovered schema by the wizard map
    job's step (``onboard._attach_map_provenance``: a row whose column is not
    in the schema is left out), and the fields left out. That step writes each
    row it keeps as ``<table>.<column>``, so a kept row is known by the row
    itself, not by its field."""
    from . import onboard as ob
    rows = copy.deepcopy(list(rows or []))
    fields = [(id(m), str(m.get("field") or "")) for m in rows if isinstance(m, dict)]
    try:
        joined = ob._attach_map_provenance(
            {"mappings": rows}, ob._mapping_schema_metadata(tables))["mappings"]
    except ValueError:
        return [], [f for _i, f in fields] or ["(a mapping row that names no field)"]
    kept = {id(m) for m in joined}
    return joined, [f for i, f in fields if i not in kept]


def _rule_sources_missing(rules: list, tables: list) -> list:
    """The source fields of ``rules`` that are not in the discovered
    ``tables``. A rule's ``source_field`` names a column of its
    ``source_table`` (any table when the rule names none; a named table must
    be one of them, :func:`_table_named`) when, on dotted segment boundaries,
    the field ends with the whole column name (any table or qualifier prefix:
    ``t.col``, ``t.t.col``) or the column ends with the whole field (the
    column's own last segments: ``code`` for ``header.code``). So a dotted
    column is never matched by its last segment alone: ``status.code`` is
    missing while only ``header.code`` exists."""
    columns = _columns_by_table(tables)
    missing = []
    for rule in rules or []:
        if not isinstance(rule, dict):
            continue
        field = str(rule.get("source_field") or "").strip()
        table = str(rule.get("source_table") or "").strip()
        named = [_table_named(table, columns) or table] if table else list(columns)
        if field and not any(_names_column(field, col)
                             for name in named for col in columns.get(name, set())):
            missing.append(field)
    return missing


def _names_column(field: str, column: str) -> bool:
    return bool(column) and (field == column or field.endswith("." + column)
                             or column.endswith("." + field))


def _run_job(job_id: str, body, claim) -> None:
    """The wizard's provision job (``onboard._run_provision_job``) for the chat:
    its steps, then the session's terminal status, then the provision claim
    released, then the job record published, however the steps end (an
    exception that escapes them is a ``failed`` session and an ``error``
    record). A chat that reads the session finished can remove the system at
    once. The steps check the claim's provision lease before each write
    (``claim.guard()``); a lost lease is the retryable
    ``provision_lease_lost``."""
    from . import onboard as ob
    from .reset_lease import LeaseLostError
    record = {"status": "error", "error": "provision_failed"}
    try:
        with claim.guard():
            record = ob._run_provision_job_steps(job_id, body)
    except LeaseLostError:
        logger.error("chat provision job %s stopped: its provision lease was lost", job_id)
        record = ob._provision_lease_lost_record()
    except Exception as exc:  # noqa: BLE001 - the thread's boundary: report it as the job's end
        logger.warning("chat provision job %s stopped: %s", job_id, type(exc).__name__)
    finally:
        try:
            _finalize(body.system_id, job_id, record)
        finally:
            claim.release()
            ob._provision_jobs[job_id] = record


def _finalize(system_id: str, job_id: str, record: dict) -> None:
    """Write the session's terminal status from a job record (conditional on
    ``provisioning``: a second writer changes nothing)."""
    store = oa._store()
    try:
        if record.get("status") == "done":
            store.finalize(system_id, STATUS_ONBOARDED, provision_job_id=job_id)
        else:
            store.finalize(system_id, STATUS_FAILED, provision_job_id=job_id,
                           failure_reason=_failure_text(record, system_id),
                           failure_code=str(record.get("reason") or record.get("error")
                                            or "provision_failed"))
    except Exception as exc:  # noqa: BLE001 - logged; the poll settles it from the job record
        logger.warning("chat provision of %s: session not finalized: %s",
                       system_id, type(exc).__name__)


def _failure_text(record: dict, system_id: str) -> str:
    """The failure reason a failed chat provision of ``system_id`` records:
    the job's own detail, else what happened and how to start the system
    over, never by reset_system for an onboarded system
    (``onboard_agent._start_over_advice``)."""
    if record.get("detail"):
        return str(record["detail"])
    if record.get("error") == "provision_interrupted":
        return ("The provision stopped before it finished (the app restarted). "
                + oa._start_over_advice(system_id, "Call reset_system to start this system "
                                                   "over, then onboard it again."))
    return "The provision failed. " + oa._start_over_advice(
        system_id, "Call reset_system to start this system over, then onboard it again.")


# ── checking on a provision ─────────────────────────────────────────────────

def provision_status(system_id: str, session: dict, deadline: "float | None" = None) -> dict:
    """The answer for a session past the claim. While the provision runs in
    this process the check waits up to :data:`CHECK_WAIT_SECONDS` (or until
    ``deadline``, a ``time.monotonic()`` value) for it to end, so a model
    checking in a loop asks a few times a minute, not many times a second. A
    ``provisioning`` session whose provision no longer runs here (the app
    restarted, or its terminal write failed) is settled from its job record
    first."""
    status = session.get("status")
    job_id = str(session.get("provision_job_id") or "")
    if status == STATUS_PROVISIONING:
        from . import onboard as ob
        if not job_id or _wait_while_running(system_id, deadline):
            return _still_provisioning(system_id, job_id)
        session = _reread(system_id)
        if session.get("status") == STATUS_PROVISIONING and not ob._provision_running(system_id):
            session = _settle(system_id, job_id)
        status = session.get("status")
    if status == STATUS_ONBOARDED:
        return _final_answer(system_id, job_id, _onboarded_answer(system_id, job_id))
    if status == STATUS_FAILED:
        return _final_answer(system_id, job_id, {
            "onboarding_id": system_id, "status": STATUS_FAILED,
            "error": str(session.get("failure_reason") or _failure_text({}, system_id)),
            "reason": str(session.get("failure_code") or "provision_failed")})
    return {"error": _NOT_RESOLVED}


def _final_answer(system_id: str, job_id: str, answer: dict) -> dict:
    """How the provision ended, with what the chat's job ``job_id`` left out
    of it; that job's record is retired, so a later answer has no counts."""
    answer = _with_left_out(system_id, job_id, answer)
    if job_id:
        _retire_chat_job(system_id, job_id, phase=STARTED)
    return answer


def _wait_while_running(system_id: str, deadline: "float | None" = None) -> bool:
    """Wait (up to :data:`CHECK_WAIT_SECONDS`, or until ``deadline``) while a
    provision of ``system_id`` runs in this process; True when it is still
    running."""
    import time
    from . import onboard as ob
    if deadline is None:
        deadline = time.monotonic() + CHECK_WAIT_SECONDS
    while ob._provision_running(system_id):
        if time.monotonic() >= deadline:
            return True
        time.sleep(_CHECK_POLL_SECONDS)
    return False


def _reread(system_id: str) -> dict:
    try:
        return oa._store().get_session(system_id)
    except SessionNotFoundError:
        return {}


def _settle(system_id: str, job_id: str) -> dict:
    from . import onboard as ob
    record = ob._provision_jobs.get(job_id) or {}
    if record.get("status") not in ("done", "error"):
        logger.warning("chat provision of %s (job %s) is no longer running; recorded as "
                       "interrupted", system_id, job_id)
        record = {"status": "error", "error": "provision_interrupted"}
    _finalize(system_id, job_id, record)
    return _reread(system_id)


def _onboarded_answer(system_id: str, job_id: str) -> dict:
    """What the provision wrote (the wizard's finished-job payload), or the
    wizard's own answer when the job record is gone: completion confirmed,
    statistics unavailable."""
    from . import onboard as ob
    job = ob._provision_jobs.get(job_id) if job_id else None
    if isinstance(job, dict) and job.get("status") == "done":
        stats = ob._finished_provision_payload(job)
    else:
        stats = {"stats_unavailable": True, "tool_name": f"query_{system_id.replace('-', '_')}"}
    return {**stats, "onboarding_id": system_id, "status": STATUS_ONBOARDED,
            **({"job_id": job_id} if job_id else {})}
