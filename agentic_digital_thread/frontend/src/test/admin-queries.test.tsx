// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
import { QueryClientProvider, useMutation, useQueryClient } from '@tanstack/react-query';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { createQueryClient } from '../api/query-client';
import { queryKeys } from '../api/query-keys';
import { AuthProvider } from '../contexts/AuthContext';
import { BrandingProvider } from '../contexts/BrandingContext';
import AdminPage from '../pages/admin/AdminPage';
import {
  ADMIN_QUERY_POLICY,
  adminModelsQueryOptions,
  installConceptPackMutationOptions,
  saveBrandingMutationOptions,
  saveDemoSettingMutationOptions,
  saveFeedbackExplorationMutationOptions,
  saveHighByteTokenMutationOptions,
  saveLiveUpdatesMutationOptions,
  saveLlmSettingsMutationOptions,
  uninstallConceptPackMutationOptions,
} from '../pages/admin/admin-queries';
import { clear as clearToasts, toastStore } from '../shared/ui/toast-store';

vi.mock('../shared/agents', () => ({ AgentsFleet: () => null }));
vi.mock('../shared/thread', () => ({ ThreadPanel: () => null }));

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const ADMIN_URLS = [
  '/api/admin/settings',
  '/api/admin/models',
  '/api/admin/live-updates',
  '/api/admin/feedback-exploration',
  '/api/admin/llm-settings',
  '/api/admin/concept-packs',
  '/api/admin/quick-links',
  '/api/version',
] as const;

const RESPONSES: Record<string, unknown> = {
  '/api/branding': { company: 'AnyCompany', tagline: 'Manufacturing Intelligence' },
  '/api/auth/me': { user_id: 'admin', email: 'admin@example.com', is_admin: true },
  '/api/admin/settings': { status: 'ok', demo_enabled: true },
  '/api/admin/models': {
    orchestrator: [{ id: 'us.orchestrator', label: 'Orchestrator' }],
    agent: [{ id: 'us.agent', label: 'Agent' }],
  },
  '/api/admin/live-updates': { status: 'ok', live_updates_enabled: true },
  '/api/admin/feedback-exploration': {
    feedback_exploration_enabled: false,
    source: 'default',
    env_kill_active: false,
  },
  '/api/admin/llm-settings': { orchestrator_model: 'us.orchestrator', agent_model: 'us.agent' },
  '/api/admin/concept-packs': { packs: [] },
  '/api/admin/quick-links': {},
  '/api/version': { version: 'test-version' },
};

function response(body: unknown, status = 200): Response {
  const text = typeof body === 'string' ? body : JSON.stringify(body);
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status >= 200 && status < 300 ? 'OK' : 'Failed',
    json: async () => body,
    text: async () => text,
  } as unknown as Response;
}

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  vi.stubGlobal('localStorage', {
    getItem: () => null,
    setItem: () => undefined,
    removeItem: () => undefined,
  });
  history.replaceState(null, '', '#tab-agents');
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

async function renderAdmin(client = createQueryClient()) {
  client.setQueryData(queryKeys.authMe(), RESPONSES['/api/auth/me']);
  client.setQueryData(queryKeys.demoStatus(), { enabled: false, workshop: true });
  client.setQueryData(queryKeys.branding(), RESPONSES['/api/branding']);
  await act(async () => {
    root.render(
      <QueryClientProvider client={client}>
        <BrandingProvider updateTitle={false}>
          <AuthProvider>
            <AdminPage />
          </AuthProvider>
        </BrandingProvider>
      </QueryClientProvider>,
    );
    await Promise.resolve();
  });
  return client;
}

async function click(element: Element) {
  await act(async () => {
    element.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    await Promise.resolve();
  });
}

