// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Findings ledger — GET /api/admin/evals/findings (+ ?window=7d), the 7-day
 * detail resolver, steward annotation, steward-triggered proposal filing, the
 * bound import, and the annotator's status read path (ADR-0062 D6/D10/D11).
 *
 * What the shape of this panel encodes:
 *
 *   - the LEDGER lists MINIMIZED rows only — taxonomy, opaque refs, status,
 *     timestamps. Nothing customer-derived is in them, so nothing customer-
 *     derived is on screen until a steward expands ONE row, which is the only
 *     call that resolves a ref against its 7-day detail;
 *   - the "This week" digest is COMPUTED ON READ: taxonomy counts plus a
 *     BOUNDED list of ux_gap references — the which-screens signal. The refs
 *     stay UNRESOLVED (ids only) until expanded; there is no clustering;
 *   - a detail row that aged out renders `detail expired` — a normal 7-day
 *     state, never an error — and a finding whose detail is gone can no longer
 *     be filed as a proposal (the mapping lived in that detail);
 *   - "File proposal" is explicit, askConfirm()-gated, and copy says plainly that
 *     filing is never automatic and lands as PENDING. It is the only door
 *     through which a suggested mapping reaches durable storage;
 *   - the import surfaces what the server THREW AWAY (discarded findings and
 *     dropped generated fields) beside what it recorded. Its third file input
 *     is OPTIONAL: the evidence bundle the audit ran with, sent only so
 *     references living in those documents resolve. It is never stored, and it
 *     cannot hide a discard — a reference resolving in neither file is still
 *     counted as discarded;
 *   - the annotator status line is MEASURED-OR-ABSENT: with no measured usage
 *     it renders NO usage element at all — never a zero, and nothing in money.
 *     "Never ran" is an explicit state, and an unavailable status route says so
 *     rather than showing zeros. Triggering a run is NOT a control here.
 *
 * Every server string — excerpt, rationale, steward note, mapping value —
 * renders as a LITERAL text node. Findings text is untrusted model output over
 * customer data: DATA, never markdown (repo zero-innerHTML sink policy).
 *
 * Nothing in this panel touches the golden-suite run affordance: that stays
 * run-ALL-only in EvalsTab, and this surface adds no run control of any kind. */
import { useState, type CSSProperties } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { ApiError } from '../../../../api/client';
import type {
  AuditStatusResponse,
  FindingIndexEntry,
  FindingRow,
  FindingsDigest,
  FindingsImportResult,
} from '../../../../api/types/admin';
import { askConfirm } from '../../../../shared/ui/confirm-store';
import {
  annotateFindingMutationOptions,
  evalAuditStatusQueryOptions,
  evalFindingQueryOptions,
  evalFindingsDigestQueryOptions,
  evalFindingsQueryOptions,
  fileFindingProposalMutationOptions,
  importFindingsMutationOptions,
} from '../../admin-queries';
import {
  NOTE_MAX_CHARS,
  annotateRejectionMessage,
  auditRunText,
  budgetText,
  createdAtText,
  digestCounts,
  hasSuggestedMapping,
  importSummaryLines,
  ledgerGroups,
  mappingRows,
  proposalConflict,
  taxonomyLabel,
  usageText,
  type ProposalConflict,
} from './findings-logic';

