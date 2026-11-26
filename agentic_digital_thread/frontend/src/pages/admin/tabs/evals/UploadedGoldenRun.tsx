// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Upload-and-run a customer golden set (ADR-0064) — a SECOND, clearly
 * separated affordance beside the packaged "Run full golden suite" button; it
 * never replaces it, and neither affordance offers a subset control (the
 * run-ALL pin is golden-suite scoped and holds for BOTH).
 *
 * Data posture, which is the whole point of the feature:
 *   - the chosen file is read at SUBMIT time only. Its bytes go into the
 *     request body and nowhere else — never component state, never the query
 *     cache (`gcTime: 0` on the mutation), never localStorage/sessionStorage/
 *     IndexedDB. `reset()` after the call drops the mutation's variables too;
 *   - the server persists NONE of the file, so a re-run needs a re-upload —
 *     that is the design, not a gap (ADR-0064 D1);
 *   - a custom-set run's report is answer-text REDACTED and expires after
 *     7 days (D5), which is why its history rows carry no excerpts;
 *   - every refusal renders its OWN message plus the server's verbatim detail,
 *     including the computed sha on the BOM/non-UTF-8 case so a steward can
 *     reconcile against their own file. File content is never echoed back.
 *
 * Every value renders as a literal text node — zero HTML-string sinks. */
import { useRef, useState, type CSSProperties } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';

import type { EvalRunStartResult } from '../../../../api/types/admin';
import { askConfirm } from '../../../../shared/ui/confirm-store';
import { startUploadedEvalRunMutationOptions } from '../../admin-queries';
import { goldenSourceLabel, uploadRefusalOf, type UploadRefusal } from './evals-logic';

const h4Style: CSSProperties = { margin: '0 0 0.5rem', fontSize: '0.85rem', fontWeight: 600 };
const helpStyle: CSSProperties = {
  fontSize: '0.72rem',
  color: 'var(--muted)',
  lineHeight: 1.5,
  margin: '0 0 0.6rem',
};
const smallMutedStyle: CSSProperties = { fontSize: '0.72rem', color: 'var(--muted)' };
const monoSmallStyle: CSSProperties = {
  ...smallMutedStyle,
  fontFamily: "'IBM Plex Mono',monospace",
};
const runBtnStyle: CSSProperties = {
  background: 'var(--surface2)',
  color: 'var(--text)',
  fontWeight: 600,
  padding: '0.4rem 1rem',
  border: '1px solid var(--border)',
  borderRadius: 4,
  fontSize: '0.8rem',
};

/** The confirm-gate copy. It names the cost AND the data posture in the
 * steward's own terms — the boundary is only real if they can read it. The
 * body's blank-line breaks render as separate paragraphs (ConfirmHost). */
export const UPLOAD_CONFIRM_TITLE = 'Run your golden set against this deployment?';
export const UPLOAD_CONFIRM_BODY =
  'The file is used for this run only: it is never stored on the server and never committed to the ADT repository, so a re-run needs a re-upload.\n\n'
  + 'Answer text from this run is not persisted. The report keeps verdicts, question ids and deterministic failures for 7 days.\n\n'
  + 'Every question is asked against the live orchestrator: this takes minutes and incurs model cost.';

/** Read the chosen file's RAW bytes. FileReader (not Blob.arrayBuffer) is the
 * repo's file-read idiom and works everywhere the tab does. The bytes exist in
 * this promise's resolution and the request body only. */
function readBytes(file: File): Promise<ArrayBuffer> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const result = reader.result;
      if (result instanceof ArrayBuffer) resolve(result);
      else reject(new Error('Could not read the selected file.'));
    };
    reader.onerror = () => reject(new Error('Could not read the selected file.'));
    reader.readAsArrayBuffer(file);
  });
}

interface StartedFacts {
  sha: string;
  questionCount: number | null;
}