describe('admin query bootstrap and policies', () => {
  it('renders the Quick Desktop quick-link only when quick_desktop is configured', async () => {
    // Configured: the tile renders with the kiosk URL.
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      if (url === '/api/admin/quick-links') {
        return response({ quick_desktop: 'https://kiosk.example/login' });
      }
      if (url in RESPONSES) return response(RESPONSES[url]);
      throw new Error(`unexpected URL: ${url}`);
    }));
    await renderAdmin();
    await vi.waitFor(() => {
      const link = document.getElementById('link-quick-desktop') as HTMLAnchorElement;
      expect(link).toBeTruthy();
      expect(link.style.display).not.toBe('none');
      expect(link.getAttribute('href')).toBe('https://kiosk.example/login');
    });
  });

  it('hides the Quick Desktop quick-link when unconfigured (empty quick-links)', async () => {
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      if (url in RESPONSES) return response(RESPONSES[url]);
      throw new Error(`unexpected URL: ${url}`);
    }));
    await renderAdmin();
    await vi.waitFor(() => expect(document.getElementById('link-quick-desktop')).toBeTruthy());
    const link = document.getElementById('link-quick-desktop') as HTMLAnchorElement;
    expect(link.style.display).toBe('none');
  });

  it('uses the app-shell logo, nav, and user-menu header structure while preserving admin controls', async () => {
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      if (url in RESPONSES) return response(RESPONSES[url]);
      throw new Error(`unexpected URL: ${url}`);
    }));
    await renderAdmin();
    await vi.waitFor(() => expect(document.querySelector('#main-content > header')).toBeTruthy());

    const header = document.querySelector('#main-content > header')!;
    expect(header.children[0].classList.contains('logo')).toBe(true);
    expect((header.children[0] as HTMLElement).id).toBe('brand-logo');
    expect(header.children[1].classList.contains('nav')).toBe(true);
    expect(header.children[1].querySelector('.admin-badge')!.textContent).toBe('Admin');
    expect(header.children[2].classList.contains('user-menu')).toBe(true);
    expect((header.children[2] as HTMLElement).id).toBe('admin-user-menu');
    expect(header.children[2].querySelector('.back-link')!.textContent).toContain('Back to App');
    expect(header.children[2].querySelector('#adm-theme-dark')).toBeTruthy();
    expect(header.children[2].querySelector('#adm-theme-light')).toBeTruthy();
  });

  it('starts every authorized admin domain before any domain resolves', async () => {
    const started: string[] = [];
    const resolvers = new Map<string, (value: Response) => void>();
    vi.stubGlobal('fetch', vi.fn((url: string) => {
      if (url === '/api/branding' || url === '/api/auth/me') {
        return Promise.resolve(response(RESPONSES[url]));
      }
      if (ADMIN_URLS.includes(url as (typeof ADMIN_URLS)[number])) {
        started.push(url);
        return new Promise<Response>((resolve) => resolvers.set(url, resolve));
      }
      throw new Error(`unexpected URL: ${url}`);
    }));

    await renderAdmin();
    await vi.waitFor(() => expect(new Set(started)).toEqual(new Set(ADMIN_URLS)));
    expect(resolvers.size).toBe(ADMIN_URLS.length);

    await act(async () => {
      for (const url of ADMIN_URLS) resolvers.get(url)!(response(RESPONSES[url]));
      await Promise.resolve();
    });
  });

  it('pins the model catalog schedule and explicit settings/packs/links policies', () => {
    const models = adminModelsQueryOptions();
    expect(models.staleTime).toBe(10 * 60_000);
    expect(models.refetchInterval).toBe(5 * 60_000);
    expect(models.refetchIntervalInBackground).toBe(false);

    expect(ADMIN_QUERY_POLICY.settings).toEqual({ staleTime: 60_000, refetchInterval: false });
    expect(ADMIN_QUERY_POLICY.conceptPacks).toEqual({ staleTime: 120_000, refetchInterval: false });
    expect(ADMIN_QUERY_POLICY.quickLinks).toEqual({ staleTime: 300_000, refetchInterval: false });
  });
});

function MutationProbe() {
  const client = useQueryClient();
  const demo = useMutation(saveDemoSettingMutationOptions(client));
  const live = useMutation(saveLiveUpdatesMutationOptions(client));
  const feedbackExploration = useMutation(saveFeedbackExplorationMutationOptions(client));
  const branding = useMutation(saveBrandingMutationOptions(client));
  const token = useMutation(saveHighByteTokenMutationOptions(client));
  const llm = useMutation(saveLlmSettingsMutationOptions(client));
  const install = useMutation(installConceptPackMutationOptions(client));
  const uninstall = useMutation(uninstallConceptPackMutationOptions(client));
  return (
    <>
      <button id="mut-demo" onClick={() => demo.mutate(true)}>demo</button>
      <button id="mut-live" onClick={() => live.mutate({ enabled: true, intervalSeconds: 120 })}>live</button>
      <button id="mut-feedback-exploration" onClick={() => feedbackExploration.mutate(true)}>feedback-exploration</button>
      <button id="mut-branding" onClick={() => branding.mutate({ company: 'Draft', tagline: 'Tag' })}>branding</button>
      <button id="mut-token" onClick={() => token.mutate('token')}>token</button>
      <button id="mut-llm" onClick={() => llm.mutate({ orchestrator_model: 'o', agent_model: 'a' })}>llm</button>
      <button id="mut-install" onClick={() => install.mutate('pack-a')}>install</button>
      <button id="mut-uninstall" onClick={() => uninstall.mutate('pack-a')}>uninstall</button>
    </>
  );
}

