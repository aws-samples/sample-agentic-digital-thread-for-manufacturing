// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
/* The concept suggestions panel ("N fields may match new concepts") shown
 * after Provision: every row reachable, each row says which system, table and
 * column it is about, what tag it has now and why the model suggested it, and
 * Accept marks a row done only when the server reports that field written. */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../views/onboard/onboard-queries', () => ({
  getOnboardQueryClient: vi.fn(),
  invalidateOnboardingRemap: vi.fn(async () => {}),
  useConceptsQuery: () => ({ data: [], isError: false, refetch: vi.fn() }),
}));

import type { RemapSuggestion } from '../api/types/onboard';
import { ObFlow } from '../views/onboard/components/ObFlow';
import {
  flowStore,
  resetToWelcome,
  showReconcileBanner,
  startWizard,
} from '../views/onboard/flow-store';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;

function suggestion(i: number, extra: Partial<RemapSuggestion> = {}): RemapSuggestion {
  return {
    field_id: `system-b.table_x.col_${i}`,
    concept_id: 'concept-n',
    concept_name: 'Concept N',
    role: 'identifier',
    confidence: 0.82,
    reasoning: `reason ${i}`,
    system_id: 'system-b',
    system_name: 'System B',
    table: 'table_x',
    column: `col_${i}`,
    current_tags: [],
    ...extra,
  };
}

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  });
}

function rows(): HTMLElement[] {
  return Array.from(container.querySelectorAll<HTMLElement>('.ob-reconcile-row'));
}

async function clickAccept(row: HTMLElement): Promise<void> {
  const accept = row.querySelector<HTMLButtonElement>('.ob-rec-accept')!;
  await act(async () => {
    accept.click();
    await Promise.resolve();
  });
  await act(async () => {
    await new Promise((r) => setTimeout(r, 0));
  });
}

beforeEach(() => {
  resetToWelcome();
  startWizard();
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  resetToWelcome();
  vi.unstubAllGlobals();
});

