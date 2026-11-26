# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A record name the answer gives that only the schema's example values hold.

Live run on a rehearsal account (2026-10-18, Quick ask exact-6): a
query was refused, and the ERP system agent was left with a batch's shipment
rows that held customer row ids and line values but no customer name. Its
answer named a customer for each row anyway. Two of the three names were the
first two example values its baked schema shows for the customer name
column, in that order; the third was in no result either. No tool result in
the call returned any customer name, the orchestrator relayed the names as
data, and one real customer's figure went to the wrong name. The prompt
already says a value comes from the call's own results, never from the
schema's example values (ROW FIGURES, RECORD IDENTIFIERS); this checks it.
The answer is never changed: names it gives that no result returned reach
the orchestrator as one fixed engine line (:func:`engine_line`), which says
not to state them as data.

* :func:`schema_names` reads the name-shaped example values of the baked
  schema's text columns (``name: type e.g. v1, v2, v3`` as
  ``schema_format.format_schema_with_samples`` renders them). A name-shaped
  value has two or more words, each starting with a capital letter or a
  digit (a few joining words such as ``of`` and ``&`` aside), and at least
  two words with letters. A value cut at the sample length cap, or redacted,
  is skipped: the answer cannot hold it whole.
* :func:`record_result` reads each of THIS call's tool results as it
  arrives (called by the capture hook,
  ``tool_result_capture.InvocationToolResultCapture``, with the agent's
  schema names) and keeps, in the call's own invocation state, which of
  those names any returned data string holds as whole words (case and
  spacing folded). The data of a result is: for a tabular result
  (``columns`` and ``rows``, at any depth) its rows, all of whose keys and
  values count; for any other JSON every key and every value except keys
  starting with ``_adt``, which the engine adds; for plain text the whole
  text; for an error result (``status`` error) nothing. So a lookup that
  found no row proves nothing, though its executed query names what it
  looked for.
