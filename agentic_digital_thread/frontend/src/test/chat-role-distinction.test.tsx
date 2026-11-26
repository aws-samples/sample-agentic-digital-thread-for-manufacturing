// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tests — user-vs-agent role distinction in the chat thread (UI improvement B).
 *
 * Pins (each names the mutation it kills):
 * 1. Role labels render for BOTH the live-turns path and the rehydrated
 *    history path — kills removing the label render from either path.
 * 2. The user bubble's textContent/data-raw-content still equal the raw
 *    question — kills moving the label INSIDE the bubble (corrupting the
 *    bookmark/data-raw-content contract).
 * 3. CSS: `.message.user` right-aligns (margin-left: auto), caps at 60% and
 *    carries the accent color-mix tint — kills reverting the bubble CSS to
 *    the old near-identical surface2 treatment.
 * 4. t('chat.roleYou') resolves (en dictionary entry present) and setLang
 *    flips the visible label — kills a missing dictionary entry and kills a
 *    hardcoded English label. (Locale key-set equality across all six
 *    dictionaries is enforced by i18n-chrome.test.tsx.) */
// @vitest-environment jsdom
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ChatThread } from '../chat/ChatThread';
import { createThreadStore, type ThreadStore } from '../chat/thread-store';
import { setLang, t } from '../lib/i18n';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const TEST_DIR = path.dirname(fileURLToPath(import.meta.url));
const DASHBOARD_CSS = readFileSync(
  path.resolve(TEST_DIR, '../styles/dashboard.css'),
  'utf-8',
);

let container: HTMLDivElement;
let root: Root;
let store: ThreadStore;
// This node/jsdom combo ships a broken localStorage (setItem is not a
// function) — stub a Map-backed store (same pattern as i18n-chrome.test.tsx).
let lsStore: Map<string, string>;

beforeEach(() => {
  lsStore = new Map<string, string>();
  vi.stubGlobal('localStorage', {
    getItem: (k: string) => lsStore.get(k) ?? null,
    setItem: (k: string, v: string) => void lsStore.set(k, String(v)),
    removeItem: (k: string) => void lsStore.delete(k),
  });
  container = document.createElement('div');
  container.id = 'messages';
  document.body.appendChild(container);
  root = createRoot(container);
  store = createThreadStore();
});

afterEach(() => {
  act(() => setLang('en'));
  act(() => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
});

const render = () => act(() => root.render(<ChatThread store={store} />));

/** One rehydrated history pair + one settled live turn. */
function seedBothPaths() {
  act(() => {
    store.setHistory([
      { role: 'user', content: 'history question' },
      { role: 'assistant', content: 'history answer' },
    ]);
    store.startTurn('live question');
    store.applyEvent('token', { text: 'live answer' });
    store.applyEvent('done', { status: 'complete' });
  });
}

describe('chat role distinction (B)', () => {
  it('renders one You + one Orchestrator label per pair, in both paths', () => {
    seedBothPaths();
    render();
    const userLabels = container.querySelectorAll('.msg-role-label.role-user');
    const asstLabels = container.querySelectorAll('.msg-role-label.role-assistant');
    // One history pair + one live turn = 2 of each.
    expect(userLabels.length).toBe(2);
    expect(asstLabels.length).toBe(2);
    for (const el of userLabels) expect(el.textContent).toBe('You');
    for (const el of asstLabels) expect(el.textContent).toBe('Orchestrator');
    // The label precedes its bubble as a SIBLING (history path order pin).
    const historyUserLabel = userLabels[0];
    expect(historyUserLabel.nextElementSibling?.classList.contains('user')).toBe(true);
  });

  it('keeps the user bubble content uncorrupted (label is a sibling, not a child)', () => {
    seedBothPaths();
    render();
    const liveUser = container.querySelectorAll<HTMLElement>('.message.user')[1];
    expect(liveUser.dataset.rawContent).toBe('live question');
    expect(liveUser.textContent).toBe('live question');
    expect(liveUser.querySelector('.msg-role-label')).toBeNull();
  });

  it('CSS right-aligns, caps and accent-tints the user bubble', () => {
    const rule = DASHBOARD_CSS.match(/\.message\.user\s*\{([^}]*)\}/);
    expect(rule).not.toBeNull();
    expect(rule![1]).toContain('margin-left: auto');
    expect(rule![1]).toContain('max-width: 60%');
    expect(rule![1]).toContain('color-mix(in srgb, var(--accent)');
    // Label styles exist.
    expect(DASHBOARD_CSS).toMatch(/\.msg-role-label\s*\{/);
    expect(DASHBOARD_CSS).toMatch(/\.msg-role-label\.role-user\s*\{[^}]*text-align: right/);
  });

  it('labels come from the dictionary and translate on setLang', () => {
    expect(t('chat.roleYou')).not.toBe('chat.roleYou');
    seedBothPaths();
    render();
    act(() => setLang('ja'));
    const userLabel = container.querySelector('.msg-role-label.role-user');
    expect(userLabel?.textContent).toBe('あなた');
  });
});
