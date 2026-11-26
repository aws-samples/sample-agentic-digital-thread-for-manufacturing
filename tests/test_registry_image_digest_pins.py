# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Every registry image the CDK app runs is pinned by digest, from one source.

A tag such as ``highbyte/intelligence-hub:4.5.3`` or ``busybox:1.36`` can move:
the registry may serve different bytes for it tomorrow, and ECS pulls the
image again at every task start, so a moved tag silently changes what runs
with the task's role. ``tests/test_dockerfile_base_image_pins.py`` covers
Dockerfile ``FROM`` lines; this module covers the images the CDK code names
directly.

The scan reads the code, not just its strings: the ref given to every
``from_registry`` call (``ecs.ContainerImage``, ``DockerImage``) is resolved,
through module constants, and must carry a digest; a ref computed at run time
is refused unless ``_DYNAMIC_ALLOWED`` names its function with the reason the
ref it receives is still a digest ref; a ``BundlingOptions(image=...)`` must be
such a call; every digest pin record must have the right keys and digest and
platform shapes; and any other string constant naming an image by tag fails.

The HighByte hub task's two images (the hub and its settings-seed init
container) are pinned ONCE, in ``stacks/_external_images.py``. The ordinary
``deploy.sh --highbyte`` stack runs ``<repository>@<linux/amd64 digest>``
from there.

Offline: source parsing and stdlib-only modules loaded by path; no synth.
"""
from __future__ import annotations

import ast
import importlib.util
import re
from collections import Counter
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

#: A container image reference with a tag or a digest:
#: ``[host[:port]/]path[:tag][@sha256:<64 hex>]``.
_IMAGE_REF = re.compile(
    r"^(?:(?P<host>[a-z0-9-]+(?:\.[a-z0-9-]+)+(?::\d+)?)/)?"
    r"(?P<path>[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*)"
    r"(?::(?P<tag>[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}))?"
    r"(?P<digest>@sha256:[0-9a-f]{64})?$")
#: A ``sha256`` digest, and an OCI platform (``os/arch[/variant]``).
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_PLATFORM = re.compile(r"^[a-z0-9]+/[a-z0-9]+(?:/[a-z0-9]+)?$")

#: A dict literal is a digest pin record when it has a ``source`` key and one
#: of these. A VALID record's ``source`` tag is provenance (the digests beside
#: it are what runs); an invalid record is reported and exempts nothing.
_PIN_RECORD_MARKERS = frozenset({"index_digest", "platform_digests",
                                 "platform_config_digests"})
_PIN_RECORD_KEYS = frozenset({"source"}) | _PIN_RECORD_MARKERS

#: Registry-image calls whose ref is neither a literal nor a module constant,
#: by (file, enclosing function) -> (count, why the ref it receives is still a
#: digest ref). Any other computed ref fails the scan. This list only SHRINKS:
#: a new site fails, and a removed one fails until its entry is lowered.
_DYNAMIC_ALLOWED: dict[tuple[str, str], tuple[int, str]] = {
    ("stacks/highbyte_opcua_stack.py", "_registry_image"): (
        1, "the demo hub task's synth-time ref: HIGHBYTE_IMAGE or SETTINGS_SEED_IMAGE "
           "(digest refs from stacks/_external_images.py, held below), or an explicit "
           "deployer override in the highbyte_image / settings_seed_image context, which "
           "_highbyte_options.images_from_node refuses unless it ends in a digest"),
    ("stacks/highbyte_opcua_stack.py", "_container_image"): (
        1, "the workshop's deploy-time image parameter, whose AllowedPattern "
           "(_highbyte_options.IMAGE_PARAMETER_PATTERN) only accepts a digest ref"),
}

#: Calls that take a registry image ref, and the keyword the ref may ride in.
_REGISTRY_CALLS = {"from_registry": "name"}
#: Options whose ``image`` must itself be a registry-image call (by digest).
_BUNDLING_OPTIONS = frozenset({"BundlingOptions"})


def _load(name: str, relpath: str):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / relpath)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pins():
    return _load("adt_test_external_image_pins", "stacks/_external_images.py")


def _cdk_app_sources() -> list[Path]:
    return sorted((REPO_ROOT / "stacks").glob("*.py")) + [REPO_ROOT / "app.py"]


def _const_str(node: ast.AST, consts: dict[str, str]) -> "str | None":
    """The string value of *node* when it is static: a literal, a module
    constant, ``+`` of static parts, ``"x" * <int>``, or an f-string with no
    placeholder. ``None`` for anything computed at run time."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return consts.get(node.id)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _const_str(node.left, consts), _const_str(node.right, consts)
        return left + right if left is not None and right is not None else None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult) \
            and isinstance(node.right, ast.Constant) and isinstance(node.right.value, int):
        left = _const_str(node.left, consts)
        return left * node.right.value if left is not None else None
    if isinstance(node, ast.JoinedStr) and all(isinstance(v, ast.Constant) for v in node.values):
        return "".join(str(v.value) for v in node.values)
    return None


