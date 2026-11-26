// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Demo-mode tests (demo.ts + DemoModal confirm dialog) — uniformity refactor.
 *
 * The demo is now a KIOSK DRIVER over the normal chat flow: prompts go
 * through window.sendMessage (persona-chip contract), the turn settles in
 * chatThreadStore, and a fresh session per query comes from
 * window.startNewSession. These tests pin:
 * - the uniform loop (startNewSession → sendMessage per prompt, full text in
 *   the composer + short display text, length-aware dwell, natural stop);
 * - demoActiveStore lifecycle (suppresses the chat-modal auto-open);
 * - the leaked-listener regression (a crash mid-loop must remove the
 *   document capture listeners — historically a leak force-navigated every
 *   background mousedown to the dashboard);
 * - the pause/confirm exit UX (Stay resumes, Leave stops, cooldown,
 *   demo-btn exemption, Escape = Stay while open);
 * - stopDemoMode idempotence and the DemoConfirmDialog component.
 *
 * Module-state isolation via vi.resetModules() + dynamic imports per test. */
// @vitest-environment jsdom
import { act } from 'react';
import { createElement } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest';

/** Controllable stand-in for the chat thread store (chatThreadStore). */
const fakeThread = vi.hoisted(() => {
  const state: { turns: Array<{ assistant: { streaming: boolean; error: string | null; answerMd: string } }> } = {
    turns: [],
  };
  return {
    state,
    getState: () => state,
    startStreamingTurn(): void {
      state.turns = [...state.turns, { assistant: { streaming: true, error: null, answerMd: '' } }];
    },
    settleLastTurn(answerMd: string, error: string | null = null): void {
      const last = state.turns[state.turns.length - 1];
      if (last) last.assistant = { streaming: false, error, answerMd };
    },
    reset(): void {
      state.turns = [];
    },
  };
});

vi.mock('../chat/useAskStream', () => ({
  chatThreadStore: { getState: fakeThread.getState },
}));
vi.mock('../views/architecture/arch-tab-bridge', () => ({ requestArchTab: vi.fn() }));

type DemoModule = typeof import('../views/dashboard/demo');
type StoreModule = typeof import('../views/dashboard/store');
type DemoActiveModule = typeof import('../lib/demo-active');

const PROMPTS = [{ source: 'registry', text: 'full question', title: 'Q1', display_text: 'Q1' }];

let switchView: Mock;
let sendMessage: Mock;
let startNewSession: Mock;
let stopStreaming: Mock;
let toggleChatSidebar: Mock;

type DemoGlobals = {
  switchView?: Mock;
  sendMessage?: Mock;
  startNewSession?: Mock;
  stopStreaming?: Mock;
  toggleChatSidebar?: Mock;
  _nextUserDisplayText?: string | null;
};
const wg = () => window as unknown as DemoGlobals;

async function setup(): Promise<{
  demo: DemoModule;
  store: StoreModule;
  demoActive: DemoActiveModule;
}> {
  vi.resetModules();
  fakeThread.reset();
  const demo = await import('../views/dashboard/demo');
  const store = await import('../views/dashboard/store');
  const demoActive = await import('../lib/demo-active');
  return { demo, store, demoActive };
}

function mousedown(target: EventTarget = document.body): void {
  target.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true }));
}

function keydown(code: string): void {
  document.body.dispatchEvent(
    new KeyboardEvent('keydown', { code, bubbles: true, cancelable: true }),
  );
}

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      if (String(input) === '/api/prompts') return jsonResponse(PROMPTS);
      throw new Error(`Unexpected fetch: ${String(input)}`);
    }),
  );
  // The demo drives the REAL chat globals — stub the ChatSidebar contract.
  switchView = vi.fn();
  sendMessage = vi.fn(() => fakeThread.startStreamingTurn());
  startNewSession = vi.fn(() => fakeThread.reset());
  stopStreaming = vi.fn();
  toggleChatSidebar = vi.fn();
  wg().switchView = switchView;
  wg().sendMessage = sendMessage;
  wg().startNewSession = startNewSession;
  wg().stopStreaming = stopStreaming;
  wg().toggleChatSidebar = toggleChatSidebar;
  // Composer input (demoAsk writes the full prompt text into it).
  const input = document.createElement('textarea');
  input.id = 'question-input';
  document.body.appendChild(input);
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
  delete wg().switchView;
  delete wg().sendMessage;
  delete wg().startNewSession;
  delete wg().stopStreaming;
  delete wg().toggleChatSidebar;
  delete wg()._nextUserDisplayText;
  document.body.replaceChildren();
});

