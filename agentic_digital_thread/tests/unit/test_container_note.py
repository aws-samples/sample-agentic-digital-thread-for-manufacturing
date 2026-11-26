# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""explore_graph adds what sits directly inside a container asset it returned
(agents/container_note.py). Fully offline: the graph read is a stub that
answers from an in-memory edge list the way the real read does. Names are
generic on purpose."""
import json

import agents.graph_tools as graph_tools
from agents.container_note import (_CHILDREN_QUERY, _PARENTS_QUERY, _READ_LIMIT, MAX_CHILDREN,
                                   MAX_CONTAINERS, container_contents)

_STATION = {"asset_name": "Station_1", "asset_type": "Station", "role": "work_unit",
            "level": "work_unit", "source_id": "st-1", "path": "S/P/Station_1"}


def _child(name, role, parent="st-1", parent_path="S/P/Station_1", path=None,
           system="sys-a", parent_role="work_unit", parent_name=None):
    return {"parent_id": parent, "parent_path": parent_path, "parent_system": system,
            "parent_role": parent_role,
            "parent_name": parent_name or str(parent_path).rsplit("/", 1)[-1],
            "name": name, "role": role, "asset_type": name.split("_")[0],
            "source_id": f"id-{name}", "path": path or f"{parent_path}/{name}"}


_PARENT_KEYS = ("parent_id", "parent_path", "parent_system", "parent_role", "parent_name")


class _Graph:
    """Answers the parent read (every distinct parent of ``children``, plus
    ``nodes`` with no children) and the child read the way the real reads do."""

    def __init__(self, children, nodes=()):
        self.children, self.calls = children, []
        records = {tuple(c[k] for k in _PARENT_KEYS) for c in children}
        records |= {tuple(n.get(k) for k in _PARENT_KEYS) for n in nodes}
        self.parents = [dict(zip(_PARENT_KEYS, r)) for r in sorted(records, key=str)]

    def __call__(self, cypher, params=None):
        self.calls.append((cypher, params))
        ids, paths = set(params["ids"]), set(params["paths"])
        pool = self.children if cypher == _CHILDREN_QUERY else self.parents
        return [c for c in pool
                if c["parent_id"] in ids or c["parent_path"] in paths][:_READ_LIMIT]


def test_container_row_lists_its_equipment_not_its_streams():
    graph = _Graph([_child("Press_1", "equipment"), _child("Press_2", "unclassified"),
                    _child("Press_1_Alarm", "data_stream"), _child("Bay_1", "work_unit")])
    out = container_contents([_STATION], graph)
    assert len(out) == 1
    entry = out[0]
    assert entry["path"] == "S/P/Station_1" and entry["role"] == "work_unit"
    assert entry["container"] == "Station_1" and entry["source_id"] == "st-1"
    assert [h["name"] for h in entry["holds"]] == ["Press_1", "Press_2"]
    assert entry["holds"][1]["role"] == "unclassified"     # may still be equipment
    assert entry["more"] == 0 and "complete" not in entry
    assert graph.calls[0][1] == {"ids": ["st-1"], "paths": []}


def test_no_container_means_no_extra_read():
    graph = _Graph([_child("Press_1", "equipment")])
    rows = [{"name": "Press_1", "role": "equipment", "source_id": "id-Press_1"}, {"x": 1}]
    assert container_contents(rows, graph) is None
    assert graph.calls == []


def test_a_graph_without_roles_gets_nothing():
    """Before the role pass (or with ADT_ASSET_ROLES=off) rows carry only a
    level: no contents read, so the role switch turns this off too."""
    row = {"a.name": "Line-1", "a.level": "line", "a.path": "S/A/Line-1", "a.system": "sys-a"}
    graph = _Graph([_child("Mixer_1", "equipment", parent=None, parent_path="S/A/Line-1")])
    assert container_contents([row], graph) is None
    assert graph.calls == []


def test_assets_the_query_already_returned_are_not_repeated():
    graph = _Graph([_child("Press_1", "equipment"), _child("Press_2", "equipment")])
    rows = [_STATION, {"name": "Press_1", "role": "equipment", "source_id": "id-Press_1"}]
    assert [h["name"] for h in container_contents(rows, graph)[0]["holds"]] == ["Press_2"]
    assert container_contents(rows, _Graph([_child("Press_1", "equipment")])) is None


def test_a_child_returned_by_path_only_is_not_repeated():
    """r4-6: the row knows the machine by path, the read by id and path."""
    graph = _Graph([_child("Press_1", "equipment"), _child("Press_2", "equipment")])
    rows = [_STATION, {"m.role": "equipment", "m.path": "S/P/Station_1/Press_1"}]
    assert [h["name"] for h in container_contents(rows, graph)[0]["holds"]] == ["Press_2"]


def test_children_are_capped_with_a_count_of_the_rest():
    graph = _Graph([_child(f"Press_{i}", "equipment") for i in range(MAX_CHILDREN + 5)])
    entry = container_contents([_STATION], graph)[0]
    assert len(entry["holds"]) == MAX_CHILDREN and entry["more"] == 5


def test_a_cut_read_says_so_and_keeps_every_container():
    """r4-5: past the read limit, 'more' is a lower bound and a container the
    cut starved still appears, marked incomplete."""
    first = [_child(f"Press_{i}", "equipment") for i in range(_READ_LIMIT)]
    other = {"role": "work_unit", "source_id": "st-2", "path": "S/P/Station_2"}
    late = _child("Press_x", "equipment", parent="st-2", parent_path="S/P/Station_2")
    out = container_contents([_STATION, other], _Graph(first + [late]))
    assert [e["complete"] for e in out] == [False, False]
    assert out[0]["more"] == _READ_LIMIT - MAX_CHILDREN
    assert out[1]["path"] == "S/P/Station_2" and out[1]["holds"] == []


def test_a_cut_read_never_lists_a_container_the_graph_denied():
    """r6-1: under truncation, a parent the graph says is equipment gets no
    entry, even with a container role borrowed from the row."""
    first = [_child(f"Press_{i}", "equipment") for i in range(_READ_LIMIT)]
    claim = {"name": "Press_9", "source_id": "id-Press_9", "role": "work_center"}
    denied = _child("Press_9_part", "equipment", parent="id-Press_9",
                    parent_path="S/P/Station_1/Press_9", parent_role="equipment")
    out = container_contents([_STATION, claim], _Graph(first + [denied]))
    assert [e["container"] for e in out] == ["Station_1"]
    assert out[0]["complete"] is False


def test_false_claims_cannot_crowd_out_a_real_container():
    """r6-2: rows only claim containers; the graph confirms them before the
    child read, so ten machine rows carrying their parent's role first do
    not hide the real station behind them, and the child read asks only for
    the confirmed station."""
    claims = [{"name": f"Press_{i}", "source_id": f"id-Press_{i}", "role": "work_unit"}
              for i in range(MAX_CONTAINERS)]
    denials = [_child(f"Press_{i}_part", "equipment", parent=f"id-Press_{i}",
                      parent_path=f"S/P/Station_1/Press_{i}", parent_role="equipment")
               for i in range(MAX_CONTAINERS)]
    graph = _Graph(denials + [_child("Mill_1", "equipment")])
    out = container_contents(claims + [_STATION], graph)
    assert [e["container"] for e in out] == ["Station_1"]
    assert [c for c, _ in graph.calls] == [_PARENTS_QUERY, _CHILDREN_QUERY]
    assert graph.calls[1][1] == {"ids": ["st-1"], "paths": []}


def test_unconfirmed_claims_never_fill_the_cap_on_a_cut_read():
    """r7-2: ten claims the graph has no record of, then a real station whose
    children fill the whole child read: only the station appears."""
    claims = [{"name": f"Ghost_{i}", "source_id": f"g-{i}", "role": "work_unit"}
              for i in range(MAX_CONTAINERS)]
    graph = _Graph([_child(f"Press_{i}", "equipment") for i in range(_READ_LIMIT + 5)])
    out = container_contents(claims + [_STATION], graph)
    assert [(e["container"], e["complete"]) for e in out] == [("Station_1", False)]


def test_fifty_denied_claims_do_not_hide_a_real_station():
    """r7-2: every claim up to the read's own bound is checked."""
    claims = [{"name": f"Press_{i}", "source_id": f"id-Press_{i}", "role": "work_unit"}
              for i in range(50)]
    denials = [{"parent_id": f"id-Press_{i}", "parent_path": f"S/P/Station_1/Press_{i}",
                "parent_system": "sys-a", "parent_role": "equipment",
                "parent_name": f"Press_{i}"} for i in range(50)]
    graph = _Graph([_child("Mill_1", "equipment")], nodes=denials)
    out = container_contents(claims + [_STATION], graph)
    assert [e["container"] for e in out] == ["Station_1"]


