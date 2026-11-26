// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tests — header plant-clock chip (ADR-0091) against a mocked fetch, no
 * network: hidden on 404 / 403 / frozen / loading / non-admin; each phase
 * label for the walk-2 fixture (anchor 1790737810, now 08:10:23Z: degrading,
 * alarm in 25 min); the local 30 s countdown decrements and floors at "now";
 * it re-syncs on refetch; the chip and the admin tab share ONE GET (D7);
 * a transient refetch failure keeps the last good chip (a 403/404 hides);
 * the ticking text is aria-live="off" and only a phase change is announced;
 * read-only (no button, no click action). */
// @vitest-environment jsdom
import { QueryClientProvider } from '@tanstack/react-query';
import { act, type ReactNode } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { createQueryClient } from '../api/query-client';
import { queryKeys } from '../api/query-keys';
import type { PhaseTimeline, StoryClock, StoryPhase, WorkshopStory } from '../api/types/workshop';
import { autoPlaceStore } from '../features/workshop/auto-place';
import { AuthProvider } from '../contexts/AuthContext';
import { t } from '../lib/i18n';
import { WorkshopTabButton, WorkshopTabSection } from '../pages/admin/tabs/workshop/WorkshopTabGate';
import PlantClockChip, { CHIP_TICK_MS } from '../pages/app/PlantClockChip';
import {
  chipAnnouncement,
  chipMinutes,
  chipServerSeconds,
  chipSourceTitle,
  chipState,
  chipText,
  chipTitle,
} from '../pages/app/plant-clock-logic';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const STORY_URL = '/api/admin/workshop/story';
const ANCHOR = 1790737810;
const NOW_ISO = '2026-09-30T08:10:23Z';

const TIMELINE: PhaseTimeline = {
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
};

/** The walk-2 live story at 08:10:23Z: degrading, alarm at 08:35:00 (1477 s);
 * a PLACED clock unless `clock` overrides the provenance fields. */
function liveStory(timeline: Partial<PhaseTimeline> = {}, clock: Partial<StoryClock> = {}): WorkshopStory {
  const phase_timeline = { ...TIMELINE, ...timeline };
  return {
    story: 'live',
    generated_at: '2026-09-30T08:10:23+00:00',
    clock: {
      phase: phase_timeline.current,
      anchor_source: 'placed',
      anchor_epoch: ANCHOR,
      anchor_at: '2026-09-30T02:30:10+00:00',
      bpfo_alarm_at: '2026-09-30T08:35:00+00:00',
      telemetry: { anchor: ANCHOR, cursor_epoch: 1790756700, backfill_complete: true },
      phase_timeline,
      ...clock,
    },
    warnings: [],
  };
}

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

type Handler = (url: string, n: number) => Response | Promise<Response>;

/** Stub fetch; `n` is the 1-based count of story GETs so far. Returns the URL log. */
function stubFetch(handler: Handler): string[] {
  const calls: string[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      calls.push(url);
      return handler(url, calls.filter((u) => u === STORY_URL).length);
    }),
  );
  return calls;
}

const never = () => new Promise<Response>(() => undefined);
const storyGets = (calls: string[]) => calls.filter((u) => u === STORY_URL).length;

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval', 'Date'] });
  vi.setSystemTime(new Date(NOW_ISO));
  autoPlaceStore.set('idle');
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

async function render(children: ReactNode = <PlantClockChip />, isAdmin = true) {
  const client = createQueryClient();
  client.setQueryDefaults(queryKeys.admin.workshopStory(), { retryDelay: 0 });
  client.setQueryData(queryKeys.authMe(), { user_id: 'admin', email: 'admin@example.com', is_admin: isAdmin });
  client.setQueryData(queryKeys.demoStatus(), { enabled: false, workshop: true });
  await act(async () => {
    root.render(
      <QueryClientProvider client={client}>
        <AuthProvider>{children}</AuthProvider>
      </QueryClientProvider>,
    );
    await Promise.resolve();
  });
  return client;
}

const chip = () => document.querySelector('[data-testid="plant-clock-chip"]') as HTMLElement | null;
/** The visible (ticking, aria-live="off") chip text. */
const chipLabel = () => document.querySelector('[data-testid="plant-clock-chip-text"]')?.textContent ?? null;
const chipAnnounce = () => document.querySelector('[data-testid="plant-clock-chip-announce"]')?.textContent ?? null;

