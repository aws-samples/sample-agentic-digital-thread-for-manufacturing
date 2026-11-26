// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* i18n ADOPTION pass — inner tabs/content components render t(key) and
 * re-render on setLang (the chrome dictionary was previously wired only to
 * the nav/user-menu; this pins the adopted surfaces).
 *
 * Light components only (no cytoscape/SSE): <PersonaGroups/> (chat.noSystems),
 * <MemoryPanel/> (memory.* header + tabs), <SystemsStrip/>
 * (dash.connectedSystems). English default keeps every legacy-text test
 * green; switching to 'ja' must swap the rendered strings live. */
// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { MemoryPanel } from '../chat/MemoryPanel';
import { setLang } from '../lib/i18n';
import { dashboardStore } from '../views/dashboard/store';
import { SystemsStrip } from '../views/dashboard/SystemsStrip';
import { PersonaGroups } from '../views/onboard/components/PersonaGroups';
import { setSuggestions, suggestionsStore } from '../views/onboard/suggestions-store';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;
let lsStore: Map<string, string>;

beforeEach(() => {
  // This node/jsdom combo ships a broken localStorage — Map-backed stub
  // (same workaround as automodal-blank-regression / arch-liveness).
  lsStore = new Map<string, string>();
  vi.stubGlobal('localStorage', {
    getItem: (k: string) => lsStore.get(k) ?? null,
    setItem: (k: string, v: string) => void lsStore.set(k, String(v)),
    removeItem: (k: string) => void lsStore.delete(k),
  });
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => ({ ok: true, status: 200, json: async () => ({}) }) as Response),
  );
  setLang('en');
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  setLang('en');
  setSuggestions({ kind: 'loading' }); // restore boot state for other suites
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('i18n adoption — components re-render on setLang', () => {
  it('PersonaGroups default chip follows chat.noSystems (after the boot loading state)', async () => {
    await act(async () => {
      root.render(<PersonaGroups />);
    });
    // Boot state is a loading indicator, NOT the no-systems chip.
    expect(suggestionsStore.get().kind).toBe('loading');
    expect(container.textContent).not.toContain('No systems connected yet');
    await act(async () => {
      setSuggestions({ kind: 'default' });
    });
    expect(container.textContent).toContain('No systems connected yet');
    await act(async () => {
      setLang('ja');
    });
    expect(container.textContent).toContain('接続されたシステムはまだありません');
  });

  it('MemoryPanel header + tabs follow memory.*', async () => {
    await act(async () => {
      root.render(
        <MemoryPanel
          refreshToken={0}
          onRehydrate={() => {}}
          collapsed={false}
          onToggleCollapsed={() => {}}
        />,
      );
    });
    expect(container.textContent).toContain('Memory');
    expect(container.textContent).toContain('Sessions');
    await act(async () => {
      setLang('ja');
    });
    expect(container.textContent).toContain('メモリ');
    expect(container.textContent).toContain('セッション');
  });

  it('SystemsStrip label follows dash.connectedSystems', async () => {
    dashboardStore.set({ ...dashboardStore.get(), systems: [] });
    await act(async () => {
      root.render(<SystemsStrip />);
    });
    expect(container.textContent).toContain('Connected Systems');
    await act(async () => {
      setLang('de');
    });
    expect(container.textContent).toContain('Verbundene Systeme');
  });
});
