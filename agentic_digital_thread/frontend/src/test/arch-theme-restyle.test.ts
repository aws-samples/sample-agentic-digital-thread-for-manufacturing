// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tests — live theme re-style for the investigation graph.
 *
 * Root cause of the dark-fills-on-light-canvas bug: syncFromStore resolved
 * the palette per sync but only applied bg/border to NEW nodes, and
 * initArchitecture baked the theme into the cytoscape stylesheet once at
 * init — nothing re-styled a LIVE instance on theme toggle.
 *
 * Covered here:
 *  - themeStore (colors.ts) flips when the <html> `light` class mutates
 *    (MutationObserver — works no matter who toggles the class);
 *  - applyInvTheme (inv-graph.ts) rebuilds the stylesheet AND re-maps every
 *    existing node's bg/border data from the theme palette. */
// @vitest-environment jsdom
import cytoscape from 'cytoscape';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import {
  invNodeTextColor,
  NODE_COLORS,
  NODE_COLORS_LIGHT,
  themeStore,
} from '../views/architecture/colors';
import { applyInvTheme, buildInvStyles } from '../views/architecture/inv-graph';

/** Normalize a color to cytoscape's rendered `rgb(r,g,b)` form. */
function rgb(hex: string): string {
  const h = hex.replace('#', '');
  const [r, g, b] = [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16));
  return `rgb(${r},${g},${b})`;
}
function norm(c: string): string {
  return c.startsWith('#') ? rgb(c) : c.replace(/\s+/g, '');
}

const tick = () => new Promise((r) => setTimeout(r, 0));

beforeEach(async () => {
  document.documentElement.classList.remove('light');
  await tick();
});
afterEach(async () => {
  document.documentElement.classList.remove('light');
  await tick();
});

describe('themeStore (colors.ts)', () => {
  it('flips when the <html> light class toggles, regardless of who toggles it', async () => {
    expect(themeStore.get()).toBe(true); // dark = no light class
    document.documentElement.classList.add('light');
    await tick();
    expect(themeStore.get()).toBe(false);
    document.documentElement.classList.remove('light');
    await tick();
    expect(themeStore.get()).toBe(true);
  });
});

describe('applyInvTheme (inv-graph.ts)', () => {
  function darkInstance() {
    return cytoscape({
      headless: true,
      styleEnabled: true,
      style: buildInvStyles(true),
      elements: [
        // Nodes minted under the DARK theme — bg/border data carry dark fills.
        { data: { id: 's1', type: 'system', label: 'MES', fontSize: 15, bg: NODE_COLORS.system.bg, border: NODE_COLORS.system.border } },
        { data: { id: 'f1', type: 'finding', label: '3 rows', fontSize: 14, bg: NODE_COLORS.finding.bg, border: NODE_COLORS.finding.border } },
        { data: { id: 'u1', type: 'mystery', label: '?', fontSize: 14, bg: NODE_COLORS.default.bg, border: NODE_COLORS.default.border } },
      ],
    });
  }

  it('re-maps EXISTING node fills to the light palette (not just new nodes)', () => {
    const inst = darkInstance();
    document.documentElement.classList.add('light');
    applyInvTheme(inst);
    expect(inst.getElementById('s1').data('bg')).toBe(NODE_COLORS_LIGHT.system.bg);
    expect(inst.getElementById('s1').data('border')).toBe(NODE_COLORS_LIGHT.system.border);
    expect(inst.getElementById('f1').data('bg')).toBe(NODE_COLORS_LIGHT.finding.bg);
    // Unknown types resolve to the theme-matched default swatch.
    expect(inst.getElementById('u1').data('bg')).toBe(NODE_COLORS_LIGHT.default.bg);
    // The data-driven stylesheet mapper renders the new fill.
    expect(norm(inst.getElementById('s1').style('background-color'))).toBe(
      norm(NODE_COLORS_LIGHT.system.bg),
    );
    inst.destroy();
  });

  it('rebuilds the stylesheet so label colors follow the theme', () => {
    const inst = darkInstance();
    expect(norm(inst.getElementById('f1').style('color'))).toBe(norm(invNodeTextColor('', true)));
    document.documentElement.classList.add('light');
    applyInvTheme(inst);
    expect(norm(inst.getElementById('f1').style('color'))).toBe(norm(invNodeTextColor('', false)));
    expect(norm(inst.getElementById('s1').style('color'))).toBe(
      norm(invNodeTextColor('system', false)),
    );
    inst.destroy();
  });

  it('round-trips back to the dark palette', () => {
    const inst = darkInstance();
    document.documentElement.classList.add('light');
    applyInvTheme(inst);
    document.documentElement.classList.remove('light');
    applyInvTheme(inst);
    expect(inst.getElementById('s1').data('bg')).toBe(NODE_COLORS.system.bg);
    expect(norm(inst.getElementById('f1').style('color'))).toBe(norm(invNodeTextColor('', true)));
    inst.destroy();
  });

  it('no-ops without an instance', () => {
    expect(() => applyInvTheme(null)).not.toThrow();
  });
});