describe('concept suggestions panel', () => {
  it('renders every suggestion, not just the first few', () => {
    showReconcileBanner(Array.from({ length: 7 }, (_, i) => suggestion(i)));
    act(() => root.render(<ObFlow />));
    expect(rows()).toHaveLength(7);
    expect(flowStore.get().banners[0].rows).toHaveLength(7);
    expect(container.querySelector('.ob-reconcile-header')?.textContent).toContain('7 fields');
  });

  it('keeps a long list inside a scrolling, keyboard-reachable region', () => {
    showReconcileBanner(Array.from({ length: 12 }, (_, i) => suggestion(i)));
    act(() => root.render(<ObFlow />));
    const list = container.querySelector<HTMLElement>('.ob-reconcile-rows')!;
    expect(list.getAttribute('tabindex')).toBe('0');
    expect(list.getAttribute('aria-label')).toBeTruthy();
  });

  it('names the system, table and column, the suggested tag and the current tag', () => {
    showReconcileBanner([
      suggestion(1, {
        system_name: 'System C',
        table: 'table_y',
        column: 'col_r',
        role: 'measure',
        confidence: 0.85,
        current_tags: [{ concept_id: 'concept-old', role: 'measure' }],
      }),
      suggestion(2),
    ]);
    act(() => root.render(<ObFlow />));
    const [first, second] = rows();
    expect(first.textContent).toContain('System C');
    expect(first.querySelector('code')?.textContent).toBe('table_y.col_r');
    expect(first.textContent).toContain('Concept N');
    expect(first.textContent).toContain('measure');
    expect(first.textContent).toContain('85%');
    expect(first.querySelector('.ob-reconcile-current')?.textContent).toBe(
      'Now: concept-old (measure)',
    );
    expect(second.querySelector('.ob-reconcile-current')?.textContent).toBe('Now: untagged');
  });

  it('shows the model reasoning in an expander on each row', () => {
    showReconcileBanner([suggestion(1, { reasoning: 'values match concept-n ids' })]);
    act(() => root.render(<ObFlow />));
    const details = rows()[0].querySelector('details');
    expect(details).not.toBeNull();
    expect(details!.querySelector('summary')?.textContent).toBeTruthy();
    expect(details!.textContent).toContain('values match concept-n ids');
  });

  it('describes what the panel does', () => {
    showReconcileBanner([suggestion(1)]);
    act(() => root.render(<ObFlow />));
    const help = container.querySelector('.ob-reconcile-help')?.textContent || '';
    expect(help).toBe(
      'Optional: fields in other systems that may belong to a concept this system added. ' +
        'Accept tags the field; Skip leaves it.',
    );
  });

  it('marks a row accepted when the server reports that field written', async () => {
    const fetchMock = vi.fn(async () =>
      jsonResponse({ remapped: 1, written: ['system-b.table_x.col_1'], not_written: [] }),
    );
    vi.stubGlobal('fetch', fetchMock);
    showReconcileBanner([suggestion(1), suggestion(2)]);
    act(() => root.render(<ObFlow />));

    await clickAccept(rows()[0]);

    expect(flowStore.get().banners[0].rows[0].decided).toBe('accepted');
    const [, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    const body = JSON.parse(String(init.body));
    expect(body.remaps).toEqual([
      {
        field_id: 'system-b.table_x.col_1',
        concept_id: 'concept-n',
        role: 'identifier',
        confidence: 0.82,
      },
    ]);
  });

  it('leaves a row open with an error when the server did not write that field', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        jsonResponse({ remapped: 0, written: [], not_written: ['system-b.table_x.col_1'] }),
      ),
    );
    showReconcileBanner([suggestion(1)]);
    act(() => root.render(<ObFlow />));

    await clickAccept(rows()[0]);

    const row = rows()[0];
    expect(flowStore.get().banners[0].rows[0].decided).toBeNull();
    expect(row.querySelector<HTMLButtonElement>('.ob-rec-accept')!.disabled).toBe(false);
    expect(row.querySelector('[role="alert"]')?.textContent).toMatch(/not tagged/i);
  });

  it('shows a failed request as an error on that row', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response('busy', { status: 503 })),
    );
    showReconcileBanner([suggestion(1)]);
    act(() => root.render(<ObFlow />));

    await clickAccept(rows()[0]);

    expect(flowStore.get().banners[0].rows[0].decided).toBeNull();
    expect(rows()[0].querySelector('[role="alert"]')?.textContent).toBeTruthy();
  });

  it('does not clear one row pending state when another row Accept finishes first', async () => {
    // Two Accepts in flight at once: resolving the second must not re-enable
    // the first row's Accept while its own request is still outstanding.
    const resolvers: Record<string, (r: Response) => void> = {};
    vi.stubGlobal(
      'fetch',
      vi.fn((_url: string, init: RequestInit) => {
        const fieldId = JSON.parse(String(init.body)).remaps[0].field_id as string;
        return new Promise<Response>((resolve) => {
          resolvers[fieldId] = resolve;
        });
      }),
    );
    showReconcileBanner([suggestion(1), suggestion(2)]);
    act(() => root.render(<ObFlow />));

    const accept = (row: HTMLElement) =>
      act(async () => {
        row.querySelector<HTMLButtonElement>('.ob-rec-accept')!.click();
        await Promise.resolve();
      });
    await accept(rows()[0]); // field col_1, request hangs
    await accept(rows()[1]); // field col_2, request hangs

    const btn0 = () => rows()[0].querySelector<HTMLButtonElement>('.ob-rec-accept')!;
    const btn1 = () => rows()[1].querySelector<HTMLButtonElement>('.ob-rec-accept')!;
    expect(btn0().disabled).toBe(true);
    expect(btn1().disabled).toBe(true);

    // The SECOND request finishes first.
    await act(async () => {
      resolvers['system-b.table_x.col_2'](
        jsonResponse({ remapped: 1, written: ['system-b.table_x.col_2'], not_written: [] }),
      );
      await new Promise((r) => setTimeout(r, 0));
    });

    // Row 0's request is still outstanding, so its Accept stays disabled.
    expect(btn0().disabled).toBe(true);
    expect(flowStore.get().banners[0].rows[1].decided).toBe('accepted');
  });
});
