// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Bulk vocabulary import panel — POST /api/admin/vocabulary/import with the
 * flat custom-pack shape {metadata:{name,...}, concepts:[{id,name,description}]}.
 * File picker + textarea, client-side shape validation BEFORE any request,
 * a dry-run Preview (POST /import/preview — zero graph writes) with a per-row
 * insert / skip-exact / skip-collision action table, and a result panel with
 * the inserted/skipped/failed counters, skipped_ids, and the normalized-id
 * collision report. Styles mirror tabs/SettingsTab.tsx. JSX only — no
 * HTML-string sinks. */
import { useState, type CSSProperties } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';

import { ApiError } from '../../../../api/client';
import type {
  VocabularyImportPack,
  VocabularyImportPreview,
  VocabularyImportPreviewAction,
  VocabularyImportPreviewRow,
  VocabularyImportResult,
} from '../../../../api/types/vocabulary';
import {
  importVocabularyMutationOptions,
  previewVocabularyImportMutationOptions,
} from '../../admin-queries';

const MONO = "'IBM Plex Mono',monospace";
const textareaStyle: CSSProperties = {
  width: '100%',
  minHeight: 140,
  padding: '0.5rem 0.75rem',
  background: 'var(--surface2)',
  border: '1px solid var(--border)',
  borderRadius: 4,
  color: 'var(--text)',
  fontSize: '0.78rem',
  fontFamily: MONO,
  boxSizing: 'border-box',
  resize: 'vertical',
};
const sectionHelpStyle: CSSProperties = {
  fontSize: '0.72rem',
  color: 'var(--muted)',
  lineHeight: 1.5,
  marginBottom: '1rem',
};
const saveBtnStyle: CSSProperties = {
  background: 'var(--accent)',
  color: '#000',
  fontWeight: 600,
  padding: '0.4rem 1rem',
  border: 'none',
  borderRadius: 4,
  fontSize: '0.8rem',
  cursor: 'pointer',
};
const previewBtnStyle: CSSProperties = {
  ...saveBtnStyle,
  background: 'var(--surface2)',
  color: 'var(--text)',
  border: '1px solid var(--border)',
};
const counterStyle: CSSProperties = {
  display: 'inline-flex',
  alignItems: 'baseline',
  gap: '0.35rem',
  padding: '0.35rem 0.75rem',
  background: 'var(--surface2)',
  border: '1px solid var(--border)',
  borderRadius: 6,
  fontSize: '0.78rem',
};
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

function errorDetail(error: unknown): string {
  return error instanceof ApiError ? error.detail : error instanceof Error ? error.message : String(error);
}

/** Client-side shape validation for the flat pack JSON. Returns the typed
 * pack or a human-readable error — no request leaves the browser on error. */
export function validateImportPack(raw: string): { pack?: VocabularyImportPack; error?: string } {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return { error: 'Not valid JSON.' };
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    return { error: 'Expected a JSON object with "metadata" and "concepts".' };
  }
  const obj = parsed as Record<string, unknown>;
  const metadata = obj.metadata as Record<string, unknown> | undefined;
  if (!metadata || typeof metadata !== 'object' || Array.isArray(metadata) || !String(metadata.name ?? '').trim()) {
    return { error: '"metadata.name" is required.' };
  }
  const concepts = obj.concepts;
  if (!Array.isArray(concepts) || concepts.length === 0) {
    return { error: '"concepts" must be a non-empty array.' };
  }
  for (let i = 0; i < concepts.length; i += 1) {
    const c = concepts[i] as Record<string, unknown> | null;
    if (!c || typeof c !== 'object' || Array.isArray(c)) {
      return { error: `Concept #${i + 1} is not an object.` };
    }
    if (!String(c.id ?? '').trim() || !String(c.name ?? '').trim()) {
      return { error: `Concept #${i + 1} needs both "id" and "name".` };
    }
  }
  return { pack: parsed as VocabularyImportPack };
}

/** Per-action display meta — collision skips must read differently from
 * exact skips (distinct label + tone), per the C7 preview contract. */
