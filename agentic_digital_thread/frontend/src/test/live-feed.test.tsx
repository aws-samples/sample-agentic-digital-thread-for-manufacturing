// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tests — Task 1C live events lifecycle truth: the LiveFeed panel renders
 * HONEST empty states from the store's iotStats slice (not configured /
 * started-but-not-connected with last_error tooltip / connected-no-publishers),
 * the hardcoded "Historian" src-badge is gone (presence-driven "IoT Core"
 * badge only when messages have been received), and the stats line renders
 * from the store instead of a direct #mqtt-stats DOM write. */
// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { IotStats } from '../api/types/dashboard';
import { liveFeedEmptyState, pollLiveFeed } from '../views/dashboard/feed';
import { LiveFeed } from '../views/dashboard/LiveFeed';
import { dashboardStore, makeFeedLine, seedFeed, setIotStats } from '../views/dashboard/store';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => {
    seedFeed([]);
    setIotStats(null);
  });
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

function render() {
  act(() => {
    root.render(<LiveFeed />);
  });
}

const stats = (extra: Partial<IotStats> = {}): IotStats => ({
  enabled: true,
  endpoint_configured: true,
  started: true,
  connected: true,
  last_error: null,
  total_messages: 0,
  unique_topics: 0,
  buffer_size: 0,
  ...extra,
});

// ── Pure helper — the three honest branches ──────────────────────────────────

describe('liveFeedEmptyState', () => {
  it('null stats (never loaded) → null (no claim made)', () => {
    expect(liveFeedEmptyState(null)).toBeNull();
    expect(liveFeedEmptyState(undefined)).toBeNull();
  });

  it('branch 1 — not enabled or no endpoint → "not configured"', () => {
    expect(liveFeedEmptyState(stats({ enabled: false }))?.text).toMatch(/not configured/i);
    expect(liveFeedEmptyState(stats({ endpoint_configured: false }))?.text).toMatch(
      /not configured/i,
    );
  });

  it('branch 2 — started but not connected → "not connected" + last_error tooltip', () => {
    const s = stats({
      connected: false,
      last_error: { message: 'connect refused', timestamp: '2026-07-15T00:00:00Z' },
    });
    const es = liveFeedEmptyState(s);
    expect(es?.text).toMatch(/not connected to iot core/i);
    expect(es?.tooltip).toContain('connect refused');
    expect(es?.tooltip).toContain('2026-07-15T00:00:00Z');
    // No last_error → no tooltip, same text.
    expect(liveFeedEmptyState(stats({ connected: false }))?.tooltip).toBeUndefined();
  });

  it('branch 3 — connected with zero messages → nothing publishing yet, vendor-neutral', () => {
    // True with or without the HighByte hub (package.py --highbyte off): the
    // copy names no OT product or pipeline, only IoT Core.
    const es = liveFeedEmptyState(stats({ total_messages: 0 }));
    expect(es?.text).toBe('Connected, but nothing is publishing to IoT Core yet.');
    expect(es?.text).not.toMatch(/highbyte|opc ua|pipeline/i);
  });

  it('connected with traffic → null (events render instead)', () => {
    expect(liveFeedEmptyState(stats({ total_messages: 12 }))).toBeNull();
  });
});

// ── Component — empty states, badge, stats line ──────────────────────────────

describe('<LiveFeed/> honest empty states', () => {
  it('renders "not configured" inside #live-feed when ingestion is disabled', () => {
    act(() => setIotStats(stats({ enabled: false })));
    render();
    const feedEl = container.querySelector('#live-feed');
    expect(feedEl?.textContent).toMatch(/not configured/i);
  });

  it('renders "not connected" with the last_error tooltip', () => {
    act(() =>
      setIotStats(stats({ connected: false, last_error: { message: 'auth denied' } })),
    );
    render();
    const empty = container.querySelector('#live-feed .feed-empty');
    expect(empty?.textContent).toMatch(/not connected to iot core/i);
    expect(empty?.getAttribute('title')).toContain('auth denied');
  });

  it('renders "nothing is publishing" when connected with zero messages', () => {
    act(() => setIotStats(stats({ total_messages: 0 })));
    render();
    const text = container.querySelector('#live-feed')?.textContent ?? '';
    expect(text).toMatch(/nothing is publishing to iot core yet/i);
    expect(text).not.toMatch(/highbyte/i);
  });

  it('feed lines take precedence over the empty state', () => {
    act(() => {
      setIotStats(stats({ connected: false }));
      seedFeed([makeFeedLine('10:00:00', 'LineA/State', 'running')]);
    });
    render();
    expect(container.querySelector('#live-feed .feed-line')).not.toBeNull();
    expect(container.querySelector('#live-feed .feed-empty')).toBeNull();
  });

  it('stats never loaded → no empty-state element (CSS :empty fallback owns it)', () => {
    render();
    expect(container.querySelector('#live-feed')?.childElementCount).toBe(0);
  });
});

