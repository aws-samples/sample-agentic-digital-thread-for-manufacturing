#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
eval_answers.py — golden-set answer evaluation, deterministic-only (E2).

Runs a pinned golden set of questions through the real agent path
(POST /api/ask, SSE) and scores each answer against DETERMINISTIC
expectations: minimum grounding counters from the intelligence_summary,
required/forbidden answer regexes, honest-absence markers, and an
injection-canary guard. The LLM NEVER produces a verdict or the exit code
(ADR-0053): scoring is pure computed functions over the parsed stream.

This lane COMPLEMENTS scripts/smoke_prompts.py and scripts/audit_learning.py —
it never replaces either. Shared machinery (stdlib HTTP client, cookie login,
SSE parsing, session namespaces, transient-retry policy, error/fabrication
classifiers) is imported from the sibling smoke_prompts module, not duplicated.

Golden questions live in the DEMO layer (agents/demo/golden_evals.json) —
dataset literals never enter this generic engine. Questions may carry
"skip_unless_env": "<ENV>" (skipped unless that env var is truthy) or
"requires_seed": true (skipped when --no-seed-fixtures is passed).

A/B diff: --previous <report.json> adds an INFORMATIONAL variant-aware A/B
diff (the report's "trend" block, C3): per-id signed deltas (+ pct) over
every numeric per-question metric, content-verdict transitions, metric-drift
flags at pinned thresholds, and a suite rollup. An invalid arm (transport loss
or state instability) is non-comparable and produces no regression or metric
delta. Informational by DEFAULT (ADR-0053 D-E2): single-run deltas on a live
LLM path flake — routing, retries, and model nondeterminism produce run-to-run
metric variance larger than most real signals, so the gate remains the golden
expectations and the diff is evidence for a HUMAN comparing configurations.
Opt-in --strict-regression exits 1 iff a valid comparison records >=1 content
pass→fail regression; metric drift NEVER gates, even under the flag.

Usage:
  python3 scripts/eval_answers.py --base-url https://<dist> \
      --password "$ADT_PASSWORD" [--golden-set <path>] [--strict] \
      [--report /tmp/eval.json] [--previous <prior.json>] \
      [--variant <name>] [--strict-regression] \
      [--repeats 1|N>=5] [--no-memory] [--memory-suppression marker|field] \
      [--no-seed-fixtures] [--timeout 200]
Runs ALL goldens (no partial-run flag by design). Env fallbacks:
ADT_BASE_URL, ADT_USERNAME (default ADT), ADT_PASSWORD, EVAL_REPORT,
EVAL_STRICT, EVAL_GOLDEN_SET, EVAL_VARIANT, EVAL_STRICT_REGRESSION,
EVAL_REPEATS.
Exit 0 = every content verdict passes and the completed measurement is valid;
1 = content failure or invalid completed campaign (transport loss/state
instability, without relabeling content); 2 = usage/login/fetch error, or the
distinct T0.2 env-unhealthy abort (a '[Briefing skipped' answer persisting
after an unstick, or >2 unsticks in one invocation).
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path


def _import_smoke():
    """Import the sibling smoke_prompts module (package or script context)."""
    try:
        from scripts import smoke_prompts  # unit tests: repo root on sys.path
        return smoke_prompts
    except ImportError:
        scripts_dir = str(Path(__file__).resolve().parent)
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        import smoke_prompts  # script context: python3 scripts/eval_answers.py
        return smoke_prompts


smoke = _import_smoke()


def _import_eval_core():
    """Import agents.eval_core (package or script context).

    The pure scoring core lives in agents/eval_core.py (the container image
    copies only agents/, so the server runs the same lane). Mirrors
    _import_smoke: package context first (unit tests: repo root on
    sys.path), then the script context fallback (python3
    scripts/eval_answers.py from anywhere).
    """
    try:
        from agentic_digital_thread.agents import eval_core
        return eval_core
    except ImportError:
        adt_dir = str(Path(__file__).resolve().parents[1]
                      / "agentic_digital_thread")
        if adt_dir not in sys.path:
            sys.path.insert(0, adt_dir)
        from agents import eval_core
        return eval_core


eval_core = _import_eval_core()

# ── Re-exports from agents/eval_core.py (extraction, behavior-preserving) ───
# Every moved name is re-exported at module scope so importers of THIS module
# (the unit tests, smoke_prompts' lazy detector import, and
# eval_feedback_exploration's single-source pins) are unchanged. The
# smoke-dependent --strict layer, the A/B diff, transport, unstick, warmup,
# and the parser stay HERE in scripts/.
ABSENCE_HONESTY_MARKERS = eval_core.ABSENCE_HONESTY_MARKERS
INJECTION_CANARY_MARKER = eval_core.INJECTION_CANARY_MARKER
_QUANTITY_UNIT_PATTERN = eval_core._QUANTITY_UNIT_PATTERN
_BARE_NUMBER_PATTERN = eval_core._BARE_NUMBER_PATTERN
_TEMPORAL_PATTERN = eval_core._TEMPORAL_PATTERN
_IDENTIFIER_PATTERN = eval_core._IDENTIFIER_PATTERN
_FABRICATION_EXCERPT_CHARS = eval_core._FABRICATION_EXCERPT_CHARS
_split_sentences = eval_core._split_sentences
FOLLOWUPS_MARKER = eval_core.FOLLOWUPS_MARKER
answer_body = eval_core.answer_body
fabrication_flags = eval_core.fabrication_flags
absence_markers_matched = eval_core.absence_markers_matched
_honesty_block = eval_core._honesty_block
_metrics_block = eval_core._metrics_block
golden_file_sha = eval_core.golden_file_sha
default_golden_set_path = eval_core.default_golden_set_path
DEFAULT_GOLDEN_SET = eval_core.default_golden_set_path()
ANSWER_EXCERPT_CHARS = eval_core.ANSWER_EXCERPT_CHARS
GOLDEN_SET_VERSION = eval_core.GOLDEN_SET_VERSION
_LEAK_CLASSES = eval_core._LEAK_CLASSES
_TREATMENT_PHASES = eval_core._TREATMENT_PHASES
_validate_golden_markings = eval_core._validate_golden_markings
load_golden_set = eval_core.load_golden_set
should_skip = eval_core.should_skip
fill_story_fields = eval_core.fill_story_fields
_expect_min_failures = eval_core._expect_min_failures
_expect_answer_failures = eval_core._expect_answer_failures
score_question = eval_core.score_question
_skipped_row = eval_core._skipped_row
_transport_failure_row = eval_core._transport_failure_row
_suite_totals = eval_core._suite_totals
suite_exit_code = eval_core.suite_exit_code


def strict_question_failures(result: dict, summary: dict) -> list[str]:
    """--strict extension: fold in the smoke fabrication/outage classifiers.

    The classifiers receive the answer BODY (``eval_core.answer_body``): the
    reserved ---FOLLOWUPS--- suggestions restate outage and memory wording
    ("Retry when the endpoint is available?"), which failed honest answers.
    Fixing only the core would leave this seam scoring chips.
    """
    err_labels = smoke.classify_error_labels(
        result.get("tool_summaries", []) + result.get("errors", []))
    return [f"{reason} (strict)" for reason in smoke.strict_failure_reasons(
        err_labels=err_labels, answer=answer_body(result.get("answer", "")),
        system_queries=(summary or {}).get("system_queries", 0),
        followup_mode=False,
    )]


# Numeric C2 metric keys diffed per question (bool planned_with_present is
# excluded); elapsed_seconds joins from the row level. The additive
# planned_with_authority_rules (T1.6) is deliberately NOT diffed: the 13-key
# diff contract is pinned (W2 T5), and the count is a near-zero-variance
# graph-state function read directly from the metrics block per arm.
_NUMERIC_METRIC_KEYS = (
    "total_tool_calls", "neptune_queries", "system_queries",
    "graph_coverage_pct", "wasted_calls", "deterministic_pct",
    "grounded_steps", "rules_applied_count", "new_proposed_mappings",
    "input_tokens", "output_tokens", "total_tokens", "cost_latency_ms",
)
_DIFF_KEYS = _NUMERIC_METRIC_KEYS + ("elapsed_seconds",)
# D3 pinned metric-drift thresholds: (min +pct, min +absolute or None).
# Informational annotations ONLY — drift never gates, even under
# --strict-regression.
_DRIFT_THRESHOLDS = {
    "total_tool_calls": (25.0, 2.0),
    "total_tokens": (25.0, None),
    "elapsed_seconds": (50.0, 10.0),
}


def _row_metric(row: dict, key: str):
    """Numeric metric value from a report row, or None when unmeasured."""
    source = row if key == "elapsed_seconds" else (row.get("metrics") or {})
    value = source.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _metric_deltas(prev_row: dict, row: dict) -> tuple[dict, list]:
    """Signed per-metric deltas (current − previous, + _pct) and drift flags.

    A delta is null when EITHER side is unmeasured (no imputation); _pct is
    present only when the previous value is a nonzero number. From a ZERO
    previous value the absolute threshold alone flags drift (pct-only
    metrics cannot flag from zero).
    """
    deltas, flags = {}, []
    for key in _DIFF_KEYS:
        prior, current = _row_metric(prev_row, key), _row_metric(row, key)
        if prior is None or current is None:
            deltas[key] = None
            continue
        delta = current - prior
        deltas[key] = round(delta, 2) if isinstance(delta, float) else delta
        pct = None
        if prior != 0:
            pct = round((current - prior) / prior * 100, 1)
            deltas[f"{key}_pct"] = pct
        rule = _DRIFT_THRESHOLDS.get(key)
        if rule and pct is not None and pct >= rule[0] and (
                rule[1] is None or delta >= rule[1]):
            flags.append(f"metric-drift: {key} {pct:+.0f}%")
        elif rule and prior == 0 and rule[1] is not None and delta >= rule[1]:
            # From-zero drift: percentage is undefined, so the absolute
            # threshold alone flags (a 0→8 tool-call jump must not be
            # invisible). Informational only — never touches exit codes.
            flags.append(f"metric-drift: {key} {delta:+g} (from 0)")
    return deltas, flags


def _transition(prev_verdict, verdict) -> str:
    """Verdict transition label. ONLY pass→fail counts as a regression."""
    if prev_verdict == "pass" and verdict == "fail":
        return "regression"
    if prev_verdict == "fail" and verdict == "pass":
        return "improvement"
    return "stable"


def _row_measurement_valid(row: dict) -> bool:
    """Validity for new and legacy rows; old transport/state markers count."""
    if row.get("measurement_valid") is False:
        return False
    if row.get("state_instability") is True or row.get("transport_failure"):
        return False
    return not (row.get("transport_failure_count")
                or row.get("transport_failures"))


def _non_comparable_deltas() -> dict:
    """No metric conclusion is computed from an invalid arm."""
    return {key: None for key in _DIFF_KEYS}


def _effective_verdicts(prev: dict, row: dict) -> tuple:
    """A/B content verdict pair, retaining compatibility with old reports."""
    if (prev.get("content_verdict") is not None
            and row.get("content_verdict") is not None):
        return prev["content_verdict"], row["content_verdict"]
    if (prev.get("majority_verdict") is not None
            and row.get("majority_verdict") is not None):
        return prev["majority_verdict"], row["majority_verdict"]
    return prev.get("verdict"), row.get("verdict")


def _diff_questions(prev_by_id: dict, rows: list[dict]) -> list[dict]:
    """Per-id A/B entries; invalid arm pairs are explicitly non-comparable."""
    entries = []
    for row in rows:
        prev = prev_by_id.get(row["id"])
        if prev is None:
            entries.append({"id": row["id"], "status": "new",
                            "verdict": row.get("verdict")})
            continue
        prev_verdict, verdict = _effective_verdicts(prev, row)
        comparable = (_row_measurement_valid(prev)
                      and _row_measurement_valid(row))
        if comparable:
            deltas, flags = _metric_deltas(prev, row)
            transition = _transition(prev_verdict, verdict)
        else:
            deltas, flags = _non_comparable_deltas(), []
            transition = "non-comparable"
        entries.append({
            "id": row["id"], "status": "compared",
            "comparable": comparable,
            "previous_verdict": prev_verdict, "verdict": verdict,
            "transition": transition, "deltas": deltas, "flags": flags,
        })
    current_ids = {row["id"] for row in rows}
    entries.extend({"id": qid, "status": "missing",
                    "previous_verdict": prev.get("verdict")}
                   for qid, prev in prev_by_id.items()
                   if qid not in current_ids)
    return entries


def _metric_totals(pairs: list[tuple[dict, dict]]) -> dict:
    """Suite totals per rollup metric, over ids measured on BOTH sides."""
    totals = {}
    for key in ("total_tool_calls", "total_tokens", "elapsed_seconds"):
        prev_sum = cur_sum = 0
        measured = 0
        for prev_row, row in pairs:
            prior, current = _row_metric(prev_row, key), _row_metric(row, key)
            if prior is None or current is None:
                continue
            prev_sum, cur_sum, measured = (prev_sum + prior, cur_sum + current,
                                           measured + 1)
        if not measured:
            totals[key] = {"previous": None, "current": None, "delta_pct": None}
            continue
        pct = (round((cur_sum - prev_sum) / prev_sum * 100, 1)
               if prev_sum else None)
        totals[key] = {"previous": round(prev_sum, 2),
                       "current": round(cur_sum, 2), "delta_pct": pct}
    return totals


def _ab_suite(prev_rows: list, rows: list, entries: list, pairs: list) -> dict:
    """C3 suite rollup; invalid comparisons never enter metric totals."""
    compared = [e for e in entries if e["status"] == "compared"]
    return {
        "pass_previous": sum(1 for q in prev_rows
                             if q.get("verdict") == "pass"),
        "pass_current": sum(1 for q in rows if q.get("verdict") == "pass"),
        "regressions": sum(1 for e in compared
                           if e["transition"] == "regression"),
        "improvements": sum(1 for e in compared
                            if e["transition"] == "improvement"),
        "non_comparable": sum(1 for e in compared
                              if e["transition"] == "non-comparable"),
        "metric_totals": _metric_totals(pairs),
    }


def _sha_non_comparable_reason(previous_sha, current_sha) -> str:
    if not previous_sha or not current_sha:
        return "golden-set-sha-missing"
    return "" if previous_sha == current_sha else "golden-set-sha-mismatch"


def _sha_non_comparable_questions(prev_rows: list, rows: list,
                                  reason: str) -> list[dict]:
    entries = [{"id": row["id"], "status": "non-comparable", "reason": reason}
               for row in rows if row.get("id")]
    current_ids = {row.get("id") for row in rows}
    entries.extend({"id": row["id"], "status": "non-comparable", "reason": reason}
                   for row in prev_rows
                   if row.get("id") and row["id"] not in current_ids)
    return entries


def ab_diff(previous: dict, rows: list[dict], *, variant=None,
            golden_set_sha=None) -> dict:
    """Build comparison products only after golden SHA comparability exists."""
    prev_rows = [q for q in (previous or {}).get("questions", []) if q.get("id")]
    previous_sha = (previous or {}).get("golden_set_sha")
    sha_reason = _sha_non_comparable_reason(previous_sha, golden_set_sha)
    base = {"informational": True, "variant": variant,
            "previous_variant": (previous or {}).get("variant")}
    if sha_reason:
        print("WARN: golden set SHA unavailable or mismatched — no A/B "
              "comparison products computed", flush=True)
        return {**base, "comparable": False,
                "questions": _sha_non_comparable_questions(
                    prev_rows, rows, sha_reason), "suite": {}}
    prev_by_id = {q["id"]: q for q in prev_rows}
    entries = _diff_questions(prev_by_id, rows)
    pairs = [(prev_by_id[row["id"]], row) for row in rows
             if row["id"] in prev_by_id
             and _row_measurement_valid(prev_by_id[row["id"]])
             and _row_measurement_valid(row)]
    validity_matches = all(
        _row_measurement_valid(row) for row in prev_rows + rows
        if row.get("verdict") != "skipped")
    if not validity_matches:
        print("WARN: invalid measurement arm — A/B diff marked comparable: false",
              flush=True)
    return {**base, "comparable": validity_matches, "questions": entries,
            "suite": _ab_suite(prev_rows, rows, entries, pairs)}


def build_eval_report(*, base_url: str, namespace, golden_set: str, strict: bool,
                      rows: list[dict], previous: dict | None = None,
                      variant: str = "",
                      golden_set_sha: str | None = None,
                      repeats: int = 1, no_memory: bool = False,
                      memory_suppression: str = "marker",
                      unsticks: list | None = None,
                      run_path: str = "cli") -> dict:
    """CLI wrapper over eval_core.build_eval_report — stamps run_path="cli".

    The core builder REQUIRES run_path (every persisted report says which
    path produced it — "cli" through CloudFront vs "server" through the
    internal chat client); this CLI entry point defaults it to "cli". The
    A/B diff builder (ab_diff) stays in scripts/, so the informational
    trend block is computed HERE and injected into the core report.
    """
    trend = None
    if previous is not None:
        trend = ab_diff(previous, rows, variant=variant or None,
                        golden_set_sha=golden_set_sha)
    return eval_core.build_eval_report(
        base_url=base_url, namespace=namespace, golden_set=golden_set,
        strict=strict, rows=rows, run_path=run_path, variant=variant,
        golden_set_sha=golden_set_sha, repeats=repeats, no_memory=no_memory,
        memory_suppression=memory_suppression, unsticks=unsticks, trend=trend)


def final_exit_code(rows: list[dict], report: dict,
                    strict_regression: bool) -> int:
    """DEFAULT exit contract byte-identical to suite_exit_code (D3).

    Opt-in --strict-regression additionally exits 1 iff the A/B diff records
    >=1 verdict pass→fail regression. Metric drift NEVER gates, even under
    the flag.
    """
    code = suite_exit_code(rows)
    if code == 0 and strict_regression:
        suite = (report.get("trend") or {}).get("suite") or {}
        if suite.get("regressions", 0) >= 1:
            return 1
    return code


def _repeats_default(env) -> int:
    """EVAL_REPEATS default (1 on unset/garbage — never crash the parser)."""
    try:
        return max(1, int(env.get("EVAL_REPEATS", "") or 1))
    except ValueError:
        return 1


def _add_instrumentation_args(ap, env) -> None:
    """T0.2 measurement-campaign flags (additive; defaults keep legacy shape)."""
    ap.add_argument("--repeats", type=int, default=_repeats_default(env),
                    help="run each golden N times with FRESH sessions; use "
                         "1 (legacy single-run shape) or >=5 for a majority "
                         "campaign; rows separate content verdict, delivery, "
                         "and measurement validity (EVAL_REPEATS)")
    ap.add_argument("--no-memory", action="store_true",
                    help="mint tryit-eval-briefing-<uuid> session ids: the "
                         "tryit- prefix trips the runtime's _is_tryit_session "
                         "learning/usage/cost containment AND the briefing "
                         "substring trips the server's is_briefing memory "
                         "suppression (write_memory=False); '[Briefing "
                         "skipped' answers run the L4-1 unstick procedure")
    ap.add_argument("--memory-suppression", choices=("marker", "field"),
                    default="marker",
                    help="with --no-memory: marker (default) = briefing "
                         "session-id marker; field = send write_memory:false "
                         "in the request body instead of the briefing marker "
                         "(post-T1.7 servers), KEEPING the tryit- prefix for "
                         "runtime containment; the mode is recorded in the "
                         "report")


def build_parser(environ=None) -> argparse.ArgumentParser:
    """Build the CLI parser, accepting an environment mapping for pure tests."""
    env = os.environ if environ is None else environ
    ap = argparse.ArgumentParser(
        description="Golden-set answer evaluation (deterministic-only scoring)")
    ap.add_argument("--base-url", default=env.get("ADT_BASE_URL", ""))
    ap.add_argument("--username", default=env.get("ADT_USERNAME", "ADT"))
    ap.add_argument("--password", default=env.get("ADT_PASSWORD", ""))
    ap.add_argument("--golden-set",
                    default=env.get("EVAL_GOLDEN_SET", "") or DEFAULT_GOLDEN_SET,
                    help="golden-set JSON path (EVAL_GOLDEN_SET; default: the "
                         "demo goldens file)")
    ap.add_argument("--timeout", type=int, default=300,
                    help="per-question timeout (s). Heavy cross-system goldens "
                         "legitimately run 150-170s; 300 leaves real headroom "
                         "so a slow-but-honest turn is not scored as a "
                         "transport failure (default was 200; raised "
                         "2026-08-07 after live turns hit 156-165s)")
    ap.add_argument("--no-failed-answers", action="store_true",
                    help="do not keep the full answer text on FAILED rows "
                         "(default keeps it: a verdict alone is not diagnosable)")
    ap.add_argument("--no-warmup", action="store_true",
                    help="skip the pre-measurement readiness ask (the warmup "
                         "gate aborts the run when a throwaway ask cannot "
                         "complete end-to-end — e.g. right after a deploy)")
    ap.add_argument("--report", default=env.get("EVAL_REPORT", ""),
                    help="machine-readable JSON report path (EVAL_REPORT; "
                         "default /tmp/eval_answers_report.json)")
    ap.add_argument("--previous", default="",
                    help="previous report JSON for the informational A/B "
                         "diff (never gates by default)")
    ap.add_argument("--variant", default=env.get("EVAL_VARIANT", ""),
                    help="variant label stamped into the report and the A/B "
                         "diff (EVAL_VARIANT; default unset)")
    ap.add_argument("--strict-regression", action="store_true",
                    default=smoke._env_flag("EVAL_STRICT_REGRESSION", env),
                    help="exit 1 iff the A/B diff records >=1 verdict "
                         "pass->fail regression (EVAL_STRICT_REGRESSION); "
                         "metric drift never gates")
    ap.add_argument("--no-seed-fixtures", action="store_true",
                    help="skip goldens marked requires_seed (envs without the "
                         "seeded fixture rows)")
    ap.add_argument("--strict", action="store_true",
                    default=smoke._env_flag("EVAL_STRICT", env),
                    help="also fail on the smoke fabrication/outage signals "
                         "per question (EVAL_STRICT)")
    _add_instrumentation_args(ap, env)
    return ap


class EvalAbortError(RuntimeError):
    """Distinct env-unhealthy abort (L4-1) — exits 2 with its own message."""


# Canned throwaway question for the L4-1 unstick ask (never scored).
UNSTICK_QUESTION = "Health check: please reply with the single word OK."

# Warmup readiness gate (2026-08-07): attempts × linear backoff before the
# first measured ask. 3 × (20s, 40s, 60s waits) tolerates a fresh deploy's
# settling window without masking a genuinely dead environment.
_WARMUP_ATTEMPTS = 3
_WARMUP_BACKOFF_SECONDS = 20
_MAX_UNSTICKS = 2
_BRIEFING_SKIPPED_MARKER = "[Briefing skipped"


def _mint_session_id(namespace, index: int, run: int, args) -> str:
    """Per-ask session id. --no-memory marker mode mints
    tryit-eval-briefing-<uuid>: the tryit- prefix trips the runtime's
    _is_tryit_session learning/usage/cost containment and the briefing
    substring trips the server's is_briefing memory suppression. field mode
    keeps the tryit- prefix but replaces the briefing leg with the
    write_memory:false body field (post-T1.7). Fresh uuid per run = fresh
    sessions; repeats>1 without --no-memory suffix -r<run> for freshness."""
    if args.no_memory:
        marker = ("tryit-eval-briefing-" if args.memory_suppression == "marker"
                  else "tryit-eval-")
        return marker + uuid.uuid4().hex
    base = namespace.session_id(index)
    return base if run == 1 else f"{base}-r{run}"


def _sniff_done_text(lines, sink: list[str]):
    """Pass-through SSE line iterator capturing the done event's data text
    (the '[Briefing skipped' marker arrives ONLY on the done event, which
    the shared smoke parser drops)."""
    event = None
    for raw in lines:
        line = raw.decode("utf-8", "replace").rstrip("\n")
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:") and event == "done":
            try:
                sink.append(json.loads(line[5:].strip()).get("text") or "")
            except (json.JSONDecodeError, AttributeError):
                pass
        yield raw


def _ask_eval(app, args, question_text: str, session_id: str) -> dict:
    """POST /api/ask like smoke.App.ask_stream, plus: the write_memory:false
    body field in --memory-suppression field mode, and done-event text
    capture (result["done_text"]) for the skip-latch detection."""
    body = {"question": question_text, "session_id": session_id,
            "user_id": "smoke"}
    if args.no_memory and args.memory_suppression == "field":
        body["write_memory"] = False
    request = urllib.request.Request(
        app.base + "/api/ask", data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    done_text: list[str] = []
    start = time.monotonic()
    with app.opener.open(request, timeout=args.timeout) as response:
        result = smoke._consume_ask_sse(_sniff_done_text(response, done_text))
    result["elapsed_seconds"] = round(time.monotonic() - start, 2)
    result["done_text"] = "".join(done_text)
    return result


def _briefing_skipped(result: dict) -> bool:
    """True when the server answered with the latched-skip marker (L4-1)."""
    return (_BRIEFING_SKIPPED_MARKER in (result.get("done_text") or "")
            or _BRIEFING_SKIPPED_MARKER in (result.get("answer") or ""))


def _run_unstick(app, args, question_id: str, unsticks: list) -> None:
    """ONE plain unstick ask (L4-1): a session id that is neither briefing-
    marked nor tryit-prefixed, a canned throwaway question, drained to full
    stream completion so the server executes its _query_in_progress = False
    clear. More than _MAX_UNSTICKS per invocation aborts (env unhealthy)."""
    if len(unsticks) >= _MAX_UNSTICKS:
        raise EvalAbortError(
            f"more than {_MAX_UNSTICKS} unsticks required in one run — "
            "env unhealthy")
    session_id = f"unstick-{uuid.uuid4().hex}"
    print(f"    '[Briefing skipped' — unstick ask on {session_id}", flush=True)
    app.ask_stream(UNSTICK_QUESTION, session_id, args.timeout)
    unsticks.append({
        "question_id": question_id, "session_id": session_id,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })


def _ask_with_unstick(app, args, question: dict, session_id: str,
                      unsticks: list) -> tuple[dict, str]:
    """Ask; on a '[Briefing skipped' answer run the unstick procedure.

    The skip marker is a LATCHED server flag (module-global
    _query_in_progress with no try/finally), so a blind retry cannot clear
    it: send ONE plain unstick ask, await full completion (executes the
    server-side flag clear), then retry the eval ask on a fresh session id.
    A retry that STILL skips raises the distinct abort."""
    result = _ask_eval(app, args, question["text"], session_id)
    if not _briefing_skipped(result):
        return result, session_id
    _run_unstick(app, args, question["id"], unsticks)
    retry_id = f"{session_id}-postunstick"
    result = _ask_eval(app, args, question["text"], retry_id)
    if _briefing_skipped(result):
        raise EvalAbortError(
            f"'{_BRIEFING_SKIPPED_MARKER}' persisted after an unstick on "
            f"{question['id']} — server _query_in_progress appears latched; "
            "env unhealthy")
    return result, retry_id


def _scored_row(question: dict, session_id: str, result: dict,
                strict: bool) -> dict:
    """Score one consumed stream into the pinned per-question report row.

    Thin CLI wrapper over eval_core._scored_row: the smoke-dependent
    --strict layer is computed HERE (the stdlib-only core never imports the
    smoke classifiers) and injected as a precomputed strict_failures list.
    """
    strict_failures = None
    if strict and result.get("done"):
        summary = result.get("summary") or {}
        strict_failures = strict_question_failures(result, summary)
    return eval_core._scored_row(question, session_id, result,
                                 strict_failures=strict_failures)


def _request_error_row(question: dict, session_id: str, exc: Exception) -> dict:
    """Report row for a question whose request never produced a stream."""
    return {
        "id": question["id"], "session_id": session_id, "verdict": "fail",
        "failures": [f"request error {type(exc).__name__}"],
        "transport_failure": True,
        "systems_queried": 0, "neptune_queries": 0, "elapsed_seconds": None,
        "answer_excerpt": "",
        "honesty": {"absence_markers_matched": [], "fabrication_flags": []},
        "metrics": None,
    }


def _run_question(app, args, namespace, question: dict, index: int,
                  total: int, run: int = 1, unsticks: list = None) -> dict:
    """Ask one golden question; one narrow transient retry (smoke policy)."""
    session_id = _mint_session_id(namespace, index, run, args)
    unsticks = [] if unsticks is None else unsticks
    label = f" (run {run}/{args.repeats})" if args.repeats > 1 else ""
    print(f"[{index}/{total}] {question['id']}{label}", flush=True)
    try:
        result, session_id = _ask_with_unstick(app, args, question,
                                               session_id, unsticks)
    except smoke._PROMPT_REQUEST_ERRORS as exc:
        if not smoke._is_transient_network_error(exc):
            print(f"    FAIL — request error: {type(exc).__name__}", flush=True)
            return _request_error_row(question, session_id, exc)
        print(f"    transient network error ({type(exc).__name__}) — "
              f"retrying once", flush=True)
        time.sleep(smoke.PROMPT_RETRY_BACKOFF_SECONDS)
        session_id = f"{session_id}-retry"
        try:
            result, session_id = _ask_with_unstick(app, args, question,
                                                   session_id, unsticks)
        except smoke._PROMPT_REQUEST_ERRORS as retry_exc:
            print(f"    FAIL — request error: {type(retry_exc).__name__}",
                  flush=True)
            return _request_error_row(question, session_id, retry_exc)
    if not result.get("done"):
        # Stream started but never delivered its done frame — same transient
        # class as a network error (observed live 2026-08-07: a mid-stream
        # client timeout was scored as content failures). ONE retry on a
        # fresh session; a second incomplete stream reports as transport.
        print("    incomplete stream (no done frame) — retrying once",
              flush=True)
        time.sleep(smoke.PROMPT_RETRY_BACKOFF_SECONDS)
        try:
            result, session_id = _ask_with_unstick(
                app, args, question, f"{session_id}-redeliver", unsticks)
        except smoke._PROMPT_REQUEST_ERRORS as retry_exc:
            return _request_error_row(question, session_id, retry_exc)
    row = _scored_row(question, session_id, result, args.strict)
    return _keep_failed_answer(row, result, args)


def _keep_failed_answer(row: dict, result: dict, args) -> dict:
    """A verdict without the answer is not diagnosable: FAILED rows carry the
    full answer under ``eval_core.FAILED_ANSWER_KEY`` (opt out with
    --no-failed-answers). Passing rows keep the 400-char excerpt only, so the
    report stays small and the passing shape is unchanged."""
    if row.get("verdict") != "fail" or getattr(args, "no_failed_answers", False):
        return row
    return {**row, eval_core.FAILED_ANSWER_KEY: (result.get("answer") or "")}


_INVALIDITY_REASON_CHARS = 160
_MIN_CAMPAIGN_REPEATS = 5


def _state_instability(runs: list[dict]) -> bool:
    """True when graph-state measurements move across DELIVERED repeats."""
    observed = set()
    for run_row in runs:
        metrics = run_row.get("metrics") or {}
        observed.add((metrics.get("planned_with_present"),
                      metrics.get("rules_applied_count")))
    return len(observed) > 1


def _invalidity_reason(transport: int, total: int, unstable: bool,
                        delivered: int) -> str | None:
    """Bounded, categorical invalidity reason; never includes answer text."""
    reasons = []
    if transport:
        reasons.append(f"transport_failure_count={transport}/{total}")
    if delivered == 0:
        reasons.append("no delivered repeats")
    if unstable:
        reasons.append("state_instability across delivered repeats")
    value = "; ".join(reasons)
    return value[:_INVALIDITY_REASON_CHARS] if value else None


def _fold_repeats(runs: list[dict]) -> dict:
    """Fold content over delivered repeats; measure availability separately."""
    delivered = [run for run in runs if not run.get("transport_failure")]
    transport = len(runs) - len(delivered)
    passes = sum(run.get("verdict") == "pass" for run in delivered)
    majority = (
        "pass" if passes > len(delivered) / 2 else "fail"
    ) if delivered else None
    base = (next(run for run in delivered if run.get("verdict") == majority)
            if delivered else runs[0])
    row = dict(base)
    row["verdict"] = majority if majority is not None else "unavailable"
    row["content_verdict"] = majority
    row["majority_verdict"] = majority
    row["repeats"] = [
        {"run": number, "verdict": run["verdict"],
         "content_verdict": (None if run.get("transport_failure")
                             else run["verdict"]),
         "transport_failure": bool(run.get("transport_failure")),
         "failures": run["failures"], "metrics": run["metrics"]}
        for number, run in enumerate(runs, 1)]
    unstable = _state_instability(delivered)
    row["state_instability"] = unstable
    row["delivered_count"] = len(delivered)
    row["transport_failure_count"] = transport
    row["measurement_valid"] = bool(delivered) and not transport and not unstable
    row["invalidity_reason"] = _invalidity_reason(
        transport, len(runs), unstable, len(delivered))
    if transport:
        row["transport_failures"] = transport  # legacy additive alias
    return row


def _repeat_rows(app, args, namespace, question: dict, index: int, total: int,
                 unsticks: list) -> dict:
    """Run one golden --repeats times with fresh sessions; N=1 = legacy row."""
    runs = [_run_question(app, args, namespace, question, index, total,
                          run=run, unsticks=unsticks)
            for run in range(1, args.repeats + 1)]
    return runs[0] if args.repeats == 1 else _fold_repeats(runs)


def _print_row(row: dict) -> None:
    """One-line content verdict plus an orthogonal measurement verdict."""
    if row["verdict"] == "skipped":
        print(f"    SKIP — {row.get('skip_reason', '')}\n", flush=True)
        return
    print(f"    systems={row['systems_queried']} "
          f"neptune={row['neptune_queries']} "
          f"elapsed={row['elapsed_seconds']}s", flush=True)
    if row["verdict"] == "pass":
        print("    CONTENT PASS", flush=True)
    elif row["verdict"] == "unavailable":
        print("    CONTENT UNAVAILABLE", flush=True)
    else:
        for failure in row["failures"]:
            print(f"    CONTENT FAIL — {failure}", flush=True)
    if row.get("measurement_valid") is False:
        print(f"    MEASUREMENT INVALID — {row.get('invalidity_reason')}",
              flush=True)
    print(flush=True)


def _print_ab_rollup(report: dict) -> None:
    """One-line A/B suite rollup after the report path (informational)."""
    trend = report.get("trend")
    if not trend:
        return
    suite = trend["suite"]
    current = trend.get("variant") or "(no variant)"
    prior = trend.get("previous_variant") or "(no variant)"
    if not suite:
        # A non-comparable trend (golden set changed) carries no suite
        # deltas; the report is already written, so say so and stop.
        print(f"A/B {current} vs {prior} (non-comparable): no suite deltas",
              flush=True)
        return
    parts = [f"pass {suite['pass_previous']}→{suite['pass_current']}"]
    for key, label in (("total_tool_calls", "tool calls"),
                       ("total_tokens", "tokens")):
        pct = suite["metric_totals"][key]["delta_pct"]
        parts.append(f"{label} n/m" if pct is None else f"{label} {pct:+.1f}%")
    status = "comparable" if trend.get("comparable") else "non-comparable"
    print(f"A/B {current} vs {prior} ({status}): {', '.join(parts)}",
          flush=True)


def _load_previous(path: str) -> dict | None:
    """Best-effort load of the --previous report (the diff is informational)."""
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"WARN: --previous unreadable ({type(exc).__name__}) — "
              f"trend omitted", flush=True)
        return None


def _write_report(args, report: dict) -> None:
    """Write the machine report; a write failure never changes the verdict."""
    path = args.report or os.path.join("/tmp", "eval_answers_report.json")  # nosec B108 - fixed report path documented in --help; report contains no secrets
    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2)
        print(f"\n== report written: {path} ==", flush=True)
    except OSError as exc:
        print(f"\n== WARN: report write failed ({type(exc).__name__}) ==",
              flush=True)


