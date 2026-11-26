// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
/* The chat flow's verify card starts the agent test for the system its
 * verify result names, shows it running while it polls, then shows the
 * result line. */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../views/onboard/onboard-queries', () => ({
  getOnboardQueryClient: vi.fn(),
  invalidateOnboardingRemap: vi.fn(async () => {}),
  useConceptsQuery: () => ({ data: [], isError: false, refetch: vi.fn() }),
}));

import { ObFlow } from '../views/onboard/components/ObFlow';
import { resetAgentTestRuns } from '../views/onboard/agent-test';
import {
  appendChatCheckpoint,
  resetToWelcome,
  startWizard,
} from '../views/onboard/flow-store';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;

function response(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  resetAgentTestRuns();
  resetToWelcome();
  startWizard();
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  resetToWelcome();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe('chat verify card', () => {
  it('runs the agent test for the verified system and shows its result', async () => {
    let polls = 0;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === '/api/onboard/verify/agent-test') {
        expect(JSON.parse(String(init?.body))).toEqual({ system_id: 'system-a' });
        return response({ job_id: 'job-1', status: 'running', deadline_s: 180 });
      }
      if (url === '/api/onboard/verify/agent-test/job-1') {
        polls += 1;
        return response(
          polls < 2
            ? { status: 'running' }
            : {
                status: 'done',
                result: {
                  outcome: 'failed',
                  step: 'gateway_target',
                  reason: 'the gateway target system-a is SYNCHRONIZE_UNSUCCESSFUL',
                },
              },
        );
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);

    appendChatCheckpoint('verify', {
      system_id: 'system-a',
      tables_accessible: 2,
      total_rows: 10,
    });
    act(() => root.render(<ObFlow />));

    expect(container.textContent).toContain('Verification Passed');
    expect(container.textContent).toContain('Running agent test');

    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });

    expect(container.textContent).toContain(
      'Agent test failed at gateway target: the gateway target system-a is SYNCHRONIZE_UNSUCCESSFUL',
    );
    expect(container.textContent).not.toContain('Running agent test');
    expect(
      fetchMock.mock.calls.filter(([input]) => String(input) === '/api/onboard/verify/agent-test'),
    ).toHaveLength(1);
  });

  it('starts nothing when the verify result names no system', () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    appendChatCheckpoint('verify', { tables_accessible: 1, total_rows: 1 });
    act(() => root.render(<ObFlow />));
    expect(container.textContent).not.toContain('agent test');
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
