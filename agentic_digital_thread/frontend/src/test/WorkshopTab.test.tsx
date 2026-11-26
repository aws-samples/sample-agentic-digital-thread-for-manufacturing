// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tests — admin Workshop tab (ADR-0091) against a mocked fetch, no network:
 * the self-gating button/section render NOTHING without a live clock (404,
 * 403, frozen `clock: null`), the live tab renders phase / anchor source /
 * backfill / phase bar, a transient refetch failure keeps the last good
 * clock with a "refresh failed" note (a 403/404 still hides), and the
 * placement is preview-then-place — the exact D1 preview body, Place gated
 * on a preview for the SAME N that is under 10 minutes old, Cancel sends
 * nothing, Confirm sends the ABSOLUTE previewed `alarm_at` with
 * `dry_run: false`, the minutes bound comes from phase_timeline, effect /
 * warning codes are localized, buttons disabled while pending, a 409
 * in_flight / stale_anchor is rendered and re-reads the story, and every
 * server string stays inert literal text (zero HTML sinks). */
// @vitest-environment jsdom
import { QueryClientProvider } from '@tanstack/react-query';
import { act, useState } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { createQueryClient } from '../api/query-client';
import { queryKeys } from '../api/query-keys';
import type { PlaceClockPreview, WorkshopStory } from '../api/types/workshop';
import { AuthProvider } from '../contexts/AuthContext';
import { formatClockTime } from '../features/workshop/clock';
import { PREVIEW_EXPIRY_CHECK_MS } from '../pages/admin/tabs/workshop/PlaceClockControl';
import { PREVIEW_TTL_MS } from '../pages/admin/tabs/workshop/workshop-logic';
import { WorkshopTabButton, WorkshopTabSection } from '../pages/admin/tabs/workshop/WorkshopTabGate';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const STORY_URL = '/api/admin/workshop/story';
const CLOCK_URL = '/api/admin/workshop/story/clock';
const ANCHOR = 1790737810;
const HOSTILE = '<img src=x onerror="window.__pwned=1"> **not markdown**';

const LIVE: WorkshopStory = {
  story: 'live',
  generated_at: '2026-09-30T08:10:23+00:00',
  clock: {
    phase: 'degrading',
    anchor_source: 'packaged',
    anchor_epoch: ANCHOR,
    anchor_at: '2026-09-30T02:30:10+00:00',
    bpfo_alarm_at: '2026-09-30T08:35:00+00:00',
    telemetry: { anchor: ANCHOR, cursor_epoch: 1790756700, backfill_complete: false },
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
  },
  warnings: [],
};

const PREVIEW: PlaceClockPreview = {
  dry_run: true,
  expected_anchor: ANCHOR,
  new_anchor: ANCHOR,
  new_anchor_iso: '2026-09-30T02:30:10+00:00',
  alarm_at: 1790757300,
  alarm_at_iso: '2026-09-30T08:35:00+00:00',
  loops_shifted: 0,
  effect: ['rewrites the three-day history and every run number', 'moves the sensor feed onto the new clock by the next alarm (recorded sensor history stays)'],
  warnings: [HOSTILE],
  requested: { alarm_in_minutes: 90, alarm_at: 1790757300 },
};

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

/** The AdminPage contract in miniature: the parent owns `active`, the gate owns visibility. */
function Harness() {
  const [active, setActive] = useState(false);
  return (
    <>
      <nav>
        <WorkshopTabButton active={active} onActivate={() => setActive(true)} />
      </nav>
      <WorkshopTabSection active={active} />
    </>
  );
}

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

async function render(isAdmin = true) {
  const client = createQueryClient();
  // Transient-failure retries are real (twice); no back-off wait in tests.
  client.setQueryDefaults(queryKeys.admin.workshopStory(), { retryDelay: 0 });
  client.setQueryData(queryKeys.authMe(), { user_id: 'admin', email: 'admin@example.com', is_admin: isAdmin });
  client.setQueryData(queryKeys.demoStatus(), { enabled: false, workshop: true });
  await act(async () => {
    root.render(
      <QueryClientProvider client={client}>
        <AuthProvider>
          <Harness />
        </AuthProvider>
      </QueryClientProvider>,
    );
    await Promise.resolve();
  });
  return client;
}

