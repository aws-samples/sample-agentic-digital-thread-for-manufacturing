// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
/* Tests — the Onboard view's ↺ Reset All button is offered only when the
 * account is known NOT to run a workshop story (useWorkshopMode over the ONE
 * shared story GET, mocked fetch, no network): shown on a 404 (base deploy),
 * a 403 and for a known non-admin (no story request); hidden on either story's 200
 * (live clock, frozen `clock: null`, a live body whose clock failed), while
 * /api/auth/me or the story GET is in flight, after /api/auth/me failed, and
 * after a transient story failure with no body. On a workshop account it is
 * never in the DOM at any point (no flash). Mounted after the header chip it
 * adds no story GET (stale 200 or cached 404). The confirm modal stays
 * reachable through the obResetAll shim. */
import { QueryClientProvider } from '@tanstack/react-query';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { createQueryClient } from '../api/query-client';
import { queryKeys } from '../api/query-keys';
import type { PhaseTimeline, WorkshopStory } from '../api/types/workshop';
import { AuthProvider } from '../contexts/AuthContext';
import { WORKSHOP_STORY_POLL_MS } from '../features/workshop/queries';
import { t } from '../lib/i18n';
import PlantClockChip from '../pages/app/PlantClockChip';

const h = vi.hoisted(() => ({ resetAll: vi.fn() }));

vi.mock('../views/onboard/catalog', () => ({ obLoadCatalog: vi.fn() }));
vi.mock('../views/onboard/chat', () => ({ obWelcomeChat: vi.fn() }));
vi.mock('../views/onboard/flow', () => ({ obResetAll: h.resetAll }));
vi.mock('../views/onboard/globals', () => ({ registerOnboardGlobals: vi.fn() }));
vi.mock('../views/onboard/state', () => ({ loadConcepts: vi.fn() }));
vi.mock('../views/onboard/runtime-questions-link', () => ({ useRuntimeQuestionsDeepLink: vi.fn() }));
vi.mock('../views/onboard/components/CatalogList', () => ({
  CatalogList: () => <div data-testid="catalog-list" />,
}));
vi.mock('../views/onboard/components/ObFlow', () => ({ ObFlow: () => null }));
vi.mock('../views/onboard/components/ObModals', () => ({ ObModals: () => null }));

import OnboardView from '../views/onboard/OnboardView';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const STORY_URL = '/api/admin/workshop/story';
const AUTH_URL = '/api/auth/me';
const ADMIN = { user_id: 'ADT', email: 'adt@example.com', is_admin: true };

const TIMELINE: PhaseTimeline = {
  loop_started_at: '2026-10-07T18:00:00+00:00',
  loop_ends_at: '2026-10-08T02:00:00+00:00',
  phases: [
    { phase: 'degrading', start_at: 'a', end_at: 'b', start_offset_s: 0, end_offset_s: 10800 },
    { phase: 'bpfo_alarm', start_at: 'b', end_at: 'c', start_offset_s: 10800, end_offset_s: 18000 },
    { phase: 'repair', start_at: 'c', end_at: 'd', start_offset_s: 18000, end_offset_s: 21600 },
    { phase: 'healthy', start_at: 'd', end_at: 'e', start_offset_s: 21600, end_offset_s: 28800 },
  ],
  current: 'degrading',
  seconds_into_phase: 7200,
  seconds_to_next_phase: 3600,
  next_phase: 'bpfo_alarm',
  next_phase_at: '2026-10-07T21:00:00+00:00',
  next_alarm_at: '2026-10-07T21:00:00+00:00',
  seconds_to_alarm: 3600,
  alarm_active: false,
  loop_fraction: 0.25,
};

function story(overrides: Partial<WorkshopStory> = {}): WorkshopStory {
  return {
    story: 'live',
    generated_at: '2026-10-07T20:00:00+00:00',
    clock: {
      phase: 'degrading',
      anchor_source: 'placed',
      anchor_epoch: 1791403200,
      anchor_at: '2026-10-07T18:00:00+00:00',
      bpfo_alarm_at: '2026-10-07T21:00:00+00:00',
      telemetry: { anchor: 1791403200, cursor_epoch: 1791410400, backfill_complete: true },
      phase_timeline: TIMELINE,
    },
    warnings: [],
    ...overrides,
  };
}

function response(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

const never = () => new Promise<Response>(() => undefined);

/** Stub fetch per URL; returns the URL log. */
function stubFetch(handlers: { story?: () => Response | Promise<Response>; auth?: () => Response | Promise<Response> }) {
  const calls: string[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
      calls.push(url);
      if (url === STORY_URL && handlers.story) return handlers.story();
      if (url === AUTH_URL && handlers.auth) return handlers.auth();
      return response({ detail: 'unexpected' }, 500);
    }),
  );
  return calls;
}

