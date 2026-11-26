// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Architecture-view palettes
 * (MAP_COLORS / MAP_COLORS_LIGHT / ISA_* / NEPTUNE_NODE_COLORS and the
 * chat NODE_COLORS / NODE_COLORS_LIGHT). */
import { createStore } from '../../lib/store';

export interface Swatch {
  bg: string;
  border: string;
}

/* Investigation-graph node palette (chat.js:947-966). */
export const NODE_COLORS: Record<string, Swatch> = {
  question: { bg: '#00d8ff', border: '#00d8ff' },
  system: { bg: '#2a2210', border: '#d29922' },
  schema: { bg: '#1a2e1a', border: '#22c55e' },
  neptune: { bg: '#1a0a2e', border: '#bc8cff' },
  entity: { bg: '#111d2e', border: '#58a6ff' },
  finding: { bg: '#0d2818', border: '#3fb950' },
  // Blue per-request "input/question" node: the specific question posed to a
  // system, sitting between the system node and that request's result so each
  // result is traceable to the exact input. Blue to echo the root question.
  qinput: { bg: '#0d3a66', border: '#00d8ff' },
  // Fallback for unknown node types (formerly hardcoded in inv-graph's
  // syncFromStore) — hoisted here so the light palette can override it.
  default: { bg: '#1e293b', border: '#475569' },
};
export const NODE_COLORS_LIGHT: Record<string, Swatch> = {
  question: { bg: '#0969da', border: '#0969da' },
  system: { bg: '#fef3c7', border: '#d29922' },
  // Light-contrast audit (arch-visual-polish): `schema` was MISSING here, so
  // light mode fell back to the dark slate swatch (#1e293b fill under the
  // default #1e293b label text — 1.0:1, an unreadable dark blob on the light
  // canvas). Emerald-100 fill keeps the green schema family; the default
  // light label (#1e293b) reads at 12.9:1.
  schema: { bg: '#d1fae5', border: '#16a34a' },
  neptune: { bg: '#f3e8ff', border: '#8b5cf6' },
  entity: { bg: '#dbeafe', border: '#3b82f6' },
  finding: { bg: '#dcfce7', border: '#22c55e' },
  qinput: { bg: '#0969da', border: '#0969da' },
  // Light fallback for unknown types — light fill + slate border (the old
  // shared fallback was the DARK slate swatch in both themes). 11.9:1 under
  // the default light label text.
  default: { bg: '#e2e8f0', border: '#64748b' },
};

/** Theme-resolved investigation-graph palette — single lookup shared by the
 * graph renderer (inv-graph.ts) and the investigation legend (Legends.tsx
 * ArchLegend), so the legend swatches and the node fills can never diverge. */
export function invPalette(isDark: boolean): Record<string, Swatch> {
  return isDark ? NODE_COLORS : NODE_COLORS_LIGHT;
}

/** Investigation-graph label color for a node type per theme. The light
 * branch fixes the two WCAG failures found in the arch-visual-polish audit:
 * `system` labels were hardcoded '#ffffff' regardless of theme (1.1:1 on the
 * light #fef3c7 fill — invisible); everything else inherits the base label
 * color. Ratios (light): question/qinput 5.2:1 (white on #0969da), system
 * 7.8:1 (#713f12 on #fef3c7), others ≥11.9:1 (#1e293b on 100-level fills). */
export function invNodeTextColor(type: string, isDark: boolean): string {
  if (type === 'question') return isDark ? '#0d1117' : '#ffffff';
  if (type === 'qinput') return '#ffffff'; // deep-blue fill in BOTH themes
  if (type === 'system') return isDark ? '#ffffff' : '#713f12';
  return isDark ? '#e2e8f0' : '#1e293b';
}

/* System-map (ISA-95) palette + levels (architecture.js:39-57). */
export const ISA_LEVEL: Record<string, number> = { ERP: 4, PLM: 4, MES: 3, CMMS: 3, Historian: 2 };
export const ISA_LABEL: Record<number, string> = {
  4: 'Level 4 · Business Planning',
  3: 'Level 3 · Manufacturing Ops',
  2: 'Level 2 · Supervisory Control',
};

export const MAP_COLORS: Record<string | number, Swatch> = {
  orchestrator: { bg: '#0a2a3a', border: '#00d8ff' },
  neptune: { bg: '#1a0a2e', border: '#bc8cff' },
  4: { bg: '#2a2210', border: '#d29922' },
  3: { bg: '#0d2818', border: '#3fb950' },
  2: { bg: '#111d2e', border: '#58a6ff' },
};
export const MAP_COLORS_LIGHT: Record<string | number, Swatch> = {
  orchestrator: { bg: '#e0f7ff', border: '#0969da' },
  neptune: { bg: '#f3e8ff', border: '#8b5cf6' },
  4: { bg: '#fef3c7', border: '#d29922' },
  3: { bg: '#dcfce7', border: '#22c55e' },
  2: { bg: '#dbeafe', border: '#3b82f6' },
};

/* Neptune knowledge-graph node palette (architecture.js:1385-1398). */
export interface NeptuneSwatch extends Swatch {
  text: string;
}

