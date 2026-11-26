# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""What the workshop hub's ``NameSpace_MCP`` tool returns, built offline.

The tool is the project's API-triggered pipeline (``config/highbyte/
intelligencehub-configuration.json``): a Smart Query over ``**`` selecting
value, path, name, model and timestamp ``as: List``. HighByte documents that
shape as one object per namespace node, the node's value with ``_path``,
``_name``, ``_model`` (and here ``_timestamp``) beside it. Each site node reads
its OPC UA server's ``<company>/<site>`` branch, and the OPC UA servers serve
the tags in the site datasets ``highbyte/generate_datasets.py`` writes
(``highbyte/components/opc-server``), so each site's value is that branch as a
tree, one value per tag. The datasets are build outputs, not in the repo, so
this fixture runs the same generator for a few steps into a temporary
directory (the tag set does not depend on the number of steps). Built from the
datasets and the documented shape, not read from a live hub (a local hub has
no OPC UA servers behind it).
"""
from __future__ import annotations

import atexit
import contextlib
import csv
import functools
import gzip
import importlib.util
import io
import shutil
import tempfile
from pathlib import Path

GENERATOR = Path(__file__).resolve().parents[3] / "highbyte" / "generate_datasets.py"
META = ("_path", "_name", "_model", "_timestamp")
TIMESTAMP = "2026-10-09T15:00:00.000Z"
_STEPS = 12


@functools.lru_cache(maxsize=None)
def _datasets() -> "tuple[Path, ...]":
    """The four site datasets, generated for :data:`_STEPS` steps (no random
    downtime; the story machine's fixed repair inside the window)."""
    spec = importlib.util.spec_from_file_location("generate_datasets_for_namespace", GENERATOR)
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    gen.NUM_STEPS = _STEPS
    gen.CMX441_DOWNTIME = (_STEPS - 4, 2)
    gen.make_downtimes = lambda *a, **k: []
    out = Path(tempfile.mkdtemp(prefix="hb-namespace-"))
    atexit.register(shutil.rmtree, out, True)
    gen.OUTPUT_DIR = out
    with contextlib.redirect_stdout(io.StringIO()):
        gen.generate_all()
    return tuple(sorted(out.glob("*.csv.gz")))


@functools.lru_cache(maxsize=None)
def _first_values(path: Path) -> dict[str, str]:
    """Each tag's first value in the dataset."""
    values: dict[str, str] = {}
    with gzip.open(path, "rt", newline="") as handle:
        for row in csv.DictReader(handle):
            values.setdefault(row["ALIAS"], row["VALUE"])
    return values


def _typed(value: str):
    for cast in (int, float):
        try:
            return cast(value)
        except ValueError:
            pass
    return value


def _aliases() -> "list[tuple[str, str]]":
    return [item for dataset in _datasets() for item in _first_values(dataset).items()]


def company() -> str:
    """The namespace root: the first segment every dataset alias shares."""
    (root,) = {alias.split("/", 1)[0] for alias, _value in _aliases()}
    return root


def site_branches() -> dict[str, dict]:
    """``{site: the <company>/<site> branch as a nested dict}``."""
    sites: dict[str, dict] = {}
    for alias, value in _aliases():
        _company, site, *rest = alias.split("/")
        node = sites.setdefault(site, {})
        for part in rest[:-1]:
            node = node.setdefault(part, {})
        node[rest[-1]] = _typed(value)
    return sites


def namespace_output() -> list[dict]:
    """The tool's JSON: one object per site node (the root company node has
    no input, so no value)."""
    root = company()
    return [{**branch, "_path": f"{root}.{site}", "_name": site, "_model": "",
             "_timestamp": TIMESTAMP}
            for site, branch in site_branches().items()]


def tag_paths() -> set[str]:
    """Every tag as discovery flattens it: the branch path below the site,
    dot-joined (sites share no area name, so no two sites collide)."""
    out: set[str] = set()

    def walk(node: dict, prefix: str) -> None:
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else key
            if isinstance(value, dict):
                walk(value, path)
            else:
                out.add(path)
    for branch in site_branches().values():
        walk(branch, "")
    return out
