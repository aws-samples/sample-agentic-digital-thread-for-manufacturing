# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""REST vendor-adapter registry (Model 1).

The ``REST API`` connection type is a *generic-REST base* with vendor **adapters**
as equal peers, plus i3X expressed as a registry adapter detected by its own
connection-type string (``SiteWise (i3X)``). This collapses the per-lifecycle
``_is_tulip`` short-circuits and the historical "REST API == i3X" grouping into a
single dispatch keyed off :func:`resolve_vendor`: provision and runtime call the
owning adapter's hook (``provision_agent`` / ``runtime_tools``); discover asks
:func:`resolve_vendor` for the owning vendor and branches on its name (the two
discover entry points build their entry/resolved shapes differently, so they
route by resolved vendor rather than share a hook).

Design (see ``aidlc/inception/domain-design/domain-design.md``):

* **i3x** — detected by ``connection_type == "SiteWise (i3X)"``. Hooks delegate to
  the *existing* i3X functions so i3X behaviour stays byte-for-byte identical;
  i3X keeps its own connection-type string (it is NOT reclassified as REST).
* **tulip** — detected by the ``Tulip (Tables API)`` connection-type string
  (a first-class type, not a ``*.tulip.co`` host sniff — so a
  self-hosted/proxied Tulip on any internal DNS name is recognized). Hooks
  delegate to the Tulip Tables-API functions.
* **generic** — the neutral fallback for any other ``REST API`` system. Discover
  uses the generic OpenAPI parse; provision/runtime raise an honest
  :class:`RestVendorUnsupported` rather than fabricating i3X behaviour (a real
  generic-REST runtime toolset is deferred until such a source is onboarded —
  no generic-REST system exists in the demo today).

``resolve_vendor`` returns the first adapter whose ``detect`` is true (order:
i3x, tulip, generic), else ``None`` for non-REST/i3X connection types, which are
handled by their own (PostgreSQL / Athena / SiteWise-Direct / MCP) paths.

Every hook imports its delegate **lazily** (inside the function body) so this
module has no import-time dependency on ``onboard.py`` / ``onboard_agent.py`` /
``system_agent.py`` — those modules import :func:`resolve_vendor` from here, so
a top-level import would create a cycle.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from .connection_types import REST_API, SITEWISE_I3X, TULIP


class RestVendorUnsupported(NotImplementedError):
    """Raised when a REST vendor has no implementation for a lifecycle hook.

    Used by the ``generic`` adapter's provision/runtime hooks: an unmatched
    generic-REST source is a real (honest) gap, not a silent fall-through to
    i3X. Building a generic-REST provision + runtime toolset is deferred until a
    generic-REST system is actually onboarded.
    """