describe('uniform demo loop through the normal chat flow', () => {
  it('runs each prompt via startNewSession + sendMessage, dwells, then stops naturally', async () => {
    const prompts = [
      { source: 'registry', text: 'full question one', display_text: 'Question one' },
      { source: 'registry', text: 'full question two', display_text: 'Question two' },
    ];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        if (String(input) === '/api/prompts') return jsonResponse(prompts);
        throw new Error(`Unexpected fetch: ${String(input)}`);
      }),
    );
    const { demo, demoActive } = await setup();

    const run = demo.startDemoMode();
    await vi.advanceTimersByTimeAsync(0); // prompts fetched, first ask sent

    // Prompt 1 went through the persona-chip contract: full text in the
    // composer, short label as the displayed user message.
    expect(startNewSession).toHaveBeenCalledTimes(1);
    expect(sendMessage).toHaveBeenCalledTimes(1);
    expect(
      (document.getElementById('question-input') as HTMLTextAreaElement).value,
    ).toBe('full question one');
    expect(wg()._nextUserDisplayText).toBe('Question one');
    // The auto-open suppression flag is up for the whole run.
    expect(demoActive.demoActiveStore.get()).toBe(true);

    // Turn 1 settles → length-aware dwell (short answer → 6s minimum).
    fakeThread.settleLastTurn('short answer');
    await vi.advanceTimersByTimeAsync(200); // settle poll
    expect(sendMessage).toHaveBeenCalledTimes(1); // still dwelling
    await vi.advanceTimersByTimeAsync(6100);

    // Prompt 2 started in a FRESH session.
    expect(startNewSession).toHaveBeenCalledTimes(2);
    expect(sendMessage).toHaveBeenCalledTimes(2);
    expect(
      (document.getElementById('question-input') as HTMLTextAreaElement).value,
    ).toBe('full question two');

    fakeThread.settleLastTurn('short answer');
    await vi.advanceTimersByTimeAsync(200);
    await vi.advanceTimersByTimeAsync(6100);

    await run; // natural stop after the last prompt
    expect(demoActive.demoActiveStore.get()).toBe(false);
    expect(switchView.mock.calls.map((c) => c[0])).toEqual(['architecture', 'dashboard']);
  });

  it('expands a collapsed chat dock (the sidebar is the answer surface)', async () => {
    const sidebar = document.createElement('div');
    sidebar.id = 'chat-sidebar';
    sidebar.classList.add('collapsed');
    document.body.appendChild(sidebar);
    const { demo } = await setup();

    const run = demo.startDemoMode();
    await vi.advanceTimersByTimeAsync(0);
    expect(toggleChatSidebar).toHaveBeenCalledTimes(1);

    demo.stopDemoMode();
    await vi.advanceTimersByTimeAsync(300);
    await run;
  });

  it('an error turn shows the normal chat error, dwells briefly, and stops the run', async () => {
    const { demo, demoActive } = await setup();
    const run = demo.startDemoMode();
    await vi.advanceTimersByTimeAsync(0);
    expect(sendMessage).toHaveBeenCalledTimes(1);

    fakeThread.settleLastTurn('', 'The runtime is temporarily unavailable.');
    await vi.advanceTimersByTimeAsync(200); // settle poll → 'error'
    await vi.advanceTimersByTimeAsync(5100); // error dwell
    await run;

    expect(sendMessage).toHaveBeenCalledTimes(1); // never advanced to another prompt
    expect(demoActive.demoActiveStore.get()).toBe(false);
    expect(switchView.mock.calls.map((c) => c[0])).toEqual(['architecture', 'dashboard']);
  });

  it('stopDemoMode mid-turn aborts through the normal Stop path (stopStreaming)', async () => {
    const { demo } = await setup();
    const run = demo.startDemoMode();
    await vi.advanceTimersByTimeAsync(0); // turn 1 streaming, never settles

    demo.stopDemoMode();
    expect(stopStreaming).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(300);
    await run;
    expect(switchView.mock.calls.map((c) => c[0])).toEqual(['architecture', 'dashboard']);
  });
});

