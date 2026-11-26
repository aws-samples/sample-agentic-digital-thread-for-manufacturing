// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tests — the System Map's first-load indicator (mapLoadingStore) and the
 * keep-previous silent reconcile. initSystemMap fetches /api/systems BEFORE
 * building, so on the very FIRST load (no map yet) the loading store must be
 * set while the fetch runs and cleared when the build completes or fails;
 * every revisit keeps the previous map rendered with NO loading state and
 * never blanks (companion to neptune-silent-reload.test.ts). */
// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// Track every cytoscape instance the module builds (hoisted — vi.mock runs
// before imports). The stub's container() returns null so safeFit no-ops.
const { cyInstances } = vi.hoisted(() => ({
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  cyInstances: [] as any[],
}));

vi.mock('cytoscape', () => {
  const cytoscape = Object.assign(
    vi.fn(() => {
      const ids = new Set<string>();
      const classes = new Map<string, Set<string>>();
      const element = (id: string) => ({
        length: ids.has(id) ? 1 : 0,
        addClass: vi.fn((name: string) => {
          const current = classes.get(id) ?? new Set<string>();
          current.add(name);
          classes.set(id, current);
          return element(id);
        }),
        removeClass: vi.fn((name: string) => {
          classes.get(id)?.delete(name);
          return element(id);
        }),
        style: vi.fn(),
        removeStyle: vi.fn(),
      });
      const inst = {
        ids,
        classes,
        add: vi.fn((entry: { data?: { id?: string } }) => {
          if (entry.data?.id) ids.add(entry.data.id);
        }),
        on: vi.fn(),
        destroy: vi.fn(),
        destroyed: () => false,
        container: () => null,
        elements: () => ({ length: 0 }),
        edges: () => ({ removeClass: vi.fn(), addClass: vi.fn() }),
        nodes: () => ({ removeClass: () => ({ removeStyle: vi.fn() }), removeStyle: vi.fn() }),
        getElementById: (id: string) => element(id),
        resize: vi.fn(),
        $: () => ({ length: 0, style: vi.fn() }),
      };
      cyInstances.push(inst);
      return inst;
    }),
    { use: vi.fn() },
  );
  return { default: cytoscape };
});
vi.mock('cytoscape-cola', () => ({ default: {} }));
vi.mock('cytoscape-popper', () => ({ default: {} }));
vi.mock('tippy.js', () => ({ default: vi.fn() }));

import { queryKeys } from '../api/query-keys';
import {
  _resetSystemsQueryClient,
  getSystemsQueryClient,
} from '../api/systems-query';
import {
  destroySystemMap,
  initSystemMap,
  mapLoadingStore,
  startMapFlow,
  stopAllMapFlows,
} from '../views/architecture/system-map';

const SYSTEMS_A = {
  systems: [{ id: 'erp', name: 'ERP', type: 'ERP', plant: 'Detroit', status: 'connected', connection_type: 'postgres' }],
};

function okResponse(body: unknown): Response {
  return { ok: true, json: async () => body } as unknown as Response;
}

function errorResponse(status: number, detail: string): Response {
  return {
    ok: false,
    status,
    statusText: '',
    text: async () => JSON.stringify({ detail }),
  } as unknown as Response;
}

beforeEach(() => {
  _resetSystemsQueryClient();
  stopAllMapFlows();
  destroySystemMap(); // reset the module singleton + lastMapKey
  (globalThis as Record<string, unknown>).mapToolToSystemId = (toolName: string) => {
    if (toolName === 'explore_graph') return null;
    if (toolName.includes('___')) return toolName.split('___')[0];
    if (toolName.startsWith('query_')) return toolName.replace(/^query_/, '').replace(/_/g, '-');
    return null;
  };
  cyInstances.length = 0;
  document.body.replaceChildren();
  const host = document.createElement('div');
  host.id = 'cy-map-graph';
  document.body.appendChild(host);
});

afterEach(() => {
  vi.unstubAllGlobals();
  destroySystemMap();
});