export const NEPTUNE_NODE_COLORS: Record<string, NeptuneSwatch> = {
  System: { bg: '#1e3a5f', border: '#3b82f6', text: '#93c5fd' },
  SchemaEntry: { bg: '#1a2e1a', border: '#22c55e', text: '#86efac' },
  Field: { bg: '#1a2a1a', border: '#16a34a', text: '#4ade80' },
  Concept: { bg: '#2d1b4e', border: '#a855f7', text: '#d8b4fe' },
  IdentityRule: { bg: '#3b1f1f', border: '#ef4444', text: '#fca5a5' },
  JoinPath: { bg: '#1f2d3b', border: '#06b6d4', text: '#67e8f9' },
  ValueMapping: { bg: '#2d2a1a', border: '#f59e0b', text: '#fcd34d' },
  AuthorityRule: { bg: '#1f2d1f', border: '#10b981', text: '#6ee7b7' },
  Correlation: { bg: '#2d1f2d', border: '#ec4899', text: '#f9a8d4' },
  Plant: { bg: '#1f2d3b', border: '#38bdf8', text: '#7dd3fc' },
  // i3X round-2 (ref architecture.js:1069-1072): telemetry hierarchy nodes
  // (Asset/AssetModel — indigo family) and the self-describing metamodel
  // vocabulary nodes (MetaNode/MetaEdge — amber family, shared fill with
  // distinct border/text). Previously these fell through to `default` gray.
  Asset: { bg: '#152238', border: '#818cf8', text: '#c7d2fe' },
  AssetModel: { bg: '#1c1a2e', border: '#6366f1', text: '#a5b4fc' },
  MetaNode: { bg: '#26201a', border: '#d97706', text: '#fbbf24' },
  MetaEdge: { bg: '#26201a', border: '#b45309', text: '#f59e0b' },
  default: { bg: '#1e293b', border: '#475569', text: '#94a3b8' },
};

/* Light-theme Neptune palette (arch-visual-polish): the KG tab previously
 * rendered the DARK swatches on the light canvas — dark navy/forest fills
 * with pale 300-level label text designed for dark mode. Same hue family per
 * type, flipped to 50/100-level fills with 700/800-level text; every
 * text-on-fill pair is ≥6.3:1 (WCAG AA for the 9–14px cytoscape labels). */
export const NEPTUNE_NODE_COLORS_LIGHT: Record<string, NeptuneSwatch> = {
  System: { bg: '#dbeafe', border: '#3b82f6', text: '#1e40af' },
  SchemaEntry: { bg: '#dcfce7', border: '#22c55e', text: '#166534' },
  Field: { bg: '#ecfdf5', border: '#16a34a', text: '#065f46' },
  Concept: { bg: '#f3e8ff', border: '#a855f7', text: '#6b21a8' },
  IdentityRule: { bg: '#fee2e2', border: '#ef4444', text: '#991b1b' },
  JoinPath: { bg: '#cffafe', border: '#06b6d4', text: '#155e75' },
  ValueMapping: { bg: '#fef3c7', border: '#f59e0b', text: '#92400e' },
  AuthorityRule: { bg: '#d1fae5', border: '#10b981', text: '#065f46' },
  Correlation: { bg: '#fce7f3', border: '#ec4899', text: '#9d174d' },
  Plant: { bg: '#e0f2fe', border: '#38bdf8', text: '#075985' },
  // Light counterparts for the i3X round-2 types — same hue families flipped
  // to 50/100-level fills with 800/900-level text. Ratios: Asset 8.0:1,
  // AssetModel 10.2:1, MetaNode 6.8:1, MetaEdge 8.1:1 (all ≥6.3:1).
  Asset: { bg: '#e0e7ff', border: '#818cf8', text: '#3730a3' },
  AssetModel: { bg: '#eef2ff', border: '#6366f1', text: '#312e81' },
  MetaNode: { bg: '#fffbeb', border: '#d97706', text: '#92400e' },
  MetaEdge: { bg: '#fef3c7', border: '#b45309', text: '#78350f' },
  default: { bg: '#e2e8f0', border: '#64748b', text: '#334155' },
};

/** Theme-resolved Neptune palette — single lookup shared by the KG graph
 * renderer (neptune-graph.ts) and the KG legend (Legends.tsx GraphLegend). */
export function neptunePalette(isDark: boolean): Record<string, NeptuneSwatch> {
  return isDark ? NEPTUNE_NODE_COLORS : NEPTUNE_NODE_COLORS_LIGHT;
}

/* Cytoscape highlight/selection accents (dark/light) — shared by the
 * investigation graph (inv-graph.ts) and the system map (system-map.ts);
 * hoisted from the repeated '#00d8ff'/'#0969da' literals in both files. */
export const HIGHLIGHT_DARK = '#00d8ff';
export const HIGHLIGHT_LIGHT = '#0969da';

/** Dark mode = documentElement does NOT carry the `light` class. */
export function isDarkTheme(): boolean {
  return !document.documentElement.classList.contains('light');
}

/* ── Live theme store ─────────────────────────────────────────────────────
 * useTheme (hooks/useTheme.ts) toggles the `light` class on <html> but its
 * state is per-hook-instance — imperative modules holding live cytoscape
 * instances (inv-graph.ts) and the legend components need a subscribable
 * signal. A MutationObserver on the <html> class attribute feeds this store,
 * so it works no matter WHO toggles the class (UserMenu, AdminPage, the
 * anti-flash inline script). Value: isDark. */
export const themeStore = createStore<boolean>(
  typeof document !== 'undefined' ? isDarkTheme() : true,
);

if (typeof document !== 'undefined' && typeof MutationObserver !== 'undefined') {
  new MutationObserver(() => themeStore.set(isDarkTheme())).observe(
    document.documentElement,
    { attributes: true, attributeFilter: ['class'] },
  );
}
