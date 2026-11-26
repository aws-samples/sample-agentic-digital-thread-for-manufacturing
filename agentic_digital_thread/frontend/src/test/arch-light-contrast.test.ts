// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tests — light-mode graph contrast (arch-visual-polish).
 *
 * The light palettes must (a) actually differ from the dark ones — light mode
 * used to fall back to dark swatches for inv-graph `schema`/unknown types and
 * for the ENTIRE Neptune KG palette — and (b) meet WCAG AA: every label color
 * reads ≥4.5:1 against its node fill in BOTH themes. Contrast is computed
 * here with the standard WCAG relative-luminance formula. */
import { describe, expect, it } from 'vitest';

import {
  invNodeTextColor,
  invPalette,
  neptunePalette,
  NEPTUNE_NODE_COLORS,
  NEPTUNE_NODE_COLORS_LIGHT,
  NODE_COLORS,
  NODE_COLORS_LIGHT,
} from '../views/architecture/colors';

/* WCAG 2.x relative luminance + contrast ratio. */
function luminance(hex: string): number {
  const h = hex.replace('#', '');
  const [r, g, b] = [0, 2, 4]
    .map((i) => parseInt(h.slice(i, i + 2), 16) / 255)
    .map((c) => (c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4)));
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}
function contrast(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

const AA = 4.5;

describe('investigation-graph light palette (NODE_COLORS_LIGHT)', () => {
  it('covers every dark-palette node type (schema was missing → dark fallback)', () => {
    for (const type of Object.keys(NODE_COLORS)) {
      expect(NODE_COLORS_LIGHT[type], `light swatch for '${type}'`).toBeDefined();
    }
  });

  it('light fills differ from dark fills for every type', () => {
    for (const [type, dark] of Object.entries(NODE_COLORS)) {
      const light = NODE_COLORS_LIGHT[type];
      expect(light.bg.toLowerCase(), `light bg for '${type}'`).not.toBe(dark.bg.toLowerCase());
    }
  });

  it('label-on-fill contrast is ≥4.5:1 for every type in the LIGHT theme', () => {
    for (const [type, swatch] of Object.entries(NODE_COLORS_LIGHT)) {
      const text = invNodeTextColor(type, false);
      const ratio = contrast(text, swatch.bg);
      expect(ratio, `light '${type}': ${text} on ${swatch.bg} = ${ratio.toFixed(2)}:1`).toBeGreaterThanOrEqual(AA);
    }
  });

  it('label-on-fill contrast is ≥4.5:1 for every type in the DARK theme (unchanged)', () => {
    for (const [type, swatch] of Object.entries(NODE_COLORS)) {
      const text = invNodeTextColor(type, true);
      const ratio = contrast(text, swatch.bg);
      expect(ratio, `dark '${type}': ${text} on ${swatch.bg} = ${ratio.toFixed(2)}:1`).toBeGreaterThanOrEqual(AA);
    }
  });

  it('system label is NOT white on the light cream fill (the 1.1:1 regression)', () => {
    expect(invNodeTextColor('system', false).toLowerCase()).not.toBe('#ffffff');
    expect(invNodeTextColor('system', true).toLowerCase()).toBe('#ffffff'); // dark untouched
  });

  it('invPalette resolves by theme (single lookup for graph + legend)', () => {
    expect(invPalette(true)).toBe(NODE_COLORS);
    expect(invPalette(false)).toBe(NODE_COLORS_LIGHT);
  });
});

describe('Neptune KG light palette (NEPTUNE_NODE_COLORS_LIGHT)', () => {
  it('covers every dark type and differs from the dark palette', () => {
    for (const [type, dark] of Object.entries(NEPTUNE_NODE_COLORS)) {
      const light = NEPTUNE_NODE_COLORS_LIGHT[type];
      expect(light, `light swatch for '${type}'`).toBeDefined();
      expect(light.bg.toLowerCase(), `light bg for '${type}'`).not.toBe(dark.bg.toLowerCase());
      expect(light.text.toLowerCase(), `light text for '${type}'`).not.toBe(dark.text.toLowerCase());
    }
  });

  it('text-on-fill contrast is ≥4.5:1 for every type in BOTH themes', () => {
    for (const [type, s] of Object.entries(NEPTUNE_NODE_COLORS_LIGHT)) {
      const ratio = contrast(s.text, s.bg);
      expect(ratio, `light '${type}': ${s.text} on ${s.bg} = ${ratio.toFixed(2)}:1`).toBeGreaterThanOrEqual(AA);
    }
    for (const [type, s] of Object.entries(NEPTUNE_NODE_COLORS)) {
      const ratio = contrast(s.text, s.bg);
      expect(ratio, `dark '${type}': ${s.text} on ${s.bg} = ${ratio.toFixed(2)}:1`).toBeGreaterThanOrEqual(AA);
    }
  });

  it('neptunePalette resolves by theme', () => {
    expect(neptunePalette(true)).toBe(NEPTUNE_NODE_COLORS);
    expect(neptunePalette(false)).toBe(NEPTUNE_NODE_COLORS_LIGHT);
  });

  it('i3X round-2 types (Asset/AssetModel/MetaNode/MetaEdge) are present in BOTH palettes', () => {
    // These previously fell through to `default` gray. The
    // iteration tests above then enforce the contrast + light≠dark rules.
    for (const type of ['Asset', 'AssetModel', 'MetaNode', 'MetaEdge']) {
      expect(NEPTUNE_NODE_COLORS[type], `dark swatch for '${type}'`).toBeDefined();
      expect(NEPTUNE_NODE_COLORS_LIGHT[type], `light swatch for '${type}'`).toBeDefined();
      // Not the shared fallback — the whole point of the port.
      expect(NEPTUNE_NODE_COLORS[type].bg).not.toBe(NEPTUNE_NODE_COLORS.default.bg);
      expect(NEPTUNE_NODE_COLORS_LIGHT[type].bg).not.toBe(NEPTUNE_NODE_COLORS_LIGHT.default.bg);
    }
    // Dark swatches are the reference verbatim values (pinned).
    expect(NEPTUNE_NODE_COLORS.Asset).toEqual({ bg: '#152238', border: '#818cf8', text: '#c7d2fe' });
    expect(NEPTUNE_NODE_COLORS.AssetModel).toEqual({ bg: '#1c1a2e', border: '#6366f1', text: '#a5b4fc' });
    expect(NEPTUNE_NODE_COLORS.MetaNode).toEqual({ bg: '#26201a', border: '#d97706', text: '#fbbf24' });
    expect(NEPTUNE_NODE_COLORS.MetaEdge).toEqual({ bg: '#26201a', border: '#b45309', text: '#f59e0b' });
  });

  it('light text-on-fill for the i3X round-2 types meets the file’s ≥6.3:1 rule (stricter than AA)', () => {
    for (const type of ['Asset', 'AssetModel', 'MetaNode', 'MetaEdge']) {
      const s = NEPTUNE_NODE_COLORS_LIGHT[type];
      const ratio = contrast(s.text, s.bg);
      expect(ratio, `light '${type}': ${s.text} on ${s.bg} = ${ratio.toFixed(2)}:1`).toBeGreaterThanOrEqual(6.3);
    }
  });
});
