# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A code-based AgentCore evaluator: the orchestrator's stated totals against
what its tools returned that turn.

AgentCore Evaluations scores sampled orchestrator traces with three LLM
judges (``stacks/_evaluations.py``). A judge grades an answer against the
agent's own findings, so it cannot see that a total is not the sum of what
the agents returned. In a manual review of real answers the orchestrator
added up per-code figures a system agent returned and got the sum wrong
(six figures summed to 4,899 when they add to 4,941), and stated totals no
tool result held. The engine's own figure check (``answer_figures``, run
inside each system agent over its query results) never sees the
orchestrator's arithmetic over the agents' prose. This module is the Lambda
body of the ``adt_figure_check`` evaluator, which measures how often that
happens.

**Contract** (https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/code-based-evaluators.html):
the input carries ``schemaVersion`` ``1.0``, ``evaluationLevel`` ``TRACE``,
``evaluationInput.sessionSpans`` (the session's OTEL span records, possibly
cut short past 6 MB) and ``evaluationTarget.traceIds`` (one trace: one
orchestrator turn). The answer is ``{label, value?, explanation}``, or
``{errorCode, errorMessage}`` for input this check cannot read: a malformed
or truncated payload is never a PASS.

**Where the text is** (measured on real spans of the target trace only):

* the final answer: the ``gen_ai.choice`` event of each top-level
  ``invoke_agent`` span, joined in start order (an output-cap continuation
  is a second agent invocation in the same trace);
* the turn's prompt: their ``gen_ai.user.message`` events. It carries the
  app's preamble (the route seed, metric-layer results, resolved time
  windows), so a figure handed to the orchestrator there is accounted for,
  the way a system agent's own question is;
* each tool result: the ``toolResult`` blocks of the ``gen_ai.tool.message``
  events on ``chat`` spans, by tool-use id, else the MCP result repr on the
  ``mcp tools/call`` span under the ``execute_tool`` span of that id. Every
  tool call the trace records (``execute_tool`` spans, ``toolUse`` blocks)
  must have a result, and every result must belong to a recorded call (an
  MCP result under a present ``execute_tool`` span, a tool message whose
  ``toolUseId`` a call has): either one missing means the input was cut
  short, never that the turn was read whole.

**The check** is the app's own reader: ``answer_figures`` reads the figures
the answer presents as totals or counts (``stated_figures``) and keeps the
ones no tool result accounts for (``unfound_figures``: a number in a result,
a sum or difference of two returned values, the sum of the figures the
answer lists next to it, a figure in the prompt). Four things are added
around it:

* counts stated with no total word: measured on real answers, the
  orchestrator writes most counts as ``N <count noun>`` (``4,941 SITE9 item
  records``, ``1 open order``), ``<label naming a count noun>: N``
  (``CLOSED orders: 8,182``) or under a multi-word count header
  (``Record Count``), which
  ``stated_figures`` reads only under a total, sum or count word or a
  one-word count header, so a wrong figure in them passed. :func:`read_figures`
  reads those shapes too (:func:`_count_shapes`), with the reader's own
  pieces: its count nouns (``_COUNT_NOUNS``), number spelling (``_NUM_BARE``,
  ``_AFTER``: no percentage, decimal, duration or time), label reader
  (``_LABELLED``), table reader (``_row_cells``, ``_column_kinds``), skip
  words (average, rate, share, change, in the figure's sentence or label) and
  identifier/date/time words (``_NOT_A_COUNT``, before the figure or in a
  label's head), plus an approximation, sign and per-unit-rate filter. Such a
  figure must pass the same support tests (:func:`_unfound_counts`), past a
  cut the cut rules too. The engine's in-app check (the same reader inside
  each system agent) has the same blind spot; widening ``stated_figures``
  changes what live answers carry, so it is a follow-up with its own live
  check;
* a pair holds a figure only from counts: system-agent results are prose,
  so every number in them (a percentage, a line or station number, minutes,
  a small count) was a pair operand, and a figure off by any such number
  passed (on real turns, a wrong total passed as the right one plus a
  "42%" written in the same result).
  Percentages are blanked from tool results (:func:`_result_text`), and the
  reader sees a view of the values (:func:`_pair_view`) whose pair operands
  are only the counts the results state: a structured result's COUNT/SUM
  cells and their sums, count-named fields, the figures :func:`read_figures`
  reads in a result's prose, and the engine's totals lines; never a figure an
  engine line flagged. Direct support (any number in a result, the prompt, a
  listed sum) is unchanged, except that a list's length (the reader's
  ``figure <= rows`` rule) holds nothing. The reader's own rules, and the
  engine, are unchanged;
