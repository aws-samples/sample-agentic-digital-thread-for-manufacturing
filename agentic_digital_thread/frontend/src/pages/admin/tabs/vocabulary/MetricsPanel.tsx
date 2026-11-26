// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Metric-definitions panel (ADR-0065) — the composed read model over
 * GET /api/admin/vocabulary/metrics: code-defined metrics (read-only),
 * accepted graph definitions, pending proposals, and shadowed graph rows with
 * the reason they were excluded.
 *
 * ONE DOOR: this panel proposes (POST /metrics/propose → PENDING) and never
 * accepts. A formula defines what a number MEANS, so acceptance stays behind
 * the existing proposals-queue review — there is deliberately NO accept or
 * reject control here, and the queue's own diff expander is the review
 * surface. Styles mirror tabs/vocabulary/RetirementPanel.tsx. JSX only — no
 * HTML-string sinks (repo sink policy). */
import { useState, type CSSProperties } from 'react';
import { useQuery } from '@tanstack/react-query';

import { ApiError } from '../../../../api/client';
import type { MetricDefinitionRow } from '../../../../api/types/vocabulary';
import { metricDefinitionsQueryOptions } from '../../metric-queries';
import { MetricDryRunAction } from '../../../../views/architecture/metric-dry-run';
import MetricDefinitionModal from './MetricDefinitionModal';

const h3Style: CSSProperties = { margin: '0 0 0.5rem', fontSize: '0.95rem', fontWeight: 600 };
const sectionHelpStyle: CSSProperties = {
  fontSize: '0.72rem',
  color: 'var(--muted)',
  lineHeight: 1.5,
  marginBottom: '1rem',
};
const statusStyle: CSSProperties = { fontSize: '0.75rem', color: 'var(--muted)' };
const thStyle: CSSProperties = {
  textAlign: 'left',
  padding: '0.4rem 0.75rem',
  background: 'var(--surface2)',
  color: 'var(--text)',
  fontWeight: 600,
  borderBottom: '1px solid var(--border)',
  fontSize: '0.72rem',
  whiteSpace: 'nowrap',
};
const tdStyle: CSSProperties = {
  padding: '0.4rem 0.75rem',
  borderBottom: '1px solid var(--border)',
  color: 'var(--text2)',
  fontSize: '0.75rem',
  verticalAlign: 'top',
};
const mutedMono: CSSProperties = {
  color: 'var(--muted)',
  fontFamily: "'IBM Plex Mono',monospace",
  fontSize: '0.72rem',
};
const addBtnStyle: CSSProperties = {
  background: 'var(--accent)',
  color: '#000',
  fontWeight: 600,
  padding: '0.4rem 1rem',
  border: 'none',
  borderRadius: 4,
  fontSize: '0.8rem',
  cursor: 'pointer',
};

/** The four visible provenance classes and how each reads to a steward.
 * `code` is registry-defined and cannot be edited from the UI; `shadowed`
 * always carries the store's reason so an excluded row is never silent. */
const STATUS_META: Record<string, { label: string; color: string; note: string }> = {
  code: {
    label: 'Code · read-only',
    color: 'var(--muted)',
    note: 'Defined in the metric registry — this panel cannot change it.',
  },
  accepted: {
    label: 'Accepted',
    color: 'var(--green, #4ade80)',
    note: 'Live graph definition — answers resolve through it.',
  },
  pending: {
    label: 'Pending review',
    color: 'var(--amber, #fbbf24)',
    note: 'Filed for steward review — not resolving yet.',
  },
  shadowed: {
    label: 'Shadowed',
    color: 'var(--red, #f87171)',
    note: 'Excluded from the metric layer.',
  },
};

function errorDetail(error: unknown): string {
  return error instanceof ApiError ? error.detail : error instanceof Error ? error.message : String(error);
}

