// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* <ConceptProposalCard/> — the conversational concept-proposal surface
 * (ADR-0068). Renders the onboarding agent's `propose_concepts` tool result
 * (machine-proposed rows — UNTRUSTED text, rendered as React text nodes only;
 * zero innerHTML) and lets the STEWARD accept selected rows. The steward
 * click is the ONLY write trigger (the machine never auto-accepts —
 * ADR-0052/0063 posture): Accept POSTs to the onboard-gated vocabulary
 * import route (flag-2 decision (b)), which delegates to the SAME classifier
 * and writer as the admin route. The card never pre-gates on role — a 403 is
 * an honest error state the steward can act on. After a successful import
 * the hint chip dispatches "run map_fields" through the existing chat-input
 * path (`welcomeSubmit`, the same dispatcher ChatCheckpoint's buttons use) —
 * the card never calls the agent directly. Vocabulary is read live per
 * `map_fields` call, so no re-map is needed after accepting. */
import { useState } from 'react';

import { ApiError, requestJson } from '../../../api/client';
import { welcomeSubmit } from '../chat';

export const ONBOARD_VOCAB_IMPORT_URL = '/api/onboard/vocabulary/import';

const MUTED = 'var(--muted)';
const RED = 'var(--red, #f87171)';
const GREEN = 'var(--green, #4ade80)';
const AMBER = 'var(--amber, #fbbf24)';

export type ConceptProposalRow = {
  id: string;
  name: string;
  description: string;
  action: 'insert' | 'skip_exact' | 'skip_collision';
  collides_with?: string;
};

/** The `propose_concepts` tool result (server-computed classification). */
export type ConceptProposalResult = {
  system_id: string;
  proposals: ConceptProposalRow[];
  insert_count?: number;
  skip_count?: number;
  collision_count?: number;
};

type SentRow = { id: string; name: string; description: string };

/** `_run_import`'s response shape (same body as the admin route — one
 * classifier/writer home; the onboard route only re-gates access). */
export type ImportResponse = {
  status?: string;
  inserted: number;
  skipped: number;
  failed: number;
  skipped_ids: string[];
  collisions: { id: string; existing_id?: string; reason?: string }[];
};

/** POST the steward's selection to the onboard-gated import route. */
export function postImport(systemId: string, rows: SentRow[]): Promise<ImportResponse> {
  return requestJson<ImportResponse>(ONBOARD_VOCAB_IMPORT_URL, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      metadata: { name: `onboarding-proposal-${systemId}` },
      concepts: rows,
    }),
  });
}

/** Honest failure text. A 403 means the route's onboard gate refused this
 * role — say so plainly instead of pre-disabling the button (flag 2). */
export function importErrorText(e: unknown): string {
  if (e instanceof ApiError && e.status === 403) {
    return 'Your role cannot import concepts — ask an admin to accept these.';
  }
  if (e instanceof ApiError) return e.detail || e.message;
  return 'Import failed — nothing was confirmed written.';
}

/** Why a row cannot be selected (preview classification, server-computed). */
export function rowNote(p: ConceptProposalRow): string {
  if (p.action === 'skip_exact') return 'already in vocabulary';
  if (p.action === 'skip_collision') {
    return `collides with ${p.collides_with || 'an existing concept'}`;
  }
  return '';
}

/** Per-row import outcome, derived from the response. The server reports
 * failures as a COUNT (per-row isolation, no failed ids), so when failed > 0
 * every non-skipped row is named as unconfirmed — a failed write must never
 * read as success. */
export function importRowLines(
  sent: { id: string }[],
  res: ImportResponse,
): { color: string; text: string }[] {
  const skipped = new Set(res.skipped_ids ?? []);
  const collided = new Map(
    (res.collisions ?? []).map((c) => [c.id, c.existing_id ?? '']),
  );
  const failed = res.failed ?? 0;
  return sent.map(({ id }) => {
    if (skipped.has(id)) {
      return { color: MUTED, text: `${id} — already in vocabulary (skipped)` };
    }
    if (collided.has(id)) {
      const existing = collided.get(id);
      return {
        color: AMBER,
        text: `${id} — collides with ${existing || 'an existing concept'}; not written`,
      };
    }
    if (failed > 0) {
      return {
        color: RED,
        text: `${id} — write not confirmed (${failed} of this batch failed)`,
      };
    }
    return { color: GREEN, text: `${id} — added` };
  });
}