describe('leaked-listener regression (bug 1)', () => {
  it('removes the capture listeners when a crash rejects the run mid-loop', async () => {
    const { demo, store } = await setup();
    // startNewSession is called inside the loop's try — throwing there
    // exercises the catch/finally cleanup path.
    startNewSession.mockImplementationOnce(() => {
      throw new Error('boom');
    });
    const addSpy = vi.spyOn(document, 'addEventListener');
    const removeSpy = vi.spyOn(document, 'removeEventListener');

    await demo.startDemoMode(); // resolves: crash is caught, finally cleans up

    // Every demo capture listener added was also removed (paired, same fn).
    const added = addSpy.mock.calls.filter(
      ([type, , opts]) => (type === 'mousedown' || type === 'keydown') && opts === true,
    );
    expect(added.length).toBeGreaterThan(0);
    for (const [type, fn, opts] of added) {
      expect(
        removeSpy.mock.calls.some(([t, f, o]) => t === type && f === fn && o === opts),
      ).toBe(true);
    }

    // Crash path still tears down gracefully (architecture → dashboard once).
    expect(switchView.mock.calls.map((c) => c[0])).toEqual(['architecture', 'dashboard']);

    // The site-wide symptom: a later background mousedown must NOT navigate
    // (and must not open the confirm either).
    switchView.mockClear();
    await vi.advanceTimersByTimeAsync(1100); // past the exit-ready cooldown
    mousedown();
    expect(switchView).not.toHaveBeenCalled();
    expect(store.dashboardStore.get().demo.confirmVisible).toBe(false);
  });
});

describe('confirm-dialog flow (bug 2)', () => {
  it('mousedown pauses + shows confirm; Stay resumes; Leave stops → dashboard', async () => {
    const { demo, store } = await setup();
    void demo.startDemoMode(); // turn 1 streaming forever → demo stays mid-query
    await vi.advanceTimersByTimeAsync(1000); // fetch + exit-ready cooldown

    mousedown();
    expect(store.dashboardStore.get().demo.confirmVisible).toBe(true);
    // Demo NOT torn down: no navigation beyond the initial 'architecture'.
    expect(switchView.mock.calls.map((c) => c[0])).toEqual(['architecture']);

    // Stay → dismissed, demo still active, listeners still armed:
    demo.demoConfirmStay();
    expect(store.dashboardStore.get().demo.confirmVisible).toBe(false);
    expect(switchView.mock.calls.map((c) => c[0])).toEqual(['architecture']);
    mousedown(); // a second gesture re-opens the confirm (listener alive)
    expect(store.dashboardStore.get().demo.confirmVisible).toBe(true);

    // Leave → existing stop path (navigates to dashboard):
    demo.demoConfirmLeave();
    expect(store.dashboardStore.get().demo.confirmVisible).toBe(false);
    expect(switchView.mock.calls.map((c) => c[0])).toEqual(['architecture', 'dashboard']);
  });

  it('ignores gestures during the 1s cooldown and on #demo-btn', async () => {
    const { demo, store } = await setup();
    void demo.startDemoMode();
    await vi.advanceTimersByTimeAsync(0); // fetch resolved, cooldown NOT elapsed

    mousedown();
    expect(store.dashboardStore.get().demo.confirmVisible).toBe(false);

    await vi.advanceTimersByTimeAsync(1000);
    const btn = document.createElement('button');
    btn.id = 'demo-btn';
    document.body.appendChild(btn);
    mousedown(btn); // demo-btn keeps toggling via its own handler — no confirm
    expect(store.dashboardStore.get().demo.confirmVisible).toBe(false);

    demo.stopDemoMode();
  });

  it('Escape routes through the confirm; Escape while open = Stay', async () => {
    const { demo, store } = await setup();
    void demo.startDemoMode();
    await vi.advanceTimersByTimeAsync(1000);

    keydown('Escape');
    expect(store.dashboardStore.get().demo.confirmVisible).toBe(true);
    keydown('Escape'); // = Stay
    expect(store.dashboardStore.get().demo.confirmVisible).toBe(false);
    expect(switchView.mock.calls.map((c) => c[0])).toEqual(['architecture']); // still running

    demo.stopDemoMode();
  });

  it('pauses the reading dwell while the confirm is up and resumes where it left off', async () => {
    const { demo, store } = await setup();
    const run = demo.startDemoMode();
    await vi.advanceTimersByTimeAsync(0);
    fakeThread.settleLastTurn('short answer'); // → 6s minimum dwell
    await vi.advanceTimersByTimeAsync(200); // settle poll
    await vi.advanceTimersByTimeAsync(1000); // 1s into the dwell

    mousedown(); // pause + confirm
    expect(store.dashboardStore.get().demo.confirmVisible).toBe(true);
    await vi.advanceTimersByTimeAsync(20_000); // paused: no advance, no exit
    expect(switchView.mock.calls.map((c) => c[0])).toEqual(['architecture']);

    demo.demoConfirmStay(); // resume the remaining ~5s of the dwell
    await vi.advanceTimersByTimeAsync(5400);
    await run; // single prompt finished → natural stop
    expect(switchView.mock.calls.map((c) => c[0])).toEqual(['architecture', 'dashboard']);
  });
});