def _prepare_run(args):
    """Validate usage, load goldens, log in. Returns (app, questions) or None."""
    if not args.base_url or not args.password:
        print("ERROR: --base-url and --password (or ADT_BASE_URL/ADT_PASSWORD)"
              " required")
        return None
    if args.repeats != 1 and args.repeats < _MIN_CAMPAIGN_REPEATS:
        print(f"ERROR: --repeats must be 1 or >= {_MIN_CAMPAIGN_REPEATS} "
              "for majority campaigns")
        return None
    try:
        questions = load_golden_set(args.golden_set)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"ERROR: golden set unreadable: {exc}")
        return None
    app = smoke.App(args.base_url)
    try:
        login = app.login(args.username, args.password)
    except (urllib.error.URLError, *smoke.TRANSIENT_NETWORK_ERRORS) as exc:
        print(f"ERROR: login request failed ({type(exc).__name__})")
        return None
    if login.get("status") != "ok":
        print("ERROR: login failed")
        return None
    return app, questions


def _connected_types(app) -> "set[str] | None":
    """System types the deployment has ONBOARDED, read once from
    ``GET /api/systems`` (status ``connected``); ``None`` when unreadable, so
    a ``requires_connected_types`` golden runs rather than skips."""
    try:
        body = app.call("GET", "/api/systems")
        return {str(s.get("type") or "") for s in body.get("systems") or []
                if s.get("status") == "connected"}
    except (urllib.error.URLError, ValueError, AttributeError,
            *smoke.TRANSIENT_NETWORK_ERRORS) as exc:
        print(f"    note: /api/systems unreadable ({type(exc).__name__}); "
              "requires_connected_types gates run", flush=True)
        return None


