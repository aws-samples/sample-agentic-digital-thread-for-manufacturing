// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tests — neptune-graph focusKgNode (KG-contribution chip target):
 * reuses the EXISTING selection machinery (select → the node:selected
 * highlight style, resize+fit pan, kgSelectionStore publish sourced from the
 * fetched graph data) and returns an HONEST boolean — false touches no state
 * when the graph is not rendered or the node id is absent (never a fake
 * highlight).
 *
 * cytoscape is mocked at the module boundary (jsdom has no canvas): the fake
 * factory captures the elements renderNeptuneGraph passes and implements the
 * minimal collection surface focusKgNode drives. */
// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const { state, cyFactory } = vi.hoisted(() => {
  interface FakeCy {
    selected: Set<string>;
    resize: ReturnType<typeof vi.fn>;
    fit: ReturnType<typeof vi.fn>;
    on: ReturnType<typeof vi.fn>;
    destroy: ReturnType<typeof vi.fn>;
    elements: () => { unselect: () => void };
    getElementById: (id: string) => unknown;
  }
  const state: { instance: FakeCy | null } = { instance: null };
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const cyFactory = vi.fn((opts: { elements: { data: Record<string, any> }[] }) => {
    const nodes = new Map<string, Record<string, unknown>>();
    const edges: { source: string; target: string }[] = [];
    for (const el of opts.elements) {
      if (el.data.source) edges.push({ source: el.data.source, target: el.data.target });
      else nodes.set(el.data.id as string, el.data);
    }
    const selected = new Set<string>();
    const instance: FakeCy = {
      selected,
      resize: vi.fn(),
      fit: vi.fn(),
      on: vi.fn(),
      destroy: vi.fn(),
      elements: () => ({ unselect: () => selected.clear() }),
      getElementById: (id: string) => {
        const data = nodes.get(id);
        if (!data) return { length: 0 };
        return {
          length: 1,
          isNode: () => true,
          select: () => selected.add(id),
          data: (k: string) => data[k],
          closedNeighborhood: () => ({ kind: 'neighborhood', id }),
          outgoers: () => ({ length: edges.filter((e) => e.source === id).length }),
          incomers: () => ({ length: edges.filter((e) => e.target === id).length }),
        };
      },
    };
    state.instance = instance;
    return instance;
  });
  // ensureCola (system-map) calls cytoscape.use(...) on the same import.
  Object.assign(cyFactory, { use: vi.fn() });
  return { state, cyFactory };
});
vi.mock('cytoscape', () => ({ default: cyFactory }));

import {
  destroyNeptuneGraph,
  focusKgNode,
  kgSelectionStore,
  loadNeptuneGraph,
} from '../views/architecture/neptune-graph';

const GRAPH = {
  nodes: [
    { id: 'n1', label: 'ERP', type: 'System', props: { name: 'ERP', vendor: 'AnyCompany' } },
    { id: 'n2', label: 'work order', type: 'Concept', props: {} },
    // Real deployed shape: cy/engine ~id (UUID-ish) differs from the node's
    // `id` PROPERTY that KG-contribution chips carry.
    {
      id: 'uuid-3f2a',
      label: 'erp↔mes asset',
      type: 'IdentityRule',
      props: { name: 'erp↔mes asset', id: 'subj-aaaaaaaaaaaaaaaa' },
    },
  ],
  edges: [{ source: 'n1', target: 'n2', label: 'HAS_CONCEPT' }],
};

function okResponse(body: unknown): Response {
  return { ok: true, json: async () => body } as unknown as Response;
}

async function loadGraph(): Promise<void> {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(okResponse(GRAPH)));
  await loadNeptuneGraph();
}

beforeEach(() => {
  destroyNeptuneGraph();
  kgSelectionStore.set(null);
  // Hosts renderNeptuneGraph needs: the empty-state overlay and the graph
  // container. (Type filtering is store-driven now — no filter DOM here.)
  document.body.replaceChildren();
  for (const id of ['cy-neptune-empty', 'cy-neptune-graph']) {
    const el = document.createElement('div');
    el.id = id;
    document.body.appendChild(el);
  }
});

afterEach(() => {
  vi.unstubAllGlobals();
  destroyNeptuneGraph();
  kgSelectionStore.set(null);
});

describe('focusKgNode', () => {
  it('resolves a chip PROPERTY id to the engine ~id before lookup', async () => {
    await loadGraph();
    // The chip carries props.id, not the cy element id.
    expect(focusKgNode('subj-aaaaaaaaaaaaaaaa')).toBe(true);
    const cy = state.instance!;
    expect(cy.selected.has('uuid-3f2a')).toBe(true); // selected by ACTUAL cy id
    expect(kgSelectionStore.get()?.label).toBe('IdentityRule');
  });

  it('returns false and touches nothing when no graph is rendered', () => {
    expect(focusKgNode('n1')).toBe(false);
    expect(kgSelectionStore.get()).toBeNull();
  });

  it('selects, pans to, and publishes the node when present (true)', async () => {
    await loadGraph();
    expect(focusKgNode('n1')).toBe(true);

    const cy = state.instance!;
    expect(cy.selected.has('n1')).toBe(true); // node:selected highlight path
    expect(cy.resize).toHaveBeenCalledTimes(1);
    expect(cy.fit).toHaveBeenCalledWith({ kind: 'neighborhood', id: 'n1' }, 60);

    // Selection published from the fetched graph data (name excluded from
    // props, same shape as the tap handler).
    expect(kgSelectionStore.get()).toEqual({
      label: 'System',
      name: 'ERP',
      props: [['vendor', 'AnyCompany']],
      outgoing: 1,
      incoming: 0,
    });
  });

  it('re-focusing replaces the previous selection (unselect-then-select)', async () => {
    await loadGraph();
    focusKgNode('n1');
    focusKgNode('n2');
    const cy = state.instance!;
    expect(cy.selected.has('n1')).toBe(false);
    expect(cy.selected.has('n2')).toBe(true);
    expect(kgSelectionStore.get()?.name).toBe('work order');
    expect(kgSelectionStore.get()?.incoming).toBe(1);
  });

  it('returns false for an absent node id — NO fake selection state', async () => {
    await loadGraph();
    expect(focusKgNode('subj-not-in-graph')).toBe(false);
    expect(state.instance!.selected.size).toBe(0);
    expect(kgSelectionStore.get()).toBeNull();
    expect(state.instance!.fit).not.toHaveBeenCalled();
  });
});
