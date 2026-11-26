// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tests — KgTypeLegend + KgViewBreadcrumb (UI improvement D, panel UI).
 * Real kg-view-store, spy renderer, NO cytoscape.
 *
 * Pins (each names the mutation it kills):
 * 1. Filtered view: every legend row shows the fixture's exact count; a
 *    static type absent from the data renders 0 + muted (row stays); a type
 *    in the data but NOT in the static list still gets a row — kills
 *    dropping zero-count rows and kills a static-only legend that cannot
 *    describe a data-driven view.
 * 2. After focusing a neighborhood (strict subset), every row's count equals
 *    the SUBSET's counts — the headline count-drift kill (counts still
 *    reading the full graph after focus).
 * 3. Toggling a type inside a neighborhood re-renders with the type hidden,
 *    stays in neighborhood mode and keeps the breadcrumb text — kills a
 *    toggle silently resetting to the filtered view.
 * 4. Breadcrumb reflects the state AND the back button restores the filtered
 *    view (renderer receives the full fixture again) — kills a breadcrumb
 *    that displays state without restoring it (dead back button) and kills
 *    one stuck on filtered. */
// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { KgTypeLegend } from '../views/architecture/KgTypeLegend';
import { KgViewBreadcrumb } from '../views/architecture/KgViewBreadcrumb';
import {
  registerKgRenderer,
  resetKgViewState,
  setCurrentView,
  type KgDataset,
  type KgViewState,
} from '../views/architecture/kg-view-store';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const STATIC_NODE_TYPES = [
  { value: 'System', label: 'System' },
  { value: 'Concept', label: 'Concept' },
  { value: 'Field', label: 'Field' },
];
const STATIC_EDGE_TYPES = [
  { value: 'LINKS_CONCEPT', label: 'Links Concept' },
  { value: 'HAS_FIELD', label: 'Has Field' },
];

const FULL: KgDataset = {
  nodes: [
    { id: 's1', label: 'Sys One', type: 'System' },
    { id: 's2', label: 'Sys Two', type: 'System' },
    { id: 'c1', label: 'Concept One', type: 'Concept' },
    // Data-driven type NOT in the static list — must still get a row.
    { id: 'w1', label: 'Wild One', type: 'WildType' },
  ],
  edges: [
    { source: 's1', target: 'c1', label: 'LINKS_CONCEPT' },
    { source: 's2', target: 'c1', label: 'LINKS_CONCEPT' },
  ],
};

/** Strict subset — the neighborhood of c1. */
const NEIGHBORHOOD: KgDataset = {
  nodes: [
    { id: 'c1', label: 'Concept One', type: 'Concept' },
    { id: 's1', label: 'Sys One', type: 'System' },
  ],
  edges: [{ source: 's1', target: 'c1', label: 'LINKS_CONCEPT' }],
};

const FOCUS = {
  mode: 'neighborhood',
  nodeId: 'c1',
  nodeLabel: 'Concept One',
  hops: 1,
} as const;

const makeRenderer = () => vi.fn((_visible: KgDataset, _view: KgViewState) => {});

let container: HTMLDivElement;
let root: Root;
let renderer: ReturnType<typeof makeRenderer>;

beforeEach(() => {
  resetKgViewState();
  renderer = makeRenderer();
  registerKgRenderer(renderer);
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  resetKgViewState();
});

function renderPanel() {
  act(() =>
    root.render(
      <>
        <KgViewBreadcrumb />
        <div id="node-legend">
          <KgTypeLegend kind="nodes" staticTypes={STATIC_NODE_TYPES} />
        </div>
        <div id="edge-legend">
          <KgTypeLegend kind="edges" staticTypes={STATIC_EDGE_TYPES} />
        </div>
      </>,
    ),
  );
}

/** label → count from the rendered rows of one legend host. */
function countsOf(hostId: string): Record<string, string> {
  const rows = container.querySelectorAll(`#${hostId} .kg-checkbox-item`);
  const out: Record<string, string> = {};
  for (const row of rows) {
    out[row.querySelector('span')!.textContent!] =
      row.querySelector('.kg-type-count')!.textContent!;
  }
  return out;
}

