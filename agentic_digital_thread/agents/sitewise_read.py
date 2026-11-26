# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Shared AWS IoT SiteWise read/introspection seam (ADR-0037, the DRY reader).

This is the SINGLE place that talks to the ``iotsitewise`` SDK for reads. It is
reused by three call sites so there is no parallel SiteWise subsystem:

* onboarding discovery (``onboard.onboard_discover`` SiteWise branch) — enumerate
  ALL asset models and their properties so event/metric properties become Fields
  that the map step can concept-tag;
* Mode A (``system_agent._make_sitewise_direct_tool``) — the ``@tool`` wrappers are
  thin shims over these helpers;
* Mode B (``system_agent._mode_b_describe_schema`` / ``_mode_b_run_query``) — schema
  enumeration + a structured property-read verb.

Design constraints (all enforced here):

* **No dataset/model literals.** Everything is driven by runtime discovery
  (``describe_asset_model`` property lists) — this module contains NO model or
  property names.
* **Rules-engine principle.** These helpers read property VALUES *live* at query
  time. Callers may sample a few values transiently for the onboarding map prompt,
  but NOTHING here writes to the graph — only the schema/concept METADATA a caller
  derives is persisted, never bulk time-series values.
* **Cross-account.** Clients are built through :func:`agents.aws_session.boto3_client_for`
  so an OPTIONAL ``role_arn`` (+ ``external_id``/``region``) in ``connection_config``
  triggers a cross-account STS AssumeRole; a blank config uses ambient credentials.

