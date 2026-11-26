// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Review queue — GET /api/admin/evals/queue, computed on read (ADR-0062 D3).
 *
 * Real traffic a user flagged, plus the failed rows of recent golden runs. The
 * steward is the ONLY authority here:
 *
 *   - the machine pre-score renders with its literal "triage-only" label and
 *     is never a verdict (D4) — it orders attention and nothing else;
 *   - the three decision buttons are the ONLY mutation on this surface;
 *   - "Download draft" hands the steward a TRANSIENT one-question draft for
 *     their EXTERNAL, customer-owned golden set (D5). The draft body lives
 *     only for the download tick: no component state, no query cache
 *     (gcTime: 0), no localStorage/sessionStorage/IndexedDB copy;
 *   - a source that aged out of its retention window renders its own honest
 *     "source expired" state, keeping the steward's decision visible.
 *
 * Every server string — question, answer excerpt, steward comment, failure
 * line, rejection detail — renders as a LITERAL text node (repo sink policy:
 * zero HTML-string sinks). It is untrusted customer data, never markdown.
 *
 * There is NO golden-subset control here and none may be added: this panel
 * decides review items, and the golden suite still runs ALL-or-nothing from
 * its own affordance in EvalsTab. */
import { useState, type CSSProperties } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import type {
  EvalQueueItem,
  EvalQueueVerdict,
  GoldenDraftBundle,
} from '../../../../api/types/admin';
import {
  downloadGoldenDraftMutationOptions,
  evalQueueQueryOptions,
  recordEvalDecisionMutationOptions,
} from '../../admin-queries';
import { startedText } from './evals-logic';
import {
  NOTE_MAX_CHARS,
  QUEUE_VERDICTS,
  decisionText,
  draftFilename,
  isDraftable,
  prescoreText,
  queueCountsText,
  rejectionText,
  sourceBadgeText,
  verdictLabel,
} from './queue-logic';

const h4Style: CSSProperties = { margin: '0 0 0.5rem', fontSize: '0.85rem', fontWeight: 600 };
const helpStyle: CSSProperties = {
  fontSize: '0.72rem',
  color: 'var(--muted)',
  lineHeight: 1.5,
  marginBottom: '1rem',
};
const mutedStyle: CSSProperties = { fontSize: '0.75rem', color: 'var(--muted)' };
const smallMutedStyle: CSSProperties = { fontSize: '0.7rem', color: 'var(--muted)' };
const cardStyle: CSSProperties = {
  border: '1px solid var(--border)',
  borderRadius: 4,
  padding: '0.75rem',
  marginBottom: '0.75rem',
};
const badgeStyle: CSSProperties = {
  padding: '0.15rem 0.45rem',
  borderRadius: 3,
  fontSize: '0.7rem',
  fontWeight: 600,
  background: 'var(--surface2)',
  color: 'var(--accent)',
  fontFamily: "'IBM Plex Mono',monospace",
};
const prescoreStyle: CSSProperties = {
  fontSize: '0.7rem',
  color: 'var(--muted)',
  fontFamily: "'IBM Plex Mono',monospace",
};
const questionStyle: CSSProperties = { fontSize: '0.8rem', margin: '0.5rem 0 0.25rem' };
const answerStyle: CSSProperties = {
  fontSize: '0.72rem',
  color: 'var(--muted)',
  whiteSpace: 'pre-wrap',
  margin: 0,
};
const failListStyle: CSSProperties = {
  margin: '0.25rem 0 0',
  paddingLeft: '1rem',
  color: 'var(--red, #f87171)',
  fontSize: '0.72rem',
};
const actionsStyle: CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  flexWrap: 'wrap',
  gap: '0.4rem',
  marginTop: '0.5rem',
};
const btnStyle: CSSProperties = {
  background: 'var(--surface2)',
  color: 'var(--text)',
  border: '1px solid var(--border)',
  borderRadius: 4,
  padding: '0.3rem 0.7rem',
  fontSize: '0.75rem',
  cursor: 'pointer',
};
const noteStyle: CSSProperties = {
  background: 'var(--surface2)',
  color: 'var(--text)',
  border: '1px solid var(--border)',
  borderRadius: 4,
  padding: '0.35rem 0.6rem',
  fontSize: '0.75rem',
  width: '100%',
  minHeight: '2.2rem',
  marginTop: '0.5rem',
};

/** Where a promoted draft goes. Named in ONE place so the boundary reads the
 * same everywhere: an external, customer-owned file — never this repository. */
const DRAFT_DESTINATION_COPY =
  'Downloads a one-question draft file for your EXTERNAL, customer-owned golden set — the '
  + 'destination is outside this repository checkout, and nothing is written to this repository '
  + 'or to the server. Land it with scripts/merge_golden_draft.py, then edit the expectations '
  + 'yourself: the draft ships no generated answer pattern, only observed hints.';

