#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Check frozen dependency licenses offline; refresh metadata only on request."""

from __future__ import annotations

import argparse
from datetime import date
from email.parser import Parser
import hashlib
from io import BytesIO
import json
from pathlib import Path
import platform
import re
import sys
from typing import Any
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = Path("config/dependency-license-policy.json")
INVENTORY_PATH = Path("config/dependency-licenses.json")
IGNORED_PARTS = {
    ".git",
    ".kiro",
    ".venv",
    "node_modules",
    "cdk.out",
    "cdk.out.engine",
}
NAME_RE = re.compile(r"[-_.]+")
PYTHON_REQUIREMENT_RE = re.compile(r"^([A-Za-z0-9_.-]+)==([^ ;\\]+)(?:\s*(?:;|\\|$))")
SPDX_TOKEN_RE = re.compile(r"\s*(\(|\)|AND\b|OR\b|WITH\b|[A-Za-z0-9][A-Za-z0-9.+-]*)")
CLASSIFIER_LICENSES = {
    "License :: OSI Approved :: Apache Software License": "Apache-2.0",
    "License :: OSI Approved :: ISC License (ISCL)": "ISC",
    "License :: OSI Approved :: MIT License": "MIT",
    "License :: OSI Approved :: Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
    "License :: OSI Approved :: Python Software Foundation License": "PSF-2.0",
    "License :: OSI Approved :: The Unlicense (Unlicense)": "Unlicense",
}
LICENSE_ALIASES = {
    "3-Clause BSD License": "BSD-3-Clause",
    "Apache 2.0": "Apache-2.0",
    "Apache License 2.0": "Apache-2.0",
    "BSD-2-Clause": "BSD-2-Clause",
    "BSD-3-Clause": "BSD-3-Clause",
    "ISC": "ISC",
    "MIT": "MIT",
    "MIT License": "MIT",
    "MIT license": "MIT",
    "MPL-2.0": "MPL-2.0",
    "PSF-2.0": "PSF-2.0",
}


class GateError(ValueError):
    """A deterministic dependency policy violation."""


def canonical_name(name: str) -> str:
    return NAME_RE.sub("-", name).lower()


def package_key(ecosystem: str, name: str, version: str) -> str:
    return f"{ecosystem}:{name}=={version}"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GateError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise GateError(f"JSON root must be an object: {path}")
    return value


def _tokenize_spdx(expression: str) -> list[str]:
    tokens: list[str] = []
    position = 0
    while position < len(expression):
        match = SPDX_TOKEN_RE.match(expression, position)
        if not match:
            raise GateError(f"invalid SPDX expression: {expression!r}")
        tokens.append(match.group(1))
        position = match.end()
    if not tokens:
        raise GateError("empty SPDX expression")
    return tokens


class _SpdxParser:
    def __init__(self, expression: str) -> None:
        self.expression = expression
        self.tokens = _tokenize_spdx(expression)
        self.position = 0

    def parse(self) -> tuple[Any, ...]:
        result = self._or_expression()
        if self.position != len(self.tokens):
            raise GateError(f"invalid SPDX expression: {self.expression!r}")
        return result

    def _or_expression(self) -> tuple[Any, ...]:
        left = self._and_expression()
        while self._accept("OR"):
            left = ("OR", left, self._and_expression())
        return left

    def _and_expression(self) -> tuple[Any, ...]:
        left = self._with_expression()
        while self._accept("AND"):
            left = ("AND", left, self._with_expression())
        return left

    def _with_expression(self) -> tuple[Any, ...]:
        left = self._primary()
        if self._accept("WITH"):
            if left[0] != "LICENSE":
                raise GateError(f"invalid WITH operand: {self.expression!r}")
            left = ("WITH", left, self._identifier())
        return left

    def _primary(self) -> tuple[Any, ...]:
        if self._accept("("):
            result = self._or_expression()
            if not self._accept(")"):
                raise GateError(f"unclosed SPDX group: {self.expression!r}")
            return result
        return ("LICENSE", self._identifier())

    def _identifier(self) -> str:
        if self.position >= len(self.tokens):
            raise GateError(f"missing SPDX identifier: {self.expression!r}")
        token = self.tokens[self.position]
        if token in {"(", ")", "AND", "OR", "WITH"}:
            raise GateError(f"missing SPDX identifier: {self.expression!r}")
        self.position += 1
        return token

    def _accept(self, token: str) -> bool:
        if self.position < len(self.tokens) and self.tokens[self.position] == token:
            self.position += 1
            return True
        return False