async function settle(ms = 20) {
  await act(async () => {
    await new Promise((r) => setTimeout(r, ms));
  });
}

/** Poll `check` between act-wrapped real-time waits, so every query
 * resolution renders inside act (vi.waitFor alone renders outside it). */
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

async function advance(ms: number) {
  await act(async () => {
    vi.advanceTimersByTime(ms);
    await Promise.resolve();
  });
}

describe('plant-clock-logic', () => {
  it('counts to the alarm while degrading, else to the next phase change', () => {
    expect(chipServerSeconds(TIMELINE)).toBe(1477);
    expect(chipServerSeconds({ ...TIMELINE, current: 'repair', seconds_to_next_phase: 600, seconds_to_alarm: 9000 })).toBe(600);
  });

  it('rounds minutes up and floors at 0', () => {
    expect(chipMinutes(1477)).toBe(25);
    expect(chipMinutes(60)).toBe(1);
    expect(chipMinutes(1)).toBe(1);
    expect(chipMinutes(0)).toBe(0);
    expect(chipMinutes(-5)).toBe(0);
    expect(chipMinutes(Number.NaN)).toBe(0);
  });

  it('announces the phase only (no countdown)', () => {
    expect(chipAnnouncement('bpfo_alarm', t)).toBe('Plant clock phase: Bearing alarm');
    expect(chipAnnouncement('mystery', t)).toBe('Plant clock phase: mystery');
  });

  it('says "now" at zero and falls back to the raw label for an unknown phase', () => {
    expect(chipText('degrading', 0, t)).toBe('Plant clock: degrading, alarm now');
    expect(chipText('mystery', 600, t)).toBe('mystery');
  });

  it('titles the chip with the next alarm time', () => {
    expect(chipTitle(TIMELINE, t, (iso) => `<${iso}>`)).toBe('Next bearing alarm: <2026-09-30T08:35:00+00:00>');
  });
});

describe('PlantClockChip — hidden without a live clock', () => {
  for (const [name, status, body] of [
    ['404 (workshop mode off)', 404, { detail: 'Workshop mode is not enabled' }],
    ['403 (not a steward)', 403, { detail: 'forbidden' }],
    ['frozen story (clock: null)', 200, { ...liveStory(), story: 'frozen', clock: null }],
  ] as const) {
    it(`renders nothing on ${name}, silently and with no retry`, async () => {
      const error = vi.spyOn(console, 'error').mockImplementation(() => undefined);
      const calls = stubFetch(() => response(body, status));
      await render();
      await until(() => expect(storyGets(calls)).toBe(1));
      await settle();
      expect(chip()).toBeNull();
      expect(container.textContent).toBe('');
      expect(storyGets(calls)).toBe(1);
      expect(error).not.toHaveBeenCalled();
    });
  }

  it('renders nothing while loading', async () => {
    const calls = stubFetch(never);
    await render();
    await until(() => expect(storyGets(calls)).toBe(1));
    await settle();
    expect(container.textContent).toBe('');
  });

  it('does not even request the story for a non-admin', async () => {
    const calls = stubFetch(() => response(liveStory()));
    await render(<PlantClockChip />, false);
    await settle();
    expect(calls).toHaveLength(0);
    expect(container.textContent).toBe('');
  });
});

