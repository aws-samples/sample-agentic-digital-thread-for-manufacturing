// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Unified concept list for the Vocabulary admin tab: search + provenance-layer
 * filter over GET /api/admin/vocabulary/concepts rows. Columns: name, id
 * (muted), provenance chip, mapped-field count, 30-day usage, status/actions.
 * Visual language follows tabs/SettingsTab.tsx (inline styles on the shared
 * CSS vars). JSX only — no HTML-string sinks. */
import { useMemo, useState, type CSSProperties } from 'react';

import type { VocabularyConcept } from '../../../../api/types/vocabulary';

export interface ConceptTableProps {
  concepts: VocabularyConcept[];
  onEdit: (concept: VocabularyConcept) => void;
  onRetire: (concept: VocabularyConcept) => void;
  /** Concept id with a retire request in flight (disables its button). */
  retirePendingId?: string | null;
}

const searchInputStyle: CSSProperties = {
  flex: 1,
  minWidth: 180,
  padding: '0.5rem 0.75rem',
  background: 'var(--surface2)',
  border: '1px solid var(--border)',
  borderRadius: 4,
  color: 'var(--text)',
  fontSize: '0.85rem',
  boxSizing: 'border-box',
};
const selectStyle: CSSProperties = { ...searchInputStyle, flex: 'none', width: 'auto', minWidth: 140 };
const thStyle: CSSProperties = {
  textAlign: 'left',
  padding: '0.5rem 0.75rem',
  background: 'var(--surface2)',
  color: 'var(--text)',
  fontWeight: 600,
  borderBottom: '1px solid var(--border)',
  fontSize: '0.75rem',
  whiteSpace: 'nowrap',
};
const tdStyle: CSSProperties = {
  padding: '0.5rem 0.75rem',
  borderBottom: '1px solid var(--border)',
  color: 'var(--text2)',
  fontSize: '0.8rem',
  verticalAlign: 'top',
};
const mutedMono: CSSProperties = {
  color: 'var(--muted)',
  fontFamily: "'IBM Plex Mono',monospace",
  fontSize: '0.72rem',
};
const smallBtnStyle: CSSProperties = {
  background: 'var(--surface2)',
  color: 'var(--text)',
  border: '1px solid var(--border)',
  padding: '0.25rem 0.6rem',
  borderRadius: 4,
  fontSize: '0.72rem',
  cursor: 'pointer',
  fontWeight: 600,
};

/** Provenance chip color by layer family (chips are informational, not actions). */
function chipColors(label: string): { bg: string; fg: string } {
  if (label === 'core') return { bg: 'var(--accent)', fg: '#000' };
  if (label === 'minted') return { bg: 'var(--surface2)', fg: 'var(--accent)' };
  if (label.startsWith('pack:custom:')) return { bg: 'var(--surface2)', fg: 'var(--green, #4ade80)' };
  if (label.startsWith('pack:')) return { bg: 'var(--surface2)', fg: 'var(--text)' };
  return { bg: 'var(--surface2)', fg: 'var(--muted)' }; // custom / unknown
}

function ProvenanceChip({ label }: { label: string }) {
  const { bg, fg } = chipColors(label);
  return (
    <span
      className="vocab-provenance-chip"
      style={{
        display: 'inline-block',
        padding: '0.1rem 0.5rem',
        borderRadius: 999,
        border: '1px solid var(--border)',
        background: bg,
        color: fg,
        fontSize: '0.68rem',
        fontWeight: 600,
        whiteSpace: 'nowrap',
      }}
    >
      {label}
    </span>
  );
}

/** "used in K answers (30d)" + last-used, or an honest em dash when unused. */
export function usageText(c: VocabularyConcept): string {
  const count = c.usage_30d ?? 0;
  if (!count) return '—';
  const base = `used in ${count} answer${count === 1 ? '' : 's'} (30d)`;
  if (!c.last_used_at) return base;
  const when = new Date(c.last_used_at);
  return Number.isNaN(when.getTime()) ? base : `${base} · last ${when.toLocaleDateString()}`;
}

function matchesSearch(c: VocabularyConcept, needle: string): boolean {
  if (!needle) return true;
  const hay = [c.name, c.id, ...(c.synonyms || [])].join(' ').toLowerCase();
  return hay.includes(needle);
}