function StatusCell({ row }: { row: MetricDefinitionRow }) {
  const status = String(row.status || '');
  const meta = STATUS_META[status] ?? {
    label: status || 'unknown',
    color: 'var(--muted)',
    note: '',
  };
  const reason = String(row.reason || '');
  return (
    <>
      <span
        className={`vocab-metric-status vocab-metric-status--${status || 'unknown'}`}
        data-metric-status={status}
        title={meta.note}
        style={{ color: meta.color, fontWeight: 600, whiteSpace: 'nowrap' }}
      >
        {meta.label}
      </span>
      {reason && (
        // The reason a row is shadowed (or a pending row failed to parse) is
        // the whole point of showing it — never hidden behind a hover.
        <div className="vocab-metric-reason" style={{ ...mutedMono, marginTop: '0.2rem' }}>
          {reason}
        </div>
      )}
    </>
  );
}

function MetricRow({ row }: { row: MetricDefinitionRow }) {
  return (
    <tr data-metric-id={row.id} data-metric-status={String(row.status || '')}>
      <td style={tdStyle}>
        <div style={{ fontWeight: 600, color: 'var(--text)' }}>{row.label || row.id}</div>
        <div style={mutedMono}>{row.id}</div>
      </td>
      <td style={tdStyle}>
        {row.formula ? (
          <code className="vocab-metric-formula" style={mutedMono}>
            {row.formula}
          </code>
        ) : (
          <span style={mutedMono}>—</span>
        )}
        {row.unit ? <div style={mutedMono}>unit: {row.unit}</div> : null}
      </td>
      <td style={tdStyle}>{row.kind || '—'}</td>
      <td style={tdStyle}>
        <StatusCell row={row} />
        {row.status === 'pending' && row.mapping_id ? (
          <MetricDryRunAction mappingId={row.mapping_id} />
        ) : null}
      </td>
    </tr>
  );
}

function MetricTable({ rows }: { rows: MetricDefinitionRow[] }) {
  return (
    <table id="vocab-metrics-table" style={{ width: '100%', borderCollapse: 'collapse' }}>
      <thead>
        <tr>
          <th style={thStyle}>Metric</th>
          <th style={thStyle}>Formula</th>
          <th style={thStyle}>Kind</th>
          <th style={thStyle}>Status</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <MetricRow key={`${row.status}-${row.id}-${row.mapping_id ?? ''}`} row={row} />
        ))}
      </tbody>
    </table>
  );
}

export default function MetricsPanel() {
  const metricsQuery = useQuery(metricDefinitionsQueryOptions());
  const [modalOpen, setModalOpen] = useState(false);
  const rows = metricsQuery.data?.metrics ?? [];

  return (
    <div id="vocab-metrics-panel">
      <h3 style={h3Style}>Metric definitions</h3>
      <p style={sectionHelpStyle}>
        Every metric the orchestrator can resolve: the ones defined in code (read-only), the ones
        accepted into the graph, the ones still awaiting review, and graph rows that were shadowed
        — with the reason each was excluded. A code-defined metric always wins over a graph row of
        the same id. Proposing a definition files it for review; accepting or rejecting happens on
        the discoveries review queue, where the formula diff is shown.
      </p>
      <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem', marginBottom: '0.75rem' }}>
        <button
          id="vocab-metric-propose-btn"
          type="button"
          style={addBtnStyle}
          onClick={() => setModalOpen(true)}
        >
          + Propose metric definition
        </button>
        {metricsQuery.isSuccess && (
          <span style={{ ...statusStyle, marginLeft: 'auto' }}>
            {metricsQuery.data?.total ?? rows.length} definitions
          </span>
        )}
      </div>

      {metricsQuery.isPending ? (
        <div style={statusStyle}>Loading metric definitions…</div>
      ) : metricsQuery.isError ? (
        <div role="alert" style={{ ...statusStyle, color: 'var(--red, #f87171)' }}>
          Metric definitions unavailable: {errorDetail(metricsQuery.error)}{' '}
          <button type="button" onClick={() => void metricsQuery.refetch()}>
            Retry
          </button>
        </div>
      ) : rows.length === 0 ? (
        <div className="vocab-metrics-empty" style={statusStyle}>
          No metric definitions are visible.
        </div>
      ) : (
        <MetricTable rows={rows} />
      )}

      {modalOpen && <MetricDefinitionModal onClose={() => setModalOpen(false)} />}
    </div>
  );
}
