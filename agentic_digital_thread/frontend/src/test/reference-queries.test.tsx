// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, type ReactElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  authMeQueryOptions,
  brandingQueryOptions,
  demoStatusQueryOptions,
  onboardStatusQueryOptions,
  REFERENCE_STALE_TIME_MS,
  STATUS_REFETCH_INTERVAL_MS,
  STATUS_STALE_TIME_MS,
  useDemoStatusQuery,
  versionQueryOptions,
} from '../api/reference-queries';
import { queryKeys } from '../api/query-keys';
import { AuthProvider, useAuth } from '../contexts/AuthContext';
import { BrandingProvider, useBranding } from '../contexts/BrandingContext';
import { fillVersionBadge } from '../lib/version-badge';
import AppShell from '../pages/app/AppShell';

const showToastMock = vi.hoisted(() => vi.fn());
const refreshSuggestionsMock = vi.hoisted(() => vi.fn(async () => {}));

vi.mock('../chat', () => ({ ChatSidebar: () => <aside id="chat-sidebar" /> }));
vi.mock('../pages/app/UserMenu', () => ({ default: () => <div id="user-menu-stub" /> }));
vi.mock('../shared/agents', () => ({ AgentsFleet: () => <div>Agents</div> }));
vi.mock('../shared/ui/ToastRegion', () => ({ ToastRegion: () => null }));
vi.mock('../shared/thread', () => ({
  closeDataModal: vi.fn(),
  DataModal: () => null,
  openDataModal: vi.fn(),
  saveChatModalAsWord: vi.fn(),
  showToast: showToastMock,
  ThreadPanel: () => <div>Threads</div>,
}));
vi.mock('../views/architecture', () => ({ ArchitectureView: () => <div>Architecture</div> }));
vi.mock('../views/dashboard', () => ({
  DashboardView: () => <div data-testid="dashboard-body">Dashboard</div>,
}));
vi.mock('../views/onboard', () => ({ OnboardView: () => <div data-testid="onboard-body">Onboard</div> }));
vi.mock('../views/onboard/chat', () => ({ refreshSuggestions: refreshSuggestionsMock }));
vi.mock('../lib/i18n', () => ({ useI18n: () => ({ t: (key: string) => key }) }));

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const roots: Root[] = [];
const containers: HTMLElement[] = [];

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status >= 400 ? 'Request failed' : 'OK',
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as Response;
}

function testClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
        refetchOnWindowFocus: false,
      },
    },
  });
}

function mount(element: ReactElement): HTMLElement {
  const container = document.createElement('div');
  document.body.appendChild(container);
  const root = createRoot(container);
  roots.push(root);
  containers.push(container);
  act(() => root.render(element));
  return container;
}

function withClient(client: QueryClient, element: ReactElement): ReactElement {
  return <QueryClientProvider client={client}>{element}</QueryClientProvider>;
}

function setVisibility(value: 'visible' | 'hidden'): void {
  Object.defineProperty(document, 'visibilityState', { configurable: true, value });
  document.dispatchEvent(new Event('visibilitychange'));
}

beforeEach(() => {
  window.history.replaceState({}, '', '/');
  setVisibility('visible');
  showToastMock.mockClear();
  refreshSuggestionsMock.mockClear();
});

afterEach(() => {
  for (const root of roots.splice(0)) act(() => root.unmount());
  for (const container of containers.splice(0)) container.remove();
  document.querySelectorAll('#version-badge').forEach((element) => element.remove());
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  setVisibility('visible');
});

describe('reference query policy and contexts', () => {
  it('centralizes the required stale and refetch policy', () => {
    for (const options of [brandingQueryOptions(), authMeQueryOptions(), versionQueryOptions()]) {
      expect(options.staleTime).toBe(REFERENCE_STALE_TIME_MS);
      expect(options.refetchIntervalInBackground).toBe(false);
    }
    for (const options of [demoStatusQueryOptions(), onboardStatusQueryOptions()]) {
      expect(options.staleTime).toBe(STATUS_STALE_TIME_MS);
      expect(options.refetchInterval).toBe(STATUS_REFETCH_INTERVAL_MS);
      expect(options.refetchIntervalInBackground).toBe(false);
    }
  });

  it('preserves branding/auth context values and dedupes two branding providers', async () => {
    const calls: string[] = [];
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      calls.push(url);
      if (url === '/api/branding') return jsonResponse({ company: 'Example Corp', tagline: 'Connected' });
      if (url === '/api/auth/me') {
        return jsonResponse({ user_id: 'u1', email: 'ops@example.com', username: null, is_admin: true });
      }
      throw new Error(`unexpected URL: ${url}`);
    }));

    function BrandingProbe({ id }: { id: string }) {
      const state = useBranding();
      return <output id={id}>{JSON.stringify(state)}</output>;
    }
    function AuthProbe() {
      const state = useAuth();
      return <output id="auth-state">{JSON.stringify(state)}</output>;
    }

    const client = testClient();
    const container = mount(withClient(client, <>
      <BrandingProvider updateTitle={false}><BrandingProbe id="brand-a" /></BrandingProvider>
      <BrandingProvider updateTitle={false}><BrandingProbe id="brand-b" /></BrandingProvider>
      <AuthProvider><AuthProbe /></AuthProvider>
    </>));

    await vi.waitFor(() => {
      expect(container.querySelector('#brand-a')?.textContent).toContain('"loaded":true');
      expect(container.querySelector('#auth-state')?.textContent).toContain('ops@example.com');
    });
    expect(container.querySelector('#brand-b')?.textContent).toContain('"company":"Example Corp"');
    expect(container.querySelector('#brand-a')?.textContent).toContain('"error":null');
    expect(container.querySelector('#auth-state')?.textContent).toContain('"loading":false');
    expect(calls.filter((url) => url === '/api/branding')).toHaveLength(1);
    expect(calls.filter((url) => url === '/api/auth/me')).toHaveLength(1);
  });

  it('keeps login branding functional without a QueryClientProvider', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({ company: 'Login Co', tagline: 'Welcome' })));
    function Probe() {
      const { branding, loaded, error } = useBranding();
      return <output>{loaded ? `${branding?.company}:${error ?? 'ok'}` : 'loading'}</output>;
    }
    const container = mount(<BrandingProvider source="direct" updateTitle={false}><Probe /></BrandingProvider>);
    await vi.waitFor(() => expect(container.textContent).toContain('Login Co:ok'));
    expect(fetch).toHaveBeenCalledOnce();
  });
});