def parse_spdx(expression: str) -> tuple[Any, ...]:
    return _SpdxParser(expression).parse()


def _spdx_allowed(node: tuple[Any, ...], approved: set[str], denied: set[str]) -> bool:
    kind = node[0]
    if kind == "LICENSE":
        identifier = node[1]
        if identifier in denied or identifier not in approved:
            return False
        return True
    if kind == "AND":
        return _spdx_allowed(node[1], approved, denied) and _spdx_allowed(
            node[2], approved, denied
        )
    if kind == "OR":
        return _spdx_allowed(node[1], approved, denied) and _spdx_allowed(
            node[2], approved, denied
        )
    return False  # WITH requires an exact package exception.


def _is_ignored(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    return any(
        part in IGNORED_PARTS or part.startswith("cdk.out") or part.startswith(".venv")
        for part in relative.parts
    )


def _discover(root: Path, *patterns: str) -> list[str]:
    found = {
        path.relative_to(root).as_posix()
        for pattern in patterns
        for path in root.rglob(pattern)
        if path.is_file() and not _is_ignored(path, root)
    }
    return sorted(found)


def discover_lock_paths(root: Path) -> tuple[list[str], list[str]]:
    """Every Python requirements surface (``*.lock`` and ``requirements*.txt``
    / ``requirements*.in`` at any depth) and every ``package-lock.json``.

    A Python file is either a lock (``python_locks``: exactly pinned, every
    pin license-checked) or a declared manifest (``python_manifests``: a
    compile input or local-only tooling file that no build installs); one
    that is neither fails the check, so a bundled requirements file can never
    be skipped silently.
    """
    python_paths = _discover(root, "*.lock", "requirements*.txt", "requirements*.in")
    return python_paths, _discover(root, "package-lock.json")


def _python_manifests(policy: dict[str, Any]) -> dict[str, str]:
    manifests = policy.get("python_manifests", {})
    if not isinstance(manifests, dict):
        raise GateError("policy python_manifests must be an object")
    for path, reason in manifests.items():
        if not isinstance(reason, str) or not reason.strip():
            raise GateError(f"python manifest needs a reason: {path}")
        if path in policy.get("python_locks", []):
            raise GateError(f"python manifest is also a lock: {path}")
    return manifests


#: ``-r <file>`` / ``--requirement <file>``, with the word before it (so
#: ``cp -r`` and ``jq -r`` are not read as an install).
INSTALL_REF_RE = re.compile(
    r"""(?:(\S+)[ \t]+)?(?<![\w-])(?:-r|--requirement)(?:[ \t]+|=)['"]?([^\s'"\\]+)"""
)
NOT_INSTALL_WORDS = {"cp", "rm", "jq", "chmod", "chown", "grep", "ls", "scp", "rsync"}
#: Dockerfile ``COPY [--opt ...] <src> <dest>`` with one source.
COPY_RE = re.compile(r"^\s*COPY\s+(?:--\S+\s+)*(\S+)\s+(\S+)\s*$", re.M)
#: Module-level string constants (``LOCK_NAME = "x.lock"``) for f-string refs.
CONSTANT_RE = re.compile(r"""^(\w+)\s*(?::\s*\w+\s*)?=\s*["']([^"'{}]+)["']\s*$""", re.M)


def _resolved_refs(text: str, dockerfile: bool) -> list[str]:
    """Each installed path in *text*: a Dockerfile COPY destination is
    replaced by its source, and ``{NAME}`` by a module string constant."""
    copies: dict[str, str] = {}
    if dockerfile:
        for src, dest in COPY_RE.findall(text):
            copies[dest] = copies[dest.rsplit("/", 1)[-1]] = src
    constants = dict(CONSTANT_RE.findall(text))
    refs = []
    for word, ref in INSTALL_REF_RE.findall(text):
        if word.lstrip("f\"'(").lower() in NOT_INSTALL_WORDS:
            continue
        ref = copies.get(ref) or copies.get(ref.rsplit("/", 1)[-1]) or ref
        refs.append(re.sub(r"\{(\w+)\}", lambda m: constants.get(m.group(1), m.group(0)), ref))
    return refs


def _install_sources(root: Path) -> list[tuple[str, str]]:
    """(source file, resolved path) for every requirements file a
    Dockerfile or a stacks/ bundling command installs with ``-r``."""
    sources = [p for p in root.rglob("Dockerfile*") if not _is_ignored(p, root)]
    sources += sorted((root / "stacks").glob("*.py")) if (root / "stacks").is_dir() else []
    refs = []
    for path in sources:
        text = path.read_text(encoding="utf-8", errors="replace")
        found = _resolved_refs(text, path.name.startswith("Dockerfile"))
        refs += [(path.relative_to(root).as_posix(), ref) for ref in found]
    return refs


def _suffix_match(ref: str, paths: list[str]) -> list[str]:
    """Paths whose last parts equal the ref's (at most two) last parts:
    ``/asset-input/x/requirements.txt`` names ``lambda-x/x/requirements.txt``."""
    parts = tuple(p for p in ref.split("/") if p and p not in {".", "asset-input", "tmp"})[-2:]
    return [p for p in paths if parts and Path(p).parts[-len(parts):] == parts]


def _check_installed_are_locks(root: Path, policy: dict[str, Any], manifests: dict[str, str]) -> None:
    """Every requirements file a build installs is declared in python_locks,
    whatever its name, and is never a manifest. A path the scan cannot
    resolve (a shell variable or an unknown constant) passes only when it
    names a ``.lock`` file, which discovery always requires to be a lock."""
    locks = list(policy.get("python_locks", []))
    for source, ref in _install_sources(root):
        where = f"{source}: -r {ref}"
        if "$" in ref or "{" in ref:
            if not ref.endswith(".lock"):
                raise GateError(f"cannot resolve an installed requirements path ({where})")
            continue
        manifest = _suffix_match(ref, list(manifests))
        if manifest:
            raise GateError(
                f"python manifest is installed by a build ({where}); "
                f"declare it in python_locks: {manifest[0]}"
            )
        if not _suffix_match(ref, locks):
            raise GateError(f"installed requirements file is not in python_locks ({where})")


MANIFEST_LINE_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?\s*(.*)$")


def _manifest_requirements(path: Path) -> list[tuple[str, str | None]]:
    """(canonical name, exact version or None) for each requirement line."""
    out = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        match = MANIFEST_LINE_RE.match(line)
        if not match:
            raise GateError(f"{path}:{number}: unreadable requirement")
        spec = match.group(2).split(";", 1)[0].strip()
        pin = re.fullmatch(r"==\s*([^\s,*]+)", spec)
        out.append((canonical_name(match.group(1)), pin.group(1) if pin else None))
    return out


def _check_manifest_licenses(
    root: Path, manifests: dict[str, str], indexed: dict[str, dict[str, Any]]
) -> None:
    """A manifest only waives the exact-pin rule: every package it names
    must be in the license-checked inventory (at its version when pinned)."""
    by_name: dict[str, set[str]] = {}
    for row in indexed.values():
        if row.get("ecosystem") == "pypi":
            by_name.setdefault(str(row.get("name")), set()).add(str(row.get("version")))
    for manifest in sorted(manifests):
        for name, version in _manifest_requirements(root / manifest):
            versions = by_name.get(name)
            if not versions or (version is not None and version not in versions):
                wanted = f"{name}=={version}" if version else name
                raise GateError(f"missing license: {manifest} names {wanted}, not in the inventory")


def _check_npm_manifests(root: Path, npm_locks: list[str]) -> None:
    """Every ``package.json`` resolves through a declared sibling lock."""
    for manifest in _discover(root, "package.json"):
        lock = str(Path(manifest).with_name("package-lock.json").as_posix())
        if lock not in npm_locks:
            raise GateError(f"package.json without a declared package-lock.json: {manifest}")


def parse_python_lock(path: Path) -> list[tuple[str, str]]:
    packages: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    in_block = False
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line or line.lstrip().startswith("#"):
            continue
        if line[0].isspace():
            if not in_block:
                raise GateError(f"{path}:{number}: orphan lock continuation")
            continue
        match = PYTHON_REQUIREMENT_RE.match(line)
        if not match:
            raise GateError(f"{path}:{number}: dependency is not exactly pinned")
        item = (canonical_name(match.group(1)), match.group(2))
        if item in seen:
            raise GateError(
                f"{path}:{number}: duplicate dependency {item[0]}=={item[1]}"
            )
        seen.add(item)
        packages.append(item)
        in_block = line.rstrip().endswith("\\")
    if not packages:
        raise GateError(f"empty Python lock: {path}")
    return packages


def _npm_name(lock_key: str) -> str | None:
    marker = "node_modules/"
    if marker not in lock_key:
        return None
    name = lock_key.rsplit(marker, 1)[1]
    if not name or ("/" in name and not name.startswith("@")):
        return None
    return name


def parse_npm_lock(path: Path) -> list[tuple[str, str, str | None]]:
    data = _read_json(path)
    if data.get("lockfileVersion") != 3 or not isinstance(data.get("packages"), dict):
        raise GateError(f"{path}: npm lockfileVersion 3 with packages is required")
    packages: list[tuple[str, str, str | None]] = []
    seen: set[tuple[str, str]] = set()
    for lock_key, metadata in data["packages"].items():
        name = _npm_name(lock_key)
        if name is None:
            continue
        if not isinstance(metadata, dict) or not isinstance(
            metadata.get("version"), str
        ):
            raise GateError(f"{path}: missing exact version for {lock_key}")
        item = (name, metadata["version"])
        if item in seen:
            continue
        seen.add(item)
        license_value = metadata.get("license")
        if license_value is not None and not isinstance(license_value, str):
            raise GateError(
                f"{path}: malformed license for {name}=={metadata['version']}"
            )
        packages.append((name, metadata["version"], license_value))
    if not packages:
        raise GateError(f"empty npm lock: {path}")
    return packages


def _with_declared_locks(root: Path, discovered: list[str], declared: list[str]) -> list[str]:
    """The discovered Python surface plus every declared lock, so a lock
    with any name (``deps.txt``) is parsed and license-checked like the
    globbed ones. A declared lock that does not exist fails."""
    for path in declared:
        if not (root / path).is_file():
            raise GateError(f"declared python lock does not exist: {path}")
    return sorted(set(discovered) | set(declared))


def collect_locked_packages(
    root: Path, policy: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    actual_python, actual_npm = discover_lock_paths(root)
    expected_python = sorted(policy.get("python_locks", []))
    actual_python = _with_declared_locks(root, actual_python, expected_python)
    manifests = _python_manifests(policy)
    _check_installed_are_locks(root, policy, manifests)
    declared_python = sorted(expected_python + list(manifests))
    expected_npm = sorted(policy.get("npm_locks", []))
    if actual_python != declared_python:
        raise GateError(
            f"Python lock discovery drift: expected {declared_python}, found {actual_python}"
        )
    if actual_npm != expected_npm:
        raise GateError(
            f"npm lock discovery drift: expected {expected_npm}, found {actual_npm}"
        )
    _check_npm_manifests(root, expected_npm)
    packages: dict[str, dict[str, Any]] = {}
    for relative in expected_python:
        for name, version in parse_python_lock(root / relative):
            key = package_key("pypi", name, version)
            packages.setdefault(
                key,
                {"ecosystem": "pypi", "name": name, "version": version, "sources": []},
            )
            packages[key]["sources"].append(relative)
    for relative in expected_npm:
        for name, version, declared_license in parse_npm_lock(root / relative):
            key = package_key("npm", name, version)
            packages.setdefault(
                key,
                {
                    "ecosystem": "npm",
                    "name": name,
                    "version": version,
                    "sources": [],
                    "declared_licenses": set(),
                },
            )
            packages[key]["sources"].append(relative)
            if declared_license:
                packages[key]["declared_licenses"].add(declared_license)
    for record in packages.values():
        record["sources"] = sorted(set(record["sources"]))
    return packages


def _validate_exceptions(
    policy: dict[str, Any], today: date
) -> dict[str, dict[str, Any]]:
    values = policy.get("exceptions")
    if not isinstance(values, list):
        raise GateError("policy exceptions must be an array")
    indexed: dict[str, dict[str, Any]] = {}
    required = (
        "ecosystem",
        "name",
        "version",
        "license",
        "owner",
        "rationale",
        "expires",
    )
    for item in values:
        if not isinstance(item, dict):
            raise GateError("license exception must be an object")
        for field in required:
            if not isinstance(item.get(field), str) or not item[field].strip():
                raise GateError(f"license exception missing {field}")
        key = package_key(item["ecosystem"], item["name"], item["version"])
        if key in indexed:
            raise GateError(f"duplicate license exception for {key}")
        try:
            expiry = date.fromisoformat(item["expires"])
        except ValueError as exc:
            raise GateError(f"invalid exception expiry: {item['expires']!r}") from exc
        if expiry < today:
            raise GateError(f"expired license exception: {key}")
        indexed[key] = item
    return indexed


def _validate_exception_inventory(
    exceptions: dict[str, dict[str, Any]], inventory: dict[str, dict[str, Any]]
) -> None:
    for key, exception in exceptions.items():
        row = inventory.get(key)
        if row is None:
            raise GateError(f"orphaned license exception: {key}")
        if exception["license"] != row.get("license"):
            raise GateError(f"exception license mismatch: {key}")


def _exception_for(
    record: dict[str, Any], exceptions: dict[str, dict[str, Any]]
) -> bool:
    key = package_key(record["ecosystem"], record["name"], record["version"])
    exception = exceptions.get(key)
    return bool(exception and exception["license"] == record.get("license"))


def _validate_license(
    record: dict[str, Any],
    policy: dict[str, Any],
    exceptions: dict[str, dict[str, Any]],
) -> None:
    expression = record.get("license")
    if (
        not isinstance(expression, str)
        or not expression.strip()
        or expression.upper() == "UNKNOWN"
    ):
        raise GateError(
            f"missing license: {record['ecosystem']}:{record['name']}=={record['version']}"
        )
    tree = parse_spdx(expression)
    if _exception_for(record, exceptions):
        return
    approved = set(policy.get("approved_licenses", []))
    denied = set(policy.get("denied_licenses", []))
    if not _spdx_allowed(tree, approved, denied):
        raise GateError(
            f"unapproved license {expression}: {record['ecosystem']}:{record['name']}=={record['version']}"
        )


def _attribution_text(root: Path, policy: dict[str, Any]) -> str:
    chunks: list[str] = []
    for relative in policy.get("attribution_files", []):
        path = root / relative
        if not path.is_file():
            raise GateError(f"missing attribution file: {relative}")
        chunks.append(path.read_text(encoding="utf-8"))
    return "\n".join(chunks)


def _validate_inventory_header(
    policy: dict[str, Any], inventory: dict[str, Any]
) -> list[dict[str, Any]]:
    if policy.get("schema_version") != 1 or inventory.get("schema_version") != 1:
        raise GateError("unsupported dependency license schema")
    tools = inventory.get("tool_versions")
    if not isinstance(tools, dict):
        raise GateError("inventory tool_versions must be an object")
    if tools.get("inventory_generator") != "1" or tools.get("npm_lockfile") != "3":
        raise GateError("inventory tool version drift")
    if not re.fullmatch(r"\d+\.\d+\.\d+", str(tools.get("python", ""))):
        raise GateError("inventory Python runtime version is missing")
    rows = inventory.get("packages")
    if not isinstance(rows, list):
        raise GateError("inventory packages must be an array")
    return rows


def _index_inventory_rows(
    rows: list[dict[str, Any]],
    policy: dict[str, Any],
    attribution_text: str,
    exceptions: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    expected_attributions = policy.get("attributions", {})
    for row in rows:
        if not isinstance(row, dict):
            raise GateError("inventory package row must be an object")
        key = package_key(
            str(row.get("ecosystem")), str(row.get("name")), str(row.get("version"))
        )
        if key in indexed:
            raise GateError(f"duplicate inventory row: {key}")
        indexed[key] = row
        if (
            not isinstance(row.get("metadata_source"), str)
            or not row["metadata_source"].strip()
        ):
            raise GateError(f"missing authoritative metadata source: {key}")
        marker = expected_attributions.get(f"{row['ecosystem']}:{row['name']}")
        if bool(row.get("attribution_required")) != bool(marker):
            raise GateError(f"attribution classification drift: {key}")
        if marker and (
            row.get("attribution_marker") != marker or marker not in attribution_text
        ):
            raise GateError(f"missing attribution marker {marker!r}: {key}")
    _validate_exception_inventory(exceptions, indexed)
    for row in rows:
        _validate_license(row, policy, exceptions)
    return indexed


def check_repository(root: Path = ROOT, today: date | None = None) -> None:
    policy = _read_json(root / POLICY_PATH)
    inventory = _read_json(root / INVENTORY_PATH)
    check_date = today or date.today()
    exceptions = _validate_exceptions(policy, check_date)
    rows = _validate_inventory_header(policy, inventory)
    locked = collect_locked_packages(root, policy)
    indexed = _index_inventory_rows(
        rows, policy, _attribution_text(root, policy), exceptions
    )
    if set(indexed) != set(locked):
        missing = sorted(set(locked) - set(indexed))
        extra = sorted(set(indexed) - set(locked))
        raise GateError(f"inventory/lock drift: missing={missing}, extra={extra}")
    _check_manifest_licenses(root, _python_manifests(policy), indexed)
    overrides = policy.get("metadata_overrides", {})
    for key, actual in locked.items():
        row = indexed[key]
        if row.get("sources") != actual["sources"]:
            raise GateError(f"source-surface drift: {key}")
        declared = actual.get("declared_licenses", set())
        expected = overrides.get(key, {}).get("license", row["license"])
        if declared and (len(declared) != 1 or declared != {expected}):
            raise GateError(f"npm lock license drift: {key}: {sorted(declared)}")


def _normalise_python_license(
    info: dict[str, Any], key: str, policy: dict[str, Any]
) -> tuple[str, str]:
    expression = info.get("license_expression")
    if isinstance(expression, str) and expression.strip():
        parse_spdx(expression)
        return expression, "published wheel Core Metadata License-Expression"
    override = policy.get("metadata_overrides", {}).get(key)
    if override:
        return override["license"], override["source"]
    raw = info.get("license")
    if isinstance(raw, str):
        raw = raw.strip()
        if raw.upper() == "UNKNOWN":
            raw = ""
        if raw in LICENSE_ALIASES:
            return LICENSE_ALIASES[raw], "published wheel Core Metadata License field"
        try:
            parse_spdx(raw)
        except GateError:
            pass
        else:
            return raw, "published wheel Core Metadata License field"
    classifiers = [
        CLASSIFIER_LICENSES[item]
        for item in info.get("classifiers", [])
        if item in CLASSIFIER_LICENSES
    ]
    if classifiers:
        unique = sorted(set(classifiers))
        return " OR ".join(unique), "published wheel Core Metadata license classifier"
    raise GateError(
        f"authoritative Python metadata has no unambiguous SPDX license: {key}"
    )


def _read_url(url: str, expected_host: str) -> bytes:
    if urlsplit(url).scheme != "https" or urlsplit(url).hostname != expected_host:
        raise GateError(f"unexpected metadata URL: {url}")
    request = Request(url, headers={"User-Agent": "adt-dependency-license-refresh/1"})
    with urlopen(request, timeout=60) as response:  # nosec B310 - host checked above  # nosemgrep: dynamic-urllib-use-detected - https scheme and the expected metadata host are checked above
        if urlsplit(response.geturl()).hostname != expected_host:
            raise GateError(f"unexpected metadata redirect: {response.geturl()}")
        return response.read()


def _select_wheel(files: Any, name: str, version: str) -> dict[str, Any]:
    wheels = [
        item
        for item in files
        if isinstance(item, dict)
        and item.get("packagetype") == "bdist_wheel"
        and isinstance(item.get("url"), str)
    ]
    if not wheels:
        raise GateError(f"no published wheel metadata: {name}=={version}")
    wheels.sort(
        key=lambda item: (
            not str(item.get("filename", "")).endswith("py3-none-any.whl"),
            int(item.get("size", 0)),
            str(item.get("filename", "")),
        )
    )
    return wheels[0]


def _metadata_bytes(wheel: dict[str, Any]) -> bytes:
    metadata_hashes = wheel.get("dist_info_metadata")
    if isinstance(metadata_hashes, dict) and metadata_hashes.get("sha256"):
        data = _read_url(wheel["url"] + ".metadata", "files.pythonhosted.org")
        expected = metadata_hashes["sha256"]
    else:
        archive = _read_url(wheel["url"], "files.pythonhosted.org")
        expected = wheel.get("digests", {}).get("sha256")
        if not expected or hashlib.sha256(archive).hexdigest() != expected:
            raise GateError(f"wheel hash mismatch: {wheel.get('filename')}")
        with ZipFile(BytesIO(archive)) as zipped:
            names = [
                name
                for name in zipped.namelist()
                if name.endswith(".dist-info/METADATA")
            ]
            if len(names) != 1:
                raise GateError(
                    f"wheel has no unique METADATA: {wheel.get('filename')}"
                )
            return zipped.read(names[0])
    if hashlib.sha256(data).hexdigest() != expected:
        raise GateError(f"wheel METADATA hash mismatch: {wheel.get('filename')}")
    return data


def _fetch_pypi(name: str, version: str) -> dict[str, Any]:
    url = f"https://pypi.org/pypi/{quote(name, safe='')}/{quote(version, safe='')}/json"
    payload = json.loads(_read_url(url, "pypi.org"))
    wheel = _select_wheel(payload.get("urls"), name, version)
    message = Parser().parsestr(_metadata_bytes(wheel).decode("utf-8", "replace"))
    return {
        "license_expression": message.get("License-Expression"),
        "license": message.get("License"),
        "classifiers": message.get_all("Classifier", []),
        "wheel": wheel.get("filename"),
    }


def _refresh_row(
    key: str, package: dict[str, Any], policy: dict[str, Any]
) -> dict[str, Any]:
    if package["ecosystem"] == "npm":
        declared = package.get("declared_licenses", set())
        if len(declared) == 1:
            license_value = next(iter(declared))
            metadata_source = "npm package-lock v3 registry metadata"
        else:
            override = policy.get("metadata_overrides", {}).get(key)
            if not override:
                raise GateError(f"npm lock has no unambiguous license metadata: {key}")
            license_value, metadata_source = override["license"], override["source"]
    else:
        info = _fetch_pypi(package["name"], package["version"])
        license_value, metadata_source = _normalise_python_license(info, key, policy)
        metadata_source = f"{metadata_source} ({info['wheel']})"
    marker = policy.get("attributions", {}).get(
        f"{package['ecosystem']}:{package['name']}"
    )
    row = {
        "ecosystem": package["ecosystem"],
        "name": package["name"],
        "version": package["version"],
        "sources": package["sources"],
        "license": license_value,
        "metadata_source": metadata_source,
        "attribution_required": bool(marker),
    }
    if marker:
        row["attribution_marker"] = marker
    return row


def refresh_inventory(root: Path = ROOT) -> None:
    policy = _read_json(root / POLICY_PATH)
    locked = collect_locked_packages(root, policy)
    rows = [_refresh_row(key, locked[key], policy) for key in sorted(locked)]
    inventory = {
        "schema_version": 1,
        "generated_by": "scripts/check_dependency_licenses.py --refresh",
        "tool_versions": {
            "inventory_generator": "1",
            "npm_lockfile": "3",
            "python": platform.python_version(),
        },
        "packages": rows,
    }
    output = root / INVENTORY_PATH
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(inventory, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    check_repository(root)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--check",
        action="store_true",
        help="offline check of locks, policy, inventory, and attribution",
    )
    mode.add_argument(
        "--refresh",
        action="store_true",
        help="explicitly query PyPI and refresh checked-in metadata",
    )
    args = parser.parse_args(argv)
    try:
        if args.refresh:
            refresh_inventory()
        else:
            check_repository()
    except GateError as exc:
        print(f"dependency-license-gate: FAIL: {exc}", file=sys.stderr)
        return 1
    print("dependency-license-gate: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