The module is imported ``from . import aws_session`` (not a direct name import) so a
test monkeypatching ``aws_session.boto3_client_for`` is honored by every helper.
"""
from __future__ import annotations

import datetime as _dt
import logging
from collections import deque
from dataclasses import dataclass, field

from . import aws_session

logger = logging.getLogger(__name__)

# SiteWise batch history hard cap (API max data points per request).
_HISTORY_API_MAX = 20000
# Public, shared history safety bounds (helper + tool boundaries).
HISTORY_MAX_ENTRIES = 128
SITEWISE_IDENTIFIER_MAX_CHARS = 256
# Associated-asset (hierarchy) traversal bounds. Downward (CHILD) walks are
# breadth-first and doubly bounded so a deep or wide estate can never run the
# system agent's turn (or memory) off a cliff: never deeper than
# ASSOCIATED_MAX_DEPTH levels, never more than ASSOCIATED_MAX_ASSETS rows total.
# A caller may request LESS; a request for more is clamped to these ceilings
# (reported back as requested_max_depth/requested_max_assets). The default
# depth of 3 reaches equipment under a site (site -> area -> line -> equipment).
ASSOCIATED_MAX_DEPTH = 5
ASSOCIATED_MAX_ASSETS = 500
ASSOCIATED_DEFAULT_DEPTH = 3
# A failed branch read is reported, never dropped silently; at most this many
# skipped branches are itemised (the full count is always reported).
ASSOCIATED_MAX_SKIPPED_REPORTED = 20
ASSOCIATED_DEFAULT_ASSETS = 200
_ASSOCIATED_DIRECTIONS = ("CHILD", "PARENT")
# ListAssociatedAssets maxResults ceiling (API reference: 1-250).
_ASSOCIATED_API_PAGE_MAX = 250
# Opaque SERVICE-minted pagination tokens are NOT identifiers and must not
# share the identifier cap: a real BatchGetAssetPropertyValueHistory nextToken
# measures 1202 chars against pilot-env data, so the 256 cap classified EVERY
# continuation as ``invalid_token`` and silently truncated every multi-page
# history read to page 1. The model then narrated around the missing data —
# invisible in report-only, and correctly refused once annotate audited
# grounding (ADR-0047). Still bounded (the token is echoed into ``seen_tokens``),
# just bounded at a size the service actually produces.
SITEWISE_PAGINATION_TOKEN_MAX_CHARS = 8192
HISTORY_MAX_HOURS = 24 * 366
HISTORY_MAX_REQUEST_JSON_CHARS = 262_144
HISTORY_MAX_RESULTS = _HISTORY_API_MAX
HISTORY_MAX_PAGES_PER_CHUNK = 8
HISTORY_MAX_TOTAL_CALLS = 24
# Coverage budgets (ADR-0065, 2026-09-18). The service page size is ALWAYS the
# API maximum: a 72 h / 4-property / 5-min read (3456 raw points) came back
# ``partial`` at ``page_limit`` because the chunk budget was 8 pages × the
# MODEL-chosen ``max_results`` (default 100) = 800 points, so coverage depended
# on how the model happened to shape the call and the tail — where the anomaly
# lived — was silently dropped. ``max_results`` is still validated for
# compatibility but never shrinks a page. The raw-point/byte budgets are
# PER CHUNK (≤16 entries) so an oversized first chunk cannot starve later ones;
# the accumulator is bounded regardless of raw count, so these bound parse
# work, not memory. Estimated bytes: 64 per numeric point, so the two caps
# below describe the same ceiling.
HISTORY_FETCH_PAGE_SIZE = _HISTORY_API_MAX
HISTORY_MAX_RAW_POINTS_PER_CHUNK = 131_072
HISTORY_MAX_RESPONSE_BYTES = HISTORY_MAX_RAW_POINTS_PER_CHUNK * 64
HISTORY_DOWNSAMPLE_POINTS = 24
# Stride-decimated accumulator: every ``stride``-th raw point is kept and the
# stride doubles when the buffer overflows, so the retained points stay
# uniformly spaced over the WHOLE window (the previous ``[::2][:24]`` + append-
# every-point scheme clustered 23 of 24 points at the tail).
_HISTORY_ACCUMULATOR_POINTS = HISTORY_DOWNSAMPLE_POINTS * 8
_HISTORY_VALUE_CHARS = 1024
# Backward-compatible private aliases used by adjacent helpers/tests.
_HISTORY_DOWNSAMPLE = HISTORY_DOWNSAMPLE_POINTS
# BatchGetAssetPropertyValue hard cap (API max entries per request).
_BATCH_VALUE_PAGE = 16
# Safety bound on nextToken continuation pages per batch request (a latest-value
# batch of ≤16 entries should never need more; guards against a pathological loop).
_BATCH_VALUE_MAX_PAGES = 8


def _client(connection_config: dict | None, region: str | None):
    """Build an ``iotsitewise`` client via the shared cross-account helper.

    Referencing ``aws_session.boto3_client_for`` through the module (not a bound
    import) keeps test monkeypatching effective.
    """
    return aws_session.boto3_client_for(
        "iotsitewise", connection_config or {}, default_region=region
    )


# ---------------------------------------------------------------------------
# Model / asset enumeration
# ---------------------------------------------------------------------------

def list_asset_models(connection_config: dict | None, region: str | None = None) -> list[dict]:
    """Return all asset models as ``[{id, name, description, status}]`` (paginated)."""
    client = _client(connection_config, region)
    models: list[dict] = []
    paginator = client.get_paginator("list_asset_models")
    for page in paginator.paginate():
        for m in page.get("assetModelSummaries", []):
            models.append({
                "id": m.get("id", ""),
                "name": m.get("name", ""),
                "description": m.get("description", ""),
                "status": m.get("status", {}).get("state", ""),
            })
    return models


def describe_asset_model(
    connection_config: dict | None, region: str | None, model_id: str
) -> dict:
    """Return one model with its property list.

    ``{id, name, description, properties: [{id, name, dataType, unit}]}`` — read
    from ``assetModelProperties`` PLUS any ``assetModelCompositeModels``
    properties (composite/path-based properties live there, not in the top-level
    list; without them a model can look property-less). De-duplicated by id.
    """
    client = _client(connection_config, region)
    resp = client.describe_asset_model(assetModelId=model_id)
    props: list[dict] = []
    seen: set[str] = set()

    def _add(p: dict) -> None:
        pid = p.get("id", "")
        if pid and pid in seen:
            return
        seen.add(pid)
        props.append({
            "id": pid,
            "name": p.get("name", ""),
            "dataType": p.get("dataType", ""),
            "unit": p.get("unit", ""),
        })

    for p in resp.get("assetModelProperties", []):
        _add(p)
    for cm in resp.get("assetModelCompositeModels", []):
        for p in cm.get("properties", []):
            _add(p)
    return {
        "id": resp.get("assetModelId", model_id),
        "name": resp.get("assetModelName", ""),
        "description": resp.get("assetModelDescription", ""),
        "properties": props,
    }


def list_asset_model_properties(
    connection_config: dict | None, region: str | None, model_id: str
) -> list[dict]:
    """Enumerate ALL properties of one model via ``ListAssetModelProperties``
    (``filter='ALL'``) — the RICHER enumeration that includes composite/path-based
    properties (data streams) that ``describe_asset_model`` can omit entirely.

    Returns the same ``[{id, name, dataType, unit}]`` shape as
    :func:`describe_asset_model` so callers are interchangeable (paginated).
    """
    client = _client(connection_config, region)
    props: list[dict] = []
    paginator = client.get_paginator("list_asset_model_properties")
    for page in paginator.paginate(assetModelId=model_id, filter="ALL"):
        for p in page.get("assetModelPropertySummaries", []):
            props.append({
                "id": p.get("id", ""),
                "name": p.get("name", ""),
                "dataType": p.get("dataType", ""),
                "unit": p.get("unit", ""),
            })
    return props


def sample_asset_names_by_model(
    connection_config: dict | None, region: str | None = None,
    model_ids: "list[str] | None" = None, *, per_model: int = 4,
) -> dict:
    """Bounded sample of asset NAMES per model, for prompt seeding only.

    Deliberately NOT folded into :func:`list_asset_models_with_properties` (the
    ADR-0037 parity seam), because onboarding discovery runs through that seam
    and must not pay extra API calls for a prompt-shaping nicety.

    Why it exists: the seeded inventory told the agent which MODELS and
    properties exist but never which ASSETS instantiate them. For estates where
    event-shaped models (machine state, alarms, stoppage reasons) are
    instantiated by CHILD component assets rather than the machine asset, the
    agent guessed the machine asset, got a property-not-found failure, and often
    spent its bounded turn budget on that dead end. A few asset names per model
    make the instantiation pattern legible without a discovery crawl.

    Best-effort and bounded: ``per_model`` names per model, names only (never
    ids or values), one ``list_assets`` per model, and ANY failure yields no
    entry for that model — an absent sample is honest, a fabricated one is not.
    """
    samples: dict = {}
    for mid in model_ids or []:
        if not mid:
            continue
        try:
            assets = list_assets(connection_config, region, asset_model_id=mid)
        except Exception as e:  # noqa: BLE001 - seeding must never sink the build
            logger.warning("asset-name sample skipped for model %s: %s", mid, e)
            continue
        names = [
            str(a.get("name") or "").strip()[:SITEWISE_IDENTIFIER_MAX_CHARS]
            for a in assets if isinstance(a, dict) and str(a.get("name") or "").strip()
        ]
        if names:
            samples[mid] = {"names": names[:max(per_model, 0)], "total": len(names)}
    return samples


def list_asset_models_with_properties(
    connection_config: dict | None,
    region: str | None = None,
    max_models: int | None = None,
) -> list[dict]:
    """Return every asset model enriched with its property list.

    THE parity seam (ADR-0037): onboarding discovery AND the query-time read
    paths (section renderer, scalar/grouped metric resolvers, Mode A/B) all
    enumerate through this ONE function, so the property set the graph was
    onboarded from is by construction the property set reads match against.

    One ``describe_asset_model`` per model (top-level + composite properties).
    LIVE-CONFIRMED gap: some models expose ZERO describe-properties — their
    values live as path-based/data-stream properties — so a model that comes
    back empty is re-enumerated via ``ListAssetModelProperties(filter='ALL')``
    (the richer enumeration) before being returned. Generic — driven entirely by
    runtime discovery; ``max_models`` optionally caps breadth (None = all). A
    per-model enumeration failure is logged and that model is returned with an
    empty property list rather than failing the whole enumeration.
    """
    models = list_asset_models(connection_config, region)
    if max_models is not None:
        models = models[:max_models]
    enriched: list[dict] = []
    for m in models:
        try:
            detail = describe_asset_model(connection_config, region, m["id"])
            props = detail.get("properties", [])
        except Exception as e:  # noqa: BLE001 - one bad model must not sink discovery
            logger.warning("describe_asset_model failed for %s: %s", m.get("id"), e)
            props = []
        if not props:
            try:
                props = list_asset_model_properties(connection_config, region, m["id"])
            except Exception as e:  # noqa: BLE001 - richer enumeration is best-effort
                logger.warning(
                    "list_asset_model_properties failed for %s: %s", m.get("id"), e)
                props = []
        enriched.append({
            "id": m["id"],
            "name": m["name"],
            "description": m.get("description", ""),
            "status": m.get("status", ""),
            "properties": props,
        })
    return enriched


def list_assets(
    connection_config: dict | None, region: str | None = None, asset_model_id: str = ""
) -> list[dict]:
    """Return asset summaries.

    With NO ``asset_model_id`` → the TOP-LEVEL/root assets (``filter='TOP_LEVEL'`` —
    an unfiltered ``list_assets`` is rejected by the API). With one → all assets of
    that model. Returns ``[{id, name, assetModelId, status, hierarchies}]``.
    """
    client = _client(connection_config, region)
    kwargs = {"assetModelId": asset_model_id} if asset_model_id else {"filter": "TOP_LEVEL"}
    paginator = client.get_paginator("list_assets")
    assets: list[dict] = []
    for page in paginator.paginate(**kwargs):
        for a in page.get("assetSummaries", []):
            assets.append({
                "id": a.get("id", ""),
                "name": a.get("name", ""),
                "assetModelId": a.get("assetModelId", ""),
                "status": a.get("status", {}).get("state", ""),
                "hierarchies": [h.get("name", "") for h in a.get("hierarchies", [])],
            })
    return assets


def describe_asset(
    connection_config: dict | None, region: str | None, asset_id: str
) -> dict:
    """Return full asset detail: id/name/assetModelId/properties/hierarchies.

    Properties are ``[{id, name, dataType, unit}]`` from ``assetProperties``.
    """
    client = _client(connection_config, region)
    resp = client.describe_asset(assetId=asset_id)
    return {
        "id": resp.get("assetId", asset_id),
        "name": resp.get("assetName", ""),
        "assetModelId": resp.get("assetModelId", ""),
        "properties": [{
            "id": p.get("id", ""),
            "name": p.get("name", ""),
            "dataType": p.get("dataType", ""),
            "unit": p.get("unit", ""),
        } for p in resp.get("assetProperties", [])],
        "hierarchies": [
            {"id": h.get("id", ""), "name": h.get("name", "")}
            for h in resp.get("assetHierarchies", [])
        ],
    }


# ---------------------------------------------------------------------------
# Hierarchy traversal (ListAssociatedAssets) — bounded, cycle-guarded
# ---------------------------------------------------------------------------

def _associated_summaries(client, asset_id: str, direction: str,
                          hierarchy_id: str = "", limit: int = 0) -> list[dict]:
    """One paginated ``ListAssociatedAssets`` call. Without a ``hierarchyId`` the
    API returns every immediate asset in ``direction`` across all hierarchies, so
    a walk makes one call per expanded asset; PARENT never takes one. A ``limit``
    stops the read (requests AND pages) after that many summaries. Each summary
    keeps ``has_children`` (the asset defines child hierarchies)."""
    kwargs: dict = {"assetId": asset_id, "traversalDirection": direction}
    if hierarchy_id:
        kwargs["hierarchyId"] = hierarchy_id
    if limit:
        kwargs["PaginationConfig"] = {"MaxItems": limit,
                                      "PageSize": min(limit, _ASSOCIATED_API_PAGE_MAX)}
    paginator = client.get_paginator("list_associated_assets")
    out: list[dict] = []
    for page in paginator.paginate(**kwargs):
        for a in page.get("assetSummaries", []):
            out.append({
                "id": a.get("id", ""),
                "name": a.get("name", ""),
                "assetModelId": a.get("assetModelId", ""),
                "has_children": bool(a.get("hierarchies")),
            })
            if limit and len(out) >= limit:
                return out
    return out


def _clamp(value, default: int, maximum: int) -> int:
    """Clamp a user-supplied bound into ``[1, maximum]`` (non-int ⇒ default)."""
    if isinstance(value, bool) or not isinstance(value, int):
        value = default
    return max(1, min(value, maximum))


def _associated_row(summary: dict, parent_id: str, depth: int, path: list[str]) -> dict:
    return {"id": summary["id"], "name": summary["name"],
            "assetModelId": summary["assetModelId"], "parentId": parent_id,
            "depth": depth, "path": path}


def _error_category(exc: Exception) -> str:
    """Safe failure category: the service error code, else the exception class.
    Never the message text, which can carry account or resource detail."""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = (response.get("Error") or {}).get("Code")
        if isinstance(code, str) and code:
            return code[:64]
    return type(exc).__name__[:64]


def _record_skip(result: dict, asset_id: str, exc: Exception) -> None:
    """A branch whose children could not be read: counted, itemised up to
    ``ASSOCIATED_MAX_SKIPPED_REPORTED``, and later marks the result partial."""
    category = _error_category(exc)
    logger.warning("list_associated_assets child read failed for %s: %s", asset_id, category)
    result["skipped_count"] += 1
    if len(result["skipped"]) < ASSOCIATED_MAX_SKIPPED_REPORTED:
        result["skipped"].append({"assetId": asset_id, "operation": "ListAssociatedAssets",
                                  "category": category})


def _list_parent(client, asset_id: str, result: dict) -> dict:
    """PARENT direction: the asset's parent (one level, no hierarchyId)."""
    try:
        parents = _associated_summaries(client, asset_id, "PARENT")
    except Exception as exc:  # noqa: BLE001 - surface the actionable id guidance
        return {"error": describe_asset_error_message(asset_id, exc)}
    for p in parents:
        result["assets"].append(_associated_row(p, asset_id, 1, [p["name"]]))
    return result