export default function UploadedGoldenRun({
  variant,
  disabled,
  onStarted,
}: {
  /** The shared variant label — steward text, so the copy warns about it. */
  variant: string;
  /** True while a run is active or a start is in flight (server-enforced too:
   * the one-run lock is shared with the packaged run and 409s). */
  disabled: boolean;
  onStarted: (runId: string) => void;
}) {
  const queryClient = useQueryClient();
  const uploadMutation = useMutation(startUploadedEvalRunMutationOptions(queryClient));
  const fileRef = useRef<HTMLInputElement>(null);
  // A boolean, NOT the file or its name: nothing about the chosen file is held
  // in component state.
  const [hasFile, setHasFile] = useState(false);
  const [status, setStatus] = useState('');
  const [started, setStarted] = useState<StartedFacts | null>(null);
  const [refusal, setRefusal] = useState<UploadRefusal | null>(null);

  const busy = disabled || uploadMutation.isPending;

  const runUploaded = async () => {
    const file = fileRef.current?.files?.[0];
    if (!file) {
      setStatus('Choose a golden-set JSON file first.');
      return;
    }
    // askConfirm()-gated like the packaged run (the steward-action convention).
    const run = await askConfirm({
      key: 'evals:run-uploaded',
      title: UPLOAD_CONFIRM_TITLE,
      body: UPLOAD_CONFIRM_BODY,
      confirmLabel: 'Run uploaded set',
    });
    if (!run) return;
    setStatus('Reading the file…');
    setStarted(null);
    setRefusal(null);
    let bytes: ArrayBuffer;
    try {
      bytes = await readBytes(file);
    } catch {
      setStatus('Could not read the selected file.');
      return;
    }
    setStatus('Starting the run…');
    let result: EvalRunStartResult;
    try {
      result = await uploadMutation.mutateAsync({ bytes, variant });
    } catch (error) {
      uploadMutation.reset();
      setStatus('');
      setRefusal(uploadRefusalOf(error));
      return;
    }
    // Drop the mutation's variables (the bytes) the moment the call settles —
    // BEFORE any state update renders the run as started. A `finally` after
    // setStarted()/onStarted() left a window in which the UI already said
    // "started" while the bytes were still held (caught by EvalsTab.test.tsx
    // under CI load, 2026-09-14). gcTime: 0 already keeps them out of the cache.
    uploadMutation.reset();
    setStatus('Run started.');
    setStarted({
      sha: result.golden_set_sha ?? '',
      questionCount: typeof result.question_count === 'number' ? result.question_count : null,
    });
    // A re-run needs a re-upload — clear the picker so that is visible in the
    // UI rather than only true on the server.
    if (fileRef.current) fileRef.current.value = '';
    setHasFile(false);
    if (result.run_id) onStarted(result.run_id);
  };

  return (
    <div id="evals-upload-section" style={{ marginBottom: '1rem' }}>
      <h4 style={h4Style}>Run your own golden set ({goldenSourceLabel('uploaded')})</h4>
      <p style={helpStyle}>
        Attach a golden set in the same file schema as the packaged demo set and this deployment
        scores ALL of it through the same computed lane — the whole set, every time (there is no
        subset selection here either). Your file is used for THIS RUN ONLY: it is never stored on
        the server and never committed to the ADT repository, so it stays in your own repository
        and a re-run needs a re-upload. Answer text from a custom-set run is NOT persisted, which
        is why its history rows show no answer excerpts; the report keeps verdicts, question ids
        and deterministic failures for 7 days and then expires. Question ids and the variant label
        ARE persisted and are your own authored strings — keep customer identity out of both.
      </p>
      <div
        id="evals-upload-run-form"
        style={{ display: 'flex', alignItems: 'center', gap: '0.75rem', flexWrap: 'wrap' }}
      >
        <input
          id="evals-upload-file-input"
          type="file"
          accept=".json,application/json"
          aria-label="Golden set JSON file"
          style={{ fontSize: '0.75rem', color: 'var(--text2)' }}
          onChange={(e) => {
            // Only whether a file is chosen is recorded — never its bytes or
            // its name. The bytes are read at submit time.
            setHasFile(!!e.target.files?.length);
            setStatus('');
            setStarted(null);
            setRefusal(null);
          }}
          ref={fileRef}
        />
        <button
          id="evals-upload-run-btn"
          type="button"
          style={{
            ...runBtnStyle,
            opacity: busy || !hasFile ? 0.5 : 1,
            cursor: busy || !hasFile ? 'not-allowed' : 'pointer',
          }}
          disabled={busy || !hasFile}
          onClick={() => void runUploaded()}
        >
          Run uploaded set
        </button>
        <span id="evals-upload-status" style={smallMutedStyle}>
          {status}
        </span>
      </div>
      {started ? (
        <div id="evals-upload-started" style={{ ...smallMutedStyle, marginTop: '0.5rem' }}>
          <div style={monoSmallStyle}>
            goldens {started.sha || '—'}
            {started.questionCount != null ? ` · ${started.questionCount} questions` : ''}
          </div>
          <div>
            That sha is sha256 over the exact bytes the server received. Verify it against your own
            file with:{' '}
            <span style={monoSmallStyle}>
              {'python3 -c "from agents import eval_core; print(eval_core.golden_file_sha(\'<path>\'))"'}
            </span>
          </div>
        </div>
      ) : null}
      {refusal ? (
        <div
          id="evals-upload-refusal"
          role="alert"
          style={{ ...smallMutedStyle, marginTop: '0.5rem', color: 'var(--red, #f87171)' }}
        >
          <div>{refusal.message}</div>
          {refusal.detail && refusal.detail !== refusal.message ? (
            <div className="evals-upload-refusal-detail" style={monoSmallStyle}>
              {refusal.reason ? `${refusal.reason}: ` : ''}
              {refusal.detail}
            </div>
          ) : null}
          {refusal.sha ? (
            <div className="evals-upload-refusal-sha" style={monoSmallStyle}>
              sha of the bytes the server received: {refusal.sha} — compare it against your file
              (a byte-order mark changes the digest).
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
