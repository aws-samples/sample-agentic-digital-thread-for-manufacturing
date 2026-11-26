# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Agents tab API — AgentCore runtime status + CloudWatch metrics + evaluation scores.
"""
import os
import time
import logging
from datetime import datetime, timezone
from fastapi import APIRouter

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin/agents")

EVAL_CONFIG_MAP = {
    "adt_orchestrator": os.environ.get("EVAL_CONFIG_ORCHESTRATOR", ""),
    # System agents do not have evaluation configs — evaluations only apply to orchestrator
}

# Display names + ordering + the provider factory live in fleet.py (single source,
# shared by the local and cloud providers).
from .fleet import AGENT_DISPLAY_NAMES, AGENT_ORDER, get_fleet_provider
from . import fleet_metrics
from ._insights import run_insights
from .offload import offload
from .public_errors import public_error, public_error_text
from .console_links import agentcore_console_links
from .eval_figure_check import EVALUATOR_NAME as FIGURE_CHECK_EVALUATOR
from .eval_figure_check import LABELS as FIGURE_CHECK_LABELS

# Live AgentManager, injected at startup (see server.py). Used to pick the local
# vs cloud fleet provider. None → cloud (safe default for deployed runtimes).
_agent_manager = None


def set_agent_manager(mgr):
    global _agent_manager
    _agent_manager = mgr


def _boto_session():
    import boto3
    return boto3.Session(region_name=os.environ.get("AWS_REGION", "eu-central-1"))


def _get_configured_models(session) -> dict:
    """Configured model IDs — shared single source (see agents/model_config.py)."""
    from .model_config import get_configured_models
    return get_configured_models()


def _get_runtimes(session) -> list[dict]:
    try:
        control = session.client("bedrock-agentcore-control")
        from .runtime_provision_helpers import list_all_agent_runtimes
        return [r for r in list_all_agent_runtimes(control)
                if r.get("agentRuntimeName", "").startswith("adt_")]
    except Exception as e:
        logger.warning(f"Failed to list runtimes: {e}")
        return []


def _run_insights(logs_client, log_group: str, query: str, hours: float) -> list[dict]:
    """Run a CloudWatch Logs Insights query, poll up to 25s."""
    end = int(time.time())
    start = end - int(hours * 3600)
    try:
        r = logs_client.start_query(logGroupName=log_group, startTime=start, endTime=end,
                                    queryString=query, limit=1000)
        qid = r["queryId"]
        for _ in range(50):
            time.sleep(0.5)  # deliberate pacing/backoff delay in a bounded polling loop  # nosemgrep: arbitrary-sleep
            s = logs_client.get_query_results(queryId=qid)
            if s["status"] in ("Complete", "Failed", "Cancelled"):
                return s.get("results", [])
        return []
    except Exception as e:
        logger.warning(f"Logs Insights failed for {log_group}: {e}")
        return []


def _parse_row(row: list[dict]) -> dict:
    return {f["field"]: f["value"] for f in row}


#: A deployed AgentCore runtime writes each log line to its group twice: as
#: plain text in the per-session runtime stream (``[trace_id=<32 hex> ...]``)
#: and as OpenTelemetry JSON in the shared ``otel-rt-logs`` stream (the same
#: text in ``body``, the trace in ``"traceId"``); a line is sometimes exported
#: more than once, and a window can hold one copy of some turns and the other
#: copy of others. The group also has a shared ``spans`` stream.
_OTEL_STREAM = "otel-rt-logs"
_SPANS_STREAM = "spans"
#: A per-session stream count leaves the two shared streams out.
_SESSION_STREAMS = (f"filter @logStream != '{_OTEL_STREAM}' "
                    f"and @logStream != '{_SPANS_STREAM}'")

#: The orchestrator's turns, one row per hour. Every turn runs in its own
#: trace, so the first stats folds all copies of a turn's lines into one row
#: keyed by its trace id; the second counts those rows. A line with no trace
#: id is keyed by its own stream, time and session, so distinct turns are
#: never folded together (both copies of such a turn would each count).
#:
#: A turn is a session when it has an ``ORCHESTRATOR COMPLETE`` line (the
#: warmup ping logs ``ORCHESTRATOR WARMUP`` instead), in the hour of that line.
#: Its tool count is the COMPLETE line's ``tool_calls=``, or for a line
#: written before that field its ``DETERMINISM: <grounded>/<calls>`` line;
#: ``tooled`` counts the sessions that had either. A turn with no COMPLETE
#: line in the window adds a row with no sessions.
_TURNS_QUERY = r"""filter @message like /ORCHESTRATOR COMPLETE|DETERMINISM: /
| parse @message /trace_id=(?<plain_trace>[0-9a-f]{32})/
| parse @message /"traceId":"(?<otel_trace>[0-9a-f]{32})"/
| parse @message /session=(?<sid>[^ "]+) total_events=(?<total_events>\d+)/
| parse @message /total_events=\d+ tool_calls=(?<line_tools>\d+)/
| parse @message /DETERMINISM: \d+\/(?<det_tools>\d+) grounded/
| fields @message like /ORCHESTRATOR COMPLETE/ as is_done,
    coalesce(plain_trace, otel_trace,
             concat('line:', @logStream, ':', toMillis(@timestamp), ':', coalesce(sid, '-'))) as turn
| stats max(is_done) as turn_done, max(total_events) as turn_events,
    max(line_tools) as turn_line_tools, max(det_tools) as turn_det_tools,
    max(is_done * toMillis(@timestamp)) as done_ms by turn
| fields datefloor(fromMillis(done_ms), 1h) as hour, turn_done * turn_events as done_events,
    turn_done * coalesce(turn_line_tools, turn_det_tools) as done_tools,
    turn_done * isPresent(coalesce(turn_line_tools, turn_det_tools)) as done_tooled
| stats sum(turn_done) as sessions, sum(done_events) as events,
    sum(done_tools) as tool_calls, sum(done_tooled) as tooled by hour
| sort hour asc"""

#: Hourly sparkline for a system agent (which logs no COMPLETE line).
_STREAM_HOURLY_QUERY = f"""{_SESSION_STREAMS}
| stats count_distinct(@logStream) as sessions by bin(1h) as hour
| sort hour asc"""

_STREAM_COUNT_QUERY = f"""{_SESSION_STREAMS}
| stats count_distinct(@logStream) as sessions"""

#: What an Insights row that cannot be read as numbers raises.
_UNREADABLE_ROW = (KeyError, TypeError, ValueError)


def _insights_rows(logs, log_group: str, query: str, hours: float) -> list | None:
    """A Logs Insights query's rows, or ``None`` when it did not complete.

    ``[]`` is a query that completed with no rows. ``None`` is one that could
    not start, ended Failed, Cancelled or Timeout, or did not finish in time
    (:func:`agents._insights.run_insights` raises for each of those).
    """
    try:
        return run_insights(logs, log_group, query, hours)
    except Exception as e:
        logger.warning(f"[METRICS] Logs Insights query failed for {log_group}: {e}")
        return None


def _row_ints(rows: list, field: str) -> list[int]:
    return [int(float(_parse_row(row).get(field, 0) or 0)) for row in rows]


def _field_int(fields: dict, field: str) -> int:
    return int(float(fields.get(field, 0) or 0))


def _turn_totals(rows: list) -> dict:
    """``sessions``, ``hourly``, ``avg_events`` and ``tool_calls`` from
    :data:`_TURNS_QUERY`'s per-hour rows, all from the same folded turns.

    Hours with no sessions are left out of the sparkline. ``tool_calls`` is
    ``None`` (unmeasured) unless every session had a tool count.
    """
    hours = [h for h in map(_parse_row, rows) if _field_int(h, "sessions") > 0]
    sessions = sum(_field_int(h, "sessions") for h in hours)
    events = sum(float(h.get("events", 0) or 0) for h in hours)
    tooled = sum(_field_int(h, "tooled") for h in hours)
    return {
        "sessions": sessions,
        "hourly": [_field_int(h, "sessions") for h in hours],
        "avg_events": events / sessions if sessions else 0.0,
        "tool_calls": (sum(_field_int(h, "tool_calls") for h in hours)
                       if tooled == sessions else None),
    }


def _orchestrator_turns(logs, log_group: str, hours: float) -> dict | None:
    """:func:`_turn_totals` of the turns query; ``None`` when the query failed
    or its rows are unreadable, zero sessions when it succeeded with none."""
    rows = _insights_rows(logs, log_group, _TURNS_QUERY, hours)
    if rows is None:
        return None
    try:
        return _turn_totals(rows)
    except _UNREADABLE_ROW as e:
        logger.warning(f"[METRICS] Unreadable turn rows for {log_group}: {e}")
        return None


def _first_count(logs, log_group: str, query: str, field: str, hours: float) -> int:
    """The first row's ``field``; 0 when the query failed, returned no rows,
    or its row is unreadable."""
    rows = _insights_rows(logs, log_group, query, hours)
    try:
        return _row_ints(rows[:1], field)[0] if rows else 0
    except _UNREADABLE_ROW as e:
        logger.warning(f"[METRICS] Unreadable Insights row for {log_group}: {e}")
        return 0


def _hourly_buckets(logs, log_group: str, query: str, hours: float) -> list[int] | None:
    """Per-hour counts, oldest first; ``None`` when the query failed or its
    rows are unreadable, ``[]`` when it succeeded with no rows."""
    rows = _insights_rows(logs, log_group, query, hours)
    if rows is None:
        return None
    try:
        return _row_ints(rows, "sessions")
    except _UNREADABLE_ROW as e:
        logger.warning(f"[METRICS] Unreadable sparkline rows for {log_group}: {e}")
        return None


_NO_TURNS = {"sessions": 0, "hourly": [], "avg_events": 0.0, "tool_calls": None}


def _get_metrics(session, runtime_id: str, runtime_name: str, hours: float) -> dict:
    """Session metrics from the runtime's CloudWatch log group (named by
    :func:`_fleet_log_group`).

    An orchestrator session is a turn with an ``ORCHESTRATOR COMPLETE`` line,
    each turn counted once however many copies of its lines the group holds
    (:data:`_TURNS_QUERY`). When that query succeeds, its count and hourly
    buckets are the session count and sparkline even at 0, and
    ``sessions_measured`` and ``hourly_measured`` are True;
    ``tool_calls_measured`` is True when every session's tool count was read.
    When it fails, and always for a system agent, a zero count falls back to
    the hourly per-session stream count, then to the number of per-session
    log streams.
    """
    logs = session.client("logs")
    log_group = _fleet_log_group(runtime_name, runtime_id)
    if "orchestrator" in runtime_name:
        turns = _orchestrator_turns(logs, log_group, hours)
        measured = turns is not None
        turns = turns or _NO_TURNS
    else:
        buckets = _hourly_buckets(logs, log_group, _STREAM_HOURLY_QUERY, hours)
        measured, turns = False, dict(_NO_TURNS, hourly=buckets or [])
    sessions = turns["sessions"]
    result = {
        "invocations_24h": sessions,
        "avg_latency_s": None,
        "tool_calls_24h": turns["tool_calls"] or 0,
        "tool_calls_measured": measured and turns["tool_calls"] is not None,
        "avg_events_per_session": round(turns["avg_events"], 0) if sessions else None,
        "log_group": log_group,
        "hourly_sessions": turns["hourly"],  # per-hour counts for the sparkline
        "sessions_measured": measured,
        "hourly_measured": measured,
    }
    if not sessions and not result["sessions_measured"]:
        result["invocations_24h"] = (
            sum(result["hourly_sessions"]) if result["hourly_sessions"]
            else _first_count(logs, log_group, _STREAM_COUNT_QUERY, "sessions", hours))
    return result


def _get_account_id(session) -> str:
    try:
        return session.client("sts").get_caller_identity()["Account"]
    except Exception:
        return os.environ.get("AWS_ACCOUNT_ID", "")


#: AgentCore writes one record per scored trace to the config's results log
#: group, in the OpenTelemetry GenAI evaluation shape: ``name`` is
#: ``gen_ai.evaluation.result`` and ``attributes`` carries
#: ``gen_ai.evaluation.name`` (the evaluator), ``gen_ai.evaluation.score.value``
#: and ``gen_ai.evaluation.score.label``
#: (https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/results-and-output.html).
#: Measured on a real results log, the reader's earlier guessed formats
#: (``"evaluatorId":...``, ``"evaluatorName":...``, ``"name":...,"value":...``)
#: returned no rows, and these two queries returned every scored trace. One
#: schema, one log group and one window for every evaluator.
EVAL_SCORE_QUERY = """fields @timestamp
| filter name = "gen_ai.evaluation.result"
| stats avg(`attributes.gen_ai.evaluation.score.value`) as avg_score, count() as count
        by `attributes.gen_ai.evaluation.name` as evaluator"""
EVAL_LABEL_QUERY = """fields @timestamp
| filter name = "gen_ai.evaluation.result"
| stats count() as count by `attributes.gen_ai.evaluation.name` as evaluator,
        `attributes.gen_ai.evaluation.score.label` as label"""


def parse_eval_rows(rows: list) -> dict:
    """Pure row parse for evaluation scores (no boto3 — fixture-tested in
    tests/unit/test_eval_scores_parse.py).

    *rows* are raw Logs Insights results (list of ``[{"field","value"},...]``)
    of ``EVAL_SCORE_QUERY``, aliased to ``evaluator``/``avg_score``/``count``.
    Returns ``{evaluator: {"avg": float, "count": int}}`` with the
    ``Builtin.`` prefix stripped; degenerate rows (empty/"0" evaluator,
    missing or negative avg, no counted traces, unparsable numbers) are
    skipped, and empty/missing input yields ``{}``. A 0.0 average over counted
    traces is kept: every answer in the window scored 0. The code-based figure
    check (its name, or its id ``<name>-<suffix>``) is keyed
    ``FIGURE_CHECK_KEY``; its scores are 1.0/0.0, so its average is a pass
    rate (its label counts replace it, :func:`merge_figure_check`).
    """
    scores = {}
    for row in rows:
        r = _parse_row(row)
        ev = r.get("evaluator", "")
        # Strip Builtin. prefix
        ev = ev.replace("Builtin.", "").replace("builtin.", "")
        figure_check = _is_figure_check(ev)
        if figure_check:
            ev = FIGURE_CHECK_KEY
        if ev and ev != "0":
            try:
                # A missing value is skipped, never read as 0: float(None) raises.
                avg = round(float(r.get("avg_score")), 3)
                cnt = int(float(r.get("count", 0)))
                # Scores are 0..1, so 0.0 over counted traces is a real result
                # (every answer scored 0) and is kept for every evaluator.
                if avg >= 0 and cnt > 0:
                    scores[ev] = {"avg": avg, "count": cnt}
            except (ValueError, TypeError):
                pass
    return scores


#: The Evals page key of the code-based figure check (agents/eval_figure_check.py).
FIGURE_CHECK_KEY = "FigureCheck"


def _is_figure_check(evaluator: str) -> bool:
    """The figure check's evaluator name, its id (``<name>-<suffix>``), or an
    ARN ending in either."""
    evaluator = evaluator.rsplit("/", 1)[-1]
    return evaluator == FIGURE_CHECK_EVALUATOR or evaluator.startswith(f"{FIGURE_CHECK_EVALUATOR}-")


def figure_check_counts(rows: list) -> dict:
    """``{label: count}`` of the figure check's results from
    ``EVAL_LABEL_QUERY`` rows: its label field, on its own rows only; other
    evaluators, unknown labels and unreadable counts are skipped. NO_FIGURES
    and UNCHECKED carry no value, so only these counts see them."""
    counts: dict = {}
    for row in rows:
        r = _parse_row(row)
        label = r.get("label")
        if not _is_figure_check(r.get("evaluator", "")) or label not in FIGURE_CHECK_LABELS:
            continue
        try:
            counts[label] = counts.get(label, 0) + int(float(r.get("count", "")))
        except (ValueError, TypeError):
            continue
    return counts


def merge_figure_check(scores: dict, counts: dict) -> dict:
    """*scores* with the figure check's entry built from its label *counts*:
    ``avg`` the pass rate over PASS and FAIL (``None`` before either exists),
    ``count`` those traces, and the NO_FIGURES and UNCHECKED counts beside it,
    never averaged in. No counts leave *scores* unchanged."""
    if not counts:
        return scores
    scored = counts.get("PASS", 0) + counts.get("FAIL", 0)
    return {**scores, FIGURE_CHECK_KEY: {
        "avg": round(counts.get("PASS", 0) / scored, 3) if scored else None,
        "count": scored, "no_figures": counts.get("NO_FIGURES", 0),
        "unchecked": counts.get("UNCHECKED", 0)}}


def _eval_candidate_groups(logs, config_id: str) -> list[str]:
    """Candidate evaluation-result log groups for *config_id* (known paths
    first, then best-effort discovery by prefix — the compatibility net)."""
    candidate_groups = [
        f"/aws/bedrock-agentcore/evaluations/results/{config_id}",
        f"/aws/bedrock-agentcore/evaluations/{config_id}",
    ]
    try:
        resp = logs.describe_log_groups(logGroupNamePrefix="/aws/bedrock-agentcore/evaluations")
        for lg in resp.get("logGroups", []):
            name = lg.get("logGroupName", "")
            if config_id in name and name not in candidate_groups:
                candidate_groups.append(name)
    except Exception:  # nosec B110 - intentional fallback, no recovery action needed
        pass
    return candidate_groups


def _get_eval_scores(session, config_id: str, hours: float) -> dict:
    if not config_id:
        return {}
    logs = session.client("logs")
    # Always use a wide window for evals — they're sparse and lag behind sessions
    eval_hours = max(hours, 168)  # at least 7 days
    candidate_groups = _eval_candidate_groups(logs, config_id)
    for log_group in candidate_groups:
        scores = _scores_in(logs, log_group, eval_hours)
        if scores:
            logger.info(f"Eval scores found in {log_group}: {list(scores.keys())}")
            return scores
    logger.info(f"No eval scores found for config {config_id} "
                f"(checked {len(candidate_groups)} log groups)")
    return {}


def _scores_in(logs, log_group: str, eval_hours: float) -> dict:
    """Every evaluator's scores in one log group and window: the value
    averages, with the figure check's entry from its label counts."""
    try:
        averages = parse_eval_rows(_run_insights(logs, log_group, EVAL_SCORE_QUERY, eval_hours))
        labels = figure_check_counts(_run_insights(logs, log_group, EVAL_LABEL_QUERY, eval_hours))
    except Exception as e:
        logger.debug(f"Eval query failed for {log_group}: {e}")
        return {}
    return merge_figure_check(averages, labels)


@router.get("")
async def get_agents(hours: float = 1.0):
    """Return agent fleet status — local or cloud per the active runtime."""
    provider = get_fleet_provider(_agent_manager)
    agents = await provider.get_fleet(hours)
    return {"agents": agents, "hours": hours, "generated_at": datetime.now(timezone.utc).isoformat()}


@router.get("/gate-health")
async def gate_health():
    """Current-replica control measurements; annotation-only, no persistence.

    ``/api/admin`` is admin-gated by AuthMiddleware. The admission snapshot is
    lock-coherent and never mutates admission state; no question text, user
    identity, token label, customer data, or persisted state enters this payload.
    """
    from . import card_live
    from .query_admission import get_admission
    from .server import _ask_queue_maxsize
    return {
        "scope": "current-replica",
        "admission": get_admission().snapshot(),
        "card_live_mode": card_live.live_mode(),
        "ask_stream_queue_bound": _ask_queue_maxsize(),
    }


# ── Native fleet metrics: batched GetMetricData (replaces the hot-path scans) ──
# The old fleet build ran, per agent, 3 Logs Insights queries (start_query+poll,
# seconds each) plus ~20-30 SERIAL get_metric_statistics token calls — all on the
# request. It now runs ONE batched CloudWatch GetMetricData round-trip for the
# WHOLE fleet (per-system tool activity + latency + errors from AWS/Bedrock-
# AgentCore, per-runtime invocations/latency/errors/memory, and per-model tokens/
# latency from AWS/Bedrock), built generically from the ``<system_id>___<tool>``
# gateway Name prefix (see agents/fleet_metrics.py). The only remaining Logs
# Insights work is the app-semantic pieces that are NOT native metrics — the
# orchestrator session count + avg-events-per-session and the sparse eval scores —
# and those run ONLY inside the background snapshot compute (never on the request).


def _custom_metric_namespace() -> str | None:
    """Phase-2 seam: the custom CloudWatch namespace the runtime metric filters
    emit to (``AGENTS_CUSTOM_METRIC_NAMESPACE``, e.g. ``ADT/Agents``). When set,
    the batched query also reads ``SessionCount``/``SessionEvents``, but the
    orchestrator card does not use them (:func:`_fleet_orchestrator_usage`):
    the filter counts each copy of a COMPLETE line, so every turn twice."""
    ns = os.environ.get("AGENTS_CUSTOM_METRIC_NAMESPACE", "").strip()
    return ns or None


def _list_tool_names(session) -> list[str]:
    """Enumerate active gateway tool ``Name`` dim values (a cheap metadata call —
    NOT a data scan). Generic: returns whatever ``<system_id>___<tool>`` names the
    account currently exposes, so per-system aggregation needs no literals."""
    cw = session.client("cloudwatch")
    names: set[str] = set()
    try:
        paginator = cw.get_paginator("list_metrics")
        for page in paginator.paginate(
            Namespace=fleet_metrics.NS_AGENTCORE,
            MetricName="Invocations",
            Dimensions=[{"Name": "Method", "Value": "tools/call"}],
        ):
            for m in page.get("Metrics", []):
                for d in m.get("Dimensions", []):
                    if d.get("Name") == "Name" and "___" in (d.get("Value") or ""):
                        names.add(d["Value"])
    except Exception as e:
        logger.warning(f"list tool names failed: {e}")
    return sorted(names)


def _get_metric_data(session, queries: list[dict], hours: float) -> list[dict]:
    """One batched GetMetricData round-trip (paginated on NextToken; values for a
    shared Id are concatenated). ``ScanBy=TimestampAscending`` so hourly series
    read oldest→newest for the sparkline."""
    if not queries:
        return []
    from datetime import timedelta
    cw = session.client("cloudwatch")
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=hours)
    merged: dict[str, dict] = {}
    order: list[str] = []
    token = None
    while True:
        kwargs = dict(MetricDataQueries=queries, StartTime=start, EndTime=end,
                      ScanBy="TimestampAscending")
        if token:
            kwargs["NextToken"] = token
        resp = cw.get_metric_data(**kwargs)
        for r in resp.get("MetricDataResults", []):
            rid = r.get("Id")
            if rid not in merged:
                merged[rid] = {"Id": rid, "Label": r.get("Label"),
                               "Timestamps": [], "Values": []}
                order.append(rid)
            merged[rid]["Timestamps"].extend(r.get("Timestamps", []) or [])
            merged[rid]["Values"].extend(r.get("Values", []) or [])
        token = resp.get("NextToken")
        if not token:
            break
    return [merged[i] for i in order]


