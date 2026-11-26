# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""System catalog and concept mappings."""

import functools
import json
import logging
import re
import threading

# NOTE: ``neptune_query`` is imported LAZILY inside the functions that use it
# (get_all_concepts / concepts_prompt_block / get_schema / get_name) so this foundational
# vocabulary module imports
# WITHOUT app-runtime deps (pg8000). The one-script deploy seeds the core vocabulary by
# importing this module in the CDK-only venv (ADR 0020) — a module-level ``from .db import``
# would re-break that. Do not add it back.

# S5.2 two-layer concept tagging (ADR 0021) was extracted to ``concept_tagging`` to keep this
# module under its size cap. Re-export the moved names so existing ``from .catalog import ...``
# call sites (onboard.py, concept_layer.py, tests) keep working unchanged. ``concept_tagging`` is
# self-contained and does NOT import ``catalog`` (no circular import). ``_ENTITY_CONCEPTS`` is
# pulled in to derive CONCEPT_LAYER below.
from .concept_tagging import (  # noqa: F401  (re-exported for backward-compatible imports)
    _ENTITY_CONCEPTS,
    ENTITY_CONCEPT_IDS,
    GENERIC_FIELD_NAMES,
    is_generic_field,
    TABLE_ENTITY_MAP,
    TABLE_EXCLUDE,
    is_excluded_table,
    ENTITY_MAPPED_SCHEMAS,
    entity_for_table,
)

import os


def _get_highbyte_url() -> str:
    """Read HighByte URL from env var, falling back to SSM.

    Pure lazy read (Step 3, Task 3.1): env first, then SSM — does NOT write the
    resolved value back into ``os.environ``. The catalog must not act as a stale
    live cache, so each call reads fresh with no global env mutation. Intended
    only as a seed/registration-time bootstrap, never a steady-state runtime
    connection read.
    """
    url = _highbyte_url_value()
    return "" if url in _HIGHBYTE_URL_SENTINELS else url


#: "placeholder": HighByte not shipped (deploy.sh's seed). "pending": the hub
#: is shipped and its URL is written once the hub's stack finishes, after the
#: app task starts.
_HIGHBYTE_PENDING = "pending"
_HIGHBYTE_URL_SENTINELS = frozenset({"placeholder", _HIGHBYTE_PENDING})
#: The SSM fallback runs at startup registration and on onboarding: one
#: request (botocore ``total_max_attempts``; ``max_attempts`` counts retries)
#: with short timeouts, so an SSM network problem costs seconds, not minutes.
_HIGHBYTE_SSM_CONFIG = {"connect_timeout": 2, "read_timeout": 2,
                        "retries": {"total_max_attempts": 1}}


def _highbyte_url_value() -> str:
    """The HighByte quick-link value: env first, then SSM while the env value
    is empty or a sentinel. A URL comes back without its trailing slash, a
    sentinel as itself, nothing (or an SSM failure) as ""."""
    url = os.environ.get("HIGHBYTE_URL", "").strip()
    if url and url not in _HIGHBYTE_URL_SENTINELS:
        return url.rstrip("/")
    env_value = url
    try:
        import boto3
        from botocore.config import Config
        region = os.environ.get("AWS_REGION", "us-east-1")
        ssm = boto3.client("ssm", region_name=region, config=Config(**_HIGHBYTE_SSM_CONFIG))
        resp = ssm.get_parameter(Name="/adt/highbyte-url")
        url = resp.get("Parameter", {}).get("Value", "").strip()
        if url:
            return url if url in _HIGHBYTE_URL_SENTINELS else url.rstrip("/")
    except Exception:  # nosec B110 - intentional fallback, no recovery action needed
        pass
    return env_value


def _highbyte_declared() -> bool:
    """True iff the deployment ships HighByte: a real URL, or the workshop
    root's "pending" (shipped, URL not written yet).

    A task-start "pending" answers with NO SSM read: this runs in the app's
    startup (the demo-system registration), where a slow SSM would hold the
    health check, and "pending" already says the hub ships."""
    env = os.environ.get("HIGHBYTE_URL", "").strip()
    if env.lower() == _HIGHBYTE_PENDING or (env and env not in _HIGHBYTE_URL_SENTINELS):
        return True
    value = _highbyte_url_value()
    return bool(value) and value != "placeholder"


def _get_highbyte_token() -> str:
    """Read HighByte MCP token from env var, falling back to the canonical secret.

    Pure lazy read (Step 3, Task 3.1): env first, then Secrets Manager — does
    NOT cache the resolved value into ``os.environ`` (see ``_get_highbyte_url``).
    Each call reads fresh; no global env mutation. Seed/registration bootstrap
    only, never a steady-state runtime connection read.

    Step 3 cut-over (T5.1): the legacy ``adt/highbyte-mcp-token``
    bare-string secret is GONE. The fallback now reads the canonical
    ``adt/conn/highbyte`` secret (JSON ``{"token": ...}``, minted by
    ``connection_resolver.write_connection`` / ``PUT /api/admin/highbyte-token``)
    and extracts the token. No legacy-secret reader remains.
    """
    return _highbyte_env_token() or _get_canonical_highbyte_token()[0]


def _highbyte_env_token() -> str:
    """The HighByte MCP token from the task env (``HIGHBYTE_I3X_TOKEN``), ``""``
    when unset or the ``placeholder`` sentinel."""
    token = os.environ.get("HIGHBYTE_I3X_TOKEN", "").strip()
    if token and token != "placeholder":  # nosec B105 - not a credential value (placeholder/key/secret arn)
        return token
    return ""


def _get_canonical_highbyte_token() -> tuple[str, str]:
    """``(token, secret_arn)`` from the canonical ``adt/conn/highbyte`` secret,
    ``("", "")`` when it is absent, unreadable or holds no token."""
    try:
        import json as _json
        import boto3
        region = os.environ.get("AWS_REGION", "us-east-1")
        sm = boto3.client("secretsmanager", region_name=region)
        resp = sm.get_secret_value(SecretId="adt/conn/highbyte")
        raw = (resp.get("SecretString") or "").strip()
        if raw:
            try:
                data = _json.loads(raw)
                tok = data.get("token", "") if isinstance(data, dict) else raw
            except (ValueError, TypeError):
                tok = raw  # bare-string secret → use verbatim
            if tok:
                return str(tok).strip(), str(resp.get("ARN") or "")
    except Exception:  # nosec B110 - intentional fallback, no recovery action needed
        pass
    return "", ""


def _get_highbyte_token_source() -> tuple[str, str]:
    """``(token, secret_arn)`` for the built-in HighByte connection: the
    canonical ``adt/conn/highbyte`` secret's token and ARN whenever it holds one
    (the setup Lambda's key, which it keeps verified), else the env token with
    no ARN. Provision points the node at that ARN instead of writing the token
    into a secret, so the setup Lambda's ``storedKey`` and ``friendlyName``
    stay."""
    token, arn = _get_canonical_highbyte_token()
    if token:
        return token, arn
    return _highbyte_env_token(), ""

logger = logging.getLogger(__name__)

#: The catalog status of a system whose removal started and did not finish
#: (its :System node carries ``removal_incomplete = true``): never served, and
#: its only action is to remove it again.
REMOVAL_FAILED = 'removal_failed'

#: The catalog status of a system whose removal cascade is RUNNING (from the
#: removal marker write to the catalog step that ends the cascade). The
#: system's graph data is still there while its runtime is torn down, so it
#: is neither served nor offered: not 'onboarded' (no queries, no identity
#: rule or join candidates) and not 'registered' (its card shows the removal
#: in flight, not Onboard). In-memory only: a restart rebuilds the entry from
#: the node's ``removal_incomplete`` marker as :data:`REMOVAL_FAILED`.
REMOVING = 'removing'

#: The catalog states of a system whose graph data is on its way out: a
#: removal runs or did not finish. Never a candidate for identity rules or
#: joins, whatever the graph still holds: one of the two signals
#: :func:`systems_being_removed` reads.
REMOVAL_STATUSES = frozenset({REMOVING, REMOVAL_FAILED})

#: The catalog field of a :data:`REMOVAL_FAILED` entry that records the mode
#: of the removal that did not finish: ``'full'`` (the registration is
#: forgotten and its own secret scheduled for deletion) or ``'default'``
#: (the registration is kept). Absent when the mode is not known (a marker
#: written before the mode was recorded).
REMOVAL_MODE = 'removal_mode'


def _removal_mode(full: bool) -> str:
    return 'full' if full else 'default'


#: The RETURN column a cross-system candidate read adds for each peer
#: ``(s:System)``: whether its removal has started (the graph marker). A
#: removal writes the marker before it removes anything, then keeps the
#: system's fields and values for the minutes its runtime and gateway target
#: take to go. Read through :func:`removed_ids`.
REMOVAL_STARTED_COLUMN = "coalesce(s.removal_incomplete, false) AS removal_incomplete"


def removal_started(row) -> bool:
    """Whether a row read with :data:`REMOVAL_STARTED_COLUMN` carries the
    graph marker (its removal is running, or stopped before it finished)."""
    return isinstance(row, dict) and row.get('removal_incomplete') is True


#: The one read of every system whose removal has started, for a check made
#: AFTER the candidate reads (a removal can start while the model answers,
#: while discovery samples, or before a write). Rows carry ``sid``. Read
#: through :func:`removing_system_ids` (or :func:`removing_system_aliases`
#: for model-written fields).
REMOVING_SYSTEMS_READ = "MATCH (s:System) WHERE s.removal_incomplete = true RETURN s.id AS sid"


def _catalog_read(cat, method: str) -> set:
    """The string keys ``cat.<method>()`` answers (``get_removal_ids`` or
    ``get_removal_keys``); none for an object without that read."""
    read = getattr(cat, method, None)
    keys = read() if callable(read) else ()
    return {k for k in keys if isinstance(k, str)} if isinstance(keys, (set, frozenset, list, tuple)) else set()


def systems_being_removed(marked_ids=(), cat=None) -> set:
    """THE answer to "which systems are being removed", as system IDS: every
    id the graph marker shows (``marked_ids``, from a read with
    :data:`REMOVAL_STARTED_COLUMN` or :data:`REMOVING_SYSTEMS_READ`) plus every
    id ``cat`` (default: this process's catalog) holds in a
    :data:`REMOVAL_STATUSES` state. Every check keyed by system id (candidate
    reads, provision, join discovery, authority selection, the route seed)
    reads it through this function; model-written fields use
    :func:`removal_aliases`.

    Two signals, one answer, because each covers a case the other cannot.
    The marker is durable and shared: written on the System node before the
    removal deletes anything, it is seen by every process (the onboarding and
    orchestrator runtimes, another app task, one that restarted) and inside a
    single Cypher statement (:data:`SERVED_ENDPOINTS_WHERE`). The catalog
    state is this process's alone, but it also knows the system's display
    name (:func:`removal_aliases`).

    Never a display name: a removed system named like another system's id
    would take that healthy system out of every id-keyed check."""
    cat = catalog if cat is None else cat
    return {str(i) for i in marked_ids or () if i} | _catalog_read(cat, 'get_removal_ids')


def removal_aliases(marked_ids=(), cat=None, served_ids=()) -> set:
    """:func:`systems_being_removed` plus the display names ``cat`` knows for
    those systems: for fields a MODEL wrote (a proposal may name a system by
    its display name). A healthy system's exact id always wins over a removed
    system's name: no id of an entry outside :data:`REMOVAL_STATUSES`
    (``SystemCatalog.get_removal_keys``) nor any of ``served_ids`` (ids the
    caller read as served) is ever an alias."""
    cat = catalog if cat is None else cat
    served = {str(i) for i in served_ids or () if i}
    return systems_being_removed(marked_ids, cat) | (_catalog_read(cat, 'get_removal_keys') - served)


def removed_ids(rows, key: str, cat=None) -> set:
    """:func:`systems_being_removed` for rows read with
    :data:`REMOVAL_STARTED_COLUMN`, each naming its system under ``key``."""
    return systems_being_removed({r.get(key) for r in rows or [] if removal_started(r)}, cat)


def removed_aliases(rows, key: str, cat=None, served_ids=()) -> set:
    """:func:`removal_aliases` for rows read with
    :data:`REMOVAL_STARTED_COLUMN`, each naming its system under ``key``."""
    return removal_aliases({r.get(key) for r in rows or [] if removal_started(r)}, cat, served_ids)