def _resolve_hierarchy(client, asset_id: str, hierarchy_id: str) -> tuple[str, dict | None]:
    """Map a hierarchy id or name on ``asset_id`` to its id, with one
    DescribeAsset on the walk's own client. Returns ``(id, error_or_None)``."""
    try:
        resp = client.describe_asset(assetId=asset_id)
    except Exception as exc:  # noqa: BLE001
        return "", {"error": describe_asset_error_message(asset_id, exc)}
    hierarchies = resp.get("assetHierarchies", [])
    for h in hierarchies:
        if hierarchy_id in (h.get("id"), h.get("name")):
            return h.get("id", ""), None
    names = ", ".join(h.get("name", "") for h in hierarchies) or "none"
    return "", _invalid_history_request(
        f"hierarchy_id '{hierarchy_id[:64]}' is not a hierarchy of this asset "
        f"(its hierarchies: {names})")


def _record_children(children: list[dict], parent_id: str, parent_path: list[str],
                     depth: int, walk: dict) -> bool:
    """Add one parent's children as rows and queue the ones to expand. Returns
    True when the asset cap is hit with a child still left over."""
    result, queue = walk["result"], walk["queue"]
    for child in children:
        if child["id"] in walk["visited"]:
            continue
        if len(result["assets"]) >= walk["asset_cap"]:
            return True  # a child beyond the cap: the list is cut here
        walk["visited"].add(child["id"])
        path = parent_path + [child["name"]]
        result["assets"].append(_associated_row(child, parent_id, depth, path))
        if not child["has_children"]:
            continue  # a leaf: nothing to list, no API call
        if depth < walk["depth_cap"]:
            queue.append((child["id"], path, depth + 1))
        else:
            result["below_depth_limit"] += 1
    return False


def _walk(client, walk: dict) -> bool:
    """Breadth-first: read each queued parent's children only while rows are
    still allowed, asking for no more than the remaining allowance plus one (to
    tell whether more exist). Returns True when the asset cap cut the list."""
    result, queue = walk["result"], walk["queue"]
    while queue:
        remaining = walk["asset_cap"] - len(result["assets"])
        if remaining <= 0:
            return True  # assets still to expand but no rows left for them
        asset_id, path, depth = queue.popleft()
        try:
            children = _associated_summaries(client, asset_id, "CHILD",
                                             limit=remaining + 1)
        except Exception as exc:  # noqa: BLE001 - reported as partial in _finalize
            _record_skip(result, asset_id, exc)
            continue
        if _record_children(children, asset_id, path, depth, walk):
            return True
    return False


def _finalize(result: dict, asset_cap: int, depth_cap: int, capped: bool) -> dict:
    """Set the partial markers the system agent's TRUNCATED RESULTS rule reads
    (``truncated``, ``status``, ``partial_note``) and the depth-limit note."""
    reasons, notes = [], []
    if capped:
        reasons.append("max_assets")
        notes.append(f"The list stopped at {asset_cap} assets, so any assets after those "
                     "are missing. List from a lower asset (an area or line) for the rest.")
    count = result["skipped_count"]
    if count:
        reasons.append("read_failed")
        ids = ", ".join(s["assetId"] for s in result["skipped"])
        extra = count - len(result["skipped"])
        more = f" and {extra} more" if extra > 0 else ""
        notes.append(f"The children of {count} asset(s) could not be read ({ids}{more}), "
                     "so the assets under them are missing from this list.")
    if reasons:
        result.update(truncated=True, status="partial", reason=",".join(reasons),
                      partial_note=" ".join(notes))
    if result["below_depth_limit"]:
        result["note"] = (
            f"{result['below_depth_limit']} listed asset(s) at depth {depth_cap} can have "
            "child assets that were not listed; call again from one of them, or with a "
            f"larger max_depth (up to {ASSOCIATED_MAX_DEPTH}), to list those.")
    return result


