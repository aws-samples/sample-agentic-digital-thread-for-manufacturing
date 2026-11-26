# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""SFC configuration generator — CONTENT, not engine (ADR-0044, ADR-0082).

Shop Floor Connectivity (SFC) writes the OPC UA telemetry to AWS IoT SiteWise
and S3 Tables (ADR-0082). It is the only continuous SiteWise writer where the
HighByte ``NameSpace_to_SiteWise`` pipeline is off
(``highbyte_sitewise_pipeline=disabled``, the live workshop package) or no hub
is deployed (``deploy.sh --highbyte --sfc`` defaults to the pipeline off); with
``--highbyte-sitewise-pipeline enabled`` the pipeline stays on, so both write. SFC is driven by a single JSON config; this module GENERATES that config (ADR-0082 decision 6)
from the SAME tag map the OPC UA datasets are generated from
(``highbyte/generate_datasets.py``), so every NodeId, asset, property and table
column is derived from one existing source of truth rather than transcribed.

Every value handled here — site names, tag paths, asset and model names — is a
demo literal, so the module lives under ``agents/demo/`` (ADR-0044). It is pure
and dependency-free (stdlib only): no AWS, no boto3, and it writes no file.
``stacks/sfc_stack.py`` calls it at synth and writes the result into the SFC
image's staged build context under ``cdk.out`` (ADR-0082 decision 6), so no
deploy step is needed.

What the config carries (ADR-0082 decisions 2, 3, 5, 8):

* one ``OpcuaServers`` entry per site, with ``Address``/``Port``/``Path`` split
  out of the stack's ``opc.tcp://<host>:26543/UA/ManufacturingServer`` endpoint
  (decision 8);
* ``Sources`` whose ``Channels`` address each variable by its STRING NodeId
  ``ns=<index>;s=<alias>`` — the alias is the tag's full slash path, exactly how
  ``server.mjs`` registers it (decision 6);
* an ``AWS-SITEWISE`` target in ``Assets`` mapping mode with explicit
  ``Properties`` (each a ``DataPath`` JMESPath ``@.sources.<source>.values.
  <channel>.value``), never ``AssetCreation`` — keeping model-mutating IAM off
  SFC's role (decision 2); assets are addressed by the external id of the tree
  :func:`asset_tree` describes (decision 9);
* an ``AWS-S3-TABLES`` target with one ``Mappings`` entry per channel against
  the narrow one-row-per-reading schema (decision 5).

The asset and property naming is a PORT of the HighByte transform, not a new
scheme (decision 3). :func:`resolve_model` reproduces the ``getModel`` function
in ``highbyte/pipeline_sitewise.json`` exactly. SFC may only write a property
that already exists on one of the 28 pre-created models
(``highbyte/DeploySitewiseModels.py``); a channel whose resolved property is not
modelled is DROPPED, which is the intended failure direction (ADR-0082
consequences) — see :data:`MODEL_MEASUREMENTS`.