def _module_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level ``NAME = <static string>`` bindings, in source order (a
    later non-static rebinding drops the name)."""
    consts: dict[str, str] = {}
    for stmt in tree.body:
        targets, value = [], None
        if isinstance(stmt, ast.Assign):
            targets, value = stmt.targets, stmt.value
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            targets, value = [stmt.target], stmt.value
        resolved = _const_str(value, consts) if value is not None else None
        for t in targets:
            if not isinstance(t, ast.Name):
                continue
            if resolved is None:
                consts.pop(t.id, None)
            else:
                consts[t.id] = resolved
    return consts


def _owners(tree: ast.AST) -> dict[int, str]:
    """id(node) -> name of the innermost function holding it."""
    out: dict[int, str] = {}
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for n in ast.walk(fn):
                out[id(n)] = fn.name
    return out


def _call_name(call: ast.Call) -> str:
    f = call.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return ""


def _is_pinned(ref: str) -> bool:
    match = _IMAGE_REF.match(ref)
    return bool(match and match["digest"])


def record_problems(record: object, where: str = "pin record") -> list[str]:
    """What is wrong with a digest pin record (``stacks/_external_images.py``
    shape): known keys only; ``source`` the tag the digests were read from;
    ``index_digest`` a sha256 digest; ``platform_digests`` a non-empty map of
    OCI platforms to sha256 digests; ``platform_config_digests`` (optional)
    the same shape, for platforms ``platform_digests`` names."""
    if not isinstance(record, dict):
        return [f"{where}: not a literal dict"]
    problems = [f"{where}: unknown key {k!r}" for k in sorted(set(record) - _PIN_RECORD_KEYS)]
    source = record.get("source")
    match = _IMAGE_REF.match(source) if isinstance(source, str) else None
    if not (match and match["tag"]):
        problems.append(f"{where}: source {source!r} is not an image ref with a tag")
    index = record.get("index_digest")
    if not (isinstance(index, str) and _DIGEST.match(index)):
        problems.append(f"{where}: index_digest {index!r} is not a sha256 digest")
    platforms = record.get("platform_digests")
    if not (isinstance(platforms, dict) and platforms):
        problems.append(f"{where}: platform_digests is missing or empty")
        platforms = {}
    for label, mapping in (("platform_digests", platforms),
                           ("platform_config_digests", record.get("platform_config_digests", {}))):
        if not isinstance(mapping, dict):
            problems.append(f"{where}: {label} is not a map")
            continue
        for platform, digest in mapping.items():
            if not (isinstance(platform, str) and _PLATFORM.match(platform)):
                problems.append(f"{where}: {label} platform {platform!r} is not os/arch[/variant]")
            if not (isinstance(digest, str) and _DIGEST.match(digest)):
                problems.append(f"{where}: {label}[{platform!r}] {digest!r} is not a sha256 digest")
            if label == "platform_config_digests" and platform not in platforms:
                problems.append(f"{where}: {label} names {platform!r}, which "
                                "platform_digests does not")
    return problems


def _pin_records(tree: ast.AST) -> list[ast.Dict]:
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            keys = {k.value for k in node.keys if isinstance(k, ast.Constant)}
            if "source" in keys and keys & _PIN_RECORD_MARKERS:
                out.append(node)
    return out


def _record_value(record: ast.Dict) -> object:
    """The record as a Python value, or ``None`` when it is not static."""
    try:
        return ast.literal_eval(record)
    except ValueError:
        pass
    keys = [_const_str(k, {}) if k is not None else None for k in record.keys]
    out: dict = {}
    for key, value in zip(keys, record.values):
        if key is None:
            return None
        if isinstance(value, ast.Dict):
            inner = _record_value(value)
            if inner is None:
                return None
            out[key] = inner
        else:
            static = _const_str(value, {})
            if static is None:
                return None
            out[key] = static
    return out


def scan(source: str, relpath: str) -> tuple[list[str], Counter]:
    """``(problems, dynamic)`` for one CDK source file.

    * every ``from_registry`` ref is resolved (a literal, a module constant or
      a static expression of them) and must carry a digest; a ref computed at
      run time is counted in ``dynamic`` under ``(relpath, enclosing
      function)`` instead;
    * every ``BundlingOptions(image=...)`` must be a registry-image call;
    * every pin record must be valid (:func:`record_problems`);
    * every other string constant naming an image with a tag must carry a
      digest (a bare ``name:tag`` with no ``/`` is too common a shape to tell
      from ordinary text there; inside a registry call it is checked above),
      except a VALID pin record's ``source``."""
    tree = ast.parse(source)
    consts, owners = _module_constants(tree), _owners(tree)
    problems: list[str] = []
    dynamic: Counter = Counter()
    exempt: set[int] = set()
    for record in _pin_records(tree):
        found = record_problems(_record_value(record), f"{relpath}:{record.lineno}: pin record")
        problems += found
        if not found:
            exempt.update(id(v) for k, v in zip(record.keys, record.values)
                          if isinstance(k, ast.Constant) and k.value == "source")
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        where = f"{relpath}:{node.lineno}"
        if name in _REGISTRY_CALLS:
            keyword = _REGISTRY_CALLS[name]
            arg = node.args[0] if node.args else next(
                (k.value for k in node.keywords if k.arg == keyword), None)
            ref = _const_str(arg, consts) if arg is not None else None
            if ref is None:
                dynamic[(relpath, owners.get(id(node), "<module>"))] += 1
            elif not _is_pinned(ref):
                problems.append(f"{where}: {name}({ref!r}) runs a registry image by tag")
        elif name in _BUNDLING_OPTIONS:
            image = next((k.value for k in node.keywords if k.arg == "image"), None)
            if image is not None and not (isinstance(image, ast.Call)
                                          and _call_name(image) in _REGISTRY_CALLS):
                problems.append(f"{where}: {name}(image=...) is not a registry image by digest")
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        if id(node) in exempt:
            continue
        match = _IMAGE_REF.match(node.value)
        if match and match["tag"] and not match["digest"] and "/" in node.value:
            problems.append(f"{relpath}:{node.lineno}: {node.value!r} names a registry image by tag")
    return problems, dynamic