def list_associated_assets(
    connection_config: dict | None,
    region: str | None,
    asset_id: str,
    direction: str = "CHILD",
    hierarchy_id: str = "",
    max_depth: int = ASSOCIATED_DEFAULT_DEPTH,
    max_assets: int = ASSOCIATED_DEFAULT_ASSETS,
) -> dict:
    """Walk the asset hierarchy from ``asset_id`` and return the associated assets.

    CHILD (default) walks breadth-first down every hierarchy, one
    ``ListAssociatedAssets`` per expanded asset (leaves are never listed), to
    ``max_depth`` levels (default 3, <= ``ASSOCIATED_MAX_DEPTH``) and at most
    ``max_assets`` rows (<= ``ASSOCIATED_MAX_ASSETS``), cycle-guarded;
    ``hierarchy_id`` (id or name) restricts the FIRST level. PARENT returns the
    parent. Rows are ``{id, name, assetModelId, parentId, depth, path}`` (``path``
    = names from the start asset). The asset cap or a failed branch read marks
    the result partial (``truncated``, ``status``, ``reason``, ``partial_note``,
    ``skipped``); ``below_depth_limit``/``note`` count listed assets at the depth
    limit that can have children. A bad id returns the
    :func:`describe_asset_error_message` family; bad arguments return
    ``{error, error_type: 'invalid_request'}``.
    """
    if not isinstance(asset_id, str) or not asset_id.strip():
        return _invalid_history_request("asset_id must be a non-empty, non-whitespace string")
    if len(asset_id) > SITEWISE_IDENTIFIER_MAX_CHARS:
        return _invalid_history_request("asset_id is too long", limit=SITEWISE_IDENTIFIER_MAX_CHARS)
    if direction not in _ASSOCIATED_DIRECTIONS:
        return _invalid_history_request(
            f"direction must be one of {', '.join(_ASSOCIATED_DIRECTIONS)}")

    depth_cap = _clamp(max_depth, ASSOCIATED_DEFAULT_DEPTH, ASSOCIATED_MAX_DEPTH)
    asset_cap = _clamp(max_assets, ASSOCIATED_DEFAULT_ASSETS, ASSOCIATED_MAX_ASSETS)
    client = _client(connection_config, region)
    result = {
        "assets": [], "truncated": False, "reason": "",
        "direction": direction, "root": asset_id,
        "requested_max_depth": depth_cap, "requested_max_assets": asset_cap,
        "skipped": [], "skipped_count": 0, "below_depth_limit": 0,
    }

    if direction == "PARENT":
        return _list_parent(client, asset_id, result)
    return _list_children(client, asset_id, hierarchy_id, depth_cap, asset_cap, result)


def _list_children(client, asset_id: str, hierarchy_id: str, depth_cap: int,
                   asset_cap: int, result: dict) -> dict:
    """CHILD direction: breadth-first on ONE client. The root read doubles as
    the id check: a bad root id surfaces the actionable describe-family error."""
    first_hierarchy = ""
    if hierarchy_id:
        first_hierarchy, error = _resolve_hierarchy(client, asset_id, hierarchy_id)
        if error:
            return error
    try:
        top = _associated_summaries(client, asset_id, "CHILD", first_hierarchy,
                                    limit=asset_cap + 1)
    except Exception as exc:  # noqa: BLE001
        return {"error": describe_asset_error_message(asset_id, exc)}
    walk = {"result": result, "queue": deque(), "visited": {asset_id},
            "asset_cap": asset_cap, "depth_cap": depth_cap}
    capped = _record_children(top, asset_id, [], 1, walk) or _walk(client, walk)
    return _finalize(result, asset_cap, depth_cap, capped)


# ---------------------------------------------------------------------------
# Live property VALUE reads (never persisted — rules-engine constraint)
# ---------------------------------------------------------------------------

def _parse_property_value(pv: dict) -> dict:
    """Normalize one SiteWise ``propertyValue``/``assetPropertyValue`` payload to
    the seam's ``{value, timestamp, quality}`` shape (shared by the single and
    batch latest-value readers so their outputs are interchangeable).

    ``time`` is the ISO-8601 UTC rendering of the numeric ``timestamp``: the
    model must never convert a raw epoch itself (a rehearsal answer reported
    future-dated points from a mis-converted ``timeInSeconds``)."""
    value = pv.get("value", {})
    timestamp = pv.get("timestamp", {}).get("timeInSeconds")
    return {
        "value": next(iter(value.values()), None) if value else None,
        "timestamp": timestamp,
        "time": _iso_from_seconds(timestamp),
        "quality": pv.get("quality", ""),
    }


def get_property_value(
    connection_config: dict | None, region: str | None, asset_id: str, property_id: str
) -> dict:
    """Return the latest value for one asset property: ``{value, timestamp, quality}``.

    A read failure returns ``{"error": ...}`` (callers surface it) rather than raising.
    """
    client = _client(connection_config, region)
    try:
        resp = client.get_asset_property_value(assetId=asset_id, propertyId=property_id)
        return _parse_property_value(resp.get("propertyValue", {}))
    except Exception as e:  # noqa: BLE001 - surface as structured error, never crash caller
        return {"error": str(e)}


def batch_get_property_values(
    connection_config: dict | None,
    region: str | None,
    entries: list[dict],
) -> dict:
    """Batch latest-value read over many ``(asset, property)`` pairs — one
    ``BatchGetAssetPropertyValue`` call per 16 entries instead of N sequential
    ``GetAssetPropertyValue`` HTTPS round-trips (a per-asset sweep over ~39
    machines freezes the single-worker server).

    ``entries`` = ``[{assetId, propertyId}, ...]``. Returns results keyed by
    ``"<assetId>_<propertyId>"`` (the same key scheme as
    :func:`get_property_history`): each value is the
    ``{value, timestamp, quality}`` shape of :func:`get_property_value`, or
    ``{"error": ...}`` for that entry.

    Per-entry error TOLERANCE: an ``errorEntries``/``skippedEntries`` member —
    or a whole failed page — surfaces as a per-key ``{"error": ...}`` and never
    sinks the rest of the batch. Uses short index-based ``entryId`` values
    (SiteWise enforces a 64-char max) mapped back to the composite key.
    """
    client = _client(connection_config, region)
    results: dict = {}
    for start in range(0, len(entries), _BATCH_VALUE_PAGE):
        page = entries[start:start + _BATCH_VALUE_PAGE]
        idx_to_key = {
            str(start + i): f"{e['assetId']}_{e['propertyId']}"
            for i, e in enumerate(page)
        }
        batch_entries = [{
            "entryId": str(start + i),
            "assetId": e["assetId"],
            "propertyId": e["propertyId"],
        } for i, e in enumerate(page)]
        try:
            kwargs: dict = {"entries": batch_entries}
            for _ in range(_BATCH_VALUE_MAX_PAGES):
                resp = client.batch_get_asset_property_value(**kwargs)
                for r in resp.get("successEntries", []):
                    key = idx_to_key.get(str(r.get("entryId")))
                    if key is not None:
                        results[key] = _parse_property_value(
                            r.get("assetPropertyValue") or {})
                for r in resp.get("errorEntries", []):
                    key = idx_to_key.get(str(r.get("entryId")))
                    if key is not None:
                        results[key] = {"error": str(
                            r.get("errorMessage") or r.get("errorCode") or "error")}
                for r in resp.get("skippedEntries", []):
                    key = idx_to_key.get(str(r.get("entryId")))
                    if key is not None and key not in results:
                        results[key] = {"error": str(
                            r.get("completionStatus") or "SKIPPED")}
                token = resp.get("nextToken")
                if not token:
                    break
                kwargs = {"entries": batch_entries, "nextToken": token}
        except Exception as e:  # noqa: BLE001 - page-level failure → per-key errors, never raise
            for key in idx_to_key.values():
                results.setdefault(key, {"error": str(e)})
    return results


