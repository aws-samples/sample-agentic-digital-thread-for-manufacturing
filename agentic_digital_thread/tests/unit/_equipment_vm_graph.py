# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The ValueMapping side of the graph the equipment post-pass writes, in memory.

``EquipmentVmGraph`` answers the statements ``onboard._persist_equipment_value_mappings``
sends, recognised by ``onboard``'s own cypher constants: the one-statement
mapping write (``_value_mapping_upsert``, with or without its Asset anchor), the
generated-duplicate retirement and the stale-row reconcile, each with its
checked readback, and the Asset count. Anything else fails the test. The
predicates mirror the cypher (``_EQUIPMENT_VM_DUPLICATES``,
``_STALE_EQUIPMENT_VMS``); ``tests/integration/test_equipment_vm_sweep_memgraph.py``
runs the real statements on an openCypher engine.

Failures are injected the way the graph reports them: ``refuse(name, params)``
makes a write answer ``None`` (refused), ``unreadable(name, params)`` makes a
checked read raise, and ``left_on_delete`` names ids a stale delete leaves in
place. The reconcile's scope is the registries the pass read in full
(``$tsys``, ``$nsys``) and, after a whole discovery, every system that is no
registry now (not in ``$regs``). ``calls`` records ``(kind, statement name, params)`` for every statement.
"""
from __future__ import annotations

import re
from collections import Counter

import agents.onboard as onboard

#: The statements recognised by identity, by their ``onboard`` constant name.
_STATEMENTS = ("_RETIRE_EQUIPMENT_VM_DUPLICATES", "_EQUIPMENT_VM_DUPLICATES_LEFT",
               "_RETIRE_STALE_EQUIPMENT_VMS", "_STALE_EQUIPMENT_VMS_LEFT",
               "_EQUIPMENT_ASSET_COUNT", "_EQUIPMENT_VM_ANCHORS")
_UPSERT = "MERGE (vm:ValueMapping {id: $vmid})"
_IDENTITY = ("from_system", "from_value", "to_system", "to_value", "concept")


class EquipmentVmGraph:
    def __init__(self, systems, assets=()):
        self.systems = set(systems)
        self.assets = Counter(assets)
        self.nodes: dict[str, dict] = {}
        self.anchors: dict[str, str] = {}
        self.calls: list[tuple[str, str, dict]] = []
        self.refuse = None
        self.unreadable = None
        self.left_on_delete: set[str] = set()

    def install(self, monkeypatch) -> "EquipmentVmGraph":
        monkeypatch.setattr(onboard, "_db_neptune_write_returning", self.write_returning,
                            raising=False)
        monkeypatch.setattr(onboard, "neptune_query_checked", self.query_checked)

        def no_bool_write(cypher, params=None):
            raise AssertionError(f"the equipment post-pass used neptune_write: {cypher[:80]}")
        monkeypatch.setattr(onboard, "neptune_write", no_bool_write)
        return self

    def add(self, vm_id: str, **props) -> None:
        """A ValueMapping already in the graph (a prior provision's, a steward's)."""
        self.nodes[vm_id] = {"id": vm_id, **props}

    def rows(self, prefix: str = "vm-equip-") -> list[tuple]:
        """``(from_system, from_value, to_system, to_value, method)`` of the
        nodes whose id starts with ``prefix``, sorted."""
        return sorted((n.get("from_system"), n.get("from_value"), n.get("to_system"),
                       n.get("to_value"), n.get("method"))
                      for i, n in self.nodes.items() if i.startswith(prefix))

    def statements(self, name: str) -> list[dict]:
        return [params for _kind, n, params in self.calls if n == name]

    # ------------------------------------------------------------------ #

    def _name(self, cypher: str) -> str:
        for name in _STATEMENTS:
            if cypher == getattr(onboard, name, None):
                return name
        if _UPSERT in cypher:
            return "upsert"
        raise AssertionError(f"the fake graph does not know this statement: {cypher[:160]}")

    def write_returning(self, cypher, params=None):
        params, name = dict(params or {}), self._name(cypher)
        self.calls.append(("write", name, params))
        if self.refuse and self.refuse(name, params):
            return None
        if name == "upsert":
            return self._upsert(cypher, params)
        if name == "_RETIRE_EQUIPMENT_VM_DUPLICATES":
            self._delete(self._duplicates(params))
            return []
        if name == "_RETIRE_STALE_EQUIPMENT_VMS":
            self._delete([i for i in self._stale(params) if i not in self.left_on_delete])
            return []
        raise AssertionError(f"{name} is not a write")

    def query_checked(self, cypher, params=None):
        params, name = dict(params or {}), self._name(cypher)
        self.calls.append(("read", name, params))
        if self.unreadable and self.unreadable(name, params):
            raise RuntimeError("graph unavailable")
        if name == "_EQUIPMENT_ASSET_COUNT":
            return [{"n": self.assets[params["aid"]]}]
        if name == "_EQUIPMENT_VM_ANCHORS":
            target = self.anchors.get(params["vmid"])
            return [{"anchors": int(target is not None), "correct": int(target == params["aid"])}]
        if name == "_EQUIPMENT_VM_DUPLICATES_LEFT":
            return [{"n": len(self._duplicates(params))}]
        if name == "_STALE_EQUIPMENT_VMS_LEFT":
            return [{"n": len(self._stale(params))}]
        raise AssertionError(f"{name} is not a read")

    def _upsert(self, cypher, params):
        if params["fsys"] not in self.systems or params["tsys"] not in self.systems:
            return []
        anchored = "MAPS_TO_ASSET" in cypher
        if anchored and self.assets[params["aid"]] != 1:
            return []
        props = {prop: params[param] for prop, param in re.findall(r"vm\.(\w+) = \$(\w+)", cypher)}
        self.nodes.setdefault(params["vmid"], {"id": params["vmid"]}).update(props)
        if anchored:
            self.anchors[params["vmid"]] = params["aid"]
        return [{"id": params["vmid"]}]

    def _duplicates(self, p) -> list[str]:
        want = (p["fsys"], p["fval"], p["tsys"], p["tval"], p["concept"])
        return [i for i, n in self.nodes.items() if i.startswith(p["prefix"]) and i != p["vmid"]
                and tuple(n.get(k) for k in _IDENTITY) == want]

    def _stale(self, p) -> list[str]:
        # ``regs``: the registries a whole discovery returned. A sweep that
        # sends none (older code) has no absent-registry clause here.
        regs = p.get("regs")

        def absent(system):
            # openCypher: ``NOT (null IN $regs)`` is null, so a row without
            # the property is never selected.
            return regs is not None and system is not None and system not in regs

        def in_scope(n):
            fsys, tsys = n.get("from_system"), n.get("to_system")
            telemetry = n.get("method") in p["tmethods"] and (fsys in p["tsys"] or absent(fsys))
            named = n.get("method") == p["nmethod"] and (
                (fsys in p["nsys"] and tsys in p["nsys"]) or absent(fsys) or absent(tsys))
            return telemetry or named
        return [i for i, n in self.nodes.items() if i.startswith(p["prefix"])
                and i not in p["keep"] and n.get("concept") == p["concept"] and in_scope(n)]

    def _delete(self, ids) -> None:
        for vm_id in list(ids):
            self.nodes.pop(vm_id, None)
            self.anchors.pop(vm_id, None)