const q = (id: string) => document.querySelector(`[data-testid="${id}"]`) as HTMLElement | null;

async function click(el: Element | null) {
  expect(el).not.toBeNull();
  await act(async () => {
    (el as HTMLElement).dispatchEvent(new MouseEvent('click', { bubbles: true }));
    await Promise.resolve();
  });
}

const setInputValue = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!;
async function typeMinutes(value: string) {
  const input = q('workshop-minutes-input') as HTMLInputElement;
  await act(async () => {
    setInputValue.call(input, value);
    input.dispatchEvent(new Event('input', { bubbles: true }));
    await Promise.resolve();
  });
}

let lastClient: ReturnType<typeof createQueryClient>;

async function openLiveTab(handler: Handler): Promise<FetchCall[]> {
  const calls = stubFetch(handler);
  lastClient = await render();
  await vi.waitFor(() => expect(q('workshop-tab-button')).toBeTruthy());
  await click(q('workshop-tab-button'));
  await vi.waitFor(() => expect(q('workshop-tab')).toBeTruthy());
  return calls;
}

/** LIVE with a shorter loop: the last phase ends at `loopSeconds`. */
function liveWithLoop(loopSeconds: number): WorkshopStory {
  const clock = LIVE.clock!;
  const phases = clock.phase_timeline.phases.map((p, i, all) =>
    i === all.length - 1 ? { ...p, end_offset_s: loopSeconds } : p,
  );
  return { ...LIVE, clock: { ...clock, phase_timeline: { ...clock.phase_timeline, phases } } };
}

function confirmButton(): HTMLButtonElement | undefined {
  const dialog = document.querySelector('[role="dialog"]');
  return Array.from(dialog?.querySelectorAll('button') ?? []).find(
    (b) => b.textContent === 'Place clock',
  ) as HTMLButtonElement | undefined;
}
function cancelButton(): HTMLButtonElement | undefined {
  const dialog = document.querySelector('[role="dialog"]');
  return Array.from(dialog?.querySelectorAll('button') ?? []).find((b) => b.textContent === 'Cancel') as
    | HTMLButtonElement
    | undefined;
}

describe('WorkshopTabGate — renders nothing without a live clock', () => {
  for (const [name, status, body] of [
    ['404 (workshop mode off)', 404, { detail: 'Workshop mode is not enabled' }],
    ['403 (not a steward)', 403, { detail: 'forbidden' }],
    ['frozen story (clock: null)', 200, { ...LIVE, story: 'frozen', clock: null }],
  ] as const) {
    it(`hides the tab button and section on ${name}, silently and with no retry`, async () => {
      const error = vi.spyOn(console, 'error').mockImplementation(() => undefined);
      const calls = stubFetch((url) => {
        if (url === STORY_URL) return response(body, status);
        throw new Error(`unexpected URL: ${url}`);
      });
      await render();
      await vi.waitFor(() => expect(storyGets(calls)).toBe(1));
      await act(async () => {
        await new Promise((r) => setTimeout(r, 20));
      });
      expect(q('workshop-tab-button')).toBeNull();
      expect(document.getElementById('tab-workshop')).toBeNull();
      expect(container.textContent).toBe('');
      expect(storyGets(calls)).toBe(1);
      expect(error).not.toHaveBeenCalled();
    });
  }

  it('does not even request the story for a non-admin', async () => {
    const calls = stubFetch((url) => {
      throw new Error(`unexpected URL: ${url}`);
    });
    await render(false);
    await act(async () => {
      await new Promise((r) => setTimeout(r, 20));
    });
    expect(calls).toHaveLength(0);
    expect(container.textContent).toBe('');
  });

  it('shows the button on a live story and mounts the tab only on activation', async () => {
    const calls = stubFetch((url) => {
      if (url === STORY_URL) return response(LIVE);
      throw new Error(`unexpected URL: ${url}`);
    });
    await render();
    await vi.waitFor(() => expect(q('workshop-tab-button')).toBeTruthy());
    expect(q('workshop-tab-button')!.getAttribute('data-tab')).toBe('tab-workshop');
    expect(document.getElementById('tab-workshop')).toBeTruthy();
    expect(q('workshop-tab')).toBeNull();
    await click(q('workshop-tab-button'));
    await vi.waitFor(() => expect(q('workshop-tab')).toBeTruthy());
    expect(document.getElementById('tab-workshop')!.className).toContain('active');
    // Button, section and tab share ONE query key: a single GET.
    expect(storyGets(calls)).toBe(1);
  });
});

