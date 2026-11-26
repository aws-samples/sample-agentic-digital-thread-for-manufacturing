// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
/* Re-map is removed for the 2026-10 release: its Save was a full,
 * destructive re-provision of an onboarded system (deleted the system's
 * onboarding identity rules and join paths, replaced its whole Field subgraph
 * with the rows sent). These pin that nothing in the mounted Onboard view can
 * reach it: no window shim, no system-detail button. */
import { QueryClientProvider } from '@tanstack/react-query';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { createQueryClient } from '../api/query-client';
import OnboardView from '../views/onboard/OnboardView';
import { SystemDetail } from '../views/onboard/components/SystemDetail';
import type { DetailState } from '../views/onboard/flow-store';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  vi.stubGlobal(
    'fetch',
    vi.fn(
      async () =>
        new Response('[]', { status: 200, headers: { 'Content-Type': 'application/json' } }),
    ),
  );
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
});

describe('Re-map is removed', () => {
  it('the mounted Onboard view installs its ob* shims but no remapSystem / saveRemappings', async () => {
    await act(async () => {
      root.render(
        <QueryClientProvider client={createQueryClient()}>
          <OnboardView />
        </QueryClientProvider>,
      );
    });

    const w = window as unknown as Record<string, unknown>;
    // Registration ran: the surviving shims are present.
    expect(typeof w.obLoadCatalog).toBe('function');
    expect(typeof w.showSystemDetail).toBe('function');
    // The withdrawn Re-map entry points are not.
    expect('remapSystem' in w).toBe(false);
    expect('saveRemappings' in w).toBe(false);
  });

  it('the onboarded system detail has no Re-map Fields button', async () => {
    const detail: DetailState = {
      phase: 'ready',
      sys: { id: 'sys-a', name: 'System A', status: 'onboarded' },
      tables: [{ name: 'items', row_count: 3 }],
      mappings: [{ field: 'items.code', concept: 'lot-ref', confidence: 0.9 }],
    };
    await act(async () => {
      root.render(<SystemDetail detail={detail} />);
    });

    expect(container.textContent).toContain('items');
    const labels = [...container.querySelectorAll('button')].map((b) => b.textContent || '');
    expect(labels.some((t) => /re-map/i.test(t))).toBe(false);
  });
});