* ids never vouch for a figure: before the reader sees a tool result or the
  prompt, every UUID and every token that mixes letters and digits
  (``WO-4941``, ``7e641``, a hex query id) is blanked, keeping a number with
  a short unit (``12h``, ``3.5k``) and an ISO date-time; the reader would
  otherwise take ``4941`` out of ``WO-4941`` or a UUID's all-digit group;
* a figure the answer states that an engine line in a tool result already
  named as in no result (``answer_figures.FIGURE_PREFIX`` /
  ``TIED_PREFIX``; the system agent neutralizes a model-written copy of
  those prefixes) fails, although that line writes the figure.

**Labels.** ``PASS`` (1.0) every stated total or count is accounted for;
``FAIL`` (0.0) at least one is not, or was flagged; ``NO_FIGURES`` (no
value, so it does not move the average) the answer states none;
``UNCHECKED`` (no value) it states some but there is nothing whole to
compare them with (no tool result, a result the reader cannot read whole,
too many abbreviated figures in the prompt, or a cut result that left
nothing unaccounted: past a cut the reader checks prose totals only).

**What it cannot check.** It compares the answer with what the agents
returned, never with the data: it re-queries nothing, so an answer whose
agents were wrong in the same way passes, and a figure the reader does not
read (a prose sum outside its shapes, a figure the answer quotes without a
count noun) is not checked. A count a result writes without its own count
noun (``1,200 and 800 records``: only 800 is read) is no pair operand, so a
correct sum of such counts fails unless the answer lists them beside it;
and where a result lists many small counts, a figure off by a small amount
can still be the sum or difference of two of them. One Lambda run per
sampled trace, no model call. Stdlib only, like the modules it imports.
"""
from __future__ import annotations

import ast
import json
import logging
import re
from decimal import Decimal

from . import answer_figures
from .tool_result_capture import _CUT_SCAN_CHARS

logger = logging.getLogger(__name__)

#: The evaluator's name (CFN ``EvaluatorName`` pattern
#: ``^[a-zA-Z][a-zA-Z0-9_]{0,47}$``); the Evals page finds its results by it.
EVALUATOR_NAME = "adt_figure_check"
SCHEMA_VERSION = "1.0"
LEVEL = "TRACE"
PASS, FAIL, NO_FIGURES, UNCHECKED = "PASS", "FAIL", "NO_FIGURES", "UNCHECKED"
LABELS = (PASS, FAIL, NO_FIGURES, UNCHECKED)
INVALID = "VALIDATION_FAILED"
INCOMPLETE = "TRACE_INCOMPLETE"
#: The most unaccounted figures one explanation names.
MAX_NAMED = 3
#: The longest tool result text the reader reads whole (the engine's bound).
_MAX_RESULT_CHARS = _CUT_SCAN_CHARS
_COMPARED = (" This check compares the answer with what the system agents returned this "
             "turn, not with the data.")
_HELD = ("in a tool result of this turn or the prompt, a sum or difference of two counts the "
         "results state, or the sum of the figures the answer lists next to it")


class InputError(ValueError):
    """Input this check cannot read; ``result`` is the documented error shape."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.result = {"errorCode": code, "errorMessage": message}


def handler(event, _context=None) -> dict:
    """The Lambda entry point. An unexpected failure answers the error shape
    with its class name only; the traceback goes to this function's log."""
    try:
        return evaluate(event)
    except Exception as exc:  # the documented error shape, never a raised Lambda error
        logger.exception("figure check failed")
        return {"errorCode": "EVALUATOR_ERROR",
                "errorMessage": f"the figure check failed ({type(exc).__name__}); "
                                "see the evaluator's Lambda log"}