describe('WorkshopTab — live clock display', () => {
  it('renders phase, anchor source, backfill, countdown and a labelled phase bar', async () => {
    await openLiveTab((url) => {
      if (url === STORY_URL) return response(LIVE);
      throw new Error(`unexpected URL: ${url}`);
    });
    expect(q('workshop-current-phase')!.textContent).toBe('Degrading');
    expect(q('workshop-anchor-source')!.getAttribute('data-source')).toBe('packaged');
    expect(q('workshop-backfill')!.getAttribute('data-complete')).toBe('false');
    expect(q('workshop-backfill')!.textContent).toContain('about 2 min');
    expect(q('workshop-alarm-countdown')!.textContent).toMatch(/0:24:3\d/);
    const bar = q('workshop-phase-bar')!;
    expect(bar.getAttribute('role')).toBe('img');
    expect(bar.getAttribute('aria-label')).toContain('Now in Degrading');
    expect(q('workshop-phase-segment-degrading')!.getAttribute('data-current')).toBe('true');
    expect(q('workshop-phase-segment-degrading')!.style.width).toMatch(/^67\.67/);
    expect(q('workshop-phase-segment-healthy')!.textContent).toBe('Healthy');
    expect(q('workshop-phase-marker')!.style.left).toBe('62.5451%');
    // The input is programmatically labelled.
    const input = q('workshop-minutes-input') as HTMLInputElement;
    expect(document.querySelector(`label[for="${input.id}"]`)!.textContent).toBe('Bearing alarm in');
    expect(input.value).toBe('90');
  });
});

describe('WorkshopTab — refresh failures (M3)', () => {
  it('keeps the last good clock on a transient refetch failure and notes it; recovers on success', async () => {
    let mode: 'ok' | 'fail' = 'ok';
    const calls = await openLiveTab((url) => {
      if (url === STORY_URL) return mode === 'ok' ? response(LIVE) : response({ detail: 'upstream' }, 502);
      throw new Error(`unexpected URL: ${url}`);
    });
    expect(q('workshop-refresh-failed')).toBeNull();
    mode = 'fail';
    await act(async () => {
      await lastClient.refetchQueries({ queryKey: queryKeys.admin.workshopStory() });
    });
    await vi.waitFor(() => expect(q('workshop-refresh-failed')).toBeTruthy());
    // 1 initial + 1 refetch + 2 retries.
    expect(storyGets(calls)).toBe(4);
    expect(q('workshop-refresh-failed')!.getAttribute('role')).toBe('status');
    expect(q('workshop-refresh-failed')!.textContent).toBe('Refresh failed; showing the last known clock.');
    expect(q('workshop-tab-button')).toBeTruthy();
    expect(q('workshop-current-phase')!.textContent).toBe('Degrading');

    mode = 'ok';
    await act(async () => {
      await lastClient.refetchQueries({ queryKey: queryKeys.admin.workshopStory() });
    });
    await vi.waitFor(() => expect(q('workshop-refresh-failed')).toBeNull());
    expect(q('workshop-tab')).toBeTruthy();
  });

  it('hides the tab when a refetch answers 404 even over cached live data', async () => {
    let status = 200;
    await openLiveTab((url) => {
      if (url === STORY_URL) return status === 200 ? response(LIVE) : response({ detail: 'off' }, status);
      throw new Error(`unexpected URL: ${url}`);
    });
    status = 404;
    await act(async () => {
      await lastClient.refetchQueries({ queryKey: queryKeys.admin.workshopStory() });
    });
    await vi.waitFor(() => expect(q('workshop-tab-button')).toBeNull());
    expect(document.getElementById('tab-workshop')).toBeNull();
  });
});

