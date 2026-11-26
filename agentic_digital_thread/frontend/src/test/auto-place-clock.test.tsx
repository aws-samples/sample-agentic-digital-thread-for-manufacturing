// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tests — Design C first-sign-in auto-place (useAutoPlaceClock) against a
 * mocked fetch, no network: it POSTs `{alarm_in_minutes, expected_anchor_epoch}`
 * exactly ONCE per page load and only for a packaged/deploy anchor with
 * `auto_place.enabled` (not for a placed clock, not when disabled/absent,
 * not for a non-admin, not before the GET has data); a 409 only re-reads
 * the story; any other error sets the chip's failed state; success re-reads
 * the story and toasts; the reset's one-shot session flag skips it once;
 * and every new i18n key exists (non-empty, same placeholders) in all six
 * locales. */
// @vitest-environment jsdom
import { QueryClientProvider } from '@tanstack/react-query';
import { act, StrictMode } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import path from 'node:path';

import { createQueryClient } from '../api/query-client';
import { queryKeys } from '../api/query-keys';
import type { StoryClock, WorkshopStory } from '../api/types/workshop';
import { AuthProvider } from '../contexts/AuthContext';
import {
  AUTO_PLACE_SUPPRESSED_KEY,
  autoPlaceRequest,
  autoPlaceStore,
  clearAutoPlaceSuppression,
  consumeAutoPlaceSuppression,
  suppressAutoPlace,
} from '../features/workshop/auto-place';
import { useAutoPlaceClock } from '../features/workshop/useAutoPlaceClock';
import PlantClockChip from '../pages/app/PlantClockChip';
import { clear as clearToasts, toastStore } from '../shared/ui/toast-store';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const STORY_URL = '/api/admin/workshop/story';
const CLOCK_URL = '/api/admin/workshop/story/clock';
const ANCHOR = 1790737810;

function story(clock: Partial<StoryClock> = {}): WorkshopStory {
  return {
    story: 'live',
    generated_at: '2026-09-30T08:10:23+00:00',
    clock: {
      phase: 'degrading',
      anchor_source: 'packaged',
      anchor_epoch: ANCHOR,
      anchor_at: '2026-09-30T02:30:10+00:00',
      bpfo_alarm_at: '2026-09-30T08:35:00+00:00',
      telemetry: { anchor: ANCHOR, cursor_epoch: null, backfill_complete: null },
      phase_timeline: {
        loop_started_at: '2026-09-30T02:30:10+00:00',
        loop_ends_at: '2026-09-30T10:30:10+00:00',
        phases: [
          { phase: 'degrading', start_at: 'a', end_at: 'b', start_offset_s: 0, end_offset_s: 19490 },
          { phase: 'bpfo_alarm', start_at: 'b', end_at: 'c', start_offset_s: 19490, end_offset_s: 25920 },
          { phase: 'repair', start_at: 'c', end_at: 'd', start_offset_s: 25920, end_offset_s: 27120 },
          { phase: 'healthy', start_at: 'd', end_at: 'e', start_offset_s: 27120, end_offset_s: 28800 },
        ],
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
      auto_place: { enabled: true, minutes: 90 },
      default_anchor_epoch: ANCHOR,
      default_anchor_iso: '2026-09-30T02:30:10+00:00',
      default_source: 'packaged',
      ...clock,
    },
    warnings: [],
  };
}

const PLACED = story({ anchor_source: 'placed', anchor_epoch: ANCHOR + 3600 });

function response(body: unknown, status = 200): Response {
  const text = JSON.stringify(body);
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status >= 200 && status < 300 ? 'OK' : 'Failed',
    json: async () => body,
    text: async () => text,
  } as unknown as Response;
}

type FetchCall = { url: string; method: string; body: unknown };
type Handler = (url: string, method: string, body: unknown) => Response | Promise<Response>;

function stubFetch(handler: Handler): FetchCall[] {
  const calls: FetchCall[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method || 'GET';
      const body = typeof init?.body === 'string' ? JSON.parse(init.body) : undefined;
      calls.push({ url, method, body });
      return handler(url, method, body);
    }),
  );
  return calls;
}