const MONO = "'IBM Plex Mono',monospace";
const h4Style: CSSProperties = { margin: '0 0 0.5rem', fontSize: '0.85rem', fontWeight: 600 };
const h5Style: CSSProperties = { margin: '0 0 0.35rem', fontSize: '0.78rem', fontWeight: 600 };
const helpStyle: CSSProperties = {
  fontSize: '0.72rem',
  color: 'var(--muted)',
  lineHeight: 1.5,
  marginBottom: '1rem',
};
const mutedStyle: CSSProperties = { fontSize: '0.75rem', color: 'var(--muted)' };
const smallMutedStyle: CSSProperties = { fontSize: '0.7rem', color: 'var(--muted)' };
const monoSmallStyle: CSSProperties = { ...smallMutedStyle, fontFamily: MONO };
const cardStyle: CSSProperties = {
  border: '1px solid var(--border)',
  borderRadius: 4,
  padding: '0.75rem',
  marginBottom: '0.75rem',
};
const chipStyle: CSSProperties = {
  display: 'inline-flex',
  gap: '0.35rem',
  alignItems: 'baseline',
  padding: '0.2rem 0.5rem',
  background: 'var(--surface2)',
  border: '1px solid var(--border)',
  borderRadius: 6,
  fontSize: '0.72rem',
};
const badgeStyle: CSSProperties = {
  padding: '0.15rem 0.45rem',
  borderRadius: 3,
  fontSize: '0.7rem',
  fontWeight: 600,
  background: 'var(--surface2)',
  color: 'var(--accent)',
  fontFamily: MONO,
};
const linkBtnStyle: CSSProperties = {
  background: 'none',
  border: 'none',
  color: 'var(--accent)',
  fontSize: '0.72rem',
  cursor: 'pointer',
  padding: 0,
  textDecoration: 'underline',
};
const actionBtnStyle: CSSProperties = {
  background: 'var(--surface2)',
  color: 'var(--text)',
  border: '1px solid var(--border)',
  borderRadius: 4,
  padding: '0.3rem 0.7rem',
  fontSize: '0.75rem',
  cursor: 'pointer',
};
const primaryBtnStyle: CSSProperties = {
  ...actionBtnStyle,
  background: 'var(--accent)',
  color: '#000',
  border: 'none',
  fontWeight: 600,
};
const inputStyle: CSSProperties = {
  background: 'var(--surface2)',
  color: 'var(--text)',
  border: '1px solid var(--border)',
  borderRadius: 4,
  padding: '0.3rem 0.55rem',
  fontSize: '0.75rem',
  minWidth: 280,
};
const proseStyle: CSSProperties = {
  fontSize: '0.75rem',
  color: 'var(--text2)',
  whiteSpace: 'pre-wrap',
  wordBreak: 'break-word',
};

function errorDetail(error: unknown): string {
  return error instanceof ApiError ? error.detail : error instanceof Error ? error.message : String(error);
}

// ── "This week" digest ──────────────────────────────────────────────────────

/** Taxonomy counts plus the BOUNDED ux_gap reference list. The refs are the
 * which-screens signal and stay unresolved: each one carries only its
 * finding_id and its opaque question_ref until a steward expands it. */
