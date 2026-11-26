// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tests — kg-view-store (UI improvement D, coherence core; pure node env,
 * NO cytoscape/DOM).
 *
 * Pins (each names the mutation it kills):
 * 1. countTypes counts the GIVEN dataset — feed a 3-node neighborhood
 *    fixture and assert neighborhood-sized counts — kills counting the full
 *    graph instead of the current view (the count-drift bug, unit level).
 * 2. applyVisibility drops hidden node types AND their edges (dangling-edge
 *    sweep — cytoscape crashes on dangling edges), while hiding an edge
 *    label keeps its endpoint nodes — kills rendering dangling edges and
 *    kills hiding nodes when only an edge label was toggled.
 * 3. setCurrentView publishes counts of the SAME dataset the registered
 *    renderer receives — kills feeding legend and render from different
 *    datasets.
 * 4. toggleType re-renders without changing the view mode — kills a toggle
 *    kicking the user out of neighborhood focus.
 * 5. returnToFilteredView restores the stashed filtered dataset without a
 *    refetch — kills a dead breadcrumb back affordance at the store level. */
import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  applyVisibility,
  countTypes,
  getCurrentViewData,
  kgCountsStore,
  kgHiddenStore,
  kgViewStore,
  registerKgRenderer,
  resetKgViewState,
  returnToFilteredView,
  setCurrentView,
  toggleType,
  type KgDataset,
} from '../views/architecture/kg-view-store';

const FULL: KgDataset = {
  nodes: [
    { id: 's1', label: 'Sys One', type: 'System' },
    { id: 's2', label: 'Sys Two', type: 'System' },
    { id: 'c1', label: 'Concept One', type: 'Concept' },
    { id: 'f1', label: 'Field One', type: 'Field' },
    { id: 'f2', label: 'Field Two', type: 'Field' },
  ],
  edges: [
    { source: 's1', target: 'c1', label: 'LINKS_CONCEPT' },
    { source: 's2', target: 'c1', label: 'LINKS_CONCEPT' },
    { source: 's1', target: 'f1', label: 'HAS_FIELD' },
    { source: 's2', target: 'f2', label: 'HAS_FIELD' },
  ],
};

const NEIGHBORHOOD: KgDataset = {
  nodes: [
    { id: 'c1', label: 'Concept One', type: 'Concept' },
    { id: 's1', label: 'Sys One', type: 'System' },
    { id: 's2', label: 'Sys Two', type: 'System' },
  ],
  edges: [
    { source: 's1', target: 'c1', label: 'LINKS_CONCEPT' },
    { source: 's2', target: 'c1', label: 'LINKS_CONCEPT' },
  ],
};

afterEach(() => {
  resetKgViewState();
  registerKgRenderer(() => {});
});

describe('countTypes', () => {
  it('counts the GIVEN dataset, never the full graph', () => {
    expect(countTypes(FULL)).toEqual({
      nodes: { System: 2, Concept: 1, Field: 2 },
      edges: { LINKS_CONCEPT: 2, HAS_FIELD: 2 },
    });
    // Neighborhood-sized input ⇒ neighborhood-sized counts.
    expect(countTypes(NEIGHBORHOOD)).toEqual({
      nodes: { Concept: 1, System: 2 },
      edges: { LINKS_CONCEPT: 2 },
    });
  });
});

describe('applyVisibility', () => {
  it('drops hidden node types AND every edge touching them (dangling sweep)', () => {
    const out = applyVisibility(FULL, { nodes: new Set(['Field']), edges: new Set() });
    expect(out.nodes.map((n) => n.id)).toEqual(['s1', 's2', 'c1']);
    // The two HAS_FIELD edges would dangle — they must be gone too.
    expect(out.edges.every((e) => e.label !== 'HAS_FIELD')).toBe(true);
    expect(out.edges.length).toBe(2);
  });

  it('drops hidden edge labels but KEEPS their endpoint nodes', () => {
    const out = applyVisibility(FULL, { nodes: new Set(), edges: new Set(['HAS_FIELD']) });
    expect(out.nodes.length).toBe(5);
    expect(out.edges.map((e) => e.label)).toEqual(['LINKS_CONCEPT', 'LINKS_CONCEPT']);
  });
});

describe('setCurrentView coherence', () => {
  it('publishes counts of the SAME dataset the renderer receives', () => {
    const renderer = vi.fn();
    registerKgRenderer(renderer);

    setCurrentView(NEIGHBORHOOD, {
      mode: 'neighborhood',
      nodeId: 'c1',
      nodeLabel: 'Concept One',
      hops: 1,
    });

    expect(kgCountsStore.get()).toEqual(countTypes(NEIGHBORHOOD));
    expect(renderer).toHaveBeenCalledTimes(1);
    const [visible, view] = renderer.mock.calls[0];
    expect(visible).toEqual(applyVisibility(NEIGHBORHOOD, kgHiddenStore.get()));
    expect(view).toEqual({ mode: 'neighborhood', nodeId: 'c1', nodeLabel: 'Concept One', hops: 1 });
    expect(getCurrentViewData()).toBe(NEIGHBORHOOD);
  });

  it('toggleType re-renders the current view WITHOUT changing the mode', () => {
    const renderer = vi.fn();
    registerKgRenderer(renderer);
    setCurrentView(NEIGHBORHOOD, {
      mode: 'neighborhood',
      nodeId: 'c1',
      nodeLabel: 'Concept One',
      hops: 1,
    });
    renderer.mockClear();

    toggleType('nodes', 'System');

    expect(renderer).toHaveBeenCalledTimes(1);
    const [visible] = renderer.mock.calls[0];
    expect(visible.nodes.map((n: { id: string }) => n.id)).toEqual(['c1']);
    // Still focused — the toggle must not kick the user back to filtered.
    expect(kgViewStore.get().mode).toBe('neighborhood');

    // Toggle back — visible again.
    renderer.mockClear();
    toggleType('nodes', 'System');
    expect(renderer.mock.calls[0][0].nodes.length).toBe(3);
  });

  it('returnToFilteredView restores the stashed filtered dataset (no refetch)', () => {
    const renderer = vi.fn();
    registerKgRenderer(renderer);
    setCurrentView(FULL, { mode: 'filtered' });
    setCurrentView(NEIGHBORHOOD, {
      mode: 'neighborhood',
      nodeId: 'c1',
      nodeLabel: 'Concept One',
      hops: 1,
    });
    renderer.mockClear();

    returnToFilteredView();

    expect(kgViewStore.get()).toEqual({ mode: 'filtered' });
    expect(kgCountsStore.get()).toEqual(countTypes(FULL));
    expect(renderer).toHaveBeenCalledTimes(1);
    expect(renderer.mock.calls[0][0]).toEqual(FULL);
  });

  it('resetKgViewState clears view, counts and hidden sets', () => {
    setCurrentView(FULL, { mode: 'filtered' });
    toggleType('edges', 'HAS_FIELD');
    resetKgViewState();
    expect(kgViewStore.get()).toEqual({ mode: 'filtered' });
    expect(kgCountsStore.get()).toEqual({ nodes: {}, edges: {} });
    expect(kgHiddenStore.get().edges.size).toBe(0);
    expect(getCurrentViewData()).toBeNull();
  });
});
