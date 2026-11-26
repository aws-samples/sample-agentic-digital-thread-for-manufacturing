// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Frontend half of the Graph Explorer left-out count parity pin. The server
 * half is tests/unit/test_server_graph_view_not_drawn.py; both read
 * fixtures/graph-view-not-drawn-cases.json. Each case's `not_drawn` (what
 * /api/neptune/graph answers) is loaded through loadNeptuneGraph and the
 * node legend is rendered with the real NODE_FILTERS, so a count the server
 * could not read (null) is checked never to show as 0 in the static Field
 * and Onboarding Log rows. */
// @vitest-environment jsdom
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { NODE_FILTERS } from '../views/architecture/ArchitectureView';
import { KgTypeLegend } from '../views/architecture/KgTypeLegend';
import { resetKgViewState } from '../views/architecture/kg-view-store';
import { destroyNeptuneGraph, loadNeptuneGraph } from '../views/architecture/neptune-graph';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

type Row = { count: string; note: string | null };
type Case = {
  id: string;
  not_drawn: Record<string, number | null>;
  legend: Record<string, Row>;
  absent: string[];
};

const FIXTURE = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  'fixtures/graph-view-not-drawn-cases.json',
);
const CASES = (JSON.parse(readFileSync(FIXTURE, 'utf8')) as { cases: Case[] }).cases;

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  destroyNeptuneGraph();
  resetKgViewState();
  container = document.createElement('div');
  document.body.replaceChildren(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  destroyNeptuneGraph();
  resetKgViewState();
  vi.unstubAllGlobals();
});

function legendRows(): Record<string, Row> {
  const out: Record<string, Row> = {};
  for (const row of container.querySelectorAll('.kg-checkbox-item')) {
    out[row.querySelector('span')!.textContent!] = {
      count: row.querySelector('.kg-type-count')!.textContent!,
      note: row.querySelector('.kg-type-note')?.textContent ?? null,
    };
  }
  return out;
}

describe('shared left-out count cases (parity with the /api/neptune/graph server half)', () => {
  it.each(CASES.map((c) => [c.id, c] as const))('%s', async (_id, c) => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          nodes: [{ id: 'sys-1', label: 'alpha-system', type: 'System', props: {} }],
          edges: [],
          not_drawn: c.not_drawn,
        }),
      } as unknown as Response),
    );
    await loadNeptuneGraph();
    act(() => root.render(<KgTypeLegend kind="nodes" staticTypes={NODE_FILTERS} />));
    const rows = legendRows();
    for (const [label, expected] of Object.entries(c.legend)) {
      expect(rows[label], label).toEqual(expected);
    }
    for (const label of c.absent) expect(rows[label], label).toBeUndefined();
    // A count the server could not read never reads as 0.
    for (const [type, value] of Object.entries(c.not_drawn)) {
      if (value !== null) continue;
      const label = NODE_FILTERS.find((f) => f.value === type)?.label ?? type;
      if (rows[label]) expect(rows[label].count, label).not.toBe('0');
    }
    expect(rows.System).toEqual({ count: '1', note: null });
  });
});
