# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The figure-check evaluator's Lambda asset: staged, pure Python, stdlib only.

The check (``agentic_digital_thread/agents/eval_figure_check.py``) runs the
app's own figure reader (``agents/answer_figures.py``) inside an AWS Lambda.
The same app ships to a workshop where a deploy may not pip-install or pull a
build image, so the asset is the handler plus copies of exactly the app
modules the check imports, staged under ``cdk.out`` at synth
(``stacks/_staging.py``; the checkout is never written). Pinned here:

* the staged tree is the handler and ``FIGURE_CHECK_MODULES``, nothing else;
* every staged module imports only the standard library and other staged
  modules at module level;
* the staged tree runs with the standard library alone (``python -I -S``: no
  site-packages, so not even boto3) over plain-text, tabular, cut and
  malformed tool results, and loads no module from outside the stage.

Offline: file copies and a local subprocess.
"""
from __future__ import annotations

import ast
import json
import subprocess  # nosec B404 - runs this interpreter on a fixed local script
import sys
from pathlib import Path

import aws_cdk as cdk

from stacks._evaluations import FIGURE_CHECK_MODULES, stage_figure_check

REPO = Path(__file__).resolve().parents[1]


def _stage(tmp_path) -> Path:
    return Path(stage_figure_check(cdk.App(outdir=str(tmp_path / "cdk.out"))))


def test_the_staged_tree_is_the_handler_and_the_listed_modules(tmp_path):
    staged = _stage(tmp_path)
    files = sorted(str(p.relative_to(staged)) for p in staged.rglob("*") if p.is_file())
    assert files == sorted(["handler.py", *(f"agents/{m}.py" for m in FIGURE_CHECK_MODULES)])
    assert str(staged).startswith(str(tmp_path / "cdk.out"))
    assert not (REPO / "lambda-figure-check" / "agents").exists()


def test_staged_modules_import_only_the_stdlib_and_each_other(tmp_path):
    staged = _stage(tmp_path)
    found = []
    for path in sorted(staged.rglob("*.py")):
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            for name in _imported(node):
                if name.startswith("agents."):
                    ok = name.split(".", 1)[1] in FIGURE_CHECK_MODULES
                else:
                    ok = name.split(".")[0] in sys.stdlib_module_names or name == "__future__"
                if not ok:
                    found.append(f"{path.relative_to(staged)}: {name}")
    assert found == []


def _imported(node) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom):
        if node.level:
            if node.module:
                return [f"agents.{node.module}"]
            return [f"agents.{alias.name}" for alias in node.names]
        return [node.module or ""]
    return []


_SCRIPT = r'''
import json, sys
sys.path.insert(0, ".")
import handler
from agents import eval_figure_check as efc
out = {"name": efc.EVALUATOR_NAME, "results": [handler.handler(e, None) for e in json.load(sys.stdin)]}
std = set(sys.stdlib_module_names) | {"__main__", "handler", "agents"}
out["outside"] = sorted(m for m in sys.modules
                        if m.split(".")[0] not in std or (m.startswith("agents.")
                        and m.split(".", 1)[1] not in sys.argv[1].split(",")))
print(json.dumps(out))
'''

_TRACE = "0123456789abcdef0123456789abcdef"


def _spans(answer: str, texts: list) -> list[dict]:
    spans = [{"traceId": _TRACE, "spanId": "agent", "parentSpanId": "root",
              "name": "invoke_agent Strands Agents", "startTimeUnixNano": 1, "attributes": {},
              "events": [{"name": "gen_ai.user.message",
                          "attributes": {"content": json.dumps([{"text": "How many?"}])}},
                         {"name": "gen_ai.choice", "attributes": {"message": answer}}]}]
    for n, text in enumerate(texts):
        result = {"toolResult": {"toolUseId": f"t{n}", "status": "success",
                                 "content": [{"text": text}]}}
        spans += [{"traceId": _TRACE, "spanId": f"exec{n}", "parentSpanId": "agent",
                   "name": "execute_tool sys___q", "attributes": {"gen_ai.tool.call.id": f"t{n}"}},
                  {"traceId": _TRACE, "spanId": f"chat{n}", "parentSpanId": "agent",
                   "name": "chat", "attributes": {}, "events": [{
                       "name": "gen_ai.tool.message",
                       "attributes": {"content": json.dumps([result])}}]}]
    return spans


def _event(answer: str, texts: list) -> dict:
    return {"schemaVersion": "1.0", "evaluatorId": "e-1", "evaluatorName": "e",
            "evaluationLevel": "TRACE", "evaluationInput": {"sessionSpans": _spans(answer, texts)},
            "evaluationReferenceInputs": [], "evaluationTarget": {"traceIds": [_TRACE]}}


def _table(cut: bool) -> str:
    body = {"columns": ["zone", "n"], "rows": [["north", 1200], ["south", 800]],
            "row_count": 2, "truncated": cut,
            "_adt_executed_query": "SELECT zone, COUNT(*) AS n FROM t GROUP BY zone"}
    if cut:
        body["partial_reason"] = "row_limit"
    return json.dumps(body)


_EVENTS = [
    _event("Total records: 2,000 [1].",
           ["Zone north logged 1,200 records and zone south 800 records."]),
    _event("Total records: 2,000 [1].", [_table(cut=False)]),
    _event("Total records: 1,999 [1].", [_table(cut=True)]),
    _event("Nothing to count.", []),
    {"schemaVersion": "1.0"},
]


def test_the_staged_check_runs_on_the_standard_library_alone(tmp_path):
    staged = _stage(tmp_path)
    run = subprocess.run(  # nosec B603 - this interpreter, a fixed script, no shell
        [sys.executable, "-I", "-S", "-c", _SCRIPT, ",".join(FIGURE_CHECK_MODULES)],
        cwd=staged, input=json.dumps(_EVENTS), capture_output=True, text=True, timeout=120,
        check=False)
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout)
    assert out["outside"] == []
    labels = [r.get("label", r.get("errorCode")) for r in out["results"]]
    assert labels == ["PASS", "PASS", "FAIL", "NO_FIGURES", "VALIDATION_FAILED"]
    assert out["name"] == "adt_figure_check"


def test_the_lambda_handler_is_the_check():
    tree = ast.parse((REPO / "lambda-figure-check" / "handler.py").read_text(encoding="utf-8"))
    imports = [node for node in tree.body if isinstance(node, ast.ImportFrom)]
    assert [(i.module, [a.name for a in i.names]) for i in imports] == [
        ("__future__", ["annotations"]), ("agents.eval_figure_check", ["handler"])]