def _fleet_token_pools(parsed: dict, region: str, orch_model_id: str,
                       agent_model_id: str) -> tuple[dict, dict, dict, dict]:
    """The orchestrator, system-agent, overlap and whole-fleet token/cost pools from AWS/Bedrock.

    Each pool sums the configured model plus the older models of its tier
    (legacy metric buckets: ``sonnet`` and ``nova_lite``). Every extra key is
    the id the catalog resolves the model to (its global profile when it has
    one), which is the ModelId Bedrock reports its tokens under; cost comes
    from the curated catalog."""
    from .model_catalog import pricing as _catalog_pricing, resolve_id
    bedrock_tokens: dict[str, dict] = {}
    for mid, m in parsed["models"].items():
        prices = _catalog_pricing(mid)
        cost = (m["input"] / 1000) * prices["input"] + (m["output"] / 1000) * prices["output"]
        bedrock_tokens[mid] = {"input": m["input"], "output": m["output"],
                               "latency_ms": m["latency_ms"], "cost": cost}

    def _merge(keys):
        merged = {"input": 0, "output": 0, "latency_ms": None, "cost": 0.0}
        for k in keys:
            entry = bedrock_tokens.get(k, {})
            merged["input"] += entry.get("input", 0)
            merged["output"] += entry.get("output", 0)
            merged["cost"] += entry.get("cost", 0.0)
            if entry.get("latency_ms") and not merged["latency_ms"]:
                merged["latency_ms"] = entry["latency_ms"]
        return merged

    sonnet_keys = {orch_model_id,
                   resolve_id("anthropic.claude-sonnet-4-6", region),
                   resolve_id("anthropic.claude-sonnet-4-5-20250929-v1:0", region)}
    nova_keys = {agent_model_id,
                 resolve_id("amazon.nova-2-lite-v1:0", region),
                 resolve_id("amazon.nova-lite-v1:0", region)}
    # Bedrock reports tokens per ModelId, not per runtime, so a model bucket in
    # both pools (the same model picked for both roles, or a legacy bucket one
    # role's model shares with the other's) cannot be split between them. Each
    # pool keeps it, so no row shows a false zero, and the fleet totals are the
    # union of the two pools, each bucket counted once. Like the pools, they
    # cover the CURRENTLY configured models (plus the legacy buckets): a model
    # switched away from earlier in the window is not counted, and they are
    # the account's AWS/Bedrock usage of those models.
    overlap = _merge(sonnet_keys & nova_keys)
    return (_merge(sonnet_keys), _merge(nova_keys), overlap,
            _merge(sonnet_keys | nova_keys))