describe('PlaceClockControl — preview then place', () => {
  it('previews with the exact D1 body, gates Place on the same N, and Cancel sends nothing', async () => {
    const calls = await openLiveTab((url, method) => {
      if (url === STORY_URL) return response(LIVE);
      if (url === CLOCK_URL && method === 'POST') return response(PREVIEW);
      throw new Error(`unexpected URL: ${url}`);
    });
    const place = () => q('workshop-place-btn') as HTMLButtonElement;
    expect(place().disabled).toBe(true);
    // The disabled Place is described by the hint carrying the needs-preview reason.
    expect(place().getAttribute('aria-describedby')).toBe(q('workshop-place-reason')!.id);
    expect(q('workshop-place-reason')!.textContent).toBe('Preview this number of minutes first.');

    await click(q('workshop-preview-btn'));
    await vi.waitFor(() => expect(q('workshop-preview')).toBeTruthy());
    expect(clockPosts(calls).map((c) => c.body)).toEqual([
      { alarm_in_minutes: 90, expected_anchor_epoch: ANCHOR, dry_run: true },
    ]);
    expect(q('workshop-preview-effect')!.querySelectorAll('li')).toHaveLength(2);
    // Sink policy: the hostile warning is literal text, never parsed markup.
    const warnings = q('workshop-preview-warnings')!;
    expect(warnings.textContent).toContain(HOSTILE);
    expect(warnings.querySelector('img')).toBeNull();
    expect((window as { __pwned?: number }).__pwned).toBeUndefined();
    expect(place().disabled).toBe(false);

    // A different N invalidates the preview; back to 90 restores it.
    await typeMinutes('95');
    expect(place().disabled).toBe(true);
    expect(q('workshop-preview')).toBeNull();
    await typeMinutes('90');
    expect(place().disabled).toBe(false);

    await click(place());
    await vi.waitFor(() => expect(confirmButton()).toBeTruthy());
    expect(q('workshop-confirm-effect')!.textContent).toContain('rewrites the three-day history');
    // The dialog shows the exact instant Confirm will send.
    const alarm = q('workshop-confirm-alarm')!;
    expect(alarm.getAttribute('data-alarm-at')).toBe(PREVIEW.alarm_at_iso);
    expect(alarm.textContent).toContain(formatClockTime(PREVIEW.alarm_at_iso));
    await click(cancelButton()!);
    await vi.waitFor(() => expect(document.querySelector('[role="dialog"]')).toBeNull());
    expect(clockPosts(calls)).toHaveLength(1);
  });

  it('rejects out-of-range minutes before any request; any whole minute is valid (step 1)', async () => {
    const calls = await openLiveTab((url) => {
      if (url === STORY_URL) return response(LIVE);
      throw new Error(`unexpected URL: ${url}`);
    });
    const input = q('workshop-minutes-input') as HTMLInputElement;
    expect(input.getAttribute('step')).toBe('1');
    expect(input.getAttribute('max')).toBe('480');
    expect(q('workshop-minutes-hint')!.textContent).toBe('A whole number from 5 to 480.');
    for (const bad of ['4', '481', '30.5', '']) {
      await typeMinutes(bad);
      expect((q('workshop-preview-btn') as HTMLButtonElement).disabled).toBe(true);
      expect(q('workshop-minutes-input')!.getAttribute('aria-invalid')).toBe('true');
      expect(q('workshop-minutes-hint')!.textContent).toBe('Enter a whole number of minutes from 5 to 480.');
    }
    await typeMinutes('22');
    expect((q('workshop-preview-btn') as HTMLButtonElement).disabled).toBe(false);
    expect(input.getAttribute('aria-invalid')).toBeNull();
    expect(input.validity.stepMismatch).toBe(false);
    expect(clockPosts(calls)).toHaveLength(0);
  });

  it('bounds the minutes by the timeline loop (a 3 h loop allows at most 180)', async () => {
    const calls = await openLiveTab((url) => {
      if (url === STORY_URL) return response(liveWithLoop(10800));
      throw new Error(`unexpected URL: ${url}`);
    });
    const input = q('workshop-minutes-input') as HTMLInputElement;
    expect(input.getAttribute('max')).toBe('180');
    expect(q('workshop-minutes-hint')!.textContent).toBe('A whole number from 5 to 180.');
    await typeMinutes('181');
    expect((q('workshop-preview-btn') as HTMLButtonElement).disabled).toBe(true);
    expect(q('workshop-minutes-hint')!.textContent).toBe('Enter a whole number of minutes from 5 to 180.');
    await typeMinutes('180');
    expect((q('workshop-preview-btn') as HTMLButtonElement).disabled).toBe(false);
    expect(clockPosts(calls)).toHaveLength(0);
  });

  it('localizes effect/warning codes and falls back to server text for unknown codes', async () => {
    const coded: PlaceClockPreview = {
      ...PREVIEW,
      loops_shifted: 1,
      effect: ['EN rewrites', 'EN brand new effect'],
      effect_codes: ['rewrites_history', 'brand_new'],
      warnings: ['EN loops warning'],
      warning_codes: ['loops_shifted'],
    };
    await openLiveTab((url) => {
      if (url === STORY_URL) return response(LIVE);
      if (url === CLOCK_URL) return response(coded);
      throw new Error(`unexpected URL: ${url}`);
    });
    await click(q('workshop-preview-btn'));
    await vi.waitFor(() => expect(q('workshop-preview')).toBeTruthy());
    const effect = Array.from(q('workshop-preview-effect')!.querySelectorAll('li')).map((li) => li.textContent);
    expect(effect).toEqual(['rewrites the three-day history and every run number', 'EN brand new effect']);
    expect(q('workshop-preview-warnings')!.textContent).toContain(
      'an earlier alarm already exists in the rewritten history; the account shows a condition order now',
    );
    expect(q('workshop-preview-warnings')!.textContent).not.toContain('EN loops warning');
  });

  it('expires a preview after 10 minutes: Place disabled with a re-preview hint', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval', 'Date'] });
    try {
      const calls = await openLiveTab((url, _method, body) => {
        if (url === STORY_URL) return response(LIVE);
        if (url === CLOCK_URL && (body as { dry_run: boolean }).dry_run) return response(PREVIEW);
        throw new Error(`unexpected URL: ${url}`);
      });
      await click(q('workshop-preview-btn'));
      await vi.waitFor(() => expect(q('workshop-preview')).toBeTruthy());
      const place = () => q('workshop-place-btn') as HTMLButtonElement;
      expect(place().disabled).toBe(false);
      expect(q('workshop-preview-expired')).toBeNull();
      expect(place().hasAttribute('aria-describedby')).toBe(false);

      await act(async () => {
        vi.advanceTimersByTime(PREVIEW_TTL_MS - PREVIEW_EXPIRY_CHECK_MS);
        await Promise.resolve();
      });
      expect(place().disabled).toBe(false);

      await act(async () => {
        vi.advanceTimersByTime(PREVIEW_EXPIRY_CHECK_MS);
        await Promise.resolve();
      });
      expect(place().disabled).toBe(true);
      expect(place().getAttribute('title')).toContain('over 10 minutes old');
      // The disabled Place is described by the hint carrying the expiry reason.
      const reason = document.getElementById(place().getAttribute('aria-describedby')!);
      expect(reason!.textContent).toContain('over 10 minutes old');
      expect(q('workshop-preview')!.getAttribute('data-expired')).toBe('true');
      expect(q('workshop-preview-expired')!.textContent).toContain('preview again');

      // A fresh preview re-enables Place.
      await click(q('workshop-preview-btn'));
      await vi.waitFor(() => expect(place().disabled).toBe(false));
      expect(q('workshop-preview-expired')).toBeNull();
      expect(clockPosts(calls).every((c) => (c.body as { dry_run: boolean }).dry_run)).toBe(true);
    } finally {
      vi.useRealTimers();
    }
  });

  it('Confirm sends the same body with dry_run:false, disables both buttons while pending, then re-reads the story', async () => {
    let releasePlace: (r: Response) => void = () => undefined;
    const calls = await openLiveTab((url, _method, body) => {
      if (url === STORY_URL) return response(LIVE);
      if (url === CLOCK_URL && (body as { dry_run: boolean }).dry_run) return response(PREVIEW);
      if (url === CLOCK_URL) return new Promise<Response>((resolve) => (releasePlace = resolve));
      throw new Error(`unexpected URL: ${url}`);
    });
    await click(q('workshop-preview-btn'));
    await vi.waitFor(() => expect(q('workshop-preview')).toBeTruthy());
    await click(q('workshop-place-btn'));
    await vi.waitFor(() => expect(confirmButton()).toBeTruthy());
    await click(confirmButton()!);

    await vi.waitFor(() => expect(clockPosts(calls)).toHaveLength(2));
    // ABSOLUTE: exactly the previewed alarm, never re-resolved from N against a later `now`.
    expect(clockPosts(calls)[1].body).toEqual({
      alarm_at: PREVIEW.alarm_at_iso,
      expected_anchor_epoch: ANCHOR,
      dry_run: false,
    });
    expect((q('workshop-preview-btn') as HTMLButtonElement).disabled).toBe(true);
    expect((q('workshop-place-btn') as HTMLButtonElement).disabled).toBe(true);
    expect(q('workshop-place-btn')!.textContent).toBe('Placing…');

    const getsBefore = storyGets(calls);
    await act(async () => {
      releasePlace(response({ alarm_at: 1790757300, alarm_at_iso: '2026-09-30T08:35:00+00:00' }));
      await Promise.resolve();
    });
    await vi.waitFor(() => expect(q('workshop-placed')).toBeTruthy());
    await vi.waitFor(() => expect(storyGets(calls)).toBe(getsBefore + 1));
    // The consumed preview is gone: a second placement needs a fresh preview.
    expect((q('workshop-place-btn') as HTMLButtonElement).disabled).toBe(true);
  });

  for (const [code, text] of [
    ['in_flight', 'already in flight'],
    ['stale_anchor', 'changed since this page loaded'],
  ] as const) {
    it(`renders a 409 ${code} in role=alert and re-reads the story`, async () => {
      const calls = await openLiveTab((url, _method, body) => {
        if (url === STORY_URL) return response(LIVE);
        if (url === CLOCK_URL && (body as { dry_run: boolean }).dry_run) return response(PREVIEW);
        if (url === CLOCK_URL) return response({ detail: { error: code, message: 'server says no' } }, 409);
        throw new Error(`unexpected URL: ${url}`);
      });
      await click(q('workshop-preview-btn'));
      await vi.waitFor(() => expect(q('workshop-preview')).toBeTruthy());
      const getsBefore = storyGets(calls);
      await click(q('workshop-place-btn'));
      await vi.waitFor(() => expect(confirmButton()).toBeTruthy());
      await click(confirmButton()!);

      await vi.waitFor(() => expect(q('workshop-place-error')).toBeTruthy());
      const alert = q('workshop-place-error')!;
      expect(alert.getAttribute('role')).toBe('alert');
      expect(alert.textContent).toContain(text);
      expect(alert.textContent).toContain('server says no');
      await vi.waitFor(() => expect(storyGets(calls)).toBe(getsBefore + 1));
      expect(q('workshop-placed')).toBeNull();
    });
  }

  it('a failed preview (422) shows the error and keeps Place disabled', async () => {
    const calls = await openLiveTab((url) => {
      if (url === STORY_URL) return response(LIVE);
      if (url === CLOCK_URL) return response({ detail: { error: 'invalid', message: 'bad alarm' } }, 422);
      throw new Error(`unexpected URL: ${url}`);
    });
    const getsBefore = storyGets(calls);
    await click(q('workshop-preview-btn'));
    await vi.waitFor(() => expect(q('workshop-place-error')).toBeTruthy());
    expect(q('workshop-place-error')!.textContent).toContain('bad alarm');
    expect((q('workshop-place-btn') as HTMLButtonElement).disabled).toBe(true);
    // A preview error is not a conflict: no story re-read.
    expect(storyGets(calls)).toBe(getsBefore);
  });
});