#: The steward route that serves the deployed workshop story's answer key
#: (the same read scripts/preflight_workshop.py checks).
STORY_ROUTE = "/api/admin/workshop/story"


def _story_answer_key(app) -> "dict | None":
    """The story answer key from :data:`STORY_ROUTE`; ``None`` when unreadable
    (workshop mode off, not a steward, network), so a ``story_fields`` golden
    skips with a named reason."""
    try:
        body = app.call("GET", STORY_ROUTE)
    except (urllib.error.URLError, ValueError,
            *smoke.TRANSIENT_NETWORK_ERRORS) as exc:
        print(f"    note: {STORY_ROUTE} unreadable ({type(exc).__name__}); "
              "story_fields goldens skip", flush=True)
        return None
    return body if isinstance(body, dict) else None


def _story_reader(app):
    """A reader for :func:`fill_story_fields` that calls the story route at
    most once per run, and only when a golden declares ``story_fields``."""
    cache: list = []

    def read():
        if not cache:
            cache.append(_story_answer_key(app))
        return cache[0]
    return read


def _collect_rows(app, args, namespace, questions: list[dict],
                  unsticks: list) -> list[dict]:
    """Run every golden (skip-aware, repeat-aware); prints per-row verdicts."""
    rows = []
    gated = any(q.get("requires_connected_types") for q in questions)
    connected = _connected_types(app) if gated else None
    read_story = _story_reader(app)
    for index, question in enumerate(questions, 1):
        reason = should_skip(question, no_seed_fixtures=args.no_seed_fixtures,
                             connected_types=connected)
        if not reason:
            question, reason = fill_story_fields(question, read_story)
        row = (_skipped_row(question, reason) if reason
               else _repeat_rows(app, args, namespace, question, index,
                                 len(questions), unsticks))
        _print_row(row)
        rows.append(row)
    return rows


