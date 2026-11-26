// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Query-guidance editor (W4.1, ADR-0050) — per-system panels over the EXISTING
 * GET /api/query-guidance (onboarded base guidance + LEARNED notes). Learned
 * notes render with the IMPORTED LgCard (single card implementation — note
 * list, prune ✗, clear-learned all come from there); prune/clear call the
 * existing steward-gated routes. "Propose note" files a guidance_note
 * proposal into the OPEN ADR-0003 lifecycle via the ADR-0050 route
 * POST /api/admin/vocabulary/guidance/{sid}/propose — open to any
 * authenticated user; a steward accepts it later, which applies the note.
 *
 * Steward gate mirror: prune/clear are admin-only on the server (403
 * otherwise) — for non-admins no handlers are wired and the imported card's
 * controls are suppressed with an editor-scoped stylesheet (LgCard is
 * imported, not re-implemented, so it has no read-only prop).
 * JSX only — no HTML-string sinks (repo sink policy). */
import { useEffect, useState, type CSSProperties } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { ApiError } from '../../../../api/client';
import type { ProposedMapping } from '../../../../api/types/proposals';
import { useAuth } from '../../../../contexts/AuthContext';
import { askConfirm } from '../../../../shared/ui/confirm-store';
import { LgCard } from '../../../../views/architecture/SlCards';
import GuidanceStabilityControls from './GuidanceStabilityControls';
import { systemDisplayLabel, useSystemDisplayNames } from './guidance-stability-ui';
import {
  clearGuidanceLearnedMutationOptions,
  guidanceProposalsQueryOptions,
  proposeGuidanceNoteMutationOptions,
  pruneGuidanceNoteMutationOptions,
  queryGuidanceQueryOptions,
  type GuidanceSystemWithBase,
} from '../../guidance-queries';

const h3Style: CSSProperties = { margin: '0 0 0.5rem', fontSize: '0.95rem', fontWeight: 600 };
const sectionHelpStyle: CSSProperties = {
  fontSize: '0.72rem',
  color: 'var(--muted)',
  lineHeight: 1.5,
  marginBottom: '1rem',
};
const statusStyle: CSSProperties = { fontSize: '0.75rem', color: 'var(--muted)' };
const baseBlockStyle: CSSProperties = {
  fontSize: '0.75rem',
  color: 'var(--text2)',
  background: 'var(--surface2)',
  border: '1px solid var(--border)',
  borderRadius: 4,
  padding: '0.5rem 0.75rem',
  whiteSpace: 'pre-wrap',
  fontFamily: "'IBM Plex Mono',monospace",
  marginTop: '0.35rem',
};
const noteInputStyle: CSSProperties = {
  flex: 1,
  padding: '0.35rem 0.5rem',
  background: 'var(--surface2)',
  border: '1px solid var(--border)',
  borderRadius: 4,
  color: 'var(--text)',
  fontSize: '0.78rem',
  boxSizing: 'border-box',
};
const proposeBtnStyle: CSSProperties = {
  background: 'var(--accent)',
  color: '#000',
  fontWeight: 600,
  padding: '0.35rem 0.9rem',
  border: 'none',
  borderRadius: 4,
  fontSize: '0.78rem',
  cursor: 'pointer',
  whiteSpace: 'nowrap',
};

// Editor-scoped read-only rule over the imported LgCard for non-admins —
// mirrors the server-side steward gate on prune/clear. Static constant.
const READONLY_CSS =
  '#vocab-guidance-editor .lg-prune,#vocab-guidance-editor .sl-reject{display:none;}';

function errorDetail(error: unknown): string {
  return error instanceof ApiError ? error.detail : error instanceof Error ? error.message : String(error);
}

interface ColoredStatus {
  text: string;
  color?: string;
}

const GREEN = 'var(--green, #4ade80)';

type TrackedPhase = 'waiting' | 'delivered' | 'gone';

/** Where a just-proposed note stands in the guidance proposals list. Until the
 * list has shown it pending once, a list without it is a list read before the
 * propose landed, so the note still counts as waiting. */
function trackedProposalPhase(
  rows: readonly ProposedMapping[] | undefined,
  mappingId: string,
  seenPending: boolean,
): TrackedPhase {
  const row = rows?.find((mapping) => mapping.mapping_id === mappingId);
  if (row?.status === 'pending') return 'waiting';
  if (row?.status === 'accepted') return 'delivered';
  if (!row && !seenPending) return 'waiting';
  return 'gone';
}

/** The propose status line for the note this panel proposed: the waiting text
 * while it is still pending, "Delivered." once a steward delivered it, and
 * nothing once it left the queue another way (rejected, expired, removed). */
