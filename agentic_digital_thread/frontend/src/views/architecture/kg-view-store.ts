// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Graph Explorer view-state coherence (UI improvement D).
 *
 * Dependency-free (lib/store + neptune API types only — NO DOM, NO
 * cytoscape) so all coherence logic is offline-testable.
 *
 * setCurrentView is THE single entry: every view change (filtered overview,
 * neighborhood expand) routes here, so the counts the legend shows are — by
 * construction — computed from the SAME dataset the renderer receives
 * (minus visibility toggles). That is the whole point: the legend can never
 * drift from the rendered view. */
import type { NeptuneEdge, NeptuneNode } from '../../api/types/neptune';
import { createStore } from '../../lib/store';

export type KgViewState =
  | { mode: 'filtered' }
  | { mode: 'neighborhood'; nodeId: string; nodeLabel: string; hops: number };

/** What the explorer currently shows (drives the breadcrumb). */
export const kgViewStore = createStore<KgViewState>({ mode: 'filtered' });

export interface KgDataset {
  nodes: NeptuneNode[];
  edges: NeptuneEdge[];
  /** Overview only: node labels the view leaves out, with their real counts
   * (GET /api/neptune/graph `not_drawn`; null where the count could not be
   * read). Never part of what is drawn. */
  notDrawn?: Record<string, number | null>;
}

export interface KgTypeCounts {
  nodes: Record<string, number>;
  edges: Record<string, number>;
}

/** Per-type counts of the CURRENT view's dataset (drives the legend). */
export const kgCountsStore = createStore<KgTypeCounts>({ nodes: {}, edges: {} });

/** Node labels the CURRENT view leaves out and how many the graph holds:
 * the dataset's own `notDrawn` (only the overview carries one; a
 * neighborhood counts exactly what it draws). Lets the legend say "not
 * drawn" instead of a bare 0. */
export const kgNotDrawnStore = createStore<Record<string, number | null>>({});

/** Hidden (unchecked) types — absent = visible. Persists across view
 * switches so a toggle survives focus/unfocus. */
export const kgHiddenStore = createStore<{ nodes: Set<string>; edges: Set<string> }>({
  nodes: new Set(),
  edges: new Set(),
});

/** Count node types and edge labels of ONE dataset (never the full graph —
 * callers pass exactly what the renderer will draw). */
export function countTypes(data: KgDataset): KgTypeCounts {
  const nodes: Record<string, number> = {};
  const edges: Record<string, number> = {};
  for (const n of data.nodes) nodes[n.type] = (nodes[n.type] || 0) + 1;
  for (const e of data.edges) edges[e.label] = (edges[e.label] || 0) + 1;
  return { nodes, edges };
}

/** Apply visibility toggles: drop hidden-type nodes, hidden-label edges, AND
 * any edge with a now-hidden endpoint — cytoscape errors on dangling edges,
 * so the endpoint sweep is load-bearing. Hiding an edge label keeps its
 * endpoint nodes. */
export function applyVisibility(
  data: KgDataset,
  hidden: { nodes: Set<string>; edges: Set<string> },
): KgDataset {
  const nodes = data.nodes.filter((n) => !hidden.nodes.has(n.type));
  const visibleIds = new Set(nodes.map((n) => n.id));
  const edges = data.edges.filter(
    (e) => !hidden.edges.has(e.label) && visibleIds.has(e.source) && visibleIds.has(e.target),
  );
  return { nodes, edges };
}

type KgRenderer = (visible: KgDataset, view: KgViewState) => void;

let _renderer: KgRenderer | null = null;
let _currentView: KgDataset | null = null;
let _filteredData: KgDataset | null = null;

/** Register the render sink (neptune-graph's renderNeptuneGraph in the app;
 * a spy in tests). Single slot — last registration wins. */
export function registerKgRenderer(fn: KgRenderer): void {
  _renderer = fn;
}

/** Re-render the current dataset through the visibility toggles. */
export function rerenderVisible(): void {
  if (_currentView && _renderer) {
    _renderer(applyVisibility(_currentView, kgHiddenStore.get()), kgViewStore.get());
  }
}

/** THE single view entry — publishes view state + counts and renders, all
 * from the one dataset. Filtered datasets are stashed so the breadcrumb's
 * back affordance can restore the overview without a refetch. */
export function setCurrentView(data: KgDataset, view: KgViewState): void {
  _currentView = data;
  if (view.mode === 'filtered') _filteredData = data;
  kgViewStore.set(view);
  kgCountsStore.set(countTypes(data));
  kgNotDrawnStore.set({ ...(data.notDrawn || {}) });
  rerenderVisible();
}

/** Legend checkbox toggle — re-renders the CURRENT view (never changes the
 * mode: toggling a type inside a neighborhood stays in the neighborhood). */
export function toggleType(kind: 'nodes' | 'edges', type: string): void {
  kgHiddenStore.update((prev) => {
    const next = { nodes: new Set(prev.nodes), edges: new Set(prev.edges) };
    if (next[kind].has(type)) next[kind].delete(type);
    else next[kind].add(type);
    return next;
  });
  rerenderVisible();
}

/** Breadcrumb back affordance — restore the filtered overview from the
 * in-memory stash (no refetch). No-op when no filtered view was shown yet. */
export function returnToFilteredView(): void {
  if (_filteredData) setCurrentView(_filteredData, { mode: 'filtered' });
}

/** Current view's dataset (tests + back affordance introspection). */
export function getCurrentViewData(): KgDataset | null {
  return _currentView;
}

/** Destroy path — clear everything (destroyNeptuneGraph). */
export function resetKgViewState(): void {
  _currentView = null;
  _filteredData = null;
  kgViewStore.set({ mode: 'filtered' });
  kgCountsStore.set({ nodes: {}, edges: {} });
  kgNotDrawnStore.set({});
  kgHiddenStore.set({ nodes: new Set(), edges: new Set() });
}
