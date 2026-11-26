# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Pydantic request models for all POST endpoints."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AskRequest(BaseModel):
    question: str
    plant: str = ""
    # Caller-provided Conversation Session (ADR-0005): honored for Memory
    # Events/Summaries coherence. Distinct from the ephemeral Runtime Session.
    session_id: str | None = None
    user_id: str | None = None
    # Optional client temporal context (agents.time_context): the user's IANA
    # timezone (e.g. "America/Denver") and locale tag (e.g. "en-US"). Validated
    # server-side against zoneinfo / a character allowlist; invalid or absent
    # values degrade to the UTC-only temporal line. Synthetic flows (warmup,
    # scheduler, briefings, smoke prompts) omit them and get UTC-only context.
    timezone: str | None = None
    # Optional memory suppression (T1.7). Honored ONLY to DISABLE: ``False``
    # suppresses Memory retrieval AND writes for this turn; ``True`` or absent
    # preserves existing behaviour exactly. A body can NEVER force-enable memory
    # on a briefing-marked session — the eval harness relies on that asymmetry so
    # a measurement run cannot accidentally re-enable the store it is suppressing.
    write_memory: bool | None = None
    locale: str | None = None
    # "Ask again" on the chat's last turn (an attempt whose answer never
    # reached the user). Only an exact ``true`` counts: the app then sends the
    # reserved ``retryUndeliveredTurn`` runtime key, and the runtime leaves
    # the trailing attempts at this question out of the history it reads
    # (agents/history_turns.py). Ordinary sends never set it.
    retry: bool | None = None


class ResolveRequest(BaseModel):
    entity_type: str
    identifier: str


class LearnRequest(BaseModel):
    type: str = 'identity_rule'
    # identity_rule fields
    concept: str = ''
    source_system: str = ''
    source_field: str = ''
    source_pattern: str = ''
    target_system: str = ''
    target_field: str = ''
    target_pattern: str = ''
    confidence: float = 0.7
    # Optional: the identifier the rule was learned from (the one a person
    # typed into the Thread panel) and the value it matched in the target
    # system. /api/thread/learn checks that both runtime readers read them
    # alike (resolve.reader_disagreement); a request without them is checked
    # on a value built from the pattern only.
    example_identifier: str = ''
    example_matched_value: str = ''
    # correlation fields
    concept_a: str = ''
    concept_b: str = ''
    system_a: str = ''
    system_b: str = ''
    pattern: str = ''
    source_metric: str = ''
    target_metric: str = ''
    # decision_boundary fields
    threshold: float = 0.0
    action: str = ''
    applies_to: str = ''
    plant_id: str = ''


class DiscoverRequest(BaseModel):
    system_id: str = ""
    connection_string: str = ""
    schema: str = Field(default="", alias="schema")
    connection_type: str = "PostgreSQL"
    # Optional structured, non-secret connection config for typed connectors
    # (e.g. Athena: workgroup/database/catalog/output_location/region). Ignored by
    # connection types that use connection_string/host instead.
    connection_config: dict = {}
    model_config = {"populate_by_name": True}

    @field_validator("connection_string", mode="before")
    @classmethod
    def _drop_masked_connection_string(cls, v):
        """A credential-MASKED display value is never a usable DSN.

        The catalog GET masks inline credentials to ``://***@`` before serving
        entries; a client that round-trips that display value into discover
        (observed live: the wizard's catalog-card onboard sent it, and the
        database driver 500'd on the literal ``***``) must fall through to the normal
        server-side resolution (node connection → seed bootstrap) exactly as if
        it had sent nothing.
        """
        if isinstance(v, str) and "://***@" in v:
            return ""
        return v


class MapRequest(BaseModel):
    system_id: str
    tables: list[dict]


class OnboardResolveRequest(BaseModel):
    system_id: str
    mappings: list[dict]
    # ADR-0040: UI/inference-confirmed table→entity + identity-key assignments,
    # passed through from provision so resolve can seed entity-anchored IdentityRule
    # candidates order-independently (each item:
    # {"table": str, "entity": str, "identity_fields": list[str], "mint": dict|None}).
    # Additive/optional — existing callers that omit it are unaffected.
    table_entities: list[dict] = []
    # Peers the job's cross-system join sweep leaves out because another
    # resolve of the same unchanged graph sweeps that pair (the workshop
    # seed's coordinated phase 2, ``Phase2JoinSweep`` once;
    # join_discovery.discover_joins_for_system). Rules are not affected: the
    # sweep only files join/crosswalk proposals. Empty: every peer.
    join_skip_peers: list[str] = []
    # The job's join sweep is the ONLY sweep of its pairs (``Phase2JoinSweep``
    # once), whether or not it skips any peer: a failing pair direction is
    # retried and reported (join_discovery.discover_joins_for_system strict).
    join_sweep_strict: bool = False