def _mark_fleet_totals(agents: list[dict], overlap: dict, fleet: dict) -> None:
    """Give every row the fleet's measured totals, and flag overlapping buckets.

    ``fleet_*`` is the union of both pools' model buckets, each counted once,
    so the header never adds the same tokens twice and never loses tokens to
    a per-row share's rounding. When a bucket is in both pools, every LLM row
    carries ``model_shared_across_roles``: its figures include tokens the
    other role's rows also show, so they may overstate its own share."""
    shared = bool(overlap.get("input") or overlap.get("output"))
    for a in agents:
        a["fleet_input_tokens_24h"] = fleet.get("input", 0)
        a["fleet_output_tokens_24h"] = fleet.get("output", 0)
        a["fleet_cost_usd_24h"] = round(fleet.get("cost", 0.0), 4)
        if shared and not a.get("no_llm"):
            a["model_shared_across_roles"] = True


def _fleet_log_group(name: str, rid: str) -> str:
    """Runtime CloudWatch log group (same derivation as _get_metrics): the
    agentRuntimeId already carries "{name}-{short_id}", so avoid re-prefixing."""
    short_id = rid.split("/")[-1] if "/" in rid else rid
    if short_id.startswith(name):
        return f"/aws/bedrock-agentcore/runtimes/{short_id}-DEFAULT"
    return f"/aws/bedrock-agentcore/runtimes/{name}-{short_id}-DEFAULT"


