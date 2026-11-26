// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Resolve-step + provision-step presentation components (jsx-migration W5.1)
 * — JSX replacements for the render.ts HTML fragment builders:
 * <BasisBadge/> (obBasisBadge), <ResolveExtras/> (renderResolveExtras),
 * <JoinCandidates/> (renderJoinCandidates), <ConnectionsDiscovered/>
 * (renderConnectionsDiscovered). React text interpolation replaces obEsc. */
import { useState } from 'react';

import type {
  CatalogSystem,
  FieldMapping,
  JoinCandidate,
  JoinPath,
  ProvisionStatusResponse,
} from '../../../api/types/onboard';
import { systemName, useSystemNamesVersion } from '../../../lib/system-names';
import { catalogSystems, showSystemDetail } from '../catalog';
import { displayTable, joinBasis, joinEvidenceLines } from '../connection-evidence';
import type { ResolveExtrasData } from '../flow-store';
import { basisFor, basisMeta, obJoinCandidateDecide } from '../render';

export function BasisBadge({
  m,
  concept,
}: {
  m: FieldMapping | null;
  concept: string;
}) {
  const b = basisFor(m, concept);
  if (!b) return null;
  return (
    <span className={`ob-basis-badge basis-${b.basis}`} title={b.title}>
      {b.label}
    </span>
  );
}

/** Basis badge from a stored basis KEY (mapping-checkpoint tag VMs). */
export function BasisBadgeKey({ basis }: { basis?: string }) {
  if (!basis) return null;
  const meta = basisMeta(basis);
  return (
    <span className={`ob-basis-badge basis-${basis}`} title={meta.title}>
      {meta.label}
    </span>
  );
}

function pctOf(confidence: number | undefined | null): number | null {
  if (confidence == null) return null;
  return confidence <= 1 ? Math.round(confidence * 100) : Math.round(confidence);
}

/**
 * The resolve step's evidence + recommendations + join candidates.
 * `evidence` = measured cross-system value overlaps; `recommendations` =
 * coverage-gap messages. Follows the existing ob-table / result style.
 */
