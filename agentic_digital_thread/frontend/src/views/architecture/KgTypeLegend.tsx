// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* KgTypeLegend — view-scoped legend toggles for the Graph Explorer panel
 * (UI improvement D). Each row is a visibility checkbox + a live count badge
 * computed from the CURRENT view's dataset (kgCountsStore — published by
 * setCurrentView alongside the render, so counts can never drift from what
 * is drawn). Zero-count types stay rendered, muted; types present in the
 * data but missing from the static list get a data-driven row so the legend
 * can always describe the view. A node label the overview leaves out on
 * purpose (kgNotDrawnStore) shows the graph's real count, marked "not drawn",
 * instead of a bare 0 that would read as "the graph has none"; one whose
 * count could not be read (null) is marked "count unavailable", never 0. */
import { useStore } from '../../lib/store';
import { kgCountsStore, kgHiddenStore, kgNotDrawnStore, toggleType } from './kg-view-store';

export interface KgLegendType {
  value: string;
  label: string;
}

const NO_NOT_DRAWN: Record<string, number | null> = {};

/** What the badge says for a type the view draws none of. */
interface LeftOut {
  badge: string;
  note: string;
  title: string;
}

function leftOutOf(value: number | null | undefined): LeftOut | null {
  if (value === null) {
    return { badge: '?', note: 'count unavailable', title: 'How many the graph holds could not be read' };
  }
  if (typeof value === 'number' && value > 0) {
    return { badge: String(value), note: 'not drawn', title: `${value} in the graph, not drawn in this view` };
  }
  return null;
}

export function KgTypeLegend({
  kind,
  staticTypes,
}: {
  kind: 'nodes' | 'edges';
  staticTypes: KgLegendType[];
}) {
  const counts = useStore(kgCountsStore, (c) => c[kind]);
  const hidden = useStore(kgHiddenStore, (h) => h[kind]);
  const notDrawnStore = useStore(kgNotDrawnStore);
  const notDrawn = kind === 'nodes' ? notDrawnStore : NO_NOT_DRAWN;

  // Static rows first (stable order), then any extra types present in the
  // current view's data, or left out of it with a count to say.
  const staticValues = new Set(staticTypes.map((t) => t.value));
  const leftOutTypes = Object.keys(notDrawn).filter((type) => leftOutOf(notDrawn[type]));
  const rows: KgLegendType[] = [
    ...staticTypes,
    ...[...new Set([...Object.keys(counts), ...leftOutTypes])]
      .filter((type) => !staticValues.has(type))
      .sort()
      .map((type) => ({ value: type, label: type })),
  ];

  return (
    <>
      {rows.map((row) => {
        const count = counts[row.value] || 0;
        const leftOut = count === 0 ? leftOutOf(notDrawn[row.value]) : null;
        return (
          <label
            key={row.value}
            className={`kg-checkbox-item${count === 0 ? ' kg-type-muted' : ''}`}
          >
            <input
              type="checkbox"
              value={row.value}
              checked={!hidden.has(row.value)}
              onChange={() => toggleType(kind, row.value)}
              aria-label={`Show ${row.label}`}
            />
            <span>{row.label}</span>
            {leftOut && <span className="kg-type-note">{leftOut.note}</span>}
            <span className="kg-type-count" title={leftOut?.title}>
              {leftOut ? leftOut.badge : count}
            </span>
          </label>
        );
      })}
    </>
  );
}
