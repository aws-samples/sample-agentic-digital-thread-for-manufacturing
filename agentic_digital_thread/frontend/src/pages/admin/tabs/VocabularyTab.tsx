// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Vocabulary admin tab (ADR-0050) — the unified concept list over
 * GET /api/admin/vocabulary/concepts with single-concept authoring (add/edit
 * through the ADR-0003 lifecycle), soft retire, bulk pack import, and the
 * report-only retirement-candidates sweep.
 *
 * Same conventions as tabs/SettingsTab.tsx: the host keeps this mounted after
 * first activation, TanStack Query owns server freshness, local state owns the
 * forms, and every visual token comes from the shared CSS vars. JSX only —
 * no HTML-string sinks (repo sink policy). */
import { useState, type CSSProperties } from 'react';
import { useMutation, useQuery, useQueryClient, type UseQueryResult } from '@tanstack/react-query';

import { ApiError } from '../../../api/client';
import type { VocabularyConcept, VocabularyConceptsResponse } from '../../../api/types/vocabulary';
import { askConfirm } from '../../../shared/ui/confirm-store';
import { retireConceptMutationOptions, vocabularyConceptsQueryOptions } from '../admin-queries';
import ArtifactsPanel from './vocabulary/ArtifactsPanel';
import ConceptFormModal from './vocabulary/ConceptFormModal';
import ConceptTable from './vocabulary/ConceptTable';
import GuidanceEditor from './vocabulary/GuidanceEditor';
import ImportPanel from './vocabulary/ImportPanel';
import MetricsPanel from './vocabulary/MetricsPanel';
import RetirementPanel from './vocabulary/RetirementPanel';

const h3Style: CSSProperties = { margin: '0 0 0.5rem', fontSize: '0.95rem', fontWeight: 600 };
const sectionHelpStyle: CSSProperties = {
  fontSize: '0.72rem',
  color: 'var(--muted)',
  lineHeight: 1.5,
  marginBottom: '1rem',
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
const statusStyle: CSSProperties = { fontSize: '0.75rem', color: 'var(--muted)' };
const hrStyle: CSSProperties = {
  border: 'none',
  borderTop: '1px solid var(--border)',
  margin: '2rem 0',
};

interface ColoredStatus {
  text: string;
  color?: string;
}

type ModalState = { mode: 'add' } | { mode: 'edit'; concept: VocabularyConcept } | null;

function errorDetail(error: unknown): string {
  return error instanceof ApiError ? error.detail : error instanceof Error ? error.message : String(error);
}

/** Retire-action state machine: confirm → mutate → colored status. */
function useRetireConcept() {
  const queryClient = useQueryClient();
  const retireMutation = useMutation(retireConceptMutationOptions(queryClient));
  const [retireStatus, setRetireStatus] = useState<ColoredStatus>({ text: '' });
  const [retirePendingId, setRetirePendingId] = useState<string | null>(null);

  const retireConcept = async (concept: VocabularyConcept) => {
    const retire = await askConfirm({
      key: `vocab:retire:${concept.id}`,
      title: `Retire "${concept.name}"?`,
      body: 'The concept stays in the graph, marked retired.',
      confirmLabel: 'Retire concept',
      tone: 'danger',
    });
    if (!retire) return;
    setRetirePendingId(concept.id);
    setRetireStatus({ text: `Retiring ${concept.name}…`, color: 'var(--muted)' });
    try {
      await retireMutation.mutateAsync(concept.id);
      setRetireStatus({ text: `✓ Retired ${concept.name}`, color: 'var(--green, #4ade80)' });
    } catch (error) {
      // A 409 means the graph still has MAPS_TO edges (e.g. a mapping landed
      // after this list was fetched) — surface the server's message verbatim.
      setRetireStatus({ text: 'Error: ' + errorDetail(error), color: 'var(--red, #f87171)' });
    } finally {
      setRetirePendingId(null);
    }
  };

  return { retireStatus, retirePendingId, retireConcept };
}

function HeaderRow({
  query,
  retireStatus,
  onAdd,
}: {
  query: UseQueryResult<VocabularyConceptsResponse>;
  retireStatus: ColoredStatus;
  onAdd: () => void;
}) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem', marginBottom: '0.75rem' }}>
      <button id="vocab-add-btn" type="button" style={addBtnStyle} onClick={onAdd}>
        + Add concept
      </button>
      <span id="vocab-retire-status" style={{ ...statusStyle, color: retireStatus.color ?? statusStyle.color }}>
        {retireStatus.text}
      </span>
      {query.isSuccess && (
        <span style={{ ...statusStyle, marginLeft: 'auto' }}>
          {query.data?.total ?? query.data?.concepts?.length ?? 0} concepts
        </span>
      )}
    </div>
  );
}

function ConceptsSection({
  query,
  onEdit,
  onRetire,
  retirePendingId,
}: {
  query: UseQueryResult<VocabularyConceptsResponse>;
  onEdit: (concept: VocabularyConcept) => void;
  onRetire: (concept: VocabularyConcept) => void;
  retirePendingId: string | null;
}) {
  if (query.isPending) {
    return <div style={{ fontSize: '0.75rem', color: 'var(--muted)' }}>Loading concepts…</div>;
  }
  if (query.isError) {
    return (
      <div role="alert" style={{ ...statusStyle, color: 'var(--red, #f87171)' }}>
        Vocabulary unavailable: {errorDetail(query.error)}{' '}
        <button type="button" onClick={() => void query.refetch()}>
          Retry
        </button>
      </div>
    );
  }
  return (
    <ConceptTable
      concepts={query.data?.concepts ?? []}
      onEdit={onEdit}
      onRetire={onRetire}
      retirePendingId={retirePendingId}
    />
  );
}

export default function VocabularyTab() {
  const conceptsQuery = useQuery(vocabularyConceptsQueryOptions());
  const { retireStatus, retirePendingId, retireConcept } = useRetireConcept();
  const [modal, setModal] = useState<ModalState>(null);

  return (
    <div style={{ maxWidth: 860, padding: '1.5rem' }}>
      <h3 style={h3Style}>Vocabulary</h3>
      <p style={sectionHelpStyle}>
        Every concept the orchestrator can reason about, across all layers — core, installed packs,
        custom imports, and concepts minted from learning. Additions and edits are human assertions
        that flow through the same propose-and-accept lifecycle as agent learning; retire is soft
        and blocked while a concept is still mapped to system fields.
      </p>

      <HeaderRow query={conceptsQuery} retireStatus={retireStatus} onAdd={() => setModal({ mode: 'add' })} />
      <ConceptsSection
        query={conceptsQuery}
        onEdit={(concept) => setModal({ mode: 'edit', concept })}
        onRetire={(concept) => void retireConcept(concept)}
        retirePendingId={retirePendingId}
      />

      <hr style={hrStyle} />
      <ImportPanel />
      <hr style={hrStyle} />
      <ArtifactsPanel />
      <hr style={hrStyle} />
      <MetricsPanel />
      <hr style={hrStyle} />
      <RetirementPanel />
      <hr style={hrStyle} />
      <GuidanceEditor />

      {modal && (
        <ConceptFormModal
          // Keyed so switching directly between add/edit or between two edit
          // targets remounts the form and reseeds its prop-derived state.
          key={modal.mode === 'edit' ? `edit-${modal.concept.id}` : 'add'}
          mode={modal.mode}
          concept={modal.mode === 'edit' ? modal.concept : undefined}
          onClose={() => setModal(null)}
        />
      )}
    </div>
  );
}