let container: HTMLDivElement;
let root: Root;
let observer: MutationObserver;
/** true once a Reset All button has been in the DOM at ANY point (flash check). */
let everShown = false;

const resetButton = () =>
  [...container.querySelectorAll('button')].find((b) => b.textContent === t('onboard.resetAll')) ?? null;

beforeEach(() => {
  everShown = false;
  h.resetAll.mockReset();
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  observer = new MutationObserver(() => {
    if (resetButton()) everShown = true;
  });
  observer.observe(container, { childList: true, subtree: true });
});

afterEach(() => {
  observer.disconnect();
  act(() => root.unmount());
  container.remove();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

/** Render the view; `user` seeds /api/auth/me (undefined = fetched via the stub). */
async function render(user?: typeof ADMIN | (Omit<typeof ADMIN, 'is_admin'> & { is_admin: boolean })) {
  const client = createQueryClient();
  client.setQueryDefaults(queryKeys.admin.workshopStory(), { retryDelay: 0 });
  client.setQueryDefaults(queryKeys.authMe(), { retryDelay: 0 });
  if (user) client.setQueryData(queryKeys.authMe(), user);
  client.setQueryData(queryKeys.demoStatus(), { enabled: false, workshop: true });
  await act(async () => {
    root.render(
      <QueryClientProvider client={client}>
        <AuthProvider>
          <OnboardView />
        </AuthProvider>
      </QueryClientProvider>,
    );
    await Promise.resolve();
  });
  if (resetButton()) everShown = true;
  return client;
}

async function settle(ms = 20) {
  await act(async () => {
    await new Promise((r) => setTimeout(r, ms));
  });
}

async function until(check: () => void, attempts = 50) {
  for (let i = 1; ; i++) {
    await settle(10);
    try {
      check();
      return;
    } catch (error) {
      if (i >= attempts) throw error;
    }
  }
}

const storyGets = (calls: string[]) => calls.filter((u) => u === STORY_URL).length;

describe('Onboard Reset All — shown when the account runs no workshop story', () => {
  it('shows on a base deploy (story GET 404) and still opens the confirm modal', async () => {
    const calls = stubFetch({ story: () => response({ detail: 'Workshop mode is not enabled' }, 404) });
    await render(ADMIN);
    await until(() => expect(resetButton()).not.toBeNull());
    expect(storyGets(calls)).toBe(1);
    act(() => resetButton()!.click());
    expect(h.resetAll).toHaveBeenCalledTimes(1);
  });

  it('shows on a 403 (not a steward)', async () => {
    stubFetch({ story: () => response({ detail: 'Steward (admin) role required' }, 403) });
    await render(ADMIN);
    await until(() => expect(resetButton()).not.toBeNull());
  });

  it('shows for a non-admin without requesting the story', async () => {
    const calls = stubFetch({ story: () => response(story()) });
    await render({ ...ADMIN, is_admin: false });
    await until(() => expect(resetButton()).not.toBeNull());
    expect(storyGets(calls)).toBe(0);
  });

  it('stays hidden until a base deploy answers, then appears (late, never a flash)', async () => {
    let answer: (r: Response) => void = () => undefined;
    stubFetch({ story: () => new Promise<Response>((resolve) => (answer = resolve)) });
    await render(ADMIN);
    await settle();
    expect(resetButton()).toBeNull();
    expect(everShown).toBe(false);
    await act(async () => answer(response({ detail: 'Workshop mode is not enabled' }, 404)));
    await until(() => expect(resetButton()).not.toBeNull());
  });
});

describe('Onboard Reset All — hidden while a workshop story runs', () => {
  for (const [name, body] of [
    ['the live story', story()],
    ['the frozen story (clock: null)', story({ story: 'frozen', clock: null })],
    ['a live body whose clock section failed', story({ clock: null, warnings: ['clock: config miss'] })],
  ] as const) {
    it(`never renders it on ${name}`, async () => {
      const calls = stubFetch({ story: () => response(body) });
      await render(ADMIN);
      await until(() => expect(storyGets(calls)).toBe(1));
      await settle();
      expect(resetButton()).toBeNull();
      expect(everShown).toBe(false);
      // The rest of the catalog header and the catalog itself still render.
      expect(container.textContent).toContain(t('onboard.systemCatalog'));
      expect(container.querySelector('[data-testid="catalog-list"]')).not.toBeNull();
    });
  }

  it('is hidden while /api/auth/me is still loading', async () => {
    const calls = stubFetch({ auth: never, story: () => response({ detail: 'nf' }, 404) });
    await render();
    await until(() => expect(calls).toContain(AUTH_URL));
    await settle();
    expect(resetButton()).toBeNull();
    expect(storyGets(calls)).toBe(0);
  });

  it('is hidden while the story GET is in flight', async () => {
    const calls = stubFetch({ story: never });
    await render(ADMIN);
    await until(() => expect(storyGets(calls)).toBe(1));
    await settle();
    expect(resetButton()).toBeNull();
  });

  it('fails closed on a transient failure with no body (5xx after retries)', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => undefined);
    const calls = stubFetch({ story: () => response({ detail: 'boom' }, 503) });
    await render(ADMIN);
    await until(() => expect(storyGets(calls)).toBe(3)); // first try + 2 retries
    await settle();
    expect(resetButton()).toBeNull();
    expect(everShown).toBe(false);
  });

  it('fails closed when /api/auth/me failed (user unknown, not a non-admin)', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => undefined);
    const calls = stubFetch({
      auth: () => response({ detail: 'unavailable' }, 503),
      story: () => response(story()),
    });
    await render();
    // First try + 3 default retries, then the auth query settles in error with no user.
    await until(() => expect(calls.filter((u) => u === AUTH_URL)).toHaveLength(4));
    await settle();
    expect(resetButton()).toBeNull();
    expect(everShown).toBe(false);
    expect(storyGets(calls)).toBe(0);
  });
});

