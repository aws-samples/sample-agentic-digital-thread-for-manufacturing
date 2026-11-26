// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
/* Confirm Schema lists the discovered COLUMN NAMES, not only row counts.
 *
 * Walkthrough feedback: the Discover checkpoint showed tables and row counts,
 * so a user could not confirm what discovery found until the Map step. The
 * POST /api/onboard/discover response already carries each table's columns
 * (the map step forwards them), so the schema table now shows a per-table
 * column count with the names in a collapsed fold. Generic fixture data only,
 * no dataset literals. */
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import type { DiscoveredTable } from '../api/types/onboard';
import { ObFlow } from '../views/onboard/components/ObFlow';
import { appendCheckpoint, resetToWelcome } from '../views/onboard/flow-store';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

function renderSchema(tables: DiscoveredTable[], assertFn: (el: HTMLElement) => void): void {
  appendCheckpoint({
    title: 'CHECKPOINT — Confirm Schema',
    content: { kind: 'schemaTable', tables },
    confirmLabel: 'Confirm Schema →',
    stepContext: { step: 'discover', systemId: 'sys-a', data: { tables }, history: [] },
    onConfirm: () => {},
  });
  const el = document.createElement('div');
  document.body.appendChild(el);
  const root = createRoot(el);
  act(() => root.render(<ObFlow />));
  try {
    assertFn(el);
  } finally {
    act(() => root.unmount());
    el.remove();
  }
}

beforeEach(() => resetToWelcome());
afterEach(() => resetToWelcome());

describe('Confirm Schema checkpoint', () => {
  it('shows each table column count with the names in a collapsed fold', () => {
    renderSchema(
      [
        {
          name: 'records_a',
          row_count: 1200,
          columns: [
            { name: 'record_id', data_type: 'integer' },
            { name: 'item_code', data_type: 'text' },
            { name: 'recorded_at', data_type: 'timestamp' },
          ],
        },
        { name: 'records_b', row_count: 5, columns: [{ name: 'code', data_type: 'text' }] },
      ],
      (el) => {
        const headers = [...el.querySelectorAll('.ob-table th')].map((th) => th.textContent);
        expect(headers).toEqual(['Table', 'Rows', 'Columns']);
        const folds = el.querySelectorAll('details.ob-schema-columns');
        expect(folds).toHaveLength(2);
        const first = folds[0] as HTMLDetailsElement;
        expect(first.open).toBe(false); // collapsed by default
        expect(first.querySelector('summary')?.textContent).toBe('3 columns');
        const names = [...first.querySelectorAll('li')].map((li) => li.textContent);
        expect(names).toEqual(['record_id (integer)', 'item_code (text)', 'recorded_at (timestamp)']);
        expect(folds[1].querySelector('summary')?.textContent).toBe('1 column');
      },
    );
  });

  it('falls back to a bare count, or a dash, when names are absent', () => {
    renderSchema(
      [
        { name: 'records_a', row_count: 3, column_count: 7 },
        { name: 'records_b', row_count: 0 },
      ],
      (el) => {
        expect(el.querySelectorAll('details.ob-schema-columns')).toHaveLength(0);
        const cells = [...el.querySelectorAll('.ob-table tbody tr')].map(
          (tr) => tr.querySelectorAll('td')[2]?.textContent,
        );
        expect(cells).toEqual(['7', '—']);
      },
    );
  });
});
