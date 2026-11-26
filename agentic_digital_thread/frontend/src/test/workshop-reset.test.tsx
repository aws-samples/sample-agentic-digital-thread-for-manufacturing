// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tests — Design C "Reset to the default clock" failsafe in the admin
 * Workshop tab, against a mocked fetch: disabled with a hint when the GET
 * has no `default_anchor_epoch` (null or absent); Preview sends the exact
 * `{reset: true, expected_anchor_epoch, dry_run: true}` body and shows the
 * default alarm/anchor plus localized effect lines; Reset is gated on that
 * preview and sits behind the ConfirmDialog (Cancel sends nothing); Confirm
 * sends `dry_run: false`, disables both buttons while pending, re-reads the
 * story and sets the one-shot `adt.autoPlace.suppressed` flag to the placed
 * anchor (even after an unmount); a disabled Reset is aria-describedby its
 * reason; a 409 is
 * shown in role=alert and re-reads the story; server strings stay inert. */
// @vitest-environment jsdom
import { QueryClientProvider } from '@tanstack/react-query';
import { act, useState } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { createQueryClient } from '../api/query-client';
import { queryKeys } from '../api/query-keys';
import type { ResetClockPreview, StoryClock, WorkshopStory } from '../api/types/workshop';
import { AuthProvider } from '../contexts/AuthContext';
import { AUTO_PLACE_SUPPRESSED_KEY } from '../features/workshop/auto-place';
import { formatClockTime } from '../features/workshop/clock';
import { t } from '../lib/i18n';
import { WorkshopTabButton, WorkshopTabSection } from '../pages/admin/tabs/workshop/WorkshopTabGate';
import {
  resetAppliedAnchor,
  resetAvailable,
  resetBlockedKey,
  resetPreviewSummary,
  PREVIEW_TTL_MS,
} from '../pages/admin/tabs/workshop/workshop-logic';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const STORY_URL = '/api/admin/workshop/story';
const CLOCK_URL = '/api/admin/workshop/story/clock';
const ANCHOR = 1790741410; // a placed anchor
const DEFAULT_ANCHOR = 1790737810;
const DEFAULT_ISO = '2026-09-30T02:30:10+00:00';
const APPLIED_ANCHOR = DEFAULT_ANCHOR + 28800; // the default normalized onto the current loop
const HOSTILE = '<img src=x onerror="window.__pwned=1"> **not markdown**';

function story(clock: Partial<StoryClock> = {}): WorkshopStory {
  return {
    story: 'live',
    generated_at: '2026-09-30T08:10:23+00:00',
    clock: {
      phase: 'degrading',
      anchor_source: 'placed',
      anchor_epoch: ANCHOR,
      anchor_at: '2026-09-30T03:30:10+00:00',
      bpfo_alarm_at: '2026-09-30T09:35:00+00:00',
      telemetry: { anchor: ANCHOR, cursor_epoch: 1790756700, backfill_complete: true },
      phase_timeline: {
        loop_started_at: '2026-09-30T03:30:10+00:00',
        loop_ends_at: '2026-09-30T11:30:10+00:00',
        phases: [
          { phase: 'degrading', start_at: 'a', end_at: 'b', start_offset_s: 0, end_offset_s: 19490 },
          { phase: 'bpfo_alarm', start_at: 'b', end_at: 'c', start_offset_s: 19490, end_offset_s: 25920 },
          { phase: 'repair', start_at: 'c', end_at: 'd', start_offset_s: 25920, end_offset_s: 27120 },
          { phase: 'healthy', start_at: 'd', end_at: 'e', start_offset_s: 27120, end_offset_s: 28800 },
        ],
        current: 'degrading',
        seconds_into_phase: 14413,
        seconds_to_next_phase: 5077,
        next_phase: 'bpfo_alarm',
        next_phase_at: '2026-09-30T09:35:00+00:00',
        next_alarm_at: '2026-09-30T09:35:00+00:00',
        seconds_to_alarm: 5077,
        alarm_active: false,
        loop_fraction: 0.5,
      },
      auto_place: { enabled: true, minutes: 90 },
      default_anchor_epoch: DEFAULT_ANCHOR,
      default_anchor_iso: DEFAULT_ISO,
      default_source: 'packaged',
      ...clock,
    },
    warnings: [],
  };
}