class ProvisionRequest(BaseModel):
    system_id: str
    system_name: str = ""
    plant: str = ""
    schema: str = Field(default="", alias="schema")
    rules: list[dict] = []
    # The caller says ``rules`` are the ones a person kept at the Identity
    # Rules checkpoint (the wizard; the chat's server-built request). A rule
    # is stamped ``onboarding-llm`` "approved by the user at the Identity
    # Rules checkpoint" only when the request says so AND the rule, exactly as
    # sent, is one the server-held Resolve result ``resolve_job_id`` names
    # listed for this system (onboard._checkpoint_rule_ids); every other rule
    # is written with the method ``provision-request``
    # (onboard._dp_write_identity_rule). The flag alone proves nothing.
    rules_from_checkpoint: bool = False
    # ``own``: the provision's replace of the onboarding rule artifacts
    # deletes only the rules this system wrote, not a peer's rules pointing
    # at it (onboard._delete_own_onboarding_rule_artifacts). Only the
    # workshop seed's coordinated phase 2 sends it (``Phase2RuleDelete``
    # own): every seeded system replaces its own rules there, and a peer's
    # rules written before this job's delete landed are kept. ``all`` (the
    # default, every other caller): every rule touching the system.
    rule_delete_scope: Literal["all", "own"] = "all"
    # The Resolve job whose result the checkpoint showed: its status answer
    # carries it as ``resolve_job_id``, and the server keeps that result's
    # rule digests for a day (onboard._bind_resolve_rules). Bounded: it is
    # part of a shared-table key.
    resolve_job_id: str = Field(default="", max_length=128)
    mappings: list[dict] = []
    value_mappings: list[dict] = []
    # ADR-0040: per-table entity + identity-key assignments confirmed by the MAP UI
    # (or supplied by inference). Each item:
    # {"table": str, "entity": str, "identity_fields": list[str], "mint": dict|None}
    # where ``mint`` = {"id","name","description"} when a NEW entity concept was chosen.
    # Optional/additive — omitting it preserves today's provision behavior (the
    # curated ``entity_for_table`` fast-path still writes the entity edge).
    table_entities: list[dict] = []
    # Wave 4 (item H): OPTIONAL per-table free-text query-guidance notes captured at
    # the MAP checkpoint (each item {"table": str, "note": str}). Provision writes
    # each non-empty note through the existing bounded/idempotent
    # ``self_learning.append_query_guidance_note`` as ``"<table>: <note>"``, so the
    # orchestrator reads it at query time. Additive — omitting it (default ``None``)
    # preserves today's provision behavior byte-identically. Items are validated at
    # this boundary (``table`` ≤ 128 chars, ``note`` ≤ 500 chars, both strings when
    # present) — an oversized note is REJECTED (HTTP 422), never truncated, so a
    # note can never evict prior LEARNED guidance via the evolve/prune path.
    table_notes: list[dict] | None = None
    connection_type: str = "PostgreSQL"
    connection_string: str = ""
    # Optional structured, non-secret connection config for typed connectors
    # (e.g. Athena: workgroup/database/catalog/output_location/region).
    connection_config: dict = {}
    model_config = {"populate_by_name": True}

    @field_validator("table_notes")
    @classmethod
    def _validate_table_notes(cls, v: list[dict] | None) -> list[dict] | None:
        """Boundary validation for table-note items (reject, never truncate)."""
        for i, item in enumerate(v or []):
            table = item.get("table")
            if table is not None and not isinstance(table, str):
                raise ValueError(f"table_notes[{i}].table must be a string")
            if isinstance(table, str) and len(table) > 128:
                raise ValueError(
                    f"table_notes[{i}].table exceeds 128 characters"
                )
            note = item.get("note")
            if note is not None and not isinstance(note, str):
                raise ValueError(f"table_notes[{i}].note must be a string")
            if isinstance(note, str) and len(note) > 500:
                raise ValueError(
                    f"table_notes[{i}].note exceeds 500 characters"
                )
        return v