describe('reference status scheduling', () => {
  function DemoProbe() {
    const query = useDemoStatusQuery();
    return <output>{query.data?.enabled ? 'enabled' : query.fetchStatus}</output>;
  }

  it('uses one scheduled refetch interval per shared key', async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn(async () => jsonResponse({ enabled: true }));
    vi.stubGlobal('fetch', fetchMock);
    const client = testClient();
    mount(withClient(client, <><DemoProbe /><DemoProbe /></>));

    await act(async () => { await Promise.resolve(); });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(STATUS_REFETCH_INTERVAL_MS); });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('pauses scheduled status refetch while the page is hidden', async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn(async () => jsonResponse({ enabled: true }));
    vi.stubGlobal('fetch', fetchMock);
    const client = testClient();
    mount(withClient(client, <DemoProbe />));
    await act(async () => { await Promise.resolve(); });
    expect(fetchMock).toHaveBeenCalledTimes(1);

    act(() => setVisibility('hidden'));
    await act(async () => { await vi.advanceTimersByTimeAsync(STATUS_REFETCH_INTERVAL_MS * 2); });
    expect(fetchMock).toHaveBeenCalledTimes(1);

    act(() => setVisibility('visible'));
    await act(async () => { await vi.advanceTimersByTimeAsync(STATUS_REFETCH_INTERVAL_MS); });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});

describe('AppShell resolved boot status', () => {
  function shellFetch(setupComplete: boolean | 'error') {
    return vi.fn(async (url: string) => {
      if (url === '/api/onboard/status') {
        return setupComplete === 'error'
          ? jsonResponse({ detail: 'Status service unavailable' }, 503)
          : jsonResponse({ setup_complete: setupComplete });
      }
      if (url === '/api/demo/status') return jsonResponse({ enabled: false });
      if (url === '/api/version') return jsonResponse({ version: '1.2.3' });
      throw new Error(`unexpected URL: ${url}`);
    });
  }

  it('selects Onboard only after setup_complete resolves false', async () => {
    const fetchMock = shellFetch(false);
    vi.stubGlobal('fetch', fetchMock);
    const container = mount(withClient(testClient(), <AppShell />));
    expect(container.querySelector('#dashboard-view')?.classList.contains('active')).toBe(true);
    await vi.waitFor(() => expect(container.querySelector('#onboard-view')?.classList.contains('active')).toBe(true));
    expect(window.location.hash).toBe('#onboard');
    expect(fetchMock.mock.calls.filter(([url]) => url === '/api/onboard/status')).toHaveLength(1);
  });

  it('keeps Dashboard selected when setup_complete resolves true', async () => {
    vi.stubGlobal('fetch', shellFetch(true));
    const client = testClient();
    const container = mount(withClient(client, <AppShell />));
    await vi.waitFor(() => expect(client.getQueryData(queryKeys.onboardStatus())).toEqual({ setup_complete: true }));
    expect(container.querySelector('#dashboard-view')?.classList.contains('active')).toBe(true);
    expect(container.querySelector('#onboard-view')?.classList.contains('active')).toBe(false);
    expect(window.location.hash).toBe('');

    act(() => client.setQueryData(queryKeys.onboardStatus(), { setup_complete: false }));
    expect(container.querySelector('#dashboard-view')?.classList.contains('active')).toBe(true);
    expect(window.location.hash).toBe('');
  });

  it('reports onboarding status failure and leaves the current view intact', async () => {
    vi.stubGlobal('fetch', shellFetch('error'));
    const container = mount(withClient(testClient(), <AppShell />));
    await vi.waitFor(() => expect(showToastMock).toHaveBeenCalledWith(
      'Could not check onboarding status: Status service unavailable',
    ));
    expect(container.querySelector('#dashboard-view')?.classList.contains('active')).toBe(true);
    expect(container.querySelector('#onboard-view')?.classList.contains('active')).toBe(false);
    expect(window.location.hash).toBe('');
  });
});

describe('imperative version badge', () => {
  it('uses the supplied QueryClient cache and stays blank on failure', async () => {
    const badge = document.createElement('span');
    badge.id = 'version-badge';
    badge.textContent = 'v...';
    document.body.appendChild(badge);
    const fetchMock = vi.fn(async () => jsonResponse({ version: '9.4.1' }));
    vi.stubGlobal('fetch', fetchMock);
    const client = testClient();

    fillVersionBadge(client);
    fillVersionBadge(client);
    await vi.waitFor(() => expect(badge.textContent).toBe('9.4.1'));
    expect(fetchMock).toHaveBeenCalledTimes(1);

    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({ detail: 'unavailable' }, 503)));
    badge.textContent = 'v...';
    fillVersionBadge(testClient());
    await vi.waitFor(() => expect(badge.textContent).toBe(''));
  });
});
