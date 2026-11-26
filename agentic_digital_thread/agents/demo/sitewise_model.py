# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""SiteWise model resolution and the asset tree SFC writes into (ADR-0082).

The pure half of the SFC config generator, split out of
:mod:`agents.demo.sfc_config` to keep that module under the repo's file-size
cap. Nothing here reads the tag map or the model script: it is the port of
HighByte's node -> model resolution (decision 3) and the Enterprise/Site/Area/
Line/process tree shape (decision 9). ``sfc_config`` re-exports every public
name, so callers keep importing from there.

Deliberately no repo imports: ``sfc_config`` is itself loaded by path (from
``stacks/sfc_stack.py``), so it loads this sibling by path as well.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

# ---------------------------------------------------------------------------
# Model resolution — an EXACT port of getModel in
# highbyte/pipeline_sitewise.json's reshapePayload stage (ADR-0082 decision 3).
# ---------------------------------------------------------------------------


def resolve_model(suffix: str, attr_keys: list[str], area: str) -> str:
    """Return the SiteWise model name for a process node.

    Faithful reproduction of the JavaScript ``getModel(suffix, attrs, area)``:

    * ``Washer``/``Filler`` gain a ``Pump`` variant when any attribute key
      contains ``Pump``;
    * ``Capper``/``CasePacker``/``WeighCheck``/``Labeller`` are all
      ``BottlingMachine``;
    * ``Palletizer`` is ``FoodPalletizer`` in a Food/Cedar area, else
      ``BottlingMachine``;
    * buffers and conveyors are ``Level``;
    * ``Environment`` splits into ``AutoEnvironment`` (AmbientTemp/CompressedAir)
      vs ``FoodEnvironment`` (RoomTemp/Refrigeration), defaulting Food;
    * anything else is its own suffix.
    """
    has_pump = any("Pump" in k for k in attr_keys)
    if suffix == "Washer":
        return "WasherPump" if has_pump else "Washer"
    if suffix == "Filler":
        return "FillerPump" if has_pump else "Filler"
    if suffix in ("Capper", "CasePacker", "WeighCheck", "Labeller"):
        return "BottlingMachine"
    if suffix == "Palletizer":
        if area and ("Food" in area or "Cedar" in area):
            return "FoodPalletizer"
        return "BottlingMachine"
    if suffix in ("Buffer", "BufferEnd", "BufferStart", "Conveyor"):
        return "Level"
    if suffix == "Environment":
        for k in attr_keys:
            if "AmbientTemp" in k or "CompressedAir" in k:
                return "AutoEnvironment"
            if "RoomTemp" in k or "Refrigeration" in k:
                return "FoodEnvironment"
        return "FoodEnvironment"
    return suffix


def _suffix(process_node: str, line: str) -> str:
    """Node suffix the transform derives: strip the leading line name and any
    trailing digits (``k.replace('^'+lineName,'').replace('[0-9]+$','')``),
    falling back to the whole node when that empties it (the line-level node)."""
    suffix = re.sub(r"[0-9]+$", "", re.sub("^" + re.escape(line), "", process_node))
    return suffix or process_node


def _channel_property(process_node: str, leaf: str) -> str:
    """Property name the transform derives for a leaf variable: strip
    ``'^'+processNode+'_?'`` and a single leading underscore
    (``ck.replace('^'+k+'_?','')``; ``if ck[0]=='_' ck=ck[1:]``)."""
    prop = re.sub("^" + re.escape(process_node) + "_?", "", leaf)
    if not prop:
        prop = leaf
    if prop.startswith("_"):
        prop = prop[1:]
    return prop


