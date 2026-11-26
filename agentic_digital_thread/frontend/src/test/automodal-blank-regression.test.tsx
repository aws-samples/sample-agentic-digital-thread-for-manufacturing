// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Regression pins — the pop-out chat modal must never show blank ("wipes
 * itself after popping open", user report 2026-07-15 on the deployed env;
 * NOT reproducible on this tree — live playwright runs against both the
 * local dev server and the deployed CloudFront env showed the modal
 * populated at open, +1s and +3s. These tests pin that behavior).
 *
 * The end-of-turn AUTO-open is retired (2026-08-06) — the modal now opens
 * from the explicit expand affordance (chat/ExpandAnswerButton.tsx; see
 * answer-expand-affordance.test.tsx for the no-auto-open pin). The blank-
 * wipe vector is unchanged: the modal renders the SAME chatThreadStore as
 * the sidebar, and the suspected wipe was the rAF-coalesced token notify
 * firing AFTER `done` — a leaked animation frame overwriting the settled
 * turn. Pinned here:
 *
 *  1. store level — a token frame leaked past `done` (cancelAnimationFrame
 *     ignored, the pathological case) must NOT clear the finalized answer;
 *  2. component level — a full mocked turn through window.sendMessage(),
 *     then the expand affordance click, opens the modal with NON-BLANK
 *     content (answer text rendered, persona empty-state hidden), and the
 *     content survives leaked rAF frames plus a 1s fake-timer advance;
 *  3. the substantive-answer guard — a degenerate short answer renders NO
 *     expand affordance (so the modal can never be led onto an empty
 *     thread by the end-of-turn flow). */
// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// Transport mock — runAskTurn drives SSE events through this.
vi.mock('../api/sse', () => ({ postSse: vi.fn() }));

import { postSse } from '../api/sse';
import { resetArchitectureState } from '../lib/arch-store';
import ChatSidebar from '../chat/ChatSidebar';
import { createThreadStore } from '../chat/thread-store';
import { chatThreadStore } from '../chat/useAskStream';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const postSseMock = vi.mocked(postSse);

const ANSWER =
  'There are 2 systems currently connected: Telem SiteWise and Lake Athena.';

/** Manual rAF queue. `fireAll` runs EVERY captured frame — including ones
 * cancelAnimationFrame removed — to simulate the pathological leaked frame
 * firing after `done`. rafPair() resolves the globals dynamically per call,
 * so stubbing here controls the module-singleton chatThreadStore too. */
function stubRaf() {
  const frames = new Map<number, FrameRequestCallback>();
  const all: FrameRequestCallback[] = [];
  let nextId = 1;
  vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => {
    const id = nextId++;
    frames.set(id, cb);
    all.push(cb);
    return id;
  });
  vi.stubGlobal('cancelAnimationFrame', (id: number) => {
    frames.delete(id);
  });
  return {
    /** Fire frames that are still scheduled (normal browser behavior). */
    firePending: () => {
      const cbs = [...frames.values()];
      frames.clear();
      cbs.forEach((cb) => cb(performance.now()));
    },
    /** Fire EVERY frame ever scheduled, canceled or not (leak simulation). */
    fireAllEvenCanceled: () => {
      frames.clear();
      all.splice(0).forEach((cb) => cb(performance.now()));
    },
  };
}

// This node/jsdom combo ships a broken localStorage (getItem is not a
// function) — stub a Map-backed store (same workaround as arch-liveness).
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
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('thread store — finalized answer survives a token frame leaked past done', () => {
  it('a leaked rAF flush after done does not clear the turn or its answer', () => {
    const raf = stubRaf();
    const store = createThreadStore();
    store.startTurn('How many systems are connected?');
    store.applyEvent('token', { text: ANSWER });
    // done: flushTokenNotify (cancel + sync notify) + finalize the answer.
    store.applyEvent('done', {});
    const turn = store.getState().turns[0];
    expect(turn.assistant.streaming).toBe(false);
    expect(turn.assistant.answerMd).toBe(ANSWER);

    // Pathological: the coalesced token frame fires anyway AFTER done.
    raf.fireAllEvenCanceled();
    const after = store.getState().turns[0];
    expect(after.assistant.answerMd).toBe(ANSWER);
    expect(store.getState().turns).toHaveLength(1);
    expect(after.assistant.streaming).toBe(false);
  });
});

describe('ChatModal opened via the expand affordance — non-blank content that stays', () => {
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

  async function sendQuestion(text: string) {
    const input = document.getElementById('question-input') as HTMLTextAreaElement;
    input.value = text;
    await act(async () => {
      window.sendMessage?.();
    });
  }

  it('affordance click opens the modal with the answer rendered, and the content survives leaked rAF frames + 1s', async () => {
    const raf = stubRaf();
    postSseMock.mockImplementationOnce(async (_url, _body, onEvent) => {
      onEvent('token', { text: ANSWER });
      onEvent('done', {});
    });
    await act(async () => {
      root.render(<ChatSidebar />);
    });
    await sendQuestion('How many systems are connected?');

    // The expand affordance appeared under the settled answer …
    const expand = container.querySelector<HTMLButtonElement>('.expand-answer-btn');
    expect(expand).not.toBeNull();
    await act(async () => {
      expand!.click();
    });

    // … and the modal opened showing the thread (persona empty-state
    // hidden), NOT a blank body.
    const overlay = document.getElementById('chat-modal-overlay')!;
    expect(overlay.classList.contains('open')).toBe(true);
    const modalMessages = document.getElementById('modal-messages')!;
    expect(modalMessages.style.display).not.toBe('none');
    expect(document.getElementById('modal-persona-panel')!.style.display).toBe('none');
    expect(modalMessages.textContent).toContain('How many systems are connected?');
    expect(modalMessages.textContent).toContain(ANSWER);

    // The done-flush window: leaked token frames fire AFTER the modal opened.
    await act(async () => {
      raf.fireAllEvenCanceled();
    });
    expect(overlay.classList.contains('open')).toBe(true);
    expect(modalMessages.textContent).toContain(ANSWER);

    // …and 1s later (fake timers) it is STILL populated ("wipes itself
    // after popping open" regression pin).
    vi.useFakeTimers();
    await act(async () => {
      vi.advanceTimersByTime(1000);
    });
    vi.useRealTimers();
    expect(overlay.classList.contains('open')).toBe(true);
    expect(document.getElementById('modal-persona-panel')!.style.display).toBe('none');
    expect(modalMessages.textContent).toContain(ANSWER);
  });

  it('does NOT render the affordance for a degenerate short answer (substantive-answer guard)', async () => {
    stubRaf();
    postSseMock.mockImplementationOnce(async (_url, _body, onEvent) => {
      onEvent('token', { text: 'ok' });
      onEvent('done', {});
    });
    await act(async () => {
      root.render(<ChatSidebar />);
    });
    await sendQuestion('ping');

    expect(container.querySelector('.expand-answer-btn')).toBeNull();
    const overlay = document.getElementById('chat-modal-overlay')!;
    expect(overlay.classList.contains('open')).toBe(false);
  });
});
