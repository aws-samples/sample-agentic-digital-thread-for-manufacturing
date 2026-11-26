// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tests — UserMenu: the consolidated header dropdown renders
 * the language select, theme toggle, admin link and sign-out behind
 * #user-menu-trigger; opens/closes on trigger click, outside click and Escape
 * (which returns focus to the trigger); in-panel clicks keep it open; and the
 * controls invoke the SPA's underlying handlers (setResponseLang→adt-lang,
 * useTheme→<html>.light + theme-mode label, logout→POST /api/auth/logout).
 * Auth-gated visibility (#user-display/#admin-link/#logout-btn) follows
 * /api/auth/me via AuthProvider. */
// @vitest-environment jsdom
import { QueryClientProvider } from '@tanstack/react-query';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { createQueryClient } from '../api/query-client';
import { relativeTime as sessionRelativeTime, SessionMenu } from '../chat/SessionMenu';
import { _resetMemoryCache, getMemorySessions } from '../chat/memory-cache';
import { AuthProvider } from '../contexts/AuthContext';
import UserMenu from '../pages/app/UserMenu';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;
// This node/jsdom combo ships a broken localStorage (getItem is not a
// function) — stub a Map-backed store (useTheme + i18n-lang read/write it).
let store: Map<string, string>;

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  store = new Map<string, string>();
  vi.stubGlobal('localStorage', {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => void store.set(k, String(v)),
    removeItem: (k: string) => void store.delete(k),
  });
  document.documentElement.classList.remove('light');
  _resetMemoryCache();
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function ok(body: unknown): Response {
  return { ok: true, status: 200, json: async () => body } as unknown as Response;
}

function errorResponse(status: number, detail: string): Response {
  return {
    ok: false,
    status,
    statusText: 'Request failed',
    text: async () => JSON.stringify({ detail }),
  } as unknown as Response;
}

const ADMIN_ME = { user_id: 'u1', email: 'ops@example.com', username: null, is_admin: true };

/** Stub fetch: /api/auth/me → `me`; everything else stays pending (so the
 * logout redirect's .then never runs — jsdom can't navigate). */
function stubFetch(me: unknown) {
  const calls: Array<[string, RequestInit | undefined]> = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, options?: RequestInit) => {
      calls.push([url, options]);
      if (url === '/api/auth/me') return ok(me);
      return new Promise<Response>(() => {}); // pending forever
    }),
  );
  return calls;
}

async function render() {
  const queryClient = createQueryClient();
  await act(async () => {
    root.render(
      <QueryClientProvider client={queryClient}>
        <AuthProvider>
          <UserMenu />
        </AuthProvider>
      </QueryClientProvider>,
    );
  });
}

const trigger = () => document.getElementById('user-menu-trigger') as HTMLButtonElement;
const dropdown = () => document.getElementById('user-dropdown') as HTMLDivElement;

async function open() {
  await act(async () => {
    trigger().dispatchEvent(new MouseEvent('click', { bubbles: true }));
  });
}