def evaluate(event) -> dict:
    """One trace's verdict (module docstring), or the error shape."""
    try:
        spans = _target_spans(event)
        answer, question = _answer_and_prompt(spans)
        stated = read_figures(answer)
        if not stated:
            return {"label": NO_FIGURES, "explanation": (
                "The answer states no total or count this check reads, so there is nothing "
                "to compare." + _COMPARED)}
        return _verdict(answer, _without_ids(question), stated, _tool_results(spans))
    except InputError as exc:
        return exc.result


# --------------------------------------------------------------------------- #
# The payload and the target trace
# --------------------------------------------------------------------------- #
def _target_spans(event) -> list[dict]:
    """The target trace's span records, each checked for the fields read."""
    if not isinstance(event, dict):
        raise InputError(INVALID, "the payload is not a JSON object")
    if event.get("schemaVersion") != SCHEMA_VERSION:
        raise InputError(INVALID, f"schemaVersion must be {SCHEMA_VERSION}")
    if event.get("evaluationLevel") != LEVEL:
        raise InputError(INVALID, f"this evaluator scores evaluationLevel {LEVEL} only")
    given = event.get("evaluationInput")
    spans = given.get("sessionSpans") if isinstance(given, dict) else None
    if not isinstance(spans, list):
        raise InputError(INVALID, "evaluationInput.sessionSpans must be a list")
    target = event.get("evaluationTarget")
    trace_ids = target.get("traceIds") if isinstance(target, dict) else None
    if not (isinstance(trace_ids, list) and len(trace_ids) == 1
            and isinstance(trace_ids[0], str) and trace_ids[0]):
        raise InputError(INVALID, "evaluationTarget.traceIds must hold exactly one trace id")
    if not all(isinstance(span, dict) for span in spans):
        raise InputError(INVALID, "a session span is not a JSON object")
    own = [span for span in spans if span.get("traceId") == trace_ids[0]]
    if not own:
        raise InputError(INCOMPLETE, "the session spans hold no span of the target trace")
    for span in own:
        _check_span(span)
    return own


def _check_span(span: dict) -> None:
    if not isinstance(span.get("name"), str):
        raise InputError(INVALID, "a span of the target trace has no name")
    if not isinstance(span.get("attributes", {}), dict):
        raise InputError(INVALID, "a span's attributes are not an object")
    events = span.get("events", [])
    if not (isinstance(events, list) and all(
            isinstance(e, dict) and isinstance(e.get("attributes", {}), dict) for e in events)):
        raise InputError(INVALID, "a span's events are not a list of objects")


def _events(span: dict, name: str):
    for event in span.get("events", []):
        if event.get("name") == name:
            yield event.get("attributes", {})


def _start(span: dict) -> int:
    value = span.get("startTimeUnixNano")
    if isinstance(value, int):
        return value
    return int(value) if isinstance(value, str) and value.isdigit() else 0


def _answer_and_prompt(spans: list[dict]) -> tuple[str, str]:
    """The turn's final answer and prompt from its top-level agent spans."""
    by_id = {span.get("spanId"): span for span in spans}
    agents = sorted((span for span in spans if span["name"].startswith("invoke_agent")
                     and not _under_agent(span, by_id)), key=_start)
    if not agents:
        raise InputError(INCOMPLETE, "the target trace has no invoke_agent span")
    answers = [_text_of(attrs.get("message")) for span in agents
               for attrs in _events(span, "gen_ai.choice")]
    prompts = [_text_of(attrs.get("content")) for span in agents
               for attrs in _events(span, "gen_ai.user.message")]
    answer = "\n\n".join(a for a in answers if a)
    if not answer:
        raise InputError(INCOMPLETE, "the target trace's agent span holds no final answer")
    return answer, "\n\n".join(p for p in prompts if p)


def _under_agent(span: dict, by_id: dict) -> bool:
    """``span`` has an ``invoke_agent`` ancestor inside the trace."""
    parent, steps = by_id.get(span.get("parentSpanId")), 0
    while parent is not None and steps < len(by_id):
        if parent["name"].startswith("invoke_agent"):
            return True
        parent, steps = by_id.get(parent.get("parentSpanId")), steps + 1
    return False


def _text_of(value) -> str:
    """A message attribute's text: a JSON list of content blocks (their
    ``text``) or the text itself."""
    if not isinstance(value, str):
        return ""
    blocks = _parsed(value)
    if isinstance(blocks, list):
        return "\n".join(b["text"] for b in blocks if isinstance(b, dict)
                         and isinstance(b.get("text"), str))
    return value