export function ResolveExtras({ res }: { res: ResolveExtrasData | null }) {
  // Systems render by catalog name (lib/system-names.ts); re-render when
  // names arrive. Field names stay as code.
  useSystemNamesVersion();
  const evidence = (res && res.evidence) || [];
  const recs = (res && res.recommendations) || [];
  return (
    <>
      {evidence.length > 0 && (
        <>
          <h4 style={{ color: 'var(--accent)', margin: '1rem 0 0.5rem' }}>
            ✦ Value Overlap Evidence
          </h4>
          <p style={{ color: 'var(--muted)', fontSize: '0.78rem', margin: '0 0 0.5rem' }}>
            Measured overlapping values across systems — the empirical basis for each
            identity rule.
          </p>
          <table className="ob-table">
            <thead>
              <tr>
                <th>Concept</th>
                <th>Fields</th>
                <th>Shared</th>
                <th>Examples</th>
                <th>Overlap</th>
              </tr>
            </thead>
            <tbody>
              {evidence.map((e, i) => {
                const ratio = pctOf(e.ratio);
                const ratioCls =
                  ratio == null ? 'low' : ratio >= 50 ? 'high' : ratio >= 20 ? 'med' : 'low';
                return (
                  <tr key={i}>
                    <td>{e.concept || ''}</td>
                    <td style={{ fontSize: '0.78rem' }}>
                      <code>{e.source_field || ''}</code> ↔ {systemName(e.target_system)}{' '}
                      <code>{e.target_field || ''}</code>
                    </td>
                    <td style={{ textAlign: 'center' }}>{Number(e.overlap_count) || 0}</td>
                    <td style={{ fontSize: '0.78rem' }}>
                      {(e.shown_examples || []).map((x, xi) => (
                        <span key={xi}>
                          <code>{x}</code>{' '}
                        </span>
                      ))}
                    </td>
                    <td>
                      <span className={`conf ${ratioCls}`}>
                        {ratio != null ? ratio + '%' : '—'}
                      </span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </>
      )}
      {recs.length > 0 && (
        <>
          <h4 style={{ color: 'var(--accent)', margin: '1rem 0 0.5rem' }}>
            ✦ Coverage Recommendations
          </h4>
          <div className="ob-rec-list">
            {recs.map((r, i) => {
              const sev = r.severity || 'info';
              return (
                <div key={i} className="ob-rec-item">
                  <div className="ob-rec-head">
                    <span className="ob-rec-concept">{r.concept || ''}</span>
                    <span className={`ob-rec-sev ${sev}`}>{sev}</span>
                  </div>
                  <div className="ob-rec-msg">{r.message || ''}</div>
                </div>
              );
            })}
          </div>
        </>
      )}
      <JoinCandidates cands={(res && res.join_candidates) || []} />
    </>
  );
}

/**
 * Wave 4 (item E, scoped to SURFACING): cross-system join candidates
 * discovered during the resolve step, as a NON-BLOCKING review panel. Each
 * row offers accept/reject wired to the EXISTING
 * /api/proposed-mappings/{id}/accept|reject routes; provisioning proceeds
 * regardless. The Architecture → Self-Learning tab remains the full review
 * surface.
 */
export function JoinCandidates({ cands }: { cands: JoinCandidate[] }) {
  // Per-row decision state (mid → accepted/rejected/pending/failed title).
  const [decided, setDecided] = useState<Record<string, 'accept' | 'reject'>>({});
  const [pending, setPending] = useState<Record<string, boolean>>({});
  const [failTitle, setFailTitle] = useState<Record<string, string>>({});

  if (!cands.length) return null;

  const decide = async (mid: string, decision: 'accept' | 'reject') => {
    setPending((p) => ({ ...p, [mid]: true }));
    try {
      await obJoinCandidateDecide(mid, decision);
      setDecided((d) => ({ ...d, [mid]: decision }));
    } catch (e) {
      setFailTitle((t) => ({
        ...t,
        [mid]: 'Failed: ' + (e as Error).message + ' (choose to retry)',
      }));
    } finally {
      setPending((p) => ({ ...p, [mid]: false }));
    }
  };

  return (
    <>
      <h4 style={{ color: 'var(--accent)', margin: '1rem 0 0.5rem' }}>
        ✦ Discovered join candidates ({cands.length})
      </h4>
      <p style={{ color: 'var(--muted)', fontSize: '0.78rem', margin: '0 0 0.5rem' }}>
        Cross-system join proposals found while resolving. Reviewing now is optional —
        provisioning proceeds regardless.{' '}
        <a
          href="#"
          style={{ color: 'var(--accent)' }}
          onClick={(e) => {
            e.preventDefault();
            const w = window as unknown as {
              switchView?: (v: string) => void;
              switchArchTab?: (t: string) => void;
            };
            w.switchView?.('architecture');
            w.switchArchTab?.('proposed');
          }}
        >
          Open full review UI →
        </a>
      </p>
      <table className="ob-table ob-join-candidates">
        <thead>
          <tr>
            <th>Candidate</th>
            <th>Type</th>
            <th>Transform</th>
            <th>Confidence</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {cands.map((c) => {
            const mid = String(c.mapping_id || '');
            const type = String(c.proposed_type || 'join_path').replace(/_/g, ' ');
            const d = decided[mid];
            return (
              <tr key={mid} data-mid={mid} style={d ? { opacity: 0.55 } : undefined}>
                <td style={{ fontSize: '0.78rem' }}>
                  <code>{c.from || ''}</code> ↔ <code>{c.to || ''}</code>
                </td>
                <td style={{ fontSize: '0.78rem', color: 'var(--muted)' }}>{type}</td>
                <td style={{ fontSize: '0.78rem' }}>{c.transform || 'identity'}</td>
                <td style={{ textAlign: 'center' }}>{String(c.confidence || '')}</td>
                <td style={{ whiteSpace: 'nowrap', textAlign: 'right' }}>
                  {d === 'accept' ? (
                    <span style={{ color: 'var(--green)', fontSize: '0.78rem' }}>
                      ✓ accepted
                    </span>
                  ) : d === 'reject' ? (
                    <span style={{ color: 'var(--muted)', fontSize: '0.78rem' }}>
                      ✗ rejected
                    </span>
                  ) : (
                    <>
                      <button
                        className="ob-jc-btn ob-jc-accept"
                        data-mid={mid}
                        disabled={!!pending[mid]}
                        title={
                          failTitle[mid] ||
                          'Accept — writes this join rule to the knowledge graph'
                        }
                        onClick={() => void decide(mid, 'accept')}
                      >
                        ✓
                      </button>
                      <button
                        className="ob-jc-btn ob-jc-reject"
                        data-mid={mid}
                        disabled={!!pending[mid]}
                        title={failTitle[mid] || 'Reject — dismisses this proposal'}
                        onClick={() => void decide(mid, 'reject')}
                      >
                        ✗
                      </button>
                    </>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </>
  );
}

const BADGE_COLORS: Record<string, string> = {
  declared_foreign_key: '#28a745',
  value_overlap: '#28a745',
  same_row_identifiers: '#28a745',
  identity_rule: '#28a745',
  column_name_pattern: '#ffc107',
  measured_reference: '#28a745',
  foreign_key_match: '#28a745',
  semantic_similarity: '#ffc107',
  hierarchy_position: '#007bff',
  value_format_match: '#28a745',
  user_confirmed: '#6c757d',
};

/** A colored label chip: a join path's basis or a value mapping's method. */
export function EvidenceBadge({ colorKey, label }: { colorKey: string; label: string }) {
  const color = BADGE_COLORS[colorKey.replace(/^method:/, '')] || '#6c757d';
  return (
    <span
      className="ob-evidence-badge"
      style={{
        display: 'inline-block',
        padding: '0.15rem 0.5rem',
        borderRadius: 3,
        fontSize: '0.65rem',
        fontWeight: 600,
        background: `${color}20`,
        color,
        border: `1px solid ${color}40`,
      }}
    >
      {label}
    </span>
  );
}

/** A join path's basis badge that opens to its evidence in plain words. */
export function JoinEvidence({ jp }: { jp: JoinPath }) {
  const basis = joinBasis(jp);
  return (
    <details className="ob-join-evidence">
      <summary style={{ cursor: 'pointer' }} title="Show what proved this connection">
        <EvidenceBadge colorKey={basis.key} label={basis.label} />
      </summary>
      <ul style={{ margin: '0.35rem 0 0 1rem', padding: 0, fontSize: '0.75rem' }}>
        {joinEvidenceLines(jp).map((line, i) => (
          <li key={i}>{line}</li>
        ))}
      </ul>
    </details>
  );
}

/** Open the system's detail page, which lists all of its connections. */
export function openSystemConnections(systemId: string): void {
  const sys: CatalogSystem = catalogSystems().find(
    (s) => (s.id || s.system_id) === systemId,
  ) || { id: systemId, name: systemName(systemId) };
  void showSystemDetail(sys);
}

/**
 * "Connections Discovered" — the JoinPaths and ValueMappings touching the
 * system after provisioning completes, each join path with its evidence, and
 * (given `systemId`) a button to the system's detail page.
 */
export function ConnectionsDiscovered({
  result,
  systemId,
}: {
  result: ProvisionStatusResponse;
  /** The provisioned system: shows "See all connections". */
  systemId?: string;
}) {
  // Systems render by catalog name; the table.field paths stay as code.
  useSystemNamesVersion();
  const methodBadge = (method?: string) => (
    <EvidenceBadge colorKey={method || ''} label={(method || 'unknown').replace(/_/g, ' ')} />
  );

  const joinPaths = result.join_paths || [];
  const valueMappings = result.value_mappings || [];
  const seeAll = systemId ? (
    <button
      type="button"
      className="ob-seed-chip ob-see-connections"
      style={{ marginTop: '0.75rem' }}
      onClick={() => openSystemConnections(systemId)}
    >
      See all connections
    </button>
  ) : null;
  if (joinPaths.length === 0 && valueMappings.length === 0) {
    return seeAll ? <div>{seeAll}</div> : null;
  }

  return (
    <div style={{ marginTop: '1rem' }}>
      {joinPaths.length > 0 && (
        <>
          <h4 style={{ color: 'var(--accent)', margin: '0 0 0.5rem' }}>
            ✦ Connections Discovered
          </h4>
          <table className="ob-table" style={{ fontSize: '0.8rem' }}>
            <thead>
              <tr>
                <th>From</th>
                <th></th>
                <th>To</th>
                <th>Evidence</th>
              </tr>
            </thead>
            <tbody>
              {joinPaths.map((jp, i) => (
                <tr key={i}>
                  <td style={{ fontSize: '0.8rem' }}>
                    {systemName(jp.from_system)}{' '}
                    <code>
                      {displayTable(jp.from_table)}.{jp.from_field || ''}
                    </code>
                  </td>
                  <td style={{ fontSize: '0.8rem', textAlign: 'center' }}>→</td>
                  <td style={{ fontSize: '0.8rem' }}>
                    {systemName(jp.to_system)}{' '}
                    <code>
                      {displayTable(jp.to_table)}.{jp.to_field || ''}
                    </code>
                  </td>
                  <td>
                    <JoinEvidence jp={jp} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
      {valueMappings.length > 0 && (
        <>
          <h4 style={{ color: 'var(--accent)', margin: '1rem 0 0.5rem' }}>
            ✦ Value Mappings
          </h4>
          <table className="ob-table" style={{ fontSize: '0.8rem' }}>
            <thead>
              <tr>
                <th>From</th>
                <th></th>
                <th>To</th>
                <th>Concept</th>
                <th>Confidence</th>
                <th>Method</th>
              </tr>
            </thead>
            <tbody>
              {valueMappings.map((vm, i) => {
                const pct = pctOf(vm.confidence);
                const conf = pct != null ? pct + '%' : '—';
                const confClass = pct != null && pct >= 90 ? 'high' : pct != null && pct >= 70 ? 'med' : 'low';
                return (
                  <tr key={i}>
                    <td style={{ fontSize: '0.8rem' }}>
                      {systemName(vm.from_system)} <code>{vm.from_value || ''}</code>
                    </td>
                    <td style={{ fontSize: '0.8rem', textAlign: 'center' }}>→</td>
                    <td style={{ fontSize: '0.8rem' }}>
                      {systemName(vm.to_system)} <code>{vm.to_value || ''}</code>
                    </td>
                    <td style={{ fontSize: '0.75rem', color: 'var(--muted)' }}>
                      {vm.concept || ''}
                    </td>
                    <td>
                      <span className={`conf ${confClass}`}>{conf}</span>
                    </td>
                    <td>{methodBadge(vm.method)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </>
      )}
      {seeAll}
    </div>
  );
}
