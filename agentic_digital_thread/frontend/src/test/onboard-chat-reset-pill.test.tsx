// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
/* The chat's reset_system pill does not claim a reset happened.
 *
 * Every reset_system call rendered as "Resetting system" and turned green
 * when the tool ended, without looking at its result. In a deployment the
 * tool often changes nothing (it hands removal off to the system's card, or
 * it is refused), so a green "Resetting system" told the user the opposite of
 * what happened. The pill now reads "Checking removal", which is true for
 * every outcome; the agent's text says what was done. */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../views/onboard/onboard-queries', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../views/onboard/onboard-queries')>();
  return {
    ...actual,
    useConceptsQuery: () => ({ isError: false, data: {}, refetch: vi.fn() }),
  };
});

import { flowSink, obStreamChat } from '../views/onboard/chat';
import { ObFlow } from '../views/onboard/components/ObFlow';
import { appendStream, resetToWelcome, startWizard } from '../views/onboard/flow-store';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;

function sse(events: [string, unknown][]): Response {
  const text = events.map(([e, d]) => `event: ${e}\ndata: ${JSON.stringify(d)}\n\n`).join('');
  const bytes = new TextEncoder().encode(text);
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(bytes);
      controller.close();
    },
  });
  return new Response(stream, { status: 200 });
}

beforeEach(() => {
  vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => {
    cb(performance.now());
    return 1;
  });
  vi.stubGlobal('cancelAnimationFrame', () => undefined);
  resetToWelcome();
  startWizard();
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
});

describe('the reset_system tool pill', () => {
  it.each([
    ['onboarding___reset_system', 'the onboarding gateway (deployed)'],
    ['reset_system', 'the in-process tools (local)'],
  ])('reads "Checking removal" for %s, never "Resetting system" (%s)', async (tool) => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        sse([
          ['tool_start', { tool, tool_id: 't1', input: {} }],
          ['tool_end', {}],
          ['token', { text: "It was not removed: remove it with Remove system on its card." }],
          ['done', { text: '' }],
        ]),
      ),
    );
    act(() => root.render(<ObFlow />));
    await act(async () => {
      await obStreamChat(
        { step: 'chat', system_id: 'plant-cmms', data: {}, message: 'remove plant-cmms',
          history: [] },
        flowSink(appendStream()),
      );
    });
    const pill = container.querySelector('.tool-pill') as HTMLElement;
    expect(pill).not.toBeNull();
    expect(pill.textContent).toBe('Checking removal');
    expect(container.textContent).not.toContain('Resetting system');
  });
});