const RESET_PREVIEW: ResetClockPreview = {
  dry_run: true,
  reset: true,
  expected_anchor: ANCHOR,
  new_anchor: DEFAULT_ANCHOR,
  new_anchor_iso: DEFAULT_ISO,
  alarm_at: 1790757300,
  alarm_at_iso: '2026-09-30T08:35:00+00:00',
  default_anchor_epoch: DEFAULT_ANCHOR,
  default_anchor_iso: DEFAULT_ISO,
  default_source: 'packaged',
  effect: ['restores the default anchor', 'rewrites the three-day history and every run number', HOSTILE],
  effect_codes: ['restores_default', 'rewrites_history', 'future_code'],
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

function Harness() {
  const [active, setActive] = useState(false);
  return (
    <>
      <WorkshopTabButton active={active} onActivate={() => setActive(true)} />
      <WorkshopTabSection active={active} />
    </>
  );
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

const q = (id: string) => document.querySelector(`[data-testid="${id}"]`) as HTMLElement | null;
const btn = (id: string) => q(id) as HTMLButtonElement;

async function click(el: Element | null | undefined) {
  expect(el).toBeTruthy();
  await act(async () => {
    (el as HTMLElement).dispatchEvent(new MouseEvent('click', { bubbles: true }));
    await Promise.resolve();
  });
}

function dialogButton(label: string): HTMLButtonElement | undefined {
  const dialog = document.querySelector('[role="dialog"]');
  return Array.from(dialog?.querySelectorAll('button') ?? []).find((b) => b.textContent === label) as
    | HTMLButtonElement
    | undefined;
}

async function openTab(handler: Handler): Promise<FetchCall[]> {
  const calls = stubFetch(handler);
  const client = createQueryClient();
  client.setQueryDefaults(queryKeys.admin.workshopStory(), { retryDelay: 0 });
  client.setQueryData(queryKeys.authMe(), { user_id: 'admin', email: 'admin@example.com', is_admin: true });
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
  await vi.waitFor(() => expect(q('workshop-tab-button')).toBeTruthy());
  await click(q('workshop-tab-button'));
  await vi.waitFor(() => expect(q('workshop-reset-control')).toBeTruthy());
  return calls;
}

describe('reset logic', () => {
  it('resetAvailable only for a finite default anchor', () => {
    expect(resetAvailable(DEFAULT_ANCHOR)).toBe(true);
    expect(resetAvailable(null)).toBe(false);
    expect(resetAvailable(undefined)).toBe(false);
    expect(resetAvailable(Number.NaN)).toBe(false);
  });

  it('resetPreviewSummary shows the server alarm and default anchor; falls back to the GET anchor', () => {
    const fmt = (iso: string) => `<${iso}>`;
    expect(resetPreviewSummary(RESET_PREVIEW, null, t, fmt)).toEqual([
      'Bearing alarm at <2026-09-30T08:35:00+00:00>',
      `Default anchor <${DEFAULT_ISO}>`,
    ]);
    expect(resetPreviewSummary({ dry_run: true, effect: [] }, DEFAULT_ISO, t, fmt)).toEqual([
      `Default anchor <${DEFAULT_ISO}>`,
    ]);
    expect(resetPreviewSummary({ dry_run: true, effect: [] }, null, t, fmt)).toEqual([]);
  });

  it('resetPreviewSummary prefers the placed new_anchor_iso over default_anchor_iso', () => {
    const fmt = (iso: string) => `<${iso}>`;
    const normalized = { ...RESET_PREVIEW, new_anchor_iso: '2026-09-30T10:30:10+00:00' };
    expect(resetPreviewSummary(normalized, null, t, fmt)[1]).toBe('Default anchor <2026-09-30T10:30:10+00:00>');
    const noNew = { ...RESET_PREVIEW, new_anchor_iso: undefined };
    expect(resetPreviewSummary(noNew, null, t, fmt)[1]).toBe(`Default anchor <${DEFAULT_ISO}>`);
  });

  it('resetAppliedAnchor: the apply report, else the preview, else the stored default', () => {
    expect(resetAppliedAnchor({ new_anchor: 42 }, RESET_PREVIEW, 7)).toBe(42);
    expect(resetAppliedAnchor({ alarm_at_iso: null }, RESET_PREVIEW, 7)).toBe(DEFAULT_ANCHOR);
    expect(resetAppliedAnchor(undefined, null, 7)).toBe(7);
    expect(resetAppliedAnchor({ new_anchor: 'x' }, { dry_run: true, effect: [] }, 7)).toBe(7);
  });

  it('resetBlockedKey names why Reset is disabled', () => {
    expect(resetBlockedKey(false, true, false)).toBe('workshop.resetUnavailable');
    expect(resetBlockedKey(true, true, true)).toBe('workshop.previewExpired');
    expect(resetBlockedKey(true, false, false)).toBe('workshop.resetNeedsPreview');
    expect(resetBlockedKey(true, true, false)).toBeNull();
  });
});

describe('ResetClockControl — availability', () => {
  it('never calls a deploy-recorded default "packaged"', async () => {
    // A Workshop Studio account records its default at deploy
    // (default_source "deploy"); the Reset copy must not claim a package.
    await openTab((url) => {
      if (url === STORY_URL) return response(story({ default_source: 'deploy' }));
      throw new Error(`unexpected URL: ${url}`);
    });
    const text = q('workshop-reset-control')!.textContent ?? '';
    expect(text).toContain('Reset to the default clock');
    expect(text.toLowerCase()).not.toContain('packaged');
  });

  for (const [name, clock] of [
    ['default_anchor_epoch null', { default_anchor_epoch: null, default_anchor_iso: null }],
    ['default fields absent (older server)', { default_anchor_epoch: undefined, default_anchor_iso: undefined }],
  ] as const) {
    it(`is disabled with a hint when ${name}`, async () => {
      const calls = await openTab((url) => {
        if (url === STORY_URL) return response(story(clock));
        throw new Error(`unexpected URL: ${url}`);
      });
      expect(q('workshop-reset-control')!.textContent).toContain('Reset to the default clock');
      expect(btn('workshop-reset-preview-btn').disabled).toBe(true);
      expect(btn('workshop-reset-btn').disabled).toBe(true);
      expect(q('workshop-reset-hint')!.textContent).toBe(
        'No default clock is available for this deployment, so the clock cannot be reset.',
      );
      expect(btn('workshop-reset-preview-btn').getAttribute('aria-describedby')).toBe(q('workshop-reset-hint')!.id);
      // The disabled Reset is described by the hint carrying the no-default reason.
      expect(btn('workshop-reset-btn').getAttribute('aria-describedby')).toBe(q('workshop-reset-hint')!.id);
      expect(q('workshop-reset-reason')).toBeNull();
      await click(q('workshop-reset-preview-btn'));
      expect(clockPosts(calls)).toHaveLength(0);
    });
  }

  it('documents that a reload auto-places again, with the default anchor time', async () => {
    await openTab((url) => {
      if (url === STORY_URL) return response(story());
      throw new Error(`unexpected URL: ${url}`);
    });
    const hint = q('workshop-reset-hint')!.textContent!;
    expect(hint).toContain(formatClockTime(DEFAULT_ISO));
    expect(hint).toContain('A reload afterwards starts the clock again automatically unless auto-place is turned off.');
    expect(btn('workshop-reset-btn').disabled).toBe(true);
    expect(btn('workshop-reset-btn').title).toBe('Preview the reset first.');
    const reasonId = btn('workshop-reset-btn').getAttribute('aria-describedby')!;
    expect(reasonId).toBe(q('workshop-reset-reason')!.id);
    expect(document.getElementById(reasonId)!.textContent).toBe('Preview the reset first.');
  });
});

describe('ResetClockControl — preview → confirm → reset', () => {
  it('previews with the exact body and shows the default alarm, anchor and localized effect lines', async () => {
    const calls = await openTab((url) => {
      if (url === STORY_URL) return response(story());
      return response(RESET_PREVIEW);
    });
    await click(q('workshop-reset-preview-btn'));
    await vi.waitFor(() => expect(q('workshop-reset-preview')).toBeTruthy());
    expect(clockPosts(calls)).toHaveLength(1);
    expect(clockPosts(calls)[0].body).toEqual({ reset: true, expected_anchor_epoch: ANCHOR, dry_run: true });
    const summary = Array.from(q('workshop-reset-preview-summary')!.querySelectorAll('li')).map((li) => li.textContent);
    expect(summary).toEqual([
      `Bearing alarm at ${formatClockTime(RESET_PREVIEW.alarm_at_iso)}`,
      `Default anchor ${formatClockTime(DEFAULT_ISO)}`,
    ]);
    const effect = Array.from(q('workshop-reset-preview-effect')!.querySelectorAll('li')).map((li) => li.textContent);
    expect(effect).toEqual([
      'restores the default anchor',
      'rewrites the three-day history and every run number',
      HOSTILE, // unknown code → the server text, as an inert literal
    ]);
    expect(q('workshop-reset-preview-effect')!.querySelector('img')).toBeNull();
    expect((window as { __pwned?: number }).__pwned).toBeUndefined();
    // A dry run does not re-read the story.
    expect(storyGets(calls)).toBe(1);
    expect(btn('workshop-reset-btn').disabled).toBe(false);
    expect(btn('workshop-reset-btn').hasAttribute('aria-describedby')).toBe(false);
    expect(q('workshop-reset-reason')).toBeNull();
  });

  it('an expired preview disables Reset, described by the expiry reason', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval', 'Date'] });
    try {
      await openTab((url) => (url === STORY_URL ? response(story()) : response(RESET_PREVIEW)));
      await click(q('workshop-reset-preview-btn'));
      await vi.waitFor(() => expect(btn('workshop-reset-btn').disabled).toBe(false));
      await act(async () => {
        vi.advanceTimersByTime(PREVIEW_TTL_MS);
        await Promise.resolve();
      });
      expect(q('workshop-reset-preview')!.getAttribute('data-expired')).toBe('true');
      expect(btn('workshop-reset-btn').disabled).toBe(true);
      const reasonId = btn('workshop-reset-btn').getAttribute('aria-describedby')!;
      expect(document.getElementById(reasonId)!.textContent).toBe(t('workshop.previewExpired'));
    } finally {
      vi.useRealTimers();
    }
  });

  it('Cancel sends nothing; Confirm sends dry_run:false, disables both buttons while pending, re-reads and suppresses auto-place', async () => {
    let releaseApply: (r: Response) => void = () => undefined;
    const calls = await openTab((url, _method, body) => {
      if (url === STORY_URL) return response(story());
      if ((body as { dry_run: boolean }).dry_run) return response(RESET_PREVIEW);
      return new Promise<Response>((resolve) => {
        releaseApply = resolve;
      });
    });
    await click(q('workshop-reset-preview-btn'));
    await vi.waitFor(() => expect(btn('workshop-reset-btn').disabled).toBe(false));

    await click(q('workshop-reset-btn'));
    await vi.waitFor(() => expect(dialogButton('Reset clock')).toBeTruthy());
    expect(document.querySelector('[role="dialog"]')!.textContent).toContain('Reset the plant clock?');
    expect(q('workshop-reset-confirm-body')!.textContent).toContain('restores the default anchor');
    await click(dialogButton('Cancel'));
    expect(document.querySelector('[role="dialog"]')).toBeNull();
    expect(clockPosts(calls)).toHaveLength(1);

    await click(q('workshop-reset-btn'));
    await vi.waitFor(() => expect(dialogButton('Reset clock')).toBeTruthy());
    await click(dialogButton('Reset clock'));
    await vi.waitFor(() => expect(clockPosts(calls)).toHaveLength(2));
    expect(clockPosts(calls)[1].body).toEqual({ reset: true, expected_anchor_epoch: ANCHOR, dry_run: false });
    await vi.waitFor(() => expect(btn('workshop-reset-btn').textContent).toBe('Resetting…'));
    expect(btn('workshop-reset-btn').disabled).toBe(true);
    expect(btn('workshop-reset-preview-btn').disabled).toBe(true);

    await act(async () => {
      releaseApply(response({ reset: true, new_anchor: APPLIED_ANCHOR, alarm_at_iso: '2026-09-30T08:35:00+00:00' }));
      await Promise.resolve();
    });
    await vi.waitFor(() => expect(q('workshop-reset-done')).toBeTruthy());
    expect(q('workshop-reset-done')!.getAttribute('role')).toBe('status');
    expect(q('workshop-reset-done')!.textContent).toBe('Reset to the default clock, sensor feed catching up.');
    expect(storyGets(calls)).toBe(2);
    // Keyed to the anchor the reset placed (the apply report's new_anchor).
    expect(session.get(AUTO_PLACE_SUPPRESSED_KEY)).toBe(String(APPLIED_ANCHOR));
    expect(q('workshop-reset-preview')).toBeNull();
  });

  it('the suppression flag is still set when the control unmounts before the reset resolves', async () => {
    let releaseApply: (r: Response) => void = () => undefined;
    await openTab((url, _method, body) => {
      if (url === STORY_URL) return response(story());
      if ((body as { dry_run: boolean }).dry_run) return response(RESET_PREVIEW);
      return new Promise<Response>((resolve) => {
        releaseApply = resolve;
      });
    });
    await click(q('workshop-reset-preview-btn'));
    await vi.waitFor(() => expect(btn('workshop-reset-btn').disabled).toBe(false));
    await click(q('workshop-reset-btn'));
    await vi.waitFor(() => expect(dialogButton('Reset clock')).toBeTruthy());
    await click(dialogButton('Reset clock'));
    await vi.waitFor(() => expect(btn('workshop-reset-btn').textContent).toBe('Resetting…'));
    // Leave the admin page mid-reset.
    act(() => root.unmount());
    root = createRoot(container);
    await act(async () => {
      releaseApply(response({ reset: true, alarm_at_iso: null }));
      await new Promise((r) => setTimeout(r, 20));
    });
    // No new_anchor in the report → the previewed new_anchor.
    await vi.waitFor(() => expect(session.get(AUTO_PLACE_SUPPRESSED_KEY)).toBe(String(DEFAULT_ANCHOR)));
  });

  for (const [code, text] of [
    ['in_flight', 'already in flight'],
    ['stale_anchor', 'changed since this page loaded'],
  ] as const) {
    it(`a 409 ${code} on Reset is shown in role=alert, re-reads the story and does not suppress`, async () => {
      const calls = await openTab((url, _method, body) => {
        if (url === STORY_URL) return response(story());
        if ((body as { dry_run: boolean }).dry_run) return response(RESET_PREVIEW);
        return response({ detail: { error: code, message: 'server says no' } }, 409);
      });
      await click(q('workshop-reset-preview-btn'));
      await vi.waitFor(() => expect(btn('workshop-reset-btn').disabled).toBe(false));
      await click(q('workshop-reset-btn'));
      await vi.waitFor(() => expect(dialogButton('Reset clock')).toBeTruthy());
      await click(dialogButton('Reset clock'));
      await vi.waitFor(() => expect(q('workshop-reset-error')).toBeTruthy());
      expect(q('workshop-reset-error')!.getAttribute('role')).toBe('alert');
      expect(q('workshop-reset-error')!.textContent).toContain(text);
      expect(q('workshop-reset-error')!.textContent).toContain('server says no');
      await vi.waitFor(() => expect(storyGets(calls)).toBe(2));
      expect(q('workshop-reset-done')).toBeNull();
      expect(session.has(AUTO_PLACE_SUPPRESSED_KEY)).toBe(false);
    });
  }

  it('a 409 on the reset preview is shown and re-reads the story; Reset stays disabled', async () => {
    const calls = await openTab((url) => {
      if (url === STORY_URL) return response(story());
      return response({ detail: { error: 'in_flight', message: 'busy' } }, 409);
    });
    await click(q('workshop-reset-preview-btn'));
    await vi.waitFor(() => expect(q('workshop-reset-error')).toBeTruthy());
    await vi.waitFor(() => expect(storyGets(calls)).toBe(2));
    expect(btn('workshop-reset-btn').disabled).toBe(true);
  });
});