def _warmup_gate(app, args) -> None:
    """Readiness gate before the first MEASURED ask (opt out: --no-warmup).

    Measured live (2026-08-07): asking goldens while the environment was not
    yet serving turns end-to-end produced repeats with no done frame that were
    scored as content failures, poisoning majority verdicts. One throwaway ask
    must complete (done frame present) before measurement starts; up to
    _WARMUP_ATTEMPTS attempts with linear backoff, then an EvalAbortError so
    the run reports "env not ready" instead of a misleading scorecard. The
    warmup session id is unstick-shaped on purpose: it must never be
    briefing-marked, and its full drain leaves the server's serial-ask flag
    clear (the L4-1 semantics)."""
    for attempt in range(1, _WARMUP_ATTEMPTS + 1):
        try:
            result = app.ask_stream(
                UNSTICK_QUESTION, f"unstick-warmup-{uuid.uuid4().hex}",
                args.timeout)
        except smoke._PROMPT_REQUEST_ERRORS as exc:
            result = {"done": False, "errors": [type(exc).__name__]}
        if result.get("done"):
            print(f"    warmup ok (attempt {attempt})\n", flush=True)
            return
        print(f"    warmup attempt {attempt}/{_WARMUP_ATTEMPTS} did not "
              f"complete — env may still be settling", flush=True)
        time.sleep(_WARMUP_BACKOFF_SECONDS * attempt)
    raise EvalAbortError(
        f"warmup ask never completed in {_WARMUP_ATTEMPTS} attempts — the "
        "environment is not serving turns end-to-end (fresh deploy still "
        "settling?); measuring now would produce a misleading scorecard")