def rejections(source: str, relpath: str = "stacks/example.py") -> list[str]:
    """Every reason the scan refuses *source*: its problems, and each computed
    registry ref beyond what ``_DYNAMIC_ALLOWED`` grants its function."""
    problems, dynamic = scan(source, relpath)
    for site, count in sorted(dynamic.items()):
        allowed = _DYNAMIC_ALLOWED.get(site, (0, ""))[0]
        if count > allowed:
            problems.append(f"{site[0]} {site[1]}: {count - allowed} registry ref(s) computed "
                            "at run time; resolve them to a digest-pinned constant")
    return problems


_ZEROS = "sha256:" + "0" * 64


@pytest.mark.parametrize("src", [
    'ecs.ContainerImage.from_registry("busybox:latest")',
    'ecs.ContainerImage.from_registry(f"vendor/image:{tag}")',
    'P = {"source": "vendor/image:latest", "index_digest": "not-a-digest"}',
    'IMG = "vendor/image:1.2"\nDockerImage.from_registry(IMG)',
    'DockerImage.from_registry(name="busybox:1.36")',
    'def make(tag):\n    return ecs.ContainerImage.from_registry("vendor/image:" + tag)',
    'BundlingOptions(image=lambda_.Runtime.PYTHON_3_12.bundling_image)',
    'P = {"source": "vendor/image:1", "index_digest": "%s",\n'
    '     "platform_digests": {"amd64": "%s"}}' % (_ZEROS, _ZEROS),
    'P = {"source": "vendor/image:1", "index_digest": "%s",\n'
    '     "platform_digests": {"linux/amd64": "%s"},\n'
    '     "platform_config_digests": {"linux/arm64": "%s"}}' % (_ZEROS, _ZEROS, _ZEROS),
    'P = {"source": "vendor/image:1", "index_digest": "%s", "platform_digests": {},\n'
    '     "extra": "x"}' % _ZEROS,
], ids=["bare-tag", "f-string", "bad-pin-record", "constant-tag", "keyword-tag",
        "computed-in-function", "bundling-default", "bad-platform",
        "config-for-unpinned-platform", "empty-platforms-unknown-key"])
def test_the_scan_rejects_mutable_and_unresolvable_refs(src):
    """Negative controls: each must be refused. The first three are the
    examples the previous constant-only scan let through."""
    assert rejections(src), src