interface ItemStatus {
  text: string;
  color?: string;
}

/** Blob-anchor download — the draft touches the DOM for one tick and is then
 * revoked. No HTML-string sink, no durable browser state. */
function saveDraftFile(bundle: GoldenDraftBundle, filename: string): void {
  const blob = new Blob([JSON.stringify(bundle, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

function SourceExpiredNote() {
  return (
    <div className="evals-queue-expired" style={smallMutedStyle}>
      Source expired — the original turn passed its retention window, so no question or answer text
      remains to review. Your decision is kept; a golden draft can no longer be built from it.
    </div>
  );
}

function ItemFacts({ item }: { item: EvalQueueItem }) {
  return (
    <>
      {item.question ? (
        <p className="evals-queue-question" style={questionStyle}>
          {item.question}
        </p>
      ) : null}
      {item.answer_excerpt ? (
        <p className="evals-queue-answer" style={answerStyle}>
          {item.answer_excerpt}
        </p>
      ) : null}
      {item.user_comment ? (
        <p className="evals-queue-comment" style={answerStyle}>
          steward-visible user comment: {item.user_comment}
        </p>
      ) : null}
      {(item.failures ?? []).length > 0 && (
        <ul className="evals-queue-failures" style={failListStyle}>
          {(item.failures ?? []).map((failure, i) => (
            <li key={i}>{failure}</li>
          ))}
        </ul>
      )}
    </>
  );
}

function QueueItemCard({
  item,
  note,
  status,
  busy,
  onNote,
  onDecide,
  onDraft,
}: {
  item: EvalQueueItem;
  note: string;
  status: ItemStatus | undefined;
  busy: boolean;
  onNote: (value: string) => void;
  onDecide: (verdict: EvalQueueVerdict) => void;
  onDraft: () => void;
}) {
  const decided = decisionText(item.decision);
  return (
    <div
      className="evals-queue-item"
      data-source-ref={item.source_ref}
      data-source-expired={item.source_expired ? 'true' : 'false'}
      style={cardStyle}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', flexWrap: 'wrap' }}>
        <span className="evals-queue-source-badge" style={badgeStyle}>
          {sourceBadgeText(item)}
        </span>
        {item.source_expired && (
          <span className="evals-queue-expired-badge badge">source expired</span>
        )}
        <span style={smallMutedStyle}>{startedText(item.occurred_at)}</span>
        <span className="mono" style={smallMutedStyle}>
          {item.ref_sha ?? '—'}
        </span>
      </div>

      {/* Machine triage hint — ordering only. It is NOT a verdict (ADR-0062 D4). */}
      <div className="evals-queue-prescore" style={prescoreStyle}>
        {prescoreText(item.machine_prescore)}
      </div>

      {item.source_expired ? <SourceExpiredNote /> : <ItemFacts item={item} />}

      {decided && (
        <div className="evals-queue-decision" style={smallMutedStyle}>
          steward decision: {decided}
        </div>
      )}
      {item.decision?.steward_note ? (
        <div className="evals-queue-steward-note" style={smallMutedStyle}>
          note: {item.decision.steward_note}
        </div>
      ) : null}

      <textarea
        className="evals-queue-note"
        aria-label="Steward note (optional)"
        placeholder="Optional steward note"
        value={note}
        maxLength={NOTE_MAX_CHARS}
        style={noteStyle}
        onChange={(event) => onNote(event.target.value)}
      />

      <div style={actionsStyle}>
        {QUEUE_VERDICTS.map((verdict) => (
          <button
            key={verdict}
            type="button"
            className="evals-queue-verdict-btn"
            data-verdict={verdict}
            style={{ ...btnStyle, opacity: busy ? 0.5 : 1 }}
            disabled={busy}
            onClick={() => onDecide(verdict)}
          >
            {verdictLabel(verdict)}
          </button>
        ))}
        <button
          type="button"
          className="evals-queue-draft-btn"
          style={{ ...btnStyle, opacity: busy || !isDraftable(item) ? 0.5 : 1 }}
          disabled={busy || !isDraftable(item)}
          title={DRAFT_DESTINATION_COPY}
          onClick={onDraft}
        >
          Download draft
        </button>
        {status?.text ? (
          <span
            className="evals-queue-item-status"
            style={{ ...smallMutedStyle, color: status.color ?? smallMutedStyle.color }}
          >
            {status.text}
          </span>
        ) : null}
      </div>
      {!item.decision?.verdict && !item.source_expired && (
        <div style={smallMutedStyle}>Record a decision first — a draft needs one.</div>
      )}
    </div>
  );
}

export default function ReviewQueue() {
  const queryClient = useQueryClient();
  const queueQuery = useQuery(evalQueueQueryOptions());
  const decideMutation = useMutation(recordEvalDecisionMutationOptions(queryClient));
  // The draft mutation is deliberately separate from every golden-suite
  // control and holds NO result state (gcTime: 0 in its options).
  const draftMutation = useMutation(downloadGoldenDraftMutationOptions());
  const [notes, setNotes] = useState<Record<string, string>>({});
  const [statuses, setStatuses] = useState<Record<string, ItemStatus>>({});
  const [pendingRef, setPendingRef] = useState<string | null>(null);

  const items = queueQuery.data?.items ?? [];

  const setStatus = (sourceRef: string, status: ItemStatus) =>
    setStatuses((prior) => ({ ...prior, [sourceRef]: status }));

  const decide = async (item: EvalQueueItem, verdict: EvalQueueVerdict) => {
    const note = (notes[item.source_ref] ?? '').trim();
    setPendingRef(item.source_ref);
    setStatus(item.source_ref, { text: 'Recording…', color: 'var(--muted)' });
    try {
      await decideMutation.mutateAsync({
        source_ref: item.source_ref,
        verdict,
        ...(note ? { note } : {}),
      });
      // The note lives on the server row now — drop the local copy.
      setNotes((prior) => ({ ...prior, [item.source_ref]: '' }));
      setStatus(item.source_ref, { text: 'Decision recorded.', color: 'var(--green, #4ade80)' });
    } catch (error) {
      // A rejected note comes back as 400 {reason, detail} — the reason is
      // shown, the note itself is never echoed by the server or by us.
      setStatus(item.source_ref, {
        text: 'Rejected — ' + rejectionText(error),
        color: 'var(--red, #f87171)',
      });
    } finally {
      setPendingRef(null);
    }
  };

  const downloadDraft = async (item: EvalQueueItem) => {
    setPendingRef(item.source_ref);
    setStatus(item.source_ref, { text: 'Building draft…', color: 'var(--muted)' });
    try {
      const bundle = await draftMutation.mutateAsync(item.source_ref);
      saveDraftFile(bundle, draftFilename(bundle, item.ref_sha));
      // Nothing retains the bundle: it is out of scope after this tick.
      setStatus(item.source_ref, {
        text: 'Draft downloaded — merge it into your external golden set.',
        color: 'var(--green, #4ade80)',
      });
    } catch (error) {
      setStatus(item.source_ref, {
        text: 'No draft — ' + rejectionText(error),
        color: 'var(--red, #f87171)',
      });
    } finally {
      setPendingRef(null);
    }
  };

  return (
    <div id="evals-queue-section">
      <h4 style={h4Style}>Review queue</h4>
      <p style={helpStyle}>
        Real turns a user flagged, plus failed rows from recent golden runs — computed fresh on every
        read from their sources, so nothing here can go stale. The machine attaches a triage-only
        hint that orders attention; it is not a verdict, and your decision is the only judgement
        recorded. Items whose source passed its retention window stay listed with your decision and
        an honest source-expired state.
      </p>

      <div id="evals-queue-summary" style={{ ...mutedStyle, marginBottom: '0.5rem' }}>
        {queueCountsText(queueQuery.data?.counts)}{' '}
        <button
          type="button"
          id="evals-queue-reload-btn"
          style={{ ...btnStyle, padding: '0.15rem 0.5rem', marginLeft: '0.4rem' }}
          onClick={() => void queueQuery.refetch()}
        >
          Reload
        </button>
      </div>

      {queueQuery.isPending && <div style={mutedStyle}>Loading the review queue…</div>}
      {queueQuery.isError && (
        <div role="alert" style={{ ...mutedStyle, color: 'var(--red, #f87171)' }}>
          Review queue unavailable: {rejectionText(queueQuery.error)}
        </div>
      )}
      {queueQuery.isSuccess && items.length === 0 && (
        <div id="evals-queue-empty" style={mutedStyle}>
          Nothing to review in this window — no flagged turns and no golden failures.
        </div>
      )}

      {items.map((item) => (
        <QueueItemCard
          key={item.source_ref}
          item={item}
          note={notes[item.source_ref] ?? ''}
          status={statuses[item.source_ref]}
          busy={pendingRef === item.source_ref}
          onNote={(value) => setNotes((prior) => ({ ...prior, [item.source_ref]: value }))}
          onDecide={(verdict) => void decide(item, verdict)}
          onDraft={() => void downloadDraft(item)}
        />
      ))}

      <p id="evals-queue-draft-help" style={{ ...helpStyle, marginTop: '0.5rem' }}>
        {DRAFT_DESTINATION_COPY}
      </p>
    </div>
  );
}
