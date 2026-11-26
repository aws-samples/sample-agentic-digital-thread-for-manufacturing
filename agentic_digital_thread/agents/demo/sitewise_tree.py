# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Create the SiteWise asset tree SFC writes into (ADR-0082 decision 9).

HighByte's ``NameSpace_to_SiteWise`` pipeline creates the assets itself
(``create: true``) where it runs. It does not run in the unattended workshop
profile (imported off with ``highbyte_sitewise_pipeline=disabled``) or without
the hub, and SFC's role deliberately cannot create anything, so ``SfcStack``
runs this as a CloudFormation custom resource (``lambda-sfc-sitewise-tree``) in
every SFC deployment, Workshop Studio included, after ensuring the models
exist. Where the pipeline does run, it finds this tree by name.

The tree is :func:`agents.demo.sfc_config.asset_tree` — HighByte's own
Enterprise/Site/Area/Line/process shape and names, plus a unique external id on
every asset. Idempotent, and it reuses rather than duplicates:

* an asset already carrying its external id is kept (a re-run does nothing);
* an asset of the right name already under the right parent — the tree a
  HighByte deployment built — is kept and given its external id;
* the CMX-441 mill (``adopt_by_name``) reuses the existing top-level asset of
  that name the feed Lambda and the workshop seed create, keeping its id;
* anything else is created and associated under its parent.

It never deletes, renames, or changes a model.

The walk is level by level, not asset by asset: every asset of a level is
found or created first, the level is polled to ACTIVE in one loop, and
associations go one child per parent per round (a parent in UPDATING rejects a
second association). Polls therefore grow with the tree's depth and widest
fan-out (five levels, fifteen) rather than its 135 assets; the serial version
hit the custom resource's 15-minute Lambda ceiling on a fresh account.

Manual re-run (optional; the stack already does this):
  cd agentic_digital_thread && PYTHONPATH=. python3 -m agents.demo.sitewise_tree \\
      --region <R> [--profile <P>]
