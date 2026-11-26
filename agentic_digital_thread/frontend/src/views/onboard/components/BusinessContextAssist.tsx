// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* <BusinessContextAssist/> — OPT-IN AI assist for the business-context
 * editor (collapsed to one small button by default; the manual textarea flow
 * is untouched when collapsed). POST /api/onboard/guided/refine is STATELESS
 * and read-only: it returns a candidate replacement draft + notes and writes
 * NOTHING — saving stays the card's existing PUT with its secret-shape gate
 * and roll warning. The draft is LLM output over untrusted steward input and
 * is itself untrusted: it renders ONLY as controlled plain-text values (a
 * read-only textarea and text nodes) — never markdown, never an HTML sink. */
import { useState } from 'react';

import { ApiError, postJson } from '../../../api/client';

export const REFINE_URL = '/api/onboard/guided/refine';
export const REFINE_INSTRUCTION_MAX_LEN = 500;

const MUTED = 'var(--muted)';
const RED = 'var(--red, #f87171)';

type RefineResponse = { draft: string; notes: string };

/** Quick asks derived from the eight intake topics the card's hint list
 * shows. Chips only PREFILL the instruction — the steward can edit it. */
export const ASSIST_CHIPS: ReadonlyArray<{ label: string; instruction: string }> = [
  {
    label: 'Re-check all eight topics',
    instruction:
      'Review the context against all eight intake topics and note which ' +
      'remain unaddressed.',
  },
  {
    label: 'Tighten wording',
    instruction: 'Tighten the wording without losing any stated facts.',
  },
  {
    label: 'Add KPI definitions',
    instruction:
      'Make each KPI definition explicit — numerator, exact denominator ' +
      'basis, and which records are excluded.',
  },
  {
    label: 'Clarify failure codes',
    instruction:
      'Make the failure-code guidance clearer — which codes are real ' +
      'problems versus routine workflow outcomes.',
  },
];

const btnStyle = { fontSize: '0.75rem' } as const;

type AssistProps = {
  systemId: string;
  /** The textarea's CURRENT text — each ask carries it, so successive asks
   * naturally refine whatever the steward has (stateless back-and-forth). */
  currentText: string;
  /** Replace the textarea value with the accepted draft (steward can then
   * hand-edit; save stays the existing Save with its roll warning). */
  onUseDraft: (draft: string) => void;
};

export function BusinessContextAssist({ systemId, currentText, onUseDraft }: AssistProps) {
  const [open, setOpen] = useState(false);
  const [instruction, setInstruction] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [preview, setPreview] = useState<RefineResponse | null>(null);

  const suggest = async () => {
    setLoading(true);
    setError(null);
    setPreview(null);
    try {
      const d = await postJson<RefineResponse>(REFINE_URL, {
        system_id: systemId,
        instruction,
        current_text: currentText,
      });
      setPreview({
        draft: typeof d.draft === 'string' ? d.draft : '',
        notes: typeof d.notes === 'string' ? d.notes : '',
      });
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : (e as Error).message);
    } finally {
      setLoading(false);
    }
  };

  if (!open) {
    return (
      <div style={{ marginTop: '0.35rem' }}>
        <button className="onboard-btn" style={btnStyle} onClick={() => setOpen(true)}>
          Improve with AI
        </button>
      </div>
    );
  }
  return (
    <div
      data-testid="bc-assist"
      style={{
        marginTop: '0.4rem',
        padding: '0.5rem',
        border: '1px solid var(--border)',
        borderRadius: 4,
        fontSize: '0.75rem',
      }}
    >
      <AssistControls
        instruction={instruction}
        setInstruction={setInstruction}
        loading={loading}
        onSuggest={() => void suggest()}
        onClose={() => setOpen(false)}
      />
      {loading && (
        <div role="status" style={{ marginTop: '0.35rem', color: MUTED }}>
          Asking the assistant…
        </div>
      )}
      {error && (
        <div
          role="status"
          data-testid="bc-assist-error"
          style={{ marginTop: '0.35rem', color: RED }}
        >
          No suggestion — {error}
        </div>
      )}
      {preview && (
        <AssistPreview
          preview={preview}
          onUse={() => {
            onUseDraft(preview.draft);
            setPreview(null);
          }}
          onDiscard={() => setPreview(null)}
        />
      )}
    </div>
  );
}

type ControlsProps = {
  instruction: string;
  setInstruction: (v: string) => void;
  loading: boolean;
  onSuggest: () => void;
  onClose: () => void;
};

function AssistControls({ instruction, setInstruction, loading, onSuggest, onClose }: ControlsProps) {
  return (
    <>
      <div style={{ color: MUTED, marginBottom: '0.35rem' }}>
        Describe the change you want; the assistant drafts a revision against
        the eight intake topics. Nothing is saved until you press Save.
      </div>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: '0.3rem', marginBottom: '0.35rem' }}>
        {ASSIST_CHIPS.map((chip) => (
          <button
            key={chip.label}
            className="onboard-btn"
            style={btnStyle}
            onClick={() => setInstruction(chip.instruction)}
          >
            {chip.label}
          </button>
        ))}
      </div>
      <div style={{ display: 'flex', gap: '0.5rem', alignItems: 'center' }}>
        <input
          aria-label="Revision instruction"
          type="text"
          value={instruction}
          maxLength={REFINE_INSTRUCTION_MAX_LEN}
          onChange={(e) => setInstruction(e.target.value.slice(0, REFINE_INSTRUCTION_MAX_LEN))}
          placeholder="e.g. make the failure-code guidance clearer"
          style={{
            flex: 1,
            background: 'var(--surface)',
            color: 'var(--text)',
            border: '1px solid var(--border)',
            borderRadius: 4,
            padding: '0.3rem 0.4rem',
            fontSize: '0.75rem',
            fontFamily: 'inherit',
          }}
        />
        <button
          className="onboard-btn"
          style={btnStyle}
          disabled={loading || !instruction.trim()}
          onClick={onSuggest}
        >
          Suggest revision
        </button>
        <button className="onboard-btn" style={btnStyle} onClick={onClose}>
          Close
        </button>
      </div>
    </>
  );
}

type PreviewProps = {
  preview: RefineResponse;
  onUse: () => void;
  onDiscard: () => void;
};

/** Read-only plain-text preview: controlled values only — the draft must
 * never reach a markdown renderer or HTML sink. */
function AssistPreview({ preview, onUse, onDiscard }: PreviewProps) {
  return (
    <div data-testid="bc-assist-preview" style={{ marginTop: '0.4rem' }}>
      <textarea
        aria-label="Suggested revision"
        value={preview.draft}
        readOnly
        rows={5}
        style={{
          width: '100%',
          boxSizing: 'border-box',
          background: 'var(--surface)',
          color: 'var(--text)',
          border: '1px dashed var(--border)',
          borderRadius: 4,
          padding: '0.4rem',
          fontSize: '0.8rem',
          fontFamily: 'inherit',
          resize: 'vertical',
        }}
      />
      {preview.notes && (
        <div data-testid="bc-assist-notes" style={{ color: MUTED, marginTop: '0.25rem' }}>
          {preview.notes}
        </div>
      )}
      <div style={{ display: 'flex', gap: '0.5rem', marginTop: '0.35rem' }}>
        <button className="onboard-btn" style={btnStyle} onClick={onUse}>
          Use this draft
        </button>
        <button className="onboard-btn" style={btnStyle} onClick={onDiscard}>
          Discard
        </button>
      </div>
    </div>
  );
}