describe('admin mutation invalidation', () => {
  it('invalidates only the domain keys affected by each successful mutation', async () => {
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      if (url === '/api/admin/settings') return response({ status: 'ok', demo_enabled: true });
      if (url === '/api/admin/live-updates') return response({ status: 'ok', live_updates_enabled: true });
      if (url === '/api/admin/feedback-exploration') {
        return response({ status: 'ok', feedback_exploration_enabled: true, source: 'admin-flag', env_kill_active: false });
      }
      if (url === '/api/admin/branding') return response({ status: 'ok' });
      if (url === '/api/admin/highbyte-token') return response({ status: 'ok', message: 'Saved' });
      if (url === '/api/admin/llm-settings') return response({ status: 'ok' });
      if (url.endsWith('/install')) return response({ status: 'ok', concepts_inserted: 3 });
      if (url.endsWith('/uninstall')) return response({ status: 'ok', deleted: 3 });
      throw new Error(`unexpected URL: ${url}`);
    }));
    const client = createQueryClient();
    const invalidate = vi.spyOn(client, 'invalidateQueries');
    act(() => root.render(<QueryClientProvider client={client}><MutationProbe /></QueryClientProvider>));

    const buttonIds = ['mut-demo', 'mut-live', 'mut-feedback-exploration', 'mut-branding', 'mut-token', 'mut-llm', 'mut-install', 'mut-uninstall'];
    for (const id of buttonIds) {
      await click(document.getElementById(id)!);
      await vi.waitFor(() => expect(invalidate.mock.calls.length).toBeGreaterThanOrEqual(buttonIds.indexOf(id) + 1));
    }

    expect(invalidate.mock.calls.map(([filters]) => filters?.queryKey)).toEqual([
      queryKeys.admin.settings(),
      queryKeys.admin.liveUpdates(),
      queryKeys.admin.feedbackExploration(),
      queryKeys.branding(),
      queryKeys.admin.quickLinks(),
      queryKeys.admin.llmSettings(),
      queryKeys.admin.conceptPacks(),
      queryKeys.concepts(),
      queryKeys.admin.conceptPacks(),
      queryKeys.concepts(),
    ]);
  });
});

describe('admin tab cache ownership', () => {
  it('keeps settings mounted and does not refetch fresh domains when tabs switch', async () => {
    const calls: string[] = [];
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      calls.push(url);
      if (url === '/demo-script.md') return response('# Agentic Digital Thread walkthrough');
      if (url in RESPONSES) return response(RESPONSES[url]);
      throw new Error(`unexpected URL: ${url}`);
    }));
    await renderAdmin();
    await vi.waitFor(() => expect(document.querySelector('[data-tab="tab-settings"]')).toBeTruthy());
    await vi.waitFor(() => expect(calls.filter((url) => ADMIN_URLS.includes(url as never))).toHaveLength(ADMIN_URLS.length));

    await click(document.querySelector('[data-tab="tab-settings"]')!);
    await vi.waitFor(() => expect((document.getElementById('settings-company') as HTMLInputElement).value).toBe('AnyCompany'));
    const input = document.getElementById('settings-company') as HTMLInputElement;
    const setInputValue = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!;
    act(() => {
      setInputValue.call(input, 'Unsaved operator draft');
      input.dispatchEvent(new Event('input', { bubbles: true }));
    });

    await click(document.querySelector('[data-tab="tab-agents"]')!);
    await click(document.querySelector('[data-tab="tab-settings"]')!);
    expect(document.getElementById('settings-company')).toBe(input);
    expect(input.value).toBe('Unsaved operator draft');
    expect(calls.filter((url) => url === '/api/admin/settings')).toHaveLength(1);
    for (const url of ADMIN_URLS) expect(calls.filter((call) => call === url)).toHaveLength(1);
  });

  it('reuses BrandingContext for DemoScript and keeps only the checked markdown fetch', async () => {
    const calls: string[] = [];
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      calls.push(url);
      if (url === '/demo-script.md') return response('# Agentic Digital Thread walkthrough');
      if (url in RESPONSES) return response(RESPONSES[url]);
      throw new Error(`unexpected URL: ${url}`);
    }));
    await renderAdmin();
    await vi.waitFor(() => expect(document.querySelector('[data-tab="tab-demoscript"]')).toBeTruthy());
    await vi.waitFor(() => expect((document.querySelector('[data-tab="tab-demoscript"]') as HTMLElement).style.display).not.toBe('none'));
    await click(document.querySelector('[data-tab="tab-demoscript"]')!);

    await vi.waitFor(() => expect(document.getElementById('demoscript-content')?.textContent).toContain('AnyCompany walkthrough'));
    expect(calls.filter((url) => url === '/api/branding')).toHaveLength(0);
    expect(calls.filter((url) => url === '/demo-script.md')).toHaveLength(1);
  });
});

