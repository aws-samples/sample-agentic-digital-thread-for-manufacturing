# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""tests/integration/seed_pg.py applies the seed files the way the seed
Lambda does (lambda-adt-seed/handler.py): every file, in order, each whole;
a file that fails does not stop the rest, and the run then fails naming every
failed file and the first error.

Offline: a fake connection; nothing connects to PostgreSQL.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))


@pytest.fixture(scope="module")
def seed_pg():
    pytest.importorskip("pg8000")
    spec = importlib.util.spec_from_file_location(
        "seed_pg_mirrors_handler", _ADT_ROOT / "tests" / "integration" / "seed_pg.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Cursor:
    def __init__(self, executed, fail):
        self.executed, self.fail = executed, fail

    def execute(self, sql, params=None):
        name = sql.split("\n", 1)[0].removeprefix("-- ")
        self.executed.append(name)
        if name in self.fail:
            raise self.fail[name]

    def close(self):
        pass


class _Conn:
    def __init__(self, fail):
        self.executed, self.fail = [], fail

    def cursor(self):
        return _Cursor(self.executed, self.fail)


def _files(tmp_path):
    paths = []
    for name in ("001_a.sql", "002_b.sql", "003_c.sql"):
        path = tmp_path / name
        path.write_text(f"-- {name}\nSELECT 1;\n", encoding="utf-8")
        paths.append(path)
    return paths


def test_every_file_applies(seed_pg, tmp_path):
    conn = _Conn({})
    seed_pg._apply(conn, _files(tmp_path))
    assert conn.executed == ["001_a.sql", "002_b.sql", "003_c.sql"]


def test_a_failed_file_does_not_stop_the_rest_and_the_run_fails_naming_it(seed_pg, tmp_path):
    conn = _Conn({"002_b.sql": RuntimeError("boom")})
    with pytest.raises(Exception) as raised:
        seed_pg._apply(conn, _files(tmp_path))
    assert conn.executed == ["001_a.sql", "002_b.sql", "003_c.sql"]
    assert str(raised.value) == ("Demo seed failed: 1 of 3 SQL files failed: 002_b.sql. "
                                 "First error, in 002_b.sql: RuntimeError: boom")