const storyGets = (calls: FetchCall[]) => calls.filter((c) => c.url === STORY_URL && c.method === 'GET').length;
const clockPosts = (calls: FetchCall[]) => calls.filter((c) => c.url === CLOCK_URL);
const never = () => new Promise<Response>(() => undefined);

/** AppShell in miniature: the hook mounted once, beside the header chip. */
function Shell() {
  useAutoPlaceClock();
  return <PlantClockChip />;
}

let container: HTMLDivElement;
let root: Root;
let session: Map<string, string>;

beforeEach(() => {
  session = new Map();
  vi.stubGlobal('sessionStorage', {
    getItem: (k: string) => session.get(k) ?? null,
    setItem: (k: string, v: string) => void session.set(k, String(v)),
    removeItem: (k: string) => void session.delete(k),
  });
  autoPlaceStore.set('idle');
  clearToasts();
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  clearToasts();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

/** A fresh "page load": a new query client and a new hook instance. */
async function load(isAdmin = true) {
  const client = createQueryClient();
  client.setQueryDefaults(queryKeys.admin.workshopStory(), { retryDelay: 0 });
  client.setQueryData(queryKeys.authMe(), { user_id: 'admin', email: 'admin@example.com', is_admin: isAdmin });
  client.setQueryData(queryKeys.demoStatus(), { enabled: false, workshop: true });
  await act(async () => {
    root.render(
      <QueryClientProvider client={client}>
        <AuthProvider>
          <Shell />
        </AuthProvider>
      </QueryClientProvider>,
    );
    await Promise.resolve();
  });
  return client;
}

async function reload(isAdmin = true) {
  act(() => root.unmount());
  root = createRoot(container);
  return load(isAdmin);
}

async function settle(ms = 30) {
  await act(async () => {
    await new Promise((r) => setTimeout(r, ms));
  });
}

async function until(check: () => void, attempts = 60) {
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

const chip = () => document.querySelector('[data-testid="plant-clock-chip"]') as HTMLElement | null;
const chipLabel = () => document.querySelector('[data-testid="plant-clock-chip-text"]')?.textContent ?? null;
const toasts = () => toastStore.get().toasts;

describe('autoPlaceRequest — the pure decision', () => {
  it('builds the body only for an enabled packaged/deploy clock', () => {
    const body = { alarm_in_minutes: 90, expected_anchor_epoch: ANCHOR };
    expect(autoPlaceRequest(story().clock)).toEqual(body);
    expect(autoPlaceRequest(story({ anchor_source: 'deploy' }).clock)).toEqual(body);
    expect(autoPlaceRequest(PLACED.clock)).toBeNull();
    expect(autoPlaceRequest(story({ anchor_source: null }).clock)).toBeNull();
    // A stored live-only move (keep-history card): an auto-place would rewrite that history.
    const liveMove = { live_anchor_source: 'placed-live' as const, live_anchor_epoch: ANCHOR + 900 };
    expect(autoPlaceRequest(story(liveMove).clock)).toBeNull();
    expect(autoPlaceRequest(story({ ...liveMove, anchor_source: 'deploy' }).clock)).toBeNull();
    expect(autoPlaceRequest(story({ live_anchor_source: null }).clock)).toEqual(body);
    expect(autoPlaceRequest(story({ auto_place: { enabled: false, minutes: 90 } }).clock)).toBeNull();
    expect(autoPlaceRequest(story({ auto_place: undefined }).clock)).toBeNull();
    expect(autoPlaceRequest(story({ auto_place: null }).clock)).toBeNull();
    expect(autoPlaceRequest(story({ auto_place: { enabled: true, minutes: 0 } }).clock)).toBeNull();
    expect(autoPlaceRequest(story({ auto_place: { enabled: true, minutes: 12.5 } }).clock)).toBeNull();
    expect(autoPlaceRequest(null)).toBeNull();
    expect(autoPlaceRequest(undefined)).toBeNull();
  });

  it('the suppression flag is one-shot and keyed to the reset anchor', () => {
    expect(consumeAutoPlaceSuppression(ANCHOR)).toBe(false);
    suppressAutoPlace(ANCHOR);
    expect(session.get(AUTO_PLACE_SUPPRESSED_KEY)).toBe(String(ANCHOR));
    // Another anchor (the clock moved on) neither suppresses nor consumes it.
    expect(consumeAutoPlaceSuppression(ANCHOR + 28800)).toBe(false);
    expect(session.get(AUTO_PLACE_SUPPRESSED_KEY)).toBe(String(ANCHOR));
    expect(consumeAutoPlaceSuppression(ANCHOR)).toBe(true);
    expect(session.has(AUTO_PLACE_SUPPRESSED_KEY)).toBe(false);
    expect(consumeAutoPlaceSuppression(ANCHOR)).toBe(false);
    suppressAutoPlace(ANCHOR);
    clearAutoPlaceSuppression();
    expect(session.has(AUTO_PLACE_SUPPRESSED_KEY)).toBe(false);
  });
});

describe('useAutoPlaceClock — fires once, only for default + enabled', () => {
  it('POSTs the exact body ONCE, re-reads the story, toasts, and the chip shows the placed clock', async () => {
    let placed = false;
    const calls = stubFetch((url) => {
      if (url === STORY_URL) return response(placed ? PLACED : story());
      placed = true;
      return response({ alarm_at_iso: '2026-09-30T09:40:00+00:00' });
    });
    const client = await load();
    await until(() => expect(clockPosts(calls)).toHaveLength(1));
    expect(clockPosts(calls)[0]).toEqual({
      url: CLOCK_URL,
      method: 'POST',
      body: { alarm_in_minutes: 90, expected_anchor_epoch: ANCHOR },
    });
    await until(() => expect(autoPlaceStore.get()).toBe('placed'));
    expect(storyGets(calls)).toBe(2); // the initial GET + the success re-read
    expect(toasts()).toHaveLength(1);
    expect(toasts()[0].kind).toBe('success');
    expect(toasts()[0].message).toBe('Your plant clock started. Bearing alarm in about 90 min.');
    await until(() => expect(chip()!.getAttribute('data-state')).toBe('placed'));
    // Later polls never fire again.
    await act(async () => {
      await client.refetchQueries({ queryKey: queryKeys.admin.workshopStory() });
    });
    await settle();
    expect(clockPosts(calls)).toHaveLength(1);
  });

  it('fires for a deploy anchor too, and never twice even if the story still says default', async () => {
    const calls = stubFetch((url) => {
      if (url === STORY_URL) return response(story({ anchor_source: 'deploy' }));
      return response({ alarm_at_iso: null });
    });
    const client = await load();
    await until(() => expect(autoPlaceStore.get()).toBe('placed'));
    for (let i = 0; i < 2; i++) {
      await act(async () => {
        await client.refetchQueries({ queryKey: queryKeys.admin.workshopStory() });
      });
    }
    await settle();
    expect(clockPosts(calls)).toHaveLength(1);
  });

  it('shows "starting…" while the POST is in flight', async () => {
    const calls = stubFetch((url) => (url === STORY_URL ? response(story()) : never()));
    await load();
    await until(() => expect(clockPosts(calls)).toHaveLength(1));
    await until(() => expect(chipLabel()).toBe('Plant clock: starting…'));
    expect(chip()!.getAttribute('data-state')).toBe('starting');
    expect(chip()!.getAttribute('data-source')).toBe('packaged');
  });

  for (const [name, body] of [
    ['a placed clock', PLACED],
    [
      'a live-only move on a packaged history (reset, then the keep-history card)',
      story({ live_anchor_source: 'placed-live', live_anchor_epoch: ANCHOR + 900 }),
    ],
    ['auto_place disabled', story({ auto_place: { enabled: false, minutes: 90 } })],
    ['auto_place absent (older server)', story({ auto_place: undefined })],
    ['the frozen story', { ...story(), story: 'frozen', clock: null }],
  ] as const) {
    it(`does NOT fire for ${name}`, async () => {
      const calls = stubFetch((url) => {
        if (url === STORY_URL) return response(body);
        throw new Error(`unexpected URL: ${url}`);
      });
      await load();
      await until(() => expect(storyGets(calls)).toBe(1));
      await settle(50);
      expect(clockPosts(calls)).toHaveLength(0);
      expect(autoPlaceStore.get()).toBe('idle');
      expect(toasts()).toHaveLength(0);
    });
  }

  it('shows the default chip when auto-place is disabled', async () => {
    stubFetch(() => response(story({ auto_place: { enabled: false, minutes: 90 } })));
    await load();
    await until(() => expect(chip()).toBeTruthy());
    expect(chipLabel()).toBe('Plant clock (default): degrading, alarm in 25 min');
    expect(chip()!.getAttribute('data-state')).toBe('default');
  });

  it('never fires once placed was seen, even if a later poll says default again', async () => {
    let body: WorkshopStory = PLACED;
    const calls = stubFetch((url) => {
      if (url === STORY_URL) return response(body);
      throw new Error(`unexpected URL: ${url}`);
    });
    const client = await load();
    await until(() => expect(chip()).toBeTruthy());
    body = story();
    await act(async () => {
      await client.refetchQueries({ queryKey: queryKeys.admin.workshopStory() });
    });
    await until(() => expect(chip()!.getAttribute('data-state')).toBe('default'));
    await settle();
    expect(clockPosts(calls)).toHaveLength(0);
  });

  it('waits for the GET: nothing is sent while the story is loading', async () => {
    const calls = stubFetch((url) => (url === STORY_URL ? never() : response({})));
    await load();
    await until(() => expect(storyGets(calls)).toBe(1));
    await settle(50);
    expect(clockPosts(calls)).toHaveLength(0);
  });

  it('does nothing for a non-admin (the steward-gated GET is never requested)', async () => {
    const calls = stubFetch((url) => {
      throw new Error(`unexpected URL: ${url}`);
    });
    await load(false);
    await settle(50);
    expect(calls).toHaveLength(0);
  });
});

describe('useAutoPlaceClock — outcomes', () => {
  for (const code of ['in_flight', 'stale_anchor']) {
    it(`a 409 ${code} only re-reads the story: no failed state, no toast, no retry`, async () => {
      const calls = stubFetch((url) => {
        if (url === STORY_URL) return response(story());
        return response({ detail: { error: code, message: 'someone else' } }, 409);
      });
      await load();
      await until(() => expect(storyGets(calls)).toBe(2));
      await settle();
      expect(clockPosts(calls)).toHaveLength(1);
      expect(autoPlaceStore.get()).toBe('idle');
      expect(chip()!.getAttribute('data-state')).toBe('default');
      expect(toasts()).toHaveLength(0);
    });
  }

  for (const status of [422, 502]) {
    it(`a ${status} sets the failed chip state ("not started. Ask for help.")`, async () => {
      const calls = stubFetch((url) => {
        if (url === STORY_URL) return response(story());
        return response({ detail: { error: 'internal', message: 'boom' } }, status);
      });
      await load();
      await until(() => expect(chip()?.getAttribute('data-state')).toBe('failed'));
      expect(chipLabel()).toBe('Plant clock: not started. Ask for help.');
      expect(chip()!.getAttribute('role')).toBe('alert');
      expect(clockPosts(calls)).toHaveLength(1);
      expect(toasts()).toHaveLength(0);
      // An apply always re-reads the story (shared mutation options) — and
      // the failed state holds over that re-read; no second POST.
      await until(() => expect(storyGets(calls)).toBe(2));
      await settle();
      expect(chip()!.getAttribute('data-state')).toBe('failed');
      expect(clockPosts(calls)).toHaveLength(1);
    });
  }

  it('a reset suppression skips this page load once; the next load auto-places again', async () => {
    const calls = stubFetch((url) => {
      if (url === STORY_URL) return response(story());
      return response({ alarm_at_iso: null });
    });
    suppressAutoPlace(ANCHOR);
    await load();
    await until(() => expect(storyGets(calls)).toBe(1));
    await settle(50);
    expect(clockPosts(calls)).toHaveLength(0);
    expect(session.has(AUTO_PLACE_SUPPRESSED_KEY)).toBe(false);
    expect(chip()!.getAttribute('data-state')).toBe('default');
    // A reload after the reset auto-places again (the packaged flag is on).
    await reload();
    await until(() => expect(clockPosts(calls)).toHaveLength(1));
  });

  it('a suppression for a different anchor does not skip, and is left for its own anchor', async () => {
    const calls = stubFetch((url) => {
      if (url === STORY_URL) return response(story());
      return response({ alarm_at_iso: null });
    });
    suppressAutoPlace(ANCHOR - 28800);
    await load();
    await until(() => expect(clockPosts(calls)).toHaveLength(1));
    expect(session.get(AUTO_PLACE_SUPPRESSED_KEY)).toBe(String(ANCHOR - 28800));
  });

  it('seeing a placed clock clears a pending suppression', async () => {
    const calls = stubFetch((url) => {
      if (url === STORY_URL) return response(PLACED);
      throw new Error(`unexpected URL: ${url}`);
    });
    suppressAutoPlace(ANCHOR);
    await load();
    await until(() => expect(storyGets(calls)).toBe(1));
    await until(() => expect(session.has(AUTO_PLACE_SUPPRESSED_KEY)).toBe(false));
    expect(clockPosts(calls)).toHaveLength(0);
  });

  it('under <React.StrictMode> (double-invoked effects) it POSTs exactly once', async () => {
    const calls = stubFetch((url) => {
      if (url === STORY_URL) return response(story());
      return response({ alarm_at_iso: '2026-09-30T09:40:00+00:00' });
    });
    const client = createQueryClient();
    client.setQueryDefaults(queryKeys.admin.workshopStory(), { retryDelay: 0 });
    client.setQueryData(queryKeys.authMe(), { user_id: 'admin', email: 'admin@example.com', is_admin: true });
    client.setQueryData(queryKeys.demoStatus(), { enabled: false, workshop: true });
    await act(async () => {
      root.render(
        <StrictMode>
          <QueryClientProvider client={client}>
            <AuthProvider>
              <Shell />
            </AuthProvider>
          </QueryClientProvider>
        </StrictMode>,
      );
      await Promise.resolve();
    });
    await until(() => expect(autoPlaceStore.get()).toBe('placed'));
    await settle(50);
    expect(clockPosts(calls)).toHaveLength(1);
    expect(toasts()).toHaveLength(1);
  });
});

// ── i18n parity for every Design C key ───────────────────────────────────────
const NEW_KEYS = [
  'plantClock.chip.default.degrading',
  'plantClock.chip.default.bpfo_alarm',
  'plantClock.chip.default.repair',
  'plantClock.chip.default.healthy',
  'plantClock.chip.default.now.degrading',
  'plantClock.chip.default.now.bpfo_alarm',
  'plantClock.chip.default.now.repair',
  'plantClock.chip.default.now.healthy',
  'plantClock.chip.starting',
  'plantClock.chip.failed',
  'plantClock.chip.titleWithSource',
  'plantClock.autoPlace.started',
  'workshop.effectCode.restores_default',
  'workshop.resetHeading',
  'workshop.resetHelp',
  'workshop.resetUnavailable',
  'workshop.resetPreview',
  'workshop.reset',
  'workshop.resetting',
  'workshop.resetNeedsPreview',
  'workshop.resetPreviewAnchor',
  'workshop.resetEffect',
  'workshop.resetConfirmTitle',
  'workshop.resetConfirmBody',
  'workshop.resetConfirm',
  'workshop.resetDone',
];

function dicts(): Record<string, Record<string, string>> {
  const src = readFileSync(path.resolve(process.cwd(), 'src/lib/i18n.ts'), 'utf-8');
  const decl = 'const I18N: Record<string, Record<string, string>> = {';
  const start = src.indexOf(decl);
  const end = src.indexOf('\n};', start) + 3;
  const objText = src.slice(src.indexOf('{', start + decl.length - 1), end - 1);
  return new Function(`return ${objText}`)() as Record<string, Record<string, string>>;
}

const placeholders = (s: string) => (s.match(/\{\w+\}/g) ?? []).sort();

describe('i18n — Design C keys in all six locales', () => {
  it('every new key is present, non-empty, with the English placeholders', () => {
    const all = dicts();
    expect(Object.keys(all)).toEqual(['en', 'ja', 'de', 'fr', 'ko', 'zh']);
    for (const key of NEW_KEYS) {
      const en = all.en[key];
      expect(en, key).toBeTruthy();
      for (const lang of Object.keys(all)) {
        expect(all[lang][key], `${lang}:${key}`).toBeTruthy();
        expect(placeholders(all[lang][key]), `${lang}:${key}`).toEqual(placeholders(en));
      }
    }
  });
});