def _fleet_orchestrator_usage(fc: dict, rt: dict, rtm: dict) -> dict:
    """The orchestrator's sessions, sparkline, evals and its (sonnet) pool."""
    name, rid = rt["agentRuntimeName"], rt["agentRuntimeId"]
    invocations = rtm.get("invocations", 0)
    hourly = rtm.get("hourly", [])
    tool_calls = fc["total_tool_calls"]
    # The optional custom-namespace SessionCount (rtm["sessions"]) is not read:
    # its metric filter adds one point per COMPLETE log line, and a deployed
    # runtime writes every line twice, so it counts each turn twice. The
    # Insights count (background only) folds the copies into one turn.
    ins = _get_metrics(fc["session"], rid, name, fc["hours"])
    # A measured COMPLETE count is the session count even at 0: the native
    # Invocations and hourly series also count the warmup pings, which run
    # no orchestrator turn.
    if ins.get("sessions_measured") or ins.get("invocations_24h"):
        invocations = ins.get("invocations_24h", 0)
    if ins.get("hourly_measured") or ins.get("hourly_sessions"):
        hourly = ins.get("hourly_sessions", [])
    # The turns' own tool calls include the ones that do not go through
    # the gateway (the graph tool), which the gateway metric never sees.
    if ins.get("tool_calls_measured"):
        tool_calls = ins.get("tool_calls_24h", 0)
    avg_events = ins.get("avg_events_per_session")
    pool = fc["orch_pool"]
    return {
        "invocations": invocations, "hourly": hourly, "avg_events": avg_events,
        "tool_calls": tool_calls,
        "eval_scores": _get_eval_scores(fc["session"], EVAL_CONFIG_MAP.get(name, ""), fc["hours"]),
        "input_tok": pool.get("input", 0), "output_tok": pool.get("output", 0),
        "cost": round(pool.get("cost", 0.0), 4),
        "lat_ms": pool.get("latency_ms") or rtm.get("latency_ms"),
        "model_id": fc["orch_model_id"],
    }