describe('#tab-workshop hash restore (M4)', () => {
  const STORY_URL = '/api/admin/workshop/story';
  const phases = [
    { phase: 'degrading', start_at: 'a', end_at: 'b', start_offset_s: 0, end_offset_s: 19490 },
    { phase: 'bpfo_alarm', start_at: 'b', end_at: 'c', start_offset_s: 19490, end_offset_s: 25920 },
    { phase: 'repair', start_at: 'c', end_at: 'd', start_offset_s: 25920, end_offset_s: 27120 },
    { phase: 'healthy', start_at: 'd', end_at: 'e', start_offset_s: 27120, end_offset_s: 28800 },
  ];
  const LIVE_STORY = {
    story: 'live',
    generated_at: '2026-09-30T08:10:23+00:00',
    warnings: [],
    clock: {
      phase: 'degrading',
      anchor_source: 'packaged',
      anchor_epoch: 1790737810,
      anchor_at: '2026-09-30T02:30:10+00:00',
      bpfo_alarm_at: '2026-09-30T08:35:00+00:00',
      telemetry: { anchor: 1790737810, cursor_epoch: 1790756700, backfill_complete: true },
      phase_timeline: {
        loop_started_at: '2026-09-30T02:30:10+00:00',
        loop_ends_at: '2026-09-30T10:30:10+00:00',
        phases,
        current: 'degrading',
        seconds_into_phase: 18013,
        seconds_to_next_phase: 1477,
        next_phase: 'bpfo_alarm',
        next_phase_at: '2026-09-30T08:35:00+00:00',
        next_alarm_at: '2026-09-30T08:35:00+00:00',
        seconds_to_alarm: 1477,
        alarm_active: false,
        loop_fraction: 0.625451,
      },
    },
  };

  const activePanel = () => document.querySelector('section.tab-panel.active')?.id;

  for (const [name, status, body] of [
    ['a frozen story (clock: null)', 200, { ...LIVE_STORY, story: 'frozen', clock: null }],
    ['workshop mode off (404)', 404, { detail: 'Workshop mode is not enabled' }],
  ] as const) {
    it(`never activates the unrendered tab on ${name}`, async () => {
      history.replaceState(null, '', '#tab-workshop');
      const calls: string[] = [];
      vi.stubGlobal('fetch', vi.fn(async (url: string) => {
        calls.push(url);
        if (url === STORY_URL) return response(body, status);
        if (url in RESPONSES) return response(RESPONSES[url]);
        throw new Error(`unexpected URL: ${url}`);
      }));
      await renderAdmin();
      await vi.waitFor(() => expect(calls).toContain('/api/admin/settings'));
      await vi.waitFor(() => expect(calls).toContain(STORY_URL));
      await act(async () => {
        await new Promise((r) => setTimeout(r, 30));
      });
      expect(activePanel()).toBe('tab-agents');
      expect(document.getElementById('tab-workshop')).toBeNull();
      expect(document.querySelector('[data-tab="tab-agents"]')!.className).toBe('active');
    });

    it(`falls back to the default tab when the gate settles hidden after the hash restored it (${name})`, async () => {
      history.replaceState(null, '', '#tab-workshop');
      let releaseStory: (r: Response) => void = () => undefined;
      vi.stubGlobal('fetch', vi.fn(async (url: string) => {
        if (url === STORY_URL) return new Promise<Response>((resolve) => (releaseStory = resolve));
        if (url in RESPONSES) return response(RESPONSES[url]);
        throw new Error(`unexpected URL: ${url}`);
      }));
      await renderAdmin();
      // Settings resolved while the gate is still pending: the hash restores the tab.
      await vi.waitFor(() => expect(document.querySelector('[data-tab="tab-agents"]')!.className).not.toBe('active'));
      await act(async () => {
        releaseStory(response(body, status));
        await Promise.resolve();
      });
      await vi.waitFor(() => expect(activePanel()).toBe('tab-agents'));
      expect(document.getElementById('tab-workshop')).toBeNull();
      expect(window.location.hash).toBe('#tab-agents');
    });
  }

  it('restores the Workshop tab from its hash on a live story', async () => {
    history.replaceState(null, '', '#tab-workshop');
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      if (url === STORY_URL) return response(LIVE_STORY);
      if (url in RESPONSES) return response(RESPONSES[url]);
      throw new Error(`unexpected URL: ${url}`);
    }));
    await renderAdmin();
    await vi.waitFor(() => expect(activePanel()).toBe('tab-workshop'));
    await vi.waitFor(() => expect(document.querySelector('[data-testid="workshop-tab"]')).toBeTruthy());
    expect(window.location.hash).toBe('#tab-workshop');
  });
});

