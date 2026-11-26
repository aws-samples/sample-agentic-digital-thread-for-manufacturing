// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* ADR-0080 D5 — the "Preview value" action on a PENDING metric definition.
 *
 * Deliberately light: the frame (which reuses the dashboard's KpiCard) is a
 * lazy chunk fetched only on the first click, so neither the admin entry nor
 * the discoveries queue pays for the card component up front. Optional
 * companion proposal ids (`with=`) let a derived definition preview together
 * with its pending inputs. Closing unmounts the frame (the result is
 * discarded); opening again recomputes. There is no accept affordance here —
 * acceptance stays on the proposal card's existing action. */
import { lazy, Suspense, useState } from 'react';

import type { fetchMetricDryRun } from '../../pages/admin/metric-queries';

const LazyMetricDryRunFrame = lazy(() => import('./MetricDryRunFrame'));

export interface MetricDryRunActionProps {
  mappingId: string;
  /** Injected in tests; production uses the preview route with resolve=1. */
  onFetch?: typeof fetchMetricDryRun;
}

/** Comma-separated companion ids → trimmed, de-duplicated, non-empty list
 * (the server re-validates and names any bad id). */
export function parseCompanionIds(raw: string): string[] {
  const seen: string[] = [];
  for (const part of raw.split(',')) {
    const id = part.trim();
    if (id && !seen.includes(id)) seen.push(id);
  }
  return seen;
}

export function MetricDryRunAction({ mappingId, onFetch }: MetricDryRunActionProps) {
  const [open, setOpen] = useState(false);
  const [companions, setCompanions] = useState('');
  const [withIds, setWithIds] = useState<string[]>([]);
  const inputId = `sl-metric-dryrun-with-${mappingId}`;

  const start = () => {
    setWithIds(parseCompanionIds(companions));
    setOpen(true);
  };

  return (
    <div className="sl-metric-dryrun-action" data-testid="sl-metric-dryrun-action">
      {open ? (
        <Suspense fallback={<div className="sl-metric-dryrun-busy">Loading preview…</div>}>
          <LazyMetricDryRunFrame
            mappingId={mappingId}
            withIds={withIds}
            onClose={() => setOpen(false)}
            onFetch={onFetch}
          />
        </Suspense>
      ) : (
        <div style={{ display: 'flex', gap: 6, alignItems: 'center', marginTop: 4 }}>
          <button type="button" className="sl-metric-dryrun-open" onClick={start}>
            Preview value
          </button>
          <label htmlFor={inputId} className="muted" style={{ fontSize: 11 }}>
            with pending ids
          </label>
          <input
            id={inputId}
            className="sl-metric-dryrun-with"
            type="text"
            value={companions}
            placeholder="optional, comma-separated"
            onChange={(e) => setCompanions(e.target.value)}
            style={{ fontSize: 11 }}
          />
        </div>
      )}
    </div>
  );
}
