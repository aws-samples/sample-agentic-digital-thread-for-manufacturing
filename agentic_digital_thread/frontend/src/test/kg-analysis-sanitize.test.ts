// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Security regression test for the /api/kg/analyze rendering path.
 *
 * jsx-migration W2.3: requestKgAiAnalysis no longer writes panel.innerHTML —
 * it publishes raw LLM output to kgAnalysisStore, and <NeptuneAnalysisPanel/>
 * renders it through the shared sanitized <Md/> sink (marked + DOMPurify).
 * A script tag or event-handler attribute smuggled into the analysis text
 * must be neutralized at render.
 *
 * jsdom environment: DOMPurify is a no-op in plain node (isSupported=false),
 * so these assertions are only meaningful with a real DOM. */
// @vitest-environment jsdom
import { act, createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { mdToSafeHtml } from '../chat/md';
import { kgAnalysisStore, requestKgAiAnalysis } from '../views/architecture/neptune-graph';
import { NeptuneAnalysisPanel } from '../views/architecture/NeptuneAnalysisPanel';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

function ok(body: unknown): Response {
  return { ok: true, status: 200, json: async () => body } as unknown as Response;
}

describe('mdToSafeHtml neutralizes hostile analysis text', () => {
  it('strips script tags', () => {
    const out = mdToSafeHtml('This work center is critical.<script>alert(1)</script> See MES.');
    expect(out).not.toContain('<script');
    expect(out).toContain('critical');
  });

  it('strips event-handler attributes', () => {
    const out = mdToSafeHtml('An image: <img src="x" onerror="alert(1)"> done.');
    expect(out).not.toContain('onerror');
  });

  it('still renders markdown emphasis', () => {
    expect(mdToSafeHtml('This is **important**.')).toContain('<strong>important</strong>');
  });
});

describe('requestKgAiAnalysis → kgAnalysisStore → <NeptuneAnalysisPanel/>', () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    kgAnalysisStore.set({ status: 'idle' });
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
    kgAnalysisStore.set({ status: 'idle' });
  });

  it('publishes the raw analysis and renders it sanitized (no script execution)', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        ok({
          analysis: 'Safe summary.<script>window.__pwned = true;</script>',
        }),
      ),
    );

    await act(async () => {
      root.render(createElement(NeptuneAnalysisPanel));
    });
    await act(async () => {
      await requestKgAiAnalysis({ labels: ['WorkCenter'], properties: { name: 'WC-100' } } as never);
    });

    expect(kgAnalysisStore.get()).toMatchObject({ status: 'ready' });
    const panel = container.querySelector('#kg-ai-analysis')!;
    expect(panel).not.toBeNull();
    expect(panel.querySelector('script')).toBeNull();
    expect(panel.innerHTML).not.toContain('<script');
    expect(panel.textContent).toContain('Safe summary.');
    expect((window as { __pwned?: boolean }).__pwned).toBeUndefined();
  });

  it('renders the analyzing / none / error states from the store', async () => {
    await act(async () => {
      root.render(createElement(NeptuneAnalysisPanel));
    });
    const panel = () => container.querySelector('#kg-ai-analysis')!;
    expect(panel().textContent).toContain('Select nodes to get AI explanation');

    await act(async () => kgAnalysisStore.set({ status: 'analyzing' }));
    expect(panel().textContent).toContain('Analyzing...');

    await act(async () => kgAnalysisStore.set({ status: 'none' }));
    expect(panel().textContent).toContain('No analysis available');

    await act(async () => kgAnalysisStore.set({ status: 'error' }));
    expect(panel().textContent).toContain('Analysis unavailable');
  });

  it('sets none when the response has no analysis, error on fetch failure', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(ok({})));
    await requestKgAiAnalysis({ labels: ['X'], properties: {} } as never);
    expect(kgAnalysisStore.get()).toEqual({ status: 'none' });

    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('boom')));
    await requestKgAiAnalysis({ labels: ['X'], properties: {} } as never);
    expect(kgAnalysisStore.get()).toEqual({ status: 'error' });
  });
});
