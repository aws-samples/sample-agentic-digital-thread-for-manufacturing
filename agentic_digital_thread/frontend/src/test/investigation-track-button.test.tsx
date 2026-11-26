// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tests — the investigation tracking button (UI improvement C; session-active
 * visibility rule, 2026-08-06 — replaces the retired streaming+30s-grace
 * window).
 *
 * Pins (each names the mutation it kills):
 * 1. Hidden on a pristine empty chat (no session id, empty thread store) —
 *    kills removing the session-active gate (unconditional render).
 * 2. Visible while streaming (with .streaming pulse) — kills dropping the
 *    streaming arm of the rule.
 * 3. Visible WELL BEYOND the old 30s grace after streaming ends, as long as
 *    the session stays active (session id + a settled turn) — THE behavior
 *    change: this test FAILS under the old grace-only rule, killing any
 *    reintroduction of a hide timer.
 * 4. Visible for a REHYDRATED session (adopted id + history, no live turn) —
 *    kills counting only live turns.
 * 5. Hidden after '+ New'/reset (store cleared; resetSessionId minted a
 *    FRESH id) — kills dropping the thread-content arm (id-only rule).
 * 6. Click calls window.switchView('architecture') then
 *    requestArchTab('investigation') — kills navigating to the wrong tab and
 *    kills bypassing the lazy-mount bridge (silent no-op on first visit).
 * 7. STREAM SURVIVES: with the REAL chatThreadStore mid-stream and the REAL
 *    archStore holding a question, clicking the button leaves the streaming
 *    flag true, the turns untouched and the arch question intact — kills any
 *    click-handler regression that aborts the turn, resets the thread store
 *    or resets the architecture state.
 * 8. Reduced motion: the pulse is plain CSS animation (covered by base.css's
 *    universal prefers-reduced-motion kill) — kills converting the pulse to
 *    a JS-driven interval and kills deleting the global reduce kill.
 * 9. t('chat.trackInvestigation') resolves — kills a missing en dictionary
 *    entry (locale-set equality enforced by i18n-chrome.test.tsx). */
// @vitest-environment jsdom
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { InvestigationTrackButton } from '../chat/InvestigationTrackButton';
import { chatThreadStore } from '../chat/useAskStream';
import { archStore } from '../lib/arch-store';
import { t } from '../lib/i18n';
import { adoptSessionId, resetSessionId } from '../lib/session';
import { requestArchTab } from '../views/architecture/arch-tab-bridge';

vi.mock('../views/architecture/arch-tab-bridge', () => ({
  requestArchTab: vi.fn(),
}));

const requestArchTabMock = vi.mocked(requestArchTab);

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const TEST_DIR = path.dirname(fileURLToPath(import.meta.url));
const BASE_CSS = readFileSync(path.resolve(TEST_DIR, '../styles/base.css'), 'utf-8');
const DASHBOARD_CSS = readFileSync(
  path.resolve(TEST_DIR, '../styles/dashboard.css'),
  'utf-8',
);
const BUTTON_SRC = readFileSync(
  path.resolve(TEST_DIR, '../chat/InvestigationTrackButton.tsx'),
  'utf-8',
);

/** The RETIRED grace window — kept as a literal so pin 3 can assert far past
 * it (the constant itself no longer exists in the component). */
const OLD_GRACE_MS = 30_000;

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  vi.useFakeTimers();
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  requestArchTabMock.mockReset();
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.useRealTimers();
  delete (window as { switchView?: unknown }).switchView;
  delete window.currentSessionId;
  chatThreadStore.clear();
  archStore.reset();
});

const renderStreaming = (streaming: boolean) =>
  act(() => root.render(<InvestigationTrackButton streaming={streaming} />));

const btn = () => container.querySelector<HTMLButtonElement>('.inv-track-btn');

/** One full turn against the REAL store: session id minted, turn settled. */
function settleOneTurn() {
  adoptSessionId('session-test-1');
  act(() => {
    chatThreadStore.startTurn('which line is slow?');
    chatThreadStore.applyEvent('token', { text: 'Line 2 is the slowest.' });
    chatThreadStore.applyEvent('done', {});
  });
}