describe('PlantClockChip — live clock', () => {
  it('renders the walk-2 phase text, a status role, data-phase and the alarm tooltip; read-only', async () => {
    stubFetch(() => response(liveStory()));
    await render();
    await until(() => expect(chip()).toBeTruthy());
    const el = chip()!;
    expect(chipLabel()).toBe('Plant clock: degrading, alarm in 25 min');
    expect(el.getAttribute('role')).toBe('status');
    expect(el.getAttribute('aria-live')).toBe('polite');
    // The minute countdown never announces; only the phase line (visually hidden) does.
    const text = el.querySelector('[data-testid="plant-clock-chip-text"]')!;
    expect(text.getAttribute('aria-live')).toBe('off');
    const announce = el.querySelector('[data-testid="plant-clock-chip-announce"]')!;
    expect(announce.getAttribute('aria-live')).toBeNull();
    expect(announce.className).toBe('visually-hidden');
    expect(chipAnnounce()).toBe('Plant clock phase: Degrading');
    expect(el.getAttribute('data-phase')).toBe('degrading');
    expect(el.getAttribute('title')).toMatch(/^Next bearing alarm: .+ · Open the plant clock in Workshop admin$/);
    // ADR-0095: the one affordance is a plain link into the admin Workshop tab,
    // where the clock is explained and can be moved; nothing else is clickable.
    const link = container.querySelector('[data-testid="plant-clock-chip-link"]') as HTMLAnchorElement;
    expect(link.tagName).toBe('A');
    expect(link.getAttribute('href')).toBe('/admin.html#tab-workshop');
    expect(link.getAttribute('aria-label')).toBe('Plant clock: degrading, alarm in 25 min. Open the plant clock in Workshop admin');
    expect(link.getAttribute('data-live')).toBe('false');
    expect(link.contains(el)).toBe(true);
    expect(container.querySelectorAll('button, a, [onclick], [tabindex]').length).toBe(1);
  });

  it('marks the link data-live when the alarm was moved without rewriting the history', async () => {
    stubFetch(() => response(liveStory({}, { live_anchor_source: 'placed-live', live_anchor_epoch: 1 })));
    await render();
    await vi.waitFor(() => expect(container.querySelector('[data-testid="plant-clock-chip-link"]')).toBeTruthy());
    expect(container.querySelector('[data-testid="plant-clock-chip-link"]')!.getAttribute('data-live')).toBe('true');
  });

  for (const [phase, text] of [
    ['bpfo_alarm', 'Plant clock: bearing alarm, failure in 10 min'],
    ['repair', 'Plant clock: repair, healthy in 10 min'],
    ['healthy', 'Plant clock: healthy, degrading in 10 min'],
  ] as const) {
    it(`labels the ${phase} phase`, async () => {
      stubFetch(() =>
        response(liveStory({ current: phase as StoryPhase, seconds_to_next_phase: 600, alarm_active: phase === 'bpfo_alarm' })),
      );
      await render();
      await until(() => expect(chip()).toBeTruthy());
      expect(chipLabel()).toBe(text);
      expect(chip()!.getAttribute('data-phase')).toBe(phase);
    });
  }

  it('ticks down locally every 30 s and never goes below "now"', async () => {
    // Later refetches hang, so only the local tick moves the countdown.
    stubFetch((_url, n) => (n === 1 ? response(liveStory({ seconds_to_alarm: 95, seconds_to_next_phase: 95 })) : never()));
    await render();
    await until(() => expect(chip()).toBeTruthy());
    expect(chipLabel()).toBe('Plant clock: degrading, alarm in 2 min');
    await advance(CHIP_TICK_MS); // 65 s left
    expect(chipLabel()).toBe('Plant clock: degrading, alarm in 2 min');
    await advance(CHIP_TICK_MS); // 35 s left
    expect(chipLabel()).toBe('Plant clock: degrading, alarm in 1 min');
    await advance(CHIP_TICK_MS * 2); // floored at 0
    expect(chipLabel()).toBe('Plant clock: degrading, alarm now');
    await advance(CHIP_TICK_MS * 4);
    expect(chipLabel()).toBe('Plant clock: degrading, alarm now');
    // The live-region text never changed with the tick.
    expect(chipAnnounce()).toBe('Plant clock phase: Degrading');
  });

  it('changes the announced text only when the phase changes', async () => {
    const calls = stubFetch((_url, n) => {
      if (n === 1) return response(liveStory());
      if (n === 2) return response(liveStory({ seconds_to_alarm: 600, seconds_to_next_phase: 600 }));
      if (n === 3) return response(liveStory({ current: 'bpfo_alarm', seconds_to_next_phase: 3000, alarm_active: true }));
      return never();
    });
    await render();
    await until(() => expect(chip()).toBeTruthy());
    expect(chipAnnounce()).toBe('Plant clock phase: Degrading');
    await advance(60_000);
    await until(() => expect(storyGets(calls)).toBe(2));
    await until(() => expect(chipLabel()).toBe('Plant clock: degrading, alarm in 10 min'));
    expect(chipAnnounce()).toBe('Plant clock phase: Degrading');
    await advance(60_000);
    await until(() => expect(storyGets(calls)).toBe(3));
    await until(() => expect(chipAnnounce()).toBe('Plant clock phase: Bearing alarm'));
    expect(chipLabel()).toBe('Plant clock: bearing alarm, failure in 50 min');
  });

  it('keeps the last good chip on a transient refetch failure; a 404 hides it', async () => {
    let status = 200;
    const calls = stubFetch(() => (status === 200 ? response(liveStory()) : response({ detail: 'x' }, status)));
    const client = await render();
    await until(() => expect(chip()).toBeTruthy());
    status = 502;
    await act(async () => {
      await client.refetchQueries({ queryKey: queryKeys.admin.workshopStory() });
    });
    await until(() => expect(storyGets(calls)).toBe(4)); // refetch + 2 retries
    await settle();
    expect(client.getQueryState(queryKeys.admin.workshopStory())?.status).toBe('error');
    expect(chipLabel()).toBe('Plant clock: degrading, alarm in 25 min');
    status = 404;
    await act(async () => {
      await client.refetchQueries({ queryKey: queryKeys.admin.workshopStory() });
    });
    await until(() => expect(chip()).toBeNull());
  });

  it('re-syncs to the server countdown on the 60 s refetch', async () => {
    const calls = stubFetch((_url, n) => {
      if (n === 1) return response(liveStory());
      if (n === 2) return response(liveStory({ seconds_to_alarm: 600, seconds_to_next_phase: 600 }));
      return never();
    });
    await render();
    await until(() => expect(chip()).toBeTruthy());
    expect(chipLabel()).toBe('Plant clock: degrading, alarm in 25 min');
    await advance(CHIP_TICK_MS); // local tick: 1447 s → still 25
    expect(chipLabel()).toBe('Plant clock: degrading, alarm in 25 min');
    await advance(CHIP_TICK_MS); // 60 s poll → the server says 600 s
    await until(() => expect(storyGets(calls)).toBe(2));
    await until(() => expect(chipLabel()).toBe('Plant clock: degrading, alarm in 10 min'));
    await advance(CHIP_TICK_MS); // ticks from the NEW fetch time: 570 s → 10
    expect(chipLabel()).toBe('Plant clock: degrading, alarm in 10 min');
  });

  it('shares ONE story GET with the admin Workshop tab (same query key)', async () => {
    const calls = stubFetch((_url, n) => (n === 1 ? response(liveStory()) : never()));
    await render(
      <>
        <PlantClockChip />
        <WorkshopTabButton active onActivate={() => undefined} />
        <WorkshopTabSection active />
      </>,
    );
    await settle(50); // inside act: the gate's mount effect settles without warnings
    expect(chip()).toBeTruthy();
    expect(document.querySelector('[data-testid="workshop-tab"]')).toBeTruthy();
    expect(storyGets(calls)).toBe(1);
  });
});

