// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Add/Edit concept modal for the Vocabulary admin tab.
 *
 * Add mode → POST /api/admin/vocabulary/concepts (human assertion through the
 * ADR-0003 lifecycle) — surfaces the returned evidence_score when present and
 * carries the informed-override checkbox. Edit mode → PATCH /concepts/{id}
 * with name/description/synonyms ONLY (the id is immutable and never enters
 * the body). Form styles mirror tabs/SettingsTab.tsx; the dialog shell is the
 * shared <ModalSurface/> (JSX only — no HTML-string sinks). */
import { useState, type CSSProperties } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';

import { ApiError } from '../../../../api/client';
import type {
  ConceptAssertionResult,
  VocabularyConcept,
} from '../../../../api/types/vocabulary';
import { ModalSurface } from '../../../../shared/ui/ModalSurface';
import { addConceptMutationOptions, editConceptMutationOptions } from '../../admin-queries';

export interface ConceptFormModalProps {
  mode: 'add' | 'edit';
  /** The concept being edited (edit mode only). */
  concept?: VocabularyConcept;
  onClose: () => void;
}

const overlayStyle: CSSProperties = {
  position: 'fixed',
  inset: 0,
  background: 'rgba(0,0,0,0.55)',
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  zIndex: 1000,
};
const dialogStyle: CSSProperties = {
  width: 'min(480px, calc(100vw - 2rem))',
  maxHeight: 'calc(100vh - 4rem)',
  overflowY: 'auto',
  background: 'var(--surface, var(--surface2))',
  border: '1px solid var(--border)',
  borderRadius: 8,
  padding: '1.25rem 1.5rem',
  boxSizing: 'border-box',
};
const inputStyle: CSSProperties = {
  width: '100%',
  padding: '0.5rem 0.75rem',
  background: 'var(--surface2)',
  border: '1px solid var(--border)',
  borderRadius: 4,
  color: 'var(--text)',
  fontSize: '0.85rem',
  boxSizing: 'border-box',
};
const fieldLabelStyle: CSSProperties = {
  display: 'block',
  fontSize: '0.75rem',
  color: 'var(--muted)',
  marginBottom: '0.35rem',
};
const fieldWrapStyle: CSSProperties = { marginBottom: '0.9rem' };
const helpTextStyle: CSSProperties = {
  margin: '0.35rem 0 0',
  fontSize: '0.72rem',
  color: 'var(--muted)',
  lineHeight: 1.4,
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
const cancelBtnStyle: CSSProperties = {
  background: 'var(--surface2)',
  color: 'var(--text)',
  border: '1px solid var(--border)',
  padding: '0.4rem 1rem',
  borderRadius: 4,
  fontSize: '0.8rem',
  cursor: 'pointer',
};

function errorDetail(error: unknown): string {
  return error instanceof ApiError ? error.detail : error instanceof Error ? error.message : String(error);
}

/** Comma-separated synonyms → trimmed, de-blanked list. */
export function parseSynonyms(raw: string): string[] {
  return raw
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean);
}