def removing_system_ids(rows, cat=None) -> set:
    """:func:`systems_being_removed` for rows read with
    :data:`REMOVING_SYSTEMS_READ`. A failed read (no rows) leaves the
    catalog's answer."""
    return systems_being_removed({r.get('sid') for r in rows or [] if isinstance(r, dict)}, cat)


def removing_system_aliases(rows, cat=None) -> set:
    """:func:`removal_aliases` for rows read with :data:`REMOVING_SYSTEMS_READ`."""
    return removal_aliases({r.get('sid') for r in rows or [] if isinstance(r, dict)}, cat)


def without_systems(items, removing, sides) -> list:
    """``items`` without the dicts that name one of ``removing`` (an id or a
    display name) under any of the keys in ``sides``; anything else is kept
    as it is."""
    return [i for i in items or []
            if not (isinstance(i, dict) and any(str(i.get(s) or '') in removing for s in sides))]


#: The WHERE of a one-statement write between two systems matched as
#: ``(fs:System {id: $fsys}), (ts:System {id: $tsys})``: both must be systems
#: whose removal has NOT started. A pre-check can be older than a removal that
#: started since, so the write's own MATCH re-checks the marker and a marked
#: endpoint writes nothing (the statement returns no row). Shared by the
#: ValueMapping upsert and the JoinPath / Correlation accepts.
SERVED_ENDPOINTS_WHERE = (
    "WHERE coalesce(fs.removal_incomplete, false) = false "
    "AND coalesce(ts.removal_incomplete, false) = false ")

#: A catalog entry's reservation of the schema its in-flight credentials
#: Edit moves it to (``SystemCatalog.reserve_schema``); records-first routing
#: keeps using ``schema`` until the move commits.
_PENDING_SCHEMA = 'pending_schema'


def _holds_schema(entry: dict, schema: str) -> bool:
    """True when ``entry`` uses ``schema`` or has reserved it."""
    return entry.get('schema') == schema or entry.get(_PENDING_SCHEMA) == schema


#: The plant Connect New System gave every registration before the form had a
#: Plant field. It names no site, but it was written onto System nodes, and a
#: restart rebuilds catalog entries from them, so it outlives the old form.
_RETIRED_PLANT_PLACEHOLDER = 'Custom'


#: What a new registration is told when it names the retired placeholder:
#: it reads as no plant, so it is refused rather than silently dropped.
RESERVED_PLANT_ERROR = ("Plant can't be 'Custom'. Leave it blank if the system "
                        "isn't tied to one site.")


def reserved_plant(plant) -> bool:
    """True when a NEW registration names the retired placeholder (any case)."""
    return str(plant or '').strip().lower() == _RETIRED_PLANT_PLACEHOLDER.lower()


def normalize_plant(plant) -> str:
    """A system's plant as the catalog, the System node and onboarding's
    same-plant rules hold it: trimmed, and ``''`` for no plant. The retired
    form placeholder is no plant, so two systems registered with it are never
    treated as sharing a site (ADR-0045)."""
    value = str(plant or '').strip()
    return '' if value == _RETIRED_PLANT_PLACEHOLDER else value


def _leased_change(method):
    """A catalog change made only while the operation running it holds its
    lease, if it holds one (``db.check_write_guard``): once a reset's or a
    provision's lease is lost, it does not move this process's catalog on
    (``reset_lease.LeaseLostError`` is raised before the change). The records
    that a removal did not finish (:meth:`SystemCatalog.mark_removal_failed`,
    :meth:`SystemCatalog.ensure_removal_failed`) are not guarded: they say
    what is true after a lost lease too."""
    @functools.wraps(method)
    def _change(self, *args, **kwargs):
        from .lease_guard import check_write_guard
        check_write_guard()
        return method(self, *args, **kwargs)
    return _change


class SystemCatalog:
    def __init__(self, systems: list[dict]):
        self._systems = list(systems)
        self._lock = threading.Lock()

    def get_all(self) -> list[dict]:
        """Return a copy of all systems."""
        with self._lock:
            return list(self._systems)

    def get(self, system_id: str) -> dict | None:
        """Find a system by ID."""
        with self._lock:
            return next((s for s in self._systems if s['id'] == system_id), None)

    def lookup(self, system_id: str) -> dict | None:
        """``get`` that HYDRATES from the graph on a miss (ADR-0069 follow-up).

        Stateless runtime containers never run the app's startup restore, and
        AgentCore routes each tool call to any of several containers — so a
        system ``get_catalog`` just listed on one container is unknown to the
        sibling that serves ``discover``. A hit never touches the graph (the
        app process is restored at startup); a miss costs ONE bounded graph
        read through the single converter, then looks again. Graph failure
        degrades to a miss and is logged.
        """
        entry = self.get(system_id)
        if entry is not None:
            return entry
        self.hydrate()
        return self.get(system_id)

    def _graph_rows(self) -> list[dict]:
        """ONE bounded :System read — seam for tests and the runtime."""
        from .db import neptune_query
        return neptune_query(f"MATCH (s:System) {SYSTEM_NODE_RETURN}")

    def hydrate(self) -> int:
        """Add graph :System rows this process has not seen. Best-effort.

        A node carrying ``removal_incomplete`` (a removal that did not finish)
        is built through :meth:`ensure_removal_failed`, the path the app's
        restore uses: never 'onboarded', also when this process hydrated it
        before the marker was written. A node without the onboarding marker
        (:func:`node_is_onboarded`) is skipped: it is not a system anyone
        onboarded. Returns the number of entries added."""
        try:
            rows = self._graph_rows()
        except Exception:
            logger.warning("catalog graph hydration failed", exc_info=True)
            return 0
        added = 0
        for r in rows or []:
            sid = r.get("id")
            if not sid:
                continue
            known = self.get(sid) is not None
            if r.get("removal_incomplete") is True:
                self.ensure_removal_failed(r)
            elif known:
                continue
            elif not node_is_onboarded(r):
                logger.debug("catalog hydration skipped %s: its System node was never "
                             "onboarded (no onboarding marker)", sid)
                continue
            else:
                self.add(entry_from_system_row(r))
            added += 0 if known else 1
        if added:
            logger.info("hydrated %d systems from the graph", added)
        return added

    def get_onboarded(self) -> list[dict]:
        """Return all systems with status 'onboarded'."""
        with self._lock:
            return [s for s in self._systems if s['status'] == 'onboarded']

    def get_onboarded_schemas(self) -> set[str]:
        """Return set of schemas for onboarded systems."""
        with self._lock:
            return {s['schema'] for s in self._systems if s['status'] == 'onboarded'}

    def get_onboarded_ids(self) -> set[str]:
        """Return set of IDs for onboarded systems."""
        with self._lock:
            return {s['id'] for s in self._systems if s['status'] == 'onboarded'}

    def get_removal_ids(self) -> set[str]:
        """The ids of every system in a :data:`REMOVAL_STATUSES` state (a
        removal runs or did not finish): what an id-keyed check reads."""
        with self._lock:
            return {s['id'] for s in self._systems
                    if s.get('status') in REMOVAL_STATUSES and s.get('id')}

    def get_removal_keys(self) -> set[str]:
        """The ids AND names of every system in a :data:`REMOVAL_STATUSES`
        state (a removal runs or did not finish). Names too: a model's
        proposal may name a system by its display name rather than its id.
        For matching model-written fields only (id-keyed checks read
        :meth:`get_removal_ids`): a name that is the id of a system outside
        those states is left out, so a healthy system's id always wins."""
        with self._lock:
            served = {s.get('id') for s in self._systems if s.get('status') not in REMOVAL_STATUSES}
            return {key for s in self._systems if s.get('status') in REMOVAL_STATUSES
                    for key in (s.get('id'), s.get('name')) if key and key not in served}

    @_leased_change
    def mark_removing(self, system_id: str) -> None:
        """Put an existing entry in the :data:`REMOVING` state for the
        duration of its removal cascade (the cascade's own catalog step ends
        it: 'registered', removed, or :meth:`mark_removal_failed`). A recorded
        removal mode is kept for the retry's sake."""
        with self._lock:
            for s in self._systems:
                if s['id'] == system_id:
                    s['status'] = REMOVING
                    return

    @_leased_change
    def update_status(self, system_id: str, status: str):
        """Update a system's status. Leaving :data:`REMOVAL_FAILED` drops the
        recorded removal mode (:data:`REMOVAL_MODE`): it only describes the
        removal still to finish."""
        with self._lock:
            for s in self._systems:
                if s['id'] == system_id:
                    s['status'] = status
                    if status != REMOVAL_FAILED:
                        s.pop(REMOVAL_MODE, None)
                    return

    def mark_removal_failed(self, system_id: str, full: bool) -> None:
        """Put an existing entry in the :data:`REMOVAL_FAILED` state with the
        mode of the removal that did not finish (:data:`REMOVAL_MODE`), so its
        retry is offered the same way."""
        with self._lock:
            for s in self._systems:
                if s['id'] == system_id:
                    s['status'] = REMOVAL_FAILED
                    s[REMOVAL_MODE] = _removal_mode(full)
                    return

    def ensure_removal_failed(self, node: dict) -> dict:
        """The entry of a system whose removal did not finish (its :System
        node carries ``removal_incomplete``), in the :data:`REMOVAL_FAILED`
        state: an existing entry changes status only; an absent one (a cold
        catalog after a restart) is rebuilt from the node row, so the card
        offers the retry. Never 'onboarded': it is not served. The node's
        ``removal_full`` (the mode the removal was asked in) becomes the
        entry's :data:`REMOVAL_MODE` when the node records it.

        A rebuilt entry also carries what the node holds of the system's
        connection that a registration's entry keeps (:func:`_cold_connection`):
        a default Remove again deletes the node and returns the entry to
        'registered', after which the entry is the only record of it.

        An entry in the :data:`REMOVING` state is left as it is: the marker
        the node carries is the running removal's own (a hydrate that sees it
        mid-cascade must not offer a retry of a removal still running)."""
        sid = node.get('id')
        recorded = node.get('removal_full')
        mode = _removal_mode(recorded) if isinstance(recorded, bool) else None
        with self._lock:
            for s in self._systems:
                if s['id'] == sid:
                    if s.get('status') == REMOVING:
                        return s
                    s['status'] = REMOVAL_FAILED
                    if mode:
                        s[REMOVAL_MODE] = mode
                    return s
            schema, connection = _cold_connection(node)
            ctype = node.get('connection_type') or ('PostgreSQL' if schema else '')
            entry = {'id': sid, 'name': node.get('name') or sid, 'plant': normalize_plant(node.get('plant')),
                     'system_type': node.get('system_type') or '', 'connection_type': ctype,
                     'connection_string': '', 'schema': schema,
                     'status': REMOVAL_FAILED, **connection}
            if node.get('connection_origin'):
                entry['connection_origin'] = node['connection_origin']
            if mode:
                entry[REMOVAL_MODE] = mode
            self._systems.append(entry)
            return entry

    @_leased_change
    def keep_node_connection(self, node: dict) -> bool:
        """Copy onto an existing entry what its :System node row holds of the
        system's connection that a registration's entry keeps
        (:func:`_cold_connection`: the non-secret settings, the connection
        string without credentials, the exact pointer to its secret; never a
        secret value).

        For a default removal, which deletes the node and keeps the
        registration: after a restart the entry was rebuilt without these
        (the node held them), and once the node is gone the entry is the only
        record of them. The node wins wherever it holds a value (an Edit
        writes the node first; the entry's write-back is best-effort and
        covers only the settings): settings key by key, the connection string,
        the pointer. A USER registration takes a node pointer only when it is
        exactly its own ``adt/conn/<id>`` (the only pointer its credentials
        are read through, ``connection_resolver.own_registration_pointer``).
        ``False`` when there is no such entry or nothing changed."""
        from .connection_resolver import is_own_connection_secret, is_user_registration
        _schema, fields = _cold_connection(node)
        if not fields:
            return False
        sid = node.get('id')
        with self._lock:
            entry = next((s for s in self._systems if s['id'] == sid), None)
            if entry is None:
                return False
            before = (dict(entry.get('connection_config') or {}),
                      entry.get('connection_string'), entry.get('secret_arn'))
            if fields.get('connection_config'):
                entry['connection_config'] = {**(entry.get('connection_config') or {}),
                                              **fields['connection_config']}
            if fields.get('connection_string'):
                entry['connection_string'] = fields['connection_string']
            arn = fields.get('secret_arn')
            if arn and (is_own_connection_secret(arn, sid) or not is_user_registration(entry)):
                entry['secret_arn'] = arn
            return before != (dict(entry.get('connection_config') or {}),
                              entry.get('connection_string'), entry.get('secret_arn'))

    @_leased_change
    def set_system_type(self, system_id: str, system_type: str) -> bool:
        """Set a system's free-form ``system_type`` label on the in-memory catalog entry.

        Descriptive metadata only (drives the fleet UI + onboarding affinity suggestions);
        concept PRECEDENCE is governed by per-concept ``AuthorityRule`` nodes, NOT by this
        label (see ``metric_resolver.set_authority`` / ``_resolve_authority``). Returns
        ``True`` if a matching entry was updated, ``False`` if no such system is registered.
        Durable persistence to the Neptune ``System`` node is the caller's responsibility
        (the admin route writes both).
        """
        with self._lock:
            for s in self._systems:
                if s['id'] == system_id:
                    s['system_type'] = system_type
                    return True
            return False

    @_leased_change
    def set_connection_config(self, system_id: str, connection_config: dict,
                              *, secret_arn: str | None = None) -> bool:
        """Set a system's ``connection_config`` on the in-memory catalog entry.

        Stores what it is given VERBATIM — callers are responsible for passing the
        NON-SECRET subset only (``connection_resolver.sanitize_connection_config``);
        ``GET /api/onboard/catalog`` additionally re-sanitizes on the way out
        (defense-in-depth). When ``secret_arn`` is a non-empty string it is ALSO
        persisted onto the entry — it is a plain Secrets Manager ARN POINTER
        (never a secret value; ``metric_resolver._full_connection_config``'s
        documented contract reads ``entry['secret_arn']`` to merge the referenced
        secret for cross-account resolution). ``secret_arn=None`` leaves any
        existing pointer untouched. Returns ``True`` if a matching entry was
        updated, ``False`` if no such system is registered. In-memory only — the
        durable source of connection state stays the Neptune ``System`` node.
        """
        with self._lock:
            for s in self._systems:
                if s['id'] == system_id:
                    s['connection_config'] = dict(connection_config)
                    if secret_arn:
                        s['secret_arn'] = str(secret_arn)
                    return True
            return False

    @_leased_change
    def add(self, entry: dict, *, unique_schema: bool = False) -> bool:
        """Add a new system entry; ``False`` (nothing added) when an entry with
        the same id is already present — or, with ``unique_schema``, when any
        entry already uses the entry's (non-empty) ``schema``. The checks and the
        append run under one lock, so two concurrent registrations of one id, or
        of one schema (a user PostgreSQL registration: records-first sampling
        routes by schema), cannot both land."""
        schema = entry.get('schema') if unique_schema else None
        if 'plant' in entry:
            entry['plant'] = normalize_plant(entry['plant'])
        with self._lock:
            if any(s['id'] == entry.get('id') for s in self._systems):
                return False
            if schema and any(_holds_schema(s, schema) for s in self._systems):
                return False
            self._systems.append(entry)
            return True

    @_leased_change
    def claim_schema(self, system_id: str, schema: str) -> bool:
        """Move ``system_id``'s entry to ``schema`` unless ANOTHER entry already
        uses or has reserved it (the duplicate-schema rule :meth:`add` applies
        with ``unique_schema``); the check and the change run under one lock.
        ``False`` (nothing changed) when another entry holds the schema or no
        such system is registered."""
        with self._lock:
            target = next((s for s in self._systems if s['id'] == system_id), None)
            if target is None or not schema:
                return False
            if any(s is not target and _holds_schema(s, schema) for s in self._systems):
                return False
            target['schema'] = schema
            return True

    @_leased_change
    def reserve_schema(self, system_id: str, schema: str) -> bool:
        """Reserve ``schema`` for ``system_id``'s pending move: no other entry may
        use or reserve it (registration and :meth:`claim_schema` refuse it),
        while the entry's own ``schema`` (records-first routing) is unchanged
        until :meth:`commit_schema`. ``False`` (nothing changed) when another
        entry holds it, the entry already has a reservation, or no such system
        is registered."""
        with self._lock:
            target = next((s for s in self._systems if s['id'] == system_id), None)
            if target is None or not schema or target.get(_PENDING_SCHEMA):
                return False
            if any(s is not target and _holds_schema(s, schema) for s in self._systems):
                return False
            target[_PENDING_SCHEMA] = schema
            return True

    @_leased_change
    def commit_schema(self, system_id: str, schema: str) -> bool:
        """Move the entry onto the schema it reserved (its old schema becomes
        free); ``False`` (nothing changed) unless that reservation is held."""
        with self._lock:
            target = next((s for s in self._systems if s['id'] == system_id), None)
            if target is None or target.get(_PENDING_SCHEMA) != schema:
                return False
            target['schema'] = target.pop(_PENDING_SCHEMA)
            return True

    @_leased_change
    def release_schema_reservation(self, system_id: str, schema: str) -> bool:
        """Drop the entry's reservation of ``schema`` (an aborted move); its own
        ``schema`` is never touched."""
        with self._lock:
            target = next((s for s in self._systems if s['id'] == system_id), None)
            if target is None or target.get(_PENDING_SCHEMA) != schema:
                return False
            target.pop(_PENDING_SCHEMA)
            return True

    @_leased_change
    def remove_entry(self, entry: dict) -> bool:
        """Remove exactly this entry object (identity, not id) — a registration
        rolling back its own claim never removes an entry someone else added."""
        with self._lock:
            before = len(self._systems)
            self._systems = [s for s in self._systems if s is not entry]
            return len(self._systems) < before

    @_leased_change
    def set_connection_origin(self, system_id: str, origin: str) -> bool:
        """Record who registered a system (``connection_resolver.ORIGIN_*``)."""
        with self._lock:
            for s in self._systems:
                if s['id'] == system_id:
                    s['connection_origin'] = origin
                    return True
            return False

    @_leased_change
    def reset_all(self):
        """Reset all systems to 'registered' status."""
        with self._lock:
            for s in self._systems:
                s['status'] = 'registered'

    @_leased_change
    def clear_all(self) -> int:
        """REMOVE every system entry — a truly blank catalog. Returns the count removed.

        Distinct from ``reset_all`` (which only re-flags entries to 'registered' and
        KEEPS their connect config): this drops the entries entirely so the catalog
        no longer advertises any registered system. Used only by the opt-in
        ``full=true`` global reset. Does NOT touch AWS Secrets Manager — connection
        secrets (agent-config / conn/<id>) are left intact.
        """
        with self._lock:
            n = len(self._systems)
            self._systems.clear()
            return n

    @_leased_change
    def remove(self, system_id: str) -> bool:
        """REMOVE a single system entry from the catalog. Returns True if removed.

        Used by the per-system reset so a reset system no longer advertises a
        connection-less 'registered' stub: a reset already wipes the system's
        Neptune node + learnings, so a catalog stub with no connection details is
        misleading (you can't Test/Onboard it). Applies uniformly to every system
        (no demo-vs-user split) — a re-onboard re-registers from scratch. Does NOT
        touch AWS Secrets Manager — connection secrets (agent-config / conn/<id>)
        are left intact so a re-onboard can reuse or re-mint them.
        """
        with self._lock:
            before = len(self._systems)
            self._systems = [s for s in self._systems if s['id'] != system_id]
            return len(self._systems) < before


