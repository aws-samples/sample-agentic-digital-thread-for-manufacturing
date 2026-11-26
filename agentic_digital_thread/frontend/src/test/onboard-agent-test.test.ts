// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* The verify step's agent test (views/onboard/agent-test.ts): the lines the
 * wizard and the chat's verify card show, and the start + poll of the job
 * behind them (POST /api/onboard/verify/agent-test, then GET .../{job_id}). */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { AgentTestResult } from '../api/types/onboard';
import {
  agentTestMessage,
  agentTestRunningText,
  resetAgentTestRuns,
  runAgentTest,
  watchAgentTest,
  type AgentTestView,
} from '../views/onboard/agent-test';

const passed: AgentTestResult = {
  outcome: 'passed',
  step: null,
  reason: null,
  tool: 'system-a___query_system_a',
  table: 'table_x',
  rows: 1234,
  query_kind: 'question',
  elapsed_s: 12.4,
  deadline_s: 180,
};

describe('agent test lines', () => {
  it('names the tool, the row count, the table and the time on a pass', () => {
    // A question tool: the capture proves the read; the number is the answer.
    expect(agentTestMessage(passed)).toBe(
      'Agent test passed: query_system_a read table_x and answered 1,234 rows in 12 s',
    );
    expect(agentTestMessage({ ...passed, rows: null })).toBe(
      'Agent test passed: query_system_a read table_x in 12 s',
    );
    // A SQL tool: the count is the query's own result.
    expect(agentTestMessage({ ...passed, query_kind: 'sql', rows: 1, elapsed_s: 3.24 })).toBe(
      'Agent test passed: query_system_a returned 1 row from table_x in 3.2 s',
    );
  });

  it.each([
    ['runtime', 'runtime'],
    ['gateway_target', 'gateway target'],
    ['tool', 'tool'],
  ] as const)('names the %s step on a failure', (step, label) => {
    expect(
      agentTestMessage({ outcome: 'failed', step, reason: 'the reason given' }),
    ).toBe(`Agent test failed at ${label}: the reason given`);
  });

  it('says when the test was not run, and why', () => {
    expect(
      agentTestMessage({ outcome: 'not_run', reason: 'the app runs in local mode' }),
    ).toBe('Agent test not run: the app runs in local mode');
  });

  it('says plainly when the tool answered but no read of the table was recorded', () => {
    expect(
      agentTestMessage({
        outcome: 'no_read',
        step: null,
        reason: 'query_system_a answered with a number but recorded no read of table_x',
      }),
    ).toBe(
      'Agent test inconclusive: query_system_a answered with a number but recorded no read of table_x',
    );
  });

  it('shows an API system that answered through the gateway as a pass', () => {
    expect(
      agentTestMessage({
        outcome: 'no_read',
        step: null,
        read_provable: false,
        tool: 'cmms-ind___query_cmms_ind',
        elapsed_s: 41,
        reason: 'query_cmms_ind answered through the gateway. That is expected here.',
      }),
    ).toBe('Agent test passed: query_cmms_ind answered through the gateway in 41 s');
  });

  it('says a failure without a step plainly', () => {
    expect(agentTestMessage({ outcome: 'failed', step: null, reason: 'it stopped' })).toBe(
      'Agent test failed: it stopped',
    );
  });

  it('shows that it is running, with the time so far', () => {
    expect(agentTestRunningText(0)).toBe('Running agent test');
    expect(agentTestRunningText(35_400)).toBe('Running agent test (35 s)');
  });

  it('uses no dashes as punctuation', () => {
    for (const text of [
      agentTestMessage(passed),
      agentTestMessage({ outcome: 'failed', step: 'tool', reason: 'r' }),
      agentTestRunningText(5000),
    ]) {
      expect(text).not.toMatch(/[\u2013\u2014]/);
    }
  });
});

function response(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('running the agent test job', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(0));
    resetAgentTestRuns();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  function stubJob(polls: unknown[]) {
    let poll = 0;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === '/api/onboard/verify/agent-test') {
        expect(init?.method).toBe('POST');
        expect(JSON.parse(String(init?.body))).toEqual({ system_id: 'system-a' });
        return response({ job_id: 'job-1', status: 'running', deadline_s: 180 });
      }
      if (url === '/api/onboard/verify/agent-test/job-1') {
        const body = polls[Math.min(poll, polls.length - 1)];
        poll += 1;
        return response(body);
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);
    return fetchMock;
  }

  it('starts the job and polls it to its result, with the caller signal', async () => {
    const fetchMock = stubJob([{ status: 'running' }, { status: 'done', result: passed }]);
    const controller = new AbortController();
    const running = vi.fn();

    const done = runAgentTest('system-a', controller.signal, running);
    await vi.advanceTimersByTimeAsync(10_000);

    await expect(done).resolves.toEqual({ ok: true, data: passed });
    expect(running).toHaveBeenCalled();
    for (const [, init] of fetchMock.mock.calls) {
      expect(init?.signal).toBe(controller.signal);
    }
  });

  it('reports a lost job', async () => {
    stubJob([{ error: "Unknown job_id 'job-1'" }]);
    const done = runAgentTest('system-a', new AbortController().signal);
    await vi.advanceTimersByTimeAsync(10_000);
    const res = await done;
    expect(res.ok).toBe(false);
    if (!res.ok) expect(res.error).toContain('lost');
  });

  it('stops polling a little after the server deadline', async () => {
    stubJob([{ status: 'running' }]);
    const done = runAgentTest('system-a', new AbortController().signal);
    await vi.advanceTimersByTimeAsync(181_000);
    let settled = false;
    void done.then(() => {
      settled = true;
    });
    await vi.advanceTimersByTimeAsync(0);
    expect(settled).toBe(false);
    await vi.advanceTimersByTimeAsync(120_000);
    const res = await done;
    expect(res.ok).toBe(false);
    if (!res.ok) expect(res.error).toContain('no result');
  });

  it('reports a refused start', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => response({ error: "Unknown system_id 'system-a'" }, 404)),
    );
    const res = await runAgentTest('system-a', new AbortController().signal);
    expect(res.ok).toBe(false);
  });

  it('runs one job per card however often the card subscribes', async () => {
    const fetchMock = stubJob([{ status: 'done', result: passed }]);
    const seenA: AgentTestView[] = [];
    const seenB: AgentTestView[] = [];

    const stopA = watchAgentTest('card-1', 'system-a', (v) => seenA.push(v));
    const stopB = watchAgentTest('card-1', 'system-a', (v) => seenB.push(v));
    expect(seenA[0]).toEqual({ phase: 'running', text: 'Running agent test' });
    await vi.advanceTimersByTimeAsync(10_000);

    const starts = fetchMock.mock.calls.filter(
      ([input]) => String(input) === '/api/onboard/verify/agent-test',
    );
    expect(starts).toHaveLength(1);
    const last = seenB[seenB.length - 1];
    expect(last).toEqual({
      phase: 'done',
      outcome: 'passed',
      text: 'Agent test passed: query_system_a read table_x and answered 1,234 rows in 12 s',
    });
    stopA();
    stopB();
    // A later subscriber sees the finished result at once.
    const late: AgentTestView[] = [];
    watchAgentTest('card-1', 'system-a', (v) => late.push(v))();
    expect(late).toEqual([last]);
  });
});
