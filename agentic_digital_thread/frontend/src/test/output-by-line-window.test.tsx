// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tests — Task 2D: decision-useful windowed Output-by-Line.
 * (1) the output loader posts the WINDOWED metric variant
 *     (actual-quantity-windowed) and stamps the resolve window derived from
 *     per-row lineage onto the stored payload;
 * (2) the panel caption under the title is presence-driven: window '24h' →
 *     'Units produced · last 24h'; window null/absent → 'Units produced · lifetime total' (the honest
 *     no-event-time fallback);
 * (3) all-zero windowed rows stay VISIBLE (zeros with samples are honest —
 *     2B's hasNonDegenerateRows rule, regression-pinned here);
 * (4) the caption is output-panel-only: the sibling count-bar panel
 *     (work-orders) renders NO caption node (2B's panels byte-identical). */
// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { DimensionPayload } from '../api/types/dashboard';
import {
  _resetPanelLoaders,
  loadOutputByLine,
  OutputByLinePanel,
  windowCaption,
  windowFromLineage,
  WorkOrdersByStatusPanel,
} from '../views/dashboard/Panels';
import {
  dashboardStore,
  setOutputByLine,
  setWorkOrders,
} from '../views/dashboard/store';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  setOutputByLine(null);
  setWorkOrders(null);
  _resetPanelLoaders();
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function render(el: React.ReactElement) {
  act(() => {
    root.render(el);
  });
}

// ── Loader: windowed metric id + lineage-derived window stamp ─────────────────

describe('output-by-line loader (Task 2D)', () => {
  it('posts the windowed metric variant and stamps window from lineage', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        status: 'ok',
        rows: [['Line 1', 42]],
        lineage: [{ query_plan: { window: '24h', window_column: 'event_ts' } }],
      }),
    } as unknown as Response);
    vi.stubGlobal('fetch', fetchMock);

    await loadOutputByLine();

    const body = String(
      (fetchMock.mock.calls[0][1] as { body?: unknown }).body,
    );
    expect(body).toContain('"metric_id":"actual-quantity-windowed"');
    expect(body).toContain('"dimension_concept":"production-line"');
    const stored = dashboardStore.get().outputByLine as
      | (DimensionPayload & { window?: string | null })
      | null;
    expect(stored).not.toBeNull();
    expect(stored!.window).toBe('24h');
  });

  it('stamps window null on the honest lifetime fallback (no lineage window)', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          status: 'ok',
          rows: [['Line 1', 42]],
          lineage: [{ query_plan: { window: null } }],
        }),
      } as unknown as Response),
    );
    await loadOutputByLine();
    const stored = dashboardStore.get().outputByLine as
      | (DimensionPayload & { window?: string | null })
      | null;
    expect(stored!.window).toBeNull();
  });
});

// ── Pure helpers ──────────────────────────────────────────────────────────────

describe('windowFromLineage / windowCaption', () => {
  it('derives the token from the first row lineage query_plan', () => {
    expect(windowFromLineage([{ query_plan: { window: '24h' } }])).toBe('24h');
    expect(windowFromLineage([{ query_plan: { window: null } }])).toBeNull();
    expect(windowFromLineage([{ query_plan: {} }])).toBeNull();
    expect(windowFromLineage([{}])).toBeNull();
    expect(windowFromLineage([])).toBeNull();
    expect(windowFromLineage(null)).toBeNull();
    expect(windowFromLineage(undefined)).toBeNull();
    expect(windowFromLineage('not-an-array')).toBeNull();
  });

  it('maps tokens to captions; null → lifetime total', () => {
    expect(windowCaption('24h')).toBe('Last 24 hours');
    expect(windowCaption('1h')).toBe('Last 1 hour');
    expect(windowCaption('7d')).toBe('Last 7 days');
    expect(windowCaption(null)).toBe('Lifetime total');
    expect(windowCaption(undefined)).toBe('Lifetime total');
    // Unrecognized tokens render as-is (presence-driven, never fabricated).
    expect(windowCaption('fortnight')).toBe('Last fortnight');
  });
});

// ── Panel caption rendering ───────────────────────────────────────────────────

const outputFixture = (
  window: string | null,
  rows: unknown[][] = [
    ['Line 1', 120],
    ['Line 2', 80],
  ],
): DimensionPayload & { window?: string | null } => ({
  status: 'ok',
  group_by: 'production-line',
  label: 'Output',
  unit: 'units',
  rows,
  window,
});

describe('OutputByLinePanel caption (Task 2D)', () => {
  it("renders 'Units produced · last 24h' under the title for a 24h-windowed payload", () => {
    setOutputByLine(outputFixture('24h'));
    render(<OutputByLinePanel />);
    const panel = document.getElementById('output-by-line-panel')!;
    expect(panel.style.display).toBe('');
    const caption = panel.querySelector('.dim-bar-window-caption')!;
    expect(caption).not.toBeNull();
    expect(caption.textContent).toBe('Units produced · last 24h');
    // Under the panel title: the caption is the title's next sibling.
    const title = panel.querySelector('.dim-bar-title')!;
    expect(title.nextElementSibling).toBe(caption);
  });

  it("renders 'Units produced · lifetime total' when window is null (honest fallback)", () => {
    setOutputByLine(outputFixture(null));
    render(<OutputByLinePanel />);
    const caption = document.querySelector(
      '#output-by-line-panel .dim-bar-window-caption',
    )!;
    expect(caption.textContent).toBe('Units produced · lifetime total');
  });

  it("renders 'Units produced · lifetime total' for legacy payloads with no window key at all", () => {
    const legacy = outputFixture(null);
    delete (legacy as { window?: unknown }).window;
    setOutputByLine(legacy);
    render(<OutputByLinePanel />);
    expect(
      document.querySelector('#output-by-line-panel .dim-bar-window-caption')!
        .textContent,
    ).toBe('Units produced · lifetime total');
  });

  it('all-zero windowed rows stay VISIBLE with the caption (zeros are honest)', () => {
    setOutputByLine(
      outputFixture('24h', [
        ['Line 1', 0],
        ['Line 2', 0],
      ]),
    );
    render(<OutputByLinePanel />);
    const panel = document.getElementById('output-by-line-panel')!;
    expect(panel.style.display).toBe('');
    expect(panel.querySelector('.dim-bar-window-caption')!.textContent).toBe(
      'Units produced · last 24h',
    );
  });

  it('hidden panel (null / empty / all-null rows) renders no caption node', () => {
    setOutputByLine(null);
    render(<OutputByLinePanel />);
    const panel = document.getElementById('output-by-line-panel')!;
    expect(panel.style.display).toBe('none');
    expect(panel.querySelector('.dim-bar-window-caption')).toBeNull();

    act(() => {
      setOutputByLine(outputFixture('24h', []));
    });
    expect(panel.style.display).toBe('none');

    act(() => {
      setOutputByLine(
        outputFixture('24h', [
          ['Line 1', null],
          ['Line 2', null],
        ]),
      );
    });
    expect(panel.style.display).toBe('none');
  });

  it('sibling count-bar panels render NO caption (2B panels unchanged)', () => {
    setWorkOrders({
      status: 'ok',
      group_by: 'lifecycle-state',
      label: 'Work Orders',
      rows: [['Open', 3]],
    });
    render(<WorkOrdersByStatusPanel />);
    const panel = document.getElementById('work-orders-by-status-panel')!;
    expect(panel.style.display).toBe('');
    expect(panel.querySelector('.dim-bar-window-caption')).toBeNull();
  });
});
