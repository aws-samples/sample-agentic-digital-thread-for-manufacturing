// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* The Knowledge Graph tab shows the server's public error text.
 *
 * A failed graph read answers HTTP 200 with `{nodes: [], edges: [], error}`
 * and a failed node analysis `{analysis: "", error}`, where `error` is a
 * fixed category message plus a reference an operator can find in the
 * server log. The tab must show that text instead of an empty graph or a
 * bare "Analysis unavailable". */
// @vitest-environment jsdom
import { act, createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  destroyNeptuneGraph,
  kgAnalysisStore,
  loadNeptuneGraph,
  requestKgAiAnalysis,
} from '../views/architecture/neptune-graph';
import { NeptuneAnalysisPanel } from '../views/architecture/NeptuneAnalysisPanel';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const GRAPH_ERROR =
  'The knowledge graph query failed. Retry in a moment; if this keeps happening, ' +
  'report it. Reference: 0123456789ab';
const GRAPH_A = { nodes: [{ id: 'n1', label: 'ERP', type: 'System', props: {} }], edges: [] };

function ok(body: unknown): Response {
  return { ok: true, status: 200, json: async () => body } as unknown as Response;
}

function overlay(): HTMLElement {
  return document.getElementById('cy-neptune-empty')!;
}

beforeEach(() => {
  destroyNeptuneGraph();
  document.body.replaceChildren();
  const empty = document.createElement('div');
  empty.id = 'cy-neptune-empty';
  document.body.appendChild(empty);
  kgAnalysisStore.set({ status: 'idle' });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  destroyNeptuneGraph();
  kgAnalysisStore.set({ status: 'idle' });
});

describe('graph load with a public error payload', () => {
  it('a FIRST load shows the category message and its reference', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(ok({ nodes: [], edges: [], error: GRAPH_ERROR })));
    await loadNeptuneGraph();
    expect(overlay().style.display).toBe('flex');
    expect(overlay().textContent).toContain('Failed to load graph');
    expect(overlay().textContent).toContain('Reference: 0123456789ab');
  });

  it('a background refresh with an error payload keeps the current graph', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(ok(GRAPH_A)));
    await loadNeptuneGraph();
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(ok({ nodes: [], edges: [], error: GRAPH_ERROR })));
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    await loadNeptuneGraph();
    expect(overlay().style.display).toBe('none');
    expect(errSpy).toHaveBeenCalled();
  });
});

describe('node analysis with a public error payload', () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
  });

  it('publishes the error state with the message and renders it', async () => {
    const message = 'An upstream service returned an error. Reference: ba9876543210';
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(ok({ analysis: '', error: message })));
    await act(async () => {
      root.render(createElement(NeptuneAnalysisPanel));
    });
    await act(async () => {
      await requestKgAiAnalysis({ labels: ['System'], properties: { name: 'erp' } } as never);
    });
    expect(kgAnalysisStore.get()).toEqual({ status: 'error', message });
    const panel = container.querySelector('#kg-ai-analysis')!;
    expect(panel.textContent).toContain('Analysis unavailable');
    expect(panel.textContent).toContain('Reference: ba9876543210');
  });
});
