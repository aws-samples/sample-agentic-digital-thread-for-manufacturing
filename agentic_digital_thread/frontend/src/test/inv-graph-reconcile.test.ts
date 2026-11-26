// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Regression test for the BUG-3 class of loss: the investigation graph must
 * reconcile the FULL accumulated store snapshot on (re)activation — a fresh
 * or partially-populated cytoscape instance receives every node/edge already
 * in the store, not just events that arrive after it (re)subscribes.
 *
 * `computeGraphDiff` is the pure diff `syncFromStore` applies to the live cy
 * instance (inv-graph.ts); the store is driven through the same
 * handleArchEvent stream the chat bridge emits. */
import { describe, expect, it } from 'vitest';

import { computeGraphDiff } from '../views/architecture/inv-graph';
import { installArchStoreDisposal, trackedArchStore as createArchStore } from './arch-store-tracking';

installArchStoreDisposal();

/** Stream a realistic two-tool turn into a fresh store. */
function populatedStore() {
  const store = createArchStore();
  store.handleArchEvent('user_msg', { question: 'How do the systems relate?' });
  store.handleArchEvent('tool_start', { tool: 'explore_graph', tool_input: { query: 'systems' } });
  store.handleArchEvent('tool_result', { tool: 'explore_graph', result: 'sys-a — sys-b' });
  store.handleArchEvent('tool_start', { tool: 'query_sys_a', tool_input: { query: 'schema' } });
  store.handleArchEvent('tool_result', { tool: 'query_sys_a', result: '3 tables' });
  store.handleArchEvent('done', {});
  return store;
}

describe('inv-graph reconcile-on-activation (BUG 3 regression)', () => {
  it('a fresh (empty) cy instance receives the ENTIRE accumulated snapshot', () => {
    const store = populatedStore();
    const state = store.getState();
    expect(state.nodes.length).toBeGreaterThan(0);
    expect(state.edges.length).toBeGreaterThan(0);

    // Fresh mount: no existing cy elements — everything must be added.
    const diff = computeGraphDiff(new Set(), new Set(), state);
    expect(diff.addNodes.map((n) => n.id).sort()).toEqual(
      state.nodes.map((n) => n.id).sort(),
    );
    const addableEdgeIds = state.edges
      .filter((e) => !!e.id)
      .map((e) => e.id)
      .sort();
    expect(diff.addEdges.map((e) => e.id).sort()).toEqual(addableEdgeIds);
    expect(diff.removeNodeIds).toEqual([]);
    expect(diff.removeEdgeIds).toEqual([]);
  });

  it('events accumulated while the view was inactive are added on return, and pre-existing elements are NOT removed', () => {
    const store = populatedStore();
    const midState = store.getState();
    const cyNodeIds = new Set(midState.nodes.map((n) => n.id));
    const cyEdgeIds = new Set(
      midState.edges.filter((e) => !!e.id).map((e) => e.id as string),
    );

    // While "away" (dashboard active), the stream continues.
    store.handleArchEvent('user_msg', { question: 'And which plant runs sys-b?' });
    store.handleArchEvent('tool_start', { tool: 'query_sys_b', tool_input: { query: 'plant' } });
    store.handleArchEvent('tool_result', { tool: 'query_sys_b', result: 'plant-1' });
    const finalState = store.getState();
    expect(finalState.nodes.length).toBeGreaterThan(midState.nodes.length);

    // On return: only the new elements are added, nothing accumulated is lost.
    const diff = computeGraphDiff(cyNodeIds, cyEdgeIds, finalState);
    expect(diff.removeNodeIds).toEqual([]);
    expect(diff.removeEdgeIds).toEqual([]);
    const expectedNew = finalState.nodes
      .filter((n) => !cyNodeIds.has(n.id))
      .map((n) => n.id)
      .sort();
    expect(diff.addNodes.map((n) => n.id).sort()).toEqual(expectedNew);
    // Every pre-leave node is retained (update path, not add/remove).
    expect(diff.updateNodes.map((n) => n.id).sort()).toEqual([...cyNodeIds].sort());
  });

  it('a store reset ("+ New") removes everything from a populated instance', () => {
    const store = populatedStore();
    const state = store.getState();
    const cyNodeIds = new Set(state.nodes.map((n) => n.id));
    const cyEdgeIds = new Set(state.edges.filter((e) => !!e.id).map((e) => e.id as string));
    store.reset();
    const diff = computeGraphDiff(cyNodeIds, cyEdgeIds, store.getState());
    expect(new Set(diff.removeNodeIds)).toEqual(cyNodeIds);
    expect(new Set(diff.removeEdgeIds)).toEqual(cyEdgeIds);
    expect(diff.addNodes).toEqual([]);
    expect(diff.addEdges).toEqual([]);
  });
});
