// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tests: the Graph Explorer legend says how many nodes the overview leaves
 * out. The overview never draws Field, Property or OnboardingLog nodes; the
 * legend counted only drawn nodes, so it read "Field 0" on a graph holding
 * thousands. /api/neptune/graph now sends those labels' real counts as
 * `not_drawn`, and the legend shows them, marked "not drawn", in the overview
 * only: a neighborhood view still counts exactly what it draws. */
// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { KgTypeLegend } from '../views/architecture/KgTypeLegend';
import {
  kgNotDrawnStore,
  registerKgRenderer,
  resetKgViewState,
  returnToFilteredView,
  setCurrentView,
  type KgDataset,
} from '../views/architecture/kg-view-store';
import { destroyNeptuneGraph, loadNeptuneGraph } from '../views/architecture/neptune-graph';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const STATIC_NODE_TYPES = [
  { value: 'System', label: 'System' },
  { value: 'Field', label: 'Field' },
  { value: 'OnboardingLog', label: 'Onboarding Log' },
];

const OVERVIEW: KgDataset = {
  nodes: [
    { id: 's1', label: 'Sys One', type: 'System' },
    { id: 's2', label: 'Sys Two', type: 'System' },
  ],
  edges: [],
  notDrawn: { Field: 1922, OnboardingLog: 16, Property: 840 },
};

const NEIGHBORHOOD: KgDataset = {
  nodes: [
    { id: 'e1', label: 'Table One', type: 'SchemaEntry' },
    { id: 'f1', label: 'Field One', type: 'Field' },
  ],
  edges: [{ source: 'e1', target: 'f1', label: 'HAS_FIELD' }],
};

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  resetKgViewState();
  registerKgRenderer(vi.fn());
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  resetKgViewState();
  vi.unstubAllGlobals();
});

function renderLegend() {
  act(() => root.render(<KgTypeLegend kind="nodes" staticTypes={STATIC_NODE_TYPES} />));
}

interface Row {
  count: string;
  note: string | null;
  muted: boolean;
}

function rowElements(): Record<string, Element> {
  const out: Record<string, Element> = {};
  for (const row of container.querySelectorAll('.kg-checkbox-item')) {
    out[row.querySelector('span')!.textContent!] = row;
  }
  return out;
}

function rows(): Record<string, Row> {
  const out: Record<string, Row> = {};
  for (const [label, row] of Object.entries(rowElements())) {
    out[label] = {
      count: row.querySelector('.kg-type-count')!.textContent!,
      note: row.querySelector('.kg-type-note')?.textContent ?? null,
      muted: row.classList.contains('kg-type-muted'),
    };
  }
  return out;
}

describe('KgTypeLegend: labels the overview leaves out', () => {
  it('shows their real counts, marked not drawn, in the overview', () => {
    act(() => setCurrentView(OVERVIEW, { mode: 'filtered' }));
    renderLegend();
    expect(rows()).toEqual({
      System: { count: '2', note: null, muted: false },
      Field: { count: '1922', note: 'not drawn', muted: true },
      'Onboarding Log': { count: '16', note: 'not drawn', muted: true },
      Property: { count: '840', note: 'not drawn', muted: true },
    });
    expect(rowElements().Field.querySelector('.kg-type-count')!.getAttribute('title')).toBe(
      '1922 in the graph, not drawn in this view',
    );
    expect(rowElements().System.querySelector('.kg-type-count')!.hasAttribute('title')).toBe(false);
  });

  it('never marks a type it draws as not drawn', () => {
    act(() =>
      setCurrentView({ ...OVERVIEW, notDrawn: { System: 7, Field: 1922 } }, { mode: 'filtered' }),
    );
    renderLegend();
    expect(rows().System).toEqual({ count: '2', note: null, muted: false });
    expect(rows().Field).toEqual({ count: '1922', note: 'not drawn', muted: true });
  });

  it('keeps node counts out of the edge legend', () => {
    act(() => setCurrentView(OVERVIEW, { mode: 'filtered' }));
    act(() =>
      root.render(<KgTypeLegend kind="edges" staticTypes={[{ value: 'HAS_FIELD', label: 'Has Field' }]} />),
    );
    expect(rows()).toEqual({ 'Has Field': { count: '0', note: null, muted: true } });
  });

  it('forgets the counts when the view state is reset', () => {
    act(() => setCurrentView(OVERVIEW, { mode: 'filtered' }));
    act(() => resetKgViewState());
    expect(kgNotDrawnStore.get()).toEqual({});
  });

  it('counts only what a neighborhood draws, and the overview counts come back on return', () => {
    act(() => setCurrentView(OVERVIEW, { mode: 'filtered' }));
    renderLegend();
    act(() =>
      setCurrentView(NEIGHBORHOOD, { mode: 'neighborhood', nodeId: 'e1', nodeLabel: 'Table One', hops: 1 }),
    );
    expect(rows()).toEqual({
      System: { count: '0', note: null, muted: true },
      Field: { count: '1', note: null, muted: false },
      'Onboarding Log': { count: '0', note: null, muted: true },
      SchemaEntry: { count: '1', note: null, muted: false },
    });
    act(() => returnToFilteredView());
    expect(rows().Field).toEqual({ count: '1922', note: 'not drawn', muted: true });
  });
});

describe('loadNeptuneGraph: not_drawn reaches the legend', () => {
  it('publishes the response not_drawn counts with the overview', async () => {
    destroyNeptuneGraph();
    document.body.replaceChildren(container);
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          nodes: [{ id: 'n1', label: 'ERP', type: 'System', props: {} }],
          edges: [],
          not_drawn: { Field: 1922, OnboardingLog: 16 },
        }),
      } as unknown as Response),
    );
    await loadNeptuneGraph();
    expect(kgNotDrawnStore.get()).toEqual({ Field: 1922, OnboardingLog: 16 });
    destroyNeptuneGraph();
  });

  it('an older server without not_drawn reads every left-out count as unavailable, never 0', async () => {
    // Rolling deploy: a new bundle can reach a task that predates not_drawn.
    destroyNeptuneGraph();
    document.body.replaceChildren(container);
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          nodes: [{ id: 'n1', label: 'ERP', type: 'System', props: {} }],
          edges: [],
        }),
      } as unknown as Response),
    );
    await loadNeptuneGraph();
    expect(kgNotDrawnStore.get()).toEqual({ Field: null, Property: null, OnboardingLog: null });
    destroyNeptuneGraph();
  });
});
