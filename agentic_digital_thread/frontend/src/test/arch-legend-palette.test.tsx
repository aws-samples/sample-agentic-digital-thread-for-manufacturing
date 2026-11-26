// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tests — legend swatches are SINGLE-SOURCED from the exported palettes.
 *
 * The .legend-swatch.<type> css rules used to duplicate the node-fill hex
 * (dashboard.css + enhancements.css) — a divergence risk against colors.ts.
 * Now Legends.tsx derives every swatch color inline from invPalette /
 * neptunePalette and re-renders on themeStore, and the css classes carry
 * shape only. */
// @vitest-environment jsdom
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import {
  NEPTUNE_NODE_COLORS,
  NEPTUNE_NODE_COLORS_LIGHT,
  NODE_COLORS,
  NODE_COLORS_LIGHT,
  themeStore,
} from '../views/architecture/colors';
import { ArchLegend, GraphLegend, MapLegend } from '../views/architecture/Legends';
import { neptuneLegendStore } from '../views/architecture/neptune-graph';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  document.documentElement.classList.remove('light');
  themeStore.set(true);
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  document.documentElement.classList.remove('light');
  themeStore.set(true);
});

/** jsdom CSSOM renders colors as `rgb(r, g, b)`. */
function rgb(hex: string): string {
  const h = hex.replace('#', '');
  const [r, g, b] = [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16));
  return `rgb(${r}, ${g}, ${b})`;
}

function goLight() {
  act(() => {
    document.documentElement.classList.add('light');
    themeStore.set(false);
  });
}

const ARCH_TYPES: [type: string, label: string][] = [
  ['question', 'Question'],
  ['system', 'System'],
  ['neptune', 'Knowledge Graph'],
  ['entity', 'Entity'],
  ['finding', 'Data'],
];

describe('ArchLegend derives swatch colors from invPalette (single source)', () => {
  it('dark theme: every swatch matches NODE_COLORS exactly', () => {
    act(() => root.render(<ArchLegend />));
    for (const [type, label] of ARCH_TYPES) {
      const item = Array.from(document.querySelectorAll('#arch-legend .legend-item')).find(
        (el) => el.textContent === label,
      )!;
      const swatch = item.querySelector('.legend-swatch') as HTMLElement;
      expect(swatch.style.backgroundColor, `${type} bg`).toBe(rgb(NODE_COLORS[type].bg));
      expect(swatch.style.borderColor, `${type} border`).toBe(rgb(NODE_COLORS[type].border));
      expect(swatch.classList.contains(type), `${type} shape class`).toBe(true);
    }
  });

  it('LIGHT theme: every swatch matches NODE_COLORS_LIGHT exactly — and a live toggle re-renders', () => {
    act(() => root.render(<ArchLegend />));
    goLight();
    for (const [type, label] of ARCH_TYPES) {
      const item = Array.from(document.querySelectorAll('#arch-legend .legend-item')).find(
        (el) => el.textContent === label,
      )!;
      const swatch = item.querySelector('.legend-swatch') as HTMLElement;
      expect(swatch.style.backgroundColor, `${type} light bg`).toBe(rgb(NODE_COLORS_LIGHT[type].bg));
      expect(swatch.style.borderColor, `${type} light border`).toBe(
        rgb(NODE_COLORS_LIGHT[type].border),
      );
    }
  });
});

describe('GraphLegend follows the theme live (neptunePalette)', () => {
  it('re-renders swatches from the light palette on toggle (no reload needed)', () => {
    act(() => neptuneLegendStore.set(['System', 'Concept', 'Asset', 'MetaNode']));
    act(() => root.render(<GraphLegend />));
    const swatchFor = (t: string) =>
      Array.from(document.querySelectorAll('#neptune-legend .legend-item'))
        .find((el) => el.textContent === t)!
        .querySelector('.legend-swatch') as HTMLElement;
    expect(swatchFor('System').style.backgroundColor).toBe(rgb(NEPTUNE_NODE_COLORS.System.bg));
    // i3X round-2 types resolve through the palette, not the gray fallback.
    expect(swatchFor('Asset').style.backgroundColor).toBe(rgb(NEPTUNE_NODE_COLORS.Asset.bg));
    expect(swatchFor('MetaNode').style.backgroundColor).toBe(rgb(NEPTUNE_NODE_COLORS.MetaNode.bg));
    goLight();
    expect(swatchFor('System').style.backgroundColor).toBe(
      rgb(NEPTUNE_NODE_COLORS_LIGHT.System.bg),
    );
    expect(swatchFor('Concept').style.backgroundColor).toBe(
      rgb(NEPTUNE_NODE_COLORS_LIGHT.Concept.bg),
    );
    expect(swatchFor('Asset').style.backgroundColor).toBe(
      rgb(NEPTUNE_NODE_COLORS_LIGHT.Asset.bg),
    );
    expect(swatchFor('MetaNode').style.backgroundColor).toBe(
      rgb(NEPTUNE_NODE_COLORS_LIGHT.MetaNode.bg),
    );
  });
});

describe('MapLegend placeholder rows resolve through the palette', () => {
  it('class-based initial rows get theme-resolved fills (css classes no longer carry color)', () => {
    act(() => root.render(<MapLegend />));
    const first = document.querySelector('#map-legend .legend-item .legend-swatch') as HTMLElement;
    // 'Mfg Intelligence' row uses swatchClass 'question'.
    expect(first.style.backgroundColor).toBe(rgb(NODE_COLORS.question.bg));
    goLight();
    const firstLight = document.querySelector(
      '#map-legend .legend-item .legend-swatch',
    ) as HTMLElement;
    expect(firstLight.style.backgroundColor).toBe(rgb(NODE_COLORS_LIGHT.question.bg));
  });
});

describe('no divergent legend hex in css (palette is the single source)', () => {
  it('.legend-swatch rules in dashboard.css/enhancements.css carry no colors', () => {
    for (const f of ['dashboard.css', 'enhancements.css']) {
      const css = readFileSync(
        fileURLToPath(new URL(`../styles/${f}`, import.meta.url)),
        'utf8',
      );
      const rules = css.match(/\.legend-swatch[^{}]*\{[^}]*\}/g) ?? [];
      // dashboard.css keeps SHAPE-only rules; enhancements.css may have none.
      if (f === 'dashboard.css') {
        expect(rules.length, `${f} still has legend-swatch shape rules`).toBeGreaterThan(0);
      }
      for (const rule of rules) {
        expect(rule, `${f}: ${rule}`).not.toMatch(/#[0-9a-fA-F]{3,8}\b/);
        expect(rule, `${f}: ${rule}`).not.toMatch(/background:\s*var\(/);
      }
    }
  });
});