# ── Canonical System-node → catalog-entry conversion (single source) ─────────
# Canonical home for the System-row → entry shape, used by the stateless
# onboarding runtime's lazy hydration (onboard_agent.get_catalog).
# FOLLOW-UP: server._restore builds the same entry inline — converging it here
# requires decomposing that 200-line startup function first (the
# maintainability gate forbids touching it piecemeal); until then keep the two
# shapes field-identical ('system_type', never 'type').

# RETURN fields for a :System node read (append to a MATCH of your choosing).
SYSTEM_NODE_RETURN = (
    "RETURN DISTINCT s.id AS id, s.name AS name, s.schema AS schema, "
    "s.plant AS plant, s.connection_type AS connection_type, "
    "s.system_type AS system_type, s.removal_incomplete AS removal_incomplete, "
    "s.removal_full AS removal_full, s.status AS status, "
    "s.connection_config AS connection_config, s.secret_arn AS secret_arn"
)


def _cold_connection(node: dict) -> tuple[str, dict]:
    """``(schema, fields)`` of a System node row for an entry rebuilt without
    one (:meth:`SystemCatalog.ensure_removal_failed`): the schema (the node's,
    else its connection settings'), the NON-SECRET connection settings
    (``connection_resolver.sanitize_connection_config``, the allowlist every
    catalog entry's config passes), the connection string the node keeps (an
    MCP Server's URL; stored without inline credentials, and stripped of any
    again here) and the exact pointer to its secret (``secret_arn``, an ARN,
    never a secret value): the fields a registration or a provision puts on
    an entry. Missing or unreadable settings give ``{}``."""
    from .connection_resolver import _strip_url_credentials, sanitize_connection_config
    raw = node.get('connection_config')
    try:
        cfg = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
    except (ValueError, TypeError):
        cfg = {}
    fields: dict = {}
    safe = sanitize_connection_config(cfg)
    if safe:
        fields['connection_config'] = safe
    conn_str = cfg.get('connection_string')
    if isinstance(conn_str, str) and conn_str:
        fields['connection_string'] = _strip_url_credentials(conn_str)
    arn = node.get('secret_arn')
    if isinstance(arn, str) and arn:
        fields['secret_arn'] = arn
    schema = node.get('schema') or (cfg.get('schema') if isinstance(cfg, dict) else '') or ''
    return str(schema), fields

#: The :System node's onboarding marker. Every in-app onboarding writer sets
#: it: provision's System-node write (with the node's name), SiteWise (Direct)
#: and i3X provisioning, and the SchemaEntry writer's ON CREATE. The Step
#: Functions provisioner writes no graph content yet.
ONBOARDED_NODE_STATUS = "onboarded"


def node_is_onboarded(row: dict | None) -> bool:
    """Whether a :System row (one that reads ``s.status AS status``) carries
    the onboarding marker. A node a connection write created before the first
    provision (``connection_resolver.write_connection``: an admin Edit of a
    registration; the scan budget) holds connection settings only and has
    none, so it never means 'onboarded'."""
    return (row or {}).get("status") == ONBOARDED_NODE_STATUS


def entry_from_system_row(row: dict, *, status: str = "onboarded") -> dict:
    """Normalize a :System graph row into the canonical catalog entry shape."""
    sid = row.get("id") or ""
    return {
        "id": sid,
        "name": row.get("name") or sid,
        "plant": normalize_plant(row.get("plant")),
        "system_type": row.get("system_type") or "",
        "connection_type": (row.get("connection_type") or "").strip() or "PostgreSQL",
        "connection_string": "",
        "schema": row.get("schema") or "",
        "status": status,
    }


def hydrate_from_graph(neptune_query) -> int:
    """Populate an EMPTY in-process catalog from the graph's :System nodes.

    For stateless processes (the adt_onboarding AgentCore runtime) that never
    run the app's startup restore — without this, their module-level catalog
    stays empty forever and get_catalog reports "no systems" while the app UI
    shows onboarded systems. Best-effort: no graph access leaves the catalog
    unchanged. Returns the number of systems added.
    """
    # Legacy injectable seam kept for callers that pass their own query fn;
    # the catalog's own ``hydrate`` is the production path.
    original = catalog._graph_rows
    catalog._graph_rows = lambda: neptune_query(f"MATCH (s:System) {SYSTEM_NODE_RETURN}")
    try:
        return catalog.hydrate()
    finally:
        catalog._graph_rows = original


catalog = SystemCatalog([])


ENTITY_CONCEPT = {
    "batch": "batch",
    "equipment": "equipment-id",
    "product": "product",
    "order": "work-order",
}