describe('UserMenu — consolidated controls', () => {
  it('renders the trigger + panel with language, theme, admin link and sign-out', async () => {
    stubFetch(ADMIN_ME);
    await render();

    const t = trigger();
    expect(t).toBeTruthy();
    expect(t.getAttribute('aria-haspopup')).toBe('menu');
    expect(t.getAttribute('aria-controls')).toBe('user-dropdown');
    expect(t.getAttribute('aria-expanded')).toBe('false');

    const d = dropdown();
    expect(d.hidden).toBe(true);
    expect(d.getAttribute('role')).toBe('menu');
    // Consolidated controls keep their contractual ids inside the panel.
    expect(d.querySelector('#lang-select')).toBeTruthy();
    expect(d.querySelector('#theme-btn')).toBeTruthy();
    expect(d.querySelector('#admin-link')).toBeTruthy();
    expect(d.querySelector('#logout-btn')).toBeTruthy();
    // Labels from the spec.
    expect(d.textContent).toContain('Preferences');
    expect(d.textContent).toContain('Language');
    expect(d.textContent).toContain('Theme');
    expect(d.textContent).toContain('Account');
    expect(d.textContent).toContain('Admin panel');
    expect(d.textContent).toContain('Sign out');
    // Real buttons (keyboard accessible), menuitem roles.
    expect(d.querySelector('#theme-btn')?.tagName).toBe('BUTTON');
    expect(d.querySelector('#logout-btn')?.tagName).toBe('BUTTON');
    expect(d.querySelector('#theme-btn')?.getAttribute('role')).toBe('menuitem');
  });

  it('opens the admin panel in a new tab so the chat and a running answer survive', async () => {
    stubFetch(ADMIN_ME);
    await render();
    const link = document.getElementById('admin-link') as HTMLAnchorElement;
    expect(link.getAttribute('href')).toBe('/admin.html');
    expect(link.getAttribute('target')).toBe('_blank');
    expect(link.getAttribute('rel')).toBe('noopener');
  });

  it('shows the display name and admin link for an admin; hides them logged out', async () => {
    stubFetch(ADMIN_ME);
    await render();
    await vi.waitFor(() => expect(document.getElementById('user-display')?.textContent).toBe('ops'));
    const name = document.getElementById('user-display')!;
    expect(name.textContent).toBe('ops'); // email before @
    expect(name.style.display).not.toBe('none');
    expect((document.getElementById('admin-link') as HTMLElement).style.display).not.toBe('none');
    expect((document.getElementById('logout-btn') as HTMLElement).style.display).not.toBe('none');

    // Logged out (auth/me rejects) → all three hidden.
    act(() => root.unmount());
    root = createRoot(container);
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 401 }) as unknown as Response));
    // authFetch redirects on 401 → stub location.href assignment is not
    // possible in jsdom; use a network error instead (same null-user result).
    vi.stubGlobal('fetch', vi.fn(async () => Promise.reject(new Error('offline'))));
    await render();
    expect((document.getElementById('user-display') as HTMLElement).style.display).toBe('none');
    expect((document.getElementById('admin-link') as HTMLElement).style.display).toBe('none');
    expect((document.getElementById('logout-btn') as HTMLElement).style.display).toBe('none');
  });
});

describe('UserMenu — open/close semantics', () => {
  it('opens on trigger click and closes on a second click', async () => {
    stubFetch(ADMIN_ME);
    await render();
    await open();
    expect(dropdown().hidden).toBe(false);
    expect(trigger().getAttribute('aria-expanded')).toBe('true');
    await open();
    expect(dropdown().hidden).toBe(true);
    expect(trigger().getAttribute('aria-expanded')).toBe('false');
  });

  it('closes on outside click but stays open for in-panel clicks', async () => {
    stubFetch(ADMIN_ME);
    await render();
    await open();

    // In-panel click (theme toggle) keeps the menu open.
    await act(async () => {
      document
        .getElementById('theme-btn')!
        .dispatchEvent(new MouseEvent('click', { bubbles: true }));
    });
    expect(dropdown().hidden).toBe(false);

    // Outside click closes.
    await act(async () => {
      document.body.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    });
    expect(dropdown().hidden).toBe(true);
  });

  it('closes on Escape and returns focus to the trigger', async () => {
    stubFetch(ADMIN_ME);
    await render();
    await open();
    await act(async () => {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    });
    expect(dropdown().hidden).toBe(true);
    expect(document.activeElement).toBe(trigger());
  });
});

