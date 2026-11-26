# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The onboarding verify step's agent test: one call to the NEW system's own
query tool, through the same AgentCore gateway the chat's assistant uses.

The verify step checks that the source can be read. The agent test checks the
path a chat question takes to it, in three steps, and says which one failed:

1. **runtime**: the system's AgentCore runtime (``adt_<id>``, the name
   ``AgentManager`` creates) exists and is READY. A runtime still CREATING or
   UPDATING is waited for.
2. **gateway target**: the system's target (``<id>`` with ``_`` folded to
   ``-``, the name ``AgentManager`` creates) exists on the query gateway and is
   READY (SYNCHRONIZING is waited for), and the gateway's tools/list carries
   the target's query tool.
3. **tool**: that tool, called with the smallest read that proves it reads
   THIS system's data: a row count of one of the system's tables. The tool's
   own input schema decides the form: a natural-language tool (``question``,
   the default ``query_<id>`` system agent) gets a one-line count question; a
   SQL tool (``query``, ``run_query``) gets ``SELECT COUNT(*)``.

A SQL tool's result carries the count itself. A natural-language tool's
answer is model text, so it passes only when the provenance capture the system
agent wrote for this call shows a statement that read the named table. That
capture proves the read; it does not carry the table's count (a ``COUNT(*)``
result is one row), so the number shown is the agent's own answer, labelled
as such. A tool that answered but recorded no read of the table is
``no_read``, not a pass.

A source whose agent records no SQL statement (the read-only API types and
the hub) can never show that capture. For Tulip the investigation
trace the system agent wrote for this call proves the read instead: each
connector declares which of its own read tools reads which of its entities
(``system_agent.entity_reads_for``), and the test
passes when the trace shows a successful call of a read tool mapped to the
tested entity whose tool parsed the source's response (it reported how many
records it read, zero for an empty entity), with the table's id in the call
for a table the source names (a Tulip table is read by its ``table_id``). The
answer text never proves it. With no such call
(and for the hub and generic REST), the answer to the count question through
the gateway is the expected ``no_read`` when it gives a count: a number, or a
count in words next to what it counts ("one record"). An answer with no
count, an error, or one that says there is no data and gives no number fails.
Every read the proof makes runs under :func:`_bounded`: a trace or id read
that does not answer in time leaves the read unproven.

Limit: a SiteWise Direct or i3X agent test is never proven from the trace,
for any table, and is decided by the answer as above, as on the base. Both
connectors have tables they fix themselves (a hierarchy table, fallback tool
tables, i3X ``Asset``, ``AssetModel``, ``Property``) and tables the source
names (asset models, object types, read by an id the app does not keep), and
discovery lets the source name a table after a fixed one. No table origin is
kept, so the name cannot tell which rule applies; each connector declares
that (``EntityReads.names_tell_origin``) and the test honours it. Proving
these reads would need the table origin and the source's own id kept with
the discovery that produced them, cleared when the system is removed.

The tables come from the knowledge graph (the system's ``SchemaEntry`` nodes,
the ones its agent was provisioned with); the first by name is used.

It runs as a ``JobStore`` job polled by the wizard, so the verify HTTP request
never waits on it (CloudFront cuts an origin request at 120 s). The job answers
by :data:`AGENT_TEST_DEADLINE_S`: each stage (graph read, control-plane list,
gateway sign-in, tools/list, identity key, tool call and session close) runs
under :func:`_bounded`, which stops waiting when the time is up. A library call
that cannot be cancelled may finish later on its own thread, but the result is
written by the deadline. No chat turn is spent and nothing is written to
memory or the graph.

Which systems are called (``_plan``): PostgreSQL, the REST-family types
(Tulip, SiteWise i3X, generic REST) and SiteWise Direct, whose system
agents only read their source, and the built-in HighByte hub, whose runtime
keeps only the hub's read tools (``builtin_highbyte``). A customer MCP server
is not called: its agent can call any tool the server lists, and nothing here
can tell which of them only read. Athena is not counted: a row count can scan
the whole table, which costs money. Local mode has no gateway to call.
"""
from __future__ import annotations

import contextlib
import json
import logging
import re
import threading
import time
import uuid
from datetime import timedelta

from . import connection_types as ct
from .builtin_highbyte import is_builtin as _is_builtin_hub
from .catalog import catalog
from .config import get_config
from .db import neptune_query
from .job_store import JobStore
from .public_errors import public_error, public_error_text

logger = logging.getLogger(__name__)

#: Seconds the whole test may take. The slowest cold start measured in this
#: repo is ~130 s (an idle VPC runtime after an in-place update, its first
#: MCP initialize: scripts/set_query_mode.py); the orchestrator's own retry
#: schedule assumes 30-60 s for a cold runtime (orchestrator
#: ``_GATEWAY_RETRY_BACKOFFS_S``). 180 s covers the slow case plus a short
#: count answer, and stays under the 300 s a Mode A caller listens
#: (``system_agent.SYSTEM_QUERY_CALLER_WINDOW_S``).
AGENT_TEST_DEADLINE_S = 180

PASSED, FAILED, NOT_RUN, NO_READ = "passed", "failed", "not_run", "no_read"
STEP_RUNTIME, STEP_GATEWAY_TARGET, STEP_TOOL = "runtime", "gateway_target", "tool"

#: Seconds between control-plane polls of a settling runtime or target.
_STATUS_POLL_S = 5.0
#: Waits between calls the gateway could not deliver (cold runtime), the
#: orchestrator's schedule; the last one repeats until the deadline.
_RETRY_BACKOFFS_S = (5.0, 15.0, 30.0)
#: The longest MCP session start-up allowed (strands' own default).
_MAX_STARTUP_S = 30
#: Connect timeout for the control-plane client (a connect that cannot be made
#: fails fast; the read timeout is capped to the time left per call).
_CONTROL_CONNECT_TIMEOUT_S = 10
#: Grace past the server deadline before a still-``running`` job record is read
#: as a terminal interrupted result (the worker that owns it has had the whole
#: deadline plus this margin to write a result).
_STALE_JOB_GRACE_S = 60.0
#: Bound on the answer text read.
_ANSWER_READ_CHARS = 20_000
#: Bound on the gateway's tools/list pages and on the tables read.
_MAX_TOOL_PAGES = 50
_MAX_TABLES = 50
#: Seconds a tool call's own read timeout ends before the deadline, so the
#: call times out and its session closes before the job stops waiting on it.
_CLOSE_MARGIN_S = 2.0
#: Tries at the per-system slot before a start gives up on a busy slot.
_CLAIM_ATTEMPTS = 3

_RUNTIME_SETTLING = frozenset({"CREATING", "UPDATING"})
_TARGET_SETTLING = frozenset({"CREATING", "UPDATING", "SYNCHRONIZING"})

#: Connection types whose system agent only reads its source.
_READ_ONLY_API_TYPES = frozenset({ct.SITEWISE_DIRECT, ct.SITEWISE_I3X, ct.REST_API,
                                  ct.TULIP})

_GATEWAY_SEP = "___"

_TABLES_CYPHER = ("MATCH (s:System {id: $sid})-[:HAS_SCHEMA]->(se:SchemaEntry) "
                  "RETURN DISTINCT se.name AS name ORDER BY name LIMIT %d" % _MAX_TABLES)

_LOCAL_MODE_REASON = ("the app runs in local mode, with no AgentCore gateway, so there "
                      "is no gateway tool to call")
_MCP_REASON = ("this system's agent can call any tool its MCP server lists, and the app "
               "can't tell which of them only read, so verify doesn't send it a question")
_ATHENA_REASON = ("a row count on an Athena table can scan the whole table, which costs "
                  "money, so verify doesn't run one")
_STRUCTURED_REASON = ("in this query mode the system's tool takes a structured request, "
                      "and the agent test only sends SQL or a one-line question")
_IDENTITY_DENY_PREFIX = "access denied by identity policy"

# One count token: 1234 or 1,234, not part of a word, a decimal or a date.
_COUNT_RE = re.compile(r"(?<![\w.,\-/:])(\d{1,3}(?:,\d{3})+|\d+)(?![\w,\-/:]|\.\d)")

_JOBS = JobStore("verify-agent-test")


class _GatewayToolError(Exception):
    """A tool's error text, logged under a reference and never shown."""


