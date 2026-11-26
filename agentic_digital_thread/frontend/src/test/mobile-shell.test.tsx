// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import ChatSidebar from '../chat/ChatSidebar';
import { chatThreadStore } from '../chat/useAskStream';
import { adoptSessionId } from '../lib/session';
import { requestArchTab, requestArchTabFocus } from '../views/architecture/arch-tab-bridge';

vi.mock('../views/architecture/arch-tab-bridge', () => ({
  requestArchTab: vi.fn(),
  requestArchTabFocus: vi.fn(),
  consumePendingArchTab: vi.fn(() => null),
}));

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const TEST_DIR = path.dirname(fileURLToPath(import.meta.url));
const STYLES_DIR = path.resolve(TEST_DIR, '../styles');
const baseCss = readFileSync(path.join(STYLES_DIR, 'base.css'), 'utf8');
const dashboardCss = readFileSync(path.join(STYLES_DIR, 'dashboard.css'), 'utf8');
const memoryCss = readFileSync(path.join(STYLES_DIR, 'memory.css'), 'utf8');
const loginCss = readFileSync(path.join(STYLES_DIR, 'login.css'), 'utf8');

/** The body of the FIRST `@media (max-width: 768px)` block in a stylesheet. */
function mobileBlock(css: string): string {
  const start = css.search(/@media\s*\(max-width:\s*768px\)\s*\{/);
  expect(start).toBeGreaterThanOrEqual(0);
  let depth = 0;
  for (let i = css.indexOf('{', start); i < css.length; i += 1) {
    if (css[i] === '{') depth += 1;
    if (css[i] === '}' && --depth === 0) return css.slice(start, i + 1);
  }
  throw new Error('unterminated @media block');
}

function createMediaQuery(initialMatches: boolean) {
  let matches = initialMatches;
  const listeners = new Set<(event: MediaQueryListEvent) => void>();
  const media = {
    get matches() {
      return matches;
    },
    media: '(max-width: 768px)',
    onchange: null,
    addEventListener: (_type: string, listener: (event: MediaQueryListEvent) => void) => {
      listeners.add(listener);
    },
    removeEventListener: (_type: string, listener: (event: MediaQueryListEvent) => void) => {
      listeners.delete(listener);
    },
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => true,
  } as MediaQueryList;

  return {
    media,
    setMatches(next: boolean) {
      matches = next;
      act(() => {
        const event = { matches: next, media: media.media } as MediaQueryListEvent;
        listeners.forEach((listener) => listener(event));
      });
    },
  };
}

function ok(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as Response;
}

describe('<ChatSidebar/> mobile drawers', () => {
  let container: HTMLDivElement;
  let root: Root;
  let query: ReturnType<typeof createMediaQuery>;

  beforeEach(() => {
    query = createMediaQuery(true);
    vi.stubGlobal('matchMedia', vi.fn(() => query.media));
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        if (url.startsWith('/api/memory/')) return new Promise<Response>(() => {});
        return url === '/api/onboard/status' ? ok({ onboarded_systems: [] }) : ok([]);
      }),
    );
    vi.stubGlobal('localStorage', {
      getItem: vi.fn(() => null),
      setItem: vi.fn(),
      removeItem: vi.fn(),
      clear: vi.fn(),
      key: vi.fn(() => null),
      length: 0,
    });
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    chatThreadStore.clear();
    delete window.currentSessionId;
    vi.mocked(requestArchTab).mockReset();
    vi.mocked(requestArchTabFocus).mockReset();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  async function mount() {
    await act(async () => root.render(<ChatSidebar />));
  }

  function sidebar(id: 'chat-sidebar' | 'memory-sidebar') {
    return container.querySelector<HTMLElement>(`#${id}`)!;
  }

  function toggle(id: 'chat-collapse-btn' | 'memory-toggle-btn') {
    act(() => container.querySelector<HTMLButtonElement>(`#${id}`)!.click());
  }

  it('keeps one full-width drawer active at a time and exposes deterministic classes', async () => {
    await mount();

    expect(sidebar('chat-sidebar').classList.contains('mobile-drawer-open')).toBe(true);
    expect(sidebar('chat-sidebar').classList.contains('collapsed')).toBe(false);
    expect(sidebar('memory-sidebar').classList.contains('collapsed')).toBe(true);
    expect(container.querySelector('#sidebar-toggles')?.getAttribute('data-mobile-active-drawer')).toBe('chat');

    toggle('memory-toggle-btn');
    expect(sidebar('chat-sidebar').classList.contains('collapsed')).toBe(true);
    expect(sidebar('memory-sidebar').classList.contains('collapsed')).toBe(false);
    expect(container.querySelector('#sidebar-toggles')?.getAttribute('data-mobile-active-drawer')).toBe('memory');
    expect(container.querySelector('#memory-toggle-btn')?.getAttribute('aria-expanded')).toBe('true');

    toggle('chat-collapse-btn');
    expect(sidebar('chat-sidebar').classList.contains('collapsed')).toBe(false);
    expect(sidebar('memory-sidebar').classList.contains('collapsed')).toBe(true);
    expect(container.querySelector('#chat-collapse-btn')?.getAttribute('aria-expanded')).toBe('true');

    toggle('chat-collapse-btn');
    expect(sidebar('chat-sidebar').classList.contains('collapsed')).toBe(true);
    expect(sidebar('memory-sidebar').classList.contains('collapsed')).toBe(true);
    expect(container.querySelector('#sidebar-toggles')?.getAttribute('data-mobile-active-drawer')).toBe('none');
  });

  it('preserves desktop side-by-side state across a temporary mobile layout', async () => {
    query = createMediaQuery(false);
    vi.stubGlobal('matchMedia', vi.fn(() => query.media));
    await mount();

    toggle('memory-toggle-btn');
    expect(sidebar('chat-sidebar').classList.contains('collapsed')).toBe(false);
    expect(sidebar('memory-sidebar').classList.contains('collapsed')).toBe(false);

    query.setMatches(true);
    expect(sidebar('chat-sidebar').classList.contains('collapsed')).toBe(false);
    expect(sidebar('memory-sidebar').classList.contains('collapsed')).toBe(true);

    toggle('memory-toggle-btn');
    expect(sidebar('chat-sidebar').classList.contains('collapsed')).toBe(true);
    expect(sidebar('memory-sidebar').classList.contains('collapsed')).toBe(false);

    query.setMatches(false);
    expect(sidebar('chat-sidebar').classList.contains('collapsed')).toBe(false);
    expect(sidebar('memory-sidebar').classList.contains('collapsed')).toBe(false);
    expect(container.querySelector('#sidebar-toggles')?.getAttribute('data-mobile-active-drawer')).toBe('desktop');
  });

  /** One settled turn on the REAL store + a session id: the investigation
   * button's session-active rule shows it. */
  function settleOneTurn() {
    adoptSessionId('session-mobile-track');
    act(() => {
      chatThreadStore.startTurn('which line is slow?');
      chatThreadStore.applyEvent('token', { text: 'Line 2 is the slowest.' });
      chatThreadStore.applyEvent('done', {});
    });
  }

  const sidebarTrackBtn = () =>
    container.querySelector<HTMLButtonElement>('#chat-sidebar .inv-track-btn')!;
  const modalTrackBtn = () =>
    document.querySelector<HTMLButtonElement>('#chat-modal-overlay .inv-track-btn')!;
  const modalOpen = () =>
    document.getElementById('chat-modal-overlay')!.classList.contains('open');

  it('mobile: the investigation button closes the full-screen chat drawer so the graph shows', async () => {
    await mount();
    settleOneTurn();
    expect(sidebar('chat-sidebar').classList.contains('collapsed')).toBe(false);

    act(() => sidebarTrackBtn().click());

    expect(requestArchTab).toHaveBeenCalledWith('investigation');
    expect(sidebar('chat-sidebar').classList.contains('collapsed')).toBe(true);
    expect(sidebar('memory-sidebar').classList.contains('collapsed')).toBe(true);
    expect(container.querySelector('#chat-collapse-btn')?.getAttribute('aria-expanded')).toBe('false');
    // Navigating never touches the conversation.
    expect(chatThreadStore.getState().turns).toHaveLength(1);
  });

  it('mobile: from the pop-out, the investigation button closes the modal AND the drawer', async () => {
    await mount();
    settleOneTurn();
    act(() => window.openChatModal?.());
    expect(modalOpen()).toBe(true);

    act(() => modalTrackBtn().click());

    expect(modalOpen()).toBe(false);
    expect(sidebar('chat-sidebar').classList.contains('collapsed')).toBe(true);
  });

  it('mobile: a pop-out opened over the MEMORY drawer clears memory too on investigation', async () => {
    await mount();
    settleOneTurn();
    toggle('memory-toggle-btn');
    expect(sidebar('memory-sidebar').classList.contains('collapsed')).toBe(false);
    // The response-ready toast's action: open the pop-out over memory.
    act(() => window.openChatModal?.());

    act(() => modalTrackBtn().click());

    expect(modalOpen()).toBe(false);
    expect(sidebar('chat-sidebar').classList.contains('collapsed')).toBe(true);
    expect(sidebar('memory-sidebar').classList.contains('collapsed')).toBe(true);
  });

  it('mobile: focus moves to the Investigation tab when the control that held it is hidden', async () => {
    await mount();
    settleOneTurn();
    act(() => sidebarTrackBtn().click());
    expect(requestArchTabFocus).toHaveBeenCalledWith('investigation');
  });

  it('desktop: the docked chat keeps focus; the pop-out hands it to the Investigation tab', async () => {
    query = createMediaQuery(false);
    vi.stubGlobal('matchMedia', vi.fn(() => query.media));
    await mount();
    settleOneTurn();

    act(() => sidebarTrackBtn().click());
    expect(requestArchTabFocus).not.toHaveBeenCalled();

    act(() => window.openChatModal?.());
    act(() => modalTrackBtn().click());
    expect(requestArchTabFocus).toHaveBeenCalledWith('investigation');
  });

  it('a collapsed drawer is inert (out of the Tab order); an open one is not', async () => {
    await mount();
    expect(sidebar('chat-sidebar').hasAttribute('inert')).toBe(false);
    expect(sidebar('memory-sidebar').hasAttribute('inert')).toBe(true);
    toggle('memory-toggle-btn');
    expect(sidebar('chat-sidebar').hasAttribute('inert')).toBe(true);
    expect(sidebar('memory-sidebar').hasAttribute('inert')).toBe(false);
    // The pop-out is a sibling, never inside an inert drawer.
    const overlay = document.getElementById('chat-modal-overlay')!;
    expect(sidebar('chat-sidebar').contains(overlay)).toBe(false);
    expect(sidebar('memory-sidebar').contains(overlay)).toBe(false);
  });

  it('desktop: the docked chat stays open beside the graph; the pop-out (which covers the page) closes', async () => {
    query = createMediaQuery(false);
    vi.stubGlobal('matchMedia', vi.fn(() => query.media));
    await mount();
    settleOneTurn();

    act(() => sidebarTrackBtn().click());
    expect(requestArchTab).toHaveBeenCalledWith('investigation');
    expect(sidebar('chat-sidebar').classList.contains('collapsed')).toBe(false);

    act(() => window.openChatModal?.());
    act(() => modalTrackBtn().click());
    expect(modalOpen()).toBe(false);
    expect(sidebar('chat-sidebar').classList.contains('collapsed')).toBe(false);
  });

  it('rail tooltips say what a tap will do, following the drawer state', async () => {
    await mount();
    const chatBtn = container.querySelector('#chat-collapse-btn')!;
    const memoryBtn = container.querySelector('#memory-toggle-btn')!;
    expect(chatBtn.getAttribute('title')).toBe('Close chat');
    expect(memoryBtn.getAttribute('title')).toBe('Open memory');
    toggle('memory-toggle-btn');
    expect(chatBtn.getAttribute('title')).toBe('Open chat');
    expect(memoryBtn.getAttribute('title')).toBe('Close memory');
  });
});