describe('Keep-history card (ADR-0095) — move only the live clock', () => {
  const LIVE_ANCHOR = ANCHOR + 1800;
  const TWO_CLOCKS: WorkshopStory = {
    ...LIVE,
    clock: { ...LIVE.clock!, live_anchor_epoch: LIVE_ANCHOR, live_anchor_at: '2026-09-30T03:00:10+00:00',
      live_anchor_source: 'placed-live' },
  };
  const LIVE_PREVIEW: PlaceClockPreview = {
    ...PREVIEW, keep_history: true, expected_anchor: LIVE_ANCHOR, warnings: [],
    effect: ['keeps the three-day history, the failed batch and every run number as written',
      'moves the sensor feed, the live work orders and the alarm onto the new clock by the next alarm'],
    effect_codes: ['keeps_history', 'moves_live'],
  };

  async function typeLiveMinutes(value: string) {
    const input = q('workshop-live-minutes-input') as HTMLInputElement;
    await act(async () => {
      setInputValue.call(input, value);
      input.dispatchEvent(new Event('input', { bubbles: true }));
      await Promise.resolve();
    });
  }

  it('renders first, recommended, with its own ids; one clock ⇒ CAS on the history anchor and keep_history in both bodies', async () => {
    const calls = await openLiveTab((url, method) => {
      if (url === STORY_URL) return response(LIVE);
      if (url === CLOCK_URL && method === 'POST') return response(LIVE_PREVIEW);
      throw new Error(`unexpected URL: ${url}`);
    });
    const cards = Array.from(document.querySelectorAll('.workshop-card'));
    expect(cards[0].getAttribute('data-testid')).toBe('workshop-live-card');
    expect(cards[0].textContent).toContain('Move the bearing alarm, keep the history');
    expect(cards[0].textContent).toContain('Recommended');
    // The full re-placement card must not read like the safe one: attendees
    // scanning for "Move the bearing alarm" landed on it and lost their run number.
    const fullCard = document.getElementById('workshop-place-heading')!;
    expect(fullCard.textContent).toBe('Re-time the whole plant story (facilitator)');
    expect(fullCard.textContent).not.toContain('Move the bearing alarm');
    expect(q('workshop-live-clock')).toBeNull();
    // The original rewrite card and its ids are untouched.
    expect(q('workshop-place-control')).toBeTruthy();
    expect(q('workshop-live-place-control')).toBeTruthy();

    await typeLiveMinutes('10');
    await click(q('workshop-live-preview-btn'));
    await vi.waitFor(() => expect(q('workshop-live-preview')).toBeTruthy());
    expect(clockPosts(calls).map((c) => c.body)).toEqual([
      { alarm_in_minutes: 10, expected_anchor_epoch: ANCHOR, dry_run: true, keep_history: true },
    ]);
    const effect = q('workshop-live-preview-effect')!.textContent!;
    expect(effect).toContain('keeps the three-day history');
    expect(effect).not.toContain('rewrites');

    await click(q('workshop-live-place-btn'));
    const moveButton = () => Array.from(document.querySelector('[role="dialog"]')?.querySelectorAll('button') ?? [])
      .find((b) => b.textContent === 'Move the alarm') as HTMLButtonElement | undefined;
    await vi.waitFor(() => expect(moveButton()).toBeTruthy());
    expect(q('workshop-live-confirm-alarm')!.textContent).toContain('This move:');
    expect(confirmButton()).toBeUndefined();
    await click(moveButton()!);
    await vi.waitFor(() => expect(q('workshop-live-placed')).toBeTruthy());
    expect(clockPosts(calls).map((c) => c.body)[1]).toEqual(
      { alarm_at: PREVIEW.alarm_at_iso, expected_anchor_epoch: ANCHOR, dry_run: false, keep_history: true },
    );
    expect(q('workshop-live-placed')!.textContent).toContain('The history is unchanged');
  });

  it('two clocks ⇒ CAS on the live anchor, and the provenance names the live clock', async () => {
    const calls = await openLiveTab((url, method) => {
      if (url === STORY_URL) return response(TWO_CLOCKS);
      if (url === CLOCK_URL && method === 'POST') return response(LIVE_PREVIEW);
      throw new Error(`unexpected URL: ${url}`);
    });
    expect(q('workshop-live-clock')!.textContent).toContain('Live clock moved separately');
    await typeLiveMinutes('10');
    await click(q('workshop-live-preview-btn'));
    await vi.waitFor(() => expect(q('workshop-live-preview')).toBeTruthy());
    expect(clockPosts(calls)[0].body).toEqual(
      { alarm_in_minutes: 10, expected_anchor_epoch: LIVE_ANCHOR, dry_run: true, keep_history: true },
    );
    // The rewrite card still places against the HISTORY anchor.
    await typeMinutes('90');
    await click(q('workshop-preview-btn'));
    await vi.waitFor(() => expect(clockPosts(calls).length).toBe(2));
    expect(clockPosts(calls)[1].body).toEqual({ alarm_in_minutes: 90, expected_anchor_epoch: ANCHOR, dry_run: true });
  });
});
