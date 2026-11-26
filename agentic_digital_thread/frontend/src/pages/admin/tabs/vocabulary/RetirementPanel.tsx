// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Retirement-candidates panel (C8) — the deterministic report-only sweep over
 * GET /api/admin/vocabulary/retirement-candidates: non-core, non-retired
 * concepts with zero mapped fields and zero 30-day usage. Retiring stays a
 * human decision through the EXISTING POST /concepts/{id}/retire mutation
 * (a 409 means the graph gained MAPS_TO edges since the sweep — fail safe,
 * message rendered verbatim). Styles mirror tabs/SettingsTab.tsx. JSX only —
 * no HTML-string sinks. */
import { useState, type CSSProperties } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { ApiError } from '../../../../api/client';
import type { RetirementCandidate } from '../../../../api/types/vocabulary';
import { askConfirm } from '../../../../shared/ui/confirm-store';
import {
  retireConceptMutationOptions,
  retirementCandidatesQueryOptions,
} from '../../admin-queries';

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
const retireBtnStyle: CSSProperties = {
  background: 'var(--surface2)',
  color: 'var(--red, #f87171)',
  border: '1px solid var(--border)',
  padding: '0.25rem 0.6rem',
  borderRadius: 4,
  fontSize: '0.72rem',
  cursor: 'pointer',
  fontWeight: 600,
};

interface ColoredStatus {
  text: string;
  color?: string;
}

function errorDetail(error: unknown): string {
  return error instanceof ApiError ? error.detail : error instanceof Error ? error.message : String(error);
}

/** Retire-action state machine — the same confirm → mutate → colored-status
 * idiom as VocabularyTab's concept-table retire. */
function useRetireCandidate() {
  const queryClient = useQueryClient();
  const retireMutation = useMutation(retireConceptMutationOptions(queryClient));
  const [status, setStatus] = useState<ColoredStatus>({ text: '' });
  const [pendingId, setPendingId] = useState<string | null>(null);

  const retire = async (candidate: RetirementCandidate) => {
    const confirmed = await askConfirm({
      key: `vocab:retire:${candidate.id}`,
      title: `Retire "${candidate.name}"?`,
      body: 'The concept stays in the graph, marked retired.',
      confirmLabel: 'Retire concept',
      tone: 'danger',
    });
    if (!confirmed) return;
    setPendingId(candidate.id);
    setStatus({ text: `Retiring ${candidate.name}…`, color: 'var(--muted)' });
    try {
      await retireMutation.mutateAsync(candidate.id);
      setStatus({ text: `✓ Retired ${candidate.name}`, color: 'var(--green, #4ade80)' });
    } catch (error) {
      // 409 = the concept gained mappings since the sweep — surface verbatim.
      setStatus({ text: 'Error: ' + errorDetail(error), color: 'var(--red, #f87171)' });
    } finally {
      setPendingId(null);
    }
  };

  return { status, pendingId, retire };
}

function CandidateRow({
  candidate,
  pending,
  onRetire,
}: {
  candidate: RetirementCandidate;
  pending: boolean;
  onRetire: (candidate: RetirementCandidate) => void;
}) {
  return (
    <tr data-candidate-id={candidate.id}>
      <td style={tdStyle}>
        <div style={{ fontWeight: 600, color: 'var(--text)' }}>{candidate.name}</div>
        <div style={mutedMono}>{candidate.id}</div>
      </td>
      <td style={tdStyle}>{candidate.source ?? '—'}</td>
      <td style={tdStyle}>
        <ul className="vocab-candidate-reasons" style={{ margin: 0, paddingLeft: '1rem' }}>
          {(candidate.reasons ?? []).map((reason) => (
            <li key={reason}>{reason}</li>
          ))}
        </ul>
      </td>
      <td style={{ ...tdStyle, whiteSpace: 'nowrap' }}>
        <button
          type="button"
          className="vocab-candidate-retire-btn"
          style={{ ...retireBtnStyle, cursor: pending ? 'not-allowed' : 'pointer' }}
          disabled={pending}
          title="Retire this concept (soft — it stays in the graph, marked retired)"
          onClick={() => onRetire(candidate)}
        >
          {pending ? 'Retiring…' : 'Retire'}
        </button>
      </td>
    </tr>
  );
}

function CandidateTable({
  candidates,
  pendingId,
  onRetire,
}: {
  candidates: RetirementCandidate[];
  pendingId: string | null;
  onRetire: (candidate: RetirementCandidate) => void;
}) {
  return (
    <table id="vocab-retirement-table" style={{ width: '100%', borderCollapse: 'collapse' }}>
      <thead>
        <tr>
          <th style={thStyle}>Concept</th>
          <th style={thStyle}>Source</th>
          <th style={thStyle}>Why a candidate</th>
          <th style={thStyle}>Action</th>
        </tr>
      </thead>
      <tbody>
        {candidates.map((candidate) => (
          <CandidateRow
            key={candidate.id}
            candidate={candidate}
            pending={pendingId === candidate.id}
            onRetire={onRetire}
          />
        ))}
      </tbody>
    </table>
  );
}

export default function RetirementPanel() {
  const candidatesQuery = useQuery(retirementCandidatesQueryOptions());
  const { status, pendingId, retire } = useRetireCandidate();
  const candidates = candidatesQuery.data?.candidates ?? [];

  return (
    <div id="vocab-retirement-panel">
      <h3 style={h3Style}>Retirement candidates</h3>
      <p style={sectionHelpStyle}>
        Non-core concepts that map to no system fields and appeared in no answers over the last 30
        days. This is a report — nothing retires automatically. Retiring is soft (the concept
        stays in the graph, marked retired) and is refused if the concept gained mappings since
        this sweep.
      </p>
      <div id="vocab-retirement-status" style={{ ...statusStyle, color: status.color ?? statusStyle.color, marginBottom: '0.5rem' }}>
        {status.text}
      </div>
      {candidatesQuery.isPending ? (
        <div style={statusStyle}>Loading retirement candidates…</div>
      ) : candidatesQuery.isError ? (
        <div role="alert" style={{ ...statusStyle, color: 'var(--red, #f87171)' }}>
          Retirement candidates unavailable: {errorDetail(candidatesQuery.error)}{' '}
          <button type="button" onClick={() => void candidatesQuery.refetch()}>
            Retry
          </button>
        </div>
      ) : candidates.length === 0 ? (
        <div className="vocab-retirement-empty" style={statusStyle}>
          No retirement candidates — every non-core concept is mapped or was used recently.
        </div>
      ) : (
        <CandidateTable candidates={candidates} pendingId={pendingId} onRetire={(c) => void retire(c)} />
      )}
    </div>
  );
}
