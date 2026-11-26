# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``agents.sitewise_read.list_associated_assets`` — bounded downward (CHILD) and
upward (PARENT) asset-hierarchy traversal via ``ListAssociatedAssets``.

Fully synthetic: the ``iotsitewise`` client is a stub driven by an in-memory
tree with ARBITRARY ids/names (never a live customer's names). No AWS, no
network, no dataset literals. The stub follows the API contract: CHILD without
a ``hierarchyId`` returns every immediate child across all hierarchies, and each
summary carries the child's own ``hierarchies``.

Asserts breadth-first depth/total caps and the partial markers, a failed branch
read marking the result partial (never a silent gap), the default depth reaching
equipment under a site, one list call per expanded non-leaf asset and one client
per walk, the cycle guard, pagination, the optional first-level hierarchy filter,
the PARENT direction (no hierarchyId), the bad-asset-id error shape (same family
as describe errors), and invalid-argument rejection.
"""
from __future__ import annotations

import agents.aws_session as aws_session
import agents.sitewise_read as sw
import agents.tool_result_summary as trs


class _ApiError(Exception):
    """Mimics a botocore ClientError: carries ``response['Error']['Code']``."""

    def __init__(self, code: str, message: str = "boom"):
        super().__init__(f"{code}: {message}")
        self.response = {"Error": {"Code": code, "Message": message}}


class _FakeTree:
    """iotsitewise double driven by a synthetic asset tree.

    ``nodes[asset_id] = {"name", "assetModelId", "hierarchies": [{"id","name"}],
    "children": {hierarchy_id: [child_asset_id, ...]}, "parents": [asset_id,...]}``.
    ``list_associated_assets`` is paginated (two child ids per page) so the
    reader's own pagination is exercised. ``fail_on`` maps an asset id to the
    error raised when its children are listed.
    """

    def __init__(self, nodes: dict, fail_on: dict | None = None):
        self._nodes = nodes
        self._fail_on = fail_on or {}
        self.assoc_calls: list[dict] = []
        self.describe_calls: list[str] = []
        self.page_size = 2
        self.pages_served = 0
        self.summaries_served = 0

    def describe_asset(self, assetId):  # noqa: N803 (boto3 kwarg)
        self.describe_calls.append(assetId)
        node = self._nodes[assetId]
        return {
            "assetId": assetId,
            "assetName": node["name"],
            "assetModelId": node.get("assetModelId", ""),
            "assetProperties": [],
            "assetHierarchies": node.get("hierarchies", []),
        }

    def _child_ids(self, asset_id, hierarchy_id):
        children = self._nodes.get(asset_id, {}).get("children", {})
        if hierarchy_id:
            return children.get(hierarchy_id, [])
        # API contract: no hierarchyId -> every immediate child, all hierarchies.
        return [cid for ids in children.values() for cid in ids]

    def _summary(self, child_id):
        node = self._nodes[child_id]
        return {"id": child_id, "name": node["name"],
                "assetModelId": node.get("assetModelId", ""),
                "hierarchies": node.get("hierarchies", [])}

    def get_paginator(self, name):
        assert name == "list_associated_assets", name

        def _paginate(**kwargs):
            self.assoc_calls.append(dict(kwargs))
            asset_id = kwargs["assetId"]
            direction = kwargs.get("traversalDirection", "CHILD")
            if direction == "CHILD" and asset_id in self._fail_on:
                raise self._fail_on[asset_id]
            if direction == "PARENT":
                ids = self._nodes.get(asset_id, {}).get("parents", [])
            else:
                if asset_id not in self._nodes:
                    raise _ApiError("ResourceNotFoundException", "asset not found")
                ids = self._child_ids(asset_id, kwargs.get("hierarchyId"))
            summaries = [self._summary(i) for i in ids]
            # Pages are served lazily (like botocore) and counted, so a test can
            # assert how much of a listing the reader actually requested. A
            # PaginationConfig PageSize (maxResults) is honoured like the API does.
            size = (kwargs.get("PaginationConfig") or {}).get("PageSize") or self.page_size
            pages = [summaries[i:i + size]
                     for i in range(0, len(summaries), size)] or [[]]

            def _serve():
                for page in pages:
                    self.pages_served += 1
                    self.summaries_served += len(page)
                    yield {"assetSummaries": page}
            return _serve()

        return type("P", (), {"paginate": staticmethod(_paginate)})()


def _patch(monkeypatch, client, capture=None):
    def fake(service, cfg, default_region=None):
        if capture is not None:
            capture["service"] = service
            capture["config"] = cfg
            capture["default_region"] = default_region
            capture["calls"] = capture.get("calls", 0) + 1
        return client
    monkeypatch.setattr(aws_session, "boto3_client_for", fake)


# A small two-site tree: Root -> (siteA, siteB); siteA -> areaA1 -> (m1, m2).
def _demo_tree():
    return {
        "root": {"name": "Root", "assetModelId": "mdl-root",
                 "hierarchies": [{"id": "h-site", "name": "sites"}],
                 "children": {"h-site": ["siteA", "siteB"]}},
        "siteA": {"name": "SiteA", "assetModelId": "mdl-site",
                  "hierarchies": [{"id": "h-area", "name": "areas"}],
                  "children": {"h-area": ["areaA1"]}, "parents": ["root"]},
        "siteB": {"name": "SiteB", "assetModelId": "mdl-site",
                  "hierarchies": [], "children": {}, "parents": ["root"]},
        "areaA1": {"name": "AreaA1", "assetModelId": "mdl-area",
                   "hierarchies": [{"id": "h-mach", "name": "machines"}],
                   "children": {"h-mach": ["m1", "m2"]}, "parents": ["siteA"]},
        "m1": {"name": "Line1CNC1", "assetModelId": "mdl-cnc",
               "hierarchies": [], "children": {}, "parents": ["areaA1"]},
        "m2": {"name": "Line1CNC2", "assetModelId": "mdl-cnc",
               "hierarchies": [], "children": {}, "parents": ["areaA1"]},
    }


# Site -> Area -> 3 Lines -> machines, each Line with FOUR child hierarchies
# (one per machine model), the shape where a per-hierarchy walk fans out.
def _site_tree():
    nodes = {
        "site": {"name": "SiteX", "assetModelId": "mdl-site",
                 "hierarchies": [{"id": "h-areas", "name": "Areas"}],
                 "children": {"h-areas": ["area"]}},
        "area": {"name": "Machining", "assetModelId": "mdl-area",
                 "hierarchies": [{"id": "h-lines", "name": "Lines"}],
                 "children": {"h-lines": ["l1", "l2", "l3"]}, "parents": ["site"]},
    }
    kinds = ("CNC", "Packaging", "QualityInspect", "Environment")
    for n in (1, 2, 3):
        line = f"l{n}"
        nodes[line] = {"name": f"Line{n}", "assetModelId": "mdl-line",
                       "hierarchies": [{"id": f"h-{k}", "name": k} for k in kinds],
                       "children": {}, "parents": ["area"]}
        for k in kinds:
            mid = f"{line}-{k}"
            nodes[line]["children"][f"h-{k}"] = [mid]
            nodes[mid] = {"name": f"Line{n}{k}", "assetModelId": f"mdl-{k}",
                          "hierarchies": [], "children": {}, "parents": [line]}
    return nodes


def test_child_one_level_lists_direct_children(monkeypatch):
    client = _FakeTree(_demo_tree())
    _patch(monkeypatch, client)
    out = sw.list_associated_assets({}, "us-east-1", "root", direction="CHILD", max_depth=1)
    assert out["truncated"] is False
    names = {a["name"] for a in out["assets"]}
    assert names == {"SiteA", "SiteB"}
    for a in out["assets"]:
        assert a["depth"] == 1
        assert a["parentId"] == "root"
        # path is names from the start asset
        assert a["path"] == [a["name"]]


def test_child_deep_walk_collects_whole_subtree_with_paths(monkeypatch):
    client = _FakeTree(_demo_tree())
    _patch(monkeypatch, client)
    out = sw.list_associated_assets({}, "us-east-1", "root", direction="CHILD", max_depth=5)
    assert out["truncated"] is False
    by_name = {a["name"]: a for a in out["assets"]}
    assert set(by_name) == {"SiteA", "SiteB", "AreaA1", "Line1CNC1", "Line1CNC2"}
    # Breadth-first depths
    assert by_name["SiteA"]["depth"] == 1
    assert by_name["AreaA1"]["depth"] == 2
    assert by_name["Line1CNC1"]["depth"] == 3
    # Path accumulates names from the start asset (start asset excluded, children named).
    assert by_name["Line1CNC1"]["path"] == ["SiteA", "AreaA1", "Line1CNC1"]
    assert by_name["Line1CNC1"]["parentId"] == "areaA1"


def test_default_depth_reaches_equipment_three_levels_below_a_site(monkeypatch):
    # Site -> Area -> Line -> machine: the default call (no max_depth) must list
    # the machines, which is the question the tool exists for.
    client = _FakeTree(_site_tree())
    _patch(monkeypatch, client)
    out = sw.list_associated_assets({}, "us-east-1", "site")
    names = {a["name"] for a in out["assets"]}
    assert {"Line1CNC", "Line2Packaging", "Line3QualityInspect"} <= names
    assert len(out["assets"]) == 1 + 3 + 12
    assert out["truncated"] is False
    assert out["requested_max_depth"] == 3


def test_walk_makes_one_list_call_per_expanded_asset_and_one_client(monkeypatch):
    # Per-hierarchy listing would make 1 + 1 + 3*4 = 14 calls on this shape plus a
    # DescribeAsset (and a fresh client) per expanded child. One unfiltered list
    # per non-leaf asset is 5 (site, area, 3 lines); leaves are never listed.
    client = _FakeTree(_site_tree())
    captured = {}
    _patch(monkeypatch, client, capture=captured)
    sw.list_associated_assets({}, "us-east-1", "site", max_depth=3)
    child_calls = [c for c in client.assoc_calls if c.get("traversalDirection") == "CHILD"]
    assert len(child_calls) == 5
    assert all("hierarchyId" not in c for c in child_calls)
    assert client.describe_calls == []
    assert captured["calls"] == 1


def test_child_depth_cap_counts_assets_left_below_the_limit(monkeypatch):
    client = _FakeTree(_demo_tree())
    _patch(monkeypatch, client)
    out = sw.list_associated_assets({}, "us-east-1", "root", direction="CHILD", max_depth=1)
    # Only depth-1 children; the walk did not descend into areas/machines.
    assert all(a["depth"] == 1 for a in out["assets"])
    assert "AreaA1" not in {a["name"] for a in out["assets"]}
    # Stopping at the requested depth is NOT a truncation of the requested result,
    # but the result says which listed assets have children of their own.
    assert out["truncated"] is False
    assert out["below_depth_limit"] == 1  # SiteA has a child hierarchy, SiteB none
    assert "max_depth" in out["note"]


def test_full_walk_has_no_depth_limit_note(monkeypatch):
    client = _FakeTree(_demo_tree())
    _patch(monkeypatch, client)
    out = sw.list_associated_assets({}, "us-east-1", "root", max_depth=5)
    assert out["below_depth_limit"] == 0
    assert "note" not in out


def test_child_total_asset_cap_marks_result_partial(monkeypatch):
    client = _FakeTree(_demo_tree())
    _patch(monkeypatch, client)
    out = sw.list_associated_assets({}, "us-east-1", "root",
                                    direction="CHILD", max_depth=5, max_assets=2)
    assert out["truncated"] is True
    assert out["status"] == "partial"
    assert out["reason"] == "max_assets"
    assert len(out["assets"]) == 2
    assert "2" in out["partial_note"]


def test_failed_branch_read_marks_result_partial_not_complete(monkeypatch):
    # Listing areaA1's children fails: its machines are missing, so the result
    # must say so instead of looking complete.
    client = _FakeTree(_demo_tree(), fail_on={"areaA1": _ApiError("ThrottlingException")})
    _patch(monkeypatch, client)
    out = sw.list_associated_assets({}, "us-east-1", "root", max_depth=5)
    names = {a["name"] for a in out["assets"]}
    assert "AreaA1" in names and "Line1CNC1" not in names
    assert out["truncated"] is True
    assert out["status"] == "partial"
    assert out["reason"] == "read_failed"
    assert out["skipped"] == [{"assetId": "areaA1", "operation": "ListAssociatedAssets",
                               "category": "ThrottlingException"}]
    assert "areaA1" in out["partial_note"]
    # The raw error message never reaches the result (category only).
    assert "boom" not in str(out)


def test_failed_branch_and_asset_cap_report_both_reasons(monkeypatch):
    client = _FakeTree(_demo_tree(), fail_on={"siteA": _ApiError("InternalFailureException")})
    _patch(monkeypatch, client)
    out = sw.list_associated_assets({}, "us-east-1", "root", max_depth=5, max_assets=1)
    assert out["truncated"] is True
    assert set(out["reason"].split(",")) <= {"max_assets", "read_failed"}
    assert "max_assets" in out["reason"]


def test_skipped_list_is_bounded(monkeypatch):
    nodes = {"root": {"name": "Root", "hierarchies": [{"id": "h", "name": "kids"}],
                      "children": {"h": [f"k{i}" for i in range(40)]}}}
    for i in range(40):
        nodes[f"k{i}"] = {"name": f"K{i}", "hierarchies": [{"id": "hh", "name": "x"}],
                          "children": {"hh": []}, "parents": ["root"]}
    fails = {f"k{i}": _ApiError("ThrottlingException") for i in range(40)}
    client = _FakeTree(nodes, fail_on=fails)
    _patch(monkeypatch, client)
    out = sw.list_associated_assets({}, "us-east-1", "root", max_depth=2)
    assert out["skipped_count"] == 40
    assert len(out["skipped"]) == sw.ASSOCIATED_MAX_SKIPPED_REPORTED
    assert "40" in out["partial_note"]


def test_partial_result_summary_says_it_is_partial(monkeypatch):
    client = _FakeTree(_demo_tree(), fail_on={"areaA1": _ApiError("ThrottlingException")})
    _patch(monkeypatch, client)
    out = sw.list_associated_assets({}, "us-east-1", "root", max_depth=5)
    import json
    wrapped = {"content": [{"text": json.dumps(out)}]}  # the tool-result envelope
    summary = trs.summarize_tool_result("sitewise_list_associated_assets", wrapped)
    assert "partial" in summary.lower() or "truncated" in summary.lower()
    assert "read_failed" in summary


def test_asset_cap_bounds_the_listing_work_not_just_the_rows(monkeypatch):
    # 199 non-leaf children under the root, 200 leaves under each, 50 per page.
    # A walk that lists every next-level parent in full before applying the cap
    # would request 800 pages here; the cap must stop the requests themselves.
    nodes = {"root": {"name": "Root", "hierarchies": [{"id": "h", "name": "kids"}],
                      "children": {"h": [f"p{i}" for i in range(199)]}}}
    for i in range(199):
        leaves = [f"p{i}-l{j}" for j in range(200)]
        nodes[f"p{i}"] = {"name": f"P{i}", "hierarchies": [{"id": "hl", "name": "leaves"}],
                          "children": {"hl": leaves}, "parents": ["root"]}
        for leaf in leaves:
            nodes[leaf] = {"name": leaf, "hierarchies": [], "children": {}}
    client = _FakeTree(nodes)
    client.page_size = 50
    _patch(monkeypatch, client)
    out = sw.list_associated_assets({}, "us-east-1", "root")
    assert len(out["assets"]) == 200
    assert out["truncated"] is True and out["reason"] == "max_assets"
    # The root listing, then ONE short read of the first parent's leaves.
    assert len(client.assoc_calls) == 2
    assert client.pages_served <= 5
    assert client.summaries_served <= 199 + 50


def test_listing_asks_for_no_more_than_the_remaining_rows(monkeypatch):
    client = _FakeTree(_demo_tree())
    _patch(monkeypatch, client)
    sw.list_associated_assets({}, "us-east-1", "root", max_depth=5, max_assets=4)
    configs = [c.get("PaginationConfig") or {} for c in client.assoc_calls]
    # Each read asks for the rows still allowed plus one (to tell whether more exist).
    assert configs[0] == {"MaxItems": 5, "PageSize": 5}
    assert all(c.get("MaxItems") and c["MaxItems"] <= 5 for c in configs)


def test_reaching_the_cap_exactly_with_nothing_left_is_not_partial(monkeypatch):
    client = _FakeTree(_demo_tree())
    _patch(monkeypatch, client)
    out = sw.list_associated_assets({}, "us-east-1", "root", max_depth=5, max_assets=5)
    assert len(out["assets"]) == 5
    assert out["truncated"] is False


def test_child_max_depth_is_capped_at_5(monkeypatch):
    client = _FakeTree(_demo_tree())
    _patch(monkeypatch, client)
    out = sw.list_associated_assets({}, "us-east-1", "root",
                                    direction="CHILD", max_depth=99)
    # Capped to 5 internally; the small tree is only 3 deep so it completes.
    assert out["truncated"] is False
    assert out["requested_max_depth"] == 5


def test_child_max_assets_is_capped_at_500(monkeypatch):
    client = _FakeTree(_demo_tree())
    _patch(monkeypatch, client)
    out = sw.list_associated_assets({}, "us-east-1", "root",
                                    direction="CHILD", max_assets=10**6)
    assert out["requested_max_assets"] == 500


def test_child_cycle_guard_does_not_loop(monkeypatch):
    # areaA1 points back up to root through a child hierarchy → a cycle.
    tree = _demo_tree()
    tree["areaA1"]["children"]["h-mach"] = ["m1", "m2", "root"]
    client = _FakeTree(tree)
    _patch(monkeypatch, client)
    out = sw.list_associated_assets({}, "us-east-1", "root", direction="CHILD", max_depth=5)
    # Each asset appears exactly once despite the back-edge to root.
    ids = [a["id"] for a in out["assets"]]
    assert len(ids) == len(set(ids))
    assert out["truncated"] is False


def test_child_multi_hierarchy_children_come_back_in_one_call(monkeypatch):
    # siteA has TWO hierarchies; both sets of children come back from one call.
    tree = _demo_tree()
    tree["siteA"]["hierarchies"] = [
        {"id": "h-area", "name": "areas"},
        {"id": "h-util", "name": "utilities"},
    ]
    tree["siteA"]["children"]["h-util"] = ["util1"]
    tree["util1"] = {"name": "Compressor1", "assetModelId": "mdl-util",
                     "hierarchies": [], "children": {}, "parents": ["siteA"]}
    client = _FakeTree(tree)
    _patch(monkeypatch, client)
    out = sw.list_associated_assets({}, "us-east-1", "siteA", direction="CHILD", max_depth=1)
    names = {a["name"] for a in out["assets"]}
    assert names == {"AreaA1", "Compressor1"}
    assert len([c for c in client.assoc_calls if c["assetId"] == "siteA"]) == 1


def test_hierarchy_filter_by_name_limits_the_first_level(monkeypatch):
    tree = _demo_tree()
    tree["siteA"]["hierarchies"] = [
        {"id": "h-area", "name": "areas"},
        {"id": "h-util", "name": "utilities"},
    ]
    tree["siteA"]["children"]["h-util"] = ["util1"]
    tree["util1"] = {"name": "Compressor1", "assetModelId": "mdl-util",
                     "hierarchies": [], "children": {}, "parents": ["siteA"]}
    client = _FakeTree(tree)
    captured = {}
    _patch(monkeypatch, client, capture=captured)
    out = sw.list_associated_assets({}, "us-east-1", "siteA", hierarchy_id="utilities",
                                    max_depth=1)
    assert [a["name"] for a in out["assets"]] == ["Compressor1"]
    first = [c for c in client.assoc_calls if c["assetId"] == "siteA"][0]
    assert first["hierarchyId"] == "h-util"
    assert captured["calls"] == 1  # the name lookup reuses the walk's client


def test_unknown_hierarchy_filter_is_invalid_request(monkeypatch):
    client = _FakeTree(_demo_tree())
    _patch(monkeypatch, client)
    out = sw.list_associated_assets({}, "us-east-1", "siteA", hierarchy_id="nope")
    assert out["error_type"] == "invalid_request"
    assert "areas" in out["error"]


def test_child_pagination_merges_pages(monkeypatch):
    # areaA1 has 5 machines; page_size is 2 so three pages are merged.
    tree = _demo_tree()
    tree["areaA1"]["children"]["h-mach"] = ["m1", "m2", "m3", "m4", "m5"]
    for i in (3, 4, 5):
        tree[f"m{i}"] = {"name": f"Line1CNC{i}", "assetModelId": "mdl-cnc",
                         "hierarchies": [], "children": {}, "parents": ["areaA1"]}
    client = _FakeTree(tree)
    _patch(monkeypatch, client)
    out = sw.list_associated_assets({}, "us-east-1", "areaA1", direction="CHILD", max_depth=1)
    machine_names = {a["name"] for a in out["assets"]}
    assert machine_names == {"Line1CNC1", "Line1CNC2", "Line1CNC3", "Line1CNC4", "Line1CNC5"}


def test_parent_returns_parent_without_hierarchy_id(monkeypatch):
    client = _FakeTree(_demo_tree())
    _patch(monkeypatch, client)
    out = sw.list_associated_assets({}, "us-east-1", "m1", direction="PARENT")
    assert [a["name"] for a in out["assets"]] == ["AreaA1"]
    # PARENT traversal passes NO hierarchyId to the API.
    parent_calls = [c for c in client.assoc_calls if c.get("traversalDirection") == "PARENT"]
    assert parent_calls and all("hierarchyId" not in c for c in parent_calls)
    assert out["assets"][0]["depth"] == 1


def test_bad_asset_id_returns_describe_family_error(monkeypatch):
    client = _FakeTree(_demo_tree())
    _patch(monkeypatch, client)
    out = sw.list_associated_assets({}, "us-east-1", "ghost", direction="CHILD")
    assert "error" in out
    assert "ghost" in out["error"] and "do NOT guess" in out["error"]


def test_invalid_direction_is_invalid_request(monkeypatch):
    client = _FakeTree(_demo_tree())
    _patch(monkeypatch, client)
    out = sw.list_associated_assets({}, "us-east-1", "root", direction="SIDEWAYS")
    assert out["error_type"] == "invalid_request"
    assert "direction" in out["error"]


def test_blank_asset_id_is_invalid_request(monkeypatch):
    client = _FakeTree(_demo_tree())
    _patch(monkeypatch, client)
    out = sw.list_associated_assets({}, "us-east-1", "   ", direction="CHILD")
    assert out["error_type"] == "invalid_request"


def test_oversized_asset_id_is_invalid_request(monkeypatch):
    client = _FakeTree(_demo_tree())
    _patch(monkeypatch, client)
    too_long = "x" * (sw.SITEWISE_IDENTIFIER_MAX_CHARS + 1)
    out = sw.list_associated_assets({}, "us-east-1", too_long, direction="CHILD")
    assert out["error_type"] == "invalid_request"


def test_cross_account_config_flows_to_helper(monkeypatch):
    client = _FakeTree(_demo_tree())
    captured = {}
    _patch(monkeypatch, client, capture=captured)
    cfg = {"role_arn": "arn:aws:iam::111122223333:role/sw", "region": "us-west-2"}
    sw.list_associated_assets(cfg, "us-east-1", "root", direction="CHILD")
    assert captured["service"] == "iotsitewise"
    assert captured["config"]["role_arn"] == "arn:aws:iam::111122223333:role/sw"
    assert captured["default_region"] == "us-east-1"