def test_a_cut_read_never_lends_one_systems_children_to_anothers_id():
    """r7-3: sys-b's Line shares sys-a's source id; sys-a's children fill the
    child read. The note names sys-b's line and lists none of sys-a's."""
    a_kids = [_child(f"Press_{i}", "equipment", parent="L1", parent_path="A/L1")
              for i in range(_READ_LIMIT)]
    b_kid = _child("Mixer_1", "equipment", parent="L1", parent_path="B/L1", system="sys-b",
                   parent_role="work_center")
    graph = _Graph(a_kids + [b_kid])
    row = {"role": "work_center", "source_id": "L1", "system": "sys-b"}
    out = container_contents([row], graph)
    assert [(e["path"], e["holds"], e["complete"]) for e in out] == [("B/L1", [], False)]
    # The parent read sees the id is shared, so without a system nothing is guessed.
    assert container_contents([{"role": "work_center", "source_id": "L1"}], graph) is None


def test_a_shared_id_is_seen_even_when_one_side_has_no_children():
    """A complete child read cannot prove an id unique: sys-b's line has no
    children, and the note must not list sys-a's under it."""
    graph = _Graph([_child("Press_A", "equipment", parent="L1", parent_path="A/L1")],
                   nodes=[{"parent_id": "L1", "parent_path": "B/L1", "parent_system": "sys-b",
                           "parent_role": "work_center", "parent_name": "L1"}])
    assert container_contents([{"role": "work_center", "source_id": "L1",
                                "system": "sys-b"}], graph) is None