const ACTION_META: Record<VocabularyImportPreviewAction, { label: string; color: string }> = {
  insert: { label: 'Insert', color: 'var(--green, #4ade80)' },
  skip_exact: { label: 'Skip — already present', color: 'var(--muted)' },
  skip_collision: { label: 'Skip — id collision', color: 'var(--amber, #fbbf24)' },
};

function PreviewCounters({ preview }: { preview: VocabularyImportPreview }) {
  return (
    <div style={{ display: 'flex', gap: '0.5rem', flexWrap: 'wrap' }}>
      <span className="vocab-preview-insert-count" style={counterStyle}>
        <strong style={{ color: 'var(--green, #4ade80)' }}>{preview.insert_count ?? 0}</strong> to insert
      </span>
      <span className="vocab-preview-skip-count" style={counterStyle}>
        <strong style={{ color: 'var(--muted)' }}>{preview.skip_count ?? 0}</strong> to skip
      </span>
      <span className="vocab-preview-collision-count" style={counterStyle}>
        <strong style={{ color: 'var(--amber, #fbbf24)' }}>{preview.collision_count ?? 0}</strong> id collisions
      </span>
    </div>
  );
}

function PreviewTable({ rows }: { rows: VocabularyImportPreviewRow[] }) {
  return (
    <table className="vocab-preview-table" style={{ width: '100%', borderCollapse: 'collapse' }}>
      <thead>
        <tr>
          <th style={thStyle}>Concept id</th>
          <th style={thStyle}>Name</th>
          <th style={thStyle}>Action</th>
          <th style={thStyle}>Collides with</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row, index) => {
          const meta = ACTION_META[row.action] ?? { label: String(row.action), color: 'var(--muted)' };
          return (
            <tr
              key={`${row.id}-${index}`}
              className={`vocab-preview-row vocab-preview-${row.action}`}
              data-action={row.action}
            >
              <td style={{ ...tdStyle, fontFamily: MONO }}>{row.id}</td>
              <td style={tdStyle}>{row.name}</td>
              <td style={{ ...tdStyle, color: meta.color, fontWeight: 600, whiteSpace: 'nowrap' }}>
                {meta.label}
              </td>
              <td style={{ ...tdStyle, fontFamily: MONO }}>{row.collides_with ?? '—'}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function PreviewResult({ preview }: { preview: VocabularyImportPreview }) {
  return (
    <div id="vocab-import-preview" style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem' }}>
      <div style={{ fontSize: '0.72rem', color: 'var(--muted)' }}>
        Preview — nothing was written. Import applies exactly this classification.
      </div>
      <PreviewCounters preview={preview} />
      <PreviewTable rows={preview.rows ?? []} />
    </div>
  );
}

function ApplyResult({ result }: { result: VocabularyImportResult }) {
  return (
    <div id="vocab-import-result" style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem' }}>
      <div style={{ display: 'flex', gap: '0.5rem', flexWrap: 'wrap' }}>
        <span className="vocab-import-inserted" style={counterStyle}>
          <strong style={{ color: 'var(--green, #4ade80)' }}>{result.inserted ?? 0}</strong> inserted
        </span>
        <span className="vocab-import-skipped" style={counterStyle}>
          <strong style={{ color: 'var(--muted)' }}>{result.skipped ?? 0}</strong> skipped
        </span>
        <span className="vocab-import-failed" style={counterStyle}>
          <strong style={{ color: 'var(--red, #f87171)' }}>{result.failed ?? 0}</strong> failed
        </span>
      </div>
      {(result.skipped_ids?.length ?? 0) > 0 && (
        <div
          className="vocab-import-skipped-ids"
          style={{ fontSize: '0.72rem', color: 'var(--muted)', fontFamily: MONO }}
        >
          Skipped (already present): {result.skipped_ids!.join(', ')}
        </div>
      )}
      {(result.collisions?.length ?? 0) > 0 && (
        <div
          className="vocab-import-collisions"
          style={{ fontSize: '0.72rem', color: 'var(--amber, #fbbf24)', fontFamily: MONO }}
        >
          Skipped (id collision):{' '}
          {result.collisions!.map((c) => `${c.id} → collides with ${c.existing_id}`).join('; ')}
        </div>
      )}
    </div>
  );
}

function PackInput({
  text,
  onText,
  onFile,
}: {
  text: string;
  onText: (value: string) => void;
  onFile: (file: File | undefined) => void;
}) {
  return (
    <>
      <div style={{ marginBottom: '0.75rem' }}>
        <input
          id="vocab-import-file"
          type="file"
          accept=".json,application/json"
          aria-label="Choose a pack JSON file"
          style={{ fontSize: '0.78rem', color: 'var(--text2)' }}
          onChange={(e) => onFile(e.target.files?.[0])}
        />
      </div>
      <div style={{ marginBottom: '0.75rem' }}>
        <textarea
          id="vocab-import-text"
          aria-label="Pack JSON"
          placeholder='{"metadata": {"name": "My pack"}, "concepts": [{"id": "example_concept", "name": "Example concept", "description": "…"}]}'
          style={textareaStyle}
          value={text}
          onChange={(e) => onText(e.target.value)}
        />
      </div>
    </>
  );
}

/** Panel state + actions: shared validate-then-dispatch for the dry-run
 * preview and the apply import (they intentionally take the same body). */
function useImportPanel() {
  const queryClient = useQueryClient();
  const importMutation = useMutation(importVocabularyMutationOptions(queryClient));
  const previewMutation = useMutation(previewVocabularyImportMutationOptions());
  const [text, setText] = useState('');
  const [error, setError] = useState('');
  const [result, setResult] = useState<VocabularyImportResult | null>(null);
  const [preview, setPreview] = useState<VocabularyImportPreview | null>(null);

  const updateText = (value: string) => {
    setText(value);
    setResult(null);
    setPreview(null);
  };

  const onFilePicked = (file: File | undefined) => {
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      updateText(typeof reader.result === 'string' ? reader.result : '');
      setError('');
    };
    reader.onerror = () => setError('Could not read the selected file.');
    reader.readAsText(file);
  };

  const run = async (mode: 'preview' | 'import') => {
    setError('');
    setResult(null);
    setPreview(null);
    const { pack, error: validationError } = validateImportPack(text);
    if (validationError || !pack) {
      setError(validationError || 'Invalid pack.');
      return;
    }
    try {
      if (mode === 'preview') setPreview(await previewMutation.mutateAsync(pack));
      else setResult(await importMutation.mutateAsync(pack));
    } catch (err) {
      setError(errorDetail(err));
    }
  };

  return { text, updateText, onFilePicked, run, error, result, preview,
    importing: importMutation.isPending, previewing: previewMutation.isPending };
}

export default function ImportPanel() {
  const p = useImportPanel();
  const busy = p.importing || p.previewing;
  return (
    <div className="vocab-import-panel">
      <h3 style={{ margin: '0 0 0.5rem', fontSize: '0.95rem', fontWeight: 600 }}>Bulk import</h3>
      <p style={sectionHelpStyle}>
        Import a flat concept pack — JSON with <code>metadata.name</code> and a{' '}
        <code>concepts</code> array of <code>{'{id, name, description}'}</code> rows. Preview shows
        what each row would do without writing anything. On import, concepts whose id already
        exists are skipped, ids that collide with an existing id after normalization are skipped
        and reported, and the rest are added with provenance <code>pack:custom:&lt;slug&gt;</code>.
      </p>

      <PackInput text={p.text} onText={p.updateText} onFile={p.onFilePicked} />

      <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem', marginBottom: '0.75rem' }}>
        <button
          id="vocab-import-preview-btn"
          type="button"
          style={previewBtnStyle}
          disabled={busy || !p.text.trim()}
          onClick={() => void p.run('preview')}
        >
          {p.previewing ? 'Previewing…' : 'Preview'}
        </button>
        <button
          id="vocab-import-btn"
          type="button"
          style={saveBtnStyle}
          disabled={busy || !p.text.trim()}
          onClick={() => void p.run('import')}
        >
          {p.importing ? 'Importing…' : 'Import'}
        </button>
        {p.error && (
          <span
            role="alert"
            className="vocab-import-error"
            style={{ fontSize: '0.75rem', color: 'var(--red, #f87171)' }}
          >
            {p.error}
          </span>
        )}
      </div>

      {p.preview && <PreviewResult preview={p.preview} />}
      {p.result && <ApplyResult result={p.result} />}
    </div>
  );
}