def _invalid_history_request(message: str, *, limit: int | None = None) -> dict:
    out = {"error": message, "error_type": "invalid_request"}
    if limit is not None:
        out["limit"] = limit
    return out


def validate_property_identifiers(asset_id, property_id) -> dict | None:
    """Return a typed error unless both SiteWise identifiers are safe strings.

    Values are validated but not normalized so valid identifiers reach the reader
    unchanged.  This helper performs no I/O and owns the shared identifier bound.
    """
    for field_name, value in (("assetId", asset_id), ("propertyId", property_id)):
        if not isinstance(value, str) or not value.strip():
            return _invalid_history_request(
                f"{field_name} must be a non-empty, non-whitespace string"
            )
        if len(value) > SITEWISE_IDENTIFIER_MAX_CHARS:
            return _invalid_history_request(
                f"{field_name} is too long", limit=SITEWISE_IDENTIFIER_MAX_CHARS
            )
    return None


def _validated_history_entries(entries) -> tuple[list[dict] | None, dict | None]:
    if not isinstance(entries, list):
        return None, _invalid_history_request("entries must be a JSON array of {assetId, propertyId} objects")
    if len(entries) > HISTORY_MAX_ENTRIES:
        return None, _invalid_history_request("too many history entries", limit=HISTORY_MAX_ENTRIES)
    deduped, seen = [], set()
    for entry in entries:
        if not isinstance(entry, dict):
            return None, _invalid_history_request("each history entry must be an object")
        asset_id, property_id = entry.get("assetId"), entry.get("propertyId")
        invalid = validate_property_identifiers(asset_id, property_id)
        if invalid:
            return None, invalid
        pair = (asset_id, property_id)
        if pair not in seen:
            seen.add(pair)
            deduped.append({"assetId": asset_id, "propertyId": property_id})
    return deduped, None


def _validated_history_bound(
    value, name: str, default: int, minimum: int, maximum: int
) -> tuple[int | None, dict | None]:
    if value is None:
        return default, None
    if isinstance(value, bool) or not isinstance(value, int):
        return None, _invalid_history_request(f"{name} must be an integer")
    if value < minimum or value > maximum:
        return None, _invalid_history_request(
            f"{name} must be between {minimum} and {maximum}", limit=maximum
        )
    return value, None


def _parse_history_timestamp(value, name: str) -> tuple[_dt.datetime | None, dict | None]:
    """Parse one ISO-8601 timestamp; naive values are treated as UTC."""
    try:
        parsed = _dt.datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None, _invalid_history_request(
            f"{name} must be an ISO-8601 timestamp (e.g. 2026-07-25T00:00:00Z)"
        )
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.UTC)
    return parsed.astimezone(_dt.UTC), None


def _validated_history_window(
    hours_back, start_iso, end_iso
) -> tuple[_dt.datetime | None, _dt.datetime | None, dict | None]:
    """Resolve the query window: ABSOLUTE when ``start_iso`` is given, else the
    trailing ``hours_back`` window anchored at NOW.

    The trailing-only surface could not address a specific past date at all —
    a day-targeted question ("what happened on the 25th?") was unanswerable
    and read as missing data/retention (live 2026-08-04 misdiagnosis on real
    per-day telemetry that a direct API read proved present). ``end_iso``
    defaults to NOW so "since <date>" works with one argument; ``end_iso``
    without ``start_iso`` is rejected rather than guessed. The absolute span
    is capped by the SAME ``HISTORY_MAX_HOURS`` bound as ``hours_back``.
    """
    if not start_iso and not end_iso:
        hours, invalid = _validated_history_bound(
            hours_back, "hours_back", 8, 1, HISTORY_MAX_HOURS
        )
        if invalid:
            return None, None, invalid
        end_time = _dt.datetime.now(_dt.UTC)
        return end_time - _dt.timedelta(hours=hours), end_time, None
    if end_iso and not start_iso:
        return None, None, _invalid_history_request(
            "end_iso requires start_iso (an absolute window needs its start)"
        )
    start_time, invalid = _parse_history_timestamp(start_iso, "start_iso")
    if invalid:
        return None, None, invalid
    if end_iso:
        end_time, invalid = _parse_history_timestamp(end_iso, "end_iso")
        if invalid:
            return None, None, invalid
    else:
        end_time = _dt.datetime.now(_dt.UTC)
    if start_time >= end_time:
        return None, None, _invalid_history_request("start_iso must be before end_iso")
    if (end_time - start_time) > _dt.timedelta(hours=HISTORY_MAX_HOURS):
        return None, None, _invalid_history_request(
            "window too large", limit=HISTORY_MAX_HOURS
        )
    return start_time, end_time, None


def _safe_history_point(point: dict) -> tuple[dict, int]:
    if not isinstance(point, dict):
        return {"value": None, "timestamp": None, "quality": ""}, 32
    raw_value = point.get("value", {})
    value = next(iter(raw_value.values()), None) if isinstance(raw_value, dict) else None
    estimated = 64
    if isinstance(value, str):
        estimated += min(len(value), _HISTORY_VALUE_CHARS)
        value = value[:_HISTORY_VALUE_CHARS]
    elif not isinstance(value, (int, float, bool, type(None))):
        value = None
    timestamp = point.get("timestamp", {})
    timestamp = timestamp.get("timeInSeconds") if isinstance(timestamp, dict) else None
    quality = point.get("quality", "")
    quality = quality[:32] if isinstance(quality, str) else ""
    return {"value": value, "timestamp": timestamp, "quality": quality}, estimated