export default function ConceptTable({ concepts, onEdit, onRetire, retirePendingId }: ConceptTableProps) {
  const [search, setSearch] = useState('');
  const [layerFilter, setLayerFilter] = useState('all');

  const layers = useMemo(() => {
    const seen = new Set<string>();
    for (const c of concepts) seen.add(c.provenance_label || 'custom');
    return Array.from(seen).sort();
  }, [concepts]);

  const visible = useMemo(() => {
    const needle = search.trim().toLowerCase();
    return concepts.filter(
      (c) =>
        matchesSearch(c, needle) &&
        (layerFilter === 'all' || (c.provenance_label || 'custom') === layerFilter),
    );
  }, [concepts, search, layerFilter]);

  return (
    <div>
      <div style={{ display: 'flex', gap: '0.5rem', marginBottom: '0.75rem', flexWrap: 'wrap' }}>
        <input
          id="vocab-search"
          type="search"
          placeholder="Search by name, id, or synonym…"
          aria-label="Search concepts"
          style={searchInputStyle}
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <select
          id="vocab-layer-filter"
          aria-label="Filter by provenance layer"
          style={selectStyle}
          value={layerFilter}
          onChange={(e) => setLayerFilter(e.target.value)}
        >
          <option value="all">All layers</option>
          {layers.map((layer) => (
            <option key={layer} value={layer}>
              {layer}
            </option>
          ))}
        </select>
      </div>

      <table id="vocab-concept-table" style={{ width: '100%', borderCollapse: 'collapse' }}>
        <thead>
          <tr>
            <th style={thStyle}>Concept</th>
            <th style={thStyle}>Provenance</th>
            <th style={thStyle}>Mapped fields</th>
            <th style={thStyle}>Usage</th>
            <th style={thStyle}>Actions</th>
          </tr>
        </thead>
        <tbody>
          {visible.length === 0 ? (
            <tr>
              <td colSpan={5} style={{ ...tdStyle, color: 'var(--muted)', textAlign: 'center', padding: '1.5rem' }}>
                No concepts match.
              </td>
            </tr>
          ) : (
            visible.map((c) => {
              const mapped = c.mapped_field_count ?? 0;
              const retireBlocked = mapped > 0;
              const retirePending = retirePendingId === c.id;
              return (
                <tr key={c.id} data-concept-id={c.id}>
                  <td style={tdStyle}>
                    <div style={{ fontWeight: 600, color: 'var(--text)' }}>
                      {c.name}
                      {c.retired && (
                        <span
                          className="vocab-retired-badge"
                          style={{
                            marginLeft: '0.5rem',
                            padding: '0.05rem 0.4rem',
                            borderRadius: 3,
                            background: 'var(--surface2)',
                            border: '1px solid var(--border)',
                            color: 'var(--red, #f87171)',
                            fontSize: '0.65rem',
                            fontWeight: 600,
                          }}
                        >
                          Retired
                        </span>
                      )}
                    </div>
                    <div style={mutedMono}>{c.id}</div>
                    {(c.synonyms?.length ?? 0) > 0 && (
                      <div style={{ color: 'var(--muted)', fontSize: '0.7rem' }}>
                        aka {c.synonyms!.join(', ')}
                      </div>
                    )}
                  </td>
                  <td style={tdStyle}>
                    <ProvenanceChip label={c.provenance_label || 'custom'} />
                  </td>
                  <td style={{ ...tdStyle, fontVariantNumeric: 'tabular-nums' }}>{mapped}</td>
                  <td style={tdStyle}>{usageText(c)}</td>
                  <td style={{ ...tdStyle, whiteSpace: 'nowrap' }}>
                    <button
                      type="button"
                      className="vocab-edit-btn"
                      style={smallBtnStyle}
                      onClick={() => onEdit(c)}
                    >
                      Edit
                    </button>{' '}
                    {!c.retired && (
                      <button
                        type="button"
                        className="vocab-retire-btn"
                        style={{
                          ...smallBtnStyle,
                          color: retireBlocked ? 'var(--muted)' : 'var(--red, #f87171)',
                          cursor: retireBlocked || retirePending ? 'not-allowed' : 'pointer',
                        }}
                        disabled={retireBlocked || retirePending}
                        aria-disabled={retireBlocked || retirePending}
                        title={
                          retireBlocked
                            ? `Cannot retire — mapped to ${mapped} field${mapped === 1 ? '' : 's'}. Remove the mappings first.`
                            : 'Retire this concept (soft — it stays in the graph, marked retired)'
                        }
                        onClick={() => {
                          if (!retireBlocked && !retirePending) onRetire(c);
                        }}
                      >
                        {retirePending ? 'Retiring…' : 'Retire'}
                      </button>
                    )}
                  </td>
                </tr>
              );
            })
          )}
        </tbody>
      </table>
    </div>
  );
}
