# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The opt-in pin cannot be bypassed by spelling: a qualified or aliased use
of a resend helper, or of ``resubmit_safe_write``, is found by the scanner
(``_resubmit_safe_writes``) and fails the pin check, and any use it cannot
tie to one pinned write is reported.

Each case plants a module in a scratch ``agents``-shaped directory and runs
the scanner over it; ``pin_mismatches`` is the pin test's own comparison.
"""
from __future__ import annotations

import textwrap

import pytest

from tests.unit import _resubmit_safe_writes as pins
from tests.unit.test_neptune_write_resubmit_opt_in import pin_mismatches

UNSAFE = "MATCH (n:Bin) DETACH DELETE n"


def _scan(tmp_path, **modules):
    for name, source in modules.items():
        (tmp_path / f"{name}.py").write_text(textwrap.dedent(source))
    return pins.opted_in_statements(tmp_path)


@pytest.mark.parametrize("source", [
    # module-qualified, through ``from . import onboard``
    '''
    from . import onboard

    def sweep(sid):
        onboard.neptune_write_resubmit_safe("MATCH (n:Bin) DETACH DELETE n", {"sid": sid})
    ''',
    # an import alias
    '''
    from .onboard import neptune_write_resubmit_safe as safe_write

    def sweep(sid):
        safe_write("MATCH (n:Bin) DETACH DELETE n", {"sid": sid})
    ''',
    # the returning helper through an aliased module
    '''
    from . import onboard as ob

    def sweep(sid):
        return ob.neptune_write_returning_resubmit_safe("MATCH (n:Bin) DETACH DELETE n", {})
    ''',
    # an absolute import of the package module
    '''
    import agents.onboard as ob

    def sweep(sid):
        ob.neptune_write_resubmit_safe("MATCH (n:Bin) DETACH DELETE n", {})
    ''',
    # ``resubmit_safe_write`` qualified through ``db``
    '''
    from . import db

    def sweep(sid):
        with db.resubmit_safe_write():
            _neptune_write("MATCH (n:Bin) DETACH DELETE n", {})
    ''',
    # ``resubmit_safe_write`` under an alias
    '''
    from .db import resubmit_safe_write as again

    def sweep(sid):
        with again():
            _neptune_write("MATCH (n:Bin) DETACH DELETE n", {})
    ''',
])
def test_a_qualified_or_aliased_opt_in_is_found_and_fails_the_pin(tmp_path, source):
    found, problems = _scan(tmp_path, planted=source)
    assert problems == []
    assert found == {("planted.py", "sweep"): frozenset({UNSAFE})}
    assert any("planted.py" in m and "not pinned" in m for m in pin_mismatches(found, problems))


def test_an_injected_writer_by_keyword_or_position_is_followed(tmp_path):
    found, problems = _scan(
        tmp_path,
        writers='''
        def put(sid, *, write):
            write("MATCH (n:Bin {id: $sid}) DETACH DELETE n", {"sid": sid})

        def put_positional(sid, write):
            write("MATCH (n:Rack {id: $sid}) DETACH DELETE n", {"sid": sid})
        ''',
        planted='''
        from . import onboard
        from .writers import put, put_positional

        def sweep(sid):
            put(sid, write=onboard.neptune_write_resubmit_safe)
            put_positional(sid, onboard.neptune_write_resubmit_safe)
        ''')
    assert problems == []
    assert found == {("planted.py", "sweep"): frozenset({
        "MATCH (n:Bin {id: $sid}) DETACH DELETE n", "MATCH (n:Rack {id: $sid}) DETACH DELETE n"})}


@pytest.mark.parametrize("source, why", [
    ('''
    from . import onboard

    def sweep(sid):
        w = onboard.neptune_write_resubmit_safe
        w("MATCH (n:Bin) DETACH DELETE n", {})
    ''', "used other than"),
    ('''
    from .onboard import neptune_write_resubmit_safe as safe_write

    WRITERS = [safe_write]
    ''', "outside a function"),
    ('''
    from . import onboard

    def sweep(sid, table):
        onboard.neptune_write_resubmit_safe(f"MATCH (n:{table}) DETACH DELETE n", {})
    ''', "unpinnable"),
    ('''
    from .db import resubmit_safe_write

    def sweep(sid):
        with resubmit_safe_write():
            _neptune_write("MATCH (n:Bin) DETACH DELETE n", {})
            _neptune_write("MATCH (n:Rack) DETACH DELETE n", {})
    ''', "exactly one"),
    ('''
    from .db import resubmit_safe_write

    def sweep(sid):
        scope = resubmit_safe_write()
        with scope:
            _neptune_write("MATCH (n:Bin) DETACH DELETE n", {})
    ''', "other than as a with-block"),
    ('''
    def relay(sid, *, write):
        other(write)

    def sweep(sid):
        from .onboard import neptune_write_resubmit_safe
        relay(sid, write=neptune_write_resubmit_safe)
    ''', "passes its writer"),
    # a reflective lookup of an opt-in name
    ('''
    from . import onboard

    def sweep(sid):
        getattr(onboard, "neptune_write_resubmit_safe")("MATCH (n:Bin) DETACH DELETE n", {})
    ''', "cannot resolve"),
    # an unaliased dotted import of the package module
    ('''
    import agents.onboard

    def sweep(sid):
        agents.onboard.neptune_write_resubmit_safe("MATCH (n:Bin) DETACH DELETE n", {})
    ''', "cannot resolve"),
    # the name rebound in the module
    ('''
    from .onboard import neptune_write as neptune_write_resubmit_safe

    def sweep(sid):
        neptune_write_resubmit_safe = print
    ''', "cannot resolve"),
])
def test_an_opt_in_the_scanner_cannot_tie_to_one_write_is_a_problem(tmp_path, source, why):
    found, problems = _scan(tmp_path, planted=source)
    assert any(why in p for p in problems), problems
    assert pin_mismatches(found, problems) != []


def test_a_parameter_text_is_read_from_every_caller(tmp_path):
    found, problems = _scan(tmp_path, planted='''
    from . import onboard

    A = "MATCH (n:Bin {id: $sid}) REMOVE n.mark"

    def _clear(sid, cypher):
        onboard.neptune_write_resubmit_safe(cypher, {"sid": sid})

    def clear_a(sid):
        _clear(sid, A)

    def clear_b(sid):
        _clear(sid, "MATCH (n:Rack {id: $sid}) REMOVE n.mark")
    ''')
    assert problems == []
    assert found == {("planted.py", "_clear"): frozenset({
        "MATCH (n:Bin {id: $sid}) REMOVE n.mark", "MATCH (n:Rack {id: $sid}) REMOVE n.mark"})}


def test_an_after_drop_opt_in_is_found_and_listed_apart(tmp_path):
    """A block with ``after_drop=True`` is an opt-in like any other and is also
    listed by ``opted_in_after_drop``; a block without it, or with a literal
    ``after_drop=False``, is not."""
    planted = '''
    from .db import resubmit_safe_write

    def seed(cid):
        with resubmit_safe_write(after_drop=True):
            neptune_write("MERGE (n:Bin {id: $cid}) ON CREATE SET n.label = $cid", {"cid": cid})

    def label(cid):
        with resubmit_safe_write(after_drop=False):
            neptune_write("MATCH (n:Bin {id: $cid}) SET n.label = $cid", {"cid": cid})

    def plain(cid):
        with resubmit_safe_write():
            neptune_write("MATCH (n:Rack {id: $cid}) SET n.label = $cid", {"cid": cid})
    '''
    found, problems = _scan(tmp_path, planted=planted)
    assert problems == [] and set(found) == {
        ("planted.py", "seed"), ("planted.py", "label"), ("planted.py", "plain")}
    after_drop, problems = pins.opted_in_after_drop(tmp_path)
    assert problems == []
    assert after_drop == {("planted.py", "seed"): frozenset({
        "MERGE (n:Bin {id: $cid}) ON CREATE SET n.label = $cid"})}


@pytest.mark.parametrize("opener", [
    "resubmit_safe_write(after_drop=flag)",
    "resubmit_safe_write(True)",
    "resubmit_safe_write(after_drop=True, other=1)",
])
def test_an_after_drop_flag_the_scanner_cannot_read_is_a_problem(tmp_path, opener):
    found, problems = _scan(tmp_path, planted=f'''
    from .db import resubmit_safe_write

    def seed(cid, flag=True):
        with {opener}:
            neptune_write("MERGE (n:Bin {{id: $cid}})", {{"cid": cid}})
    ''')
    assert any("after_drop" in p for p in problems), problems
    assert pin_mismatches(found, problems) != []


def test_the_resend_helper_cannot_widen_to_drops(tmp_path):
    """``after_drop`` on the onboard helper's own block would widen every site
    that calls the helper at once, so the helper body check refuses it."""
    found, problems = _scan(tmp_path, onboard='''
    from .db import resubmit_safe_write

    def neptune_write_resubmit_safe(cypher, parameters=None):
        with resubmit_safe_write(after_drop=True):
            return neptune_write(cypher, parameters)
    ''')
    assert any("helper body changed" in p for p in problems), problems
