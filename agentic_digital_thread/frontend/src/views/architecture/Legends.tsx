// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Architecture-view legends (jsx-migration W2.3).
 *
 * Replaces the MAP_LEGEND_INITIAL / ARCH_LEGEND_INITIAL opaque-HTML blobs and
 * the imperative legend.innerHTML= writes in system-map.ts / neptune-graph.ts:
 * the imperative modules now publish legend items to small external stores
 * (lib/store.ts) and these components render them. Contractual ids preserved:
 * #map-legend, #arch-legend, #neptune-legend.
 *
 * Swatch COLORS are derived from the exported palettes in colors.ts (single
 * source — the css .legend-swatch.<type> rules used to duplicate the hex and
 * could drift from the node fills); the classes remain for SHAPE only
 * (border-radius / border-style / clip-path). All three legends subscribe to
 * themeStore, so a live theme toggle re-renders them alongside the graphs. */
import { useStore } from '../../lib/store';
import { invPalette, neptunePalette, themeStore } from './colors';
import { neptuneLegendStore } from './neptune-graph';
import { mapLegendStore } from './system-map';

/** System Map legend (#map-legend) — palette-class swatches before
 * initSystemMap resolves its systems payload, explicit colors after. */
export function MapLegend() {
  const items = useStore(mapLegendStore);
  const isDark = useStore(themeStore);
  const pal = invPalette(isDark);
  return (
    <div className="arch-legend" id="map-legend">
      {items.map((it) => {
        // Pre-resolution placeholder rows carry a palette class instead of
        // explicit colors — resolve it through the same exported palette the
        // graphs use (the css classes no longer carry fills).
        const c = it.bg
          ? { bg: it.bg, border: it.border || '' }
          : it.swatchClass && pal[it.swatchClass]
            ? pal[it.swatchClass]
            : undefined;
        return (
          <div key={it.label} className="legend-item">
            <span
              className={`legend-swatch${it.swatchClass ? ` ${it.swatchClass}` : ''}`}
              style={c ? { background: c.bg, borderColor: c.border } : undefined}
            />
            {it.label}
          </div>
        );
      })}
    </div>
  );
}

const ARCH_LEGEND_ITEMS: [type: string, label: string][] = [
  ['question', 'Question'],
  ['system', 'System'],
  // Purple KG hub node (NODE_COLORS.neptune) — the investigation graph always
  // rendered explore_graph activity purple, but no legend entry explained it
  // (missing from the legacy static markup too).
  ['neptune', 'Knowledge Graph'],
  ['entity', 'Entity'],
  ['finding', 'Data'],
];

/** Investigation legend (#arch-legend) — swatch colors come straight from
 * invPalette (NODE_COLORS / NODE_COLORS_LIGHT), so they match the node fills
 * exactly in both themes; the type class keeps only the shape styling. */
export function ArchLegend() {
  const isDark = useStore(themeStore);
  const pal = invPalette(isDark);
  return (
    <div className="arch-legend" id="arch-legend">
      {ARCH_LEGEND_ITEMS.map(([type, label]) => {
        const c = pal[type] || pal.default;
        return (
          <div key={type} className="legend-item">
            <span
              className={`legend-swatch ${type}`}
              style={{ background: c.bg, borderColor: c.border }}
            />
            {label}
          </div>
        );
      })}
    </div>
  );
}

/** Knowledge-Graph-tab legend (#neptune-legend) — node types published by
 * neptune-graph.ts renders. Labels come from graph data; JSX escapes them
 * (replaces the slEsc'd innerHTML string build). */
export function GraphLegend() {
  const types = useStore(neptuneLegendStore);
  // Theme-resolved swatches — themeStore subscription re-renders the legend
  // on a live toggle (previously it waited for the next graph load).
  const isDark = useStore(themeStore);
  const pal = neptunePalette(isDark);
  return (
    <div className="arch-legend" id="neptune-legend">
      {types.map((t) => {
        const c = pal[t] || pal.default;
        return (
          <div key={t} className="legend-item">
            <span
              className="legend-swatch"
              style={{ background: c.bg, border: `1.5px solid ${c.border}` }}
            />
            {t}
          </div>
        );
      })}
    </div>
  );
}
