# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The customer-golden REPOSITORY BOUNDARY gate (evals Phase 2, Task 1.3).

This repository is published to AWS Samples. Customer-specific golden data
(question text derived from customer traffic, customer expectations) must
NEVER enter this repo's git. Nothing else enforces that today:

* ``test_engine_domain_neutrality.py`` scans only ``*.py``/``*.ts``/``*.tsx``/
  ``*.js`` — its own comment says demo golden JSON is out of scope BY
  EXTENSION — and its denylist catches only known term families.
* ``.gitignore`` is NOT a boundary (``git add -f``, renames).

So this gate pins the boundary with four pins — the first three at the
git-tracking level, the fourth at the source level:

(i)   **Shape scan** — every TRACKED ``*.json`` file (``git ls-files``) is
      parsed; anything with golden-set SHAPE (top-level ``version`` +
      ``questions[]`` whose entries carry ``id``/``text``/``expect``) outside
      the ONE sanctioned path ``agentic_digital_thread/agents/demo/
      golden_evals.json`` fails. Shape, not name: a renamed customer golden
      still trips.
(ii)  **Pinned manifest** — the sanctioned sample golden's question ids are
      enumerated HERE. Adding/renaming a question requires a deliberate edit
      of this gate, i.e. a customer-data review.
(iii) **Customer-identity denylist** — the ``customer-*`` denylist families
      from ``test_engine_domain_neutrality.py`` (imported, never duplicated)
      run over the sanctioned golden's full text. The demo families
      (fictional demo city/site names) are the sample's sanctioned content
      and are NOT applied; customer identity has no sanctioned home anywhere.
(iv)  **ONE filesystem golden source on the SERVER** (ADR-0064 D4) — the
      server-side golden-run modules (``agents/eval_runs.py``,
      ``agents/eval_routes.py``) may read a golden set from EXACTLY ONE
      filesystem source, ``eval_core.default_golden_set_path()``. WHY: a
      steward may UPLOAD a customer golden set for a server-side run, and
      those bytes are transient request/process data that must never acquire
      a filesystem home — a second path-shaped golden reference (an
      ``open()``/``Path()``/tempfile destination, a hardcoded ``*.json``
      path, a ``load_golden_set`` of anything else) is exactly the seam an
      uploaded set could be written to and then, one ``git add -f`` later,
      committed into a published repo. Pins (i)–(iii) stop the COMMIT; pin
      (iv) stops the file from ever existing. Source-scanned via ``ast``
      (never a bare substring grep), with intra-module resolution of the
      sanctioned call chain, and PROVEN TO BITE against planted-line temp
      copies of the real module source.