def _fleet_system_usage(fc: dict, rt: dict, rtm: dict, is_tools_runtime: bool) -> dict:
    """A system runtime's usage; its tokens are assigned in the second pass
    (share by invocations). adt_digital_thread is a knowledge-graph TOOLS
    runtime: the orchestrator sends it Cypher directly, it runs NO LLM, so it
    reports no model (the Agents page labels it "Tools · no LLM"). The others
    report the LIVE configured agent model (mirrors LocalFleetProvider), NOT
    the stale per-system s.agent_model graph snapshot. (adt_onboarding DOES run
    an LLM for field mapping, so it is not flagged.)"""
    sid = fleet_metrics.runtime_system_key(rt["agentRuntimeName"])
    return {
        "invocations": rtm.get("invocations", 0), "hourly": rtm.get("hourly", []),
        "avg_events": None, "tool_calls": fc["tools"].get(sid, {}).get("calls", 0),
        "eval_scores": {}, "input_tok": 0, "output_tok": 0, "cost": 0.0,
        "lat_ms": fc["agent_pool"].get("latency_ms") or rtm.get("latency_ms"),
        "model_id": "" if is_tools_runtime else fc["agent_model_id"],
    }


def _fleet_agent_entry(fc: dict, rt: dict) -> dict:
    """One runtime's fleet row (the frontend's per-agent dict shape)."""
    name, rid = rt["agentRuntimeName"], rt["agentRuntimeId"]
    rtm = fc["rt_metrics"].get(name, {})
    last = rt.get("lastUpdatedAt", "")
    # Tools-only runtime (no LLM): the digital-thread graph runtime.
    is_tools_runtime = "digital_thread" in name
    usage = (_fleet_orchestrator_usage(fc, rt, rtm) if "orchestrator" in name
             else _fleet_system_usage(fc, rt, rtm, is_tools_runtime))
    log_group = _fleet_log_group(name, rid)
    lat_ms, avg_events = usage["lat_ms"], usage["avg_events"]
    return {
        "name": name,
        "runtime_name": name,
        "display_name": AGENT_DISPLAY_NAMES.get(name, name.replace("adt_", "").replace("_", " ").title()),
        "runtime_id": rid,
        "status": rt.get("status", "UNKNOWN"),
        "last_updated": last.isoformat() if hasattr(last, "isoformat") else str(last),
        "has_eval": bool(EVAL_CONFIG_MAP.get(name, "")),
        "model_id": usage["model_id"],
        "no_llm": is_tools_runtime,
        "invocations_24h": usage["invocations"],
        "avg_latency_s": round(lat_ms / 1000, 2) if lat_ms else None,
        "tool_calls_24h": usage["tool_calls"],
        "avg_events_per_session": round(avg_events, 0) if avg_events else None,
        "hourly_sessions": usage["hourly"],
        "log_group": log_group,
        "input_tokens_24h": usage["input_tok"],
        "output_tokens_24h": usage["output_tok"],
        "cost_usd_24h": usage["cost"],
        "cost_per_request": 0.0,
        "eval_scores": usage["eval_scores"],
        **agentcore_console_links(fc["region"], log_group),
    }