def test_entries_are_capped_after_confirmation():
    stations = [{"role": "work_unit", "source_id": f"st-{i}", "path": f"S/P/Station_{i}"}
                for i in range(MAX_CONTAINERS + 3)]
    graph = _Graph([_child(f"Press_{i}", "equipment", parent=f"st-{i}",
                           parent_path=f"S/P/Station_{i}") for i in range(MAX_CONTAINERS + 3)])
    out = container_contents(stations, graph)
    assert len(out) == MAX_CONTAINERS
    assert [e["source_id"] for e in out] == [f"st-{i}" for i in range(MAX_CONTAINERS)]


def test_node_shaped_rows_and_same_prefix_columns():
    node = {"s": {"~id": "1", "~labels": ["Asset"],
                  "~properties": {"name": "Station_1", "role": "work_unit",
                                  "source_id": "st-1", "path": "S/P/Station_1"}}}
    aliased = {"kind": "x", "station_role": "work_unit", "station_id": "st-1"}
    for row in (node, aliased):
        out = container_contents([row], _Graph([_child("Press_1", "equipment")]))
        assert out and out[0]["holds"][0]["name"] == "Press_1", row


def test_a_role_is_never_borrowed_from_another_nodes_columns():
    """r4-4: a machine's id beside its PARENT's role is not a container."""
    row = {"asset_id": "id-Press_1", "asset_name": "Press_1", "role": "work_center"}
    graph = _Graph([_child("Press_1_part", "equipment", parent="id-Press_1",
                           parent_path="S/P/Station_1/Press_1", parent_role="equipment")])
    assert container_contents([row], graph) is None
    assert graph.calls == []


def test_bare_aliases_from_two_nodes_never_make_a_container():
    """r5-2: the row's role belongs to the line, the id to the machine. The
    graph says the machine is equipment, so no entry."""
    row = {"name": "Press_1", "source_id": "id-Press_1", "line": "L1", "role": "work_center"}
    graph = _Graph([_child("Press_1_part", "unclassified", parent="id-Press_1",
                           parent_path="S/P/Station_1/Press_1", parent_role="equipment")])
    assert container_contents([row], graph) is None


def test_a_display_name_in_a_system_column_does_not_hide_the_note():
    """r5-2: ``s.name AS system`` is not the graph's system id; with one
    system behind the id, the graph's own system decides."""
    row = {"name": "Station_1", "role": "work_unit", "source_id": "st-1", "system": "Plant MES"}
    out = container_contents([row], _Graph([_child("Press_1", "equipment")]))
    assert out and out[0]["holds"][0]["name"] == "Press_1"


def test_a_row_without_path_or_name_gets_the_graphs_name():
    """r5-3: two stations returned by id and role only stay distinguishable."""
    rows = [{"site": "S", "asset_name": "Station_1", "role": "work_unit", "source_id": "st-1"},
            {"site": "S", "asset_name": "Station_2", "role": "work_unit", "source_id": "st-2"}]
    graph = _Graph([_child("Press_1", "equipment"),
                    _child("Press_2", "equipment", parent="st-2", parent_path="S/P/Station_2")])
    out = container_contents(rows, graph)
    assert [(e["container"], e["source_id"], e["path"]) for e in out] == [
        ("Station_1", "st-1", "S/P/Station_1"), ("Station_2", "st-2", "S/P/Station_2")]
    assert [[h["name"] for h in e["holds"]] for e in out] == [["Press_1"], ["Press_2"]]