"""
from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any, Callable

# Deliberately no repo imports at module level: this file is also the SFC
# stack's custom-resource Lambda code (lambda-sfc-sitewise-tree), which ships
# it with the tree as JSON and without the generator. ``asset_tree`` is
# imported lazily only where the tree is computed locally.

_NOT_FOUND = "ResourceNotFoundException"
_CONFLICT = "ConflictingOperationException"


class TreeError(RuntimeError):
    """The tree cannot be built as specified (missing model, FAILED asset...)."""


def _code(exc: Exception) -> str:
    return getattr(exc, "response", {}).get("Error", {}).get("Code", "")


class _Tree:
    def __init__(self, client: Any, *, sleep: Callable[[float], None] = time.sleep,
                 timeout_s: float = 120.0, log: Callable[[str], None] = print) -> None:
        self.c, self.sleep, self.timeout_s, self.log = client, sleep, timeout_s, log
        self.models: dict[str, str] = {}
        self.hier: dict[tuple[str, str], str] = {}
        self.children: dict[tuple[str, str], dict[str, str]] = {}
        self.ids: dict[str, str] = {}
        self.stats = {"created": 0, "adopted": 0, "kept": 0, "associated": 0}

    # -- lookups ------------------------------------------------------------

    def load_models(self, needed: set[str]) -> None:
        for page in self.c.get_paginator("list_asset_models").paginate():
            for m in page.get("assetModelSummaries", []):
                self.models.setdefault(m["name"], m["id"])
        missing = sorted(needed - set(self.models))
        if missing:
            raise TreeError(
                f"SiteWise asset models missing: {missing}. They are created by "
                "highbyte/DeploySitewiseModels.py (deploy.sh step 1a) — run it first.")

    def hierarchy_id(self, model: str, name: str) -> str:
        key = (model, name)
        if key not in self.hier:
            desc = self.c.describe_asset_model(assetModelId=self.models[model])
            for h in desc.get("assetModelHierarchies", []):
                self.hier[(model, h["name"])] = h["id"]
        if key not in self.hier:
            raise TreeError(f"model {model!r} has no hierarchy {name!r}")
        return self.hier[key]

    def by_external_id(self, ext: str) -> dict | None:
        try:
            return self.c.describe_asset(assetId=f"externalId:{ext}")
        except Exception as exc:  # noqa: BLE001 - only NotFound means "absent"
            if _code(exc) == _NOT_FOUND:
                return None
            raise

    def child_ids(self, parent_id: str, hier_id: str) -> dict[str, str]:
        key = (parent_id, hier_id)
        if key not in self.children:
            found: dict[str, str] = {}
            for page in self.c.get_paginator("list_associated_assets").paginate(
                    assetId=parent_id, hierarchyId=hier_id, traversalDirection="CHILD"):
                for a in page.get("assetSummaries", []):
                    found.setdefault(a["name"], a["id"])
            self.children[key] = found
        return self.children[key]

    def top_level_by_name(self, model: str, name: str) -> str | None:
        for page in self.c.get_paginator("list_assets").paginate(
                assetModelId=self.models[model], filter="TOP_LEVEL"):
            for a in page.get("assetSummaries", []):
                if a["name"] == name:
                    return a["id"]
        return None

    # -- mutations ----------------------------------------------------------

    def wait_active(self, asset_id: str) -> dict:
        return self.settle([asset_id])[asset_id]

    def settle(self, asset_ids: list[str] | set[str]) -> dict[str, dict]:
        """Poll every asset in one loop until all are ACTIVE; one sleep per
        round, however many assets are pending. Returns their descriptions."""
        pending, done, deadline = set(asset_ids), {}, self.timeout_s
        while pending:
            for asset_id in sorted(pending):
                desc = self.c.describe_asset(assetId=asset_id)
                state = desc["assetStatus"]["state"]
                if state == "ACTIVE":
                    done[asset_id] = desc
                    pending.discard(asset_id)
                elif state == "FAILED":
                    raise TreeError(
                        f"asset {asset_id} FAILED: {desc['assetStatus'].get('error')}")
            if not pending:
                break
            if deadline <= 0:
                raise TreeError(f"{len(pending)} assets not ACTIVE within {self.timeout_s}s: "
                                f"{sorted(pending)[:5]}")
            self.sleep(1.0)  # bounded poll  # nosemgrep: arbitrary-sleep
            deadline -= 1.0
        return done

    def retry_conflict(self, fn: Callable[[], Any], asset_id: str) -> Any:
        for _ in range(10):
            try:
                return fn()
            except Exception as exc:  # noqa: BLE001
                if _code(exc) != _CONFLICT:
                    raise
                self.wait_active(asset_id)
        return fn()

    def place(self, spec: Any) -> SimpleNamespace:
        """Find or create one asset without waiting on it; the level is
        settled as a whole afterwards (``build``)."""
        model_id = self.models[spec.model]
        parent_id = self.ids[spec.parent] if spec.parent else None
        hier_id = self.hierarchy_id(self.spec_models[spec.parent], spec.hierarchy) \
            if spec.parent else None

        desc = self.by_external_id(spec.external_id)
        asset_id = desc["assetId"] if desc else None
        how = "kept"
        if asset_id is None and parent_id:
            asset_id = self.child_ids(parent_id, hier_id).get(spec.name)
            how = "adopted"
        if asset_id is None and not parent_id:
            asset_id = self.top_level_by_name(spec.model, spec.name)
            how = "adopted"
        if asset_id is None and spec.adopt_by_name:
            asset_id = self.top_level_by_name(spec.model, spec.name)
            how = "adopted"
        if asset_id is None:
            asset_id = self.c.create_asset(assetName=spec.name, assetModelId=model_id,
                                           assetExternalId=spec.external_id)["assetId"]
            how = "created"
        self.ids[spec.external_id] = asset_id
        return SimpleNamespace(spec=spec, asset_id=asset_id, parent_id=parent_id,
                               hier_id=hier_id, how=how, name=spec.name)

    def tag(self, placed: list[SimpleNamespace]) -> None:
        """Settle the level, check each asset's model, and give every asset
        that lacks its external id one (then settle those again)."""
        descs = self.settle([p.asset_id for p in placed])
        retag: list[str] = []
        for p in placed:
            current = descs[p.asset_id]
            p.name = current["assetName"]
            if current.get("assetModelId") != self.models[p.spec.model]:
                raise TreeError(f"{p.spec.external_id}: existing asset {p.asset_id} is not "
                                f"a {p.spec.model!r} asset")
            if current.get("assetExternalId") != p.spec.external_id:
                self.retry_conflict(lambda: self.c.update_asset(
                    assetId=p.asset_id, assetName=current["assetName"],
                    assetExternalId=p.spec.external_id), p.asset_id)
                retag.append(p.asset_id)
        self.settle(retag)

    def link(self, placed: list[SimpleNamespace]) -> None:
        """Associate each asset under its parent. A parent can take one
        association at a time (it goes UPDATING), so each round associates one
        child per parent, then settles everything touched; rounds = the largest
        fan-out at this level, not the number of assets."""
        todo = [p for p in placed if p.parent_id
                and p.asset_id not in self.child_ids(p.parent_id, p.hier_id).values()]
        while todo:
            later, touched, taken = [], set(), set()
            for p in todo:
                if p.parent_id in taken:
                    later.append(p)
                    continue
                taken.add(p.parent_id)
                self.retry_conflict(lambda: self.c.associate_assets(
                    assetId=p.parent_id, hierarchyId=p.hier_id, childAssetId=p.asset_id),
                    p.parent_id)
                self.child_ids(p.parent_id, p.hier_id)[p.name] = p.asset_id
                self.stats["associated"] += 1
                touched.update((p.parent_id, p.asset_id))
            self.settle(touched)
            todo = later

    @staticmethod
    def levels(specs: list[Any]) -> list[list[Any]]:
        """Specs grouped by depth, root first, input order kept within a level."""
        depth: dict[str, int] = {}
        for s in specs:  # parents precede children in the generated tree
            depth[s.external_id] = depth[s.parent] + 1 if s.parent else 0
        out: list[list[Any]] = [[] for _ in range(max(depth.values(), default=-1) + 1)]
        for s in specs:
            out[depth[s.external_id]].append(s)
        return out

    def build(self, specs: list[Any]) -> dict[str, int]:
        # AssetSpec dataclasses (local) or plain dicts (the custom resource).
        specs = [SimpleNamespace(**s) if isinstance(s, dict) else s for s in specs]
        self.spec_models = {s.external_id: s.model for s in specs}
        self.load_models({s.model for s in specs})
        for level in self.levels(specs):
            placed = [self.place(s) for s in level]
            self.tag(placed)
            self.link(placed)
            for p in placed:
                self.stats[p.how] += 1
        return self.stats


def ensure_tree(client: Any, specs: list[Any] | None = None, **kwargs: Any) -> dict[str, int]:
    """Create or reuse every asset of the tree; returns counts per outcome.

    ``specs`` are ``sfc_config.AssetSpec`` objects or their dict form."""
    if specs is None:
        from agents.demo.sfc_config import asset_tree
        specs = asset_tree()
    return _Tree(client, **kwargs).build(specs)


def _main(argv: list[str] | None = None) -> int:  # pragma: no cover - deploy-time CLI
    import argparse

    import boto3
    from botocore.config import Config

    parser = argparse.ArgumentParser(prog="python3 -m agents.demo.sitewise_tree")
    parser.add_argument("--region", required=True)
    parser.add_argument("--profile", default=None)
    args = parser.parse_args(argv)
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    client = session.client("iotsitewise",
                            config=Config(retries={"mode": "adaptive", "max_attempts": 10}))
    from agents.demo.sfc_config import asset_tree

    specs = asset_tree()
    stats = ensure_tree(client, specs)
    print(f"SiteWise asset tree: {len(specs)} assets — " +
          ", ".join(f"{k} {v}" for k, v in stats.items()))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