describe('i3X explorer link failures (app toast, never alert)', () => {
  const I3X_LINKS = {
    i3x_explorer: true,
    highbyte: 'https://highbyte.example',
    highbyte_i3x_token: 'configured',
  };

  afterEach(() => clearToasts());

  async function clickI3x(mint: () => Response) {
    const nativeAlert = vi.fn();
    vi.stubGlobal('alert', nativeAlert);
    vi.spyOn(window, 'open').mockImplementation(() => null);
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      if (url === '/api/admin/quick-links') return response(I3X_LINKS);
      if (url === '/api/admin/i3x-explorer-link/highbyte') return mint();
      if (url in RESPONSES) return response(RESPONSES[url]);
      throw new Error(`unexpected URL: ${url}`);
    }));
    await renderAdmin();
    await vi.waitFor(() =>
      expect((document.getElementById('link-i3x-highbyte') as HTMLElement).style.display).not.toBe('none'),
    );
    await click(document.getElementById('link-i3x-highbyte')!);
    return nativeAlert;
  }

  it('a refused mint is an urgent error toast with the server message', async () => {
    const nativeAlert = await clickI3x(() => response({ status: 'error', message: 'token expired' }));
    await vi.waitFor(() =>
      expect(toastStore.get().toasts.map((t) => [t.kind, t.message, t.urgent])).toEqual([
        ['error', 'Could not open i3X explorer: token expired', true],
      ]),
    );
    expect(nativeAlert).not.toHaveBeenCalled();
    expect(window.open).not.toHaveBeenCalled();
  });

  it('a failed mint request is an urgent error toast with the error detail', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    const nativeAlert = await clickI3x(() => response({ detail: 'i3X backend unavailable' }, 503));
    await vi.waitFor(() => {
      const [toast] = toastStore.get().toasts;
      expect(toast?.kind).toBe('error');
      expect(toast?.urgent).toBe(true);
      expect(toast?.message).toContain('Could not open i3X explorer: ');
      expect(toast?.message).toContain('i3X backend unavailable');
    });
    expect(nativeAlert).not.toHaveBeenCalled();
  });

  it('hides the links when the server has no explorer build', async () => {
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      if (url === '/api/admin/quick-links') return response({ ...I3X_LINKS, i3x_explorer: false });
      if (url in RESPONSES) return response(RESPONSES[url]);
      throw new Error(`unexpected URL: ${url}`);
    }));
    await renderAdmin();
    await vi.waitFor(() =>
      expect((document.getElementById('link-highbyte') as HTMLElement).style.display).not.toBe('none'),
    );
    expect((document.getElementById('link-i3x-highbyte') as HTMLElement).style.display).toBe('none');
  });
});
