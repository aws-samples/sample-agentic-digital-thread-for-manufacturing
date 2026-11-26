// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
/* Coverage gaps (status 'uncovered' — e.g. mid-onboarding, no system maps the
 * concept yet) must render the NEUTRAL needs-data presentation, never error
 * styling (red + Retry + raw resolver internals). Pins the panel branch and
 * the line-state model classification. */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { QualityByLinePanel } from '../views/dashboard/Panels';
import { computeLineState } from '../views/dashboard/lib';
import { dashboardStore } from '../views/dashboard/store';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;
const initial = dashboardStore.get();

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => {
    root.unmount();
  });
  container.remove();
  dashboardStore.set(initial);
});

describe('uncovered (mapping gap) presentation', () => {
  it('quality panel renders needs-data guidance, not error styling', async () => {
    dashboardStore.update((s) => ({
      ...s,
      quality: {
        status: 'uncovered',
        missing_concepts: ['quality-yield-rate'],
        detail: "no onboarded system maps concept 'quality-yield-rate' yet",
        rows: [],
      },
    }));
    await act(async () => {
      root.render(<QualityByLinePanel />);
    });
    // The uncovered panel routes into the awaiting-data strip (steward
    // feedback 2026-08-06) and renders NOTHING in the grid — no placeholder
    // card, no error styling, no Retry.
    expect(container.textContent).toBe('');
    expect(dashboardStore.get().uncovered['panel:quality-by-line']).toEqual({
      title: 'Quality by line',
      missing: ['quality-yield-rate'],
      kind: 'structural',
    });
  });

  it('computeLineState classifies uncovered as its own kind with friendly diag', () => {
    const model = computeLineState({
      status: 'uncovered',
      missing_concepts: ['machine-state'],
      detail: "no MAPS_TO field for concept 'machine-state'",
    });
    expect(model.kind).toBe('uncovered');
    expect(model.diag).toContain('machine-state');
    expect(model.diag).toContain('Waiting for data');
    // never the raw resolver internals in the friendly line
    expect(model.diag).not.toContain('MAPS_TO');
  });

  it('error payloads still render the error path (regression)', () => {
    const model = computeLineState({ status: 'error', error: 'join failed' });
    expect(model.kind).toBe('error');
  });
});