# Core manufacturing concepts with descriptions.
# Descriptions guide the LLM during field mapping — be specific about field names and values.
CORE_CONCEPT_DEFINITIONS = {
    # ── Equipment & Assets ──────────────────────────────────────────────────
    "equipment-id":          "Unique identifier for a physical piece of equipment or asset. Synonyms (ISA-95 equipment level): machine, asset, serial. Fields: asset_id, asset_tag, equipment_id, machine_id, serial_number, asset_name. Values are site-specific asset codes, often a site or line prefix plus an equipment tag and a sequence number.",
    "asset-class":           "The class or type of asset. Fields: asset_type, asset_class, equipment_type. Values name the kind of machine or the operation it performs — process-specific equipment classes (machining, forming, mixing, packaging, or inspection unit types), not individual asset ids.",
    "lifecycle-state":       "The lifecycle state of an asset or record. Fields: operational_status, status, state. Values: operational, maintenance, decommissioned, in_review, released.",
    "criticality":           "The criticality level of an asset or issue. Fields: criticality, priority, severity. Values: critical, high, medium, low.",
    "runtime-hours":         "Cumulative operating hours for equipment. Fields: runtime_hours, operating_hours, run_time, hours_since_maintenance.",
    "asset":                 "A maintained physical asset record — the CMMS `assets` table. The record type that the `equipment-id` identifier and `asset-class` category describe. Fields: asset_id, asset_tag, serial_number, asset_type, facility, area, criticality, install_date. Distinct from the `equipment-id` identifier (a field) and `asset-class` (a field) — this is the entity those columns hang off.",

    # ── Production & Lines ──────────────────────────────────────────────────
    "production-line":       "A physical production line in a manufacturing facility. Synonyms (ISA-95 work-center level, BELOW site): line, work cell, cell, station, work center. Fields named 'line', 'line_id', 'area', 'line_name', 'cell', 'work_center', 'station' with values like Line1, Line2, Line3, L3, L5, L7. KEY CROSS-SYSTEM LINK between MES (line_id), CMMS (area), and OPC UA (path segment). A 'cell'/'work center'/'station' is a work-center-level location — map it here, NOT to 'site' (site is a higher, plant level). Do NOT map to work-order, location, or equipment-id.",
    "production-run":        "A scheduled or active production run or batch execution. Fields: run_id, batch_number, work_order_ref, line_id, equipment_id, product_code, planned_qty, good_qty, reject_qty, scrap_qty, start_time, end_time, status.",
    "production-order":      "A production order from ERP driving manufacturing. Fields: order_number, order_type, material_number, plant, quantity, scheduled_start, scheduled_finish, status, production_line, priority. ERP values: PP01, REL, CONF. One system's production order number can equal another's production run number: the system that executes an order often keys its run by the order's number.",
    "shift":                 "A work shift period. Fields: shift, shift_id, shift_number, shift_name, operator_badge, shift_start, shift_end. Values: A, B, C (MES), Day, Night, Swing, 1, 2, 3 (OPC UA).",
    "batch":                 "A production batch or lot. Fields: batch_id, batch_number, run_number, lot_number, batch_size, recipe_version. The SAME batch often appears under DIFFERENT field names across systems (one system's run_number can equal another's batch_number) — cross-system joins align these identifiers. One system's production order number can equal another's batch number: a plant that builds each order as one batch keys the batch by the order's number.",
    "availability":          "OEE availability ratio — the fraction of planned production time equipment was actually running (run time / planned production time). A direct INPUT to the derived `oee` metric. Fields: availability, OEE_Availability, availability_rate, uptime_ratio, uptime_pct. Values: a 0-1 fraction or 0-100%. Distinct from `lifecycle-state` (categorical status) and from the composite `oee-score`.",
    "performance":           "OEE performance ratio — actual throughput vs ideal/rated throughput (ideal cycle time * total count / run time). A direct INPUT to the derived `oee` metric. Fields: performance, OEE_Performance, performance_rate, speed_ratio, rate_efficiency. Values: a 0-1 fraction or 0-100%. Distinct from raw `sensor-reading` speeds/rates.",
    "oee-score":             "A precomputed/stored Overall Equipment Effectiveness value supplied directly by a source system (the composite, not its inputs). Fields: oee, oee_score, oee_percentage, OEE_Overall. The resolver reads this stored value directly when present; otherwise the `oee` metric is DERIVED from availability * performance * quality (quality via `quality-yield-rate`). Do NOT map OEE_Availability/OEE_Performance/OEE_Quality here — those map to `availability`/`performance`/`quality-yield-rate`.",

    # ── Quality ─────────────────────────────────────────────────────────────
    "quality-metric":        "A count of GOOD units, or a quality score. Not a reject count (scrap-waste) or a sampled check's tallies (inspection). Fields: quality_score, defect_rate, good_qty, good_quantity, good_count, FirstPassYield, TolerancePass. A 0-100 pass/yield rate column (pass_rate, first_pass_yield) is `quality-yield-rate`, not this concept.",
    "inspection":            "A quality inspection or check. Its sample tallies (sample size, units passed, units failed) are measures of it. Fields: inspection_id, inspection_type, check_type, measurement_equipment, sample_number. Includes dimensional (CMM), vision, contaminant, and inline weight checks. The pass/fail OUTCOME column of an inspection maps to `inspection-result`.",
    "inspection-result":     "The categorical outcome/disposition of a quality inspection — pass/fail or accept/reject. Fields: disposition, pass_fail, result, check_result, inspection_result, outcome. Values: PASS, FAIL, ACCEPT, REJECT, PASSED, FAILED. Distinct from `inspection` (the inspection record/event), from `quality-yield-rate` (a numeric 0-100 rate), and from `non-conformance` (the defect record a FAIL may raise).",
    "specification":         "A technical specification, tolerance, or SPC limit. Fields: spec_id, specification, tolerance, nominal, lsl, usl, target_value, min_value, max_value, parameter. SPC: lower/upper spec limit for CNC dimensions, fill weights, temperatures.",
    "non-conformance":       "A quality non-conformance or defect record. Fields: nc_id, defect_id, non_conformance_type, disposition, notes. MES: 'diameter drift detected by SPC'.",
    "defect-type":           "The category or type of quality defect. Fields: defect_type, defect_code, failure_type, nc_type.",
    "scrap-waste":           "Rejected or scrapped production output. Fields: scrap_qty, scrap_quantity, reject_qty, reject_quantity, bad_qty, bad_quantity, bad_count, waste_units, scrap_reason.",
    "actual-quantity":       "Total units produced (good + rejected) by a work order, run or batch. Not a running count (a station tally, or one repeated per event row). Fields: actual_quantity, total_quantity, produced_qty, quantity_produced, units_produced, total_produced. A counter READING or snapshot logged on an event or history row (the running count at a start, stop or state change) repeats the record's count, so summing it overcounts; a row that records the units made in its own interval is `throughput`. Used as denominator for quality yield calculation (good/actual).",
    "quality-yield-rate":    "A quality pass/yield rate as a percentage (0-100). Fields: pass_rate, first_pass_yield, first_pass_yield_rate, quality_yield_rate, yield_rate, pass_rate_pct.",

    # ── Maintenance ─────────────────────────────────────────────────────────
    "maintenance-event":     "A maintenance activity, work order, or service event on equipment. Fields: wo_number, work_order_id, wo_type (preventive/corrective/inspection), maintenance_type, description, failure_code, assigned_to, estimated_hours, actual_hours.",
    "maintenance-schedule":  "A recurring maintenance schedule for equipment. Fields: schedule_id, maintenance_type, frequency_days, last_performed, next_due, assigned_team, status. Values: scheduled, overdue. Calibration, preventive, inspection intervals.",
    "failure-mode":          "The type or category of equipment failure — including the reason/fault code attached to a machine-state transition or downtime event. Fields: failure_code, failure_mode, failure_type, fault_code, reason_code, reason, stop_reason. Values: INSTR-DRIFT, VIB-BEARING, MECH-NOISE, TEMP-HEATER, CHEM-CONC, ALIGN-MECH.",
    "downtime":              "A period when equipment or a production line is not running. Fields: downtime_id, downtime_reason, downtime_category, duration_minutes, Down (OPC UA boolean), end_time (NULL = active).",
    "alarm":                 "An alert or alarm condition from equipment or a control system. Fields: alarm_id, alarm_code, alarm_type, severity, acknowledged.",
    "spare-part":            "A spare part or replacement component for equipment maintenance. Fields: part_number, part_name, compatible_asset_type, unit_cost, supplier, lead_time_days. Values: KT-FVS-2000, MZ-SPINDLE-KIT, CX-DIE-PLATE.",

    # ── Sensor & Process Data ────────────────────────────────────────────────
    "sensor-reading":        "A real-time or time-series measurement from a sensor or OPC UA tag. Fields: value, reading, measurement, temperature, pressure, vibration, flow, speed, displacement, SpindleSpeed, FeedRate, FurnaceTemp, SoakTime, TempZone1/2/3, ScrewSpeed, ConveyorSpeed, BathTemp, SealTemp, FillWeight, Weight.",
    "event-time":            "A timestamp marking when a record or measurement occurred — the time-ordering field used to pick the LATEST value per group (DISTINCT ON ordering for sensor heatmaps / latest readings). Fields: check_time, event_time, reading_time, measurement_time, sample_time, recorded_at, timestamp, ts, created_at, start_time, alert_time, read_at. Map a single timestamp/datetime column per measurement table; do NOT map a duration or a date-only planning field here.",
    "cycle-time":            "The time to complete one production cycle or machining operation. Fields: cycle_time, CycleTime, takt_time, process_time. Automotive CNC: CycleTime in seconds per part.",
    "tool-life":             "Remaining useful life or wear state of a cutting tool or consumable. Fields: ToolLifeRemaining, tool_life_pct, ToolChangeEvent, insert_count. CNC machining: decrements from 100% to 0, resets on tool change.",
    "food-safety":           "Food safety compliance data including HACCP, allergen, and sanitation records. Fields: HACCP_CriticalTemp, TempCompliant, AllergenVerified, PackageIntegrity, LabelVerified, MetalDetect_Pass.",

    # ── OT / Machine Telemetry (live equipment state & rates) ────────────────
    "machine-state":         "The operational state of a machine/asset and its transitions. Fields: state, machine_state, from_state, to_state, status_code, state_duration. A categorical/transition concept distinct from `lifecycle-state` (record status) — this is the live running/stopped/idle/changeover state of equipment. A reason/fault attached to a state transition is `failure-mode`; the elapsed time of a stopped state is `downtime`.",
    "throughput":            "Produced-output rate or count over a time bucket. Fields: amount, count, produced_per_interval, output_count, amount_per_interval, throughput. Distinct from `scrap-waste` (rejected output) and `performance` (OEE ratio); this is raw produced quantity/rate.",
    "machine-speed":         "The actual running speed/rate of a machine. Fields: speed, mech_speed, actual_speed, act_speed, line_speed, rpm. A raw rate reading; distinct from `cycle-time`, from the `performance` ratio, and from `target-machine-speed` (the rated/target setpoint), and its own concept (NOT a generic `sensor-reading`).",
    "target-machine-speed":  "The target/rated running speed of a machine — the setpoint the actual `machine-speed` is compared against (actual/target gives the performance ratio). Fields: target_speed, target_mech_speed, rated_speed, design_speed, setpoint_speed, nominal_speed. A rate setpoint; distinct from `machine-speed` (the actual reading) and from `specification` (quality/SPC limits).",

    # ── Materials & Inventory ────────────────────────────────────────────────
    "material":              "A raw material, component, or ingredient. Fields: material_id, material_number, material_name, material_type, material_group, sku, part_number, base_unit, grade, inspection_status. Tables: inventory_items, materials. Do NOT create new concepts for inventory_item — use this concept.",
    "lot-number":            "A material lot or batch tracking number. Fields: lot_number, lot_id, material_lot, batch_number, inspection_lot. Tables: material_lots, lot_warehouse_movements, lot_production_usage. Do NOT create new concepts for material_lot or lot — use this concept.",
    "inventory-level":       "Current stock quantity of a material or product. Fields: stock_level, inventory_count, balance_quantity. Tables: inventory_balances, inventory_items. Do NOT create new concepts for inventory_balance or inventory_item — use this concept.",
    "mro-inventory":         "On-hand quantity of maintenance/MRO spare parts and stockroom stock. Fields: quantity_on_hand, on_hand_qty, reorder_point. Tables: spare_parts, stockroom. A measure concept (single numeric field), distinct from raw-material/product stock (inventory-level) and the spare-part entity.",
    "inventory-transaction": "An inventory movement or transaction. Fields: transaction_id, transaction_type, quantity_moved, from_location, to_location. Tables: inventory_transactions, inventory_transfers.",
    "material-consumption":  "Material consumed during production. Fields: consumed_quantity, material_consumed, consumption_date, lot_consumed. Tables: mes_material_consumption, material_consumption.",
    "bill-of-materials":     "A bill of materials, recipe, or formula. Fields: bom_id, bom_name, bom_version, component_id, component_name, quantity, unit_of_measure, lead_time_days. Tables: bill_of_materials. PLM: Water Bottling BOM, Carbonated Beverage BOM.",

    # ── Products & PLM ───────────────────────────────────────────────────────
    "product":               "A finished or semi-finished product. Fields: product_id, product_name, product_code, sku, item_number, product_type. Tables: products. Values: 7742, 8810, 9901 (MES codes), Still Water Line, Cola Line.",
    "product-category":      "A product category or family. Fields: product_category, category_name, product_family, product_type. Values: typeA, typeB, typeC, Beverages, Automotive, Food.",
    "product-revision":      "A product or BOM revision/version. Fields: revision, bom_version, version, rev_level. PLM: revision '2.1', '3.0', '1.5'. Engineering change management.",
    "engineering-change":    "An engineering change order or request (ECO/ECR). Fields: change_id, change_type, description, status, requested_by, approved_by. PLM: material, formula, design changes.",

    # ── Orders & Supply Chain ────────────────────────────────────────────────
    "order-status":          "The status of an order or work order. Fields: status, order_status, wo_status. Values: PENDING, CONFIRMED, REL, CONF, RUNNING, COMPLETED, SHIPPED, Complete, Stopped, Scheduled.",
    "purchase-order":        "A purchase order for materials. Fields: po_id, po_number, purchase_order_id, po_status, expected_delivery_date, actual_delivery_date, total_amount, quantity_ordered, quantity_received, unit_price, line_total. Tables: purchase_orders, po_line_items, po_receipts. On-time delivery (OTD) compares actual_delivery_date to expected_delivery_date; supplier spend sums total_amount (or line_total) per supplier.",
    "sales-order":           "A customer sales order — the ERP `customer_orders` table (line items in `order_line_items` fold in). Fields: order_id, order_number, customer_id, order_date, requested_delivery_date, order_status, line_item_id, quantity, unit_price, line_total. The commercial demand record a `shipment` fulfils. Distinct from `purchase-order` (procurement) and `production-order` (manufacturing demand).",
    "schedule":              "A production schedule or planned timeline. Fields: scheduled_start, scheduled_end, planned_start, due_date, next_due_date, frequency_days.",
    "customer":              "A customer or buyer. Fields: customer_id, customer_name, customer_number, account_number, buyer_id. Tables: customers, customer_orders.",
    "supplier":              "A vendor or supplier of materials or parts. Fields: supplier_id, supplier_name, vendor_id, vendor_number, name, rating, payment_terms. Tables: suppliers, item_suppliers.",
    "invoice":               "A billing invoice or line item. Fields: invoice_id, invoice_number, invoice_date, invoice_amount, line_item_id, unit_price, line_total. Tables: invoices, invoice_line_items. Do NOT create new concepts for line_item or invoice_line_item.",
    "payment":               "A payment transaction. Fields: payment_id, payment_date, payment_amount, payment_status. Tables: payments.",
    "shipment":              "An outbound shipment of finished goods — the ERP `shipments` table (line items in `shipment_items` fold in). Fields: shipment_id, order_id, ship_date, carrier, tracking_number, status (PREPARING/PICKED/PACKED/SHIPPED/IN_TRANSIT/DELIVERED), batch_id, product_id, quantity. Its stage says who holds the goods: before dispatch they are still at the shipping site; once handed to a carrier and until delivery is confirmed they are with the carrier, which may be able to stop or intercept them; after delivery they are with the recipient. Cancellation, refusal, return, loss and partial delivery change that, so read them from the shipment's own records. Fulfils a `sales-order`; joins shipment_items.shipment_id = shipments.shipment_id and shipments.order_id = customer_orders.order_id.",
    "cost":                  "A monetary cost or price. Fields: unit_cost, total_cost, price, cost_per_unit, estimated_cost, acquisition_cost.",

    # ── People & Organization ────────────────────────────────────────────────
    "operator":              "A human operator or technician. Fields: operator_id, operator_name, operator_badge, assigned_to, technician. Values: OP-0101, maint_jones, maint_smith.",
    "personnel":             "A person — employee, operator, or technician. Fields: person_id, employee_id, user_id, assigned_to, created_by.",

    # ── Locations & Sites ────────────────────────────────────────────────────
    "facility":              "A manufacturing facility or plant. Fields: facility, facility_id, plant, plant_id, site_name. Values are plant or city names.",
    "site":                  "A manufacturing site or plant location code. Synonyms (ISA-95 site level): plant, facility, location (site-level). Fields: site_id, site_code, site_name, plant, plant_id, facility_code. Values are short site letter codes or numeric plant ids like 1000. Site is the PLANT level (the whole facility); work-center-level locations below it belong to 'production-line'.",
    "location":              "A physical location within a facility (NOT a production line). Fields: location_id, warehouse_location, bin, zone, storage_area.",

    # ── Work Orders ──────────────────────────────────────────────────────────
    "work-order":            "A manufacturing or maintenance work order. Fields: work_order_id, work_order_number, wo_number, work_order_ref, wo_type, status, priority, description, assigned_to.",
}