def _share_agent_pool(agents: list[dict], pool: dict) -> None:
    """Second pass: distribute the SYSTEM-AGENT model token/cost pool (whatever
    the configured/overridden agent model is; ``nova_lite`` is just the legacy
    metric-bucket label) by invocation share, ONLY across system agents that
    actually run an LLM. adt_digital_thread is tools-only (no_llm): the
    orchestrator authors its Cypher, so that LLM work is already counted in the
    orchestrator's numbers; giving the graph runtime a share both fabricated its
    cost AND diluted the real agents' shares. Its sessions/invocations stay
    reported (each is a real Cypher round-trip)."""
    sys_agents = [a for a in agents
                  if "orchestrator" not in a["name"] and not a.get("no_llm")]
    total_sys_sessions = sum(a["invocations_24h"] for a in sys_agents) or 1
    pool_input = pool.get("input", 0)
    pool_output = pool.get("output", 0)
    pool_cost = pool.get("cost", 0.0)
    for a in sys_agents:
        share = a["invocations_24h"] / total_sys_sessions
        a["input_tokens_24h"] = int(pool_input * share)
        a["output_tokens_24h"] = int(pool_output * share)
        a["cost_usd_24h"] = round(pool_cost * share, 4)
        a["cost_per_request"] = round(a["cost_usd_24h"] / max(a["invocations_24h"], 1), 6)
    for a in agents:
        if "orchestrator" in a["name"]:
            sessions = a["invocations_24h"]
            if sessions == 0 and a["input_tokens_24h"] > 0:
                sessions = max(1, round(a["input_tokens_24h"] / 3000))
            if sessions > 0:
                a["cost_per_request"] = round(a["cost_usd_24h"] / sessions, 4)


def _compute_cloud_fleet(hours: float = 1.0) -> list[dict]:
    """Compute the cloud fleet from native CloudWatch metrics (one batched
    GetMetricData) + the small orchestrator-only Insights enrichment.

    This is the SNAPSHOT COMPUTE — it runs in the background poll / startup warm /
    cold-miss, never on the request hot path (the request serves the warmed
    snapshot via :func:`_cached_fleet`). Same per-agent dict shape as before.

    The per-system ``s.agent_model`` graph property is a snapshot baked at onboard
    time and drifts when the deployment model is changed later, so the fleet shows
    the LIVE configured agent model (``get_configured_models``) instead, mirroring
    LocalFleetProvider and /api/admin/llm-settings."""
    session = _boto_session()
    runtimes = _get_runtimes(session)
    configured_models = _get_configured_models(session)
    orch_model_id = configured_models["orchestrator"]
    agent_model_id = configured_models["agent"]
    region = os.environ.get("AWS_REGION", "us-east-1")

    # ── ONE batched GetMetricData for the whole fleet ─────────────────────
    from .model_catalog import all_resolved_ids
    model_ids = sorted(({m for m in all_resolved_ids(region)} |
                        {orch_model_id, agent_model_id}) - {""})
    queries, index = fleet_metrics.build_fleet_queries(
        runtimes, _list_tool_names(session), model_ids, hours,
        custom_namespace=_custom_metric_namespace(),
    )
    parsed = fleet_metrics.parse_fleet_metrics(_get_metric_data(session, queries, hours), index)
    orch_pool, agent_pool, overlap_pool, fleet_pool = _fleet_token_pools(
        parsed, region, orch_model_id, agent_model_id)
    tools = parsed["tools"]
    fc = {
        "session": session, "hours": hours, "region": region,
        "rt_metrics": parsed["runtimes"], "tools": tools,
        # The total gateway tools/call across all systems (per-system counts
        # land on each system). The orchestrator card shows it only when the
        # turns' own tool-call count was not measured (_fleet_orchestrator_usage).
        "total_tool_calls": sum(t.get("calls", 0) for t in tools.values()),
        "orch_pool": orch_pool, "agent_pool": agent_pool,
        "orch_model_id": orch_model_id, "agent_model_id": agent_model_id,
    }
    agents = [_fleet_agent_entry(fc, rt) for rt in runtimes]
    _share_agent_pool(agents, agent_pool)
    _mark_fleet_totals(agents, overlap_pool, fleet_pool)
    agents.sort(key=lambda a: (AGENT_ORDER.index(a["name"]) if a["name"] in AGENT_ORDER else 99, a["name"]))
    return agents


# ── Snapshot TTL cache (mirrors the dashboard cards cache) ─────────────────────
# The GET handler serves this warmed snapshot INSTANTLY; a background poll (see
# server.py, AGENTS_FLEET_POLL) recomputes it on a TTL so no request ever blocks
# on the compute above. Keyed by the requested ``hours`` bucket (the frontend
# offers 8h / 24h / 1w); the background warmer keeps the primary bucket fresh.
_FLEET_CACHE: dict[str, dict] = {}


def _fleet_ttl_seconds() -> float:
    """Snapshot TTL (seconds). Override via ``AGENTS_FLEET_TTL``; 0 disables caching."""
    try:
        return float(os.environ.get("AGENTS_FLEET_TTL", "150"))
    except ValueError:
        return 150.0


def _fleet_primary_hours() -> float:
    """The window the background warmer keeps fresh (frontend default = 24h).
    Override via ``AGENTS_FLEET_PRIMARY_HOURS``."""
    try:
        return float(os.environ.get("AGENTS_FLEET_PRIMARY_HOURS", "24"))
    except ValueError:
        return 24.0


def _fleet_background_warming_active() -> bool:
    """True when the background fleet poll is wired (``AGENTS_FLEET_POLL``). When
    active, a present-but-expired snapshot is served as-is rather than blocking the
    request on a fresh compute — the warmer's next tick refreshes it."""
    return bool(os.environ.get("AGENTS_FLEET_POLL", "").strip())


def _hours_key(hours: float) -> str:
    return f"{float(hours):g}"


def invalidate_fleet_cache() -> None:
    """Drop all cached fleet snapshots (e.g. after onboarding changes the roster)."""
    _FLEET_CACHE.clear()


