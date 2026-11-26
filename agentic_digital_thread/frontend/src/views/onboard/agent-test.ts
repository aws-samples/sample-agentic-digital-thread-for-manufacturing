// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* The verify step's agent test (agents/onboard_verify_agent.py): the new
 * system's own query tool, called through the gateway as a polled job.
 *
 * The wizard (wizard.ts) runs it after the verification line with the
 * onboarding's abort signal; the chat's verify card (components/AgentTestLine)
 * watches one run per card through `watchAgentTest`, so a re-render or a
 * second subscriber never starts a second job. */
import { requestJson } from '../../api/client';
import type {
  AgentTestResult,
  AgentTestStartResponse,
  AgentTestStatusResponse,
} from '../../api/types/onboard';
import { isAbortError, pollJob, throwIfAborted } from './job-poll';
import { obStepErrorText, type StepAttempt } from './step-retry';

export const AGENT_TEST_URL = '/api/onboard/verify/agent-test';
/** The server's deadline when the start reply names none. */
const DEFAULT_DEADLINE_S = 180;
/** How long past the server's own deadline the client keeps polling. */
export const AGENT_TEST_POLL_MARGIN_MS = 60_000;

const STEP_LABELS: Record<string, string> = {
  runtime: 'runtime',
  gateway_target: 'gateway target',
  tool: 'tool',
};

function bareTool(name: string | null | undefined): string {
  const text = String(name || '');
  const cut = text.lastIndexOf('___');
  return cut === -1 ? text : text.slice(cut + 3);
}

function seconds(value: number): string {
  const s = Number.isFinite(value) && value > 0 ? value : 0;
  return String(s < 10 ? Math.round(s * 10) / 10 : Math.round(s));
}

/** The line shown for a finished agent test. */
export function agentTestMessage(result: AgentTestResult): string {
  const reason = String(result.reason || '');
  if (result.outcome === 'passed') {
    const tool = bareTool(result.tool);
    const table = result.table || 'the table';
    const took = `in ${seconds(Number(result.elapsed_s))} s`;
    const known = result.rows !== null && result.rows !== undefined;
    const rows = Number(result.rows ?? 0);
    const counted = `${rows.toLocaleString('en-US')} ${rows === 1 ? 'row' : 'rows'}`;
    if (result.query_kind === 'question') {
      // The capture proves the agent read the table; the number is its answer.
      return known
        ? `Agent test passed: ${tool} read ${table} and answered ${counted} ${took}`
        : `Agent test passed: ${tool} read ${table} ${took}`;
    }
    return `Agent test passed: ${tool} returned ${counted} from ${table} ${took}`;
  }
  if (result.outcome === 'not_run') return `Agent test not run: ${reason}`;
  if (result.outcome === 'no_read') {
    // An API or hub system records no statement, so the read can't be
    // confirmed by design; the gateway path itself worked.
    if (result.read_provable === false) {
      return `Agent test passed: ${bareTool(result.tool)} answered through the gateway in ${seconds(Number(result.elapsed_s))} s`;
    }
    return `Agent test inconclusive: ${reason}`;
  }
  const label = result.step ? STEP_LABELS[result.step] || result.step : '';
  return label ? `Agent test failed at ${label}: ${reason}` : `Agent test failed: ${reason}`;
}

/** The line shown while the job runs. */
export function agentTestRunningText(elapsedMs: number): string {
  const s = Math.round(elapsedMs / 1000);
  return s > 0 ? `Running agent test (${s} s)` : 'Running agent test';
}

/**
 * Start the agent test for `systemId` and poll it until it finishes, its job
 * is lost or the server's deadline (plus a margin) passes. Every failure
 * resolves `{ok:false, error}`; cancellation rejects with AbortError.
 */
export async function runAgentTest(
  systemId: string,
  signal: AbortSignal,
  onRunning?: () => void,
): Promise<StepAttempt<AgentTestResult>> {
  try {
    const start = await requestJson<AgentTestStartResponse>(AGENT_TEST_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ system_id: systemId }),
      signal,
    });
    throwIfAborted(signal);
    const startErr = obStepErrorText(null, start);
    if (startErr) return { ok: false, error: startErr };
    if (!start.job_id) return { ok: false, error: 'the server returned no job id' };
    const deadlineMs = (start.deadline_s || DEFAULT_DEADLINE_S) * 1000 + AGENT_TEST_POLL_MARGIN_MS;
    const poll = await pollJob<AgentTestStatusResponse>(`${AGENT_TEST_URL}/${start.job_id}`, signal, {
      deadlineMs,
      messages: {
        failed: 'the job failed',
        lost: 'the job was lost (the server restarted)',
        timedOut: `no result from the server within ${Math.round(deadlineMs / 1000)} s`,
      },
      onRunning: () => onRunning?.(),
    });
    if (!poll.ok) return poll;
    if (!poll.data.result) return { ok: false, error: 'the server returned no result' };
    return { ok: true, data: poll.data.result };
  } catch (e) {
    if (isAbortError(e, signal)) throw e;
    return { ok: false, error: (e as Error).message };
  }
}

/** What a watcher sees: running (with its line) or the finished line. */
export type AgentTestView =
  | { phase: 'running'; text: string }
  | { phase: 'done'; outcome: AgentTestResult['outcome']; text: string };

interface Run {
  view: AgentTestView;
  listeners: Set<(view: AgentTestView) => void>;
}

const runs = new Map<string, Run>();

function finished(res: StepAttempt<AgentTestResult>): AgentTestView {
  if (res.ok) return { phase: 'done', outcome: res.data.outcome, text: agentTestMessage(res.data) };
  return { phase: 'done', outcome: 'failed', text: `Agent test failed: ${res.error}` };
}

/**
 * Watch the agent test run for `key` (one per chat verify card), starting it
 * the first time. The listener gets the current view at once and every change
 * after; the returned function stops listening (the run itself goes on until
 * its deadline, so a remounted card picks up where it left off).
 */
export function watchAgentTest(
  key: string,
  systemId: string,
  listener: (view: AgentTestView) => void,
): () => void {
  let run = runs.get(key);
  if (!run) {
    const created: Run = {
      view: { phase: 'running', text: agentTestRunningText(0) },
      listeners: new Set(),
    };
    runs.set(key, created);
    run = created;
    const started = Date.now();
    const publish = (view: AgentTestView): void => {
      created.view = view;
      created.listeners.forEach((l) => l(view));
    };
    void runAgentTest(systemId, new AbortController().signal, () =>
      publish({ phase: 'running', text: agentTestRunningText(Date.now() - started) }),
    )
      .then((res) => publish(finished(res)))
      .catch((e: unknown) => publish(finished({ ok: false, error: (e as Error).message })));
  }
  const current = run;
  current.listeners.add(listener);
  listener(current.view);
  return () => {
    current.listeners.delete(listener);
  };
}

/** Forget every run (tests). */
export function resetAgentTestRuns(): void {
  runs.clear();
}
