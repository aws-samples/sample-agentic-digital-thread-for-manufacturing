// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* ADR-0080 D5 — the dry-run preview of a PENDING metric definition, rendered
 * AS the card it would become.
 *
 * Lazy-loaded (see metric-dry-run.tsx) so the dashboard card component never
 * lands in an entry bundle. The value is computed server-side by the SAME
 * resolver a live card uses; this frame reuses the dashboard's own KpiCard
 * inside an unmistakable "Preview — not accepted" frame with the computed_at
 * timestamp and the window. Nothing here is persisted: the result lives in
 * component state only, closing discards it and re-opening recomputes.
 * Accepting stays on the proposal card's EXISTING action (one door).
 * All text via JSX — no HTML-string sink. */
import { useEffect, useState, type CSSProperties } from 'react';

import { ApiError } from '../../api/client';
import type { DashboardCard } from '../../api/types/dashboard';
import type { MetricDefinitionDryRun, MetricDryRunCost } from '../../api/types/vocabulary';
import { systemName, useSystemNamesVersion } from '../../lib/system-names';
import { fetchMetricDryRun } from '../../pages/admin/metric-queries';
import { KpiCard } from '../dashboard/Cards';

export interface MetricDryRunFrameProps {
  mappingId: string;
  withIds: readonly string[];
  onClose: () => void;
  /** Injected in tests; production uses the preview route with resolve=1. */
  onFetch?: typeof fetchMetricDryRun;
}

const FRAME_STYLE: CSSProperties = {
  border: '1px dashed var(--border, #64748b)',
  borderRadius: 6,
  padding: 8,
  marginTop: 6,
  fontSize: 11,
};

/** The dry-run entry → the DashboardCard the dashboard component renders. */
export function previewCard(dryRun: MetricDefinitionDryRun): DashboardCard {
  const id = dryRun.metric_id || 'unnamed';
  return {
    card_id: 'preview-' + id,
    title: dryRun.label || id,
    viz_type: 'kpi',
    status: dryRun.status,
    metrics: [{ ...dryRun, metric_id: id }],
    drill: null,
  };
}

/** Measured-only cost text (ADR-0053): counts and engine-reported bytes. */
export function costText(cost: MetricDryRunCost | undefined): string {
  if (!cost || !cost.query_count) return 'Measured cost: nothing executed.';
  const parts = (cost.systems || []).map(
    (s) =>
      systemName(s.system_id) +
      ': ' +
      s.query_count +
      (typeof s.scanned_bytes === 'number' ? ', ' + s.scanned_bytes + ' bytes scanned' : ''),
  );
  return (
    'Measured cost: ' +
    cost.query_count +
    (cost.query_count === 1 ? ' value query' : ' value queries') +
    (parts.length ? ' (' + parts.join('; ') + ')' : '') +
    '.'
  );
}

function errorText(error: unknown): string {
  if (error instanceof ApiError && error.detail) return error.detail;
  return error instanceof Error && error.message ? error.message : 'request failed';
}

function DryRunBody({ dryRun }: { dryRun: MetricDefinitionDryRun }) {
  return (
    <>
      <div className="sl-metric-dryrun-meta muted">
        {'computed ' + dryRun.computed_at + ' · window: ' + (dryRun.window || 'none declared')}
      </div>
      <div className="sl-metric-dryrun-status" role="status" data-status={dryRun.status}>
        {'Status: ' + (dryRun.status || 'unknown')}
        {dryRun.missing_inputs && dryRun.missing_inputs.length
          ? ' — missing: ' + dryRun.missing_inputs.join(', ')
          : ''}
      </div>
      <div className="sl-metric-dryrun-card">
        <KpiCard card={previewCard(dryRun)} meta={undefined} />
      </div>
      <div className="sl-metric-dryrun-cost muted">{costText(dryRun.cost)}</div>
    </>
  );
}

interface DryRunState {
  dryRun: MetricDefinitionDryRun | null;
  error: string | null;
  recompute: () => void;
}

/** One dry-run request per (mapping, with-set, recompute) — result held in
 * component state ONLY; a stale response after unmount/change is dropped. */
function useDryRun(
  mappingId: string,
  withIds: readonly string[],
  onFetch: typeof fetchMetricDryRun | undefined,
): DryRunState {
  const [dryRun, setDryRun] = useState<MetricDefinitionDryRun | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [nonce, setNonce] = useState(0);
  const withKey = withIds.join(',');

  useEffect(() => {
    let live = true;
    setDryRun(null);
    setError(null);
    (onFetch ?? fetchMetricDryRun)(mappingId, withKey ? withKey.split(',') : [])
      .then((preview) => {
        if (!live) return;
        if (preview.dry_run) setDryRun(preview.dry_run);
        else setError('the server returned no dry-run block');
      })
      .catch((e: unknown) => {
        if (live) setError(errorText(e));
      });
    return () => {
      live = false;
    };
  }, [mappingId, withKey, nonce, onFetch]);

  return { dryRun, error, recompute: () => setNonce((n) => n + 1) };
}

function DryRunControls({ onRecompute, onClose }: { onRecompute: () => void; onClose: () => void }) {
  return (
    <div style={{ display: 'flex', gap: 6, marginTop: 6 }}>
      <button type="button" className="sl-metric-dryrun-recompute" onClick={onRecompute}>
        Recompute
      </button>
      <button type="button" className="sl-metric-dryrun-close" onClick={onClose}>
        Close preview
      </button>
    </div>
  );
}

export default function MetricDryRunFrame({
  mappingId,
  withIds,
  onClose,
  onFetch,
}: MetricDryRunFrameProps) {
  // The cost line names systems: re-render when catalog names arrive.
  useSystemNamesVersion();
  const { dryRun, error, recompute } = useDryRun(mappingId, withIds, onFetch);
  return (
    <section
      className="sl-metric-dryrun"
      aria-label="Preview — not accepted"
      data-testid="sl-metric-dryrun"
      style={FRAME_STYLE}
    >
      <div className="sl-metric-dryrun-label" style={{ fontWeight: 600 }}>
        Preview — not accepted
      </div>
      {error ? (
        <div className="sl-metric-dryrun-error" role="alert">
          {'Preview unavailable: ' + error + '. Nothing was accepted.'}
        </div>
      ) : dryRun ? (
        <DryRunBody dryRun={dryRun} />
      ) : (
        <div className="sl-metric-dryrun-busy">Computing preview…</div>
      )}
      <DryRunControls onRecompute={recompute} onClose={onClose} />
    </section>
  );
}