def refresh_fleet_cache(hours: float | None = None) -> list[dict]:
    """FORCE-recompute the snapshot for ``hours`` (default = primary window) and
    store it. FAIL-SOFT: never raises into the scheduler/startup — on error it
    logs and leaves the prior snapshot intact. Respects ``AGENTS_FLEET_TTL=0``."""
    hours = _fleet_primary_hours() if hours is None else hours
    key = _hours_key(hours)
    if _fleet_ttl_seconds() <= 0:
        try:
            return _compute_cloud_fleet(hours)
        except Exception:
            logger.warning("refresh_fleet_cache: compute failed (cache disabled)", exc_info=True)
            return (_FLEET_CACHE.get(key) or {}).get("agents") or []
    try:
        agents = _compute_cloud_fleet(hours)
    except Exception:
        logger.warning("refresh_fleet_cache: compute failed; leaving prior snapshot", exc_info=True)
        return (_FLEET_CACHE.get(key) or {}).get("agents") or []
    _FLEET_CACHE[key] = {"ts": time.monotonic(), "agents": agents}
    return agents


def _cached_fleet(hours: float) -> list[dict]:
    """Serve the warmed snapshot for ``hours``; compute once on a cold miss.

    Warm hit → cached. Present-but-expired while the background warmer is active →
    serve slightly-stale (never block on a fresh compute). Cold (never computed) →
    compute inline once, then cache. ``AGENTS_FLEET_TTL=0`` disables caching."""
    ttl = _fleet_ttl_seconds()
    if ttl <= 0:
        return _compute_cloud_fleet(hours)
    key = _hours_key(hours)
    now = time.monotonic()
    entry = _FLEET_CACHE.get(key)
    if entry and entry.get("agents") is not None and (now - entry["ts"]) < ttl:
        return entry["agents"]
    if entry and entry.get("agents") is not None and _fleet_background_warming_active():
        return entry["agents"]  # serve stale; the warmer refreshes on its next tick
    agents = _compute_cloud_fleet(hours)
    _FLEET_CACHE[key] = {"ts": now, "agents": agents}
    return agents


async def _build_cloud_fleet(hours: float = 1.0) -> list[dict]:
    """Cloud fleet for the request hot path — returns the warmed snapshot cache
    instantly (compute runs off the event loop only on a cold miss)."""
    import asyncio
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _cached_fleet, hours)


