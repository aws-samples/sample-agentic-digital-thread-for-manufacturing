// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tests — the Knowledge Graph tab's keep-previous silent reconcile
 * (loading-state audit): re-activating the tab (switchArchTab('neptune') →
 * loadNeptuneGraph()) must NOT blank the already-rendered graph. The first
 * load shows the loading overlay; every later load keeps the existing
 * graph/state rendered while data refreshes in the background, a failed
 * refresh keeps the current graph, and the chat-mirroring stores
 * (kgAnalysisStore/kgSelectionStore) are never touched by a reload. */
// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  destroyNeptuneGraph,
  kgAnalysisStore,
  kgSelectionStore,
  loadNeptuneGraph,
} from '../views/architecture/neptune-graph';

const GRAPH_A = { nodes: [{ id: 'n1', label: 'ERP', type: 'System', props: {} }], edges: [] };

function okResponse(body: unknown): Response {
  return { ok: true, json: async () => body } as unknown as Response;
}

function errorResponse(status: number, body: unknown = {}): Response {
  return {
    ok: false,
    status,
    statusText: '',
    text: async () => JSON.stringify(body),
  } as unknown as Response;
}

function emptyOverlay(): HTMLElement {
  return document.getElementById('cy-neptune-empty')!;
}

beforeEach(() => {
  destroyNeptuneGraph(); // resets the module data cache
  // The overlay host only — no #cy-neptune-graph / filter containers, so the
  // cytoscape render path no-ops (store/DOM-level test, no canvas needed).
  document.body.replaceChildren();
  const empty = document.createElement('div');
  empty.id = 'cy-neptune-empty';
  document.body.appendChild(empty);
});

afterEach(() => {
  vi.unstubAllGlobals();
  destroyNeptuneGraph();
  kgSelectionStore.set(null);
});

describe('loadNeptuneGraph — keep-previous silent reconcile', () => {
  it('FIRST load shows the loading overlay, then hides it when data arrives', async () => {
    let resolveFetch!: (r: Response) => void;
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation(() => new Promise<Response>((res) => (resolveFetch = res))),
    );

    const p = loadNeptuneGraph();
    expect(emptyOverlay().style.display).toBe('flex');
    expect(emptyOverlay().textContent).toBe('Loading knowledge graph…');

    resolveFetch(okResponse(GRAPH_A));
    await p;
    expect(emptyOverlay().style.display).toBe('none');
  });

  it('re-activation does NOT blank: existing data stays rendered until fresh data arrives', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(okResponse(GRAPH_A)));
    await loadNeptuneGraph(); // first load — graph now present

    // Chat-mirroring state that must survive a navigate-away-and-back.
    kgAnalysisStore.set({ status: 'ready', analysis: 'ERP is the system of record' });
    kgSelectionStore.set({ label: 'System', name: 'ERP', props: [], outgoing: 1, incoming: 0 });

    // Second activation: the refresh hangs — nothing may blank meanwhile.
    let resolveSecond!: (r: Response) => void;
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation(() => new Promise<Response>((res) => (resolveSecond = res))),
    );
    const p = loadNeptuneGraph();

    // No loading overlay (residual hidden text from the first load is fine —
    // display governs blanking), no cleared state, stores untouched.
    expect(emptyOverlay().style.display).toBe('none');
    expect(kgAnalysisStore.get()).toEqual({
      status: 'ready',
      analysis: 'ERP is the system of record',
    });
    expect(kgSelectionStore.get()?.name).toBe('ERP');

    resolveSecond(okResponse(GRAPH_A)); // unchanged payload
    await p;
    expect(emptyOverlay().style.display).toBe('none');
    expect(kgAnalysisStore.get().status).toBe('ready'); // still untouched
  });

  it('a failed background refresh keeps the current graph (no error overlay)', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(okResponse(GRAPH_A)));
    await loadNeptuneGraph();

    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('network down')));
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    await loadNeptuneGraph();

    expect(emptyOverlay().style.display).toBe('none'); // graph still shown
    expect(emptyOverlay().textContent).not.toContain('Failed');
    expect(errSpy).toHaveBeenCalled(); // logged, not surfaced over the graph
  });

  it('a non-2xx background refresh keeps the current graph (no error overlay)', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(okResponse(GRAPH_A)));
    await loadNeptuneGraph();

    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(errorResponse(503, { detail: 'graph backend unavailable' })),
    );
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    await loadNeptuneGraph();

    expect(emptyOverlay().style.display).toBe('none');
    expect(emptyOverlay().textContent).not.toContain('Failed');
    expect(errSpy).toHaveBeenCalled();
  });

  it('a failed FIRST load surfaces the error in the overlay', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('network down')));
    await loadNeptuneGraph();

    expect(emptyOverlay().style.display).toBe('flex');
    expect(emptyOverlay().textContent).toContain('Failed to load graph');
  });

  it('a non-ok FIRST response surfaces as an error (resp.ok check)', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(errorResponse(502, { detail: 'graph unavailable' })),
    );
    await loadNeptuneGraph();

    expect(emptyOverlay().style.display).toBe('flex');
    expect(emptyOverlay().textContent).toContain('graph unavailable');
  });
});