Offline, fast, no network. ``git`` is invoked with a FIXED argv list and no
shell (the ``test_response_integrity_iteration*`` idiom). When ``.git`` is
absent (packaged-source runs) or the ``git`` binary is unavailable, the
tracked-file pin SKIPS LOUDLY rather than false-failing; the manifest and
denylist pins need only the file and skip only when it is absent. Pin (iv)
needs only the module sources and skips when one is absent.
"""
from __future__ import annotations

import ast
import json
import re
import subprocess  # nosec B404 - fixed argv lists, no shell, internal inputs only
from pathlib import Path

import pytest

# Reuse the neutrality gate's roots and PINNED denylist — never duplicate the
# patterns (one sanctioned home for the customer-identity terms).
from tests.unit.test_engine_domain_neutrality import (
    DENYLIST,
    REPO_ROOT,
    _SCAN_ROOTS,
)

# The ONE sanctioned golden-set path (repo-root-relative posix).
SANCTIONED_GOLDEN = "agentic_digital_thread/agents/demo/golden_evals.json"

# ---------------------------------------------------------------------------
# PINNED MANIFEST — the reviewed question ids of the sanctioned SAMPLE golden.
# Adding, removing, or renaming a question in golden_evals.json requires a
# deliberate edit here, which is the customer-data review moment: confirm the
# new question is fictional demo content, never customer-derived.
# ---------------------------------------------------------------------------
APPROVED_QUESTION_IDS = frozenset({
    "g-cross-plant-quality",
    "g-orders-at-risk",
    "g-critical-work-orders",
    "g-supplier-quality-trace",
    "g-spec-deviation",
    "g-downtime-root-cause",
    "g-single-system-open-orders",
    "g-lot-traceability",
    "g-wo-lot-supplier-trace",
    "g-ambiguous-scope",
    "g-absent-data-honesty",
    "g-injection-via-data",
    # Fictional workshop story (erp_simulation/story_history.py + db-init
    # CUST-MCH-0001..0003 demo names); runs only on ADT_WORKSHOP_STORY=live.
    "g-workshop-live-m2-q1-shipped-batch",
    "g-workshop-live-m7b-lot-total",
    # Same fictional story, modules 5 and 6 (after the plant CMMS is onboarded).
    "g-workshop-live-m5-q1-maintenance",
    "g-workshop-live-m6-machine-at-risk",
})


# ---------------------------------------------------------------------------
# Helpers (pure; the temp-git fixture test reuses them against another root).
# ---------------------------------------------------------------------------
def _tracked_json_files(root: Path) -> list[str]:
    """Repo-relative posix paths of all git-TRACKED ``*.json`` files.

    Fixed argv, no shell. ``-z`` for unambiguous separators (no quoting of
    non-ASCII paths). Raises the underlying error to the caller so tests can
    decide between skip (environmental) and fail (real).
    """
    output = subprocess.run(  # nosec B603 B607 - fixed argv list, no shell, internal inputs only; well-known tool resolved from PATH by design
        ["git", "ls-files", "-z", "--", "*.json"],
        cwd=root, check=True, capture_output=True, text=True,
    ).stdout
    return [p for p in output.split("\0") if p]


def _golden_shape(data: object) -> bool:
    """True when ``data`` has golden-set SHAPE: a dict with a top-level
    ``version`` and a ``questions`` list where AT LEAST ONE entry carries
    ``id``/``text``/``expect``. 'Any entry' (not 'all') is the fail-closed
    choice: one malformed sibling entry must not launder a customer question
    past the gate."""
    if not isinstance(data, dict) or "version" not in data:
        return False
    questions = data.get("questions")
    if not isinstance(questions, list):
        return False
    return any(
        isinstance(q, dict) and {"id", "text", "expect"} <= set(q)
        for q in questions
    )


def _golden_shaped_offenders(root: Path, sanctioned: str) -> list[str]:
    """Tracked JSON files with golden-set shape outside the sanctioned path.

    Unparseable JSON (e.g. tsconfig-style comments) cannot be golden-shaped
    and is skipped; files tracked in the index but deleted from the working
    tree have no readable content and are skipped too.
    """
    offenders: list[str] = []
    for rel in _tracked_json_files(root):
        if rel == sanctioned:
            continue
        path = root / rel
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if _golden_shape(data):
            offenders.append(rel)
    return offenders


def _require_git_repo() -> None:
    """Skip LOUDLY (never false-fail) outside a git checkout — e.g. a
    packaged-source run — or when the git binary is unavailable."""
    if not (REPO_ROOT / ".git").exists():
        pytest.skip(
            "customer-golden boundary gate needs a git checkout to enumerate "
            "TRACKED files (.git absent — packaged-source run?); the "
            "boundary is only enforceable against the git index"
        )
    try:
        subprocess.run(  # nosec B603 B607 - fixed argv list, no shell
            ["git", "rev-parse", "--git-dir"],
            cwd=REPO_ROOT, check=True, capture_output=True, text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        pytest.skip(f"git unavailable for the boundary gate: {exc}")


def _load_sanctioned_golden() -> dict:
    path = REPO_ROOT / SANCTIONED_GOLDEN
    if not path.is_file():
        pytest.skip(
            f"sanctioned sample golden absent ({SANCTIONED_GOLDEN}) — "
            f"nothing to pin (packaged/partial source run?)"
        )
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Pin (i): tracked-JSON golden-shape scan.
# ---------------------------------------------------------------------------
def test_no_golden_shaped_json_tracked_outside_sanctioned_path():
    """Any git-TRACKED JSON with golden-set shape outside the ONE sanctioned
    demo path is customer-golden leakage into a published repo — remove it
    from git and keep customer sets EXTERNAL (CLI ``--golden-set``)."""
    _require_git_repo()
    offenders = _golden_shaped_offenders(REPO_ROOT, SANCTIONED_GOLDEN)
    assert not offenders, (
        "golden-set-shaped JSON tracked in git OUTSIDE the sanctioned demo "
        f"path {SANCTIONED_GOLDEN!r}:\n"
        + "\n".join(f"  - {rel}" for rel in sorted(offenders))
        + "\n\nThis repo is published to AWS Samples: customer-derived golden "
        "sets must NEVER be committed here (not even renamed or force-added "
        "— shape is what this gate matches). Keep customer golden sets in an "
        "EXTERNAL, customer-owned location and point the CLI lanes at them "
        "via --prompts-file/--golden-set. If this is genuinely NEW fictional "
        "demo content, it belongs inside the sanctioned file (with a "
        "manifest edit in this gate), never in a second file."
    )


def test_gate_bites_on_a_tracked_golden_shaped_file(tmp_path):
    """Prove pin (i) actually fails: a temp git repo with a tracked
    customer-shaped golden JSON is DETECTED, and detection clears once the
    file is untracked."""
    try:
        subprocess.run(  # nosec B603 B607 - fixed argv list, no shell
            ["git", "init", "-q", str(tmp_path)],
            check=True, capture_output=True, text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        pytest.skip(f"git unavailable for the fixture repo: {exc}")

    leaked = tmp_path / "data" / "innocuous_name.json"
    leaked.parent.mkdir()
    leaked.write_text(json.dumps({
        "version": 1,
        "questions": [{
            "id": "cust-q-1",
            "text": "customer-derived question text",
            "expect": {"systems_min": 1},
        }],
    }), encoding="utf-8")
    # Also a non-golden JSON — must NOT be flagged.
    (tmp_path / "package.json").write_text('{"name": "x"}', encoding="utf-8")
    subprocess.run(  # nosec B603 B607 - fixed argv list, no shell
        ["git", "add", "-f", "--", "data/innocuous_name.json", "package.json"],
        cwd=tmp_path, check=True, capture_output=True, text=True,
    )

    offenders = _golden_shaped_offenders(tmp_path, SANCTIONED_GOLDEN)
    assert offenders == ["data/innocuous_name.json"], (
        f"the boundary gate failed to flag a tracked golden-shaped file "
        f"(got {offenders!r}) — pin (i) is not biting"
    )

    subprocess.run(  # nosec B603 B607 - fixed argv list, no shell
        ["git", "rm", "-q", "--cached", "--", "data/innocuous_name.json"],
        cwd=tmp_path, check=True, capture_output=True, text=True,
    )
    assert _golden_shaped_offenders(tmp_path, SANCTIONED_GOLDEN) == [], (
        "an UNTRACKED golden-shaped file must not trip the gate — the "
        "boundary is git tracking, not working-tree presence"
    )


# ---------------------------------------------------------------------------
# Pin (ii): the sanctioned sample golden's question-id manifest.
# ---------------------------------------------------------------------------
def test_sanctioned_golden_question_ids_match_pinned_manifest():
    """Adding/removing/renaming a question in the sanctioned sample golden
    requires a deliberate edit of APPROVED_QUESTION_IDS — the customer-data
    review moment for new golden content."""
    data = _load_sanctioned_golden()
    actual = [q.get("id") for q in data.get("questions", [])]
    dupes = {i for i in actual if actual.count(i) > 1}
    assert not dupes, f"duplicate question ids in {SANCTIONED_GOLDEN}: {sorted(dupes)}"
    added = set(actual) - APPROVED_QUESTION_IDS
    removed = APPROVED_QUESTION_IDS - set(actual)
    assert not added and not removed, (
        f"{SANCTIONED_GOLDEN} question ids diverge from the reviewed "
        f"manifest in this gate — added: {sorted(added) or 'none'}, "
        f"removed: {sorted(removed) or 'none'}.\n"
        "If this change is intentional: confirm every added question is "
        "FICTIONAL demo content (never customer-derived — this repo is "
        "published to AWS Samples), then update APPROVED_QUESTION_IDS in "
        "tests/unit/test_customer_golden_boundary.py as the review record."
    )


# ---------------------------------------------------------------------------
# Pin (iii): customer-identity denylist over the sanctioned golden's text.
# ---------------------------------------------------------------------------
def test_sanctioned_golden_carries_no_customer_identity_terms():
    """The ``customer-*`` denylist families (imported from the neutrality
    gate — the ONE home for those patterns) must find NOTHING in the
    sanctioned golden's full text. The demo families are the sample's
    sanctioned fictional content and are deliberately not applied here."""
    customer_classes = {
        cls: pattern for cls, pattern in DENYLIST.items()
        if cls.startswith("customer-")
    }
    assert customer_classes, (
        "no customer-* classes found in the imported DENYLIST — the "
        "neutrality gate's customer-identity families were renamed; realign "
        "this gate with test_engine_domain_neutrality.DENYLIST"
    )
    path = REPO_ROOT / SANCTIONED_GOLDEN
    _load_sanctioned_golden()  # skip-cleanly path + parse validation
    text = path.read_text(encoding="utf-8")
    hits = [
        f"[{cls}] {m.group(0)!r} at offset {m.start()}"
        for cls, pattern in customer_classes.items()
        for m in re.finditer(pattern, text)
    ]
    assert not hits, (
        f"customer-identity terms found in the sanctioned sample golden "
        f"{SANCTIONED_GOLDEN}:\n" + "\n".join(f"  - {h}" for h in hits)
        + "\nCustomer identity has NO sanctioned home in this repo — replace "
        "with fictional demo content."
    )


# ---------------------------------------------------------------------------
# Premise pin: this gate exists BECAUSE the neutrality ratchet skips JSON.
# ---------------------------------------------------------------------------
def test_premise_neutrality_gate_scans_no_json():
    """If the neutrality gate ever grows a ``*.json`` glob, revisit the
    division of labour between the two gates (this one stays authoritative
    for golden SHAPE and the manifest either way)."""
    globs = {pattern for _, patterns in _SCAN_ROOTS for pattern in patterns}
    assert not any(g.endswith(".json") for g in globs), (
        f"test_engine_domain_neutrality._SCAN_ROOTS now covers JSON "
        f"({sorted(globs)}) — the premise of this boundary gate changed; "
        "reconcile the two gates' scopes"
    )


# ===========================================================================
# Pin (iv): the SERVER has EXACTLY ONE filesystem golden source (ADR-0064 D4).
# ===========================================================================
# Scoped deliberately to the server-side RUN surface. Out of scope, with
# reasons: ``agents/eval_core.py`` is the ONE sanctioned home of the packaged
# path literal and of the golden file read itself (that is the single source
# this pin points at), and ``agents/eval_promote.py`` builds a DOWNLOAD
# filename, never a filesystem destination (ADR-0062: the draft is
# download-only, with zero server-side persistence).
SERVER_RUN_MODULES: tuple[str, ...] = (
    "agentic_digital_thread/agents/eval_runs.py",
    "agentic_digital_thread/agents/eval_routes.py",
)

# The ONE sanctioned filesystem golden source. Its argument-less shape is
# what makes this pin checkable: a path that cannot be influenced by a
# request cannot become a second source.
SANCTIONED_PATH_HELPER = "default_golden_set_path"

# Calls that read a golden set FROM A PATH. Allowed, but only when the path
# resolves to SANCTIONED_PATH_HELPER; an uploaded set must reach the parser
# as BYTES (``parse_golden_set``/``golden_bytes_sha``), never as a file.
_GOLDEN_PATH_READERS = frozenset({"load_golden_set", "golden_file_sha"})

# Filesystem constructs of any kind. In these modules there is nothing
# legitimate to read or write on disk, so each one is an offender unless its
# path resolves to the sanctioned helper. Writes matter as much as reads:
# a write seam is how an uploaded set would acquire a filesystem home.
_FS_CONSTRUCTS = frozenset({
    "open", "fdopen",
    "Path", "PurePath",
    "NamedTemporaryFile", "TemporaryFile", "SpooledTemporaryFile",
    "TemporaryDirectory", "mkstemp", "mkdtemp", "mkdir", "makedirs",
    "copyfile", "copyfileobj", "copy2", "copytree", "move",
})

# Path METHODS — the receiver, not an argument, carries the path.
_PATH_METHODS = frozenset({
    "read_text", "read_bytes", "write_text", "write_bytes",
})

_FS_CONSTRUCTS = _FS_CONSTRUCTS | _PATH_METHODS

# A path-shaped JSON literal (``goldens.json``, ``/var/tmp/x.json``).
# Media types (``application/json``) carry no dot before ``json`` and are
# additionally guarded, so the pin never fires on a Content-Type check.
_JSON_PATH_RE = re.compile(r"[^\s\"']*\.json(?![\w])")
_MEDIA_TYPE_RE = re.compile(r"(?i)^[a-z][a-z0-9.+-]*/[a-z0-9.+-]+$")


# ---------------------------------------------------------------------------
# Detector (pure, AST-based — never a bare substring grep: a comment or a
# docstring must not trip it, and a real call must not hide behind
# formatting). The planted-line fixture below runs this SAME code.
# ---------------------------------------------------------------------------
def _callee_name(func: ast.expr) -> str:
    """The called name, ignoring the receiver (``a.b.load_golden_set`` ->
    ``load_golden_set``) — an import alias must not launder a call past."""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _scope_map(tree: ast.AST) -> dict:
    """node id -> innermost enclosing function name (``None`` = module)."""
    scopes: dict = {}

    def walk(node: ast.AST, current) -> None:
        for child in ast.iter_child_nodes(node):
            name = (child.name
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                    else current)
            scopes[id(child)] = name
            walk(child, name)

    walk(tree, None)
    return scopes


def _local_functions(tree: ast.AST) -> dict:
    return {node.name: node for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _positional_params(fn) -> list:
    args = fn.args
    return [p.arg for p in (*getattr(args, "posonlyargs", ()), *args.args)]


def _name_bindings(tree: ast.AST, scopes: dict, funcs: dict) -> dict:
    """``(scope, name) -> [(value_node, value_scope), ...]`` for simple
    assignments and for parameters bound at INTRA-MODULE call sites,
    including a ``threading.Thread(target=..., args=(...))`` tuple (the
    shape the real worker is started with). Fail-closed by construction: a
    name with no binding the scan can see is NOT sanctioned."""
    bindings: dict = {}

    def add(scope, name, value, value_scope) -> None:
        bindings.setdefault((scope, name), []).append((value, value_scope))

    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            if node.value is None:
                continue
            targets = (node.targets if isinstance(node, ast.Assign)
                       else [node.target])
            scope = scopes.get(id(node))
            for target in targets:
                if isinstance(target, ast.Name):
                    add(scope, target.id, node.value, scope)
            continue
        if not isinstance(node, ast.Call):
            continue
        call_scope = scopes.get(id(node))
        if isinstance(node.func, ast.Name) and node.func.id in funcs:
            fn = funcs[node.func.id]
            names = _positional_params(fn)
            for i, arg in enumerate(node.args):
                if i < len(names) and not isinstance(arg, ast.Starred):
                    add(fn.name, names[i], arg, call_scope)
            for kw in node.keywords:
                if kw.arg:
                    add(fn.name, kw.arg, kw.value, call_scope)
            continue
        if _callee_name(node.func) == "Thread":
            target_fn = None
            args_seq = None
            for kw in node.keywords:
                if kw.arg == "target" and isinstance(kw.value, ast.Name):
                    target_fn = funcs.get(kw.value.id)
                elif kw.arg == "args" and isinstance(kw.value, (ast.Tuple, ast.List)):
                    args_seq = kw.value
            if target_fn is not None and args_seq is not None:
                names = _positional_params(target_fn)
                for i, arg in enumerate(args_seq.elts):
                    if i < len(names) and not isinstance(arg, ast.Starred):
                        add(target_fn.name, names[i], arg, call_scope)
    return bindings


def _is_sanctioned(node, scope, helpers: set, names: set) -> bool:
    """True only for the sanctioned path expression: a call to
    ``default_golden_set_path()``, a call to a resolved argument-less local
    helper that returns it, or a name provably bound to one. A string
    literal is NEVER sanctioned — that is the whole point."""
    if isinstance(node, ast.Call):
        called = _callee_name(node.func)
        return called == SANCTIONED_PATH_HELPER or called in helpers
    if isinstance(node, ast.Name):
        return (scope, node.id) in names or (None, node.id) in names
    return False


def _resolve_sanctioned(funcs: dict, bindings: dict) -> tuple:
    """Fixpoint over the module's own call graph: which argument-less
    helpers return the sanctioned path, and which names are provably bound
    to it. Two hops are needed on the real module (``_golden_set_path()``
    -> the worker's thread arg -> the epilogue's parameter)."""
    helpers: set = set()
    names: set = set()

    returns: dict = {}
    for fname, fn in funcs.items():
        if _positional_params(fn) or fn.args.kwonlyargs:
            continue
        values = [n.value for n in ast.walk(fn)
                  if isinstance(n, ast.Return) and n.value is not None]
        if values:
            returns[fname] = values

    for _ in range(len(funcs) + len(bindings) + 3):
        changed = False
        for fname, values in returns.items():
            if fname not in helpers and all(
                    _is_sanctioned(v, fname, helpers, names) for v in values):
                helpers.add(fname)
                changed = True
        for key, bound in bindings.items():
            if key not in names and bound and all(
                    _is_sanctioned(v, s, helpers, names) for v, s in bound):
                names.add(key)
                changed = True
        if not changed:
            break
    return helpers, names


def _docstring_node_ids(tree: ast.AST) -> set:
    out: set = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef,
                                 ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            out.add(id(first.value))
    return out


def _checked_expr(call: ast.Call):
    """The expression whose ORIGIN decides the verdict: the receiver for a
    path method, else the first positional argument (a path-ish keyword as a
    fallback)."""
    if (_callee_name(call.func) in _PATH_METHODS
            and isinstance(call.func, ast.Attribute)):
        return call.func.value
    if call.args and not isinstance(call.args[0], ast.Starred):
        return call.args[0]
    for kw in call.keywords:
        if kw.arg in ("path", "file", "filename", "dir", "dest"):
            return kw.value
    return None


def _golden_source_findings(source: str, label: str) -> tuple:
    """``(offenders, accepted)`` for one module's source text.

    ``offenders`` are ``"<label>:<line>: <what> — <why>"`` strings; ``accepted``
    lists the golden file reads that DO resolve to the sanctioned helper (the
    anti-vacuity evidence: a pin that finds nothing at all proves nothing).
    Raises ``SyntaxError`` to the caller.
    """
    tree = ast.parse(source)
    scopes = _scope_map(tree)
    funcs = _local_functions(tree)
    helpers, names = _resolve_sanctioned(funcs, _name_bindings(tree, scopes, funcs))
    doc_ids = _docstring_node_ids(tree)

    offenders: list = []
    accepted: list = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            called = _callee_name(node.func)
            if called not in _GOLDEN_PATH_READERS and called not in _FS_CONSTRUCTS:
                continue
            expr = _checked_expr(node)
            scope = scopes.get(id(node))
            ok = expr is not None and _is_sanctioned(expr, scope, helpers, names)
            if called in _GOLDEN_PATH_READERS:
                if ok:
                    accepted.append(f"{label}:{node.lineno}: {called}(...)")
                else:
                    offenders.append((node.lineno, (
                        f"{label}:{node.lineno}: `{called}(...)` reads a golden "
                        f"set from a path that does NOT resolve to "
                        f"{SANCTIONED_PATH_HELPER}()")))
            elif not ok:
                offenders.append((node.lineno, (
                    f"{label}:{node.lineno}: filesystem construct "
                    f"`{called}(...)` — a second filesystem golden seam")))
            continue
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in doc_ids
                and _JSON_PATH_RE.search(node.value)
                and not _MEDIA_TYPE_RE.match(node.value.strip())):
            offenders.append((node.lineno, (
                f"{label}:{node.lineno}: hardcoded JSON path literal "
                f"{node.value!r} — the ONLY golden path literal lives in "
                f"eval_core ({SANCTIONED_PATH_HELPER}())")))
    return [text for _, text in sorted(offenders)], accepted


_PIN_IV_REMEDY = (
    "\n\nThis repo is published to AWS Samples and a steward may UPLOAD a "
    "customer golden set for a server-side run (ADR-0064): those bytes are "
    "transient request/process data and must NEVER acquire a filesystem home "
    "— not a temp file, not a cached copy, not a second configurable path. "
    f"The server has EXACTLY ONE filesystem golden source, "
    f"eval_core.{SANCTIONED_PATH_HELPER}() (the packaged demo set). An "
    "uploaded set reaches the loader as BYTES (eval_core.parse_golden_set / "
    "golden_bytes_sha) and is never written down. If you need a different "
    "golden set on the server, upload it — do not add a path."
)


def _module_source(rel: str) -> str:
    """Skip cleanly (never false-fail) when a scanned module is absent — the
    packaged/partial-source case pins (ii)/(iii) already handle this way."""
    path = REPO_ROOT / rel
    if not path.is_file():
        pytest.skip(
            f"server-side run module absent ({rel}) — nothing to scan "
            f"(packaged/partial source run?)"
        )
    return path.read_text(encoding="utf-8")


def _scan_module(rel: str) -> tuple:
    try:
        return _golden_source_findings(_module_source(rel), rel)
    except SyntaxError as exc:  # a real breakage, not an environment gap
        pytest.fail(f"{rel} does not parse ({exc}) — pin (iv) cannot scan it")


def test_server_run_modules_have_exactly_one_filesystem_golden_source():
    """Every golden-set file read in the server-side run modules resolves to
    ``eval_core.default_golden_set_path()``, and those modules touch the
    filesystem nowhere else."""
    offenders: list = []
    for rel in SERVER_RUN_MODULES:
        offenders.extend(_scan_module(rel)[0])
    assert not offenders, (
        "a SECOND filesystem golden source (or a filesystem seam) in the "
        "server-side run modules:\n"
        + "\n".join(f"  - {o}" for o in offenders)
        + _PIN_IV_REMEDY
    )


def test_pin_iv_is_not_vacuous_the_sanctioned_read_is_recognised():
    """Anti-rot: the scan must still SEE the sanctioned golden read. If the
    server stops reading the packaged set through
    ``default_golden_set_path()``, the pin above would pass by finding
    nothing — so assert the resolver recognises the real call chain."""
    accepted: list = []
    for rel in SERVER_RUN_MODULES:
        accepted.extend(_scan_module(rel)[1])
    assert accepted, (
        "pin (iv) found NO golden-set file read resolving to "
        f"eval_core.{SANCTIONED_PATH_HELPER}() in {list(SERVER_RUN_MODULES)} "
        "— either the packaged read moved (re-scope SERVER_RUN_MODULES) or "
        "the resolver no longer follows the call chain (the pin would then "
        "be vacuously green). Verify by hand before relaxing anything."
    )


# ---------------------------------------------------------------------------
# Pin (iv) bite-proof — the pin (i) idiom (a temp fixture, the SAME detector).
# Each case is a temp COPY of the real module source with ONE planted line;
# planted names are neutral placeholders (no customer text of any kind).
# ---------------------------------------------------------------------------
_PLANTED_SECOND_SOURCES = (
    pytest.param(
        "agentic_digital_thread/agents/eval_runs.py",
        '\n\ndef _planted_second_source(supplied_path: str) -> list:\n'
        '    """PLANTED: a golden set loaded from a caller-supplied path."""\n'
        "    return eval_core.load_golden_set(supplied_path)\n",
        "load_golden_set(supplied_path)",
        id="load_golden_set-of-a-supplied-path",
    ),
    pytest.param(
        "agentic_digital_thread/agents/eval_runs.py",
        '\n\n_PLANTED_GOLDEN_FILE = "/var/tmp/second_goldens.json"\n',
        "second_goldens.json",
        id="hardcoded-json-path-literal",
    ),
    pytest.param(
        "agentic_digital_thread/agents/eval_routes.py",
        "\n\ndef _planted_persist(raw: bytes, dest: str) -> None:\n"
        '    """PLANTED: an uploaded set acquiring a filesystem home."""\n'
        '    with open(dest, "wb") as handle:\n'
        "        handle.write(raw)\n",
        'open(dest, "wb")',
        id="open-for-write",
    ),
    pytest.param(
        "agentic_digital_thread/agents/eval_routes.py",
        "\n\ndef _planted_persist_via_path(raw: bytes, dest: str) -> None:\n"
        '    """PLANTED: the same seam through pathlib."""\n'
        "    Path(dest).write_bytes(raw)\n",
        "write_bytes(raw)",
        id="pathlib-write",
    ),
    pytest.param(
        "agentic_digital_thread/agents/eval_routes.py",
        "\n\ndef _planted_spool(raw: bytes) -> str:\n"
        '    """PLANTED: a temp-file spool of the uploaded bytes."""\n'
        "    handle = tempfile.NamedTemporaryFile(delete=False)\n"
        "    handle.write(raw)\n"
        "    return handle.name\n",
        "NamedTemporaryFile(delete=False)",
        id="tempfile-spool",
    ),
)


@pytest.mark.parametrize("rel,planted,needle", _PLANTED_SECOND_SOURCES)
def test_pin_iv_bites_on_a_planted_second_filesystem_golden_source(
        tmp_path, rel, planted, needle):
    """Prove pin (iv) FAILS on a planted second filesystem golden reference:
    a temp copy of the real module source plus one line must be flagged, at
    the planted line, by the same detector the pin uses."""
    original = _module_source(rel)
    if not original.endswith("\n"):
        original += "\n"
    fixture = tmp_path / "planted_module.py"
    fixture.write_text(original + planted, encoding="utf-8")

    label = f"{rel} (planted)"
    offenders, _ = _golden_source_findings(
        fixture.read_text(encoding="utf-8"), label)

    planted_lines = planted.splitlines()
    hits = [i for i, line in enumerate(planted_lines, start=1) if needle in line]
    assert hits, f"fixture bug: needle {needle!r} not in the planted snippet"
    expected = len(original.splitlines()) + hits[0]

    assert any(f"{label}:{expected}:" in o for o in offenders), (
        f"pin (iv) did NOT flag the planted second golden source at line "
        f"{expected} ({needle!r}) — the pin is not biting.\nOffenders "
        f"reported:\n" + ("\n".join(f"  - {o}" for o in offenders) or "  (none)")
    )


def test_pin_iv_accepts_the_sanctioned_call_shapes(tmp_path):
    """The mirror of the bite-proof: the detector must NOT be "any golden
    call fails". A planted read through the sanctioned helper — directly and
    through the module's own argument-less wrapper — stays clean, so a
    failure above always means a genuinely new filesystem source."""
    rel = SERVER_RUN_MODULES[0]
    original = _module_source(rel)
    if not original.endswith("\n"):
        original += "\n"
    fixture = tmp_path / "sanctioned_module.py"
    fixture.write_text(original + (
        "\n\ndef _planted_direct_packaged_read() -> list:\n"
        "    return eval_core.load_golden_set("
        "eval_core.default_golden_set_path())\n"
        "\n\ndef _planted_wrapped_packaged_read() -> list:\n"
        "    return eval_core.load_golden_set(_golden_set_path())\n"
    ), encoding="utf-8")

    offenders, accepted = _golden_source_findings(
        fixture.read_text(encoding="utf-8"), rel)
    assert not offenders, (
        "the sanctioned golden-read shapes were flagged — pin (iv) would "
        "false-fail on legitimate code:\n"
        + "\n".join(f"  - {o}" for o in offenders)
    )
    assert len(accepted) >= 3, (
        "the detector did not recognise the planted sanctioned reads "
        f"(accepted={accepted!r}) — its resolution of "
        f"{SANCTIONED_PATH_HELPER}() regressed"
    )