class _OutOfTime(BaseException):
    """A stage did not return before the deadline.

    A ``BaseException`` on purpose: each stage reports its own failures with
    ``except Exception``, and running out of time must reach
    :func:`run_agent_test` instead of being reported as that stage's error."""

    def __init__(self, step: "str | None", what: str, reason: "str | None" = None):
        super().__init__(what)
        self.step = step
        self.what = what
        #: The failure's reason, when the stage words it itself.
        self.reason = reason


def _bounded(run: "_Run", step: "str | None", what: str, fn, *args, **kwargs):
    """``fn(*args, **kwargs)`` on its own daemon thread, waited on for no
    longer than the time left (at least a moment, so an instant call is never
    cut). Raises :class:`_OutOfTime` when the deadline comes first and passes
    on whatever ``fn`` raised. A call the library cannot cancel may finish
    later on its thread; the job no longer waits for it."""
    box: dict = {}

    def _target():
        try:
            box["value"] = fn(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001 - handed to the caller below
            box["error"] = exc
    worker = threading.Thread(target=_target, daemon=True, name="verify-agent-test-stage")
    worker.start()
    worker.join(max(0.05, run.remaining()))
    if worker.is_alive():
        raise _OutOfTime(step, what)
    if "error" in box:
        raise box["error"]
    return box.get("value")


# ── naming (the same names AgentManager provisions) ──────────────────────────

def runtime_name(system_id: str) -> str:
    from .agent_manager import AgentManager
    return AgentManager._runtime_name(system_id)


def target_name(system_id: str) -> str:
    return system_id.replace("_", "-")[:100]


def _tool_names(system_id: str) -> tuple[str, str]:
    """The target's natural-language tool and its SQL tool, as the gateway
    lists them (``system_agent`` Mode A and Mode B)."""
    target = target_name(system_id)
    return (f"{target}{_GATEWAY_SEP}query_{system_id.replace('-', '_')}",
            f"{target}{_GATEWAY_SEP}run_query")


def bare_tool_name(name: str) -> str:
    return str(name or "").rsplit(_GATEWAY_SEP, 1)[-1]


# ── the result ───────────────────────────────────────────────────────────────

class _Run:
    """Clock, deadline and the facts collected along the way."""

    def __init__(self, system_id: str, deadline_s: float, clock, sleep,
                 session_id: "str | None" = None):
        self.system_id = system_id
        self.deadline_s = deadline_s
        self.clock = clock
        self.sleep = sleep
        self.session_id = session_id or _own_session_id(system_id)
        self.started = clock()
        self.deadline = self.started + deadline_s
        self.facts: dict = {}

    def remaining(self) -> float:
        return self.deadline - self.clock()

    def expired(self) -> bool:
        return self.remaining() <= 0

    def result(self, outcome: str, step: "str | None" = None,
               reason: "str | None" = None) -> dict:
        out = {"outcome": outcome, "step": step, "reason": reason,
               "system_id": self.system_id, "tool": self.facts.get("tool"),
               "table": self.facts.get("table"), "rows": self.facts.get("rows"),
               "query_kind": self.facts.get("query_kind"),
               "read_provable": self.facts.get("read_provable"),
               "elapsed_s": round(self.clock() - self.started, 1),
               "deadline_s": self.deadline_s}
        return out

    def failed(self, step: str, reason: str) -> dict:
        return self.result(FAILED, step, reason)

    def not_run(self, reason: str) -> dict:
        return self.result(NOT_RUN, None, reason)

    def no_read(self, reason: str) -> dict:
        """A tool answered, but the test could not confirm it read the named
        table (no captured statement, or a capture of another table). Its own
        plain outcome: not a pass, and not a failure of a step."""
        return self.result(NO_READ, None, reason)


def _deadline_text(run: _Run) -> str:
    seconds = run.deadline_s
    return f"{int(seconds) if float(seconds).is_integer() else seconds} s"


# ── plan: what to call for this connection type ─────────────────────────────

def _plan(system_id: str, entry: dict) -> "tuple[bool, str | None]":
    """``(sql_ok, not_run_reason)``: whether a SQL count may be sent, or why
    the test does not call this system at all."""
    ctype = entry.get("connection_type") or ct.POSTGRESQL
    if ctype == ct.POSTGRESQL:
        return True, None
    if ctype == ct.ATHENA:
        return False, _ATHENA_REASON
    if ctype == ct.MCP_SERVER:
        return False, None if _is_builtin_hub(system_id) else _MCP_REASON
    if ctype in _READ_ONLY_API_TYPES:
        return False, None
    return False, f"verify has no agent test for {ctype} systems"


def _read_provable(entry: dict) -> bool:
    """Whether this system's agent records the table a read touched. Only a
    PostgreSQL agent captures its SQL; the read-only API types and the hub
    record no statement, so for them a no_read is the expected outcome, not a
    sign that anything went wrong (an API type's read may still be proven from
    its call's trace: :func:`_traced_read`)."""
    return (entry.get("connection_type") or ct.POSTGRESQL) == ct.POSTGRESQL


def _first_table(run: _Run) -> "tuple[str | None, str | None]":
    """``(table, not_run_reason)`` from the system's graph schema."""
    try:
        rows = _bounded(run, None, "reading this system's tables from the knowledge graph",
                        neptune_query, _TABLES_CYPHER, {"sid": run.system_id}) or []
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        return None, ("could not read this system's tables from the knowledge graph. "
                      + public_error_text(exc, "onboard.verify.agent_test.tables",
                                          default="graph_query_failed"))
    names = sorted({str(r.get("name")) for r in rows
                    if isinstance(r, dict) and r.get("name")})
    if not names:
        return None, ("the knowledge graph lists no tables for this system, so there "
                      "is nothing to count")
    return names[0], None


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _question(entry: dict, table: str) -> str:
    if (entry.get("connection_type") or ct.POSTGRESQL) == ct.POSTGRESQL:
        schema = entry.get("schema") or ""
        qualified = f"{schema}.{table}" if schema else table
        return (f"How many rows are in the table {qualified}? Count them with one "
                "SELECT COUNT(*) query and reply with only the number.")
    return (f'How many records does "{table}" hold in this system? Count them with as '
            "few reads as you can and reply with only the number.")


def _count_sql(entry: dict, table: str) -> str:
    schema = entry.get("schema") or ""
    relation = (f"{_quote_ident(schema)}.{_quote_ident(table)}" if schema
                else _quote_ident(table))
    return f"SELECT COUNT(*) AS row_count FROM {relation}"  # nosec B608 - schema and table are each double-quoted by _quote_ident (embedded quotes doubled); no values


# ── reading an answer ────────────────────────────────────────────────────────

def count_from_answer(text: str, names) -> "int | None":
    """The count a one-line count answer gives, or None.

    Reads the first paragraph only (the system agent appends its engine notes
    after a blank line) with every name in ``names`` (the table and schema)
    taken out, so digits in a table name are never read as the count. The
    first whole number left (``1234`` or ``1,234``, not a decimal or a date)
    is the count."""
    if not isinstance(text, str) or not text.strip():
        return None
    head = re.split(r"\n\s*\n", text.strip(), maxsplit=1)[0]
    for name in sorted({str(n) for n in names or () if n}, key=len, reverse=True):
        head = re.sub(re.escape(name), " ", head, flags=re.IGNORECASE)
    match = _COUNT_RE.search(head)
    return int(match.group(1).replace(",", "")) if match else None


#: The number words :func:`_count_in_words` reads: zero to ninety-nine, and
#: "single". A count past that comes back as digits, which
#: :func:`count_from_answer` reads; every form added here is more prose that
#: could read as a count.
_UNIT_WORDS = {word: value for value, word in enumerate((
    "zero one two three four five six seven eight nine ten eleven twelve thirteen "
    "fourteen fifteen sixteen seventeen eighteen nineteen").split())}
_TENS_WORDS = {word: 10 * value for value, word in enumerate(
    "twenty thirty forty fifty sixty seventy eighty ninety".split(), start=2)}
#: What a number word must sit right before to be a count (or the table name).
_COUNT_NOUNS = frozenset({"row", "rows", "record", "records", "entry", "entries",
                          "item", "items"})
#: Negate the verb after them, so a count right after that verb: "couldn't
#: read a single record", "cannot return even a single record".
_VERB_NEGATIONS = frozenset({"not", "never", "cannot", "nor"})
#: Negate only a count right after them: "no single record" and "without a
#: single record" are not counts, "no errors and one record" is.
_NOUN_NEGATIONS = frozenset({"no", "without"})
#: What may sit between a noun negation and the number word it negates.
_NEGATED_COUNT_FILLERS = frozenset({"a", "even"})
#: Stand-ins for the table name and the other names while an answer is read.
_TABLE_TOKEN, _NAME_TOKEN = "\ue000", "\ue001"
_WORD_TOKEN_RE = re.compile(r"[\ue000\ue001]|[a-z0-9_]+(?:'[a-z]+)?|\S")


def _answer_tokens(text: str, names, table: str) -> list:
    """The words of the first paragraph of ``text``, lowercased, with each of
    ``names`` as one stand-in token (the table's own, or another), so a
    number word inside a name is never read."""
    head = re.split(r"\n\s*\n", text.strip(), maxsplit=1)[0].lower().replace("\u2019", "'")
    table = str(table or "").lower()
    for name in sorted({str(n).lower() for n in names or () if n}, key=len, reverse=True):
        token = _TABLE_TOKEN if name == table else _NAME_TOKEN
        head = re.sub(r"(?<![a-z0-9_])" + re.escape(name) + r"(?![a-z0-9_])",
                      f" {token} ", head)
    return _WORD_TOKEN_RE.findall(re.sub(r"(?<=[a-z])-(?=[a-z])", " ", head))


def _number_words(tokens: list, i: int) -> "tuple[int, int] | None":
    """``(value, index after it)`` for a count spelled from ``tokens[i]``."""
    word, following = tokens[i], tokens[i + 1] if i + 1 < len(tokens) else ""
    if word == "single":   # "a single record"
        return 1, i + 1
    if word in _UNIT_WORDS:
        return _UNIT_WORDS[word], i + 1
    if word in _TENS_WORDS:
        unit = _UNIT_WORDS.get(following, 0)
        return ((_TENS_WORDS[word] + unit, i + 2) if 1 <= unit <= 9
                else (_TENS_WORDS[word], i + 1))
    return None


def _negated(tokens: list, i: int) -> bool:
    """Whether the count spelled from ``tokens[i]`` is negated by a short
    construction that governs it: a negation right before it ("not one",
    "no single", "without a single", "never a single"), or a negated verb
    one word before it ("couldn't read a single", "cannot return even a
    single"), past only "a" or "even". A negation further back is not read
    ("No errors and one record", "did not see errors and one record"): an
    answer with a digit gets no such reading either."""
    j = i - 1
    while j >= 0 and tokens[j] in _NEGATED_COUNT_FILLERS:
        j -= 1
    if j < 0:
        return False
    if _negation(tokens[j]) or tokens[j] in _NOUN_NEGATIONS:
        return True
    return tokens[j][:1].isalpha() and j >= 1 and _negation(tokens[j - 1])


def _negation(word: str) -> bool:
    """A word that negates the verb after it."""
    return word in _VERB_NEGATIONS or word.endswith("n't")


def _count_in_words(text: str, names, table: str) -> "int | None":
    """The count an answer spells in words, or None: a number word
    (:data:`_UNIT_WORDS`, :data:`_TENS_WORDS`, "single") right before a
    count noun (:data:`_COUNT_NOUNS`) or the table name, not negated, in the
    first paragraph. "one record" is 1; "one moment" and a bare "six" are
    not counts. Only the agent test of a source that records no statement
    reads it."""
    if not isinstance(text, str) or not text.strip():
        return None
    tokens = _answer_tokens(text, names, table)
    for i in range(len(tokens)):
        parsed = _number_words(tokens, i)
        if parsed is None or _negated(tokens, i) or parsed[1] >= len(tokens):
            continue
        after = tokens[parsed[1]]
        if after == _TABLE_TOKEN or after in _COUNT_NOUNS:
            return parsed[0]
    return None


def _sql_count(text: str) -> "int | None":
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None
    rows = data.get("rows") if isinstance(data, dict) else None
    if not (isinstance(rows, list) and rows and isinstance(rows[0], (list, tuple))
            and rows[0]):
        return None
    value = rows[0][0]
    if isinstance(value, bool):
        return None
    try:
        count = int(value)
    except (TypeError, ValueError):
        return None
    return count if count >= 0 else None


def _result_text(result) -> str:
    if not isinstance(result, dict):
        return ""
    parts = [item.get("text") for item in result.get("content") or []
             if isinstance(item, dict) and isinstance(item.get("text"), str)]
    return "".join(parts)[:_ANSWER_READ_CHARS]


def _transient_kind(text: str) -> "str | None":
    """``reach`` or ``timeout`` or ``busy`` for an error the gateway raises
    while a runtime is cold or busy (the orchestrator's retry markers), else
    None."""
    from .orchestrator import _TRANSIENT_TOOL_ERROR_MARKERS
    low = text.lower()
    if not any(marker in low for marker in _TRANSIENT_TOOL_ERROR_MARKERS):
        return None
    if "timed out" in low or "timeout" in low:
        return "timeout"
    if any(m in low for m in ("not reachable", "no endpoint", "endpoint not found",
                              "not responding")):
        return "reach"
    return "busy"


def _reference(text: str, where: str) -> str:
    """Log ``text`` under a reference and return the reference sentence."""
    ref = public_error(_GatewayToolError(text), where,
                       default="upstream_error").get("error_ref", "")
    return f"Reference: {ref}" if ref else ""


# ── control plane ────────────────────────────────────────────────────────────

def _control_client(region: str, *, connect_timeout_s: "float | None" = None,
                    read_timeout_s: "float | None" = None):
    import boto3
    from botocore.config import Config
    connect = int(max(1, connect_timeout_s or _CONTROL_CONNECT_TIMEOUT_S))
    read = int(max(1, read_timeout_s)) if read_timeout_s else None
    config = Config(connect_timeout=connect,
                    retries={"max_attempts": 2, "mode": "standard"},
                    **({"read_timeout": read} if read else {}))
    return boto3.client("bedrock-agentcore-control", region_name=region, config=config)


def _wait_ready(run: _Run, list_items, name: str, key: str, settling: frozenset,
                what: str, where: str, step: str) -> "str | None":
    """Poll until the item named ``name`` is READY; the reason it is not,
    else None. Each list call is bounded by the time left."""
    while True:
        try:
            items = _bounded(run, step, f"reading the {what}", list_items)
        except Exception as exc:  # noqa: BLE001 - reported, never raised
            return f"could not read the {what}. " + public_error_text(
                exc, where, default="upstream_error")
        item = next((i for i in items if isinstance(i, dict) and i.get(key) == name), None)
        if item is None:
            return f"there is no {what} named {name}"
        status = str(item.get("status") or "") or "in an unknown state"
        if status == "READY":
            return None
        if status not in settling:
            return f"the {what} {name} is {status}"
        if run.remaining() < _STATUS_POLL_S:
            return f"the {what} {name} was still {status} after {_deadline_text(run)}"
        run.sleep(_STATUS_POLL_S)


def _check_runtime(run: _Run, control) -> "str | None":
    from .runtime_provision_helpers import list_all_agent_runtimes
    return _wait_ready(run, lambda: list_all_agent_runtimes(control),
                       runtime_name(run.system_id), "agentRuntimeName", _RUNTIME_SETTLING,
                       "agent runtime", "onboard.verify.agent_test.runtime", STEP_RUNTIME)


def _check_target(run: _Run, control, gateway_id: str) -> "str | None":
    from .runtime_provision_helpers import list_all_gateway_targets
    return _wait_ready(run, lambda: list_all_gateway_targets(control, gateway_id),
                       target_name(run.system_id), "name", _TARGET_SETTLING,
                       "gateway target", "onboard.verify.agent_test.target",
                       STEP_GATEWAY_TARGET)


# ── gateway ──────────────────────────────────────────────────────────────────

class _GatewaySession:
    """One MCP session on the gateway (a strands ``MCPClient``)."""

    def __init__(self, client):
        self._client = client

    def list_tools(self) -> list:
        tools, token = [], None
        for _ in range(_MAX_TOOL_PAGES):
            page = self._client.list_tools_sync(pagination_token=token)
            for agent_tool in page:
                mcp_tool = getattr(agent_tool, "mcp_tool", None)
                tools.append({"name": getattr(mcp_tool, "name", ""),
                              "input_schema": getattr(mcp_tool, "inputSchema", None) or {}})
            token = getattr(page, "pagination_token", None)
            if not token:
                break
        return tools

    def call_tool(self, name: str, arguments: dict, timeout_s: float,
                  tool_use_id: "str | None" = None):
        return self._client.call_tool_sync(
            tool_use_id or uuid.uuid4().hex, name, arguments,
            read_timeout_seconds=timedelta(seconds=timeout_s))


@contextlib.contextmanager
def _open_gateway_session(url: str, bearer: str, startup_timeout_s: int):
    """A session on the gateway as ``bearer``; closed (its thread joined) on exit."""
    from .gateway_auth import bearer_mcp_client
    client = bearer_mcp_client(url, bearer, startup_timeout=startup_timeout_s)
    with client:
        yield _GatewaySession(client)


def _gateway_bearer(config, user_access_token: "str | None") -> str:
    """The bearer a chat turn would send: the user's token, else the app's
    service token (``orchestrator._resolve_gateway_bearer``)."""
    from .orchestrator import _resolve_gateway_bearer
    return _resolve_gateway_bearer(config, user_access_token)


def _claim_secret() -> "str | None":
    """The identity-claim signing key, read by name (as the app's delegated-
    identity verifier reads it); None when unreadable, and None WITHOUT a
    read when this app is not granted the key (every deploy but the Quick
    delegated-token mode; ``delegated_identity.signing_key_granted``). With
    no key, an identity denial from a group-restricted system is reported
    ``not_run`` (finding 2); on the workshop path no key is deployed, so the
    chat turn attaches no claim either."""
    from .auth.delegated_identity import _fetch_identity_claim_secret
    return _fetch_identity_claim_secret()


def _identity_claim_args(user_access_token: "str | None") -> "tuple[dict, bool]":
    """``(args, signed)``: the ADR-0049 claim a chat turn attaches for this
    user, minted fresh, so a group-restricted system is tested as the chat
    would reach it, and whether a claim was actually signed. ``({}, False)``
    without a user token or a readable key: the app cannot reach a
    group-restricted system the way a chat turn does, so an identity denial
    is reported ``not_run`` rather than ``failed`` (finding 2)."""
    if not user_access_token:
        return {}, False
    from .auth import identity_claim
    identity = identity_claim.identity_from_access_token(user_access_token)
    secret = _claim_secret() if identity else None
    if not secret:
        return {}, False
    sub, groups = identity
    return ({identity_claim.RESERVED_CLAIM_ARG:
             identity_claim.mint_claim(sub, groups, secret)}, True)


#: A session id this test owns (never a user's chat session): the provenance
#: capture is keyed by it, and nothing is written to memory under it.
def _own_session_id(system_id: str) -> str:
    return f"verify-agent-test-{system_id}-{uuid.uuid4().hex}"[:256]


def _provenance_args(run: _Run) -> "tuple[dict, str]":
    """``(reserved_arg_dict, query_ref)`` for one tool call: a fresh
    ``tool_use_id`` and the deterministic ``query_ref`` the system-agent
    runtime will key its sidecar write by (``mint_query_ref``), wrapped in
    the reserved-arg value. The caller sends ``tool_use_id`` as the MCP call
    id so the runtime's ref matches. The value is issued through the ONE
    capability home (``provenance_store.issue_provenance_arg``), which mints
    and durably records a per-call write token; the runtime writes its
    capture only with that token, so this server-minted pair is protected the
    same way the gateway's interactive turns are."""
    from .provenance import mint_query_ref
    from .provenance_store import issue_provenance_arg
    from .provenance import RESERVED_PROVENANCE_ARG
    tool_use_id = run.facts.setdefault("tool_use_id", uuid.uuid4().hex)
    query_ref = mint_query_ref(run.session_id, tool_use_id)
    return {RESERVED_PROVENANCE_ARG: issue_provenance_arg(run.session_id, query_ref)}, query_ref


def _captured_read(session_id: str, query_ref: str, table: str) -> bool:
    """Whether the provenance capture the system agent wrote for this call
    shows a statement that read the named table (so the inner agent read it
    from the source). False for no capture, a capture of another table, or an
    unreadable store. The capture's ``row_count`` is the size of that
    statement's result (one row for a ``COUNT(*)``), never the table's count,
    so it is not read here."""
    try:
        from . import provenance_store
        entry = provenance_store.get_query(session_id, query_ref)
    except Exception:  # noqa: BLE001 - no capture is honest absence, never raised
        return False
    if not isinstance(entry, dict):
        return False
    entity = entry.get("entity") if isinstance(entry.get("entity"), dict) else {}
    captured_table = str(entity.get("table") or "").strip()
    return bool(captured_table) and captured_table.casefold() == str(table).strip().casefold()


def _traced_read(run: _Run, query_ref: str) -> bool:
    """Whether the investigation trace the system agent wrote for this call
    shows a read of the tested table (:func:`_trace_proves`). The tool call
    proves the read, never the answer text. One bounded stage: the rules, the
    trace and the table's id are read under the deadline, and a read that
    fails or does not answer in time leaves the read unproven, so the outcome
    is what it is with no trace. That is why running out of time is caught
    here and not passed on, unlike every other stage: this proof is an extra
    read, and a slow trace store must not fail a test the answer settles."""
    try:
        return bool(_bounded(run, STEP_TOOL, "reading the call's trace",
                             _trace_proves, run, query_ref))
    except (Exception, _OutOfTime):  # noqa: BLE001 - an unproven read, never raised
        return False


def _trace_proves(run: _Run, query_ref: str) -> bool:
    """The trace check: the connector's rule for the tested table
    (``system_agent.entity_reads_for``; none for PostgreSQL, the hub and
    generic REST, and none used for a connector whose table names do not tell
    their origin: SiteWise Direct and i3X), a successful call in the trace of
    a read tool it maps that
    reported a count (``entity_reads``), and, for a table the source names,
    the table's id in that call. The trace is read consistently: the runtime
    wrote it a moment before it answered."""
    from . import entity_reads, investigation_trace_store
    from .system_agent import entity_reads_for
    reads = entity_reads_for(run.facts.get("connection_type") or "")
    if reads is None or not reads.names_tell_origin:
        return False   # no rules, or a name that cannot tell which rule applies
    rule = reads.rule(run.facts.get("table") or "")
    if rule is None:
        return False
    trace = investigation_trace_store.get_trace(run.session_id, query_ref, consistent=True)
    calls = entity_reads.traced_reads(
        trace.get("steps") if isinstance(trace, dict) else None, rule)
    entity_id = _source_id(run) if calls and rule.id_arg else ""
    return entity_reads.proves_read(calls, rule, str(entity_id or ""))


def _source_id(run: _Run) -> str:
    """The source's own id of the tested table, for a connector whose tables
    the source names (a Tulip table); ``""`` for any other type."""
    if run.facts.get("connection_type") == ct.TULIP:
        return _tulip_table_id(run.system_id, run.facts.get("table") or "")
    return ""


def _tulip_table_id(system_id: str, table: str) -> str:
    """The id of the Tulip table named ``table``, exactly as discovery saved
    the name: the saved discovery first, else one bounded live table list
    (``resolve_evidence._tulip_table_id_for``, the lookup the value sampler
    uses; provision seeds the agent's table ids from the same two sources).
    ``""`` when it cannot be found or more than one table has the name."""
    from .resolve_evidence import _tulip_connection_config, _tulip_table_id_for
    return _tulip_table_id_for(system_id, table, _tulip_connection_config(system_id, {}))


def _startup(run: _Run) -> int:
    return int(max(1, min(_MAX_STARTUP_S, run.remaining())))


def _find_tool(run: _Run, open_session, url: str, bearer: str,
               sql_ok: bool) -> "tuple[str | None, str | None, dict | None]":
    """``(tool_name, kind, failure_or_not_run)`` from the gateway's tools/list."""
    def _list():
        with open_session(url, bearer, _startup(run)) as session:
            return session.list_tools()
    try:
        tools = _bounded(run, STEP_GATEWAY_TARGET, "listing the gateway's tools", _list)
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        return None, None, run.failed(STEP_GATEWAY_TARGET, (
            "could not list the gateway's tools. " + public_error_text(
                exc, "onboard.verify.agent_test.tools_list", default="upstream_error")))
    listed = {str(t.get("name") or ""): t for t in tools if isinstance(t, dict)}
    question_tool, sql_tool = _tool_names(run.system_id)
    name = question_tool if question_tool in listed else sql_tool if sql_tool in listed else None
    if name is None:
        prefix = target_name(run.system_id) + _GATEWAY_SEP
        own = sorted(bare_tool_name(n) for n in listed if n.startswith(prefix))
        reason = (f"the gateway lists no {bare_tool_name(question_tool)} tool for "
                  f"{target_name(run.system_id)}")
        reason += f" (it lists {', '.join(own[:5])})" if own else " (it lists none)"
        return None, None, run.failed(STEP_GATEWAY_TARGET, reason)
    run.facts["tool"] = name
    props = (listed[name].get("input_schema") or {}).get("properties") or {}
    if "question" in props:
        return name, "question", None
    if "query" in props:
        return (name, "sql", None) if sql_ok else (None, None, run.not_run(_STRUCTURED_REASON))
    return None, None, run.failed(
        STEP_TOOL, f"{bare_tool_name(name)} takes no question or query argument")


def _read_proven(run: _Run, query_ref: str, table: str) -> bool:
    """Whether this call's read of ``table`` is proven. A source that captures
    its SQL (PostgreSQL): by that capture, read as a stage under the deadline
    (a store that does not answer fails the test at the deadline, as any
    stage does). Any other source writes no statement, so its capture is not
    read: by the call's trace (:func:`_traced_read`)."""
    if run.facts.get("read_provable") is False:
        return _traced_read(run, query_ref)
    what = "reading the call's provenance capture"
    try:
        return bool(_bounded(run, STEP_TOOL, what, _captured_read, run.session_id,
                             query_ref, table))
    except _OutOfTime as late:
        # The capture read gets only what the earlier stages left of the
        # deadline, so the reason says the test ran out of time, not how long
        # the read took.
        raise _OutOfTime(STEP_TOOL, what, f"the agent test ran out of time "
                         f"({_deadline_text(run)}) while {what}") from late


def _judge(run: _Run, kind: str, text: str, names, query_ref: "str | None") -> dict:
    """The outcome of a call that returned without an error.

    SQL (Mode B) is deterministic: its own result carries the count. For a
    natural-language tool (Mode A) a number in the model's text is NOT proof
    the table was read, so the test passes only when this call's provenance
    capture or trace shows the read (:func:`_read_proven`, finding 1). The
    model's number is never echoed in a reason (finding 5): an answer with
    no count is reported with a reference, not an excerpt."""
    bare = bare_tool_name(run.facts.get("tool") or "")
    if not text.strip():
        return run.failed(STEP_TOOL, f"{bare} returned an empty result")
    if kind == "sql":
        rows = _sql_count(text)
        if rows is None:
            return run.failed(STEP_TOOL, f"{bare} returned no row count. "
                              + _reference(text, "onboard.verify.agent_test.sql"))
        run.facts["rows"] = rows
        return run.result(PASSED)
    # Mode A: the read of the table is proven by the capture, not the text.
    from .query_intelligence import failure_marker
    marker = failure_marker(text)
    model_number = None if marker and marker[0] == "error" else count_from_answer(text, names)
    table = run.facts.get("table") or ""
    if query_ref and _read_proven(run, query_ref, table):
        # The capture or the trace proves the read but carries no count, so
        # the number shown is the agent's answer (``query_kind`` question
        # says so); a read with no number in the answer passes without one.
        run.facts["rows"] = model_number
        return run.result(PASSED)
    if run.facts.get("read_provable") is False and (
            model_number is not None
            or (marker is None and _count_in_words(text, names, table) is not None)):
        # This type records no statement and its trace showed no read, so no
        # answer can prove the read, and a count in words ("one record") is
        # the same answer as "1". An error or empty marker vetoes the words,
        # as before. Never a pass.
        return run.no_read(f"{bare} answered through the gateway. This type of "
                           "system doesn't record which table a read used, so the "
                           f"test can't confirm it read {table}. That's expected "
                           "here.")
    if model_number is None:
        return run.failed(STEP_TOOL, f"{bare} returned no row count for {table}. "
                          + _reference(text, "onboard.verify.agent_test.answer"))
    # It gave a number, but no captured statement read the named table, so the
    # read is unproven: its own plain outcome, not a pass.
    return run.no_read(f"{bare} answered with a number but recorded no read of "
                       f"{table}, so the test could not confirm it read this "
                       "system's data")


def _tool_error(run: _Run, text: str, signed_claim: bool) -> dict:
    bare = bare_tool_name(run.facts.get("tool") or "")
    if text.strip().lower().startswith(_IDENTITY_DENY_PREFIX):
        if not signed_claim:
            # The app could not sign the user's identity claim here, so it
            # cannot reach a group-restricted system the way a chat turn does.
            # That is not a failure of the system (finding 2).
            return run.not_run("this system only answers members of a group, and the "
                               "app could not sign that identity claim here, so the "
                               "agent test did not run")
        return run.failed(STEP_TOOL, "the system's identity policy denied the call")
    return run.failed(STEP_TOOL, f"{bare} returned an error. "
                      + _reference(text, "onboard.verify.agent_test.tool"))


def _gave_up(run: _Run, kind: str, text: str) -> dict:
    """The deadline passed on a call the gateway could not deliver."""
    ref = _reference(text, "onboard.verify.agent_test.cold")
    if kind == "timeout":
        return run.failed(STEP_TOOL, f"no answer within {_deadline_text(run)}. {ref}".strip())
    if kind == "reach":
        reason = "the gateway could not reach the system's runtime"
    else:
        reason = "the system's runtime stayed busy or unavailable"
    return run.failed(STEP_RUNTIME, f"{reason} within {_deadline_text(run)}. {ref}".strip())


def _call(run: _Run, open_session, url: str, bearer: str, tool: str,
          arguments: dict, names, kind: str, signed_claim: bool) -> dict:
    """Call ``tool`` until it answers, retrying what the gateway could not
    deliver to a cold runtime, never past the deadline. A fresh session per
    attempt: a failed call can end the session it was sent on. Each attempt
    carries the F1 provenance context (one deterministic ``query_ref`` per
    run, read back to prove the table was read)."""
    prov_args, query_ref = _provenance_args(run)
    tool_use_id = run.facts.get("tool_use_id")
    call_args = {**arguments, **prov_args}
    attempt = 0
    while True:
        if run.expired():  # fail fast rather than start a call past the deadline
            return _gave_up(run, "timeout", "")

        def _once():
            # The call's own read timeout ends a margin before the deadline,
            # so it times out and its session closes before the wait below.
            with open_session(url, bearer, _startup(run)) as session:
                return session.call_tool(tool, call_args,
                                         max(0.1, run.remaining() - _CLOSE_MARGIN_S),
                                         tool_use_id=tool_use_id)
        try:
            result = _bounded(run, STEP_TOOL, "the tool call", _once)
        except Exception as exc:  # noqa: BLE001 - reported, never raised
            return run.failed(STEP_GATEWAY_TARGET, "could not open a gateway session. "
                              + public_error_text(exc, "onboard.verify.agent_test.session",
                                                  default="upstream_error"))
        text = _result_text(result)
        if not (isinstance(result, dict) and result.get("status") == "error"):
            return _judge(run, kind, text, names, query_ref)
        transient = _transient_kind(text)
        if transient is None:
            return _tool_error(run, text, signed_claim)
        # A count is a read, so a call the gateway could not deliver (or that
        # timed out before the deadline) is safe to send again.
        wait = _RETRY_BACKOFFS_S[min(attempt, len(_RETRY_BACKOFFS_S) - 1)]
        if run.remaining() <= wait:
            return _gave_up(run, transient, text)
        run.sleep(wait)
        attempt += 1


def run_agent_test(system_id: str, *, user_access_token: "str | None" = None,
                   deadline_s: float = AGENT_TEST_DEADLINE_S, control=None,
                   open_session=None, clock=None, sleep=None,
                   session_id: "str | None" = None) -> dict:
    """Run the agent test for ``system_id`` and return its result dict:
    ``outcome`` (passed | failed | not_run | no_read), ``step`` (runtime |
    gateway_target | tool, set on a failure), ``reason`` (plain text, no raw
    backend text), ``tool``, ``table``, ``rows``, ``query_kind`` (question |
    sql), ``elapsed_s`` and ``deadline_s``. Never raises."""
    run = _Run(system_id, deadline_s, clock or time.monotonic, sleep or time.sleep,
               session_id=session_id)
    try:
        return _run_steps(run, user_access_token, control, open_session or _open_gateway_session)
    except _OutOfTime as late:
        return run.failed(late.step, late.reason or f"{late.what} took longer than the "
                          f"{_deadline_text(run)} the test allows")
    except Exception as exc:  # noqa: BLE001 - a job result, never raised
        return run.result(FAILED, None, "the agent test stopped. " + public_error_text(
            exc, "onboard.verify.agent_test"))


def _expired_result(run: _Run) -> dict:
    """The result when the deadline passed between stages (a slow graph read
    or a slow control-plane list used all the time left)."""
    return run.failed(STEP_RUNTIME,
                      f"the agent test ran out of time after {_deadline_text(run)}")


def _sign_in(run: _Run, config, user_access_token) -> "tuple[str | None, dict | None]":
    """``(bearer, failure)``: the token a chat turn would sign in with, got
    within the time left."""
    try:
        return _bounded(run, STEP_GATEWAY_TARGET, "signing in to the gateway",
                        _gateway_bearer, config, user_access_token), None
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        return None, run.failed(STEP_GATEWAY_TARGET, "the app has no token to sign in to the "
                                "gateway. " + public_error_text(
                                    exc, "onboard.verify.agent_test.bearer",
                                    default="auth_failed"))


def _run_steps(run: _Run, user_access_token, control, open_session) -> dict:
    entry = catalog.get(run.system_id)
    if not entry:
        return run.not_run("this system is not in the catalog")
    config = get_config()
    if config.is_local_mode or not config.has_gateway:
        return run.not_run(_LOCAL_MODE_REASON)
    sql_ok, reason = _plan(run.system_id, entry)
    if reason:
        return run.not_run(reason)
    run.facts["read_provable"] = _read_provable(entry)
    run.facts["connection_type"] = entry.get("connection_type") or ct.POSTGRESQL
    table, reason = _first_table(run)   # bounded internally; may be slow
    if reason:
        return run.not_run(reason)
    if run.expired():
        return _expired_result(run)
    run.facts["table"] = table
    # The control client's read timeout is capped to the time left, so a hung
    # list cannot outlast the deadline (finding 3); it is remade before the
    # target stage with the time then left.
    if control is None:
        control = _control_client(config.aws_region, read_timeout_s=run.remaining())
    reason = _check_runtime(run, control)
    if reason:
        return run.failed(STEP_RUNTIME, reason)
    if run.expired():
        return _expired_result(run)
    reason = _check_target(run, control, config.agentcore_gateway_id)
    if reason:
        return run.failed(STEP_GATEWAY_TARGET, reason)
    if run.expired():
        return _expired_result(run)
    bearer, stop = _sign_in(run, config, user_access_token)
    if stop is not None:
        return stop
    if run.expired():
        return _expired_result(run)
    url = config.agentcore_gateway_url
    tool, kind, stop = _find_tool(run, open_session, url, bearer, sql_ok)
    if stop is not None:
        return stop
    run.facts["query_kind"] = kind
    arguments = ({"query": _count_sql(entry, table)} if kind == "sql"
                 else {"question": _question(entry, table)})
    claim_args, signed_claim = _bounded(run, STEP_TOOL, "reading the identity-claim key",
                                        _identity_claim_args, user_access_token)
    arguments.update(claim_args)
    names = [table, entry.get("schema") or ""]
    return _call(run, open_session, url, bearer, tool, arguments, names, kind, signed_claim)


# ── the job (the preflight / resolve idiom) ──────────────────────────────────

def _slot_key(system_id: str) -> str:
    return f"slot:{system_id}"


def _stale_seconds() -> float:
    """How long a ``running`` record is believed before it is taken over or
    read as interrupted: the whole deadline plus a grace for the worker to
    write its result."""
    return AGENT_TEST_DEADLINE_S + _STALE_JOB_GRACE_S


def _interrupted_result(system_id: str, job: dict) -> dict:
    """A terminal result for a ``running`` record whose worker never wrote
    one and whose deadline (plus grace) has passed — a replica that died, or
    a fail-soft final write that reached only another replica's memory."""
    return {"outcome": FAILED, "step": None,
            "reason": ("the agent test stopped before it reported a result "
                       f"(no answer within {_deadline_text_secs(job.get('deadline_s'))})"),
            "system_id": system_id, "tool": None, "table": None, "rows": None,
            "query_kind": None, "elapsed_s": None,
            "deadline_s": job.get("deadline_s", AGENT_TEST_DEADLINE_S)}


def _deadline_text_secs(seconds) -> str:
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        value = AGENT_TEST_DEADLINE_S
    return f"{int(value) if value.is_integer() else value} s"


def _is_stale_running(job: dict, *, now: "float | None" = None) -> bool:
    if not isinstance(job, dict) or job.get("status") != "running":
        return False
    started = job.get("started_at")
    try:
        started = float(started)
    except (TypeError, ValueError):
        return False
    now = time.time() if now is None else now
    return now - started > _stale_seconds()


def _run_job(job_id: str, system_id: str, user_access_token: "str | None",
             session_id: "str | None" = None) -> None:
    """Worker thread: run the test, store its result, and release the slot it
    holds. The user's token lives only in this thread's arguments, never in
    the job record."""
    result = run_agent_test(system_id, user_access_token=user_access_token,
                            session_id=session_id)
    logger.info("[VERIFY] agent test %s %s: %s step=%s elapsed=%ss", job_id, system_id,
                result.get("outcome"), result.get("step"), result.get("elapsed_s"))
    _JOBS[job_id] = {"status": "done", "system_id": system_id, "result": result}
    # Release the per-system slot iff we still hold it (a newer worker that
    # took over a stale claim keeps its own slot — exact-owner release).
    try:
        _JOBS.release_slot(_slot_key(system_id), job_id,
                           {"status": "done", "job_id": job_id, "system_id": system_id})
    except Exception as exc:  # noqa: BLE001 - the result is already stored
        logger.warning("[VERIFY] agent test %s slot release failed (%s)",
                       job_id, type(exc).__name__)


def start_agent_test(body, session) -> "dict":
    """``POST /api/onboard/verify/agent-test`` body: start the job for
    ``system_id`` and answer ``{job_id, status: running, deadline_s}`` at once;
    404 for a system the catalog does not hold. One live test per system: a
    second start while one is in flight returns the live job rather than
    starting a duplicate model/read job (finding 4)."""
    from fastapi.responses import JSONResponse
    from .auth.forwarding import user_access_token_from_session
    system_id = str((body or {}).get("system_id") or "").strip() if isinstance(body, dict) else ""
    if not system_id or not catalog.get(system_id):
        return JSONResponse(status_code=404,
                            content={"error": f"Unknown system_id '{system_id}'"})
    job_id = uuid.uuid4().hex[:16]
    slot_state = {"status": "running", "job_id": job_id, "system_id": system_id,
                  "started_at": time.time(), "deadline_s": AGENT_TEST_DEADLINE_S}
    claimed, live = _claim_or_live(system_id, slot_state)
    if live is not None:
        return live
    if not claimed:
        return JSONResponse(status_code=409, content={
            "error": "Another agent test for this system is starting. Try again."})
    _JOBS[job_id] = {"status": "running", "system_id": system_id,
                     "started_at": time.time(), "deadline_s": AGENT_TEST_DEADLINE_S}
    session_id = _own_session_id(system_id)
    threading.Thread(target=_run_job, daemon=True, name=f"verify-agent-test-{job_id}",
                     args=(job_id, system_id, user_access_token_from_session(session),
                           session_id)).start()
    return {"job_id": job_id, "status": "running", "deadline_s": AGENT_TEST_DEADLINE_S}


def _claim_or_live(system_id: str, slot_state: dict) -> "tuple[bool, dict | None]":
    """``(claimed, live_job)``: take the per-system slot, or find the job that
    holds it. A holder that finishes between a failed claim and the read of
    the slot frees it for the next try, where another caller may win it, so
    a start goes ahead only on its own successful claim."""
    for _ in range(_CLAIM_ATTEMPTS):
        try:
            if _JOBS.claim_slot(_slot_key(system_id), slot_state,
                                stale_seconds=_stale_seconds()):
                return True, None
        except Exception as exc:  # noqa: BLE001 - fail soft: run without the slot
            logger.warning("[VERIFY] agent test slot claim failed (%s); running unslotted",
                           type(exc).__name__)
            return True, None
        live = _live_job_for(system_id)
        if live is not None:
            return False, live
    return False, None


def _live_job_for(system_id: str) -> "dict | None":
    """The in-flight job for ``system_id`` (from the per-system slot), or
    ``None`` when the slot's holder is done, stale, or missing."""
    slot = _JOBS.get(_slot_key(system_id))
    if not isinstance(slot, dict) or slot.get("status") != "running":
        return None
    if _is_stale_running(slot):
        return None
    job_id = slot.get("job_id")
    if not (isinstance(job_id, str) and job_id):
        return None
    return {"job_id": job_id, "status": "running", "deadline_s": AGENT_TEST_DEADLINE_S}


def agent_test_status(job_id: str) -> dict:
    """``GET /api/onboard/verify/agent-test/{job_id}``: running, or done with
    the result; an unknown id answers the jobs' shared ``Unknown job_id``. A
    ``running`` record whose deadline (plus a grace) has passed with no result
    is read as a terminal interrupted result, so a dead worker never leaves a
    client polling forever (finding 4)."""
    job = _JOBS.get(job_id)
    if not isinstance(job, dict):
        return {"error": f"Unknown job_id '{job_id}'"}
    if job.get("status") == "done":
        return {"status": "done", "result": job.get("result") or {}}
    if _is_stale_running(job):
        result = _interrupted_result(job.get("system_id", ""), job)
        _JOBS[job_id] = {"status": "done", "system_id": job.get("system_id"),
                         "result": result}
        return {"status": "done", "result": result}
    return {"status": "running", "deadline_s": job.get("deadline_s", AGENT_TEST_DEADLINE_S)}