describe('InvestigationTrackButton (C, session-active rule)', () => {
  it('is hidden on a pristine empty chat (no session activity)', () => {
    renderStreaming(false);
    expect(btn()).toBeNull();
  });

  it('shows while streaming, with the pulse', () => {
    renderStreaming(true);
    expect(btn()).not.toBeNull();
    expect(btn()!.classList.contains('streaming')).toBe(true);
    expect(btn()!.getAttribute('aria-label')).toBe(t('chat.trackInvestigation'));
  });

  it('stays visible FAR beyond the old 30s grace while the session is active', () => {
    settleOneTurn();
    renderStreaming(true);
    expect(btn()).not.toBeNull();

    renderStreaming(false); // turn settled — under the old rule the grace timer arms
    expect(btn()).not.toBeNull();
    expect(btn()!.classList.contains('streaming')).toBe(false);

    // 10 minutes — 20x the retired grace window. The old grace-only rule
    // hid the button at +30s; the session-active rule keeps it.
    act(() => vi.advanceTimersByTime(OLD_GRACE_MS * 20));
    expect(btn()).not.toBeNull();
    expect(btn()!.classList.contains('streaming')).toBe(false);
  });

  it('shows for a rehydrated session (adopted id + history, no live turn)', () => {
    adoptSessionId('session-rehydrated-1');
    act(() => {
      chatThreadStore.setHistory([
        { role: 'user', content: 'which line is slow?' },
        { role: 'assistant', content: 'Line 2 is the slowest.' },
      ]);
    });
    renderStreaming(false);
    expect(btn()).not.toBeNull();
  });

  it("hides after '+ New' (store cleared) even though a FRESH session id exists", () => {
    settleOneTurn();
    renderStreaming(false);
    expect(btn()).not.toBeNull();

    // startNewSession semantics: fresh id + thread cleared.
    act(() => {
      resetSessionId();
      chatThreadStore.clear();
    });
    expect(window.currentSessionId).toBeTruthy(); // id ALONE must not show it
    renderStreaming(false);
    expect(btn()).toBeNull();
  });

  it('click navigates: switchView(architecture) then requestArchTab(investigation)', () => {
    const switchView = vi.fn();
    (window as { switchView?: (v: string) => void }).switchView = switchView;
    renderStreaming(true);

    act(() => btn()!.click());

    expect(switchView).toHaveBeenCalledTimes(1);
    expect(switchView).toHaveBeenCalledWith('architecture');
    expect(requestArchTabMock).toHaveBeenCalledTimes(1);
    expect(requestArchTabMock).toHaveBeenCalledWith('investigation');
    expect(switchView.mock.invocationCallOrder[0]).toBeLessThan(
      requestArchTabMock.mock.invocationCallOrder[0],
    );
  });

  it('STREAM SURVIVES: clicking mid-stream touches neither the thread nor the arch state', () => {
    // Real module-level stores — the same objects the app streams through.
    // switchView spy is the app-behavior surrogate: the real switchView only
    // swaps the visible view; the sidebar (and its stream) never unmounts.
    const switchView = vi.fn();
    (window as { switchView?: (v: string) => void }).switchView = switchView;

    act(() => {
      chatThreadStore.startTurn('which line is slow?');
    });
    archStore.handleArchEvent('user_msg', { question: 'which line is slow?' });
    const turnsBefore = chatThreadStore.getState().turns.length;
    expect(archStore.getState().questionCounter).toBe(1);
    expect(archStore.getState().activeQuestion).not.toBe('');

    renderStreaming(true);
    act(() => btn()!.click());

    const state = chatThreadStore.getState();
    expect(state.turns.length).toBe(turnsBefore);
    expect(state.turns[state.turns.length - 1].assistant.streaming).toBe(true);
    // No resetArchitectureState / archStore.reset() — the question survives.
    expect(archStore.getState().questionCounter).toBe(1);
    expect(archStore.getState().activeQuestion).not.toBe('');
  });

  it('reduced motion: CSS-only pulse, covered by the universal base.css kill', () => {
    // The global reduce kill must still exist…
    const reduceBlock = BASE_CSS.match(
      /@media \(prefers-reduced-motion: reduce\)[\s\S]*?animation-duration: 1ms/,
    );
    expect(reduceBlock).not.toBeNull();
    // …and the pulse must be a plain CSS animation (which that kill covers).
    expect(DASHBOARD_CSS).toMatch(/\.inv-track-btn\.streaming\s*\{[^}]*animation: inv-track-pulse/);
    expect(DASHBOARD_CSS).toContain('@keyframes inv-track-pulse');
    // No JS-driven animation loop (or resurrected grace timer) in the component.
    expect(BUTTON_SRC.includes('setInterval')).toBe(false);
    expect(BUTTON_SRC.includes('requestAnimationFrame')).toBe(false);
    expect(BUTTON_SRC.includes('setTimeout')).toBe(false);
  });

  it('the tooltip/aria string resolves from the dictionary', () => {
    expect(t('chat.trackInvestigation')).not.toBe('chat.trackInvestigation');
  });
});
