// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* End-of-turn answer-ready GATING (chat/auto-open.ts + ChatSidebar).
 *
 * The takeover is RETIRED (2026-08-06): the pop-out modal NEVER opens by
 * itself on `done` — see answer-expand-affordance.test.tsx for the explicit
 * affordance that replaced it. What this file pins is the remaining
 * notification split: contexts where the settled answer is already surfaced
 * in-app (modal open, dock expanded, architecture view) fire NOTHING, and
 * anywhere else a clickable "Response ready" toast is queued — activating
 * the toast opens the modal (explicit action).
 *
 * Complements automodal-blank-regression.test.tsx (affordance-opened modal
 * is never blank) and answer-expand-affordance.test.tsx (no auto-open pin +
 * the affordance behavior). */
// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// Transport mock — runAskTurn drives SSE events through this.
vi.mock('../api/sse', () => ({ postSse: vi.fn() }));

import { postSse } from '../api/sse';
import { answerAlreadySurfaced } from '../chat/auto-open';
import ChatSidebar from '../chat/ChatSidebar';
import { chatThreadStore } from '../chat/useAskStream';
import { resetArchitectureState } from '../lib/arch-store';
import { setDemoActive } from '../lib/demo-active';
import { clear as clearToasts, toastStore } from '../shared/ui/toast-store';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const postSseMock = vi.mocked(postSse);

const ANSWER =
  'There are 2 systems currently connected: Telem SiteWise and Lake Athena.';

describe('answerAlreadySurfaced (pure gating)', () => {
  it('surfaced when the dock is expanded (answer + affordance in the thread)', () => {
    expect(answerAlreadySurfaced({ modalOpen: false, dockOpen: true, view: 'dashboard' })).toBe(true);
  });
  it('surfaced when the modal is already open (the answer streamed into it)', () => {
    expect(answerAlreadySurfaced({ modalOpen: true, dockOpen: false, view: 'dashboard' })).toBe(true);
  });
  it('surfaced on the architecture view even with the dock collapsed', () => {
    expect(answerAlreadySurfaced({ modalOpen: false, dockOpen: false, view: 'architecture' })).toBe(true);
  });
  it('NOT surfaced with the dock collapsed on other views (incl. no hash) — toast context', () => {
    for (const view of ['dashboard', 'threads', 'onboard', null]) {
      expect(answerAlreadySurfaced({ modalOpen: false, dockOpen: false, view })).toBe(false);
    }
  });
});

