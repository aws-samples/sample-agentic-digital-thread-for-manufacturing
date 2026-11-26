// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tests — the explicit expand-answer affordance that REPLACED the end-of-turn
 * modal auto-open (2026-08-06; chat/ExpandAnswerButton.tsx + auto-open.ts).
 *
 * Pins (each names the mutation it kills):
 * 1. MUTATION-CRITICAL: after a full turn settles with a substantive answer
 *    in the DEFAULT takeover-friendly context (dock expanded — exactly where
 *    the old code popped the modal), the modal is NOT open. This test FAILS
 *    if anyone restores an openModal/setModalOpen call on the done path.
 * 2. The affordance appears after a substantive answer, as a real <button>
 *    with an aria-label resolving from the i18n dictionary — kills dropping
 *    the replacement control (auto-open removed with nothing added) and
 *    kills a div-with-onclick regression.
 * 3. No affordance on a pristine thread and none for a short degenerate
 *    answer (THE shared substantive-answer rule, auto-open.ts
 *    isSubstantiveAnswer — same rule the notify hook uses) — kills rendering
 *    it unconditionally and kills forking a second threshold.
 * 4. Clicking the affordance opens the SAME modal over the SAME store — the
 *    modal body shows the very question/answer of this session (no forked
 *    modal state) — kills wiring the button to anything but ChatSidebar's
 *    openChatModal path.
 * 5. The affordance hides again while a follow-up streams (the active turn
 *    is unsettled) — kills a stale affordance over an in-flight answer.
 * 6. isSubstantiveAnswer is the ONE rule: component source imports it from
 *    auto-open.ts and carries no own `> 10`/length literal — kills quietly
 *    re-inventing the condition. */
// @vitest-environment jsdom
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// Transport mock — runAskTurn drives SSE events through this.
vi.mock('../api/sse', () => ({ postSse: vi.fn() }));

import { postSse } from '../api/sse';
import { isSubstantiveAnswer } from '../chat/auto-open';
import ChatSidebar from '../chat/ChatSidebar';
import { chatThreadStore } from '../chat/useAskStream';
import { resetArchitectureState } from '../lib/arch-store';
import { t } from '../lib/i18n';
import { clear as clearToasts, toastStore } from '../shared/ui/toast-store';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const postSseMock = vi.mocked(postSse);

const TEST_DIR = path.dirname(fileURLToPath(import.meta.url));
const BUTTON_SRC = readFileSync(
  path.resolve(TEST_DIR, '../chat/ExpandAnswerButton.tsx'),
  'utf-8',
);

const ANSWER =
  'There are 2 systems currently connected: Telem SiteWise and Lake Athena.';

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
  delete window.currentSessionId;
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function mockTurn(answerText: string) {
  postSseMock.mockImplementationOnce(async (_url, _body, onEvent) => {
    onEvent('token', { text: answerText });
    onEvent('done', {});
  });
}

async function mountSidebar() {
  await act(async () => {
    root.render(<ChatSidebar />);
  });
}

async function sendQuestion(text: string) {
  const input = document.getElementById('question-input') as HTMLTextAreaElement;
  input.value = text;
  await act(async () => {
    window.sendMessage?.();
  });
}

const affordance = () =>
  container.querySelector<HTMLButtonElement>('.expand-answer-btn');

describe('end-of-turn: the modal NEVER opens by itself', () => {
  it('MUTATION-CRITICAL: dock expanded + settled substantive answer — modal stays closed', async () => {
    // Dock expanded on the dashboard view: exactly the context where the
    // retired takeover popped the modal unprompted.
    history.replaceState(null, '', '#dashboard');
    mockTurn(ANSWER);
    await mountSidebar();
    await sendQuestion('How many systems are connected?');

    const overlay = document.getElementById('chat-modal-overlay')!;
    expect(overlay.classList.contains('open')).toBe(false);
    // And no toast either in this context — the affordance is the surface.
    expect(toastStore.get().toasts).toHaveLength(0);
    // Sanity: the turn really settled with the substantive answer.
    const turns = chatThreadStore.getState().turns;
    expect(turns[turns.length - 1].assistant.answerMd).toBe(ANSWER);
  });
});

describe('the expand affordance', () => {
  it('appears after a substantive answer, as an accessible button', async () => {
    mockTurn(ANSWER);
    await mountSidebar();
    await sendQuestion('How many systems are connected?');

    const btn = affordance();
    expect(btn).not.toBeNull();
    expect(btn!.tagName).toBe('BUTTON');
    expect(btn!.getAttribute('type')).toBe('button');
    expect(btn!.getAttribute('aria-label')).toBe(t('chat.expandAnswer'));
    // Resolves from the dictionary — never the raw key.
    expect(t('chat.expandAnswer')).not.toBe('chat.expandAnswer');
    // Keyboard reachable: a real button is focusable by default — nothing
    // may opt it out of the tab order.
    expect(btn!.getAttribute('tabindex')).not.toBe('-1');
    expect(btn!.disabled).toBe(false);
  });

  it('does NOT appear on a pristine thread', async () => {
    await mountSidebar();
    expect(affordance()).toBeNull();
  });

  it('does NOT appear for a degenerate short answer (shared substantive-answer rule)', async () => {
    mockTurn('ok');
    await mountSidebar();
    await sendQuestion('ping');
    expect(affordance()).toBeNull();
    // The rule itself, at its boundary (the notify hook shares it).
    expect(isSubstantiveAnswer('ok')).toBe(false);
    expect(isSubstantiveAnswer(ANSWER)).toBe(true);
  });

  it('click opens the SAME modal over the SAME store (no forked state)', async () => {
    mockTurn(ANSWER);
    await mountSidebar();
    await sendQuestion('How many systems are connected?');

    await act(async () => {
      affordance()!.click();
    });

    const overlay = document.getElementById('chat-modal-overlay')!;
    expect(overlay.classList.contains('open')).toBe(true);
    // The modal body renders THIS session's thread — same store, so the
    // question and answer of the just-settled turn are both there.
    const modalMessages = document.getElementById('modal-messages')!;
    expect(modalMessages.textContent).toContain('How many systems are connected?');
    expect(modalMessages.textContent).toContain(ANSWER);
  });

  it('hides while a follow-up streams (active turn unsettled)', async () => {
    mockTurn(ANSWER);
    await mountSidebar();
    await sendQuestion('How many systems are connected?');
    expect(affordance()).not.toBeNull();

    // Start a follow-up whose stream never settles within the test.
    postSseMock.mockImplementationOnce(() => new Promise(() => {}));
    await sendQuestion('and which one is slowest?');
    expect(affordance()).toBeNull();
  });

  it('carries no forked answer-length rule (imports THE shared one)', () => {
    expect(BUTTON_SRC).toContain("isSubstantiveAnswer");
    expect(BUTTON_SRC).toContain("from './auto-open'");
    expect(BUTTON_SRC).not.toMatch(/[>>=]\s*10/);
    expect(BUTTON_SRC).not.toMatch(/\.length\s*[><]/);
  });
});
