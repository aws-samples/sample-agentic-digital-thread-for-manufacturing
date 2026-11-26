// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tests — the Investigation legend (#arch-legend) includes the purple
 * Knowledge Graph entry. The investigation graph has always rendered
 * explore_graph activity as a purple NODE_COLORS.neptune hub node, but no
 * legend entry explained it (dropped from the legacy static markup too);
 * bug-bash fix adds it between System and Entity. */
// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { NODE_FILTERS } from '../views/architecture/ArchitectureView';
import { ArchLegend } from '../views/architecture/Legends';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

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

describe('ArchLegend (#arch-legend)', () => {
  it('renders the five entries in order, including the Knowledge Graph entry', () => {
    act(() => root.render(<ArchLegend />));

    const legend = document.getElementById('arch-legend')!;
    expect(legend).toBeTruthy();
    const items = Array.from(legend.querySelectorAll('.legend-item'));
    expect(items.map((el) => el.textContent)).toEqual([
      'Question',
      'System',
      'Knowledge Graph',
      'Entity',
      'Data',
    ]);
  });

  it('the Knowledge Graph entry carries the purple neptune swatch class', () => {
    act(() => root.render(<ArchLegend />));

    const legend = document.getElementById('arch-legend')!;
    const kgItem = Array.from(legend.querySelectorAll('.legend-item')).find(
      (el) => el.textContent === 'Knowledge Graph',
    )!;
    expect(kgItem).toBeTruthy();
    const swatch = kgItem.querySelector('.legend-swatch')!;
    expect(swatch.classList.contains('neptune')).toBe(true);
  });
});

describe('Knowledge Graph node filters', () => {
  it('carries only what the legend consumes: value+label rows, no pseudo-rows', () => {
    // Kills: re-adding an 'all' pseudo-row (the legend renders every row —
    // a pseudo-row would draw a bogus type with a zero count), or dropping
    // a label (the legend would render an unnamed checkbox).
    expect(NODE_FILTERS.length).toBeGreaterThan(0);
    for (const f of NODE_FILTERS) {
      expect(f.value).toBeTruthy();
      expect(f.value).not.toBe('all');
      expect(f.label).toBeTruthy();
    }
  });
});