@dataclass(frozen=True)
class RestVendorAdapter:
    """One REST-family vendor's lifecycle hooks.

    All hooks are pure references (no network at import). ``detect`` selects the
    adapter; the lifecycle hooks (``provision_agent`` / ``runtime_tools``)
    delegate to the existing per-vendor implementations. ``telemetry_rich``
    single-sources the map-prompt policy (read via ``resolve_vendor`` in
    ``onboard._is_telemetry_rich``). Each vendor's default auth scheme lives
    inside its own hooks (they are the only consumers), not on a field here.
    """

    name: str
    telemetry_rich: bool
    detect: Callable[[str, str, Optional[dict]], bool]
    # Discover is a registry hook too, so a new REST vendor
    # is truly "one entry" — both discover entry points dispatch through
    # ``adapter.discover(system_id, entry, resolved)`` instead of branching on
    # adapter.name. Each hook adapts the uniform args to what its underlying
    # discovery function needs.
    discover: Callable[..., object]
    provision_agent: Callable[..., object]
    runtime_tools: Callable[..., object]
    # ----- OPTIONAL hooks (default None → today's generic behavior) ------------
    # These extend the registry along the SAME onboarding-phase structure that
    # justified adding ``discover``: a phase exists, but a per-vendor variation
    # was previously hand-edited outside the registry. Each defaults to ``None``
    # so the existing i3x/tulip/generic adapters stay BYTE-IDENTICAL (they omit
    # these fields) and only a vendor that needs the behavior supplies it.
    #
    # Register: shape the raw submitted connection_config into the dict
    # stored in Secrets Manager (``adt/conn/<id>``). None → the generic
    # ``{external_id, token}`` allowlist in ``_store_registration_secret``.
    # (Tulip's api_key+api_secret→base64 branch could move here too, but is
    # left in place for now.)
    encode_registration_secret: Optional[Callable[[dict], dict]] = None
    # Resolve: return up to ``limit`` distinct sample values for a field,
    # for cross-system value-overlap join evidence. None → ``sample_field_values``
    # falls through to its generic ``[]`` (today's behavior for REST/tulip).
    sample_values: Optional[Callable[..., list]] = None
    # Resolve: which of ``values`` EXIST in one field of this vendor's system
    # (``list``; ``[]`` = probed, none found) or ``None`` when it cannot answer.
    # Feeds ``resolve_evidence.probe_field_values`` so the deterministic
    # exact-agreement pass can confirm a join on this vendor's side. None →
    # the vendor cannot be probed (the pair fails closed, today's behavior).
    membership_probe: Optional[Callable[..., Optional[list]]] = None
    # Preflight and Test: a per-vendor reachability probe. None → the shared
    # ``_probe_rest`` (health GET). A vendor whose liveness is NOT a health GET
    # (for example an OAuth token exchange) supplies its own.
    probe: Optional[Callable[[dict], str]] = None
    # Whether the deterministic readers (the dashboard's section renderer and
    # the metric fan-out) can read this vendor's systems. Both compose SQL and
    # have no REST reader; only i3X has a dashboard renderer of its own. False
    # (the default) leaves a new vendor's systems off the dashboard instead of
    # listing them as failed sites, with no declaration elsewhere
    # (connection_types.structured_read_supported reads this flag).
    structured_reader: bool = False
    # Equipment pass: list the records of one entity, so the system can be an
    # equipment registry like a SQL one (the tag pick's evidence, the
    # telemetry match and the cross-registry name match read the records;
    # ``equipment_api_registry``). ``listable_entities`` names the entities
    # (discover table names) the hook lists, and only fields on those can be
    # the registry's tag. ``list_records(system_id, entity, *, page_size,
    # max_rows, deadline)`` returns ``{"rows": [record dict], "partial": bool}``
    # (``partial``: more records exist than were returned, at most
    # ``max_rows``) and raises when the read fails. ``deadline`` is a
    # ``time.monotonic()`` value (``None``: no time bound): every request and
    # retry ends by then, and a listing that cannot finish by then raises or
    # answers ``partial``. Unset (the default): the pass never reads the
    # vendor's systems.
    listable_entities: tuple = ()
    list_records: Optional[Callable[..., dict]] = None


# ---------------------------------------------------------------------------
# i3x adapter — delegates to the existing i3X functions (byte-identical).
# ---------------------------------------------------------------------------
def _i3x_detect(connection_type: str, base_url: str,
                entry: Optional[dict] = None) -> bool:
    return connection_type == SITEWISE_I3X


def _i3x_provision_agent(ctx, base_url: str, token: str, auth_scheme: str,
                         secret_arn: str):
    from .onboard import _dp_agent_i3x_native, _dp_i3x_url_error
    err = _dp_i3x_url_error(base_url)
    if err is not None:
        return err
    return _dp_agent_i3x_native(ctx, base_url, token, auth_scheme, secret_arn)


def _i3x_runtime_tools(base_url: str, secret_arn: str, token: str,
                       auth_scheme: str, connection_config: Optional[dict] = None):
    # connection_config is accepted (and ignored) for a uniform runtime_tools
    # call site; an adapter may read it for non-secret fields.
    from .system_agent import _make_i3x_tools
    return _make_i3x_tools(base_url, secret_arn, token, auth_scheme or "bearer")


def _i3x_discover(system_id: str, entry: dict, resolved: dict):
    # i3X is NOT dispatched through the REST-family discover
    # hook — it has its OWN `connection_type == 'SiteWise (i3X)'` branch in both
    # discover entry points (its data-model discovery differs from the generic
    # OpenAPI parse and from Tulip). This hook exists only to satisfy the
    # dataclass contract; reaching it is a wiring bug.
    raise NotImplementedError(
        "i3X discover is dispatched by its own connection-type branch, "
        "not the REST-vendor discover hook"
    )