# Identifier concepts the vocabulary declares INTERCHANGEABLE across systems, as
# (concept, declaring concept) pairs: the declaring concept's definition above says
# one system's identifier of ``concept`` can equal another's of the declaring one
# (``batch``: "one system's run_number can equal another's batch_number";
# ``production-run``: "a production run or batch execution", keyed by batch_number).
# Consumed ONLY by the exact-agreement pass's concept gate (ADR-0087 amendment
# 2026-09-30); agreement is symmetric, not transitive, and never evidence alone.
# ``work-order`` ~ ``batch`` (amendment 2026-10-05): a lot-tracking record's batch
# reference is often the work order that consumed the lot, so one system's
# work_order_number can equal another's batch_id. Every measured gate (shape,
# probe floor, key cardinality, same-class cross-site guard) still decides.
# ``production-run`` ~ ``production-order`` (amendment 2026-10-21): the system
# that executes a production order often keys its run by the order's number,
# so one system's production order number can equal another's run number
# (``production-order`` declares it, in its last sentence, past the 140
# characters the wizard's map prompt shows). The same gates still decide.
# ``production-order`` ~ ``batch`` (amendment 2026-10-21): a plant that builds
# each order as one batch keys the batch by the order's number, so an MES
# order column can hold the numbers an ERP keeps only as shipment batch ids
# (``batch`` declares it, in its last sentence, also past 140 characters).
# A rule it agrees names ``batch``, as the run and work-order hops do.
RELATED_IDENTIFIER_CONCEPTS: frozenset[tuple[str, str]] = frozenset({
    ("production-run", "batch"),
    ("work-order", "batch"),
    ("production-run", "production-order"),
    ("production-order", "batch"),
})
_unknown_related = sorted({c for pair in RELATED_IDENTIFIER_CONCEPTS for c in pair}
                          - set(CORE_CONCEPT_DEFINITIONS))
if _unknown_related or any(a == b for a, b in RELATED_IDENTIFIER_CONCEPTS):
    raise ValueError(f"RELATED_IDENTIFIER_CONCEPTS must pair two distinct core concepts: {_unknown_related}")

# Concepts the vocabulary defines as the SAME kind of thing at different breadth,
# as (narrower, broader) pairs: an ``operator`` (a human operator or technician)
# is a kind of ``personnel`` (a person: employee, operator or technician); both
# definitions above list ``assigned_to``. The field mapper can file one person
# column under either, so two systems' columns for the same people can land on
# different concepts (a CMMS log author mapped ``personnel`` while the MES
# ``resolved_by`` it copies is ``operator``). Consumed ONLY by the resolve
# candidate prefilter (``onboard._do_resolve``, ADR-0040 amendment 2026-10-17):
# a pair whose concepts are declared here is sampled and overlap-measured like a
# same-concept pair. The relation decides what is MEASURED, never what is
# proposed: the evidence gates (``resolve_gates``) and the site gate still
# decide every rule. Symmetric, not transitive.
NARROWER_CONCEPTS: frozenset[tuple[str, str]] = frozenset({
    ("operator", "personnel"),
})
_unknown_narrower = sorted({c for pair in NARROWER_CONCEPTS for c in pair}
                           - set(CORE_CONCEPT_DEFINITIONS))
if _unknown_narrower or any(a == b for a, b in NARROWER_CONCEPTS):
    raise ValueError(f"NARROWER_CONCEPTS must pair two distinct core concepts: {_unknown_narrower}")


# Core concepts whose records are people (the "People & Organization" group
# above): a column mapped to one of these identifies a person. Consumed by the
# engine's form crossing (``form_crossing``, ``form_pairing``, ADR-0087
# amendment 2026-11-07) as the second check behind its allowlist
# (:data:`CROSSABLE_CONCEPTS`). Concept ids only, never table or column names.
# Every concept declared narrower or broader than one of these
# (``NARROWER_CONCEPTS``) must be listed too.
PERSON_CONCEPTS: frozenset[str] = frozenset({"operator", "personnel"})
_unknown_person = sorted(PERSON_CONCEPTS - set(CORE_CONCEPT_DEFINITIONS))
_open_person = sorted({c for a, b in NARROWER_CONCEPTS for c in (a, b)
                       if {a, b} & PERSON_CONCEPTS} - PERSON_CONCEPTS)
if _unknown_person or _open_person:
    raise ValueError(f"PERSON_CONCEPTS must be core and closed under NARROWER_CONCEPTS: "
                     f"unknown={_unknown_person} missing={_open_person}")

# The core concepts whose record identifiers the engine's form crossing may
# carry from one system to another (``form_crossing``, ``form_pairing``,
# ADR-0087 amendment 2026-11-07): the production records a plant generates
# itself (runs, production and work orders, batches and lots). A
# server-owned ALLOWLIST: every concept a form's exact join records and every
# concept each read column maps to must be one of these; any other concept,
# and any unknown or custom one, refuses. Records that can carry a person or
# counterparty reference (purchase and sales orders, shipments, parts,
# materials, products, assets and equipment) are left out and come back only
# with a classification source, a stored marking that a column's values name
# no person or counterparty. ADR-0087's amendment lists what was left out and
# why. None may be a person concept (checked below and again at every use,
# :func:`crossable_concept`), and every catalog-related identifier pair must
# be crossable on both sides, since a form's join can record either.
CROSSABLE_CONCEPTS: frozenset[str] = frozenset({
    "production-run", "production-order", "work-order", "batch", "lot-number",
})
_unknown_crossable = sorted(CROSSABLE_CONCEPTS - set(CORE_CONCEPT_DEFINITIONS))
_person_crossable = sorted(CROSSABLE_CONCEPTS & PERSON_CONCEPTS)
_open_related = sorted({c for pair in RELATED_IDENTIFIER_CONCEPTS for c in pair}
                       - CROSSABLE_CONCEPTS)
if _unknown_crossable or _person_crossable or _open_related:
    raise ValueError(f"CROSSABLE_CONCEPTS must be core non-person concepts covering "
                     f"RELATED_IDENTIFIER_CONCEPTS: unknown={_unknown_crossable} "
                     f"person={_person_crossable} related={_open_related}")


def definition_fields(concept: str) -> frozenset[str]:
    """The field names a core concept's definition lists after ``Fields:``
    (up to that sentence's end), lowercased; empty when it lists none."""
    _head, found, rest = CORE_CONCEPT_DEFINITIONS.get(concept, "").partition("Fields:")
    listed = rest.split(".")[0] if found else ""
    return frozenset(f.strip().lower() for f in listed.split(",") if f.strip())