describe('UserMenu — underlying handlers', () => {
  it('theme item toggles the light class and updates the mode readout', async () => {
    stubFetch(ADMIN_ME);
    await render();
    await open();
    const label = document.getElementById('theme-mode-label')!;
    expect(label.textContent).toBe('Dark');
    await act(async () => {
      document
        .getElementById('theme-btn')!
        .dispatchEvent(new MouseEvent('click', { bubbles: true }));
    });
    expect(document.documentElement.classList.contains('light')).toBe(true);
    expect(localStorage.getItem('theme')).toBe('light');
    expect(label.textContent).toBe('Light');
  });

  it('language select persists via setResponseLang (adt-lang)', async () => {
    stubFetch(ADMIN_ME);
    await render();
    await open();
    const sel = document.getElementById('lang-select') as HTMLSelectElement;
    await act(async () => {
      sel.value = 'de';
      sel.dispatchEvent(new Event('change', { bubbles: true }));
    });
    expect(localStorage.getItem('adt-lang')).toBe('de');
    expect(sel.value).toBe('de');
    // Panel stays open — the select lives inside it (in-panel interaction).
    expect(dropdown().hidden).toBe(false);
  });

  it('sign-out POSTs /api/auth/logout', async () => {
    const calls = stubFetch(ADMIN_ME);
    await render();
    await open();
    await act(async () => {
      document
        .getElementById('logout-btn')!
        .dispatchEvent(new MouseEvent('click', { bubbles: true }));
    });
    const logoutCall = calls.find(([url]) => url === '/api/auth/logout');
    expect(logoutCall).toBeTruthy();
    expect(logoutCall![1]?.method).toBe('POST');
    expect(logoutCall![1]?.credentials).toBe('same-origin');
  });

  it('clears authenticated query data before replacing history after sign-out', async () => {
    const replace = vi.fn();
    vi.stubGlobal('location', { replace });
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        if (url === '/api/auth/logout') return ok({ status: 'ok' });
        throw new Error(`unexpected URL: ${url}`);
      }),
    );
    const queryClient = createQueryClient();
    queryClient.setQueryData(['auth', 'me'], ADMIN_ME);
    queryClient.setQueryData(['private', 'secret'], { value: 'cached identity data' });
    const cancelQueries = vi.spyOn(queryClient, 'cancelQueries');

    await act(async () => {
      root.render(
        <QueryClientProvider client={queryClient}>
          <AuthProvider>
            <UserMenu />
          </AuthProvider>
        </QueryClientProvider>,
      );
    });
    await open();
    await act(async () => {
      document
        .getElementById('logout-btn')!
        .dispatchEvent(new MouseEvent('click', { bubbles: true }));
    });

    await vi.waitFor(() => expect(replace).toHaveBeenCalledWith('/login.html'));
    expect(cancelQueries).toHaveBeenCalledOnce();
    expect(queryClient.getQueryCache().getAll()).toHaveLength(0);
  });

  it('shows logout server detail and keeps the menu usable when sign-out fails', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        if (url === '/api/auth/me') return ok(ADMIN_ME);
        if (url === '/api/auth/logout') return errorResponse(503, 'Identity provider unavailable');
        throw new Error(`unexpected URL: ${url}`);
      }),
    );
    await render();
    await open();
    await act(async () => {
      document
        .getElementById('logout-btn')!
        .dispatchEvent(new MouseEvent('click', { bubbles: true }));
    });

    expect(dropdown().hidden).toBe(false);
    expect(dropdown().querySelector('[role="alert"]')?.textContent).toContain(
      'Sign out failed: Identity provider unavailable',
    );
    expect((document.getElementById('logout-btn') as HTMLButtonElement).disabled).toBe(false);
  });
});


describe('SessionMenu — shared memory cache', () => {
  it('retains the week-bucket relative-time contract', () => {
    const now = Date.now();
    expect(sessionRelativeTime(null)).toBe('');
    expect(sessionRelativeTime(now - 6 * 24 * 60 * 60 * 1000)).toBe('6d ago');
    expect(sessionRelativeTime(now - 8 * 24 * 60 * 60 * 1000)).toBe('1w ago');
  });

  it('shares one in-flight sessions request with the memory cache owner', async () => {
    let resolveFetch!: (response: Response) => void;
    const fetchMock = vi.fn(
      () => new Promise<Response>((resolve) => {
        resolveFetch = resolve;
      }),
    );
    vi.stubGlobal('fetch', fetchMock);

    const panelRead = getMemorySessions();
    await act(async () => {
      root.render(
        <SessionMenu
          onNewSession={() => {}}
          onRehydrate={() => {}}
          focusQuestionInput={() => {}}
        />,
      );
    });
    await act(async () => {
      document.getElementById('session-menu-trigger')!.dispatchEvent(
        new MouseEvent('click', { bubbles: true }),
      );
    });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    await act(async () => {
      resolveFetch(ok({
        sessions: [{ sessionId: 'shared-session', label: 'Shared investigation' }],
      }));
      await panelRead;
    });

    expect(document.getElementById('session-menu')!.textContent).toContain('Shared investigation');
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('shows a shared retryable error and recovers through memory-cache', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(errorResponse(503, 'memory offline'))
      .mockResolvedValueOnce(
        ok({ sessions: [{ sessionId: 'recovered', label: 'Recovered session' }] }),
      );
    vi.stubGlobal('fetch', fetchMock);

    await act(async () => {
      root.render(
        <SessionMenu
          onNewSession={() => {}}
          onRehydrate={() => {}}
          focusQuestionInput={() => {}}
        />,
      );
    });
    await act(async () => {
      document.getElementById('session-menu-trigger')!.click();
    });
    await vi.waitFor(() => {
      expect(document.querySelector('#session-menu .async-state--error')?.textContent).toContain(
        'Sessions unavailable',
      );
    });

    await act(async () => {
      (document.querySelector('#session-menu .async-state--error button') as HTMLButtonElement).click();
    });
    await vi.waitFor(() => {
      expect(document.getElementById('session-menu')!.textContent).toContain('Recovered session');
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});