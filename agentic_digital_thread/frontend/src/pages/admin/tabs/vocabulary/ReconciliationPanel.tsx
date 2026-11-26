// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useQuery } from '@tanstack/react-query';

import type { GuidanceReconciliationResult } from '../../../../api/types/proposals';
import { guidanceReconciliationQueryOptions } from '../../guidance-queries';
import {
  AMBER,
  AuthorityTag,
  compactStyle,
  MONO,
  numberOrUnavailable,
  panelStyle,
  RED,
  type AuthorityState,
} from './guidance-stability-ui';

export function reconciliationState(data: GuidanceReconciliationResult): AuthorityState {
  const { accepted, graph, delivered } = data.counts;
  if (data.status === 'unavailable') return 'UNAVAILABLE';
  if (data.status === 'mismatch' || data.mismatch_count > 0) return 'PENDING';
  if (accepted === 0 && graph === 0 && delivered === 0) return 'ZERO';
  const strict = data.status === 'ok'
    && data.sources.accepted === 'ok'
    && data.sources.graph === 'ok'
    && data.sources.delivery === 'ok'
    && accepted !== null
    && accepted > 0
    && accepted === graph
    && graph === delivered;
  return strict ? 'DELIVERED' : 'UNAVAILABLE';
}

function ReconciliationMismatches({ data }: { data: GuidanceReconciliationResult }) {
  if (data.mismatch_count === 0) return null;
  return (
    <div style={{ marginTop: '0.45rem' }}>
      <strong style={{ ...compactStyle, color: AMBER }}>
        {data.mismatch_count} opaque mismatch{data.mismatch_count === 1 ? '' : 'es'}
      </strong>
      <ol className="guidance-reconciliation-mismatches" style={{ ...compactStyle, fontFamily: MONO, margin: '0.25rem 0 0', paddingLeft: '1.25rem' }}>
        {data.mismatches.map((item, index) => (
          <li key={`${item.system_digest}-${item.note_digest}-${item.kind}-${index}`}>
            {item.kind}: {item.system_digest} / {item.note_digest}
          </li>
        ))}
      </ol>
      {data.truncated && <div style={compactStyle}>Mismatch list truncated by the server.</div>}
    </div>
  );
}

function ReconciliationReport({ data }: { data: GuidanceReconciliationResult }) {
  return (
    <section className="guidance-reconciliation" aria-label="Read-only guidance reconciliation" style={panelStyle}>
      <div style={{ display: 'flex', alignItems: 'center', gap: '0.45rem' }}>
        <strong style={{ fontSize: '0.78rem' }}>Delivery reconciliation</strong>
        <AuthorityTag state={reconciliationState(data)} />
        <span style={compactStyle}>read-only</span>
      </div>
      <div className="guidance-reconciliation-counts" style={{ ...compactStyle, marginTop: '0.35rem', fontFamily: MONO }}>
        accepted {numberOrUnavailable(data.counts.accepted)} · graph {numberOrUnavailable(data.counts.graph)} · delivered {numberOrUnavailable(data.counts.delivered)}
      </div>
      <div style={{ ...compactStyle, marginTop: '0.2rem' }}>
        sources: accepted {data.sources.accepted} · graph {data.sources.graph} · delivery {data.sources.delivery}
      </div>
      <ReconciliationMismatches data={data} />
    </section>
  );
}

export function ReconciliationPanel({ enabled }: { enabled: boolean }) {
  const query = useQuery(guidanceReconciliationQueryOptions(enabled));
  if (!enabled) return null;
  if (query.isPending) {
    return <div style={panelStyle}><AuthorityTag state="PREVIEW" /> <span style={compactStyle}>Reading delivery reconciliation…</span></div>;
  }
  if (query.isError) {
    return (
      <div className="guidance-reconciliation" style={panelStyle} role="alert">
        <AuthorityTag state="UNAVAILABLE" />{' '}
        <span style={{ ...compactStyle, color: RED }}>Reconciliation is unavailable. Try again.</span>
      </div>
    );
  }
  return <ReconciliationReport data={query.data} />;
}