* The record holds proven names, possibly returned names and a cut count,
  never result text, so it is bounded by the schema's example set however
  many results the call returns, and it spans the first pass and the
  elicitation retry and nothing else (the agent is a long-lived singleton
  whose transcript holds earlier calls, and strands' sliding window trims
  that transcript from the front, so it is not this call's evidence). A
  result too large to read whole (past :data:`JSON_NODE_BUDGET` nodes or
  :data:`MAX_RESULT_CHARS` characters) proves the names in the part read; a
  name its raw text holds anywhere else may be in the unread part, so it is
  kept as possibly returned and never flagged. Names already proven stay
  proven.
* :func:`unreturned_names` lists the example names the answer writes (whole
  words, case and spacing folded, whatever word follows) that no result
  proved or possibly returned, unless the delegated question (with its
  appended context) names them the same way.

The contract is conservative: the line fires only when a name is in no
returned data value at all, and every ambiguity resolves toward no line.
Accepted misses (no line where one might be right):

* a non-tabular result that echoes the looked-up name outside an ``_adt``
  key, such as a wrapped search API's ``query`` field, or an engine refusal
  whose note names the value it refused;
* a different record whose value contains the example name as whole words
  (``<name> West`` returned proves ``<name>``);
* a name the agent made up outright: a value the schema does not show as an
  example is never checked.

Stdlib only, no dataset knowledge.
"""
from __future__ import annotations

import functools
import json
import re
from typing import Any, Iterable

#: Attribute the system agent factory sets on the agent: the tuple of
#: name-shaped example values of its baked schema.
SCHEMA_NAMES_ATTR = "_adt_schema_names"
#: Invocation-state key for the names the answer gives that no result returned.
STATE_KEY = "_adt_unreturned_names"
#: Opening words of the engine line.
MARKER = "[engine] Names in no result:"
#: Most names one line quotes.
MAX_NAMES = 5
#: Invocation-state key of what this call's results proved.
RESULT_NAMES_STATE_KEY = "_adt_answer_name_results"
#: JSON nodes read per result; past it the rest of that result is unread.
JSON_NODE_BUDGET = 200_000
#: Characters of one result's text read; past it the rest is unread.
MAX_RESULT_CHARS = 4_000_000
_TEXT_TYPE = re.compile(
    r"^(character varying|varchar|text|character|char|bpchar|citext|nvarchar|nchar|string)"
    r"(\s*\(\s*\d+\s*\))?$", re.IGNORECASE)
_FRAGMENT = re.compile(r"^\s*([^:]+?)\s*:\s*(.+?)\s+e\.g\.\s+(.+?)\s*$", re.S)
_JOINING = frozenset({"of", "and", "the", "for", "de", "&", "-"})
_SPACE = re.compile(r"\s+")


def _name_shaped(value: str) -> bool:
    if not value or value.endswith("\u2026") or "[REDACTED" in value.upper():
        return False
    words = value.split()
    if len(words) < 2:
        return False
    lettered = 0
    for word in words:
        if word.lower() in _JOINING:
            continue
        if not (word[0].isupper() or word[0].isdigit()):
            return False
        if any(ch.isalpha() for ch in word):
            lettered += 1
    return lettered >= 2


def schema_names(entries: Iterable) -> tuple[str, ...]:
    """Name-shaped example values of the text columns in ``entries``.

    ``entries`` yields ``(table, column_fragments, ...)`` as
    ``system_agent._desc_entries`` does. Order kept, duplicates dropped.
    """
    found: list[str] = []
    for entry in entries or ():
        try:
            fragments = entry[1]
        except (TypeError, IndexError):
            continue
        for fragment in fragments or ():
            match = _FRAGMENT.match(str(fragment))
            if not match or not _TEXT_TYPE.match(match.group(2).strip()):
                continue
            for value in match.group(3).split(", "):
                value = value.strip()
                if _name_shaped(value) and value not in found:
                    found.append(value)
    return tuple(found)


@functools.lru_cache(maxsize=1024)
def _name_pattern(name: str) -> "re.Pattern | None":
    """The name as whole words, case and spacing folded."""
    words = str(name).split()
    if not words:
        return None
    return re.compile(r"(?<!\w)" + r"\s+".join(re.escape(w) for w in words) + r"(?!\w)",
                      re.IGNORECASE)


@functools.lru_cache(maxsize=1024)
def _key_pattern(key: str) -> "re.Pattern":
    """A folded key as whole words in folded text."""
    return re.compile(r"(?<!\w)" + re.escape(key) + r"(?!\w)")


def _find(name: str, text: Any):
    pattern = _name_pattern(name)
    return pattern.search(text) if pattern and isinstance(text, str) else None


def _key(name: str) -> str:
    return " ".join(str(name).split()).casefold()


def _fold(text: str) -> str:
    """Spacing collapsed, case folded, ends kept."""
    return _SPACE.sub(" ", text).casefold()


def _data_strings(payload: Any, out: list[str], budget: list[int], numbers: bool = False) -> bool:
    """Append the data strings of one parsed payload (see the module notes),
    and with ``numbers`` each number's text too; ``False`` when ``budget``
    ran out first."""
    stack: list[tuple[Any, bool]] = [(payload, False)]
    while stack:
        if budget[0] <= 0:
            return False
        budget[0] -= 1
        node, rows = stack.pop()
        if isinstance(node, str):
            out.append(node)
        elif numbers and isinstance(node, (int, float)) and not isinstance(node, bool):
            out.append(str(node))
        elif isinstance(node, dict):
            if not rows and isinstance(node.get("columns"), list) \
                    and isinstance(node.get("rows"), list):
                stack.append((node["rows"], True))
                continue
            for key, value in reversed(list(node.items())):
                if isinstance(key, str) and key.lower().startswith("_adt"):
                    continue
                out.append(str(key))
                stack.append((value, rows))
        elif isinstance(node, (list, tuple)):
            stack.extend((item, rows) for item in reversed(node))
    return True


def _result_strings(content: Any, numbers: bool = False) -> tuple[list[str], list[Any], bool]:
    """``(data strings, raw parts, read whole)`` of a tool result's content;
    with ``numbers`` the data strings include each number's text."""
    strings: list[str] = []
    raw: list[Any] = []
    whole = True
    budget = [JSON_NODE_BUDGET]
    for item in content if isinstance(content, list) else ():
        if not isinstance(item, dict):
            continue
        if isinstance(item.get("text"), str):
            text = item["text"]
            raw.append(text)
            if len(text) > MAX_RESULT_CHARS:
                whole = False
                continue
            node: Any = None
            if text.lstrip()[:1] in ("{", "["):
                try:
                    node = json.loads(text)
                except ValueError:
                    node = None
            if node is None:
                strings.append(text)
            else:
                whole = _data_strings(node, strings, budget, numbers) and whole
        elif "json" in item:
            raw.append(item["json"])
            whole = _data_strings(item["json"], strings, budget, numbers) and whole
    return strings, raw, whole


def _raw_text(part: Any) -> str:
    if isinstance(part, str):
        return part
    try:
        return json.dumps(part, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return ""


def _record(invocation_state: dict) -> dict:
    record = invocation_state.get(RESULT_NAMES_STATE_KEY)
    if not isinstance(record, dict):
        record = {"proven": set(), "possible": set(), "cut": 0}
        invocation_state[RESULT_NAMES_STATE_KEY] = record
    return record


def record_result(invocation_state: Any, result: Any, names: Iterable[str] | None) -> None:
    """Note which of ``names`` one of this call's tool results returns as data."""
    if not isinstance(invocation_state, dict) or not isinstance(result, dict) or not names:
        return
    if str(result.get("status") or "").lower() == "error":
        return
    record = _record(invocation_state)
    keys = list(dict.fromkeys(
        _key(name) for name in names if _key(name)
        and _key(name) not in record["proven"] and _key(name) not in record["possible"]))
    if not keys:
        return
    strings, raw, whole = _result_strings(result.get("content"))
    joined = "\x00".join(" ".join(text.split()).casefold() for text in strings)
    for key in keys:
        if key in joined and _key_pattern(key).search(joined):
            record["proven"].add(key)
    if whole:
        return
    record["cut"] += 1
    unread = _fold(" ".join(_raw_text(part) for part in raw))
    for key in keys:
        if key not in record["proven"] and key in unread:
            record["possible"].add(key)


def unreturned_names(answer: str, names: Iterable[str], invocation_state: Any,
                     question: str = "") -> list[str]:
    """The ``names`` that ``answer`` writes, that no result of this call
    proved or possibly returned (:func:`record_result`) and that
    ``question`` does not name, in the answer's own spelling and order, at
    most :data:`MAX_NAMES`."""
    if not isinstance(answer, str) or not answer:
        return []
    record = invocation_state.get(RESULT_NAMES_STATE_KEY) if isinstance(
        invocation_state, dict) else None
    record = record if isinstance(record, dict) else {}
    held = set(record.get("proven") or ()) | set(record.get("possible") or ())
    hits: dict[str, tuple[int, str]] = {}
    for name in names or ():
        key = _key(name)
        if not key or key in held or _find(name, question):
            continue
        match = _find(name, answer)
        if match:
            written = " ".join(match.group(0).split())
            hits.setdefault(written.casefold(), (match.start(), written))
    return [written for _pos, written in sorted(hits.values())][:MAX_NAMES]


def _quoted(names: list[str]) -> str:
    return ", ".join(f'"{name}"' for name in names)


def engine_line(names: list[str]) -> str:
    """The fixed line for the orchestrator about ``names``."""
    one = len(names) == 1
    return (
        f"{MARKER} the answer gives {_quoted(names)}, which no result in this call "
        f"returned ({'it is an' if one else 'they are'} example value{'' if one else 's'} "
        "from the system's schema, not data). Do not state "
        f"{'it' if one else 'them'} as data: give the record's id or key instead, or say "
        "its name was not returned."
    )