describe('initSystemMap — first-load indicator + silent reconcile', () => {
  it('FIRST load sets the loading store while the fetch runs, clears it when the build completes', async () => {
    let resolveFetch!: (r: Response) => void;
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation(() => new Promise<Response>((res) => (resolveFetch = res))),
    );

    const p = initSystemMap();
    expect(mapLoadingStore.get()).toBe(true); // indicator visible on the blank canvas

    resolveFetch(okResponse(SYSTEMS_A));
    await p;
    expect(mapLoadingStore.get()).toBe(false); // hidden once built
    expect(cyInstances.length).toBe(1); // map was built
  });

  it('a failed FIRST load clears the indicator and still builds the static hub (error path)', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(errorResponse(400, 'systems unavailable')));
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {});

    const p = initSystemMap();
    expect(mapLoadingStore.get()).toBe(true);
    await p;

    expect(mapLoadingStore.get()).toBe(false); // never stuck on
    expect(errSpy).toHaveBeenCalled(); // existing error path preserved
    expect(cyInstances.length).toBe(1); // static hub (orchestrator + KG) still built
    errSpy.mockRestore();
  });

  it('revisit does NOT show the indicator and does NOT blank the existing map', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(okResponse(SYSTEMS_A)));
    await initSystemMap(); // first load — map now present
    expect(cyInstances.length).toBe(1);
    await getSystemsQueryClient().invalidateQueries({
      queryKey: queryKeys.systems(),
      refetchType: 'none',
    });

    // Second activation after invalidation: the refresh hangs — nothing may blank meanwhile.
    let resolveSecond!: (r: Response) => void;
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation(() => new Promise<Response>((res) => (resolveSecond = res))),
    );
    const p = initSystemMap();
    expect(mapLoadingStore.get()).toBe(false); // silent reconcile — no indicator

    resolveSecond(okResponse(SYSTEMS_A)); // unchanged payload
    await p;
    expect(mapLoadingStore.get()).toBe(false);
    expect(cyInstances[0].destroy).not.toHaveBeenCalled(); // instance kept — never blanked
    expect(cyInstances.length).toBe(1); // no rebuild for unchanged data
  });

  it('a non-2xx background refresh keeps the existing map untouched', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(okResponse(SYSTEMS_A)));
    await initSystemMap();
    await getSystemsQueryClient().invalidateQueries({
      queryKey: queryKeys.systems(),
      refetchType: 'none',
    });

    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(errorResponse(400, 'systems unavailable')),
    );
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    await initSystemMap();

    expect(mapLoadingStore.get()).toBe(false);
    expect(cyInstances[0].destroy).not.toHaveBeenCalled();
    expect(cyInstances.length).toBe(1);
    errSpy.mockRestore();
  });

  it('replays an active flow that starts while the first systems fetch is pending', async () => {
    let resolveFetch!: (r: Response) => void;
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation(() => new Promise<Response>((res) => (resolveFetch = res))),
    );
    vi.stubGlobal('requestAnimationFrame', vi.fn(() => 1));
    vi.stubGlobal('cancelAnimationFrame', vi.fn());

    const p = initSystemMap();
    startMapFlow('erp___query'); // event arrives before cyMap exists
    resolveFetch(okResponse(SYSTEMS_A));
    await p;

    expect(cyInstances[0].classes.get('e-orch-map-erp')).toContain('flowing');
    expect(cyInstances[0].classes.get('map-erp')).toContain('querying');

    startMapFlow('digital-thread___explore_graph');
    expect(cyInstances[0].classes.get('e-orch-map-neptune')).toContain('flowing');
    expect(cyInstances[0].classes.get('map-neptune')).toContain('querying');
  });

  it('a failed background refresh keeps the existing map untouched', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(okResponse(SYSTEMS_A)));
    await initSystemMap();
    await getSystemsQueryClient().invalidateQueries({
      queryKey: queryKeys.systems(),
      refetchType: 'none',
    });

    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('network down')));
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    await initSystemMap();

    expect(mapLoadingStore.get()).toBe(false); // no indicator over the live map
    expect(cyInstances[0].destroy).not.toHaveBeenCalled(); // map kept as-is
    errSpy.mockRestore();
  });
});