describe('PlantClockChip — provenance states (Design C)', () => {
  it('chipState: placed always wins; starting/failed only over a default anchor; unknown otherwise', () => {
    expect(chipState({ anchor_source: 'placed' }, 'failed')).toBe('placed');
    expect(chipState({ anchor_source: 'packaged' }, 'idle')).toBe('default');
    expect(chipState({ anchor_source: 'deploy' }, 'placed')).toBe('default');
    expect(chipState({ anchor_source: 'packaged' }, 'starting')).toBe('starting');
    expect(chipState({ anchor_source: 'deploy' }, 'failed')).toBe('failed');
    expect(chipState({ anchor_source: null }, 'idle')).toBe('unknown');
    // A stored live-only move (the keep-history card) is a placement, whatever the history's source.
    expect(chipState({ anchor_source: 'packaged', live_anchor_source: 'placed-live' }, 'idle')).toBe('placed');
    expect(chipState({ anchor_source: 'deploy', live_anchor_source: 'placed-live' }, 'failed')).toBe('placed');
    expect(chipState({ anchor_source: 'packaged', live_anchor_source: null }, 'idle')).toBe('default');
  });

  it('chipText prefixes "(default)" for a default anchor, including the "now" form', () => {
    expect(chipText('degrading', 78 * 60, t, true)).toBe('Plant clock (default): degrading, alarm in 78 min');
    expect(chipText('repair', 0, t, true)).toBe('Plant clock (default): repair, healthy now');
    expect(chipText('degrading', 78 * 60, t)).toBe('Plant clock: degrading, alarm in 78 min');
  });

  it('chipSourceTitle names the anchor source', () => {
    expect(chipSourceTitle(TIMELINE, 'packaged', t, (iso) => `<${iso}>`)).toBe(
      'Next bearing alarm: <2026-09-30T08:35:00+00:00> · Clock source: packaged default',
    );
  });

  it('placed: "Plant clock: degrading, alarm in 78 min", data-state=placed, data-source=placed, source in title', async () => {
    stubFetch(() => response(liveStory({ seconds_to_alarm: 78 * 60, seconds_to_next_phase: 78 * 60 })));
    await render();
    await until(() => expect(chip()).toBeTruthy());
    expect(chipLabel()).toBe('Plant clock: degrading, alarm in 78 min');
    expect(chip()!.getAttribute('data-state')).toBe('placed');
    expect(chip()!.getAttribute('data-source')).toBe('placed');
    expect(chip()!.getAttribute('title')).toMatch(/· Clock source: placed · Open the plant clock in Workshop admin$/);
  });

  for (const source of ['packaged', 'deploy'] as const) {
    it(`a live-only move on a ${source} history reads as placed: no "(default)", source placed in the title`, async () => {
      stubFetch(() =>
        response(liveStory({}, { anchor_source: source, live_anchor_source: 'placed-live', live_anchor_epoch: 1 })),
      );
      await render();
      await until(() => expect(chip()).toBeTruthy());
      expect(chipLabel()).toBe('Plant clock: degrading, alarm in 25 min');
      expect(chip()!.getAttribute('data-state')).toBe('placed');
      expect(chip()!.getAttribute('data-source')).toBe(source);
      expect(chip()!.getAttribute('title')).toMatch(/· Clock source: placed · Open the plant clock in Workshop admin$/);
    });
  }

  for (const source of ['packaged', 'deploy'] as const) {
    it(`${source}: "Plant clock (default): …", data-state=default, data-source=${source}`, async () => {
      stubFetch(() => response(liveStory({}, { anchor_source: source })));
      await render();
      await until(() => expect(chip()).toBeTruthy());
      expect(chipLabel()).toBe('Plant clock (default): degrading, alarm in 25 min');
      expect(chip()!.getAttribute('data-state')).toBe('default');
      expect(chip()!.getAttribute('data-source')).toBe(source);
      expect(chip()!.getAttribute('role')).toBe('status');
      const label = source === 'packaged' ? 'packaged default' : 'set at deploy';
      expect(chip()!.getAttribute('title')).toMatch(new RegExp(`· Clock source: ${label} · Open the plant clock in Workshop admin$`));
    });
  }

  it('starting: "Plant clock: starting…" while the auto-place is in flight', async () => {
    stubFetch(() => response(liveStory({}, { anchor_source: 'packaged' })));
    await render();
    await until(() => expect(chip()).toBeTruthy());
    await act(async () => autoPlaceStore.set('starting'));
    expect(chipLabel()).toBe('Plant clock: starting…');
    expect(chip()!.getAttribute('data-state')).toBe('starting');
    expect(chip()!.getAttribute('role')).toBe('status');
  });

  it('failed: "Plant clock: not started. Ask for help." in ONE role=alert, data-state=failed', async () => {
    stubFetch(() => response(liveStory({}, { anchor_source: 'packaged' })));
    await render();
    await until(() => expect(chip()).toBeTruthy());
    await act(async () => autoPlaceStore.set('failed'));
    const el = chip()!;
    expect(chipLabel()).toBe('Plant clock: not started. Ask for help.');
    expect(el.getAttribute('data-state')).toBe('failed');
    expect(el.getAttribute('data-source')).toBe('packaged');
    expect(el.getAttribute('role')).toBe('alert');
    expect(el.getAttribute('aria-live')).toBeNull();
    expect(document.querySelectorAll('[role="alert"]')).toHaveLength(1);
    expect(chipAnnounce()).toBeNull();
    // The static alert text does not change on the tick (announced once).
    await advance(CHIP_TICK_MS * 2);
    expect(chip()).toBe(el);
    expect(chipLabel()).toBe('Plant clock: not started. Ask for help.');
  });

  it('a placed clock from the server overrides a failed auto-place', async () => {
    stubFetch(() => response(liveStory()));
    await render();
    await act(async () => autoPlaceStore.set('failed'));
    await until(() => expect(chip()).toBeTruthy());
    expect(chip()!.getAttribute('data-state')).toBe('placed');
    expect(chipLabel()).toBe('Plant clock: degrading, alarm in 25 min');
  });
});
