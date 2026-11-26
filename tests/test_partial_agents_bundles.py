# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Every bundle that ships part of the ``agents`` package carries what its
modules import from the package.

One bundle copies a subset of ``agentic_digital_thread/agents`` rather than
the whole package: the figure-check Lambda (``stacks/_evaluations.py``).
Importing any module of a bundle runs
``agents/__init__.py`` first, which installs the lease write check from
``agents/lease_guard.py``: a bundle that stages the one without the other
fails at its first import. Pinned here, for every bundle as it is really
built (the staged tree):

* every ``agents`` module a staged module imports at import time, and every
  package ``__init__`` above it, is staged too; so is every ``agents`` module
  the bundle's own entrypoints (a handler, a custom-resource script) import;
* every ``agents`` package the built artifacts carry is one of the bundles
  checked, found in the artifacts rather than listed by hand;
* these are the only partial copies: every Dockerfile copies the whole
  package, and the only other synth code that names a file under ``agents/``
  copies or loads a single module outside the package, which imports nothing
  from it at module level.

Offline: file copies, ASTs.
"""
from __future__ import annotations

import ast
import functools
import re
import subprocess
from pathlib import Path

import aws_cdk as cdk
import pytest

REPO = Path(__file__).resolve().parents[1]
APP = REPO / "agentic_digital_thread"
from stacks._evaluations import stage_figure_check  # noqa: E402


# ── the bundles, as they are built ─────────────────────────────────────────

def _figure_check(tmp_path: Path) -> Path:
    return Path(stage_figure_check(cdk.App(outdir=str(tmp_path / "cdk.out"))))


#: Bundle -> how to build it -> the directory that holds its ``agents/``.
BUNDLES = {
    "figure-check Lambda": _figure_check,
}


# ── import closure ─────────────────────────────────────────────────────────

def _module(rel: Path) -> str:
    parts = list(rel.with_suffix("").parts)
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _is_module(name: str) -> bool:
    """Whether *name* is a module of the package in the checkout."""
    path = APP.joinpath(*name.split("."))
    return path.with_suffix(".py").is_file() or (path / "__init__.py").is_file()


def _optional(node: ast.Try) -> bool:
    caught = {getattr(h.type, "id", None) for h in node.handlers}
    return bool(caught & {"ImportError", "ModuleNotFoundError"})


def _import_time(body: list):
    """The import statements that can run when the module is imported or run
    as a script: every block the module itself executes (``if``, except an
    ``if TYPE_CHECKING`` body; loops; ``with``; ``match``; class bodies;
    ``try`` and its handlers, except a ``try`` that falls back on an
    ImportError). Function and lambda bodies run only when called, so they
    are skipped."""
    for node in body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            yield node
        elif isinstance(node, ast.If):
            if "TYPE_CHECKING" not in ast.unparse(node.test):
                yield from _import_time(node.body)
            yield from _import_time(node.orelse)
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
            yield from _import_time(node.body + node.orelse)
        elif isinstance(node, (ast.With, ast.AsyncWith, ast.ClassDef)):
            yield from _import_time(node.body)
        elif isinstance(node, ast.Match):
            for case in node.cases:
                yield from _import_time(case.body)
        elif isinstance(node, (ast.Try, getattr(ast, "TryStar", ast.Try))) and not _optional(node):
            handlers = [stmt for h in node.handlers for stmt in h.body]
            yield from _import_time(node.body + handlers + node.orelse + node.finalbody)


def _package_imports(module: str, is_package: bool, tree: ast.Module) -> set:
    """The ``agents`` modules *module* needs at import time."""
    needed: set = set()
    for node in _import_time(tree.body):
        if isinstance(node, ast.Import):
            needed |= {a.name for a in node.names if a.name.split(".")[0] == "agents"}
            continue
        if node.level:
            base = module.split(".")[: len(module.split(".")) - node.level + (1 if is_package else 0)]
            target = ".".join(base + ([node.module] if node.module else []))
        else:
            target = node.module or ""
        if target.split(".")[0] != "agents":
            continue
        needed.add(target)
        needed |= {f"{target}.{a.name}" for a in node.names if _is_module(f"{target}.{a.name}")}
    return needed


def _missing(root: Path) -> list:
    staged = {}
    for path in sorted((root / "agents").rglob("*.py")):
        rel = path.relative_to(root)
        staged[_module(rel)] = path
    missing = []
    for module, path in staged.items():
        parents = {".".join(module.split(".")[:i]) for i in range(1, len(module.split(".")))}
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for name in sorted(parents | _package_imports(module, path.name == "__init__.py", tree)):
            if name not in staged:
                missing.append(f"{module} needs {name}")
    # The bundle's own entrypoints (a handler, a CR script) import the package
    # by absolute name; what they need must be staged too.
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root)
        if rel.parts[0] == "agents":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in _import_time(tree.body):
            if isinstance(node, ast.ImportFrom) and node.level:
                continue
            for name in sorted(_package_imports("", False, ast.Module(body=[node], type_ignores=[]))):
                if name not in staged:
                    missing.append(f"{rel} needs {name}")
    return missing


@pytest.mark.parametrize("bundle", sorted(BUNDLES))
def test_a_partial_bundle_carries_what_its_modules_import_from_the_package(tmp_path, bundle):
    root = BUNDLES[bundle](tmp_path)
    assert (root / "agents" / "__init__.py").is_file(), f"{bundle}: no agents/__init__.py"
    assert _missing(root) == [], bundle


def _package_roots(tree: Path) -> list:
    """Every directory under *tree* that holds an ``agents`` package."""
    return sorted(str(p.parent.parent.relative_to(tree)) for p in tree.rglob("agents/__init__.py"))


def test_every_agents_package_a_built_bundle_carries_is_one_checked_above(tmp_path):
    """Found in the built artifacts themselves, not listed by hand: a new
    ``agents`` root added to the figure-check stage fails here until it joins
    :data:`BUNDLES` (and so the closure check)."""
    stage = _figure_check(tmp_path / "fc")
    assert _package_roots(stage) == ["."]


_RUNS_AT_IMPORT = '''
import agents.a
if TYPE_CHECKING:
    import agents.hidden_typing
else:
    import agents.b
for _ in ():
    import agents.c
while False:
    import agents.d
with open(__file__):
    import agents.e
class K:
    import agents.f
match 1:
    case 1:
        import agents.g
try:
    import agents.h
except ValueError:
    import agents.i
finally:
    import agents.j
try:
    import agents.hidden_optional
except ImportError:
    pass
def later():
    import agents.hidden_function
'''


def test_the_walker_reads_every_block_a_module_runs_and_no_function_body():
    names = {a.name for n in _import_time(ast.parse(_RUNS_AT_IMPORT).body) for a in n.names}
    assert names == {f"agents.{c}" for c in "abcdefghij"}


def test_an_entrypoint_outside_the_package_counts_toward_the_closure(tmp_path):
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "handler.py").write_text("from agents.lease_guard import install\n",
                                         encoding="utf-8")
    assert _missing(tmp_path) == ["handler.py needs agents.lease_guard"]
    (tmp_path / "agents" / "lease_guard.py").write_text("", encoding="utf-8")
    assert _missing(tmp_path) == []


def test_every_bundle_installs_the_lease_write_check():
    """The package's ``__init__`` imports the lease guard unconditionally, so a
    bundle that ships the package can never run without it (it fails at its
    first import instead)."""
    tree = ast.parse((APP / "agents" / "__init__.py").read_text(encoding="utf-8"))
    imports = [(n.level, n.module) for n in tree.body if isinstance(n, ast.ImportFrom)]
    assert (1, "lease_guard") in imports
    assert not [n for n in tree.body if isinstance(n, ast.Try)]


# ── these are the only partial copies ──────────────────────────────────────

_NAMES_AGENTS = re.compile(r"""["']agentic_digital_thread["']\s*[,/]\s*["']agents["']""")