describe('KgTypeLegend (D)', () => {
  it('shows exact per-type counts of the filtered view, muted zeros, data-driven rows', () => {
    act(() => setCurrentView(FULL, { mode: 'filtered' }));
    renderPanel();

    expect(countsOf('node-legend')).toEqual({
      System: '2',
      Concept: '1',
      Field: '0', // static type absent from the data — row stays, count 0
      WildType: '1', // data-driven extra row
    });
    expect(countsOf('edge-legend')).toEqual({
      'Links Concept': '2',
      'Has Field': '0',
    });

    const fieldRow = Array.from(
      container.querySelectorAll('#node-legend .kg-checkbox-item'),
    ).find((r) => r.querySelector('span')!.textContent === 'Field')!;
    expect(fieldRow.classList.contains('kg-type-muted')).toBe(true);
    const systemRow = Array.from(
      container.querySelectorAll('#node-legend .kg-checkbox-item'),
    ).find((r) => r.querySelector('span')!.textContent === 'System')!;
    expect(systemRow.classList.contains('kg-type-muted')).toBe(false);
  });

  it('counts follow the view: focusing a neighborhood shows SUBSET counts', () => {
    act(() => setCurrentView(FULL, { mode: 'filtered' }));
    renderPanel();
    act(() => setCurrentView(NEIGHBORHOOD, FOCUS));

    // The headline count-drift kill: counts must be neighborhood-sized.
    expect(countsOf('node-legend')).toEqual({
      System: '1',
      Concept: '1',
      Field: '0',
    });
    expect(countsOf('edge-legend')).toEqual({
      'Links Concept': '1',
      'Has Field': '0',
    });
  });

  it('toggling a type inside a neighborhood hides it WITHOUT leaving focus', () => {
    act(() => setCurrentView(FULL, { mode: 'filtered' }));
    act(() => setCurrentView(NEIGHBORHOOD, FOCUS));
    renderPanel();
    const crumbBefore = container.querySelector('.kg-view-breadcrumb')!.textContent;
    renderer.mockClear();

    const systemBox = Array.from(
      container.querySelectorAll<HTMLInputElement>('#node-legend input[type="checkbox"]'),
    ).find((cb) => cb.value === 'System')!;
    act(() => systemBox.click());

    expect(renderer).toHaveBeenCalledTimes(1);
    const [visible, view] = renderer.mock.calls[0];
    expect(visible.nodes.map((n: { type: string }) => n.type)).toEqual(['Concept']);
    expect(view.mode).toBe('neighborhood');
    expect(container.querySelector('.kg-view-breadcrumb')!.textContent).toBe(crumbBefore);
    expect(systemBox.checked).toBe(false);
  });
});

describe('KgViewBreadcrumb (D)', () => {
  it('describes the filtered view with no back affordance', () => {
    act(() => setCurrentView(FULL, { mode: 'filtered' }));
    renderPanel();
    const crumb = container.querySelector('.kg-view-breadcrumb')!;
    expect(crumb.textContent).toBe('Viewing: all nodes (filtered)');
    expect(crumb.querySelector('.kg-breadcrumb-back')).toBeNull();
  });

  it('describes the neighborhood and the back button RESTORES the filtered view', () => {
    act(() => setCurrentView(FULL, { mode: 'filtered' }));
    act(() => setCurrentView(NEIGHBORHOOD, FOCUS));
    renderPanel();

    const crumb = container.querySelector('.kg-view-breadcrumb')!;
    expect(crumb.textContent).toContain('neighborhood of Concept One');
    expect(crumb.textContent).toContain('1 hop');
    const back = crumb.querySelector<HTMLButtonElement>('.kg-breadcrumb-back')!;
    expect(back).not.toBeNull();
    renderer.mockClear();

    act(() => back.click());

    // State restored AND the renderer received the full fixture again —
    // a breadcrumb that only flips text would fail the renderer assert.
    expect(container.querySelector('.kg-view-breadcrumb')!.textContent).toBe(
      'Viewing: all nodes (filtered)',
    );
    expect(renderer).toHaveBeenCalledTimes(1);
    expect(renderer.mock.calls[0][0]).toEqual(FULL);
    expect(countsOf('node-legend')).toEqual({
      System: '2',
      Concept: '1',
      Field: '0',
      WildType: '1',
    });
  });
});