describe('<LiveFeed/> empty-env visibility gate', () => {
  // The panel self-hides until event data actually flows. Gate is
  // display:none — NEVER unmount — because #live-feed-panel / #live-feed are
  // CONTRACTUAL ids (e2e test_dashboard_layout.py + the Task 1C tests above
  // assert on the attached nodes).
  const panel = () => container.querySelector<HTMLElement>('#live-feed-panel');

  it('hidden but ATTACHED when no lines and stats never loaded (initial empty env)', () => {
    render();
    expect(panel()).not.toBeNull();
    expect(panel()!.style.display).toBe('none');
    expect(container.querySelector('#live-feed')).not.toBeNull();
  });

  it('hidden when stats loaded but zero messages seen (not configured / no publishers)', () => {
    act(() => setIotStats(stats({ enabled: false })));
    render();
    expect(panel()!.style.display).toBe('none');

    act(() => setIotStats(stats({ total_messages: 0 })));
    expect(panel()!.style.display).toBe('none');
  });

  it('visible once feed lines exist', () => {
    act(() => seedFeed([makeFeedLine('10:00:00', 'LineA/State', 'running')]));
    render();
    expect(panel()!.style.display).toBe('');
  });

  it('started-then-quiet stays visible: messages HAVE flowed, current window empty', () => {
    act(() => setIotStats(stats({ total_messages: 42 })));
    render();
    expect(panel()!.style.display).toBe('');
    // The honest-empty-state area is the CSS :empty fallback here (traffic
    // branch of liveFeedEmptyState returns null).
    expect(container.querySelector('#live-feed .feed-line')).toBeNull();
  });
});

describe('<LiveFeed/> presence-driven badge + stats line', () => {
  it('the hardcoded "Historian" badge is gone in every state', () => {
    render();
    expect(container.textContent).not.toContain('Historian');
    act(() => setIotStats(stats({ total_messages: 3 })));
    expect(container.textContent).not.toContain('Historian');
  });

  it('badge hidden when nothing publishes, shown once messages arrive', () => {
    act(() => setIotStats(stats({ total_messages: 0 })));
    render();
    expect(container.querySelector('.src-badge')).toBeNull();
    act(() => setIotStats(stats({ total_messages: 42, unique_topics: 5 })));
    const badge = container.querySelector('.src-badge');
    expect(badge?.textContent).toBe('IoT Core');
  });

  it('#mqtt-stats renders counts from the store slice', () => {
    act(() => setIotStats(stats({ total_messages: 1234, unique_topics: 7 })));
    render();
    const se = container.querySelector('#mqtt-stats');
    expect(se?.textContent).toContain('1,234 messages');
    expect(se?.textContent).toContain('7 topics');
  });

  it('no stats yet → no #mqtt-stats element', () => {
    render();
    expect(container.querySelector('#mqtt-stats')).toBeNull();
  });
});

// ── pollLiveFeed — /api/iot/stats sr.ok gate ──────────────────────────────
// A non-2xx JSON error envelope must never be stored as IotStats: it would
// render a misleading "not configured" empty state. The fetch now throws on
// !sr.ok inside the existing try, so the slice keeps the last good stats.

describe('pollLiveFeed /api/iot/stats sr.ok gate', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  function stubFetch(statsResp: { ok: boolean; status?: number; body: unknown }) {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: RequestInfo | URL) => {
        const u = String(url);
        if (u.startsWith('/api/iot/stats')) {
          return {
            ok: statsResp.ok,
            status: statsResp.status ?? (statsResp.ok ? 200 : 500),
            json: async () => statsResp.body,
          } as Response;
        }
        // /api/iot/recent — empty list, irrelevant to these cases.
        return { ok: true, status: 200, json: async () => ({ messages: [] }) } as Response;
      }),
    );
  }

  it('stores stats on a 2xx response', async () => {
    stubFetch({ ok: true, body: stats({ total_messages: 9, unique_topics: 2 }) });
    await pollLiveFeed();
    expect(dashboardStore.get().iotStats?.total_messages).toBe(9);
  });

  it('a JSON error envelope on !ok is NOT stored — last good stats kept', async () => {
    act(() => setIotStats(stats({ total_messages: 7 })));
    stubFetch({ ok: false, status: 503, body: { error: 'upstream unavailable' } });
    await pollLiveFeed();
    const s = dashboardStore.get().iotStats;
    expect(s?.total_messages).toBe(7); // last good stats survive
    expect(s && ('error' in (s as object))).toBe(false); // envelope never stored
  });

  it('!ok with no prior stats leaves the slice null (no misleading "not configured")', async () => {
    stubFetch({ ok: false, status: 500, body: { error: 'boom' } });
    await pollLiveFeed();
    expect(dashboardStore.get().iotStats).toBeNull();
  });
});