#: Synth and packaging code that names a path under ``agents/``, and why.
_REFERENCES = {
    "stacks/_evaluations.py": "package bundle (figure check)",
    "stacks/_model_allowlist.py": "single modules loaded by path at synth",
    "stacks/_quick_gateway_auth.py": "a single module staged outside the package",
    "stacks/sfc_stack.py": "single modules loaded at synth or staged outside the package",
    "scripts/eval_feedback_exploration.py": "a JSON file, no module",
}
#: The single modules those copy or load, as named in their source.
_SINGLE_MODULES = {
    "stacks/_model_allowlist.py": ["model_catalog.py", "runtime_env_contract.py"],
    "stacks/_quick_gateway_auth.py": ["auth/identity_claim.py"],
    "stacks/sfc_stack.py": ["demo/sfc_config.py", "demo/sitewise_tree.py"],
}


@functools.lru_cache(maxsize=None)
def _tracked(root: Path) -> frozenset:
    """Every path git tracks under *root*: build output a local package run
    leaves in the tree (gitignored ``cdk.out*``, ``dist/``, ``node_modules``)
    holds copies of the source, which are not source."""
    out = subprocess.run(["git", "-C", str(root), "ls-files", "-z"],
                         check=True, capture_output=True).stdout.decode("utf-8")
    return frozenset(root / p for p in out.split("\0") if p)