describe('ChatSidebar end-of-turn gating (takeover retired)', () => {
  let container: HTMLDivElement;
  let root: Root;
  let lsStore: Map<string, string>;

  beforeEach(() => {
    lsStore = new Map<string, string>();
    vi.stubGlobal('localStorage', {
      getItem: (k: string) => lsStore.get(k) ?? null,
      setItem: (k: string, v: string) => void lsStore.set(k, String(v)),
      removeItem: (k: string) => void lsStore.delete(k),
    });
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
    history.replaceState(null, '', '#');
    resetArchitectureState();
    chatThreadStore.clear();
    clearToasts();
    postSseMock.mockReset();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  function mockAnswerTurn() {
    postSseMock.mockImplementationOnce(async (_url, _body, onEvent) => {
      onEvent('token', { text: ANSWER });
      onEvent('done', {});
    });
  }

  async function mountAndSend({ collapseDock }: { collapseDock: boolean }) {
    await act(async () => {
      root.render(<ChatSidebar />);
    });
    if (collapseDock) {
      await act(async () => {
        window.toggleChatSidebar?.();
      });
    }
    const input = document.getElementById('question-input') as HTMLTextAreaElement;
    input.value = 'How many systems are connected?';
    await act(async () => {
      window.sendMessage?.();
    });
  }

  it('dock collapsed + non-architecture view: queues a clickable toast instead of the modal, and activating it opens the modal', async () => {
    history.replaceState(null, '', '#dashboard');
    mockAnswerTurn();
    await mountAndSend({ collapseDock: true });

    const overlay = document.getElementById('chat-modal-overlay')!;
    expect(overlay.classList.contains('open')).toBe(false);

    const toasts = toastStore.get().toasts;
    expect(toasts).toHaveLength(1);
    expect(toasts[0].message).toContain('Response ready');
    expect(typeof toasts[0].onActivate).toBe('function');

    await act(async () => {
      toasts[0].onActivate?.();
    });
    expect(overlay.classList.contains('open')).toBe(true);
  });

  it('dock collapsed: a degenerate short answer fires NO toast (the notify hook is gated on isSubstantiveAnswer)', async () => {
    history.replaceState(null, '', '#dashboard');
    postSseMock.mockImplementationOnce(async (_url, _body, onEvent) => {
      onEvent('token', { text: 'ok' });
      onEvent('done', {});
    });
    await mountAndSend({ collapseDock: true });

    expect(document.getElementById('chat-modal-overlay')!.classList.contains('open')).toBe(false);
    expect(toastStore.get().toasts).toHaveLength(0);
  });

  it('dock collapsed + architecture view: fires NOTHING (no modal, no toast)', async () => {
    history.replaceState(null, '', '#architecture');
    mockAnswerTurn();
    await mountAndSend({ collapseDock: true });

    const overlay = document.getElementById('chat-modal-overlay')!;
    expect(overlay.classList.contains('open')).toBe(false);
    expect(toastStore.get().toasts).toHaveLength(0);
  });

  it('dock expanded (default): fires NOTHING — the in-thread affordance is the surface', async () => {
    history.replaceState(null, '', '#dashboard');
    mockAnswerTurn();
    await mountAndSend({ collapseDock: false });

    const overlay = document.getElementById('chat-modal-overlay')!;
    expect(overlay.classList.contains('open')).toBe(false);
    expect(toastStore.get().toasts).toHaveLength(0);
    // The explicit affordance IS rendered in the thread.
    expect(container.querySelector('.expand-answer-btn')).not.toBeNull();
  });

  it('demo mode: suppresses BOTH the modal and the toast (nothing may cover the graph)', async () => {
    history.replaceState(null, '', '#architecture');
    setDemoActive(true);
    try {
      mockAnswerTurn();
      await mountAndSend({ collapseDock: false });

      const overlay = document.getElementById('chat-modal-overlay')!;
      expect(overlay.classList.contains('open')).toBe(false);
      expect(toastStore.get().toasts).toHaveLength(0);
    } finally {
      setDemoActive(false);
    }
  });
});

// --------------------------------------------------------------------------- //
// Teardown hygiene: the dock toggle defers a graph resize by 250 ms. An
// un-cleared handle fires against a torn-down window — in jsdom that surfaces
// as an "Uncaught ReferenceError: window is not defined" that fails the whole
// lane while every test still reports green (observed in CI 2026-09-03).
// Unmount MUST cancel it.
// --------------------------------------------------------------------------- //
describe('ChatSidebar teardown hygiene', () => {
  it('cancels the deferred graph resize on unmount (no work after teardown)', async () => {
    vi.useFakeTimers();
    try {
      const host = document.createElement('div');
      document.body.appendChild(host);
      const localRoot = createRoot(host);
      const resize = vi.fn();
      vi.stubGlobal('cy', { resize });

      await act(async () => {
        localRoot.render(<ChatSidebar />);
      });
      act(() => {
        window.toggleChatSidebar?.();
      });

      // Unmount BEFORE the 250 ms transition window elapses.
      act(() => localRoot.unmount());
      host.remove();
      act(() => {
        vi.advanceTimersByTime(1_000);
      });

      // The pending resize was cancelled: nothing ran after unmount. (Timer
      // COUNT is deliberately not asserted — react-query keeps its own 5-min
      // cache-GC handles, which are library-owned and touch no DOM global.)
      expect(resize).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });
});