_WORD = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+")


def field_words(name) -> tuple[str, ...]:
    """A column name as lowercase words: split at underscores, hyphens, dots
    and every other non-alphanumeric character, at camelCase and PascalCase
    boundaries and around digits (``operatorBadge2`` -> operator, badge, 2;
    ``CustomerID`` -> customer, id)."""
    return tuple(w.lower() for w in _WORD.findall(str(name or "")))


def word_run(words: tuple, within: tuple) -> bool:
    """Whether ``words`` (non-empty) occurs in ``within`` as a contiguous run."""
    n = len(words)
    return n > 0 and any(tuple(within[i:i + n]) == tuple(words)
                         for i in range(len(within) - n + 1))


# Field names the vocabulary itself gives to person-valued and counterparty
# fields: every field a person concept's or ``customer``'s definition lists;
# the identifier and name fields ``supplier``'s definition lists (not its bare
# ``name``, ``rating`` or ``payment_terms``); the bare ``supplier`` field
# ``spare-part`` lists, with ``vendor`` and ``customer``, the heads of those
# fields; and the person-valued fields other definitions list
# (``work-order``'s assignee, ``shift``'s operator badge,
# ``engineering-change``'s requester and approver, ``maintenance-schedule``'s
# assigned team). Consumed by the engine's form crossing as a field-level
# check (:func:`person_valued_field`): a column named like one of these is not
# carried, whatever concept it maps to. Catalog vocabulary only (checked
# below: each entry's words are a run of a field a core definition lists),
# never a dataset's column names.
PERSON_VALUED_FIELDS: frozenset[str] = frozenset().union(
    *(definition_fields(c) for c in (*sorted(PERSON_CONCEPTS), "customer")),
    definition_fields("supplier") - {"name", "rating", "payment_terms"},
    {"supplier", "vendor", "customer"},
    {"assigned_to", "assigned_team", "operator_badge", "requested_by", "approved_by"})
_VOCABULARY_WORDS = tuple(field_words(f) for c in CORE_CONCEPT_DEFINITIONS
                          for f in definition_fields(c))
_unlisted_person_fields = sorted(
    f for f in PERSON_VALUED_FIELDS
    if not any(word_run(field_words(f), v) for v in _VOCABULARY_WORDS))
if _unlisted_person_fields:
    raise ValueError(f"PERSON_VALUED_FIELDS must be words of fields the core definitions "
                     f"list: {_unlisted_person_fields}")
_PERSON_FIELD_WORDS = tuple(sorted({field_words(f) for f in PERSON_VALUED_FIELDS}))


def crossable_concept(concept) -> bool:
    """Whether the engine's form crossing may carry identifiers of ``concept``:
    on :data:`CROSSABLE_CONCEPTS` and, the second check, not a person concept
    (:data:`PERSON_CONCEPTS`). An unknown or custom concept is not."""
    return (isinstance(concept, str) and concept in CROSSABLE_CONCEPTS
            and concept not in PERSON_CONCEPTS)


def person_valued_field(name) -> bool:
    """Whether a column name holds one of :data:`PERSON_VALUED_FIELDS` as a
    contiguous run of its words (:func:`field_words`): ``assigned_to``,
    ``wo_assigned_to``, ``assignedTo``, ``supplierRef``, ``CustomerID``; not
    ``assigned_total`` or ``unassigned``."""
    words = field_words(name)
    return any(word_run(entry, words) for entry in _PERSON_FIELD_WORDS)


def comparable_concepts(concept: str) -> frozenset[str]:
    """``concept`` plus every concept ``NARROWER_CONCEPTS`` declares narrower or
    broader than it (one step, either direction). Empty for an empty concept."""
    if not concept:
        return frozenset()
    related = {b for a, b in NARROWER_CONCEPTS if a == concept}
    related |= {a for a, b in NARROWER_CONCEPTS if b == concept}
    return frozenset({concept} | related)

# Human-readable concept names (adjacent map, ADR 0020). Key-set MUST equal
# CORE_CONCEPT_DEFINITIONS (enforced by the import-time invariant below). Consumed by
# graph_analytics.py (ISA-95 view) via the seeded Concept.name property.
CONCEPT_NAME: dict[str, str] = {
    # ── Equipment & Assets ──────────────────────────────────────────────────
    "equipment-id":          "Equipment Identifier",
    "asset-class":           "Asset Class",
    "lifecycle-state":       "Lifecycle State",
    "criticality":           "Criticality / Priority",
    "runtime-hours":         "Runtime Hours",
    "asset":                 "Asset",
    # ── Production & Lines ──────────────────────────────────────────────────
    "production-line":       "Production Line",
    "production-run":        "Production Run",
    "production-order":      "Production Order",
    "shift":                 "Shift",
    "batch":                 "Production Batch / Lot",
    "availability":          "OEE Availability",
    "performance":           "OEE Performance",
    "oee-score":             "OEE Score",
    # ── Quality ─────────────────────────────────────────────────────────────
    "quality-metric":        "Quality Metric",
    "inspection":            "Inspection",
    "inspection-result":     "Inspection Result",
    "specification":         "Specification",
    "non-conformance":       "Non-Conformance",
    "defect-type":           "Defect Type",
    "scrap-waste":           "Scrap / Waste",
    "actual-quantity":       "Actual Quantity",
    "quality-yield-rate":    "Quality Yield Rate",
    # ── Maintenance ─────────────────────────────────────────────────────────
    "maintenance-event":     "Maintenance Event",
    "maintenance-schedule":  "Maintenance Schedule",
    "failure-mode":          "Failure Mode",
    "downtime":              "Downtime",
    "alarm":                 "Alarm",
    "spare-part":            "Spare Part",
    # ── Sensor & Process Data ────────────────────────────────────────────────
    "sensor-reading":        "Sensor Reading",
    "event-time":            "Event Time",
    "cycle-time":            "Cycle Time",
    "tool-life":             "Tool Life",
    "food-safety":           "Food Safety",
    # ── OT / Machine Telemetry ────────────────────────────────────────────────
    "machine-state":         "Machine State",
    "throughput":            "Throughput",
    "machine-speed":         "Machine Speed",
    "target-machine-speed":  "Target Machine Speed",
    # ── Materials & Inventory ────────────────────────────────────────────────
    "material":              "Material / Part",
    "lot-number":            "Lot Number",
    "inventory-level":       "Inventory Level",
    "mro-inventory":         "MRO Inventory",
    "inventory-transaction": "Inventory Transaction",
    "material-consumption":  "Material Consumption",
    "bill-of-materials":     "Bill of Materials",
    # ── Products & PLM ───────────────────────────────────────────────────────
    "product":               "Product",
    "product-category":      "Product Category",
    "product-revision":      "Product Revision",
    "engineering-change":    "Engineering Change",
    # ── Orders & Supply Chain ────────────────────────────────────────────────
    "order-status":          "Order Status",
    "purchase-order":        "Purchase Order",
    "sales-order":           "Sales Order",
    "schedule":              "Schedule",
    "customer":              "Customer",
    "supplier":              "Supplier / Vendor",
    "invoice":               "Invoice",
    "payment":               "Payment",
    "shipment":              "Shipment",
    "cost":                  "Cost / Price",
    # ── People & Organization ────────────────────────────────────────────────
    "operator":              "Operator",
    "personnel":             "Personnel",
    # ── Locations & Sites ────────────────────────────────────────────────────
    "facility":              "Facility / Plant",
    "site":                  "Manufacturing Site",
    "location":              "Location / Area",
    # ── Work Orders ──────────────────────────────────────────────────────────
    "work-order":            "Work Order",
}

# ISA-95 level (integer 0-4) per concept (adjacent map, ADR 0020). 47 migrated verbatim from
# the retired data/neptune/core.cypher; 7 (availability, performance, oee-score, tool-life,
# food-safety, mro-inventory, payment) + 3 S5.2 entities (asset=2, sales-order=4, shipment=4)
# assigned by meaning. facility(1)/site(4) preserved verbatim from core.cypher. Consumed by
# graph_analytics.py and onboard.py:180 cleanup (deletes Concepts with isa95_level/source NULL).
CONCEPT_ISA95_LEVEL: dict[str, int] = {
    # ── Equipment & Assets ──────────────────────────────────────────────────
    "equipment-id":          2,
    "asset-class":           2,
    "lifecycle-state":       3,
    "criticality":           3,
    "runtime-hours":         3,
    "asset":                 2,
    # ── Production & Lines ──────────────────────────────────────────────────
    "production-line":       3,
    "production-run":        3,
    "production-order":      4,
    "shift":                 3,
    "batch":                 3,
    "availability":          3,
    "performance":           3,
    "oee-score":             3,
    # ── Quality ─────────────────────────────────────────────────────────────
    "quality-metric":        3,
    "inspection":            3,
    "inspection-result":     3,
    "specification":         3,
    "non-conformance":       3,
    "defect-type":           3,
    "scrap-waste":           3,
    "actual-quantity":       3,
    "quality-yield-rate":    3,
    # ── Maintenance ─────────────────────────────────────────────────────────
    "maintenance-event":     3,
    "maintenance-schedule":  3,
    "failure-mode":          3,
    "downtime":              3,
    "alarm":                 2,
    "spare-part":            3,
    # ── Sensor & Process Data ────────────────────────────────────────────────
    "sensor-reading":        2,
    "event-time":            3,
    "cycle-time":            3,
    "tool-life":             2,
    "food-safety":           3,
    # ── OT / Machine Telemetry ────────────────────────────────────────────────
    "machine-state":         3,
    "throughput":            3,
    "machine-speed":         2,
    "target-machine-speed":  2,
    # ── Materials & Inventory ────────────────────────────────────────────────
    "material":              4,
    "lot-number":            3,
    "inventory-level":       4,
    "mro-inventory":         4,
    "inventory-transaction": 4,
    "material-consumption":  3,
    "bill-of-materials":     4,
    # ── Products & PLM ───────────────────────────────────────────────────────
    "product":               4,
    "product-category":      4,
    "product-revision":      3,
    "engineering-change":    4,
    # ── Orders & Supply Chain ────────────────────────────────────────────────
    "order-status":          4,
    "purchase-order":        4,
    "sales-order":           4,
    "schedule":              4,
    "customer":              4,
    "supplier":              4,
    "invoice":               4,
    "payment":               4,
    "shipment":              4,
    "cost":                  4,
    # ── People & Organization ────────────────────────────────────────────────
    "operator":              3,
    "personnel":             3,
    # ── Locations & Sites ────────────────────────────────────────────────────
    "facility":              1,
    "site":                  4,
    "location":              2,
    # ── Work Orders ──────────────────────────────────────────────────────────
    "work-order":            3,
}

# Single canonical Concept MERGE template (ADR 0020). Parameterized so every seed path
# (deploy.sh, graph_reset direct mode, onboard_reset, server backfill) emits byte-identical
# statements. Sets name + isa95_level + source='core' + description on every core concept.
_CORE_CONCEPT_MERGE = (
    "MERGE (c:Concept {id: $id}) "
    "SET c.name = $name, c.isa95_level = $isa95_level, "
    "c.source = 'core', c.description = $description"
)


def core_concept_merges() -> list[tuple[str, dict]]:
    """Canonical (cypher, params) Concept MERGEs — the SINGLE seed source (ADR 0020).

    One statement per CORE_CONCEPT_DEFINITIONS id; every emitted Concept carries
    name + isa95_level + source='core' + description so onboard.py:180 cleanup never
    deletes a core concept and graph_analytics.py viz has its fields. Pure/offline.
    """
    return [
        (_CORE_CONCEPT_MERGE, {
            "id": cid,
            "name": CONCEPT_NAME[cid],
            "isa95_level": CONCEPT_ISA95_LEVEL[cid],
            "description": desc,
        })
        for cid, desc in CORE_CONCEPT_DEFINITIONS.items()
    ]