describe('mobile CSS contracts', () => {
  it('defines narrow shell breakpoints, dynamic viewport fallback, overflow containment, and safe areas', () => {
    expect(baseCss).toMatch(/@media\s*\(max-width:\s*768px\)/);
    expect(baseCss).toMatch(/@media\s*\(max-width:\s*430px\)/);
    expect(baseCss).toMatch(/height:\s*100vh;[\s\S]*height:\s*100dvh;/);
    expect(baseCss).toMatch(/body[\s\S]*overflow-x:\s*hidden/);
    expect(baseCss).toContain('env(safe-area-inset-bottom)');
    expect(baseCss).toMatch(/\.resize-handle\s*\{[^}]*display:\s*none\s*!important/);
  });

  it('defines coarse-pointer 44px controls and persistent memory delete affordances', () => {
    expect(baseCss).toMatch(/@media\s*\(pointer:\s*coarse\)/);
    expect(baseCss).toMatch(/@media\s*\(pointer:\s*coarse\)[\s\S]*?\.resize-handle\s*\{[^}]*display:\s*none\s*!important/);
    expect(baseCss).toMatch(/min-height:\s*44px/);
    expect(memoryCss).toMatch(/@media\s*\(pointer:\s*coarse\)/);
    expect(memoryCss).toMatch(/\.memory-del-btn\s*\{[\s\S]*?opacity:\s*1/);
    expect(memoryCss).toContain(':focus-visible');
    expect(memoryCss).toContain(':active');
  });

  it('pins the open memory drawer to both viewport edges so its close control stays on screen', () => {
    // Event 29, ~390px phone: the drawer's close rendered at x~485, off-screen.
    const mobile = memoryCss.slice(memoryCss.indexOf('@media (max-width: 768px)'));
    expect(mobile).toMatch(/\.memory-sidebar\s*\{[^}]*inset:\s*0;[^}]*width:\s*auto\s*!important;[^}]*max-width:\s*100vw;/);
    expect(mobile).toMatch(/\.memory-sidebar\.collapsed\s*\{[^}]*width:\s*0\s*!important/);
    expect(mobile).toMatch(/\.memory-header\s*>\s*div\s*\{\s*flex-shrink:\s*0;/);
  });

  it('makes memory and login dynamic-height, narrow-safe, and safe-area aware', () => {
    expect(memoryCss).toMatch(/@media\s*\(max-width:\s*768px\)/);
    expect(memoryCss).toMatch(/height:\s*100vh;[\s\S]*height:\s*100dvh;/);
    expect(memoryCss).toContain('env(safe-area-inset-bottom)');
    expect(loginCss).toMatch(/@media\s*\(max-width:\s*430px\)/);
    expect(loginCss).toMatch(/min-height:\s*100vh;[\s\S]*min-height:\s*100dvh;/);
    expect(loginCss).toContain('env(safe-area-inset-bottom)');
  });

  it('an open drawer\'s toggle renders pressed (keyed on aria-expanded)', () => {
    expect(baseCss).toMatch(
      /\.sidebar-toggle-btn\[aria-expanded="true"\]\s*\{[^}]*box-shadow:\s*inset[^}]*\}/,
    );
  });

  it('mobile: the rail stays a vertical side tab and the open drawer keeps clear of it', () => {
    const mobile = mobileBlock(baseCss);
    const rail = mobile.match(/\.sidebar-toggles\s*\{[^}]*\}/)![0];
    // Vertical like desktop: no row override (a row rail sat on top of Send).
    expect(rail).not.toMatch(/flex-direction:\s*row/);
    expect(rail).toMatch(/bottom:/);
    // The composer reserves the rail's width on the right, and its height.
    const composer = mobile.match(/\.chat-sidebar \.chat-input\s*\{[^}]*\}/)![0];
    expect(composer).toMatch(/padding-right:\s*calc\(var\(--sidebar-rail-w\)/);
    expect(composer).toMatch(/min-height:\s*calc\(2 \* var\(--sidebar-rail-w\)/);
    // The memory list scrolls its last row clear of the rail.
    const memoryContent = mobileBlock(memoryCss).match(/\.memory-content\s*\{[^}]*\}/)![0];
    expect(memoryContent).toMatch(/padding-bottom:\s*calc\(2 \* var\(--sidebar-rail-w\)/);
    // One rail size source: 28px, 44px on coarse pointers.
    expect(baseCss).toMatch(/:root\s*\{\s*--sidebar-rail-w:\s*28px;\s*\}/);
    expect(baseCss).toMatch(/@media\s*\(pointer:\s*coarse\)[\s\S]*?--sidebar-rail-w:\s*44px/);
  });

  it('mobile: the pop-out chat fills the screen (no desktop side margins)', () => {
    expect(dashboardCss).toMatch(/\.chat-modal\s*\{[^}]*margin:\s*0 7rem/);
    const modal = mobileBlock(dashboardCss).match(/\.chat-modal\s*\{[^}]*\}/)![0];
    expect(modal).toMatch(/margin:\s*0;/);
  });
});