def _append_bounded_point(state: "_HistoryReadState", key: str, point: dict) -> None:
    """Stride-decimated append: keep every ``stride``-th raw point, double the
    stride when the buffer overflows. Kept points therefore stay uniformly
    spaced over everything read so far; the newest raw point is tracked
    separately so the tail is never lost to decimation."""
    buffer = state.histories.setdefault(key, [])
    count = state.raw_counts.get(key, 0)
    stride = state.strides.get(key, 1)
    if count % stride == 0:
        buffer.append(point)
    state.raw_counts[key] = count + 1
    state.last_points[key] = point
    if len(buffer) > _HISTORY_ACCUMULATOR_POINTS:
        buffer[:] = buffer[::2]
        state.strides[key] = stride * 2


def _downsample_points(points: list[dict]) -> list[dict]:
    """Pick ``HISTORY_DOWNSAMPLE_POINTS`` points spread evenly over the covered
    TIME span (by nearest timestamp), always keeping the first and last."""
    if len(points) <= HISTORY_DOWNSAMPLE_POINTS:
        return points
    last = len(points) - 1
    stamps = [p.get("timestamp") for p in points]
    if not all(isinstance(s, (int, float)) for s in stamps) or stamps[0] == stamps[-1]:
        indexes = [round(i * last / (HISTORY_DOWNSAMPLE_POINTS - 1))
                   for i in range(HISTORY_DOWNSAMPLE_POINTS)]
        return [points[index] for index in indexes]
    span = stamps[-1] - stamps[0]
    chosen: list[int] = []
    cursor = 0
    for i in range(HISTORY_DOWNSAMPLE_POINTS):
        target = stamps[0] + span * i / (HISTORY_DOWNSAMPLE_POINTS - 1)
        while cursor < last and abs(stamps[cursor + 1] - target) <= abs(stamps[cursor] - target):
            cursor += 1
        if not chosen or chosen[-1] != cursor:
            chosen.append(cursor)
    if chosen[-1] != last:
        chosen.append(last)
    return [points[index] for index in chosen]


@dataclass
class _HistoryReadState:
    results: dict[str, object] = field(default_factory=dict)
    histories: dict[str, list[dict]] = field(default_factory=dict)
    partial_reasons: dict[str, str] = field(default_factory=dict)
    raw_counts: dict[str, int] = field(default_factory=dict)
    strides: dict[str, int] = field(default_factory=dict)
    last_points: dict[str, dict] = field(default_factory=dict)
    total_calls: int = 0
    # Per-chunk budgets — reset by ``begin_chunk``.
    raw_points: int = 0
    response_bytes: int = 0
    # Global halt (call budget) — stops every remaining chunk.
    halt_reason: str = ""
    requested_start: str = ""
    requested_end: str = ""

    def begin_chunk(self) -> None:
        self.raw_points = 0
        self.response_bytes = 0

    def covered_points(self, key: str) -> list[dict]:
        """Downsampled points for ``key`` with the newest raw point restored,
        each stamped with an ISO ``time`` AFTER selection (the downsampler
        needs the numeric stamps; see :func:`_with_iso_time`)."""
        points = list(self.histories.get(key, []))
        last = self.last_points.get(key)
        if last is not None and (not points or points[-1] is not last):
            points.append(last)
        return [_with_iso_time(point) for point in _downsample_points(points)]


def _with_iso_time(point: dict) -> dict:
    """Copy of a finalized history point with ``time`` (ISO-8601 UTC) next to
    its numeric ``timestamp`` — the model reads the ISO string instead of
    converting an epoch itself. Adds a bounded ~25 chars to each of the
    ≤ ``HISTORY_DOWNSAMPLE_POINTS`` returned points; the raw-parse budgets
    are computed before this and are unaffected."""
    return {"value": point.get("value"), "timestamp": point.get("timestamp"),
            "time": _iso_from_seconds(point.get("timestamp")),
            "quality": point.get("quality", "")}


def _history_key(entry: dict) -> str:
    return f"{entry['assetId']}_{entry['propertyId']}"


def _history_error(message: object, error_type: str) -> dict:
    return {
        "error": str(message or error_type)[:512],
        "error_type": error_type,
    }


def _iso_from_seconds(value) -> str | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        return _dt.datetime.fromtimestamp(value, tz=_dt.UTC).isoformat()
    except (OverflowError, OSError, ValueError):
        # An out-of-range service stamp must not sink the whole read.
        return None


def _history_partial(reason: str, points: list[dict] | None = None,
                     requested_start: str = "", requested_end: str = "") -> dict:
    """Partial outcome that states WHICH window the returned points cover, so
    a reader (human or model) cannot mistake a truncated head for the whole
    requested span (live 2026-09-18: 44 of 72 h read as the full window)."""
    points = points or []
    covered_start = _iso_from_seconds(points[0].get("timestamp")) if points else None
    covered_end = _iso_from_seconds(points[-1].get("timestamp")) if points else None
    if covered_start and covered_end:
        coverage = f"points cover {covered_start} -> {covered_end}"
    else:
        coverage = "no points returned"
    note = (
        f"Partial history ({reason}): {coverage} of the requested window "
        f"{requested_start or '?'} -> {requested_end or '?'}. Data outside the "
        "covered span was NOT read; narrow the window or split the entries "
        "rather than treating this as the full range."
    )
    return {
        "status": "partial",
        "truncated": True,
        "reason": reason,
        "covered_start": covered_start,
        "covered_end": covered_end,
        "requested_start": requested_start or None,
        "requested_end": requested_end or None,
        "note": note,
        "points": points,
    }


def _mark_chunk_partial(state: _HistoryReadState, keys, reason: str) -> None:
    for key in keys:
        state.partial_reasons.setdefault(key, reason)


def _bounded_response_entries(response: dict, field_name: str) -> list:
    entries = response.get(field_name, []) if isinstance(response, dict) else []
    return entries[:HISTORY_MAX_ENTRIES] if isinstance(entries, list) else []


def _record_success_entry(success: dict, key: str, state: _HistoryReadState) -> str:
    """Fold one success entry's points into ``state``; return a chunk-stop
    reason when a PER-CHUNK budget is exhausted, else ``""``."""
    points = success.get("assetPropertyValueHistory", [])
    if not isinstance(points, list):
        state.partial_reasons.setdefault(key, "invalid_response")
        return ""
    for raw in points:
        if state.raw_points >= HISTORY_MAX_RAW_POINTS_PER_CHUNK:
            return "raw_point_limit"
        safe, size = _safe_history_point(raw)
        if state.response_bytes + size > HISTORY_MAX_RESPONSE_BYTES:
            return "response_size_limit"
        state.raw_points += 1
        state.response_bytes += size
        _append_bounded_point(state, key, safe)
    return ""


def _record_history_page(response: dict, idx_to_key: dict[str, str],
                         state: _HistoryReadState) -> tuple[int, str]:
    """Record one API page; return ``(recognized terminal entries, chunk-stop reason)``."""
    recognized = 0
    stop_reason = ""
    for success in _bounded_response_entries(response, "successEntries"):
        if not isinstance(success, dict):
            continue
        key = idx_to_key.get(str(success.get("entryId")))
        if key is None:
            continue
        recognized += 1
        state.histories.setdefault(key, [])
        stop_reason = _record_success_entry(success, key, state)
        state.results.pop(key, None)
        if stop_reason:
            break
    for field_name, error_type, fallback in (
        ("errorEntries", "sitewise_error", "error"),
        ("skippedEntries", "sitewise_skipped", "SKIPPED"),
    ):
        for failed in _bounded_response_entries(response, field_name):
            if not isinstance(failed, dict):
                continue
            key = idx_to_key.get(str(failed.get("entryId")))
            if key is None or key in state.histories:
                continue
            recognized += 1
            message = (
                failed.get("errorMessage") or failed.get("errorCode")
                or failed.get("errors") or failed.get("completionStatus") or fallback
            )
            state.results[key] = _history_error(message, error_type)
    return recognized, stop_reason