# ---------------------------------------------------------------------------
# tulip adapter — delegates to the Tulip Tables-API functions.
# ---------------------------------------------------------------------------
def _tulip_detect(connection_type: str, base_url: str,
                  entry: Optional[dict] = None) -> bool:
    # Tulip identity is the DECLARED connection type, not a `*.tulip.co`
    # host sniff — so a self-hosted/proxied Tulip on any internal DNS name is
    # recognized, and runtime re-detection (which lacks the catalog entry) can
    # never disagree with provision. base_url/entry are unused for identity.
    return connection_type == TULIP


def _tulip_provision_agent(ctx, base_url: str, token: str, auth_scheme: str,
                           secret_arn: str):
    from .onboard import _dp_agent_tulip
    return _dp_agent_tulip(ctx, base_url, token, secret_arn)


def _tulip_runtime_tools(base_url: str, secret_arn: str, token: str,
                         auth_scheme: str, connection_config: Optional[dict] = None):
    # Tulip reads the OPTIONAL non-secret ``workspace_id`` and ``key_scope`` from
    # the passed connection_config (the full non-secret node config flows through
    # here): an instance-scoped key scopes every call to /api/v3/w/<id>/...; a
    # workspace-scoped key uses the unscoped path. The ``credential_source``
    # marker (``"secret_arn"`` for an ARN-sourced seat system, absent/"" for an
    # inline 'API key and secret' system whose secret_arn is its own
    # adt/conn/<id>) tells the client whether to read the seat secret fresh at
    # use time or to use the snapshot / own-secret token. For a seat-backed
    # system the client reads routing + credential together from a fresh
    # seat-secret read at use time.
    # An OPTIONAL non-secret ``table_label_prefix`` keeps the tools to the
    # tables whose label starts with it (a seat-backed client reads the seat's
    # own at use time); passed only when set, so an unscoped system's tools are
    # built exactly as before.
    from .system_agent import _make_tulip_tools
    cfg = connection_config or {}
    scope = ({"table_label_prefix": cfg["table_label_prefix"]}
             if cfg.get("table_label_prefix") else {})
    return _make_tulip_tools(base_url, secret_arn, token, auth_scheme or "basic",
                             workspace_id=cfg.get("workspace_id", ""),
                             key_scope=cfg.get("key_scope", ""),
                             system_id=cfg.get("system_id", ""),
                             credential_source=cfg.get("credential_source", ""),
                             **scope)


def _tulip_discover(system_id: str, entry: dict, resolved: dict):
    # Tulip discovery is the registry's job now — callers
    # invoke adapter.discover(...) with a UNIFORM (system_id, entry, resolved)
    # and each hook adapts internally, so the dispatcher never branches on the
    # vendor. Tulip's Tables-API discovery keys off the system id + resolved
    # connection; it only needs {'id': system_id} as its entry.
    from .onboard_agent import _discover_tulip
    return _discover_tulip({'id': system_id}, resolved)


def _tulip_sample_values(system_id: str, connection_config: dict,
                         field: str = "", limit: int = 5, table: str = "") -> list:
    # Tulip writes NO graph Asset nodes either, so its cross-system join keys
    # (run numbers, order ids, equipment tags that live in Tulip Table columns)
    # are not reachable by the SiteWise asset-name sampler — this hook feeds the
    # identity-rules step. One bounded, read-only GET of
    # a Tulip table's records returns up to ``limit`` distinct non-empty values
    # of ``field`` (the column), scoped to the resolved workspace. ``table`` names
    # the Tulip table (its id resolved from the saved discovery). Never raises
    # here — the caller's best-effort ``[]`` swallows any error.
    from .resolve_evidence import _sample_tulip_records
    return _sample_tulip_records(system_id, connection_config or {}, limit, field, table)


def _tulip_membership_probe(system_id: str, table: str, field: str, values) -> Optional[list]:
    # The probe twin of _tulip_sample_values: which of ``values`` exist in the
    # Tulip column, so the deterministic exact-agreement pass can confirm a
    # Tulip join instead of failing it closed. Bounded, read-only, workspace
    # scoped; None when it cannot answer.
    from .resolve_evidence import _probe_tulip_records
    return _probe_tulip_records(system_id, table, field, values)