class ConnectRequest(BaseModel):
    """Register a new (typically non-catalog) system so it can be onboarded.

    The HTTP peer of the onboarding agent's ``connect_new_system`` tool — both are
    thin adapters over the SAME ``connect_new_system`` business logic (DRY). Lets
    the UI / programmatic drivers add typed connectors (Athena, SiteWise, etc.)
    with a structured ``connection_config``.
    """
    name: str
    system_type: str = "Custom"
    plant: str = ""
    connection_type: str = "PostgreSQL"
    connection_string: str = ""
    schema: str = Field(default="", alias="schema")
    connection_config: dict = {}
    # Guidance-through-registration: an onboarding driver may supply the system's
    # query_guidance/capabilities/tool_prefix at connect time so they are carried
    # through to the Neptune System node at provision (the orchestrator reads them
    # at query time). Omitted => the generic per-connection-type default applies.
    query_guidance: str = ""
    capabilities: str = ""
    tool_prefix: str = ""
    # Explicit id passthrough: when supplied it is used VERBATIM (sanitized to a
    # tool-safe slug) instead of slugifying ``name`` — so a programmatic driver can
    # pin the canonical system id (e.g. ``mes-mil``). Accepts ``id`` as an alias.
    system_id: str = Field(default="", alias="id")
    model_config = {"populate_by_name": True}


class VerifyRequest(BaseModel):
    system_id: str
    schema: str = Field(default="", alias="schema")
    model_config = {"populate_by_name": True}


class OnboardChatRequest(BaseModel):
    step: str
    system_id: str
    data: dict = {}
    message: str
    history: list[dict] = []


class SetupRequest(BaseModel):
    packs: list[str]  # e.g. ['automotive', 'pharma']


class RemapField(BaseModel):
    field_id: str
    concept_id: str
    role: str  # identifier, measure, correlate, derive
    confidence: float = 0.9
    old_concept: str = ''


class RemapRequest(BaseModel):
    remaps: list[RemapField]


class ReconcileRequest(BaseModel):
    """``POST /api/onboard/reconcile``: the system whose onboarding just
    finished and the id of the provision run that onboarding was. The run's
    new concepts are the only ones the check compares against; no run id, no
    suggestions."""
    system_id: str = Field(default="", max_length=256)
    run_id: str = Field(default="", max_length=256)


class ResolveMetricRequest(BaseModel):
    metric_id: str


class ResolveMetricByDimensionRequest(BaseModel):
    """Resolve a direct metric GROUP BY a dimension concept (S5.3 §6.3).

    ``system_id`` (2A, optional): pin authority to ONE system for the whole
    grouped resolve (per-site bars / the line-state per-machine drill). Absent
    ⇒ the single-authority behavior, byte-identical."""
    metric_id: str
    dimension_concept: str
    system_id: str | None = None


class ResolveHeatmapRequest(BaseModel):
    """Resolve a direct metric over TWO dimension concepts into a severity grid
    (S5.3 §6.3 sensor heatmap). ``threshold_concept`` is optional — absent/unmapped
    degrades the grid to 'uncovered' (no fabricated threshold)."""
    metric_id: str
    dimension_concept_x: str
    dimension_concept_y: str
    threshold_concept: str | None = None


class SetAuthorityRequest(BaseModel):
    concept: str
    system_id: str
    # ADR-0076 D2: the steward's binding choice — the EXACT stored Field.name
    # (may contain dots) and, when that name is bound on 2+ tables, the
    # SchemaEntry.name. Absent ⇒ the historical system-authority write only.
    field: str | None = None
    table: str | None = None


class UserDefinedMappingRequest(BaseModel):
    """A relationship an END USER asserts directly (source='user'), a learning the
    AI never proposed. Peer of a metric correction, but for the general
    cross-system value-mapping surface. All fields default to "" so the handler can
    return a clear 400 listing the missing required field(s) rather than a 422. The
    handler forwards them verbatim to one of two doors, by the caller's steward
    verdict: a steward's to ``self_learning.record_user_correction`` (accepted on
    entry, user wins over any agent proposal), anyone else's to
    ``self_learning.record_user_proposal`` (filed pending for steward review, with
    no graph access). ``description`` and ``proposed_type`` are optional."""
    from_system: str = ""
    from_field: str = ""
    from_value: str = ""
    to_system: str = ""
    to_field: str = ""
    to_value: str = ""
    concept: str = ""
    description: str = ""
    proposed_type: str = "value_mapping"


class EditMappingRequest(BaseModel):
    """Request to edit an existing pending self-learning proposal before accepting.
    
    All fields are optional - only provided fields will be updated. Fields are
    validated against the onboarded schema when they refer to column names
    (from_field, to_field). Any other key is refused (422): an edit changes the
    relationship a proposal names, never where the row came from (``source``,
    ``method``, ``confidence``), its status or its ids.
    """
    model_config = ConfigDict(extra="forbid")

    from_system: str | None = None
    from_field: str | None = None
    from_value: str | None = None
    to_system: str | None = None
    to_field: str | None = None
    to_value: str | None = None
    concept: str | None = None
    description: str | None = None