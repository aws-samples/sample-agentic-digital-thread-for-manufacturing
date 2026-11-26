// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* <SystemDetail/> (jsx-migration W5.1) — the onboarded-system detail screen
 * that replaces the flow (the old showSystemDetail innerHTML swap; back-nav
 * restores the flow-store snapshot). */
import { obRestoreFlow } from '../catalog';
import type { DetailState } from '../flow-store';
import { BusinessContextCard } from './BusinessContextCard';
import { SystemConnections } from './SystemConnections';

const statTile: React.CSSProperties = {
  background: 'var(--surface)',
  border: '1px solid var(--border)',
  borderRadius: 6,
  padding: '0.75rem',
};

const statLabel: React.CSSProperties = {
  fontSize: '0.65rem',
  color: 'var(--muted)',
  textTransform: 'uppercase',
  letterSpacing: '0.05em',
};

const statValue: React.CSSProperties = {
  fontSize: '0.95rem',
  fontWeight: 500,
  marginTop: '0.15rem',
};

const sectionLabel: React.CSSProperties = {
  fontSize: '0.75rem',
  color: 'var(--muted)',
  textTransform: 'uppercase',
  letterSpacing: '0.05em',
  marginBottom: '0.5rem',
};

const th: React.CSSProperties = {
  textAlign: 'left',
  padding: '0.4rem 0.5rem',
  color: 'var(--muted)',
  fontWeight: 500,
};

export function SystemDetail({ detail }: { detail: DetailState }) {
  if (detail.phase === 'loading') {
    return (
      <div style={{ padding: '1.5rem', color: 'var(--muted)' }}>
        Loading system details…
      </div>
    );
  }
  if (detail.phase === 'error') {
    return (
      <div style={{ padding: '1.5rem', color: 'var(--red)' }}>{detail.errorText}</div>
    );
  }
  const { sys, tables, mappings } = detail;
  const totalRows = tables.reduce((s, t) => s + (t.row_count || 0), 0);

  return (
    <div style={{ padding: '1.5rem' }}>
      <button className="ob-back-btn" onClick={() => obRestoreFlow()}>
        ← Back to chat
      </button>
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: '0.5rem',
          marginBottom: '1rem',
        }}
      >
        <h3 style={{ margin: 0 }}>{sys.name}</h3>
        <span
          style={{
            background: 'var(--green)',
            color: '#000',
            padding: '0.15rem 0.5rem',
            borderRadius: 3,
            fontSize: '0.65rem',
            fontWeight: 600,
          }}
        >
          CONNECTED
        </span>
      </div>
      <div
        style={{
          display: 'grid',
          gridTemplateColumns: '1fr 1fr',
          gap: '0.75rem',
          marginBottom: '1.25rem',
        }}
      >
        <div style={statTile}>
          <div style={statLabel}>Type</div>
          <div style={statValue}>{sys.system_type || '—'}</div>
        </div>
        <div style={statTile}>
          <div style={statLabel}>Plant</div>
          <div style={statValue}>{sys.plant || '—'}</div>
        </div>
        <div style={statTile}>
          <div style={statLabel}>Tables</div>
          <div style={statValue}>{tables.length}</div>
        </div>
        <div style={statTile}>
          <div style={statLabel}>Total Rows</div>
          <div style={statValue}>{totalRows.toLocaleString()}</div>
        </div>
      </div>
      <SystemConnections systemId={String(sys.id || sys.system_id || '')} />
      <BusinessContextCard systemId={String(sys.id || sys.system_id || '')} />
      <div style={sectionLabel}>Schema</div>
      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.8rem' }}>
        <thead>
          <tr style={{ borderBottom: '1px solid var(--border)' }}>
            <th style={th}>Table</th>
            <th style={{ ...th, textAlign: 'right' }}>Rows</th>
            <th style={th}>Columns</th>
          </tr>
        </thead>
        <tbody>
          {tables.map((t, i) => (
            <tr key={i}>
              <td style={{ fontWeight: 500 }}>{t.name}</td>
              <td style={{ textAlign: 'right' }}>
                {((t.row_count as number) || 0).toLocaleString()}
              </td>
              <td style={{ color: 'var(--muted)', fontSize: '0.75rem' }}>
                {(t.columns || []).map((c) => c.name).join(', ')}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <div
        style={{
          ...sectionLabel,
          margin: '1.25rem 0 0.5rem',
          paddingTop: '1rem',
          borderTop: '1px solid var(--border)',
        }}
      >
        Field Mappings
      </div>
      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.8rem' }}>
        <thead>
          <tr style={{ borderBottom: '1px solid var(--border)' }}>
            <th style={th}>Field</th>
            <th style={th}>Concept</th>
          </tr>
        </thead>
        <tbody>
          {mappings.length ? (
            mappings.map((m, i) => (
              <tr key={i}>
                <td>{m.field}</td>
                <td>
                  <span className="also-tag" style={{ fontSize: '0.7rem' }}>
                    {m.concept}
                  </span>
                </td>
              </tr>
            ))
          ) : (
            <tr>
              <td colSpan={2} style={{ color: 'var(--muted)' }}>
                No mappings stored
              </td>
            </tr>
          )}
        </tbody>
      </table>
      {/* No "Re-map Fields" here: Re-map was removed for the 2026-10 release.
          Its Save POSTed /api/onboard/provision, the FULL re-provision, which
          deletes every non-steward ir-* IdentityRule and jp-* JoinPath of the
          system, drops the whole SchemaEntry/Field subgraph and recreates
          only the rows sent, and seeds the restarted agent's concept_mappings
          from the mapper's roles, not the steward's pick. A field-only remap
          needs its own server path first: in-place MAPS_TO changes on the
          existing Field nodes, an explicit lifecycle for the rules and join
          paths built on the old concept, and the runtime's concept_mappings
          rebuilt from the confirmed mapping. Its UI must poll the map job
          through runMapJob (job-poll.ts). */}
      <div style={{ marginTop: '0.75rem', fontSize: '0.75rem', color: 'var(--muted)' }}>
        <div
          style={{
            fontFamily: "'IBM Plex Mono',monospace",
            fontSize: '0.7rem',
            wordBreak: 'break-all',
          }}
        >
          {String(sys.schema || sys.connection_string || '')}
        </div>
      </div>
    </div>
  );
}