function useTrackedProposalStatus(mappingId: string | null, status: ColoredStatus): ColoredStatus {
  const proposals = useQuery(guidanceProposalsQueryOptions(!!mappingId));
  const [seenId, setSeenId] = useState<string | null>(null);
  const phase = mappingId ? trackedProposalPhase(proposals.data, mappingId, seenId === mappingId) : 'waiting';
  useEffect(() => {
    if (mappingId && proposals.data?.some((m) => m.mapping_id === mappingId && m.status === 'pending')) {
      setSeenId(mappingId);
    }
  }, [mappingId, proposals.data]);
  if (!mappingId || phase === 'waiting') return status;
  return phase === 'delivered' ? { text: 'Delivered.', color: GREEN } : { text: '' };
}

function SystemGuidancePanel({
  sys,
  names,
  isAdmin,
  onPrune,
  onClearLearned,
  onPropose,
}: {
  sys: GuidanceSystemWithBase;
  names: ReadonlyMap<string, string>;
  isAdmin: boolean;
  onPrune: (systemId: string, note: string) => void;
  onClearLearned: (systemId: string) => void;
  onPropose: (systemId: string, note: string) => Promise<string | undefined>;
}) {
  const sid = sys.system_id || '';
  const base = (sys.base || '').trim();
  const length = typeof sys.length === 'number' ? sys.length : null;
  const maxLength = typeof sys.max_len === 'number' ? sys.max_len : null;
  const headroom = length !== null && maxLength !== null ? Math.max(0, maxLength - length) : null;
  const [note, setNote] = useState('');
  const [proposeStatus, setProposeStatus] = useState<ColoredStatus>({ text: '' });
  const [proposing, setProposing] = useState(false);
  // The note this panel last proposed: its status line follows that proposal
  // in the delivery queue instead of staying on "waiting" after Deliver.
  const [trackedId, setTrackedId] = useState<string | null>(null);
  const shownStatus = useTrackedProposalStatus(trackedId, proposeStatus);

  const propose = async () => {
    const clean = note.trim();
    setTrackedId(null);
    if (!clean) {
      setProposeStatus({ text: 'Enter a note to propose.', color: 'var(--red, #f87171)' });
      return;
    }
    setProposing(true);
    setProposeStatus({ text: 'Proposing…', color: 'var(--muted)' });
    try {
      const mappingId = await onPropose(sid, clean);
      setNote('');
      setProposeStatus({
        text: isAdmin
          ? 'Proposed. It is waiting in the Guidance delivery queue above: choose Preview delivery, then Deliver.'
          : 'Proposed. It is waiting in the Guidance delivery queue above for a steward to deliver.',
        color: GREEN,
      });
      setTrackedId(mappingId || null);
    } catch (error) {
      setProposeStatus({ text: 'Error: ' + errorDetail(error), color: 'var(--red, #f87171)' });
    } finally {
      setProposing(false);
    }
  };

  return (
    <div className="vocab-guidance-system" data-system-id={sid} style={{ marginBottom: '1rem' }}>
      <details className="vocab-guidance-base" style={{ marginBottom: '0.35rem' }}>
        <summary style={{ ...statusStyle, cursor: 'pointer' }}>
          Base guidance: {systemDisplayLabel(sid, names)} {base ? '' : '(none onboarded)'}
        </summary>
        <div className="vocab-guidance-base-text" style={baseBlockStyle}>
          {base || 'No base guidance was onboarded for this system.'}
        </div>
      </details>

      <LgCard
        sys={sys}
        onPrune={isAdmin ? onPrune : undefined}
        onClearLearned={isAdmin ? onClearLearned : undefined}
      />

      <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', marginTop: '0.35rem' }}>
        <input
          type="text"
          className="vocab-guidance-note-input"
          aria-label={`Guidance note to propose for ${systemDisplayLabel(sid, names)}`}
          placeholder="Propose a query-guidance note for this system…"
          style={noteInputStyle}
          value={note}
          onChange={(e) => setNote(e.target.value)}
        />
        <button
          type="button"
          className="vocab-guidance-propose-btn"
          style={proposeBtnStyle}
          disabled={proposing}
          onClick={() => void propose()}
        >
          {proposing ? 'Proposing…' : 'Propose note'}
        </button>
      </div>
      <div
        className="vocab-guidance-propose-status"
        role={shownStatus.color === 'var(--red, #f87171)' ? 'alert' : undefined}
        style={{ ...statusStyle, color: shownStatus.color ?? statusStyle.color, marginTop: '0.25rem' }}
      >
        {shownStatus.text}
      </div>

      <details className="vocab-guidance-technical" style={{ marginTop: '0.35rem' }}>
        <summary style={{ ...statusStyle, cursor: 'pointer' }}>Technical detail</summary>
        <div className="vocab-guidance-authority" style={{ ...statusStyle, fontFamily: "'IBM Plex Mono',monospace", marginTop: '0.25rem' }}>
          scope QUERY HINTS + ANSWER REQUIREMENTS · character budget{' '}
          {length === null || maxLength === null ? 'UNAVAILABLE' : `${length}/${maxLength}`} · headroom{' '}
          {headroom === null ? 'UNAVAILABLE' : headroom}
        </div>
      </details>
    </div>
  );
}