function DigestSection({
  digest,
  onExpand,
}: {
  digest: FindingsDigest;
  onExpand: (findingId: string) => void;
}) {
  const counts = digestCounts(digest);
  const refs = digest.ux_gap_refs ?? [];
  return (
    <div id="evals-findings-digest" style={cardStyle}>
      <h5 style={h5Style}>This week</h5>
      <div style={{ ...smallMutedStyle, marginBottom: '0.5rem' }}>
        Computed on read over the last {digest.window_days ?? 7} days — {digest.total ?? 0} findings.
        Nothing is written to produce this view.
      </div>
      {counts.length === 0 ? (
        <div id="evals-findings-digest-empty" style={mutedStyle}>
          No findings recorded in this window.
        </div>
      ) : (
        <div style={{ display: 'flex', gap: '0.4rem', flexWrap: 'wrap' }}>
          {counts.map((row) => (
            <span
              key={row.taxonomy}
              className="evals-digest-count"
              data-taxonomy={row.taxonomy}
              style={chipStyle}
            >
              <strong>{row.count}</strong> {row.label}
            </span>
          ))}
        </div>
      )}
      {refs.length > 0 && (
        <div style={{ marginTop: '0.6rem' }}>
          <div style={{ ...smallMutedStyle, marginBottom: '0.25rem' }}>
            UX gap references — which screens the findings point at. These are references only;
            expanding one resolves it against its 7-day detail.
          </div>
          <ul id="evals-digest-ux-gaps" style={{ margin: 0, paddingLeft: '1rem' }}>
            {refs.map((ref) => (
              <li
                key={ref.finding_id ?? ref.question_ref}
                className="evals-digest-ux-gap"
                data-finding-id={ref.finding_id}
                style={monoSmallStyle}
              >
                {ref.finding_id ?? '—'} · question {ref.question_ref ?? '—'}{' '}
                <button
                  type="button"
                  className="evals-digest-expand"
                  data-finding-id={ref.finding_id}
                  style={linkBtnStyle}
                  onClick={() => ref.finding_id && onExpand(ref.finding_id)}
                >
                  Expand
                </button>
              </li>
            ))}
          </ul>
          {digest.ux_gap_refs_truncated && (
            <div id="evals-digest-ux-gaps-truncated" style={smallMutedStyle}>
              More UX gap findings exist than the bounded reference list shows — the ledger below
              lists them all.
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ── taxonomy-grouped ledger (minimized rows) ────────────────────────────────

function LedgerRow({
  entry,
  expanded,
  onExpand,
}: {
  entry: FindingIndexEntry;
  expanded: boolean;
  onExpand: (findingId: string) => void;
}) {
  return (
    <tr className="evals-findings-row" data-finding-id={entry.finding_id}>
      <td className="mono">{entry.finding_id}</td>
      <td className="mono">{entry.question_ref ?? '—'}</td>
      <td className="mono">{(entry.source_report_sha ?? '').slice(0, 12) || '—'}</td>
      <td>{createdAtText(entry.created_at)}</td>
      <td>
        <button
          type="button"
          className="evals-findings-expand"
          data-finding-id={entry.finding_id}
          style={linkBtnStyle}
          onClick={() => onExpand(entry.finding_id)}
        >
          {expanded ? 'Collapse' : 'Expand'}
        </button>
      </td>
    </tr>
  );
}

function LedgerGroups({
  groups,
  expandedId,
  onExpand,
}: {
  groups: ReturnType<typeof ledgerGroups>;
  expandedId: string | null;
  onExpand: (findingId: string) => void;
}) {
  return (
    <div id="evals-findings-ledger">
      {groups.map((group) => (
        <div
          key={group.taxonomy}
          className="evals-findings-group"
          data-taxonomy={group.taxonomy}
          style={{ marginBottom: '1rem' }}
        >
          <h5 style={h5Style}>
            {group.label} <span style={smallMutedStyle}>({group.count})</span>
          </h5>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Finding</th>
                  <th>Question ref</th>
                  <th>Report</th>
                  <th>Recorded</th>
                  <th>Detail</th>
                </tr>
              </thead>
              <tbody>
                {group.entries.map((entry) => (
                  <LedgerRow
                    key={entry.finding_id}
                    entry={entry}
                    expanded={entry.finding_id === expandedId}
                    onExpand={onExpand}
                  />
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ))}
    </div>
  );
}

// ── one resolved finding: the 7-day detail, annotation, filing ──────────────

/** The 7-day customer-derived block, or its honest expired state. */
function DetailBody({ row }: { row: FindingRow }) {
  if (row.detail_expired || !row.detail) {
    return (
      <div id="evals-finding-detail-expired" style={{ ...mutedStyle, marginBottom: '0.5rem' }}>
        Detail expired — the customer-derived detail (claim excerpt, rationale, suggested mapping)
        is kept 7 days and has aged out. The minimized finding row remains; a proposal can no
        longer be filed from it. Re-import the findings to restore the detail.
      </div>
    );
  }
  const mapping = mappingRows(row);
  return (
    <div id="evals-finding-detail-body" style={{ marginBottom: '0.5rem' }}>
      <div style={{ marginBottom: '0.35rem' }}>
        <span className="evals-finding-dataclass" style={badgeStyle}>
          {row.detail.data_class ?? 'customer-derived'}
        </span>{' '}
        <span style={smallMutedStyle}>kept 7 days, then removed</span>
      </div>
      {row.detail.claim_excerpt ? (
        <div className="evals-finding-excerpt" style={proseStyle}>
          Claim: {row.detail.claim_excerpt}
        </div>
      ) : null}
      {row.detail.rationale ? (
        <div className="evals-finding-rationale" style={proseStyle}>
          Rationale: {row.detail.rationale}
        </div>
      ) : null}
      {mapping.length > 0 && (
        <ul className="evals-finding-mapping" style={{ ...monoSmallStyle, paddingLeft: '1rem' }}>
          {mapping.map((pair) => (
            <li key={pair.key}>
              {pair.key}: {pair.value}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** Bounded steward annotation. A refused note surfaces FIXED copy keyed by the
 * server's reason — the rejected text is never printed back. */
function AnnotateForm({ findingId }: { findingId: string }) {
  const queryClient = useQueryClient();
  const annotateMutation = useMutation(annotateFindingMutationOptions(queryClient));
  const [note, setNote] = useState('');
  const [status, setStatus] = useState('');

  const submit = async (nextStatus: 'annotated' | 'dismissed') => {
    setStatus('');
    try {
      await annotateMutation.mutateAsync({
        findingId,
        status: nextStatus,
        ...(note.trim() ? { note: note.trim() } : {}),
      });
      setStatus(nextStatus === 'dismissed' ? 'Dismissed.' : 'Annotation saved.');
    } catch (error) {
      setStatus(annotateRejectionMessage(error));
    }
  };

  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', flexWrap: 'wrap' }}>
      <input
        id="evals-finding-note"
        type="text"
        aria-label="Steward note"
        placeholder={`Steward note (optional, ≤${NOTE_MAX_CHARS} chars)`}
        maxLength={NOTE_MAX_CHARS}
        value={note}
        style={inputStyle}
        onChange={(e) => setNote(e.target.value)}
      />
      <button
        id="evals-finding-annotate-btn"
        type="button"
        style={actionBtnStyle}
        disabled={annotateMutation.isPending}
        onClick={() => void submit('annotated')}
      >
        Save annotation
      </button>
      <button
        id="evals-finding-dismiss-btn"
        type="button"
        style={actionBtnStyle}
        disabled={annotateMutation.isPending}
        onClick={() => void submit('dismissed')}
      >
        Dismiss
      </button>
      {status && (
        <span id="evals-finding-annotate-status" role="status" style={smallMutedStyle}>
          {status}
        </span>
      )}
    </div>
  );
}

/** The ONE door for a suggested mapping — explicit, askConfirm()-gated, and it
 * lands as a PENDING proposal. The two 409s render as distinct states. */
function FileProposalAction({ findingId }: { findingId: string }) {
  const queryClient = useQueryClient();
  const fileMutation = useMutation(fileFindingProposalMutationOptions(queryClient));
  const [message, setMessage] = useState('');
  const [conflict, setConflict] = useState<ProposalConflict['kind'] | ''>('');

  const file = async () => {
    const fileIt = await askConfirm({
      key: `evals:file-proposal:${findingId}`,
      title: 'File this suggested mapping as a proposal?',
      body: 'Filing is never automatic: the mapping enters the proposal queue as pending for a steward to accept or reject, and nothing is applied to the graph now.',
      confirmLabel: 'File proposal',
    });
    if (!fileIt) return;
    setMessage('');
    setConflict('');
    try {
      const result = await fileMutation.mutateAsync(findingId);
      setMessage(
        result.status === 'filed'
          ? `Filed as pending proposal ${result.mapping_id ?? ''} — a steward accepts or rejects it in the proposals surface.`
          : 'The learning pipeline absorbed this mapping, so nothing landed for review and the finding was not marked filed.',
      );
    } catch (error) {
      const outcome = proposalConflict(error);
      setConflict(outcome.kind);
      setMessage(outcome.message);
    }
  };

  return (
    <div style={{ marginTop: '0.5rem' }}>
      <button
        id="evals-finding-file-proposal"
        type="button"
        style={primaryBtnStyle}
        disabled={fileMutation.isPending}
        onClick={() => void file()}
      >
        File proposal
      </button>
      <div style={{ ...smallMutedStyle, marginTop: '0.25rem' }}>
        Filing is never automatic. This is the only door through which a suggested mapping reaches
        storage, and it enters the proposal queue as pending for steward review.
      </div>
      {message && (
        <div
          id="evals-finding-proposal-status"
          role="status"
          data-conflict={conflict || undefined}
          style={{ ...smallMutedStyle, marginTop: '0.25rem' }}
        >
          {message}
        </div>
      )}
    </div>
  );
}

function StewardAnnotation({ row }: { row: FindingRow }) {
  const steward = row.steward;
  if (!steward) return null;
  return (
    <div className="evals-finding-steward" style={{ ...smallMutedStyle, marginBottom: '0.4rem' }}>
      Steward: {steward.status ?? '—'} · {createdAtText(steward.annotated_at)} ·{' '}
      {steward.annotated_by || 'unknown'}
      {steward.steward_note ? (
        <div className="evals-finding-steward-note" style={proseStyle}>
          {steward.steward_note}
        </div>
      ) : null}
    </div>
  );
}

/** The expanded finding — the ONLY fetch that resolves refs (one per expand). */
function FindingDetail({ findingId }: { findingId: string }) {
  const detailQuery = useQuery(evalFindingQueryOptions(findingId));
  if (detailQuery.isPending) {
    return (
      <div id="evals-finding-detail" style={{ ...cardStyle, ...mutedStyle }}>
        Loading finding…
      </div>
    );
  }
  if (detailQuery.isError) {
    return (
      <div id="evals-finding-detail" role="alert" style={{ ...cardStyle, ...mutedStyle }}>
        Finding unavailable: {errorDetail(detailQuery.error)}
      </div>
    );
  }
  const row = detailQuery.data;
  const usage = usageText(row.usage);
  return (
    <div id="evals-finding-detail" style={cardStyle}>
      <div style={{ marginBottom: '0.4rem' }}>
        <span className="evals-finding-id" style={badgeStyle}>
          {row.finding_id}
        </span>{' '}
        <span style={smallMutedStyle}>
          {taxonomyLabel(row.taxonomy)} · status {row.status ?? '—'} · question{' '}
          {row.question_ref ?? '—'} · recorded {createdAtText(row.created_at)}
        </span>
      </div>
      {/* Measured tokens or NOTHING — an unmeasured run gets no element here. */}
      {usage ? (
        <div className="evals-finding-usage" style={monoSmallStyle}>
          {usage}
        </div>
      ) : null}
      <DetailBody row={row} />
      <StewardAnnotation row={row} />
      <AnnotateForm findingId={findingId} />
      {hasSuggestedMapping(row) && <FileProposalAction findingId={findingId} />}
    </div>
  );
}

// ── bound import (findings document + its source report, together) ──────────

interface ImportFiles {
  findingsText: string;
  reportText: string;
  evidenceText: string;
}

function parseFindingsDoc(text: string): { doc?: unknown; error?: string } {
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    return { error: 'The findings file is not valid JSON.' };
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    return { error: 'The findings file must be a JSON object.' };
  }
  if (!Array.isArray((parsed as { findings?: unknown }).findings)) {
    return { error: 'The findings file needs a "findings" array.' };
  }
  return { doc: parsed };
}

/** The OPTIONAL evidence bundle. Empty ⇒ nothing to send (the key is omitted
 * entirely). A single document needs no wrapper array — the auditor is usually
 * pointed at one file. Shape is checked here so a malformed pick is refused
 * before a request, matching the server's `invalid_evidence` posture rather
 * than silently importing without it. */
function parseEvidenceDocs(text: string): { docs?: unknown[]; error?: string } {
  if (!text.trim()) return {};
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    return { error: 'The evidence file is not valid JSON.' };
  }
  const docs = Array.isArray(parsed) ? parsed : [parsed];
  const objects = docs.every((doc) => !!doc && typeof doc === 'object' && !Array.isArray(doc));
  if (!objects) {
    return { error: 'The evidence file must be a JSON object or a list of JSON objects.' };
  }
  return { docs };
}

function readText(file: File, onText: (text: string) => void, onError: (message: string) => void) {
  const reader = new FileReader();
  reader.onload = () => onText(typeof reader.result === 'string' ? reader.result : '');
  reader.onerror = () => onError('Could not read the selected file.');
  reader.readAsText(file);
}

function ImportResultView({ result }: { result: FindingsImportResult }) {
  const lines = importSummaryLines(result);
  return (
    <div id="evals-findings-import-result" style={{ marginTop: '0.5rem' }}>
      <ul style={{ ...smallMutedStyle, margin: 0, paddingLeft: '1rem' }}>
        {lines.map((line) => (
          <li key={line}>{line}</li>
        ))}
      </ul>
      {result.source_report_sha ? (
        <div style={monoSmallStyle}>report {result.source_report_sha.slice(0, 16)}</div>
      ) : null}
    </div>
  );
}

/** One file picker in the import card. Shared by all three parts, so the
 * optional evidence input is not a third copy of the same markup. */
function ImportFileInput({
  id,
  label,
  onPick,
  hint,
}: {
  id: string;
  label: string;
  onPick: (file: File | undefined) => void;
  hint?: string;
}) {
  return (
    <div style={{ marginBottom: '0.45rem' }}>
      <input
        id={id}
        type="file"
        accept=".json,application/json"
        aria-label={label}
        style={{ fontSize: '0.75rem', color: 'var(--text2)' }}
        onChange={(e) => onPick(e.target.files?.[0])}
      />
      {hint ? (
        <div id={`${id}-hint`} style={smallMutedStyle}>
          {hint}
        </div>
      ) : null}
    </div>
  );
}

/** The import affordance. Both halves are required: the findings document and
 * the EXACT report text it was derived from (the binding is a sha over those
 * bytes, so the report is sent as read — never re-serialized).
 *
 * A THIRD part is optional: the evidence bundle the audit ran with
 * (`audit_answer.py --evidence <path>...`). Findings can legitimately cite ids
 * that exist only in those documents, and without them those findings come
 * back discarded. It is sent for reference resolution ONLY, is never stored,
 * and cannot hide a discard — a reference that resolves in neither the report
 * nor the evidence is still counted as discarded in the summary below. */
function FindingsImport() {
  const queryClient = useQueryClient();
  const importMutation = useMutation(importFindingsMutationOptions(queryClient));
  const [open, setOpen] = useState(false);
  const [files, setFiles] = useState<ImportFiles>({
    findingsText: '',
    reportText: '',
    evidenceText: '',
  });
  const [status, setStatus] = useState('');
  const [result, setResult] = useState<FindingsImportResult | null>(null);

  const pick = (which: keyof ImportFiles) => (file: File | undefined) => {
    if (!file) return;
    setResult(null);
    setStatus('');
    readText(
      file,
      (text) => setFiles((prev) => ({ ...prev, [which]: text })),
      (message) => setStatus(message),
    );
  };

  const submit = async () => {
    setStatus('');
    setResult(null);
    const { doc, error } = parseFindingsDoc(files.findingsText);
    if (error || !doc) {
      setStatus(error || 'Invalid findings file.');
      return;
    }
    if (!files.reportText.trim()) {
      setStatus('The source report is required — the import binds findings to the report they came from.');
      return;
    }
    const evidence = parseEvidenceDocs(files.evidenceText);
    if (evidence.error) {
      setStatus(evidence.error);
      return;
    }
    try {
      setResult(
        await importMutation.mutateAsync({
          findingsFile: doc,
          sourceReport: files.reportText,
          ...(evidence.docs ? { evidenceDocs: evidence.docs } : {}),
        }),
      );
    } catch (err) {
      setStatus('Import refused: ' + errorDetail(err));
    }
  };

  const ready = !!files.findingsText && !!files.reportText;
  return (
    <div id="evals-findings-import" style={{ marginTop: '1rem' }}>
      <button
        id="evals-findings-import-toggle"
        type="button"
        style={actionBtnStyle}
        onClick={() => setOpen(!open)}
      >
        {open ? 'Hide import' : 'Import findings'}
      </button>
      {open && (
        <div style={{ ...cardStyle, marginTop: '0.5rem' }}>
          <div style={{ ...smallMutedStyle, marginBottom: '0.5rem' }}>
            Upload the findings file from scripts/audit_answer.py TOGETHER WITH the exact report it
            was derived from — the server binds them by digest and refuses a mismatch. Only
            minimized rows are stored; the uploaded report is never persisted, and re-importing a
            report replaces its earlier generated rows (steward annotations survive).
          </div>
          <ImportFileInput
            id="evals-findings-file"
            label="Findings file"
            onPick={pick('findingsText')}
          />
          <ImportFileInput
            id="evals-findings-report"
            label="Source report file"
            onPick={pick('reportText')}
          />
          <ImportFileInput
            id="evals-findings-evidence"
            label="Evidence bundle file (optional)"
            onPick={pick('evidenceText')}
            hint={
              'Optional — the evidence bundle the audit ran with (audit_answer.py --evidence). ' +
              'Supply it only if findings reference ids that live in those documents rather than ' +
              'in the report; it is used to resolve those references and is never stored. ' +
              'References that resolve in neither file are still reported as discarded.'
            }
          />
          {files.evidenceText ? (
            <div id="evals-findings-evidence-loaded" style={smallMutedStyle}>
              Evidence bundle attached — sent for reference resolution only, and not stored.
            </div>
          ) : null}
          <button
            id="evals-findings-import-btn"
            type="button"
            style={{ ...primaryBtnStyle, opacity: ready ? 1 : 0.5, marginTop: '0.25rem' }}
            disabled={!ready || importMutation.isPending}
            onClick={() => void submit()}
          >
            {importMutation.isPending ? 'Importing…' : 'Import'}
          </button>
          {status && (
            <span
              id="evals-findings-import-status"
              role="status"
              style={{ ...smallMutedStyle, marginLeft: '0.5rem' }}
            >
              {status}
            </span>
          )}
          {result && <ImportResultView result={result} />}
        </div>
      )}
    </div>
  );
}

// ── annotator status (read path only — triggering a run lives elsewhere) ────

function AuditRuns({ runs }: { runs: NonNullable<AuditStatusResponse['runs']> }) {
  return (
    <ul id="evals-audit-runs" style={{ ...smallMutedStyle, margin: '0.25rem 0 0', paddingLeft: '1rem' }}>
      {runs.map((run) => {
        const usage = usageText(run.usage);
        return (
          <li key={run.run_id} className="evals-audit-run" data-run-id={run.run_id}>
            <span style={{ fontFamily: MONO }}>{run.run_id}</span> · {auditRunText(run)}
            {/* Measured tokens or NOTHING — never a fabricated zero. */}
            {usage ? <span className="evals-audit-run-usage"> · {usage}</span> : null}
          </li>
        );
      })}
    </ul>
  );
}

/** Status of the bounded triage annotation job. Its counters come from the
 * artifact-usage ledger (ADR-0052/D10) — this panel only reads them. */
function AnnotatorStatus() {
  const statusQuery = useQuery(evalAuditStatusQueryOptions());
  const data = statusQuery.data;
  const runs = data?.runs ?? [];
  const budget = budgetText(data?.budget);
  const telemetry = Object.entries(data?.telemetry ?? {});
  return (
    <div id="evals-audit-status" style={{ marginTop: '1.25rem' }}>
      <h5 style={h5Style}>Bounded triage annotation</h5>
      <div style={{ ...smallMutedStyle, marginBottom: '0.4rem' }}>
        A bounded job that annotates flagged items from feedback and recent report failures — not
        the deep-context CLI auditor. Scheduled runs are off unless this environment enables them.
      </div>
      {statusQuery.isPending && <div style={mutedStyle}>Loading annotator status…</div>}
      {statusQuery.isError && (
        <div id="evals-audit-status-unavailable" style={mutedStyle}>
          Annotator status unavailable: {errorDetail(statusQuery.error)}
        </div>
      )}
      {statusQuery.isSuccess && (
        <>
          {runs.length === 0 ? (
            <div id="evals-audit-never-ran" style={mutedStyle}>
              The bounded triage annotator has never run in this environment.
            </div>
          ) : (
            <AuditRuns runs={runs} />
          )}
          {/* Budget/telemetry render ONLY when the server reported them. */}
          {budget ? (
            <div id="evals-audit-budget" style={smallMutedStyle}>
              {budget}
              {data?.budget?.durable === false
                ? ' — in-process counter (no durable budget record configured)'
                : ''}
            </div>
          ) : null}
          {telemetry.length > 0 && (
            <div id="evals-audit-telemetry" style={monoSmallStyle}>
              usage ledger:{' '}
              {telemetry.map(([kind, count]) => `${kind} ${count}`).join(' · ')}
            </div>
          )}
        </>
      )}
    </div>
  );
}

// ── the panel ───────────────────────────────────────────────────────────────

export default function FindingsLedger() {
  const digestQuery = useQuery(evalFindingsDigestQueryOptions(7));
  const ledgerQuery = useQuery(evalFindingsQueryOptions());
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const groups = ledgerGroups(ledgerQuery.data);

  const toggleExpanded = (findingId: string) =>
    setExpandedId((current) => (current === findingId ? null : findingId));

  return (
    <div id="evals-findings-section">
      <h4 style={h4Style}>Findings ledger</h4>
      <p style={helpStyle}>
        Root-cause findings from answer audits. Stored rows are minimized — taxonomy, opaque
        references, status, timestamps — and the generated detail behind each one is customer-derived
        and kept 7 days, so it resolves only when you expand a finding. Nothing here files a
        proposal on its own: a suggested mapping reaches storage only when you file it, and then
        only as pending.
      </p>

      {digestQuery.isError && (
        <div id="evals-findings-digest-unavailable" style={{ ...mutedStyle, marginBottom: '0.75rem' }}>
          Weekly digest unavailable: {errorDetail(digestQuery.error)}
        </div>
      )}
      {digestQuery.isSuccess && (
        <DigestSection digest={digestQuery.data} onExpand={toggleExpanded} />
      )}

      {ledgerQuery.isPending && <div style={mutedStyle}>Loading findings…</div>}
      {ledgerQuery.isError && (
        <div id="evals-findings-unavailable" role="alert" style={mutedStyle}>
          Findings ledger unavailable: {errorDetail(ledgerQuery.error)}{' '}
          <button type="button" style={linkBtnStyle} onClick={() => void ledgerQuery.refetch()}>
            Retry
          </button>
        </div>
      )}
      {ledgerQuery.isSuccess && groups.length === 0 && (
        <div id="evals-findings-empty" style={mutedStyle}>
          No findings recorded yet — import a findings file from an answer-audit run below.
        </div>
      )}
      {groups.length > 0 && (
        <LedgerGroups groups={groups} expandedId={expandedId} onExpand={toggleExpanded} />
      )}

      {expandedId && <FindingDetail key={expandedId} findingId={expandedId} />}

      <FindingsImport />
      <AnnotatorStatus />
    </div>
  );
}