# ---------------------------------------------------------------------------
# generic adapter — neutral REST fallback (matches any REST API host last).
# ---------------------------------------------------------------------------
def _generic_detect(connection_type: str, base_url: str,
                    entry: Optional[dict] = None) -> bool:
    return connection_type == REST_API


def _generic_provision_agent(ctx, base_url: str, token: str, auth_scheme: str,
                             secret_arn: str):
    raise RestVendorUnsupported(
        f"No provision adapter for REST host {base_url!r}. Register a vendor "
        "adapter in REST_VENDOR_REGISTRY (generic-REST provisioning is not yet "
        "implemented)."
    )


def _generic_runtime_tools(base_url: str, secret_arn: str, token: str,
                           auth_scheme: str, connection_config: Optional[dict] = None):
    raise RestVendorUnsupported(
        f"No runtime toolset for REST host {base_url!r}. Register a vendor "
        "adapter in REST_VENDOR_REGISTRY (generic-REST query tools are not yet "
        "implemented)."
    )


def _generic_discover(system_id: str, entry: dict, resolved: dict):
    # Generic REST discovery — the OpenAPI-spec parse.
    # Uniform (system_id, entry, resolved) contract: generic builds its
    # OpenAPI rest_entry from entry+resolved, then parses. Unlike
    # provision/runtime (which raise RestVendorUnsupported), generic discover
    # IS implemented, so a generic REST source is discoverable even though its
    # provision/runtime toolset is not yet built.
    from .onboard_agent import _discover_openapi, _discover_rest_entry
    return _discover_openapi(_discover_rest_entry(entry, resolved))


# ---------------------------------------------------------------------------
# The registry (ordered; first match wins). A 3rd/4th REST source is one entry.
# ---------------------------------------------------------------------------
REST_VENDOR_REGISTRY: list[RestVendorAdapter] = [
    RestVendorAdapter(
        name="i3x",
        telemetry_rich=True,
        detect=_i3x_detect,
        discover=_i3x_discover,  # Not dispatched here (own type branch); satisfies the contract
        provision_agent=_i3x_provision_agent,
        runtime_tools=_i3x_runtime_tools,
        structured_reader=True,  # entity_render has its own i3X section renderer
    ),
    RestVendorAdapter(
        name="tulip",
        telemetry_rich=False,
        detect=_tulip_detect,
        discover=_tulip_discover,  # Tulip Tables-API discovery via the registry hook
        provision_agent=_tulip_provision_agent,
        runtime_tools=_tulip_runtime_tools,
        # Tulip join keys live in Tulip Table columns (no graph Asset nodes), so
        # it supplies the sample_values hook that feeds cross-system identity-rule
        # evidence.
        sample_values=_tulip_sample_values,
        # ...and the membership probe the exact-agreement pass needs to CONFIRM
        # such a join (without it the Tulip side cannot answer and fails closed).
        membership_probe=_tulip_membership_probe,
    ),
    RestVendorAdapter(
        name="generic",
        telemetry_rich=False,
        detect=_generic_detect,
        discover=_generic_discover,  # Generic OpenAPI discovery via the registry hook
        provision_agent=_generic_provision_agent,
        runtime_tools=_generic_runtime_tools,
    ),
]


def resolve_vendor(connection_type: str, base_url: str = "",
                   entry: Optional[dict] = None) -> Optional[RestVendorAdapter]:
    """Return the REST vendor adapter owning ``(connection_type, base_url)``.

    First match in :data:`REST_VENDOR_REGISTRY` wins (i3x → tulip → generic),
    each keyed on the DECLARED connection-type string (tulip is the
    ``Tulip (Tables API)`` type, no longer a ``*.tulip.co`` host sniff, so
    ``base_url``/``entry`` are not consulted for identity today). Returns
    ``None`` for non-REST/i3X/Tulip connection types (PostgreSQL, Athena,
    SiteWise-Direct, MCP), which keep their own dispatch paths.
    """
    for adapter in REST_VENDOR_REGISTRY:
        if adapter.detect(connection_type, base_url or "", entry):
            return adapter
    return None