function ProposalTable({
  proposals,
  checked,
  locked,
  onToggle,
}: {
  proposals: ConceptProposalRow[];
  checked: boolean[];
  locked: boolean;
  onToggle: (i: number) => void;
}) {
  return (
    <table className="ob-table">
      <thead>
        <tr>
          <th aria-label="select" />
          <th>Concept</th>
          <th>Description</th>
          <th aria-label="status" />
        </tr>
      </thead>
      <tbody>
        {proposals.map((p, i) => (
          <tr key={p.id}>
            <td>
              <input
                type="checkbox"
                aria-label={`select ${p.id}`}
                checked={checked[i] === true}
                disabled={locked || p.action !== 'insert'}
                onChange={() => onToggle(i)}
              />
            </td>
            <td>
              {p.id}
              {p.name && p.name !== p.id && (
                <div style={{ color: MUTED, fontSize: '0.72rem' }}>{p.name}</div>
              )}
            </td>
            <td style={{ fontSize: '0.78rem' }}>{p.description}</td>
            <td style={{ color: MUTED, fontSize: '0.72rem' }}>{rowNote(p)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function ImportOutcome({
  sent,
  res,
  sendChat,
}: {
  sent: SentRow[];
  res: ImportResponse;
  sendChat: (msg: string) => void;
}) {
  const lines = importRowLines(sent, res);
  return (
    <div style={{ marginTop: '0.5rem' }}>
      <ul style={{ margin: 0, paddingLeft: 0, listStyle: 'none' }}>
        {lines.map((l, i) => (
          <li key={i} style={{ color: l.color, fontSize: '0.78rem' }}>
            {l.text}
          </li>
        ))}
      </ul>
      {res.failed > 0 && (
        <div style={{ color: RED, fontSize: '0.75rem', marginTop: '0.3rem' }}>
          {res.failed} write{res.failed === 1 ? '' : 's'} failed — verify in Admin →
          Vocabulary before relying on these concepts.
        </div>
      )}
      {res.inserted > 0 && (
        <div style={{ marginTop: '0.4rem' }}>
          <span style={{ color: GREEN, fontSize: '0.8rem' }}>
            {res.inserted} concept{res.inserted === 1 ? '' : 's'} added — mapping will
            use them.
          </span>{' '}
          <button
            type="button"
            className="suggestion-chip"
            onClick={() => sendChat('run map_fields')}
          >
            Run map_fields
          </button>
        </div>
      )}
    </div>
  );
}

/** Import call state — one steward-triggered write, honest error text. */
function useImport(systemId: string) {
  const [busy, setBusy] = useState(false);
  const [sent, setSent] = useState<SentRow[] | null>(null);
  const [res, setRes] = useState<ImportResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const accept = async (rows: SentRow[]) => {
    setBusy(true);
    setError(null);
    try {
      const response = await postImport(systemId, rows);
      setSent(rows);
      setRes(response);
    } catch (e) {
      setError(importErrorText(e));
    } finally {
      setBusy(false);
    }
  };
  return { busy, sent, res, error, accept };
}

function EmptyCard() {
  return (
    <div className="ob-checkpoint">
      <h4>✦ Proposed concepts</h4>
      <p style={{ color: MUTED, fontSize: '0.82rem' }}>No new concepts proposed.</p>
    </div>
  );
}

export function ConceptProposalCard({
  result,
  sendChat = welcomeSubmit,
}: {
  result: ConceptProposalResult;
  /** Chat-input dispatcher — injectable for tests; NEVER a direct agent call. */
  sendChat?: (msg: string) => void;
}) {
  const proposals = result.proposals ?? [];
  const [checked, setChecked] = useState<boolean[]>(() =>
    proposals.map((p) => p.action === 'insert'),
  );
  const { busy, sent, res, error, accept } = useImport(result.system_id);
  const selected = proposals.filter((_, i) => checked[i]);

  if (proposals.length === 0) return <EmptyCard />;
  return (
    <section aria-label="Proposed concepts" className="ob-checkpoint">
      <h4>✦ Proposed concepts</h4>
      <p style={{ color: MUTED, fontSize: '0.78rem', margin: '0.25rem 0 0.5rem' }}>
        The agent proposed these from unmatched columns. Nothing is written until you
        accept — rows already in the vocabulary can be refined via a proposal instead.
      </p>
      <ProposalTable
        proposals={proposals}
        checked={checked}
        locked={busy || res !== null}
        onToggle={(i) => setChecked((c) => c.map((v, j) => (j === i ? !v : v)))}
      />
      <button
        type="button"
        className="ob-confirm-btn"
        disabled={busy || res !== null || selected.length === 0}
        onClick={() =>
          void accept(selected.map(({ id, name, description }) => ({ id, name, description })))
        }
      >
        Accept selected ({selected.length})
      </button>
      {error && (
        <div style={{ color: RED, fontSize: '0.78rem', marginTop: '0.4rem' }}>{error}</div>
      )}
      {res && sent && <ImportOutcome sent={sent} res={res} sendChat={sendChat} />}
    </section>
  );
}

export default ConceptProposalCard;