def _next_history_token(response: dict, seen_tokens: set[str], page_number: int,
                        *, continuation: bool, recognized: int) -> tuple[str, str]:
    token = response.get("nextToken") if isinstance(response, dict) else None
    if not token:
        if continuation and recognized == 0:
            return "", "empty_continuation"
        return "", ""
    token_text = token if isinstance(token, str) else ""
    if not token_text or len(token_text) > SITEWISE_PAGINATION_TOKEN_MAX_CHARS:
        return "", "invalid_token"
    if token_text in seen_tokens:
        return "", "repeated_token"
    if page_number >= HISTORY_MAX_PAGES_PER_CHUNK:
        return "", "page_limit"
    seen_tokens.add(token_text)
    return token_text, ""


def _read_history_chunk(client, batch_entries: list[dict], idx_to_key: dict[str, str],
                        state: _HistoryReadState) -> None:
    keys = tuple(idx_to_key.values())
    state.begin_chunk()
    kwargs = {"entries": batch_entries, "maxResults": HISTORY_FETCH_PAGE_SIZE}
    seen_tokens: set[str] = set()
    page_number = 0
    while page_number < HISTORY_MAX_PAGES_PER_CHUNK:
        if state.total_calls >= HISTORY_MAX_TOTAL_CALLS:
            state.halt_reason = "call_limit"
            _mark_chunk_partial(state, keys, state.halt_reason)
            return
        continuation = page_number > 0
        state.total_calls += 1  # attempted calls, including a failing CURRENT chunk
        try:
            response = client.batch_get_asset_property_value_history(**kwargs)
        except Exception as exc:  # noqa: BLE001 - preserve sibling outcomes
            reason = "continuation_error" if continuation else "history_read_error"
            for key in keys:
                if continuation or key in state.histories:
                    state.partial_reasons.setdefault(key, reason)
                else:
                    state.results.setdefault(key, _history_error(exc, reason))
            return
        page_number += 1
        recognized, stop_reason = _record_history_page(response, idx_to_key, state)
        if not stop_reason:
            token, stop_reason = _next_history_token(
                response, seen_tokens, page_number,
                continuation=continuation, recognized=recognized,
            )
        if stop_reason:
            _mark_chunk_partial(state, keys, stop_reason)
            return
        if not token:
            return
        kwargs = {
            "entries": batch_entries,
            "maxResults": HISTORY_FETCH_PAGE_SIZE,
            "nextToken": token,
        }


def _finalize_history_outcomes(normalized: list[dict], state: _HistoryReadState) -> dict:
    """Return exactly one complete/error/partial outcome for every unique key."""
    finalized: dict[str, object] = {}
    for entry in normalized:
        key = _history_key(entry)
        reason = state.partial_reasons.get(key) or state.halt_reason
        if key in state.histories:
            points = state.covered_points(key)
            finalized[key] = points if not reason else _history_partial(
                reason, points, state.requested_start, state.requested_end)
        elif key in state.results:
            finalized[key] = state.results[key]
        elif reason:
            finalized[key] = _history_partial(
                reason, None, state.requested_start, state.requested_end)
        else:
            finalized[key] = _history_error(
                "SiteWise response omitted the requested entry", "missing_entry"
            )
    return finalized


#: Top-level key of a history result naming the series whose returned points
#: are a sample of more values than were returned. It starts with ``_`` so
#: readers that walk the per-series outcomes skip it, as they skip
#: ``_adt_source``. At most one entry per requested series
#: (``HISTORY_MAX_ENTRIES``), each two integers; the note is fixed text apart
#: from the reader's cap (:func:`_history_sampling_note`).
HISTORY_SAMPLING_KEY = "_sampling"


def _history_sampling_note() -> str:
    """The note of a history result's sampling summary. Its only figure is
    this reader's per-series cap (``HISTORY_DOWNSAMPLE_POINTS``), so it says
    the cap is the tool's own and holds for any window."""
    cap = HISTORY_DOWNSAMPLE_POINTS
    return (
        "Each series listed under series returned a sample of the values it read, not every "
        "stored value: points_returned of values_read. This tool returns at most "
        f"{cap} values per series per read, whatever the window or max_results, so only a "
        f"window holding no more than {cap} stored values returns every one. "
        "The first and last value read are always "
        "included; the rest are picked toward evenly spaced times between them, so spacing is "
        "uneven where the data has gaps. values_read is the number of values "
        "stored in the requested window, except for a series whose outcome is partial, "
        "where it counts only the values read before the cut. When you list or count "
        "these readings or give their spacing, say they are a sample and give both counts."
    )


def _outcome_points(outcome) -> list | None:
    """The returned points of a complete or partial outcome, else ``None``."""
    if isinstance(outcome, list):
        return outcome
    if isinstance(outcome, dict) and outcome.get("status") == "partial":
        points = outcome.get("points")
        return points if isinstance(points, list) else None
    return None


def _history_sampling(finalized: dict, state: _HistoryReadState) -> dict | None:
    """``{"note", "series"}`` for every series that returned fewer points
    than it read (``{key: {"points_returned", "values_read"}}``), or ``None``
    when every series returned every value it read."""
    series = {}
    for key, outcome in finalized.items():
        points = _outcome_points(outcome)
        read = state.raw_counts.get(key, 0)
        if points is not None and read > len(points):
            series[key] = {"points_returned": len(points), "values_read": read}
    return {"note": _history_sampling_note(), "series": series} if series else None


def _history_batch_page(page: list[dict], offset: int, start_time, end_time
                        ) -> tuple[dict[str, str], list[dict]]:
    """Build one chunk's ``entryId``→key map and its API entries."""
    idx_to_key = {
        str(offset + index): _history_key(entry)
        for index, entry in enumerate(page)
    }
    batch_entries = [
        {
            "entryId": str(offset + index),
            "assetId": entry["assetId"],
            "propertyId": entry["propertyId"],
            "startDate": start_time,
            "endDate": end_time,
            "timeOrdering": "ASCENDING",
        }
        for index, entry in enumerate(page)
    ]
    return idx_to_key, batch_entries