def log_tail(lines: int = 40):
    """Return recent orchestrator log lines for live tail display."""
    session = _boto_session()
    logs = session.client("logs")

    # Dynamically find the orchestrator runtime log group
    control = session.client("bedrock-agentcore-control")
    try:
        from .runtime_provision_helpers import list_all_agent_runtimes
        orch_id = ""
        for r in list_all_agent_runtimes(control):
            if r.get("agentRuntimeName") == "adt_orchestrator":
                orch_id = r["agentRuntimeId"]
                break
        if not orch_id:
            return {"lines": [], "error": "Orchestrator runtime not found"}
        log_group = f"/aws/bedrock-agentcore/runtimes/{orch_id}-DEFAULT"
    except Exception as e:
        return {"lines": [], "error": "Failed to find orchestrator: " + public_error_text(
            e, "agents.log_tail.find_runtime", default="upstream_error")}

    # Find the most recently active log stream
    try:
        streams = logs.describe_log_streams(
            logGroupName=log_group,
            orderBy="LastEventTime",
            descending=True,
            limit=3,
        )
        log_streams = [s["logStreamName"] for s in streams.get("logStreams", [])]
    except Exception as e:
        return {"lines": [], **public_error(e, "agents.log_tail.streams", default="upstream_error")}

    if not log_streams:
        return {"lines": [], "error": "No log streams found"}

    # Pull recent events from the latest streams (queries create new streams)
    events = []
    try:
        for stream_name in log_streams[:3]:
            resp = logs.get_log_events(
                logGroupName=log_group,
                logStreamName=stream_name,
                limit=lines,
                startFromHead=False,
            )
            raw = resp.get("events", [])
            for e in raw:
                msg = e.get("message", "").strip()
                ts = e.get("timestamp", 0)
                import re
                msg = re.sub(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d+ \S+ \S+ ', '', msg)
                # Filter out noise — only show meaningful orchestrator activity
                if not msg:
                    continue
                if any(skip in msg for skip in ['Found credentials from IAM', 'Invalid HTTP request received']):
                    continue
                events.append({"ts": ts, "msg": msg})
        # Sort by timestamp, keep most recent
        events.sort(key=lambda x: x["ts"])
        events = events[-lines:]
    except Exception as e:
        logger.warning(f"log_tail failed: {e}")

    return {"lines": events, "stream": log_streams[0], "log_group": log_group}


@router.get("/log-tail")
async def log_tail_route(lines: int = 40):
    """Recent orchestrator log lines for the live tail. Sync body: ``log_tail``
    (AgentCore listing and CloudWatch Logs reads)."""
    return await offload(log_tail, lines=lines)


def eval_debug():
    """Debug endpoint — inspect raw eval log groups and sample messages.
    Sync body (CloudWatch Logs reads); the route offloads it."""
    session = _boto_session()
    logs = session.client("logs")
    result = {"log_groups": [], "sample_messages": {}}

    try:
        resp = logs.describe_log_groups(logGroupNamePrefix="/aws/bedrock-agentcore/evaluations")
        for lg in resp.get("logGroups", []):
            name = lg.get("logGroupName", "")
            result["log_groups"].append({
                "name": name,
                "stored_bytes": lg.get("storedBytes", 0),
                "retention_days": lg.get("retentionInDays"),
            })
            # Grab a few sample messages from this group
            try:
                streams = logs.describe_log_streams(
                    logGroupName=name, orderBy="LastEventTime", descending=True, limit=1
                )
                for stream in streams.get("logStreams", []):
                    events = logs.get_log_events(
                        logGroupName=name,
                        logStreamName=stream["logStreamName"],
                        limit=3,
                        startFromHead=False,
                    )
                    result["sample_messages"][name] = [e["message"] for e in events.get("events", [])]
            except Exception as e:
                result["sample_messages"][name] = ["Error: " + public_error_text(
                    e, "agents.eval_debug.samples", default="upstream_error")]
    except Exception as e:
        result.update(public_error(e, "agents.eval_debug", default="upstream_error"))

    return result


@router.get("/eval-debug")
async def eval_debug_route():
    """Raw eval log groups and sample messages. Sync body: ``eval_debug``."""
    return await offload(eval_debug)


#: Neptune Analytics on-demand USD per m-NCU-hour (1 m-NCU = 1 GB of graph
#: memory), from the AWS Price List API ``AmazonNeptune``
#: ``MemoryOptimizedGraphUsage:16-m-NCU`` hourly SKU per region, divided by 16
#: (the rate is linear across sizes), retrieved 2026-10-28. us-east-1: SKU
#: NBECQJKR9S6A9SYY, "$0.48 per Hrs for MemoryOptimizedGraphUsage:16-m-NCU".
#: A region not listed here shows no cost estimate rather than a guessed one.
NEPTUNE_ANALYTICS_MNCU_HOUR_USD: dict[str, float] = {
    "us-east-1": 0.03,
    "us-east-2": 0.03,
    "us-west-2": 0.03,
    "eu-west-1": 0.033,
    "eu-central-1": 0.0363125,
    "ap-northeast-1": 0.0363125,
    "ap-southeast-2": 0.0363,
}
#: A stopped graph's rate as a fraction of the running rate: exactly 0.1 in
#: every region above (Price List ``MemoryOptimizedStoppedGraphUsage:16-m-NCU``,
#: retrieved 2026-10-28; us-east-1 $0.048 vs $0.48 per hour).
NEPTUNE_ANALYTICS_STOPPED_RATE_FRACTION = 0.1


def get_neptune_metrics(hours: float = 24.0):
    """Return Neptune Analytics graph metrics and cost estimate."""
    region = os.environ.get("AWS_REGION", "eu-central-1")
    graph_id = os.environ.get("GRAPH_IDENTIFIER", "")
    result = {
        "graph_id": graph_id,
        "hours": hours,
        "status": "unknown",
        "provisioned_memory_gb": None,
        "ncu_count": None,         # m-NCUs (1 m-NCU = 1 GB of graph memory)
        "mncu_price_per_hour": None,  # USD per m-NCU-hour at billing_state
        "billing_state": None,     # "running" | "stopped": the graph's state now
        "cost_usd": None,          # cost for the requested window (hours)
        "cost_ncu": None,          # m-NCU-hour component (the whole bill)
        "query_rate_avg": None,
        "query_rate_peak": None,
        "cpu_avg": None,
        "cpu_peak": None,
        "edges": None,
        "vertex_properties": None,
        "graph_size_mb": None,
        "replica_count": 0,
    }
    _neptune_graph_details(result, region, graph_id)
    _neptune_cloudwatch_stats(result, region, graph_id, hours)
    _neptune_cost_estimate(result, region, hours)
    return result


def _neptune_graph_details(result: dict, region: str, graph_id: str) -> None:
    """Status, provisioned memory, m-NCU count and replicas from the Neptune API."""
    import boto3
    try:
        graph = boto3.client("neptune-graph", region_name=region).get_graph(
            graphIdentifier=graph_id)
        result["status"] = graph.get("status", "unknown")
        mem_gb = graph.get("provisionedMemory", 0)
        result["provisioned_memory_gb"] = mem_gb
        # Neptune Analytics capacity is provisioned in m-NCUs: 1 m-NCU = 1 GB of
        # graph memory, so the m-NCU count IS the provisioned memory.
        result["ncu_count"] = mem_gb
        result["replica_count"] = graph.get("replicaCount", 0)
    except Exception as e:
        logger.warning(f"Neptune graph API failed: {e}")


def _neptune_cloudwatch_stats(result: dict, region: str, graph_id: str, hours: float) -> None:
    """Query rate, CPU, edges, properties and graph size from AWS/Neptune metrics."""
    import boto3
    from datetime import datetime, timezone, timedelta
    try:
        cw = boto3.client("cloudwatch", region_name=region)
        end = datetime.now(timezone.utc)
        # One datapoint for the whole window up to 24 h; hourly points beyond.
        period = max(3600, int(hours * 3600))
        window = {"start": end - timedelta(hours=hours), "end": end,
                  "period": period if period <= 86400 else 3600}

        def stat(metric, kind):
            return _neptune_cw_stat(cw, graph_id, metric, kind, window)

        result["query_rate_avg"] = stat("NumOpenCypherRequestsPerSec", "Average")
        result["query_rate_peak"] = stat("NumOpenCypherRequestsPerSec", "Maximum")
        result["cpu_avg"] = stat("CPUUtilization", "Average")
        result["cpu_peak"] = stat("CPUUtilization", "Maximum")
        result["edges"] = stat("NumEdges", "Maximum")
        result["vertex_properties"] = stat("NumVertexProperties", "Maximum")
        size_bytes = stat("GraphSizeBytes", "Maximum")
        if size_bytes:
            result["graph_size_mb"] = round(size_bytes / 1024 / 1024, 1)
    except Exception as e:
        logger.warning(f"Neptune CloudWatch metrics failed: {e}")


def _neptune_cw_stat(cw, graph_id: str, metric: str, kind: str, window: dict):
    """One AWS/Neptune statistic over the window (Average or Maximum); None if absent."""
    try:
        resp = cw.get_metric_statistics(
            Namespace="AWS/Neptune", MetricName=metric,
            Dimensions=[{"Name": "GraphIdentifier", "Value": graph_id}],
            StartTime=window["start"], EndTime=window["end"],
            Period=window["period"], Statistics=[kind],
        )
        vals = [p[kind] for p in resp.get("Datapoints", []) if p.get(kind) is not None]
        if not vals:
            return None
        return sum(vals) / len(vals) if kind == "Average" else max(vals)
    except Exception:
        return None


def _neptune_cost_estimate(result: dict, region: str, hours: float) -> None:
    """m-NCU-hours x the region's Price List rate; no storage or I/O charge.

    A STOPPED graph is priced at the stopped rate, 10% of the running rate in
    every listed region (Price List ``MemoryOptimizedStoppedGraphUsage``). The
    whole window is priced at the graph's CURRENT state (no state history is
    read); ``billing_state`` says which. Snapshots are billed separately and
    not shown. An estimate, not a quote: see
    https://aws.amazon.com/neptune/pricing/. A region without a listed rate
    gets no estimate rather than a guessed one.
    """
    rate = NEPTUNE_ANALYTICS_MNCU_HOUR_USD.get(region)
    stopped = result.get("status") == "STOPPED"
    result["billing_state"] = "stopped" if stopped else "running"
    if rate is not None and stopped:
        rate = round(rate * NEPTUNE_ANALYTICS_STOPPED_RATE_FRACTION, 10)
    result["mncu_price_per_hour"] = rate
    if result["ncu_count"] is not None and rate is not None:
        total_mncu = result["ncu_count"] * (1 + result["replica_count"])
        result["cost_ncu"] = round(total_mncu * rate * hours, 4)
        result["cost_usd"] = result["cost_ncu"]


@router.get("/neptune")
async def get_neptune_metrics_route(hours: float = 24.0):
    """Return Neptune Analytics graph metrics and cost estimate. Sync body: ``get_neptune_metrics``."""
    return await offload(get_neptune_metrics, hours=hours)