def main(argv=None) -> int:
    """Run ALL goldens; deterministic exit contract (0 pass / 1 fail / 2 usage)."""
    args = build_parser().parse_args(argv)
    prepared = _prepare_run(args)
    if prepared is None:
        return 2
    app, questions = prepared
    namespace = smoke.create_session_namespace(prefix="eval")
    print(f"== eval: {len(questions)} goldens; run_id={namespace.run_id} ==\n",
          flush=True)
    unsticks: list[dict] = []
    try:
        if not args.no_warmup:
            _warmup_gate(app, args)
        rows = _collect_rows(app, args, namespace, questions, unsticks)
    except EvalAbortError as exc:
        print(f"\n== ABORT (env unhealthy): {exc} ==", flush=True)
        return 2
    report = build_eval_report(
        base_url=args.base_url, namespace=namespace,
        golden_set=args.golden_set, strict=args.strict, rows=rows,
        previous=_load_previous(args.previous), variant=args.variant,
        golden_set_sha=golden_file_sha(args.golden_set),
        repeats=args.repeats, no_memory=args.no_memory,
        memory_suppression=args.memory_suppression, unsticks=unsticks,
        run_path="cli")
    _write_report(args, report)
    _print_ab_rollup(report)
    code = final_exit_code(rows, report, args.strict_regression)
    suite = report["suite"]
    result = "PASS" if code == 0 else "FAIL"
    if code != 0 and suite.get("measurement_valid") is False \
            and suite.get("fail", 0) == 0:
        result = "INVALID"
    print(f"\n== RESULT: {result} — "
          f"{suite['pass']} content pass / {suite['fail']} content fail / "
          f"{suite['skipped']} skipped ==", flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