describe('Onboard Reset All — no extra story polling', () => {
  it('adds no story GET when OnboardView mounts after the header chip (offset poll timers)', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval', 'Date'] });
    const start = new Date('2026-10-07T20:00:00Z').getTime();
    vi.setSystemTime(start);
    /** Seconds after start of each story GET. */
    const at: number[] = [];
    stubFetch({
      story: () => {
        at.push(Math.round((Date.now() - start) / 1000));
        return response(story());
      },
    });
    const client = createQueryClient();
    client.setQueryData(queryKeys.authMe(), ADMIN);
    client.setQueryData(queryKeys.demoStatus(), { enabled: false, workshop: true });
    const mount = (onboard: boolean) =>
      act(async () => {
        root.render(
          <QueryClientProvider client={client}>
            <AuthProvider>
              <PlantClockChip />
              {onboard && <OnboardView />}
            </AuthProvider>
          </QueryClientProvider>,
        );
        await Promise.resolve();
      });
    /** Advance fake time 10 s at a time, letting each GET finish before the next timer. */
    const advance = async (seconds: number) => {
      for (let s = 0; s < seconds; s += 10) {
        await act(async () => {
          vi.advanceTimersByTime(10_000);
          await Promise.resolve();
        });
        await settle();
      }
    };
    const poll = WORKSHOP_STORY_POLL_MS / 1000;

    // The app shell's header chip is mounted at startup and owns the 60 s poll.
    await mount(false);
    await until(() => expect(at).toEqual([0]));
    expect(document.querySelector('[data-testid="plant-clock-chip"]')).not.toBeNull();

    // First Onboard visit at t=40 s: past the 30 s staleTime, between header polls.
    await advance(40);
    await mount(true);
    await settle();
    expect(resetButton()).toBeNull();

    // Past both of Onboard's own 60 s boundaries (t=100 s, t=160 s).
    await advance(130);
    expect(Math.round((Date.now() - start) / 1000)).toBe(170);
    expect(at).toEqual([0, poll, 2 * poll]);
    expect(resetButton()).toBeNull();
    expect(everShown).toBe(false);
  });

  it('reads the header chip\'s cached 404 without refetching it (base deploy)', async () => {
    const calls = stubFetch({ story: () => response({ detail: 'Workshop mode is not enabled' }, 404) });
    const client = createQueryClient();
    client.setQueryData(queryKeys.authMe(), ADMIN);
    client.setQueryData(queryKeys.demoStatus(), { enabled: false, workshop: true });
    const mount = (onboard: boolean) =>
      act(async () => {
        root.render(
          <QueryClientProvider client={client}>
            <AuthProvider>
              <PlantClockChip />
              {onboard && <OnboardView />}
            </AuthProvider>
          </QueryClientProvider>,
        );
        await Promise.resolve();
      });

    // The header chip loads the story first: a 404, cached as an error with no data.
    await mount(false);
    await until(() => expect(storyGets(calls)).toBe(1));
    await settle();
    expect(client.getQueryState(queryKeys.admin.workshopStory())?.status).toBe('error');

    // Onboard then mounts over that cached error: the button shows, no second GET.
    await mount(true);
    await until(() => expect(resetButton()).not.toBeNull());
    await settle(50);
    expect(storyGets(calls)).toBe(1);
  });
});