export default function GuidanceEditor() {
  const { user } = useAuth();
  const isAdmin = !!user?.is_admin;
  const queryClient = useQueryClient();

  const guidanceQuery = useQuery(queryGuidanceQueryOptions());
  const names = useSystemDisplayNames();
  const pruneMutation = useMutation(pruneGuidanceNoteMutationOptions(queryClient));
  const clearMutation = useMutation(clearGuidanceLearnedMutationOptions(queryClient));
  const proposeMutation = useMutation(proposeGuidanceNoteMutationOptions(queryClient));

  const [actionStatus, setActionStatus] = useState<ColoredStatus>({ text: '' });

  const systems = guidanceQuery.data ?? [];

  const prune = async (systemId: string, note: string) => {
    const remove = await askConfirm({
      key: `guidance:prune:${systemId}:${note}`,
      title: 'Remove learned note?',
      body: 'This removes the learned note from the system guidance.',
      confirmLabel: 'Remove note',
      tone: 'danger',
    });
    if (!remove) return;
    setActionStatus({ text: 'Pruning…', color: 'var(--muted)' });
    try {
      await pruneMutation.mutateAsync({ systemId, note });
      setActionStatus({ text: '✓ Learned note pruned', color: 'var(--green, #4ade80)' });
    } catch (error) {
      setActionStatus({ text: 'Error: ' + errorDetail(error), color: 'var(--red, #f87171)' });
    }
  };

  const clearLearned = async (systemId: string) => {
    const clear = await askConfirm({
      key: `guidance:clear:${systemId}`,
      title: `Remove all learned notes for ${systemDisplayLabel(systemId, names)}?`,
      body: 'The onboarded base guidance is kept.',
      confirmLabel: 'Remove all notes',
      tone: 'danger',
    });
    if (!clear) return;
    setActionStatus({ text: 'Clearing…', color: 'var(--muted)' });
    try {
      const result = await clearMutation.mutateAsync(systemId);
      setActionStatus({
        text: `✓ Cleared ${result.removed || 0} learned note${result.removed === 1 ? '' : 's'}`,
        color: 'var(--green, #4ade80)',
      });
    } catch (error) {
      setActionStatus({ text: 'Error: ' + errorDetail(error), color: 'var(--red, #f87171)' });
    }
  };

  return (
    <div id="vocab-guidance-editor">
      {!isAdmin && <style>{READONLY_CSS}</style>}

      <h3 style={h3Style}>Query guidance</h3>
      <p style={sectionHelpStyle}>
        Each onboarded system carries query guidance the agents read before writing queries — the
        base text captured at onboarding plus notes learned from answering questions. Stewards can
        prune learned notes that stopped being true; anyone can propose a new note, which applies
        only after a steward accepts it.
      </p>

      <div
        id="vocab-guidance-action-status"
        style={{ ...statusStyle, color: actionStatus.color ?? statusStyle.color, marginBottom: '0.5rem' }}
      >
        {actionStatus.text}
      </div>

      {guidanceQuery.isPending ? (
        <div style={statusStyle}>Loading query guidance…</div>
      ) : guidanceQuery.isError ? (
        <div role="alert" style={{ ...statusStyle, color: 'var(--red, #f87171)' }}>
          Query guidance unavailable: {errorDetail(guidanceQuery.error)}{' '}
          <button type="button" onClick={() => void guidanceQuery.refetch()}>
            Retry
          </button>
        </div>
      ) : systems.length === 0 ? (
        <div className="vocab-guidance-empty" style={statusStyle}>
          No systems carry query guidance yet — guidance appears after a system is onboarded.
        </div>
      ) : (
        <>
          <GuidanceStabilityControls systems={systems} isAdmin={isAdmin} />
          {systems.map((sys) => (
            <SystemGuidancePanel
              key={sys.system_id || ''}
              sys={sys}
              names={names}
              isAdmin={isAdmin}
              onPrune={(sid, note) => void prune(sid, note)}
              onClearLearned={(sid) => void clearLearned(sid)}
              onPropose={async (sid, note) => {
                const result = await proposeMutation.mutateAsync({ systemId: sid, note });
                return result.mapping_id;
              }}
            />
          ))}
        </>
      )}
    </div>
  );
}
