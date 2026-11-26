// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Onboard view-model helpers (jsx-migration W5.1) — the pure logic behind the
 * former HTML fragment builders. The markup moved to React components
 * (<BasisBadge/>, <ResolveExtras/>, <JoinCandidates/>,
 * <ConnectionsDiscovered/> in components/); this module keeps the
 * basis-badge lookup and the join-candidate decide API call. The ADR-0040 D5
 * per-table entity/identity panels and their table_entities / table_notes
 * collectors lived only on the Re-map screen and were removed with it
 * (2026-10). */
import { requestJson } from '../../api/client';
import type { FieldMapping } from '../../api/types/onboard';

/**
 * Concept-suggestion BASIS lookup. When the map step attaches a `basis` to a
 * concept (concept_suggest.py: value_overlap > isa95_synonym > semantic —
 * either on the mapping's ranked `candidates` list or, for an upgraded column,
 * as the top-level `suggestion_basis`), surface WHY the concept was suggested.
 * Returns null when there is no basis to show (e.g. a manual/plain mapping).
 */
/** Human label + tooltip for a suggestion-basis key. */
export function basisMeta(basis: string): { label: string; title: string } {
  const LABELS: Record<string, string> = {
    value_overlap: 'value match',
    isa95_synonym: 'ISA-95 synonym',
    semantic: 'semantic',
  };
  const TITLES: Record<string, string> = {
    value_overlap:
      'Suggested because sampled values overlap a peer system\u2019s values for this concept',
    isa95_synonym: 'Suggested from an ISA-95 name synonym for this concept',
    semantic: 'Suggested by the semantic field mapper',
  };
  return {
    label: LABELS[basis] || String(basis).replace(/_/g, ' '),
    title: TITLES[basis] || 'Why this concept was suggested',
  };
}

export function basisFor(
  m: FieldMapping | null,
  concept: string,
): { basis: string; label: string; title: string } | null {
  if (!m || !concept) return null;
  let basis = '';
  const hit = (m.candidates || []).find((c) => c && c.concept === concept);
  if (hit && hit.basis) basis = hit.basis;
  else if (m.suggestion_basis && concept === m.concept) basis = m.suggestion_basis;
  if (!basis) return null;
  return { basis, ...basisMeta(basis) };
}

/** Join-candidates decide: accept/reject one proposal through the EXISTING
 * proposed-mappings routes. Throws on failure (the component re-enables its
 * buttons — retryable, never blocks the onboarding flow). Registered as the
 * window.obJoinCandidateDecide shim (globals.ts). */
export async function obJoinCandidateDecide(
  mappingId: string,
  decision: 'accept' | 'reject',
): Promise<void> {
  const d = await requestJson<{ error?: string }>(
    `/api/proposed-mappings/${encodeURIComponent(mappingId)}/${decision === 'accept' ? 'accept' : 'reject'}`,
    { method: 'POST' },
  );
  if (d.error) throw new Error(d.error);
}