The tag-map import wrinkle (ADR-0082, and the T3 brief): ``generate_datasets.py``
is not part of the ``agentic_digital_thread`` package and has no ``__init__``, so
it cannot be ``import``-ed by dotted name. It is loaded BY PATH with
``importlib`` — the same pattern ``app.py`` uses to regenerate missing datasets
at synth time and that ``tests/unit/test_generate_datasets_totals.py`` uses to
exercise it. No file outside this task changes.
"""
from __future__ import annotations

import importlib.util
import json
import random
import re
import sys
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from types import ModuleType
from typing import Any

# ---------------------------------------------------------------------------
# Site topology (demo literals — ADR-0044). Each site is one OPC UA server and
# one SFC source; the prefix is the tag map's own alias prefix and the area is
# its middle segment, so both agree with generate_datasets.py by construction.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Site:
    """One manufacturing site: its OPC UA server and its tag-map generator.

    ``key`` is the SFC source/server id; ``prefix`` is the alias prefix the tag
    map builds from (``AnyCompany/<City>/<Area>``); ``area`` is the area segment
    the HighByte transform passes to ``getModel``.

    The tag map comes from ``generate_datasets.py`` in one of two ways, matching
    how that module itself builds the site: ``tag_fn`` names a per-line
    ``fn(prefix, line)`` generator called for every :data:`LINES` entry, or
    ``lines_fn`` names a whole-site ``fn(prefix) -> [(tags, schedule), ...]``
    builder (Indianapolis: ``machining_site_lines``, which alone knows that
    Line3's mill is the CMX-441 story asset). Exactly one is set.
    """

    key: str
    prefix: str
    area: str
    tag_fn: str | None = None
    lines_fn: str | None = None

    def line_tags(self, datasets: ModuleType) -> list[tuple[str, dict[str, tuple[str, Any]]]]:
        """``[(line, {alias: (dtype, gen)}), ...]`` for this site, in line order."""
        if (self.tag_fn is None) == (self.lines_fn is None):
            raise ValueError(f"site {self.key!r}: set exactly one of tag_fn / lines_fn")
        if self.tag_fn is not None:
            fn = getattr(datasets, self.tag_fn)
            return [(line, fn(self.prefix, line)) for line in LINES]
        out: list[tuple[str, dict[str, tuple[str, Any]]]] = []
        for tags, _schedule in getattr(datasets, self.lines_fn)(self.prefix):
            # The line is the alias segment after the prefix; every alias of
            # one line agrees on it by construction of the tag map.
            lines = {a[len(self.prefix) + 1:].split("/", 1)[0] for a in tags}
            if len(lines) != 1:
                raise ValueError(f"site {self.key!r}: tags span lines {sorted(lines)}")
            out.append((lines.pop(), tags))
        return out


# One entry per OPC UA server in stacks/highbyte_opcua_stack.py OPC_UA_SITES
# (key = that name lowercased); test_sites_match_the_opcua_servers pins the two
# together so a new server cannot be silently left out of SFC again.
SITES: tuple[Site, ...] = (
    Site("milwaukee", "AnyCompany/Milwaukee/Bottling", "Bottling", tag_fn="bottling_tags"),
    Site("detroit", "AnyCompany/Detroit/AutoParts", "AutoParts", tag_fn="automotive_tags"),
    Site("cedarrapids", "AnyCompany/CedarRapids/FoodProcessing", "FoodProcessing", tag_fn="food_tags"),
    # ADR-0072: the fourth site, carrying the CMX-441 story live on Line3.
    Site("indianapolis", "AnyCompany/Indianapolis/Machining", "Machining",
         lines_fn="machining_site_lines"),
)

# The three production lines every site runs (generate_datasets.py: Line1..3).
LINES: tuple[str, ...] = ("Line1", "Line2", "Line3")

# OPC UA endpoint shape the highbyte_opcua_stack emits, one per site:
# ``opc.tcp://<host>:26543/UA/ManufacturingServer`` (stacks/highbyte_opcua_stack.py).
DEFAULT_OPCUA_PORT = 26543
DEFAULT_OPCUA_PATH = "/UA/ManufacturingServer"

# STRING-NodeId namespace index. server.mjs sets every NodeId's namespace to
# ``registerNamespace('ManufacturingPlatform').index``. That index is assigned
# at RUNTIME after server.initialize() has created namespace 0 (the OPC UA
# namespace) and namespace 1 (the server's own URN), so under node-opcua it is
# 2 — but this is a runtime property, not a statically guaranteed constant, so
# it is a parameter (default 2), never a silent hardcode. See the module note
# and generate_sfc_config(namespace_index=...).
DEFAULT_NAMESPACE_INDEX = 2


# ---------------------------------------------------------------------------
# Model measurement schemas — the 28 pre-created models
# (highbyte/DeploySitewiseModels.py). SFC may only write a property that
# already exists on one of these (ADR-0082 decisions 2 and its consequences).
# This is loaded from DeploySitewiseModels.py by path (same wrinkle, same
# importlib pattern as the tag map) so the allow-set can never drift from the
# models the deploy actually creates.
# ---------------------------------------------------------------------------

_HIGHBYTE_DIR = Path(__file__).resolve().parents[3] / "highbyte"
_DATASETS_PATH = _HIGHBYTE_DIR / "generate_datasets.py"
_MODELS_PATH = _HIGHBYTE_DIR / "DeploySitewiseModels.py"


def _load_by_path(
    name: str, path: Path, stub_modules: tuple[str, ...] = ()
) -> ModuleType:
    """Import a highbyte module BY PATH (app.py / test_generate_datasets_totals
    importlib pattern). The highbyte tree is not an installed package, so it
    cannot be imported by dotted name; nothing outside this task changes.

    ``stub_modules`` names top-level imports to satisfy with an empty stub while
    the module executes — used for ``DeploySitewiseModels.py``'s ``import boto3``
    (boto3 is used only inside its ``main()`` at deploy time, never at import,
    so a stub lets this pure generator read its static schema without depending
    on the AWS SDK being installed). Any pre-existing real module is left alone.
    """
    injected: list[str] = []
    for stub in stub_modules:
        if stub not in sys.modules:
            sys.modules[stub] = ModuleType(stub)
            injected.append(stub)
    try:
        spec = importlib.util.spec_from_file_location(name, str(path))
        if spec is None or spec.loader is None:  # pragma: no cover - defensive
            raise ImportError(f"cannot load {name} from {path}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        for stub in injected:
            sys.modules.pop(stub, None)


def _model_measurements() -> dict[str, set[str]]:
    """Map every SiteWise model name to the set of property names SFC may write.

    Built from DeploySitewiseModels.py's ``PROCESS_MEASUREMENTS`` plus the
    per-model ``HB_ROUTING`` fields it appends to every process model. The
    ``_path``/``_hierarchyName`` routing measurements are HighByte's own and are
    not tag-map channels, so they never match a channel and need no special
    exclusion here.
    """
    models = _load_by_path(
        "deploy_sitewise_models_under_gen", _MODELS_PATH, stub_modules=("boto3",)
    )
    routing = {m["name"] for m in models.HB_ROUTING}
    return {
        name: {m["name"] for m in props} | routing
        for name, props in models.PROCESS_MEASUREMENTS.items()
    }


MODEL_MEASUREMENTS: dict[str, set[str]] = _model_measurements()


def _model_property_types() -> dict[str, dict[str, str]]:
    """``{model: {property: SiteWise dataType}}`` for the process models.

    SFC's asset-by-external-id mode does not look the type up itself, and
    SiteWise rejects a value whose type differs from the property's, so the
    config states the MODEL's type (not the tag's) for every property."""
    models = _load_by_path(
        "deploy_sitewise_models_types_under_gen", _MODELS_PATH, stub_modules=("boto3",)
    )
    return {
        name: {m["name"]: m["dataType"] for m in list(props) + list(models.HB_ROUTING)}
        for name, props in models.PROCESS_MEASUREMENTS.items()
    }


MODEL_PROPERTY_TYPES: dict[str, dict[str, str]] = _model_property_types()


# Model resolution (decision 3) and the asset-tree shape (decision 9) live in
# the pure sibling ``sitewise_model.py``; re-exported here so callers keep one
# import. Loaded by path for the same reason this module is (see above), and
# registered in ``sys.modules`` first because dataclasses resolve their module
# by name while the class body runs.
def _load_sibling(name: str, filename: str) -> ModuleType:
    """Load a sibling module by path once, registered BEFORE it executes."""
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            name, str(Path(__file__).with_name(filename)))
        if spec is None or spec.loader is None:  # pragma: no cover - defensive
            raise ImportError(f"cannot load {filename}")
        sys.modules[name] = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(sys.modules[name])
        except BaseException:
            sys.modules.pop(name, None)
            raise
    return sys.modules[name]


_sitewise_model = _load_sibling("adt_sitewise_model", "sitewise_model.py")
resolve_model = _sitewise_model.resolve_model
_suffix = _sitewise_model._suffix
_channel_property = _sitewise_model._channel_property
external_id = _sitewise_model.external_id
AssetSpec = _sitewise_model.AssetSpec
Channel = _sitewise_model.Channel
_CHILD_HIERARCHY = _sitewise_model._CHILD_HIERARCHY


def _line_nodes(tags: dict[str, tuple[str, Any]], line: str) -> dict[str, dict[str, str]]:
    """Group a line's tag aliases into ``{process_node: {leaf: alias}}``.

    Mirrors how server.mjs walks the alias path: the penultimate '/' segment is
    the process node (a folder) and the last is the leaf variable.
    """
    nodes: dict[str, dict[str, str]] = {}
    for alias in tags:
        parts = alias.split("/")
        if len(parts) < 2:
            continue
        node, leaf = parts[-2], parts[-1]
        nodes.setdefault(node, {})[leaf] = alias
    return nodes


def resolve_channels(datasets: ModuleType | None = None) -> list[Channel]:
    """Resolve every writable channel across every site from the tag map.

    For each site × line × process node, reproduce the transform's node→model
    resolution, then keep only leaves whose derived property exists on the
    resolved model (ADR-0082: SFC never writes an unmodelled property). Ordering
    is deterministic (site, line, node, leaf) so the generated config is stable.
    """
    if datasets is None:
        datasets = _load_by_path("generate_datasets_for_sfc", _DATASETS_PATH)

    channels: list[Channel] = []
    for site in SITES:
        for line, tags in site.line_tags(datasets):
            nodes = _line_nodes(tags, line)
            for node, leaves in nodes.items():
                suffix = _suffix(node, line)
                attr_keys = [_channel_property(node, leaf) for leaf in leaves]
                model = resolve_model(suffix, attr_keys, site.area)
                allowed = MODEL_MEASUREMENTS.get(model)
                if allowed is None:
                    # Not one of the 28 pre-created models (e.g. the line-level
                    # metadata node whose suffix is the line itself): dropped.
                    continue
                asset = _asset_name(node, leaves, tags)
                for leaf, alias in sorted(leaves.items()):
                    prop = _channel_property(node, leaf)
                    if prop not in allowed:
                        continue  # unmodelled property — dropped (intended)
                    channels.append(
                        Channel(site.key, site.area, line, asset, model, prop,
                                alias, tags[alias][0], node)
                    )
    return channels


def _asset_name(node: str, leaves: dict[str, str], tags: dict[str, tuple[str, Any]]) -> str:
    """SiteWise asset name for a process node.

    The node name (the HighByte transform's ``_name``), unless the tag map gives
    the node an explicit, constant ``<node>_AssetName`` tag — how
    ``generate_datasets.py`` names the CMX-441 story mill
    (``condition_monitor.STORY_SITEWISE_ASSET``, the asset the rest of the
    workshop and ``lambda-sitewise-feed`` address). Read from the tag map, never
    hard-coded here; a non-constant name is refused rather than sampled.
    """
    alias = next((a for leaf, a in leaves.items()
                  if _channel_property(node, leaf) == "AssetName"), None)
    if alias is None:
        return node
    dtype, gen = tags[alias]
    names = {gen(step) for step in (0, 1, 1000)}
    if dtype != "STRING" or len(names) != 1:
        raise ValueError(f"{alias}: AssetName must be a constant STRING tag")
    return names.pop()


def asset_tree(channels: list[Channel] | None = None) -> list[AssetSpec]:
    """The Enterprise/Site/Area/Line/process tree the channels write into
    (ADR-0082 decision 9; built by :func:`sitewise_model.asset_tree`)."""
    return _sitewise_model.asset_tree(resolve_channels() if channels is None else channels)


# ---------------------------------------------------------------------------
# Endpoint split (ADR-0082 decision 8).
# ---------------------------------------------------------------------------


def split_endpoint(endpoint: str) -> tuple[str, int, str]:
    """Split ``opc.tcp://<host>:<port>/<path>`` into ``(address, port, path)``.

    The stack emits ``opc.tcp://<nlb-dns>:26543/UA/ManufacturingServer``; SFC's
    ``OpcuaServers`` entry needs ``Address``/``Port``/``Path`` separately.

    Two details are load-bearing, both established by running SFC against
    a generated config rather than inferred:

    * ``Address`` KEEPS the ``opc.tcp://`` scheme. Stripping it makes SFC compose
      a schemeless URL and fail with ``unsupported protocol: null``. The upstream
      Quickstart agrees (``"Address": "opc.tcp://localhost"``).
    * ``Path`` carries NO leading slash. SFC joins address, port and path with a
      ``/`` of its own, so a leading slash yields ``…:26543//UA/…``.

    Together these compose ``opc.tcp://<host>:26543/UA/ManufacturingServer``,
    which SFC accepts and uses to fetch the server's endpoints.
    """
    m = re.match(r"^opc\.tcp://([^:/]+):(\d+)(/.*)?$", endpoint)
    if not m:
        raise ValueError(f"not an opc.tcp endpoint: {endpoint!r}")
    host, port, path = m.group(1), int(m.group(2)), m.group(3) or ""
    return f"opc.tcp://{host}", port, path.lstrip("/")


# ---------------------------------------------------------------------------
# S3 Tables narrow schema (ADR-0082 decision 5): one row per reading, day
# partition. event_time is required; the rest are the reading's coordinates.
#
# ``value`` is Optional=False in the CONFIGURED schema even though the table
# column is nullable. Every site's poll is evaluated against EVERY mapping (all
# four sites'), and SFC writes a row for each mapping whose required fields
# resolve -- so with only event_time required, the other sites' mappings and any
# channel absent from this poll each produced an all-null row (75% of rows on
# the 2026-09-26 sandbox run). SFC documents a null on a non-optional column as
# "value not present, no record for that mapping". The writer's schema check
# compares names and types only, so the nullable table column still matches.
# ---------------------------------------------------------------------------

S3_TABLE_SCHEMA: list[dict[str, Any]] = [
    {"Name": "event_time", "Type": "timestamptz", "Optional": False},
    {"Name": "site", "Type": "string"},
    {"Name": "area", "Type": "string"},
    {"Name": "line", "Type": "string"},
    {"Name": "asset", "Type": "string"},
    {"Name": "channel", "Type": "string"},
    {"Name": "value", "Type": "double", "Optional": False},
    {"Name": "quality", "Type": "string"},
]
S3_TABLE_PARTITION: dict[str, str] = {"day": "event_time"}

#: S3 Tables writer batching (SFC BufferCount / Interval). One write per
#: minute or per 5,000 records, whichever comes first; see _s3tables_target.
S3_TABLES_BUFFER_COUNT = 5000
S3_TABLES_INTERVAL_MS = 60_000

#: Value of the ``quality`` column. SFC's OPC UA adapter passes only
#: Good-status values to its targets, so every stored reading is Good.
QUALITY_GOOD = "GOOD"

#: Transformation that turns a BOOLEAN reading into 0/1 for the double
#: ``value`` column, and the suffix of the channel that carries it.
BOOL_TO_NUMBER = "BoolToNumber"
BOOL_NUMERIC_SUFFIX = "__num"
TRANSFORMATIONS: dict[str, list[dict[str, str]]] = {
    BOOL_TO_NUMBER: [{"Operator": "BoolToNumber"}],
}


# ---------------------------------------------------------------------------
# Config assembly.
# ---------------------------------------------------------------------------


def _data_path(source: str, channel_id: str) -> str:
    """JMESPath into SFC's per-poll value bag (ADR-0082 decision 2)."""
    return f"@.sources.{source}.values.{channel_id}.value"


def _literal(value: str) -> dict[str, str]:
    """DO NOT USE for a constant column — kept only to fail loudly.

    SFC's ValueQuery accepts a JMESPath EXPRESSION, and its parser rejects
    JMESPath literals outright. Verified against SFC: both the JSON
    literal form (```` `"milwaukee"` ````) and the raw-string form
    (``'milwaukee'``) are refused with ``ValueQuery … is not a valid JmesPath
    expression``, which aborts the whole S3 Tables target writer. Constants must
    therefore be declared as ``Metadata`` and READ with a real path — see
    :func:`_source_meta_path` and :func:`_channel_meta_path`.
    """
    raise NotImplementedError(
        "SFC rejects JMESPath literals in ValueQuery; declare the value as "
        "source/channel Metadata and reference it with a path instead"
    )


def _source_meta_path(source: str, key: str) -> dict[str, str]:
    """A ColumnMappingConfiguration reading a SOURCE-level metadata value."""
    return {"ValueQuery": f"@.sources.{source}.metadata.{key}"}


def _channel_meta_path(source: str, channel_id: str, key: str) -> dict[str, str]:
    """A ColumnMappingConfiguration reading a CHANNEL-level metadata value."""
    return {"ValueQuery": f"@.sources.{source}.values.{channel_id}.metadata.{key}"}


def _channels_by_site(datasets: ModuleType | None) -> dict[str, list[Channel]]:
    """Every resolved channel grouped by its site key."""
    by_site: dict[str, list[Channel]] = {}
    for ch in resolve_channels(datasets):
        by_site.setdefault(ch.site, []).append(ch)
    return by_site


def _opcua_server(endpoint: str) -> dict[str, Any]:
    """One ``OpcuaServers`` entry for a site's endpoint (decision 8)."""
    address, port, path = split_endpoint(endpoint)
    return {
        "Address": address,
        "Port": port,
        "Path": path,
        "ConnectionType": "None",
    }


def _source_channel(ch: Channel, namespace_index: int) -> dict[str, Any]:
    """The SFC source channel reading one OPC UA variable."""
    return {
        "Name": ch.channel_id,
        "NodeId": f"ns={namespace_index};s={ch.alias}",
        # Read back by the S3 Tables mappings below — SFC rejects a
        # JMESPath literal, so a per-row constant travels as metadata.
        "Metadata": {
            "line": ch.line,
            "asset": ch.asset,
            "channel": ch.channel_id,
        },
    }


def _add_sitewise_property(
    sitewise_assets: dict[str, dict[str, Any]], source_id: str, ch: Channel
) -> None:
    """Append ``ch`` as a Property on its SiteWise asset.

    The asset is addressed by its EXTERNAL ID, never its name: names repeat
    across sites (HighByte's tree has a Line1Washer at every site), external ids
    do not (asset_tree / ADR-0082 decision 9)."""
    asset = sitewise_assets.setdefault(
        ch.asset_external_id,
        {"AssetExternalId": ch.asset_external_id, "Properties": []},
    )
    asset["Properties"].append(
        {
            "PropertyName": ch.prop,
            "DataType": MODEL_PROPERTY_TYPES[ch.model][ch.prop].lower(),
            "DataPath": _data_path(source_id, ch.channel_id),
            # Each poll carries ONE site's values, and SFC evaluates every
            # asset's DataPaths against it, so the other sites' properties are
            # absent on every poll by design. SFC's default (true) logged one
            # warning per absent property: ~170 lines/s on a 4-site config
            # while the task ran at 90-100% CPU (live 2026-10-03). Same
            # reasoning as the S3 Tables target's WarnIfValueMissing below.
            "WarnIfNotPresent": False,
        }
    )


def _table_channel(source_channels: dict[str, Any], ch: Channel) -> str | None:
    """The channel id the S3 Tables row reads ``value`` from, or None.

    The narrow schema's ``value`` column is a double (ADR-0082 decision 5).
    BOOLEAN channels (Down, BPFO_Alarm, ...) are read a second time through a
    BoolToNumber channel so the table gets 0/1 while SiteWise still receives the
    raw boolean its BOOLEAN property requires. STRING channels (Status, ...)
    have no numeric form and are written to SiteWise only."""
    if ch.dtype == "STRING":
        return None
    if ch.dtype != "BOOLEAN":
        return ch.channel_id
    table_ch = f"{ch.channel_id}{BOOL_NUMERIC_SUFFIX}"
    source_channels[table_ch] = {
        **source_channels[ch.channel_id],
        "Name": table_ch,
        "Transformation": BOOL_TO_NUMBER,
    }
    return table_ch


def _s3_mapping(source_id: str, table_ch: str) -> dict[str, Any]:
    """One S3 Tables ``Mappings`` entry against the narrow schema.

    Every value is a ColumnMappingConfiguration — SFC requires
    {"ValueQuery": <JMESPath>}, never a bare string — and a JMESPath LITERAL is
    rejected by SFC's parser, so the dimension columns are declared as Metadata
    (source-level for site/area, channel-level for line/asset/channel) and read
    back with a real path. Verified against SFC."""
    return {
        "event_time": {"ValueQuery": "@.timestamp"},
        "site": _source_meta_path(source_id, "site"),
        "area": _source_meta_path(source_id, "area"),
        "line": _channel_meta_path(source_id, table_ch, "line"),
        "asset": _channel_meta_path(source_id, table_ch, "asset"),
        "channel": _channel_meta_path(source_id, table_ch, "channel"),
        "value": {"ValueQuery": _data_path(source_id, table_ch)},
        "quality": _source_meta_path(source_id, "quality"),
    }


def _site_source(
    site: Site,
    site_channels: list[Channel],
    namespace_index: int,
    sitewise_assets: dict[str, dict[str, Any]],
    s3_mappings: list[dict[str, Any]],
) -> dict[str, Any]:
    """A site's SFC source; adds its SiteWise properties and S3 Tables rows."""
    source_id = site.key
    source_channels: dict[str, Any] = {}
    for ch in site_channels:
        source_channels[ch.channel_id] = _source_channel(ch, namespace_index)
        _add_sitewise_property(sitewise_assets, source_id, ch)
        table_ch = _table_channel(source_channels, ch)
        if table_ch is not None:
            s3_mappings.append(_s3_mapping(source_id, table_ch))
    return {
        "Name": site.key,
        "ProtocolAdapter": "OPCUA",
        "AdapterOpcuaServer": site.key,
        # Per-site constants the S3 Tables mappings read back by path.
        # SFC forwards only Good OPC UA values, so quality is constant.
        "Metadata": {"site": site.key, "area": site.area, "quality": QUALITY_GOOD},
        "Channels": source_channels,
    }


def _schedule(source_id: str, targets: list[str]) -> dict[str, Any]:
    """The 5 s poll schedule for one site's source."""
    return {
        "Name": f"{source_id}-schedule",
        "Interval": 5000,
        "Active": True,
        "Sources": {source_id: ["*"]},
        "Targets": targets,
    }


# ---------------------------------------------------------------------------
# One-time history replay (the replay ADR). A SECOND SFC source on the SAME
# Indianapolis server reads the story mill's HISTORY nodes
# ("<HISTORY_ROOT>/<leaf alias>", a root folder outside the site's branch, so
# a reader of the plant's branch never sees them), on a faster schedule whose
# TimestampLevel is "Channel" so each point lands at its historical
# SourceTimestamp. SFC stays the only SiteWise writer; the live schedules keep
# TimestampLevel unset (None), so live stamping is unchanged. Off by default —
# the whole block is omitted, a byte-identical config.
#
# The history schedule writes through its OWN two targets, never the live
# ones: SFC's SiteWise writer walks every configured asset property and its
# S3 Tables writer every table mapping for each value it receives, so history
# entries in the live targets would add that work to every live 5 s read on
# a task that already runs near its limits. The history S3 target writes its
# own table (``readings_replay``): the narrow schema plus ``ingest_time``, SFC's
# clock when it handed the row on, so a replayed point written twice (a
# restart, or a Reset and a new placement) resolves to one reading (the
# newest ``ingest_time`` per key, the same value SiteWise kept). A second
# writer on the live table would also contend for the same Iceberg commits.
# ---------------------------------------------------------------------------

STORY_SITE_KEY = "indianapolis"
STORY_MILL_NODE = "Line3CNC1"              # the CMX-441 mill's process node on Line3
HISTORY_SOURCE_ID = "indianapolis-history"
HISTORY_ROOT = "StoryHistoryReplay"        # server.mjs HISTORY_ROOT: "<root>/<leaf alias>"
HISTORY_SCHEDULE_INTERVAL_MS = 1000        # faster than the 5 s live schedule, paced replay
HISTORY_TIMESTAMP_LEVEL = "Channel"        # each point at its OPC UA SourceTimestamp (an Instant)
SITEWISE_HISTORY_TARGET = "sitewise-history-target"
S3TABLES_HISTORY_TARGET = "s3tables-history-target"
# SFC's SiteWise target sends its buffer every BatchSize (10) reads, and on a
# timer only when Interval is set: the default is no timer (SFC v1.11.0
# AwsSiteWiseTargetConfiguration.kt:67-74, AwsSiteWiseTargetWriter.kt:204-209).
# The live targets read every 5 s for ever, so their buffers always fill; the
# replay stops, and its last partial batch (up to 9 points, the newest ones
# before the placement) would never be sent. Seen on a local run of SFC
# v1.11.0 against the real server: the last 8 points never arrived. The
# history target therefore sends at least every 5 s (SFC requires >= 10 ms).
HISTORY_SITEWISE_INTERVAL_MS = 5000
REPLAY_TABLE_NAME = "readings_replay"      # stacks/sfc_s3tables_stack.DEFAULT_REPLAY_TABLE_NAME
INGEST_TIME_COLUMN = "ingest_time"
S3_REPLAY_TABLE_SCHEMA: list[dict[str, Any]] = [
    *S3_TABLE_SCHEMA, {"Name": INGEST_TIME_COLUMN, "Type": "timestamptz", "Optional": False},
]
#: The readings of the replay table, one per key: the newest copy (by
#: ``ingest_time``) of each (site, area, line, asset, channel, event_time). A
#: replay that runs again (SFC restarted mid-replay) writes identical copies; a
#: Reset and a new placement write the new era's values later, which are the
#: values SiteWise keeps (it overwrites an equal timestamp). ``{table}`` is the
#: replay table, in Athena ``"s3tablescatalog/<bucket>"."<namespace>"."readings_replay"``.
REPLAY_READINGS_SQL = (
    "SELECT event_time, site, area, line, asset, channel, value, quality FROM ("
    "SELECT *, row_number() OVER (PARTITION BY site, area, line, asset, channel, event_time "
    "ORDER BY ingest_time DESC) AS copy FROM {table}) AS replayed WHERE copy = 1"
)


def _history_alias(alias: str) -> str:
    """The history-node alias for a story-mill leaf alias, under the history
    root folder (".../Line3CNC1/Line3CNC1_X" -> "<root>/.../Line3CNC1/Line3CNC1_X"),
    matching server.mjs."""
    return f"{HISTORY_ROOT}/{alias}"


def _story_mill_channels(channels: list[Channel]) -> list[Channel]:
    """The Indianapolis story-mill channels (node :data:`STORY_MILL_NODE`)."""
    return [ch for ch in channels if ch.site == STORY_SITE_KEY and ch.node == STORY_MILL_NODE]


def _history_sitewise_property(source_id: str, ch: Channel) -> dict[str, Any]:
    """A SiteWise history twin: the SAME asset external id and PropertyName as
    the live feed, a DataPath into the history source, and an explicit
    TimestampPath to the channel's historical time (the Channel-level stamp)."""
    data_path = _data_path(source_id, ch.channel_id)
    return {
        "PropertyName": ch.prop,
        "DataType": MODEL_PROPERTY_TYPES[ch.model][ch.prop].lower(),
        "DataPath": data_path,
        "TimestampPath": data_path.rsplit(".value", 1)[0] + ".timestamp",
        "WarnIfNotPresent": False,
    }


def _history_s3_mapping(source_id: str, table_ch: str) -> dict[str, Any]:
    """An S3 Tables mapping for a history channel: like the live mapping but
    event_time reads the channel's historical timestamp, not SFC's clock, and
    ``ingest_time`` is SFC's clock (which copy of a point is the newest)."""
    mapping = _s3_mapping(source_id, table_ch)
    mapping["event_time"] = {"ValueQuery": f"@.sources.{source_id}.values.{table_ch}.timestamp"}
    mapping[INGEST_TIME_COLUMN] = {"ValueQuery": "@.timestamp"}
    return mapping


def _history_sections(channels: list[Channel], namespace_index: int) -> dict[str, Any]:
    """The second source for the story mill's history nodes and the SiteWise
    properties / S3 mappings its own targets write (the same asset external
    id and PropertyName as the live feed, at the historical time)."""
    source_id = HISTORY_SOURCE_ID
    source_channels: dict[str, Any] = {}
    assets: dict[str, dict[str, Any]] = {}
    mappings: list[dict[str, Any]] = []
    for ch in _story_mill_channels(channels):
        hist = replace(ch, alias=_history_alias(ch.alias))
        source_channels[hist.channel_id] = _source_channel(hist, namespace_index)
        assets.setdefault(
            hist.asset_external_id,
            {"AssetExternalId": hist.asset_external_id, "Properties": []},
        )["Properties"].append(_history_sitewise_property(source_id, hist))
        table_ch = _table_channel(source_channels, hist)
        if table_ch is not None:
            mappings.append(_history_s3_mapping(source_id, table_ch))
    source = {
        "Name": source_id,
        "ProtocolAdapter": "OPCUA",
        "AdapterOpcuaServer": STORY_SITE_KEY,
        "Metadata": {"site": STORY_SITE_KEY, "area": "Machining", "quality": QUALITY_GOOD},
        "Channels": source_channels,
    }
    return {"source": source, "assets": list(assets.values()), "mappings": mappings}


def _history_schedule() -> dict[str, Any]:
    """The history source's schedule: fast, Channel-timestamped so each
    replayed point lands at its historical time (the live schedules keep
    None), and written through the history targets only."""
    return {
        "Name": f"{HISTORY_SOURCE_ID}-schedule",
        "Interval": HISTORY_SCHEDULE_INTERVAL_MS,
        "Active": True,
        "TimestampLevel": HISTORY_TIMESTAMP_LEVEL,
        "Sources": {HISTORY_SOURCE_ID: ["*"]},
        "Targets": [SITEWISE_HISTORY_TARGET, S3TABLES_HISTORY_TARGET],
    }


def _history_targets(history: dict[str, Any], region: str, table_bucket: str,
                     namespace: str, replay_table_name: str) -> dict[str, Any]:
    """The history schedule's own SiteWise and S3 Tables targets."""
    return {
        SITEWISE_HISTORY_TARGET: {"TargetType": "AWS-SITEWISE", "Region": region,
                                  "Interval": HISTORY_SITEWISE_INTERVAL_MS,
                                  "Assets": history["assets"]},
        S3TABLES_HISTORY_TARGET: _s3tables_target(
            region, table_bucket, namespace, replay_table_name, history["mappings"],
            schema=S3_REPLAY_TABLE_SCHEMA),
    }


def _s3tables_target(
    region: str, table_bucket: str, namespace: str, table_name: str,
    s3_mappings: list[dict[str, Any]], schema: list[dict[str, Any]] = S3_TABLE_SCHEMA,
) -> dict[str, Any]:
    """The AWS-S3-TABLES target writing the narrow table (decision 5)."""
    return {
        "TargetType": "AWS-S3-TABLES",
        "Region": region,
        "TableBucket": table_bucket,
        "Namespace": namespace,
        "AutoCreate": False,
        # A missing value is EXPECTED (other sites' mappings on every
        # poll), so do not log a warning per skipped mapping.
        "WarnIfValueMissing": False,
        # Each append is an Iceberg commit costing ~10-17 s whatever its size.
        # At SFC's defaults (about 100 records or 10 s per write) two commits
        # were in flight at all times and the writer kept up with ~15 rows/s
        # against up to ~145 rows/s offered (live 2026-10-03), while the task
        # sat at 95-100% memory before SiteWise writes stalled. A minute of
        # readings per commit spreads that fixed cost over far more rows.
        "BufferCount": S3_TABLES_BUFFER_COUNT,
        "Interval": S3_TABLES_INTERVAL_MS,
        "Tables": [
            {
                "TableName": table_name,
                "Schema": schema,
                "Partition": S3_TABLE_PARTITION,
                "Mappings": s3_mappings,
            }
        ],
    }


#: AdapterTypes / TargetTypes bind each in-process adapter to its jar directory
#: and factory class. ProtocolAdapters references the "OPCUA" AdapterType, so
#: omitting this block leaves that reference dangling and SFC cannot construct
#: the adapter.
ADAPTER_TYPES: dict[str, Any] = {
    "OPCUA": {
        "JarFiles": ["${MODULES_DIR}/opcua/lib"],
        "FactoryClassName": "com.amazonaws.sfc.opcua.OpcuaAdapter",
    },
}
TARGET_TYPES: dict[str, Any] = {
    "AWS-SITEWISE": {
        "JarFiles": ["${MODULES_DIR}/aws-sitewise-target/lib"],
        "FactoryClassName": (
            "com.amazonaws.sfc.awssitewise.AwsSiteWiseTargetWriter"
        ),
    },
    "AWS-S3-TABLES": {
        "JarFiles": ["${MODULES_DIR}/aws-s3-tables-target/lib"],
        "FactoryClassName": (
            "com.amazonaws.sfc.awss3tables.AwsS3TablesTargetWriter"
        ),
    },
}


def _check_endpoint_keys(endpoints: dict[str, str]) -> None:
    """Refuse an endpoint key that matches no site (see generate_sfc_config)."""
    unknown = sorted(set(endpoints) - {site.key for site in SITES})
    if unknown:
        raise ValueError(
            f"unknown endpoint key(s) {unknown}; expected a subset of "
            f"{[site.key for site in SITES]}"
        )


def _site_sections(
    endpoints: dict[str, str],
    datasets: ModuleType | None,
    namespace_index: int,
    targets: list[str],
    backfill: bool = False,
) -> dict[str, Any]:
    """The per-site config sections for every site that has an endpoint.

    A site absent from ``endpoints`` (or with no channels) is skipped, so a
    partial deployment still yields a valid config for the sites it has. With
    ``backfill`` and the story site present, ``history`` holds a SECOND source
    on the story server and what its own targets write (the replay ADR)."""
    by_site = _channels_by_site(datasets)
    parts: dict[str, Any] = {
        "schedules": [], "sources": {}, "opcua_servers": {},
        # AWS-SITEWISE Assets mapping: one entry per asset external id with an
        # explicit Properties list, so all of an asset's channels group.
        "sitewise_assets": {}, "s3_mappings": [], "history": None,
    }
    for site in SITES:
        endpoint, site_channels = endpoints.get(site.key), by_site.get(site.key)
        if endpoint is None or not site_channels:
            continue
        parts["opcua_servers"][site.key] = _opcua_server(endpoint)
        parts["sources"][site.key] = _site_source(
            site, site_channels, namespace_index,
            parts["sitewise_assets"], parts["s3_mappings"])
        parts["schedules"].append(_schedule(site.key, targets))
    if backfill and by_site.get(STORY_SITE_KEY) and STORY_SITE_KEY in parts["sources"]:
        parts["history"] = _history_sections(by_site[STORY_SITE_KEY], namespace_index)
        parts["sources"][HISTORY_SOURCE_ID] = parts["history"]["source"]
        parts["schedules"].append(_history_schedule())
    return parts


def _target_configs(parts: dict[str, Any], targets: list[str], s3_ids: tuple[str, str, str],
                    table_name: str, replay_table_name: str) -> dict[str, Any]:
    """The live SiteWise and S3 Tables targets, plus the history replay's own
    pair when :func:`_site_sections` built one."""
    region, table_bucket, namespace = s3_ids
    sitewise_target, s3tables_target = targets
    configs = {
        sitewise_target: {"TargetType": "AWS-SITEWISE", "Region": region,
                          "Assets": list(parts["sitewise_assets"].values())},
        s3tables_target: _s3tables_target(
            region, table_bucket, namespace, table_name, parts["s3_mappings"]),
    }
    if parts["history"] is not None:
        configs.update(_history_targets(parts["history"], region, table_bucket, namespace, replay_table_name))
    return configs


def generate_sfc_config(
    endpoints: dict[str, str],
    *,
    region: str,
    table_bucket: str,
    namespace: str,
    table_name: str,
    namespace_index: int = DEFAULT_NAMESPACE_INDEX,
    sitewise_target: str = "sitewise-target",
    s3tables_target: str = "s3tables-target",
    datasets: ModuleType | None = None,
    backfill: bool = False,
    replay_table_name: str = REPLAY_TABLE_NAME,
) -> dict[str, Any]:
    """Build the full SFC configuration dict from the tag map.

    ``endpoints`` maps each :data:`SITES` key to its
    ``opc.tcp://<host>:26543/UA/ManufacturingServer`` endpoint (the stack's
    ``opc_ua_endpoints`` values); a site absent from the map is skipped so a
    partial deployment still produces a valid config for the sites it has. An
    endpoint key that matches no site is a CONFIGURATION ERROR and raises —
    silently skipping it would emit a config that reads nothing at all, which
    is the failure mode the repo's honest-data rule exists to prevent.

    ``region``, ``table_bucket``, ``namespace`` and ``table_name`` identify the
    S3 Tables target and are REQUIRED by SFC's own schema; they come from
    ``SfcS3TablesStack``'s exported attributes (ADR-0082 decision 4), so there
    is deliberately no default that could ship a config pointing nowhere.

    Returns a plain dict ready to serialise with ``json.dumps`` — no file is
    written here (the stack stages it under cdk.out).
    """
    _check_endpoint_keys(endpoints)
    targets = [sitewise_target, s3tables_target]
    parts = _site_sections(endpoints, datasets, namespace_index, targets, backfill=backfill)
    s3_ids = (region, table_bucket, namespace)
    return {
        "AWSVersion": "2022-04-02",
        "Schedules": parts["schedules"],
        "Sources": parts["sources"],
        "Transformations": TRANSFORMATIONS,
        "Targets": _target_configs(parts, targets, s3_ids, table_name, replay_table_name),
        "ProtocolAdapters": {
            "OPCUA": {"AdapterType": "OPCUA", "OpcuaServers": parts["opcua_servers"]},
        },
        "AdapterTypes": ADAPTER_TYPES,
        "TargetTypes": TARGET_TYPES,
    }


# ---------------------------------------------------------------------------
# Placeholders, plus a CLI shim (``python3 -m agents.demo.sfc_config``) for
# inspecting the generated config by hand; deployments never need it, since the
# SFC stack generates the config at synth (ADR-0082 decision 6).
#
# The OPC UA hostnames are NOT known at synth — they are internal NLB DNS
# names that exist only after ADTHighByteOpcuaStack deploys. So the endpoints are
# emitted as SFC ``${name}`` PLACEHOLDERS and resolved from the container
# environment at startup, which is SFC's own documented behaviour (the same
# mechanism ${MODULES_DIR} already relies on) and is what keeps this a
# single-pass deploy with no post-deploy config regeneration step.
# ---------------------------------------------------------------------------

#: Env var per site that stacks/sfc_stack.py must set on the container.
OPCUA_HOST_ENV_PREFIX = "SFC_OPCUA_HOST_"


def opcua_host_env(site_key: str) -> str:
    """Name of the env var carrying one site's OPC UA host."""
    return f"{OPCUA_HOST_ENV_PREFIX}{site_key.upper()}"


def placeholder_endpoints() -> dict[str, str]:
    """Endpoints whose HOST is an SFC placeholder, resolved at container start."""
    return {
        site.key: (
            f"opc.tcp://${{{opcua_host_env(site.key)}}}"
            f":{DEFAULT_OPCUA_PORT}{DEFAULT_OPCUA_PATH}"
        )
        for site in SITES
    }


# ---------------------------------------------------------------------------
# Freshness sentinels, for the SFC watchdog (stacks/_sfc_watchdog.py).
#
# SFC can keep a source connected while its OPC UA subscription delivers
# nothing, and only an SFC restart clears that. The watchdog reads one channel
# per source and restarts SFC when that channel has stored nothing new for a
# few minutes. That only works on a channel whose value changes on EVERY
# server tick: SFC's subscription forwards a value only when it changes, so a
# channel that holds a value (a stopped machine, a counter on an idle line)
# stores nothing by design.
#
# The server plays one dataset row per tick (server.mjs getData and
# advanceDataIndex), so a channel changes on every tick exactly when its
# dataset value differs from each row to the next. The datasets come from the
# tag map's generator functions (generate_datasets.py), so a channel
# qualifies when its generator, driven through a whole loop in every line
# state, never repeats a formatted value. That is a property of the generator
# function, not of one random draw; tests/unit/test_sfc_sentinels.py also
# checks the chosen channels against the generated datasets themselves.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Sentinel:
    """One source's freshness channel: the SiteWise asset (by external id) and
    property SFC writes it to, and the OPC UA alias it is read from."""

    source: str
    asset_external_id: str
    prop: str
    alias: str


#: Line states the probe drives a generator through, and how many rows each
#: stretch lasts: longer than the watchdog's five-minute window (60 rows).
_PROBE_STATES = ("active", "idle", "down")
_PROBE_STRETCH_ROWS = 61


@contextmanager
def _random_state_kept():
    """Leave the process's ``random`` state as it was. Loading the tag map
    seeds it and its generators draw from it."""
    saved = random.getstate()
    try:
        yield
    finally:
        random.setstate(saved)


def changes_every_row(datasets: ModuleType, alias: str, dtype: str, generator: Any) -> bool:
    """Whether ``generator`` gives ``alias`` a different dataset value on every
    row of a loop, wrap included, in every line state, the way
    ``generate_site_csv`` computes and formats it."""
    with _random_state_kept():
        values = []
        try:
            for step in range(datasets.NUM_STEPS):
                state = _PROBE_STATES[(step // _PROBE_STRETCH_ROWS) % len(_PROBE_STATES)]
                value = datasets._compute_tag_value(alias, generator, state, "1", {}, {})
                if value is datasets._SKIP:
                    return False
                values.append(str(datasets._format_csv_value(dtype, value)))
        except RuntimeError:          # a derived tag whose siblings are not driven here
            return False
        return all(values[i] != values[i - 1] for i in range(len(values)))


def _datasets_module(datasets: ModuleType | None, name: str) -> ModuleType:
    if datasets is not None:
        return datasets
    with _random_state_kept():
        return _load_by_path(name, _DATASETS_PATH)


def freshness_sentinels(datasets: ModuleType | None = None) -> list[Sentinel]:
    """One sentinel per live SFC source (:data:`SITES` order): the first DOUBLE
    channel SFC writes to SiteWise whose value changes on every row
    (:func:`changes_every_row`). Raises when a source has none, so the
    watchdog never ships a source it cannot judge."""
    datasets = _datasets_module(datasets, "generate_datasets_for_sentinels")
    with _random_state_kept():
        tags = {alias: tag for site in SITES for _line, line_tags in site.line_tags(datasets)
                for alias, tag in line_tags.items()}
        picked: dict[str, Sentinel] = {}
        for ch in resolve_channels(datasets):
            if ch.site in picked or ch.dtype != "DOUBLE":
                continue
            dtype, generator = tags[ch.alias]
            if changes_every_row(datasets, ch.alias, dtype, generator):
                picked[ch.site] = Sentinel(ch.site, ch.asset_external_id, ch.prop, ch.alias)
    missing = [site.key for site in SITES if site.key not in picked]
    if missing:
        raise ValueError(f"no channel changes on every row for source(s) {missing}")
    return [picked[site.key] for site in SITES]


def dataset_rows(datasets: ModuleType | None = None) -> int:
    """Rows in each site's dataset: the loop the server plays, one row per tick."""
    return int(_datasets_module(datasets, "generate_datasets_for_rows").NUM_STEPS)


def _main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python3 -m agents.demo.sfc_config",
        description="Write the SFC configuration (for inspection; the stack generates its own).",
    )
    parser.add_argument("--out", required=True, help="path to write sfc-config.json")
    parser.add_argument("--region", required=True)
    parser.add_argument("--table-bucket", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--table-name", required=True)
    args = parser.parse_args(argv)

    config = generate_sfc_config(
        placeholder_endpoints(),
        region=args.region,
        table_bucket=args.table_bucket,
        namespace=args.namespace,
        table_name=args.table_name,
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(config, indent=2), encoding="utf-8")
    channels = sum(len(s["Channels"]) for s in config["Sources"].values())
    print(f"wrote {out} ({channels} channels across {len(config['Sources'])} sources)")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(_main())
