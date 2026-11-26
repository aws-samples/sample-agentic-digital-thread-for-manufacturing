// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tests — Task 1H investigation-trace liveness.
 *
 *  - questionPulseActive: the pure idle-but-live pulse rule the graph sync
 *    applies to the current question node (active && no tool in flight),
 *    driven through real store lifecycles;
 *  - single thinking affordance: the header #loading-indicator (ChatSidebar)
 *    and #modal-loading-indicator (ChatModal) are DELETED — exactly one
 *    stream-position .thinking-inline per chat surface while streaming;
 *  - useAskStream error paths settle the arch lifecycle: SSE `event: error`,
 *    a non-abort transport failure, and a user Stop (abort) all emit arch
 *    'done' so investigationActive never sticks;
 *  - the additive `reasoning` SSE event routes into the arch trace. */
// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// Transport mock — runAskTurn drives SSE events through this.
vi.mock('../api/sse', () => ({ postSse: vi.fn() }));

import { postSse } from '../api/sse';
import { archStore, resetArchitectureState } from '../lib/arch-store';
import { questionPulseActive } from '../views/architecture/inv-graph';
import ChatSidebar from '../chat/ChatSidebar';
import { chatThreadStore, runAskTurn } from '../chat/useAskStream';
import { installArchStoreDisposal, trackedArchStore as createArchStore } from './arch-store-tracking';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

installArchStoreDisposal();

const postSseMock = vi.mocked(postSse);

// This node/jsdom combo ships a broken localStorage (getItem is not a
// function) — stub a Map-backed store: runAskTurn reads 'adt-lang' through
// wrapResponseLang.
let lsStore: Map<string, string>;

beforeEach(() => {
  lsStore = new Map<string, string>();
  vi.stubGlobal('localStorage', {
    getItem: (k: string) => lsStore.get(k) ?? null,
    setItem: (k: string, v: string) => void lsStore.set(k, String(v)),
    removeItem: (k: string) => void lsStore.delete(k),
  });
});

afterEach(() => {
  resetArchitectureState();
  chatThreadStore.clear();
  postSseMock.mockReset();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('questionPulseActive — pure idle-but-live pulse rule', () => {
  it('pulses only while the investigation is live with NO tool in flight', () => {
    const store = createArchStore();
    // Idle store: nothing live, no pulse.
    expect(questionPulseActive(store.getState())).toBe(false);
    // Ask start → live, nothing in flight yet: PULSE (question renders at
    // ask start; the gap before the first tool batch is visibly alive).
    store.handleArchEvent('user_msg', { question: 'q' });
    expect(questionPulseActive(store.getState())).toBe(true);
    // tool_start clears it (the in-flight system node pulses instead).
    store.handleArchEvent('tool_start', { tool: 'erp___query_erp', input: {} });
    expect(questionPulseActive(store.getState())).toBe(false);
    // Between batches (all results in, turn not done): PULSE again.
    store.handleArchEvent('tool_result', { tool: 'erp___query_erp', summary: '1 record found', input: {} });
    expect(questionPulseActive(store.getState())).toBe(true);
    // Turn settled: no pulse.
    store.handleArchEvent('done', {});
    expect(questionPulseActive(store.getState())).toBe(false);
  });

  it('does not pulse after an error-path done or a reset', () => {
    const store = createArchStore();
    store.handleArchEvent('user_msg', { question: 'q' });
    store.handleArchEvent('done', {}); // error-path done from useAskStream
    expect(questionPulseActive(store.getState())).toBe(false);
    store.handleArchEvent('user_msg', { question: 'q2' });
    store.reset();
    expect(questionPulseActive(store.getState())).toBe(false);
  });
});

describe('single thinking affordance (headers deleted, .thinking-inline kept)', () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    // Generic ok-JSON stub for the mount-time fetches (onboard status,
    // memory panel, prompts) — all callers tolerate an empty object.
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({ ok: true, status: 200, json: async () => ({}) }) as Response),
    );
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
  });

  it('while streaming, the sidebar shows ONLY the stream-position .thinking-inline; the modal header copy is gone too', async () => {
    await act(async () => {
      root.render(<ChatSidebar />);
    });
    // Start a streaming turn on the shared store (assistant.streaming=true).
    act(() => chatThreadStore.startTurn('q', 'q'));

    // The TOP header indicators are DELETED (ids gone entirely, not hidden).
    expect(document.getElementById('loading-indicator')).toBeNull();
    expect(document.getElementById('modal-loading-indicator')).toBeNull();

    // Exactly ONE thinking affordance in the sidebar thread.
    expect(document.querySelectorAll('#messages .thinking-inline')).toHaveLength(1);
    expect(document.querySelectorAll('.loading-indicator')).toHaveLength(0);

    // Open the pop-out modal: its surface renders its own single
    // .thinking-inline from the SAME store — and still no header indicator.
    act(() => window.openChatModal?.());
    expect(document.getElementById('modal-loading-indicator')).toBeNull();
    expect(document.querySelectorAll('#modal-messages .thinking-inline')).toHaveLength(1);
  });
});

describe('useAskStream error paths settle the arch lifecycle (arch done)', () => {
  const deps = { plant: 'all', showThread: () => {}, onAnswerReady: () => {} };

  it('SSE `event: error` emits arch done — investigationActive does not stick', async () => {
    postSseMock.mockImplementationOnce(async (_url, _body, onEvent) => {
      onEvent('error', { text: 'boom' });
    });
    await runAskTurn('q', new AbortController(), deps);
    const s = archStore.getState();
    expect(s.questionCounter).toBe(1); // user_msg reached the store
    expect(s.investigationActive).toBe(false);
  });

  it('a non-abort transport failure emits arch done', async () => {
    postSseMock.mockRejectedValueOnce(new Error('network down'));
    await runAskTurn('q', new AbortController(), deps);
    expect(archStore.getState().investigationActive).toBe(false);
    // HUD rows (none here) settled; the turn carries the error text.
    const turns = chatThreadStore.getState().turns;
    expect(turns[turns.length - 1].assistant.streaming).toBe(false);
  });

  it('a user Stop (AbortError) also settles the lifecycle', async () => {
    postSseMock.mockImplementationOnce(async (_url, _body, onEvent) => {
      onEvent('tool_start', { tool: 'erp___query_erp', input: {} });
      const e = new Error('aborted');
      e.name = 'AbortError';
      throw e;
    });
    await runAskTurn('q', new AbortController(), deps);
    const s = archStore.getState();
    expect(s.investigationActive).toBe(false);
    // The in-flight HUD row was marked stopped by the arch done.
    expect(s.hud.find((h) => h.kind === 'tool')?.status).toBe('stopped');
  });

  it('routes the additive `reasoning` SSE event into the arch trace', async () => {
    postSseMock.mockImplementationOnce(async (_url, _body, onEvent) => {
      onEvent('reasoning', { text: 'preparing ERP query…' });
      onEvent('done', {});
    });
    await runAskTurn('q', new AbortController(), deps);
    const s = archStore.getState();
    expect(
      s.trace.some((t) => t.type === 'decision' && t.text === 'preparing ERP query…'),
    ).toBe(true);
    // Empty/whitespace reasoning payloads are dropped (no blank trace rows).
    postSseMock.mockImplementationOnce(async (_url, _body, onEvent) => {
      onEvent('reasoning', { text: '   ' });
      onEvent('done', {});
    });
    await runAskTurn('q2', new AbortController(), deps);
    const traceAfter = archStore.getState().trace;
    expect(traceAfter.filter((t) => t.label === 'Reasoning')).toHaveLength(1);
  });
});