def _parsed(value: str):
    try:
        return json.loads(value)
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# The turn's tool results
# --------------------------------------------------------------------------- #
def _tool_results(spans: list[dict]) -> list[list]:
    """Each tool call's result content (a list of content items): every call
    the trace records answered by one, and every result read belonging to a
    call the trace records. AgentCore may cut ``sessionSpans`` short, so a
    result whose call is missing (an MCP result with no ``execute_tool``
    parent span, a tool message whose ``toolUseId`` no call has), a call with
    no result and a cut result are each an incomplete input, never evidence."""
    results, wanted = _chat_results(spans)
    for key, content in _mcp_results(spans).items():
        if key.startswith(_NO_CALL):
            raise InputError(INCOMPLETE, "an MCP tool result has no execute_tool span of its "
                                         "call in the trace (input cut short?)")
        if key in results:
            continue
        if content is None:
            raise InputError(INCOMPLETE, "an MCP tool result is cut short and no chat span "
                                         "holds it")
        results[key] = content
    unrecorded = set(results) - wanted
    if unrecorded:
        raise InputError(INCOMPLETE, f"{len(unrecorded)} tool result(s) of the target trace "
                                     "belong to no call the spans record (input cut short?)")
    missing = wanted - set(results)
    if missing:
        raise InputError(INCOMPLETE, f"{len(missing)} tool call(s) of the target trace have no "
                                     "result in the spans (input cut short?)")
    return list(results.values())


def _chat_results(spans: list[dict]) -> tuple[dict, set]:
    """``({tool-use id: content}, every tool-use id the trace records)``."""
    results, wanted = {}, set()
    for span in spans:
        if span["name"].startswith("execute_tool"):
            tool_id = span.get("attributes", {}).get("gen_ai.tool.call.id")
            if isinstance(tool_id, str):
                wanted.add(tool_id)
        if span["name"] != "chat" and not span["name"].startswith("chat "):
            continue
        for attrs in _events(span, "gen_ai.tool.message"):
            for result in _blocks(attrs.get("content"), "toolResult", required=True):
                tool_id, content = result.get("toolUseId"), result.get("content")
                if not (isinstance(tool_id, str) and isinstance(content, list)):
                    raise InputError(INCOMPLETE, "a chat span's tool result has no id or content")
                results.setdefault(tool_id, content)
        for name, key in (("gen_ai.choice", "message"), ("gen_ai.assistant.message", "content")):
            for attrs in _events(span, name):
                wanted.update(use["toolUseId"] for use in _blocks(attrs.get(key), "toolUse")
                              if isinstance(use.get("toolUseId"), str))
    return results, wanted


def _blocks(value, kind: str, required: bool = False) -> list[dict]:
    """The ``kind`` blocks of a JSON list of content blocks. A tool message
    that does not parse is an error (``required``); other messages may be
    plain text."""
    blocks = _parsed(value) if isinstance(value, str) else None
    if not isinstance(blocks, list):
        if required:
            raise InputError(INCOMPLETE, "a chat span's tool message is not a JSON list "
                                         "(cut short?)")
        return []
    return [b[kind] for b in blocks if isinstance(b, dict) and isinstance(b.get(kind), dict)]


_REPR_TEXT = re.compile(r"\btext=('(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\")", re.S)
_REPR_ITEM = re.compile(r"\bTextContent\(")
#: A whole ``CallToolResult`` repr ends on its last field.
_REPR_END = re.compile(r"\bisError=(?:True|False)\s*$")
#: The key of an MCP result whose call span (its ``execute_tool`` parent with
#: a tool-use id) is not in the trace.
_NO_CALL = "no-call:"