def test_duplicate_read_rows_are_listed_once():
    """r5-4: a duplicated edge returns a child twice."""
    graph = _Graph([_child("Press_1", "equipment"), _child("Press_1", "equipment"),
                    _child("Press_2", "equipment")])
    entry = container_contents([_STATION], graph)[0]
    assert [h["name"] for h in entry["holds"]] == ["Press_1", "Press_2"] and entry["more"] == 0


def test_a_path_only_container_is_scoped_to_its_system():
    """r4-2: two systems share a path; the note lists only the row's own."""
    shared = "Plant/Area/Line"
    graph = _Graph([_child("Press_A", "equipment", parent="a-line", parent_path=shared),
                    _child("Press_B", "equipment", parent="b-line", parent_path=shared,
                           system="sys-b")])
    row = {"l.role": "work_center", "l.path": shared, "l.system": "sys-a"}
    assert [h["name"] for h in container_contents([row], graph)[0]["holds"]] == ["Press_A"]
    # Without a system, a path alone is not enough to read anything.
    no_system = {"l.role": "work_center", "l.path": shared}
    assert container_contents([no_system], graph) is None


def test_an_id_shared_by_two_systems_needs_the_rows_system():
    graph = _Graph([_child("Press_A", "equipment", parent="L1", parent_path="P/L1"),
                    _child("Press_B", "equipment", parent="L1", parent_path="Q/L1",
                           system="sys-b")])
    assert container_contents([{"role": "work_center", "source_id": "L1"}], graph) is None
    named = {"role": "work_center", "source_id": "L1", "system": "sys-b"}
    assert [h["name"] for h in container_contents([named], graph)[0]["holds"]] == ["Press_B"]


def test_edges_a_rewalk_left_behind_are_ignored():
    """r4-3: an edge to an old ancestor (a tier was inserted) or to another
    branch does not make a child direct."""
    process = {"role": "work_center", "source_id": "p-1", "path": "S/A/P"}
    graph = _Graph([
        _child("Station_9", "work_unit", parent="p-1", parent_path="S/A/P"),
        _child("Press_1", "equipment", parent="p-1", parent_path="S/A/P",
               path="S/A/P/Station_9/Press_1"),                    # old ancestor edge
        _child("Press_7", "equipment", parent="p-1", parent_path="S/A/P",
               path="S/Q/Other/Press_7"),                          # other branch
        _child("Gauge_1", "equipment", parent="p-1", parent_path="S/A/P"),
    ])
    assert [h["name"] for h in container_contents([process], graph)[0]["holds"]] == ["Gauge_1"]


def test_a_name_with_a_slash_is_still_a_direct_child():
    graph = _Graph([_child("Press 1/2", "equipment")])
    assert container_contents([_STATION], graph)[0]["holds"][0]["name"] == "Press 1/2"


def test_the_note_lists_facts_never_review_flags():
    """r5-1: a missing identifier elsewhere does not tell a machine from its
    container's data, so the note carries no such hint, and its read never
    touches AssetModelRole."""
    graph = _Graph([_child("Press_1", "equipment"), _child("Oven_1", "equipment")])
    holds = container_contents([_STATION], graph)[0]["holds"]
    assert holds == [{"name": n, "role": "equipment", "asset_type": n.split("_")[0],
                      "source_id": f"id-{n}"} for n in ("Press_1", "Oven_1")]
    assert "AssetModelRole" not in graph.calls[0][0]


def test_a_failed_read_returns_none():
    def broken(cypher, params=None):
        raise RuntimeError("graph down")
    assert container_contents([_STATION], broken) is None


def test_explore_graph_envelope_carries_the_contents(monkeypatch):
    graph = _Graph([_child("Press_1", "equipment")])
    monkeypatch.setattr(graph_tools, "neptune_query_checked", lambda c: [dict(_STATION)])
    monkeypatch.setattr(graph_tools, "neptune_query", graph)
    out = json.loads(graph_tools.explore_graph("MATCH (a:Asset) RETURN a.role AS role"))
    assert out["results"] == [_STATION]
    assert out["inside_containers"][0]["holds"][0]["name"] == "Press_1"
    assert "containers" in out["inside_containers_note"]
    assert "container's own data" not in out["inside_containers_note"]


def test_explore_graph_keeps_its_result_when_the_contents_read_fails(monkeypatch):
    def broken(cypher, params=None):
        raise RuntimeError("graph down")
    monkeypatch.setattr(graph_tools, "neptune_query_checked", lambda c: [dict(_STATION)])
    monkeypatch.setattr(graph_tools, "neptune_query", broken)
    out = json.loads(graph_tools.explore_graph("MATCH (a:Asset) RETURN a"))
    assert out == {"results": [_STATION]}