def _code_files(root: Path = REPO):
    tracked = _tracked(root)
    for pattern in ("stacks/*.py", "scripts/*.py", "app.py",
                    "lambda-*/**/*.py", "lambda_/**/*.py"):
        for path in sorted(root.glob(pattern)):
            if path in tracked and "tests" not in path.relative_to(root).parts:
                yield path


def _partial_dockerfiles(root: Path) -> list:
    """The tracked Dockerfiles under *root* that copy part of ``agents/``, relative."""
    partial = []
    for dockerfile in sorted(p for p in _tracked(root) if p.name.startswith("Dockerfile")):
        for line in dockerfile.read_text(encoding="utf-8").splitlines():
            words = line.split()
            if len(words) >= 3 and words[0] == "COPY" and words[1].startswith("agents/") \
                    and words[1:3] != ["agents/", "agents/"]:
                partial.append(str(dockerfile.relative_to(root)))
                break
    return partial


def test_every_reference_into_the_package_from_synth_code_is_known():
    found = sorted({str(p.relative_to(REPO)) for p in _code_files()
                    if _NAMES_AGENTS.search(p.read_text(encoding="utf-8"))})
    assert found == sorted(_REFERENCES)


def _path_parts(node: ast.BinOp) -> list:
    """The string parts of a ``root / "a" / "b"`` chain, in order."""
    parts = []
    while isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        if not (isinstance(node.right, ast.Constant) and isinstance(node.right.value, str)):
            return []
        parts.append(node.right.value)
        node = node.left
    return parts[::-1]


def _named_modules(source: Path) -> list:
    """The ``.py`` paths an ``os.path.join(..., "agentic_digital_thread",
    "agents", ...)``, or a ``root / "agentic_digital_thread" / "agents" / ...``
    path, in *source* names."""
    out = []
    for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call) and ast.unparse(node.func) == "os.path.join":
            parts = [a.value for a in node.args if isinstance(a, ast.Constant)]
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            parts = _path_parts(node)
        else:
            continue
        if parts[:2] == ["agentic_digital_thread", "agents"] and parts[-1].endswith(".py"):
            out.append("/".join(parts[2:]))
    return sorted(out)


@pytest.mark.parametrize("source", sorted(_SINGLE_MODULES))
def test_a_single_module_copied_outside_the_package_imports_nothing_from_it(source):
    assert _named_modules(REPO / source) == sorted(_SINGLE_MODULES[source])
    for rel in _SINGLE_MODULES[source]:
        path = APP / "agents" / rel
        tree = ast.parse(path.read_text(encoding="utf-8"))
        needed = _package_imports(_module(Path("agents") / rel), False, tree)
        relative = [n for n in _import_time(tree.body) if isinstance(n, ast.ImportFrom) and n.level]
        assert (sorted(needed), relative) == ([], []), rel


def test_every_dockerfile_copies_the_whole_package():
    assert _partial_dockerfiles(REPO) == []


def test_the_repo_scans_read_only_what_git_tracks(tmp_path):
    """A local package run leaves its build output in the tree
    (``cdk.out``, gitignored), holding copies of the
    source and of the Dockerfiles. The scans above read what git
    tracks, so a copy in build output is never counted as source."""
    def git(*args):
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)

    git("init", "-q")
    (tmp_path / ".gitignore").write_text("cdk.out/\n", encoding="utf-8")
    (tmp_path / "lambda-x").mkdir()
    (tmp_path / "lambda-x" / "synth.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "Dockerfile.part").write_text("COPY agents/one.py agents/\n", encoding="utf-8")
    stray = tmp_path / "lambda-x" / "cdk.out" / "asset.1"
    stray.mkdir(parents=True)
    (stray / "Dockerfile.part").write_text("COPY agents/one.py agents/\n", encoding="utf-8")
    (stray / "copied.py").write_text("x = 1\n", encoding="utf-8")
    git("add", "-A")
    assert _partial_dockerfiles(tmp_path) == ["Dockerfile.part"]
    assert [str(p.relative_to(tmp_path)) for p in _code_files(tmp_path)] == ["lambda-x/synth.py"]