# ---------------------------------------------------------------------------
# Channel model — one resolved OPC UA variable bound to a SiteWise property.
# Resolved from the tag map by sfc_config.resolve_channels.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Channel:
    """One writable OPC UA variable resolved to a SiteWise asset property.

    ``name`` is the SFC channel id (unique within a source); ``alias`` is the
    STRING-NodeId value (the full slash path); ``asset`` is the SiteWise asset
    name (the process node); ``model``/``prop`` are the resolved model and the
    property on it SFC writes to.
    """

    site: str
    area: str
    line: str
    asset: str
    model: str
    prop: str
    alias: str
    #: OPC UA data type from the tag map (DOUBLE / INTEGER / BOOLEAN / STRING).
    dtype: str = "DOUBLE"
    #: The process NODE (tag-map folder). Equal to ``asset`` except where the tag
    #: map renames the asset (the CMX-441 story mill); identity keys use it.
    node: str = ""

    @property
    def asset_external_id(self) -> str:
        """SiteWise external id of this channel's asset (see :func:`asset_tree`)."""
        return external_id(self.site, self.line, self.node or self.asset)

    @property
    def channel_id(self) -> str:
        """SFC channel id: the alias with '/' replaced by '_' so it is a legal
        JSON key and a valid JMESPath identifier segment. Aliases are globally
        unique, so the channel id is too."""
        return self.alias.replace("/", "_")


# ---------------------------------------------------------------------------
# The SiteWise asset tree SFC writes into (ADR-0082 decision 9).
#
# SFC's role cannot create assets. HighByte's pipeline creates them by name
# (create:true) only where it runs: it is off in the unattended workshop profile
# (highbyte_sitewise_pipeline=disabled) and absent without the hub, so the SFC
# stack creates them in every SFC deployment. The tree
# is the one HighByte's reshape stage builds — Enterprise -> Site -> Area -> Line
# -> Processes_<Model> children, same names, same models — because every
# SiteWise consumer (onboarding's walk from the ``Site`` assets, the graph's
# site/line/asset paths) already reads that shape. Only the external ids are
# new: asset names repeat across sites (every site has a ``Line1``), so each
# asset carries a unique, path-derived external id and SFC addresses assets by
# it. Assets are created by the SFC stack's custom resource
# (agents/demo/sitewise_tree.py); SFC's own role stays write-only.
# ---------------------------------------------------------------------------

#: Hierarchy each model uses for its children (highbyte/DeploySitewiseModels.py).
_CHILD_HIERARCHY = {"Enterprise": "Sites", "Site": "Areas", "Area": "Lines"}


def external_id(*parts: str) -> str:
    """Path-derived SiteWise external id: ``adt-<part>-<part>...``, lower-case.

    SiteWise allows letters, digits, ``-`` and ``_`` (2-128 chars), so anything
    else becomes ``-``."""
    raw = "-".join(("adt",) + tuple(p.lower() for p in parts))
    return re.sub(r"[^a-z0-9_-]", "-", raw)


@dataclass(frozen=True)
class AssetSpec:
    """One asset of the tree, parents listed before children.

    ``adopt_by_name`` marks an asset the tag map renames (the CMX-441 mill):
    an existing top-level asset of that name and model is ADOPTED into the tree
    rather than duplicated, so the id the feed Lambda and the workshop seed
    already use is kept.
    """

    external_id: str
    name: str
    model: str
    parent: str | None = None
    hierarchy: str | None = None
    adopt_by_name: bool = False


def asset_tree(channels: Iterable[Channel]) -> list[AssetSpec]:
    """The Enterprise/Site/Area/Line/process tree the channels write into."""
    specs: dict[str, AssetSpec] = {}

    def add(spec: AssetSpec) -> None:
        seen = specs.setdefault(spec.external_id, spec)
        if seen != spec:
            raise ValueError(f"conflicting assets for {spec.external_id}: {seen} / {spec}")

    for ch in channels:
        enterprise, city = ch.alias.split("/")[:2]
        ent_id = external_id(enterprise)
        site_id = external_id(ch.site)
        area_id = external_id(ch.site, ch.area)
        line_id = external_id(ch.site, ch.line)
        add(AssetSpec(ent_id, enterprise, "Enterprise"))
        add(AssetSpec(site_id, city, "Site", ent_id, _CHILD_HIERARCHY["Enterprise"]))
        add(AssetSpec(area_id, ch.area, "Area", site_id, _CHILD_HIERARCHY["Site"]))
        add(AssetSpec(line_id, ch.line, "Line", area_id, _CHILD_HIERARCHY["Area"]))
        node = ch.node or ch.asset
        add(AssetSpec(ch.asset_external_id, ch.asset, ch.model, line_id,
                      f"Processes_{ch.model}", adopt_by_name=ch.asset != node))
    return list(specs.values())