export default function ConceptFormModal({ mode, concept, onClose }: ConceptFormModalProps) {
  const queryClient = useQueryClient();
  const addMutation = useMutation(addConceptMutationOptions(queryClient));
  const editMutation = useMutation(editConceptMutationOptions(queryClient));

  const [id, setId] = useState('');
  // Reviewed (react-props-in-state): deliberate seed-once form state — the
  // call site keys this modal by mode+concept.id, so a change of edit target
  // remounts the form and reseeds these values.
  // nosemgrep
  const [name, setName] = useState(concept?.name ?? '');
  // nosemgrep
  const [description, setDescription] = useState(concept?.description ?? '');
  // nosemgrep
  const [synonyms, setSynonyms] = useState((concept?.synonyms ?? []).join(', '));
  const [evidenceSystem, setEvidenceSystem] = useState('');
  const [evidenceField, setEvidenceField] = useState('');
  const [overrideEvidence, setOverrideEvidence] = useState(false);
  const [error, setError] = useState('');
  const [result, setResult] = useState<ConceptAssertionResult | null>(null);

  const pending = addMutation.isPending || editMutation.isPending;

  const submit = async () => {
    setError('');
    const nameVal = name.trim();
    const descVal = description.trim();
    if (!nameVal) {
      setError('Name is required');
      return;
    }
    try {
      if (mode === 'add') {
        if (!descVal) {
          setError('Description is required');
          return;
        }
        const outcome = await addMutation.mutateAsync({
          ...(id.trim() ? { id: id.trim() } : {}),
          name: nameVal,
          description: descVal,
          synonyms: parseSynonyms(synonyms),
          ...(evidenceSystem.trim() ? { evidence_system: evidenceSystem.trim() } : {}),
          ...(evidenceField.trim() ? { evidence_field: evidenceField.trim() } : {}),
          override_evidence: overrideEvidence,
        });
        setResult(outcome); // keep open: show lifecycle status + evidence score
      } else {
        await editMutation.mutateAsync({
          conceptId: concept?.id ?? '',
          name: nameVal,
          description: descVal,
          synonyms: parseSynonyms(synonyms),
        });
        onClose();
      }
    } catch (err) {
      setError(errorDetail(err));
    }
  };

  const title = mode === 'add' ? 'Add concept' : `Edit concept — ${concept?.name ?? ''}`;

  return (
    <ModalSurface
      open
      onClose={onClose}
      label={title}
      closeOnOverlayClick
      className="vocab-modal-overlay"
      style={overlayStyle}
    >
      <div className="vocab-concept-modal" style={dialogStyle}>
        <h3 style={{ margin: '0 0 1rem', fontSize: '0.95rem', fontWeight: 600 }}>{title}</h3>

        {result ? (
          <div className="vocab-assert-result">
            <p style={{ fontSize: '0.85rem', color: 'var(--text)' }}>
              {result.status === 'accepted'
                ? '✓ Accepted — the concept is live in the vocabulary.'
                : 'Submitted — the assertion is pending steward review.'}
            </p>
            {result.evidence_score != null && (
              <p className="vocab-evidence-score" style={{ fontSize: '0.8rem', color: 'var(--text2)' }}>
                Evidence score: <strong>{result.evidence_score}</strong>
              </p>
            )}
            <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: '1rem' }}>
              <button type="button" style={saveBtnStyle} onClick={onClose}>
                Close
              </button>
            </div>
          </div>
        ) : (
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void submit();
            }}
          >
            {mode === 'add' ? (
              <div style={fieldWrapStyle}>
                <label style={fieldLabelStyle} htmlFor="vocab-form-id">
                  ID (optional)
                </label>
                <input
                  id="vocab-form-id"
                  type="text"
                  placeholder="derived from the name when left blank"
                  style={{ ...inputStyle, fontFamily: 'monospace' }}
                  value={id}
                  onChange={(e) => setId(e.target.value)}
                />
              </div>
            ) : (
              <div style={fieldWrapStyle}>
                <label style={fieldLabelStyle}>ID (immutable)</label>
                <div style={{ fontFamily: 'monospace', fontSize: '0.8rem', color: 'var(--muted)' }}>
                  {concept?.id}
                </div>
              </div>
            )}
            <div style={fieldWrapStyle}>
              <label style={fieldLabelStyle} htmlFor="vocab-form-name">
                Name
              </label>
              <input
                id="vocab-form-name"
                type="text"
                style={inputStyle}
                value={name}
                onChange={(e) => setName(e.target.value)}
              />
            </div>
            <div style={fieldWrapStyle}>
              <label style={fieldLabelStyle} htmlFor="vocab-form-description">
                Description
              </label>
              <textarea
                id="vocab-form-description"
                rows={3}
                style={{ ...inputStyle, resize: 'vertical' }}
                value={description}
                onChange={(e) => setDescription(e.target.value)}
              />
            </div>
            <div style={fieldWrapStyle}>
              <label style={fieldLabelStyle} htmlFor="vocab-form-synonyms">
                Synonyms (comma-separated)
              </label>
              <input
                id="vocab-form-synonyms"
                type="text"
                style={inputStyle}
                value={synonyms}
                onChange={(e) => setSynonyms(e.target.value)}
              />
            </div>

            {mode === 'add' && (
              <>
                <div style={fieldWrapStyle}>
                  <label style={fieldLabelStyle} htmlFor="vocab-form-evidence-system">
                    Evidence system (optional)
                  </label>
                  <input
                    id="vocab-form-evidence-system"
                    type="text"
                    placeholder="system id to score this assertion against"
                    style={inputStyle}
                    value={evidenceSystem}
                    onChange={(e) => setEvidenceSystem(e.target.value)}
                  />
                </div>
                <div style={fieldWrapStyle}>
                  <label style={fieldLabelStyle} htmlFor="vocab-form-evidence-field">
                    Evidence field (optional)
                  </label>
                  <input
                    id="vocab-form-evidence-field"
                    type="text"
                    placeholder="field name in that system"
                    style={inputStyle}
                    value={evidenceField}
                    onChange={(e) => setEvidenceField(e.target.value)}
                  />
                </div>
                <div style={fieldWrapStyle}>
                  <label
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      gap: '0.5rem',
                      fontSize: '0.8rem',
                      color: 'var(--text)',
                      cursor: 'pointer',
                    }}
                  >
                    <input
                      id="vocab-form-override"
                      type="checkbox"
                      style={{ width: 16, height: 16, accentColor: 'var(--accent)', cursor: 'pointer' }}
                      checked={overrideEvidence}
                      onChange={(e) => setOverrideEvidence(e.target.checked)}
                    />
                    Informed override — assert even if the evidence score is low
                  </label>
                  <p style={helpTextStyle}>
                    The override is recorded on the proposal so stewards can see the assertion was
                    made knowingly.
                  </p>
                </div>
              </>
            )}

            {error && (
              <div
                role="alert"
                className="vocab-form-error"
                style={{ fontSize: '0.75rem', color: 'var(--red, #f87171)', marginBottom: '0.75rem' }}
              >
                {error}
              </div>
            )}

            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '0.5rem' }}>
              <button type="button" style={cancelBtnStyle} onClick={onClose}>
                Cancel
              </button>
              <button type="submit" style={saveBtnStyle} disabled={pending}>
                {pending ? 'Saving…' : mode === 'add' ? 'Add concept' : 'Save changes'}
              </button>
            </div>
          </form>
        )}
      </div>
    </ModalSurface>
  );
}