# Concept → authoritative system type mapping.
# Used by the provision writer (c4) to assign AuthorityRules correctly regardless of onboarding order.
CONCEPT_AUTHORITY = {
    # MES is authoritative for execution data
    "production-run": "MES",
    "production-line": "MES",
    "shift": "MES",
    "batch": "MES",
    "oee-score": "MES",
    "availability": "MES",
    "performance": "MES",
    "quality-metric": "MES",
    "scrap-waste": "MES",
    "actual-quantity": "MES",
    "quality-yield-rate": "MES",
    "cycle-time": "MES",
    "non-conformance": "MES",
    "defect-type": "MES",
    # MES natively tracks live line telemetry (machine state, rates, runtime)
    "machine-state": "MES",
    "throughput": "MES",
    "machine-speed": "MES",
    "target-machine-speed": "MES",
    "inspection-result": "MES",
    "runtime-hours": "MES",
    # ERP is authoritative for planning/commercial data
    "work-order": "ERP",
    "production-order": "ERP",
    "product": "ERP",
    "product-category": "ERP",
    "customer": "ERP",
    "supplier": "ERP",
    "purchase-order": "ERP",
    "sales-order": "ERP",
    "shipment": "ERP",
    "invoice": "ERP",
    "payment": "ERP",
    "material": "ERP",
    "lot-number": "ERP",
    "inventory-level": "ERP",
    "inventory-transaction": "ERP",
    "bill-of-materials": "ERP",
    "cost": "ERP",
    "order-status": "ERP",
    "schedule": "ERP",
    # CMMS is authoritative for maintenance/asset data
    "equipment-id": "CMMS",
    "asset-class": "CMMS",
    "asset": "CMMS",
    "maintenance-event": "CMMS",
    "maintenance-schedule": "CMMS",
    "failure-mode": "CMMS",
    "downtime": "CMMS",
    "spare-part": "CMMS",
    "mro-inventory": "CMMS",
    "criticality": "CMMS",
    "lifecycle-state": "CMMS",
    "alarm": "CMMS",
    # Historian/IIoT is authoritative for real-time sensor data
    "sensor-reading": "Historian",
    "tool-life": "Historian",
    "food-safety": "Historian",
    # PLM is authoritative for engineering data
    "product-revision": "PLM",
    "engineering-change": "PLM",
    "specification": "PLM",
    "inspection": "PLM",
    # Shared/no clear authority — first onboarded wins
    "operator": None,
    "personnel": None,
    "facility": None,
    "site": None,
    "location": None,
    "material-consumption": None,
}

# Concept → set of system types that TYPICALLY provide it (ADR 0007 affinity prior).
# Authored from manufacturing-domain MEANING, NOT from observed LLM output (ADR 0012).
# Superset of CONCEPT_AUTHORITY: the authoritative owner of a concept is always a member
# of that type's set (enforced by the import-time invariant below).
CONCEPT_AFFINITY: dict[str, frozenset[str]] = {
    "MES": frozenset({
        "actual-quantity", "asset-class", "availability", "batch", "cycle-time",
        "defect-type", "downtime", "equipment-id", "inspection", "inspection-result",
        "lot-number", "machine-speed", "machine-state", "material-consumption",
        "non-conformance", "oee-score", "operator", "order-status", "performance",
        "product", "production-line", "production-order", "production-run",
        "quality-metric", "quality-yield-rate", "runtime-hours", "schedule",
        "scrap-waste", "shift", "target-machine-speed", "throughput", "work-order",
    }),  # 32 concepts
    "ERP": frozenset({
        "batch", "bill-of-materials", "cost", "customer", "inventory-level",
        "inventory-transaction", "lot-number", "material", "material-consumption",
        "order-status", "payment", "product", "product-category",
        "production-order", "purchase-order", "sales-order", "schedule",
        "shipment", "supplier", "invoice", "work-order",
    }),  # 21 concepts
    "CMMS": frozenset({
        "alarm", "asset", "asset-class", "cost", "criticality", "downtime",
        "equipment-id", "failure-mode", "lifecycle-state", "maintenance-event",
        "maintenance-schedule", "mro-inventory", "operator", "production-line",
        "runtime-hours", "spare-part", "work-order",
    }),  # 17 concepts
    "Historian": frozenset({
        "alarm", "asset-class", "availability", "cycle-time", "downtime",
        "equipment-id", "food-safety", "machine-state", "oee-score", "performance",
        "production-line", "runtime-hours", "sensor-reading", "shift", "tool-life",
    }),  # 15 concepts; machine-state may originate in OT history, so MES-only affinity was wrong
    "PLM": frozenset({
        "bill-of-materials", "engineering-change", "inspection", "lifecycle-state",
        "product", "product-category", "product-revision", "specification",
    }),  # 8 concepts
}


@functools.lru_cache(maxsize=None)
def system_types_for(concept: str) -> tuple[str, ...]:
    """System types that typically provide ``concept`` (sorted, memoized). () if none."""
    return tuple(sorted(t for t, cs in CONCEPT_AFFINITY.items() if concept in cs))


def missing_expected(system_type: str, mapped_concepts) -> list[str]:
    """Concepts AFFINITY expects from ``system_type`` that are not in ``mapped_concepts``.

    Sorted; returns [] for unknown types. = CONCEPT_AFFINITY.get(type, frozenset()) - mapped.
    """
    return sorted(CONCEPT_AFFINITY.get(system_type, frozenset()) - set(mapped_concepts))


def _validate_concept_affinity(affinity, authority, definitions) -> None:
    """Raise ValueError on (a) unknown concept id or (b) authority ⊄ affinity. Pure; testable."""
    unknown = {c for cs in affinity.values() for c in cs if c not in definitions}
    if unknown:
        raise ValueError(f"CONCEPT_AFFINITY references unknown concept ids: {sorted(unknown)}")
    violations = [(c, t) for c, t in authority.items()
                  if t is not None and c not in affinity.get(t, frozenset())]
    if violations:
        raise ValueError(f"CONCEPT_AUTHORITY ⊄ CONCEPT_AFFINITY: {sorted(violations)}")


def _validate_concept_metadata(names, levels, definitions) -> None:
    """Fail-loud (ADR 0020) if CONCEPT_NAME / CONCEPT_ISA95_LEVEL key-sets drift from
    CORE_CONCEPT_DEFINITIONS. Catches drift at import, not just in the no-drift guard test."""
    core = set(definitions)
    if set(names) != core:
        raise ValueError(
            "CONCEPT_NAME key-set drifted from CORE_CONCEPT_DEFINITIONS: "
            f"missing={sorted(core - set(names))} extra={sorted(set(names) - core)}"
        )
    if set(levels) != core:
        raise ValueError(
            "CONCEPT_ISA95_LEVEL key-set drifted from CORE_CONCEPT_DEFINITIONS: "
            f"missing={sorted(core - set(levels))} extra={sorted(set(levels) - core)}"
        )


# ── Two-layer concept tagging (S5.2 / ADR 0021) ─────────────────────────────
# The static tagging vocabulary + pure table→entity helpers live in ``concept_tagging`` (imported
# and re-exported at the top of this module). CONCEPT_LAYER / FIELD_CONCEPT_IDS stay HERE because
# they are derived over CORE_CONCEPT_DEFINITIONS (which, with its validator, lives in this module);
# building them from the entity-id set ``concept_tagging`` owns avoids a concept_tagging→catalog
# import cycle. A concept's layer is its CANONICAL RENDERING role: 'entity' (in _ENTITY_CONCEPTS)
# or 'field' (all else).
CONCEPT_LAYER: dict[str, str] = {
    cid: ("entity" if cid in _ENTITY_CONCEPTS else "field")
    for cid in CORE_CONCEPT_DEFINITIONS
}
FIELD_CONCEPT_IDS: frozenset[str] = frozenset(
    cid for cid, layer in CONCEPT_LAYER.items() if layer == "field"
)


def _validate_concept_layers(layer, definitions, entity_ids, table_map) -> None:
    """Fail-loud (ADR 0020/0021) layer invariants checked at import: every core concept tagged
    exactly once; values ∈ {entity,field}; ENTITY_CONCEPT_IDS non-empty and ⊆ core; every
    TABLE_ENTITY_MAP target is an entity; the 3 S5.2 entities carry name+isa95+authority+affinity."""
    core = set(definitions)
    if set(layer) != core:
        raise ValueError(f"CONCEPT_LAYER key-set drifted from CORE_CONCEPT_DEFINITIONS: "
                         f"missing={sorted(core - set(layer))} extra={sorted(set(layer) - core)}")
    bad = sorted(c for c, v in layer.items() if v not in ("entity", "field"))
    if bad:
        raise ValueError(f"CONCEPT_LAYER values must be 'entity' or 'field'; bad: {bad}")
    if not entity_ids:
        raise ValueError("ENTITY_CONCEPT_IDS must be non-empty")
    stray = sorted(set(entity_ids) - core)
    if stray:
        raise ValueError(f"ENTITY_CONCEPT_IDS not in CORE_CONCEPT_DEFINITIONS: {stray}")
    bad_targets = sorted({t for t in table_map.values() if t not in entity_ids})
    if bad_targets:
        raise ValueError(f"TABLE_ENTITY_MAP targets are not entity-capable: {bad_targets}")
    for new in ("asset", "sales-order", "shipment"):
        auth = CONCEPT_AUTHORITY.get(new)
        if not (new in core and CONCEPT_NAME.get(new) and new in CONCEPT_ISA95_LEVEL and auth
                and new in CONCEPT_AFFINITY.get(auth, frozenset())):
            raise ValueError(f"new S5.2 entity '{new}' missing name/isa95/authority/affinity")


_validate_concept_affinity(CONCEPT_AFFINITY, CONCEPT_AUTHORITY, CORE_CONCEPT_DEFINITIONS)
_validate_concept_metadata(CONCEPT_NAME, CONCEPT_ISA95_LEVEL, CORE_CONCEPT_DEFINITIONS)
_validate_concept_layers(CONCEPT_LAYER, CORE_CONCEPT_DEFINITIONS, ENTITY_CONCEPT_IDS, TABLE_ENTITY_MAP)

CORE_CONCEPTS = list(CORE_CONCEPT_DEFINITIONS.keys())

MANUFACTURING_CONCEPTS = CORE_CONCEPTS  # backward compatibility


def get_all_concepts(include_retired: bool = False) -> list[str]:
    """Return all concept IDs from Neptune merged with CORE_CONCEPTS fallback.

    Default EXCLUDES concepts whose graph node carries ``retired = true``
    (ADR 0050 D5 — retired concepts leave the map vocabulary); pass
    ``include_retired=True`` for the unfiltered id-set. Core ids are never
    retired, so the CORE_CONCEPTS fallback merge stays unconditional.
    """
    ids = set(CORE_CONCEPTS)
    where = "" if include_retired else " WHERE coalesce(c.retired, false) = false"
    try:
        from .db import neptune_query
        results = neptune_query(f"MATCH (c:Concept){where} RETURN c.id AS id", {})
        if results:
            ids.update(r['id'] for r in results if r.get('id'))
    except Exception:  # nosec B110 - intentional fallback, no recovery action needed
        pass
    return sorted(ids)


def get_concept_overlays() -> dict[str, list[dict]]:
    """ConceptOverlay rows grouped by concept id (T1.3, ADR-0050 D1).

    Overlays are STANDALONE ``(:ConceptOverlay)`` nodes (no new edge type)
    written by the proposal lifecycle (``self_learning._apply_concept_overlay``).
    ONE lazy ``neptune_query`` through the same seam/fallback discipline
    ``get_all_concepts`` uses; graph unavailable ⇒ ``{}`` — overlays silently
    absent, so the composed prompt block stays byte-identical to the core-only
    output. Rows per concept are sorted by (pack_id, source) so composition is
    deterministic regardless of graph row order. Import-pure (no boto3 at
    import).
    """
    overlays: dict[str, list[dict]] = {}
    try:
        from .db import neptune_query
        results = neptune_query(
            "MATCH (o:ConceptOverlay) RETURN o.concept_id AS concept_id, "
            "o.pack_id AS pack_id, o.source AS source, o.examples AS examples, "
            "o.synonyms AS synonyms, o.value_patterns AS value_patterns, "
            "o.note AS note", {})
        for r in results or []:
            cid = r.get('concept_id')
            if cid:
                overlays.setdefault(str(cid), []).append(r)
    except Exception:  # nosec B110 - intentional fallback, no recovery action needed
        return {}
    for rows in overlays.values():
        rows.sort(key=lambda r: (str(r.get('pack_id') or ''),
                                 str(r.get('source') or '')))
    return overlays