describe('stopDemoMode idempotence', () => {
  it('is safe to call twice — second call is a full no-op', async () => {
    const { demo, store } = await setup();
    void demo.startDemoMode();
    await vi.advanceTimersByTimeAsync(1000);

    demo.stopDemoMode();
    expect(switchView.mock.calls.map((c) => c[0])).toEqual(['architecture', 'dashboard']);
    expect(() => demo.stopDemoMode()).not.toThrow();
    // No second navigation, listeners stay removed:
    expect(switchView.mock.calls.map((c) => c[0])).toEqual(['architecture', 'dashboard']);
    mousedown();
    expect(store.dashboardStore.get().demo.confirmVisible).toBe(false);
    expect(switchView.mock.calls.map((c) => c[0])).toEqual(['architecture', 'dashboard']);
  });
});

describe('DemoConfirmDialog component', () => {
  it('renders when confirmVisible, focuses Stay, and Stay click dismisses', async () => {
    vi.useRealTimers(); // React act flushing does not need fake timers
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    const { store } = await setup();
    const { DemoModal } = await import('../views/dashboard/DemoModal');

    const container = document.createElement('div');
    document.body.appendChild(container);
    const root = createRoot(container);
    try {
      act(() => {
        root.render(createElement(DemoModal));
      });
      expect(container.querySelector('#demo-confirm-overlay')).toBeNull();

      act(() => {
        store.demoConfirmShow();
      });
      const overlay = container.querySelector('#demo-confirm-overlay');
      expect(overlay).not.toBeNull();
      expect(overlay!.getAttribute('role')).toBe('dialog');
      const stay = container.querySelector<HTMLButtonElement>('#demo-confirm-stay');
      expect(stay).not.toBeNull();
      expect(document.activeElement).toBe(stay);
      expect(container.querySelector('#demo-confirm-leave')).not.toBeNull();

      act(() => {
        stay!.click(); // real demoConfirmStay → store confirm hidden
      });
      expect(store.dashboardStore.get().demo.confirmVisible).toBe(false);
      expect(container.querySelector('#demo-confirm-overlay')).toBeNull();
    } finally {
      act(() => root.unmount());
      container.remove();
    }
  });
});