def test_the_scan_accepts_digest_refs_and_valid_pin_records():
    src = f'''
PINNED = ("public.ecr.aws/docker/library/python:3.12-slim@"
          "{_ZEROS}")
DockerImage.from_registry(PINNED)
BundlingOptions(image=DockerImage.from_registry(PINNED))
ecs.ContainerImage.from_registry(name="vendor/image@{_ZEROS}")
PINS = {{"hub": {{"source": "vendor/image:4.5.3", "index_digest": "{_ZEROS}",
                 "platform_digests": {{"linux/amd64": "{_ZEROS}"}},
                 "platform_config_digests": {{"linux/amd64": "{_ZEROS}"}}}}}}
D = "orders.status"
E = "arn:aws:iam::111122223333:role/x"
F = "/usr/local/highbyte/appData"
'''
    assert rejections(src) == []


def test_every_registry_image_the_cdk_app_names_is_digest_pinned():
    refused = []
    for path in _cdk_app_sources():
        rel = path.relative_to(REPO_ROOT).as_posix()
        refused += rejections(path.read_text(encoding="utf-8"), rel)
    assert not refused, (
        "registry image not pinned by digest (a tag can move): " + "; ".join(refused)
        + ". Pin it by digest (the HighByte hub task's images live in "
        "stacks/_external_images.py).")


def test_the_dynamic_allowlist_only_shrinks():
    found: Counter = Counter()
    for path in _cdk_app_sources():
        found += scan(path.read_text(encoding="utf-8"), path.relative_to(REPO_ROOT).as_posix())[1]
    stale = {site: (count, found.get(site, 0)) for site, (count, _why) in _DYNAMIC_ALLOWED.items()
             if found.get(site, 0) < count}
    assert not stale, f"allowlisted computed refs fixed or removed {stale} (allowed, found); lower them"


def test_the_allowlisted_computed_refs_only_receive_digest_refs():
    """The reasons in _DYNAMIC_ALLOWED hold: the demo default refs are digest
    refs, a deployer's synth-time override is refused unless it is one, and the
    workshop parameter's pattern refuses a tag."""
    from stacks import _highbyte_options as opts
    from stacks import highbyte_opcua_stack as hb
    assert _is_pinned(hb.HIGHBYTE_IMAGE) and _is_pinned(hb.SETTINGS_SEED_IMAGE)

    class _Node:
        def __init__(self, **context):
            self._context = context

        def try_get_context(self, key):
            return self._context.get(key)

    defaults = (hb.HIGHBYTE_IMAGE, hb.SETTINGS_SEED_IMAGE)
    assert opts.images_from_node(_Node(), *defaults) == defaults
    for key in (opts.HUB_IMAGE_KEY, opts.SETTINGS_SEED_IMAGE_KEY):
        for tagged in ("vendor/hub:latest", "busybox:latest", "vendor/hub:1.2"):
            with pytest.raises(ValueError, match="pinned by digest"):
                opts.images_from_node(_Node(**{key: tagged}), *defaults)
    pinned = "vendor/hub:1.2@" + _ZEROS
    assert opts.images_from_node(_Node(**{opts.HUB_IMAGE_KEY: pinned,
                                          opts.SETTINGS_SEED_IMAGE_KEY: pinned}), *defaults) \
        == (pinned, pinned)
    assert re.fullmatch(opts.IMAGE_PARAMETER_PATTERN, "highbyte/intelligence-hub:4.5.3") is None
    assert re.fullmatch(opts.IMAGE_PARAMETER_PATTERN, hb.HIGHBYTE_IMAGE)


def test_the_shared_pins_are_valid_pin_records():
    for name, record in _pins().EXTERNAL_IMAGES.items():
        assert record_problems(record, name) == []


def test_the_hub_task_runs_the_shared_pins_by_platform_digest():
    from stacks import highbyte_opcua_stack as hb
    pins = _pins()
    platform = pins.HUB_TASK_PLATFORM
    assert platform == "linux/amd64"
    for name, ref in (("highbyte-hub", hb.HIGHBYTE_IMAGE),
                      ("settings-seed", hb.SETTINGS_SEED_IMAGE)):
        spec = pins.EXTERNAL_IMAGES[name]
        repo = spec["source"].rsplit(":", 1)[0]
        assert ref == f"{repo}@{spec['platform_digests'][platform]}", name
        assert _is_pinned(ref)


def test_the_hub_release_stays_readable_beside_its_digest():
    from stacks import highbyte_opcua_stack as hb
    assert hb.HIGHBYTE_IMAGE_SOURCE == _pins().EXTERNAL_IMAGES["highbyte-hub"]["source"]
    assert re.fullmatch(r"highbyte/intelligence-hub:\d+\.\d+\.\d+", hb.HIGHBYTE_IMAGE_SOURCE)