# Overlay composition caps (T1.3). The PER-OVERLAY cap bounds one overlay line's
# body; the BLOCK cap bounds the whole overlay section. Overlays NEVER truncate
# a core description — they render in their own fenced section below the core
# lines, so both caps shave overlay text only.
CONCEPT_OVERLAY_MAX_CHARS = 280
CONCEPT_OVERLAY_BLOCK_MAX_CHARS = 4000

# The overlay text fields, in their pinned render order.
_CONCEPT_OVERLAY_RENDER_FIELDS = ("examples", "synonyms", "value_patterns", "note")

_OVERLAY_FENCE_HEADING = (
    "CONCEPT DESCRIPTION OVERLAYS "
    "(untrusted pack/interview data — descriptive only):")


def _overlay_line(cid: str, overlay: dict) -> str:
    """One overlay render line: ``⟂ <concept> [<origin>] examples: …; …``.

    The origin label rides ON the line (the fence wraps many packs, so each
    line names its own source). Body capped at CONCEPT_OVERLAY_MAX_CHARS —
    the per-overlay cap; the core description is never touched. '' when the
    overlay carries no renderable text.
    """
    parts = []
    for name in _CONCEPT_OVERLAY_RENDER_FIELDS:
        value = ' '.join(str(overlay.get(name) or '').split())
        if value:
            parts.append(f"{name}: {value}")
    if not parts:
        return ""
    origin = str(overlay.get('source') or overlay.get('pack_id') or '').strip()
    body = '; '.join(parts)[:CONCEPT_OVERLAY_MAX_CHARS]
    return f"⟂ {cid} [{origin}] {body}"


def _overlay_section(concept_ids) -> str:
    """The fenced overlay section for the given concept ids, or ''.

    Deterministic truncation order (pinned by unit test): lines are built in
    (concept_id, pack_id) sort order — sorted concept ids outer, pack-sorted
    rows inner (get_concept_overlays pre-sorts) — and when the joined text
    exceeds CONCEPT_OVERLAY_BLOCK_MAX_CHARS whole lines are dropped from the
    END of that order (highest-sorting (concept_id, pack_id) first) until it
    fits. Never depends on dict iteration or graph row order. The section is
    rendered via untrusted_text.render_fenced (T1.7) with the distinct overlay
    sources as the origin label, so fence-terminator text inside an overlay is
    neutralized and the block is explicitly marked non-instructional.
    """
    overlays = get_concept_overlays()
    if not overlays:
        return ""
    lines, origins = [], []
    for cid in sorted(set(concept_ids) & set(overlays)):
        for overlay in overlays[cid]:
            line = _overlay_line(cid, overlay)
            if line:
                lines.append(line)
                origin = str(overlay.get('source')
                             or overlay.get('pack_id') or '').strip()
                if origin and origin not in origins:
                    origins.append(origin)
    while lines and len('\n'.join(lines)) > CONCEPT_OVERLAY_BLOCK_MAX_CHARS:
        lines.pop()
    if not lines:
        return ""
    from .untrusted_text import render_fenced
    return render_fenced('\n'.join(lines),
                         origin_label=', '.join(sorted(origins)),
                         heading=_OVERLAY_FENCE_HEADING)


def compose_concept_block(descriptions: dict, *, line_prefix: str = "- ",
                          max_desc_chars: "int | None" = 140,
                          sort_ids: bool = True,
                          bare_when_empty: bool = True) -> str:
    """THE shared overlay-aware composition point (T1.3).

    BOTH map-prompt surfaces route through here — ``concepts_prompt_block``
    (the onboard.py mapping pipeline, 140-char cap) and ``concept_hints_block``
    (the onboard_agent.py ``CONCEPT DEFINITIONS`` header, untruncated) — so
    overlay lookup, the T1.7 untrusted fence, and the overlay caps exist ONCE
    and the two surfaces cannot diverge. Core lines render first (per-surface
    prefix/cap/order); the fenced overlay section, when overlays exist for the
    surfaced concepts, appends BELOW them (overlays never truncate a core
    description). With no graph/overlays the output is byte-identical to the
    core-only block. Deterministic — no LLM (contract C2).
    """
    ids = sorted(descriptions) if sort_ids else list(descriptions)
    lines = []
    for cid in ids:
        desc = ' '.join(str(descriptions[cid] or '').split())
        if max_desc_chars is not None:
            desc = desc[:max_desc_chars]
        if desc or not bare_when_empty:
            lines.append(f"{line_prefix}{cid}: {desc}")
        else:
            lines.append(f"{line_prefix}{cid}")
    block = '\n'.join(lines)
    overlay_section = _overlay_section(ids)
    return block + overlay_section if overlay_section else block


def concepts_prompt_block(max_desc_chars: int = 140) -> str:
    """Format the non-retired concept vocabulary as map-prompt description lines.

    One line per concept, sorted by id: ``- <id>: <description>`` (description
    whitespace-normalized and truncated to ``max_desc_chars``; no description
    ⇒ bare ``- <id>``). ONE graph read via the same lazy ``neptune_query``
    seam ``get_all_concepts`` uses; when the graph is unavailable the block
    falls back to the curated ``CORE_CONCEPT_DEFINITIONS``. Core descriptions
    always come from the curated definitions (the ``concept_layer`` rule).
    Overlay-aware via the shared composition point (T1.3). Deterministic — no
    LLM (contract C2).
    """
    descriptions: dict[str, str] = dict(CORE_CONCEPT_DEFINITIONS)
    try:
        from .db import neptune_query
        results = neptune_query(
            "MATCH (c:Concept) WHERE coalesce(c.retired, false) = false "
            "RETURN c.id AS id, c.description AS description", {})
        for r in results or []:
            cid = r.get('id')
            if cid and cid not in CORE_CONCEPT_DEFINITIONS:
                descriptions[cid] = r.get('description') or ''
    except Exception:  # nosec B110 - intentional fallback, no recovery action needed
        pass
    return compose_concept_block(descriptions, line_prefix="- ",
                                 max_desc_chars=max_desc_chars,
                                 sort_ids=True, bare_when_empty=True)


def concept_hints_block() -> str:
    """The UNTRUNCATED ``CONCEPT DEFINITIONS`` map-prompt surface (T1.3).

    Replaces onboard_agent.py's inline ``'  <id>: <desc>'`` join over
    ``CORE_CONCEPT_DEFINITIONS`` — same lines, same insertion order, NO cap —
    but routed through the shared composition point so this surface carries
    the SAME fenced overlay treatment as ``concepts_prompt_block`` (the pilot-env
    review found it leaking full descriptions un-fenced). With no overlays the
    output is byte-identical to the legacy join.
    """
    return compose_concept_block(dict(CORE_CONCEPT_DEFINITIONS),
                                 line_prefix="  ", max_desc_chars=None,
                                 sort_ids=False, bare_when_empty=False)


def get_schema(sys_id: str) -> str:
    entry = catalog.get(sys_id)
    if entry:
        return entry.get('schema', '')
    from .db import neptune_query
    results = neptune_query("MATCH (s:System {id: $sid}) RETURN s.schema AS schema", {"sid": sys_id})
    return results[0]['schema'] if results else ''


def get_name(sys_id: str) -> str:
    entry = catalog.get(sys_id)
    if entry:
        return f"{entry['name']} ({entry['plant']})"
    from .db import neptune_query
    results = neptune_query("MATCH (s:System {id: $sid}) RETURN s.name AS name", {"sid": sys_id})
    return results[0]['name'] if results else sys_id


# Shared mapping-prompt disambiguation guidance (ADR 0020, mapping-quality hardening W2).
# Single-sourced here (already imported by both onboard.py and onboard_agent.py) and injected
# into BOTH onboarding map prompts so column→concept mapping picks the MOST SPECIFIC concept.
# Pure data — no behavior beyond the added guidance text. The four hints below are eval-exposed
# confusions (measure-vs-entity, attribute-vs-entity, asset-class, facility-vs-location).
MAPPING_DISAMBIGUATION = (
    "DISAMBIGUATION — map each column to its MOST SPECIFIC concept:\n"
    "- A measure/quantity field is a MEASURE concept, not its parent ENTITY. "
    "A spare part's quantity_on_hand / reorder_point is the measure 'mro-inventory' "
    "(NOT the 'spare-part' entity, and NOT ERP 'inventory-level').\n"
    "- An attribute like priority/status is ITS OWN concept, not the parent record. "
    "work_orders.priority -> 'criticality'; work_orders.status -> 'order-status' or "
    "'lifecycle-state' (NOT the 'work-order' entity).\n"
    "- compatible_asset_type -> 'asset-class' (or 'spare-part'), NOT 'equipment-id'.\n"
    "- assets.facility -> 'facility' / 'site', NOT 'location'.\n"
    "- A 'cell', 'work cell', 'work center', or 'station' is a WORK-CENTER-level "
    "location (below site) -> 'production-line', NOT 'site'. 'plant'/'facility' "
    "(the whole site) -> 'site'.\n"
    "- A quality pass/yield RATE (a 0-100 percentage) is the measure 'quality-yield-rate', "
    "NOT 'quality-metric': quality_checks.pass_rate / first_pass_yield (role 'measure') -> "
    "'quality-yield-rate'. 'quality-metric' is for good-unit counts and scores (good_quantity, "
    "quality_score). The surrogate key check_id is an id column: leave it "
    "UNMAPPED; never map a quality concept to check_id or any *_id.\n"
    "- 'actual-quantity' is the total units a work order, run or batch produced. A running "
    "count is NOT 'actual-quantity': a station's piece tally that only grows, or a counter "
    "READING or snapshot logged on an event or history row (the running count recorded at a "
    "start, stop or state change). It accumulates or repeats the count, so summing it "
    "overcounts. Leave such a column UNMAPPED. A row that records "
    "the units made in its OWN interval (a delta, not a running count) is 'throughput'. "
    "Without samples you can't tell the two apart, so for a bare quantity on an event or "
    "log row whose name does not say it is per interval, leave it UNMAPPED.\n"
    "- A count of GOOD units is 'quality-metric'; a count of rejected, scrapped or bad units is "
    "'scrap-waste'. Never map a bad or reject count to 'quality-metric'.\n"
    "- The tallies of a SAMPLED quality check (sample size, units passed and units failed in "
    "that sample) are measures of 'inspection' (role 'measure'), NOT 'quality-metric' or "
    "'scrap-waste': they count the sample, not the production output. Counts of good and "
    "rejected units from inspecting EVERY unit made are output: 'quality-metric' and "
    "'scrap-waste'.\n"
    "- A timestamp of when a record happened (when it started, ended, was checked, recorded "
    "or occurred) is 'event-time' (role 'measure'), the column time windows filter on. Map "
    "it to 'event-time' even when it belongs to a record that has its own concept. Two "
    "exceptions: an end or resolution time that stays empty while the record is still open "
    "(an active downtime, stop or incident) is not 'event-time' and not that record's own "
    "concept: leave it UNMAPPED; "
    "and a planned or scheduled date (a scheduled or planned start, a due date) is "
    "'schedule' (role 'measure'), not 'event-time'.\n"
    "- 'machine-state' is the column holding a machine's or line's current or transitioned "
    "state (running, stopped, idle). The type of an event logged against an order or run "
    "(start, stop, changeover, complete) is NOT 'machine-state' in any role, not even "
    "'correlate': leave it UNMAPPED. The time a state began is 'event-time' only, never "
    "'machine-state'.\n"
    "- A line's or plant's rated CAPACITY (the units it can make per hour or shift) is not "
    "'throughput' (what it actually produced) and not 'target-machine-speed' (a machine's "
    "speed setpoint, compared with its actual speed): leave it UNMAPPED.\n"
    "- 'machine-speed' is a machine's or line's live running speed, read from the machine or "
    "line. The ACTUAL rate stored on an order or run record (its average units per minute "
    "over the whole order or run) is NOT 'machine-speed' and NOT 'performance': leave it "
    "UNMAPPED.\n"
    "- When one record carries a code AND the text description of the same thing, map the "
    "code to the concept it names (a stop, fault or reason code is 'failure-mode') and leave "
    "the text description UNMAPPED.\n"
)