def get_property_history(connection_config: dict | None, region: str | None,
                         entries: list[dict], hours_back: int = 8,
                         max_results: int = 100, start_iso: str | None = None,
                         end_iso: str | None = None) -> dict:
    """Bounded history with one honest terminal outcome per deduplicated key.

    ``start_iso``/``end_iso`` (ISO-8601, UTC assumed for naive values) select
    an ABSOLUTE window for historical questions; otherwise the trailing
    ``hours_back`` window anchored at NOW applies (see
    ``_validated_history_window``). ``max_results`` is validated for
    compatibility only — the service page size is always
    ``HISTORY_FETCH_PAGE_SIZE`` so coverage never depends on the caller's
    page-size choice (ADR-0065, 2026-09-18).

    A series that read more values than it returned is named, with both
    counts, under ``HISTORY_SAMPLING_KEY`` (see :func:`_history_sampling`).
    """
    normalized, invalid = _validated_history_entries(entries)
    if invalid:
        return invalid
    if not normalized:
        return {}
    start_time, end_time, invalid = _validated_history_window(
        hours_back, start_iso, end_iso
    )
    if invalid:
        return invalid
    _, invalid = _validated_history_bound(
        max_results, "max_results", 100, 1, HISTORY_MAX_RESULTS
    )
    if invalid:
        return invalid
    client = _client(connection_config, region)
    state = _HistoryReadState(requested_start=start_time.isoformat(),
                              requested_end=end_time.isoformat())

    for start in range(0, len(normalized), _BATCH_VALUE_PAGE):
        if state.halt_reason:
            break
        idx_to_key, batch_entries = _history_batch_page(
            normalized[start:start + _BATCH_VALUE_PAGE], start, start_time, end_time
        )
        _read_history_chunk(client, batch_entries, idx_to_key, state)
    finalized = _finalize_history_outcomes(normalized, state)
    finalized = _mark_windows_before_first_value(client, normalized, finalized, state,
                                                 start_time, end_time)
    return _with_history_summaries(finalized, state)


#: Top-level key of a history result stating the window the call read: its
#: resolved ``requested_start`` and ``requested_end`` (UTC ISO), the same for
#: every series. A complete series is a bare list of points, and a point's
#: time is when that value was stored, so without it the result did not say
#: which window was asked for. It starts with ``_`` so readers that walk the
#: per-series outcomes skip it, as they skip ``HISTORY_SAMPLING_KEY``.
HISTORY_WINDOW_KEY = "_window"


def _with_history_summaries(finalized: dict, state: _HistoryReadState) -> dict:
    """``finalized`` with the read's two summaries beside its series: the
    sampling summary when a series was sampled (:func:`_history_sampling`),
    and the requested window, always."""
    sampling = _history_sampling(finalized, state)
    if sampling:
        finalized[HISTORY_SAMPLING_KEY] = sampling
    finalized[HISTORY_WINDOW_KEY] = {"requested_start": state.requested_start,
                                     "requested_end": state.requested_end}
    return finalized


# First-stored-value probe (2026-10-04). On a fresh account the feed starts
# minutes before use, so a window on an earlier date is empty and an answer
# read "no historical data" as lost data. A COMPLETE empty series gets ONE
# bounded single-property call for the earliest stored value; when that value
# is after the window's end the outcome says where the data starts (timestamps
# only). Any probe failure keeps the plain empty list.
#
# 2026-10-05: the probe span starts at the EARLIER of the
# requested window's start and the fixed lookback, so a window older than the
# lookback sits inside the probed span (a value found inside the lookback is
# then never mislabeled as the first stored value while older data could sit
# before and inside the window); the outcome names that span (``probed_from``)
# so the claim is bounded, not absolute; and every probe is one attempted call
# against ``HISTORY_MAX_TOTAL_CALLS`` alongside the batch pages, so a request
# never exceeds the budget the gateway window was sized for.
HISTORY_MAX_FIRST_VALUE_PROBES = 8
_FIRST_VALUE_LOOKBACK = _dt.timedelta(days=3660)
BEFORE_FIRST_VALUE_STATUS = "before_first_value"


def _first_value_probe_start(start_time: _dt.datetime, now: _dt.datetime) -> _dt.datetime:
    """Whole-second start of the probed span: the requested window's start or
    the lookback from now, whichever is earlier."""
    return min(start_time, now - _FIRST_VALUE_LOOKBACK).replace(microsecond=0)


def _first_stored_seconds(client, entry: dict, probe_start: _dt.datetime,
                          now: _dt.datetime) -> float | None:
    # Whole seconds: GetAssetPropertyValueHistory rejects a date with a
    # fractional part ("The date can only be in seconds", live 2026-10-04).
    try:
        response = client.get_asset_property_value_history(
            assetId=entry["assetId"], propertyId=entry["propertyId"],
            startDate=probe_start, endDate=now,
            timeOrdering="ASCENDING", maxResults=1,
        )
    except Exception:  # noqa: BLE001 - the probe is advisory; degrade to today's outcome
        return None
    values = response.get("assetPropertyValueHistory") if isinstance(response, dict) else None
    if not isinstance(values, list) or not values:
        return None
    stamp = _safe_history_point(values[0])[0]["timestamp"]
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
        return None
    return stamp


def _before_first_value(first_s: float, state: _HistoryReadState,
                        probe_start: _dt.datetime) -> dict | None:
    starts = _iso_from_seconds(first_s)
    if not starts:
        return None
    return {
        "status": BEFORE_FIRST_VALUE_STATUS,
        "points": [],
        "data_starts_at": starts,
        "requested_start": state.requested_start or None,
        "requested_end": state.requested_end or None,
        "probed_from": probe_start.isoformat(),
        "note": (
            f"No values in the requested window because this property's first "
            f"stored value since {probe_start.isoformat()} is at {starts}, after "
            "the window ends. The window predates the property's data; nothing "
            "was lost."
        ),
    }


def _mark_windows_before_first_value(client, normalized: list[dict], finalized: dict,
                                     state: _HistoryReadState, start_time, end_time) -> dict:
    now = _dt.datetime.now(_dt.UTC).replace(microsecond=0)
    probe_start = _first_value_probe_start(start_time, now)
    probes = 0
    for entry in normalized:
        key = _history_key(entry)
        outcome = finalized.get(key)
        if not isinstance(outcome, list) or outcome:
            continue
        if probes >= HISTORY_MAX_FIRST_VALUE_PROBES or state.total_calls >= HISTORY_MAX_TOTAL_CALLS:
            break
        probes += 1
        state.total_calls += 1  # attempted calls share the batch pages' budget
        first_s = _first_stored_seconds(client, entry, probe_start, now)
        if first_s is None or first_s <= end_time.timestamp():
            continue
        finalized[key] = _before_first_value(first_s, state, probe_start) or outcome
    return finalized


# ---------------------------------------------------------------------------
# Actionable error messaging (shared with system_agent — re-exported there)
# ---------------------------------------------------------------------------

def describe_asset_error_message(asset_id: str, exc: Exception) -> str:
    """Actionable message for a failed ``describe_asset``.

    A common failure is passing a knowledge-graph node id as a SiteWise ``assetId``.
    Rather than echo the raw error (which invites retrying a fabricated id), guide the
    caller back to the list-then-describe flow so it uses a REAL asset UUID.
    """
    err = str(exc)
    low = err.lower()
    if any(tok in low for tok in ("resourcenotfound", "not found", "invalid", "validation")):
        return (
            f"asset '{asset_id}' not found or invalid — do NOT guess asset ids. "
            f"List assets (no argument for the TOP_LEVEL root assets, or with an "
            f"asset_model_id) to obtain a valid asset id, then describe that. "
            f"(underlying error: {err})"
        )
    return err