def _mcp_results(spans: list[dict]) -> dict:
    """``{tool-use id (else _NO_CALL + span id): content or None when cut}``
    from the MCP client spans' result reprs, each keyed by its parent
    ``execute_tool`` span's tool-use id."""
    by_id = {span.get("spanId"): span for span in spans}
    out = {}
    for span in spans:
        value = span.get("attributes", {}).get("gen_ai.tool.call.result")
        if not (span["name"].startswith("mcp tools/call") and isinstance(value, str)):
            continue
        parent = by_id.get(span.get("parentSpanId")) or {"name": ""}
        tool_id = parent.get("attributes", {}).get("gen_ai.tool.call.id")
        called = parent["name"].startswith("execute_tool") and isinstance(tool_id, str)
        key = tool_id if called else f"{_NO_CALL}{span.get('spanId')}"
        texts = _repr_texts(value)
        out[key] = None if texts is None else [{"text": text} for text in texts]
    return out


def _repr_texts(value: str) -> list[str] | None:
    """The texts of an MCP ``CallToolResult`` repr; ``None`` when it is cut."""
    literals = _REPR_TEXT.findall(value)
    if not _REPR_END.search(value) or len(literals) != len(_REPR_ITEM.findall(value)):
        return None
    try:
        texts = [ast.literal_eval(literal) for literal in literals]
    except (ValueError, SyntaxError):
        return None
    return texts if all(isinstance(t, str) for t in texts) else None


# --------------------------------------------------------------------------- #
# Ids never vouch for a figure
# --------------------------------------------------------------------------- #
_UUID = re.compile(r"(?<![0-9A-Za-z])[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                   r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}(?![0-9A-Za-z])")
_TOKEN = re.compile(r"[A-Za-z0-9_]+(?:-[A-Za-z0-9_]+)*")
#: Letter-and-digit tokens that are not ids: a number with a short unit the
#: reader reads (``answer_figures._UNIT``) and an ISO date-time's day-and-hour.
_NOT_AN_ID = re.compile(r"\d+(?:ms|secs?|s|mins?|m|hrs?|h|k|K|M)|\d{4}-\d{2}-\d{2}T\d{2}")
_LETTER = re.compile(r"[A-Za-z]")
_DIGIT = re.compile(r"\d")
#: The orchestrator's citation stamp on a tool result (``citation_ordinals``,
#: which imports strands, so its pattern is restated here and parity-tested):
#: an ordinal, not data, and ``figure +- 1`` would otherwise pass.
_STAMP = re.compile(
    r"\[engine\] (?:Cite this result as \[\d{1,4}\]"
    r"|Returned for an earlier question in this chat\. Cite it as \[E\d{1,2}\])\.")


def _without_ids(text: str) -> str:
    """``text`` with every UUID and every letter-and-digit token blanked."""
    return _TOKEN.sub(_id_blank, _UUID.sub(" ", text))


#: A percentage, as a result writes one (``42%``, ``42 %``, ``40-42%``,
#: ``42 percent``, ``3.5 per cent``, ``42 pct``, ``(42%)``): never a count,
#: and never read as a stated figure, so it is no value and no pair operand.
_PERCENT_NUMBER = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
_PERCENTAGE = re.compile(rf"(?<![\w.,])[-+]?(?:{_PERCENT_NUMBER}\s*[-\u2013]\s*)?"
                         rf"{_PERCENT_NUMBER}(?:\s*%|{answer_figures._PERCENT})")


def _result_text(text: str) -> str:
    """A tool result's text as the reader sees it: percentages and ids blanked."""
    return _without_ids(_PERCENTAGE.sub(" ", text))


def _id_blank(match) -> str:
    token = match.group(0)
    if _NOT_AN_ID.fullmatch(token) or not (_LETTER.search(token) and _DIGIT.search(token)):
        return token
    return " "


def _without_ids_json(node):
    """A JSON value with every string (keys too) blanked of percentages and ids."""
    if isinstance(node, dict):
        return {_result_text(str(k)): _without_ids_json(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_without_ids_json(v) for v in node]
    return _result_text(node) if isinstance(node, str) else node


def _masked(item):
    """One content item as the reader sees it: the citation stamp dropped,
    percentages and ids blanked, a JSON text kept JSON so its table is still
    read as a table."""
    if not isinstance(item, dict):
        return item
    if isinstance(item.get("text"), str):
        text = _STAMP.sub(" ", item["text"])
        if text.strip()[:1] in ("[", "{") and len(text) <= _MAX_RESULT_CHARS:
            body = _parsed(text)
            if isinstance(body, (dict, list)):
                return {"text": json.dumps(_without_ids_json(body))}
        return {"text": _result_text(text)}
    if "json" in item:
        return {"json": _without_ids_json(item["json"])}
    return item


# --------------------------------------------------------------------------- #
# Counts stated without a total word
# --------------------------------------------------------------------------- #
_AF = answer_figures


def read_figures(answer) -> list[str]:
    """The figures ``answer`` states as totals or counts: the app reader's
    (``answer_figures._stated``) and the count shapes it does not read under a
    total word (``answer_figures._count_shapes``), in order of first occurrence,
    each once. The reader now lives in ``answer_figures``, shared with the
    in-app check; the evaluator reads every count shape (its support view
    decides naming), so this merge applies no in-app subset-scope filter and
    its result is unchanged."""
    lines = _AF._lines(answer)
    stated = _AF._stated(lines)
    counts = _AF._count_shapes(lines, {spelling for spelling, _i in stated})
    return [spelling for spelling, _i in sorted(stated + counts, key=lambda item: item[1][0])]


def _count_shapes(lines, known, limit=_AF.MAX_FIGURES):
    """The count-shape reader, now owned by ``answer_figures`` and shared with
    the in-app check (:func:`answer_figures._count_shapes`). Kept here as a thin
    delegator because the measure tools import it by this name."""
    return _AF._count_shapes(lines, known, limit)


def _unfound_counts(answer: str, values: dict, question: str) -> list[str]:
    """The count-shape figures nothing accounts for, by the reader's own
    support tests (:func:`answer_figures.unfound_count_shapes`). A thin
    delegator, kept for the measure tools that import it by this name; the
    reader itself is shared with the in-app check."""
    return _AF.unfound_count_shapes(answer, values, question)


def _count_view(values: dict) -> tuple[dict | None, list | None]:
    """Delegator to :func:`answer_figures._count_view`."""
    return _AF._count_view(values)


# --------------------------------------------------------------------------- #
# A pair holds a figure only from counts the results state
# --------------------------------------------------------------------------- #
_VIEW_KEYS = ("raw", "data", "derived")
_AGGREGATE = re.compile(r"\b(?:COUNT|SUM)\s*\(", re.I)
#: Where an engine totals line's entries start: the totals line itself, and
#: the totals part of a figure line (``answer_figures.figure_line``).
_TOTALS_LEADS = (_AF.TOTALS_PREFIX, "Engine totals, in this turn's queries numbered as above: ")
#: One engine total: the figure after its aggregate (``n = COUNT(*) 1,157``)
#: or its column name (``late_n 5``), ending its item.
_ENGINE_TOTAL = re.compile(rf"(?:\)|\b[A-Za-z_][\w.$]*) {_AF._NUM_BARE}(?=[,;.]?(?:\s|$)| \()")


def _pair_view(values: dict | None, results: list[list], flagged=frozenset()) -> dict | None:
    """``values`` whose pair operands are only the counts the results state
    (:func:`_stated_counts`), so a figure held ONLY by a sum or difference of
    two returned values passes only when both are counts; a figure an engine
    line named as in no result (``flagged``) is no count. Direct support is
    kept: every other value stays a value (``raw``), a non-integer one still
    rounds (``derived``). The reader's row-count rule (a figure no larger than
    the rows and list elements the results returned) is off (``rows`` 0): a
    list's length is no count a result states, so it never holds a figure.
    Past a cut, the complete view's operands come from the complete results
    only, as the reader keeps that view."""
    if values is None:
        return None
    from .tool_result_capture import result_row_cut
    every, whole = set(), set()
    for content in results:
        masked = [_masked(item) for item in content]
        counts = _stated_counts(masked) - set(flagged)
        every |= counts
        if not result_row_cut({"content": masked})[0]:
            whole |= counts
    view = _with_count_pairs(values, every)
    complete = values.get("complete")
    if isinstance(complete, dict) and all(isinstance(complete.get(k), set) for k in _VIEW_KEYS):
        view["complete"] = _with_count_pairs(complete, whole)
    return view


def _with_count_pairs(view: dict, counts: set) -> dict:
    data = view["data"]
    recorded = view["raw"] | data | view["derived"]
    return {**view, "data": {c for c in counts if c in recorded}, "raw": view["raw"] | data,
            "derived": view["derived"] | {v for v in data if v != v.to_integral_value()},
            "rows": 0}


def _stated_counts(content: list) -> set:
    """The counts one tool result states: a structured result's count cells
    (:func:`_json_counts`); in prose, every figure the evaluator reads as a
    total or count (:func:`_operand_figures`) and the engine's totals
    (:func:`_engine_totals`)."""
    out: set = set()
    for item in content:
        if not isinstance(item, dict):
            continue
        text, body = item.get("text"), item.get("json")
        if isinstance(text, str) and text.strip()[:1] in ("[", "{"):
            body = _parsed(text)
        if isinstance(body, (dict, list)):
            out |= _json_counts(body)
        elif isinstance(text, str):
            out |= _operand_figures(text) | _engine_totals(text)
    return out


def _operand_figures(text: str) -> set:
    """What :func:`read_figures` reads in ``text``, without its budgets (each
    line read on its own, tables whole) and without lower bounds."""
    lines = _AF._lines(text)
    spellings = {s for line in lines for s, _i in _AF._stated([line])}
    spellings |= {s for s, _i in _AF._table_figures(lines)}
    spellings |= {s for s, _i in _count_shapes(lines, set(), limit=None)}
    spellings -= _AF._lower_bound_spellings(lines)
    return {Decimal(s.replace(",", "")) for s in spellings}


def _engine_totals(text: str) -> set:
    """The totals an engine totals line in ``text`` states (COUNT or SUM
    results, so counts), read after the line's lead only."""
    out: set = set()
    for line in text.splitlines():
        for lead in _TOTALS_LEADS:
            at = line.find(lead)
            if at >= 0:
                out.update(Decimal(m.group(1).replace(",", ""))
                           for m in _ENGINE_TOTAL.finditer(line, at + len(lead)))
    return out


def _json_counts(node) -> set:
    """The counts a structured result states: in a table (``columns`` and
    ``rows``), each COUNT or SUM column's cells and their sum
    (:func:`_table_counts`); elsewhere, a number under a key that names a
    count noun or a total (:func:`_count_head`). Bounded like the reader's
    JSON walk."""
    out, stack, budget = set(), [(None, node)], _AF._MAX_JSON_NODES
    while stack and budget > 0:
        budget -= 1
        key, value = stack.pop()
        if isinstance(value, dict) and isinstance(value.get("columns"), list) \
                and isinstance(value.get("rows"), list):
            out |= _table_counts(value)
        elif isinstance(value, dict):
            stack.extend((k, v) for k, v in value.items() if not str(k).startswith("_adt_"))
        elif isinstance(value, list):
            stack.extend((key, v) for v in value)
        elif key is not None and _count_head(str(key)):
            cell = _AF._column([[value]], 0)
            if cell and cell[0] is not None:
                out.add(cell[0])
    return out


def _table_counts(payload: dict) -> set:
    """A table's count cells: each column whose SELECT item is a COUNT or SUM
    (``_select_texts``), or, without a statement that splits into its
    columns, whose name names a count noun or a total; its cells and its sum."""
    columns, rows = payload["columns"], payload["rows"]
    sql = payload.get("_adt_executed_query")
    items = _AF._select_texts(sql, len(columns)) if isinstance(sql, str) else None
    if any(not isinstance(row, list) or len(row) != len(columns) for row in rows):
        return set()
    out: set = set()
    for index, name in enumerate(columns):
        counted = (bool(items[index] and _AGGREGATE.search(items[index])) if items
                   else _count_head(str(name)))
        cells = _AF._column(rows, index) if counted else None
        if cells:
            values = [cell for cell in cells if cell is not None]
            out.update(values)
            out.add(sum(values, Decimal(0)))
    return out


def _count_head(name: str) -> bool:
    """A column or field name that names a count (``record_count``,
    ``failed_records``) or a total (``total``)."""
    return _AF._count_label(name) or _AF._label_ok(name)


# --------------------------------------------------------------------------- #
# The verdict
# --------------------------------------------------------------------------- #
def _verdict(answer: str, question: str, stated: list[str], results: list[list]) -> dict:
    flags = _flagged(results)
    values = _pair_view(_values(results), results, flags)
    reason = _unchecked_reason(values, question)
    flagged = [s for s in stated if Decimal(s.replace(",", "")) in flags]
    unfound = [] if reason else [spelling for spelling, _sums in
                                 answer_figures.unfound_figures(answer, values, [], question,
                                                                read_counts=False)]
    if not reason:
        unfound += _unfound_counts(answer, values, question)
    named = [s for s in stated if s in set(unfound) | set(flagged)]
    if named:
        return {"label": FAIL, "value": 0.0,
                "explanation": _fail_text(len(stated), named, unfound, flagged)}
    if reason is None and values.get("cut"):
        reason = ("a tool result was cut short, so only totals written in prose were checked, "
                  "and none of those was unaccounted for")
    one = len(stated) == 1
    if reason:
        return {"label": UNCHECKED, "explanation": (
            f"The answer states {_count(len(stated))}, but {reason}, so "
            f"{'it was' if one else 'they were'} not compared." + _COMPARED)}
    return {"label": PASS, "value": 1.0, "explanation": (
        (f"The answer's one total or count is accounted for: it is {_HELD}." if one else
         f"All {_count(len(stated))} the answer states are accounted for: each is {_HELD}.")
        + _COMPARED)}


def _values(results: list[list]) -> dict | None:
    """The reader's record of every number the turn's tool results returned."""
    state: dict = {}
    for content in results:
        answer_figures.record_result_values(state, {"content": [_masked(i) for i in content]})
    return answer_figures.result_values(state)


def _unchecked_reason(values, question: str) -> str | None:
    if values is None or not values.get("results"):
        return "this turn has no tool result to compare with"
    if values.get("unread"):
        return ("a tool result could not be read whole (too long, in too many parts, or "
                "holding too many numbers)")
    if answer_figures.question_ranges(question) is None:
        return "the prompt holds more abbreviated figures than the check reads"
    return None


def _count(n: int) -> str:
    return f"{n} total{'' if n == 1 else 's'} or count{'' if n == 1 else 's'}"


def _listed(figures: list[str]) -> str:
    shown = figures[:MAX_NAMED]
    more = len(figures) - len(shown)
    return ", ".join(shown) + (f" and {more} more" if more else "")


def _fail_text(stated: int, named: list[str], unfound: list[str], flagged: list[str]) -> str:
    """Which stated figures failed, at most ``MAX_NAMED`` named per list."""
    lead = "it is" if stated == 1 else f"{len(named)} {'is' if len(named) == 1 else 'are'}"
    text = f"The answer states {_count(stated)}; {lead} unaccounted for: {_listed(named)}."
    if any(s not in flagged for s in unfound):
        text += f" A figure is accounted for when it is {_HELD}."
    if flagged:
        text += (f" {_listed(flagged)} {'was' if len(flagged) == 1 else 'were'} already named "
                 "as in no result by a system agent's own figure check.")
    return text + _COMPARED


# --------------------------------------------------------------------------- #
# Figures an engine line already named
# --------------------------------------------------------------------------- #
_SPELLING = r"(?:\d{1,3}(?:,\d{3})+|\d+)"
_FLAGGED_LIST = re.compile(
    rf"(?:^|\. )({_SPELLING}(?:(?:, | and ){_SPELLING})*)(?: and \d+ more)? in this answer ")
#: An engine figure line is never longer than the executed-filters line.
_MAX_FLAG_LINE = 4_000


def _flagged(results: list[list]) -> set:
    """The values the engine's figure lines in the turn's tool results name."""
    out: set = set()
    for content in results:
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                out |= _flagged_in(item["text"])
    return out


def _flagged_in(text: str) -> set:
    """The figures each ``[engine] Figures in no result`` line of ``text``
    names (``answer_figures._figure_head``: the list before ``in this
    answer`` in each of its sentences; a line that only counts its figures
    names none)."""
    out: set = set()
    for prefix in (answer_figures.FIGURE_PREFIX, answer_figures.TIED_PREFIX):
        at = text.find(prefix)
        while at >= 0:
            line = text[at + len(prefix):].split("\n", 1)[0][:_MAX_FLAG_LINE]
            for match in _FLAGGED_LIST.finditer(line):
                out.update(Decimal(s.replace(",", ""))
                           for s in re.findall(_SPELLING, match.group(1)))
            at = text.find(prefix, at + len(prefix))
    return out
