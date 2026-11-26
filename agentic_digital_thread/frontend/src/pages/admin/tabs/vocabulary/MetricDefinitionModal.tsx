// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Propose-a-metric-definition modal (ADR-0065) — a structured form over the
 * C1 document: id, label, synonyms, unit, then either a DIRECT
 * concept+aggregation or a DERIVED `{op, inputs[]}` formula.
 *
 * Two-stage feedback, deliberately: obvious shape mistakes are caught here
 * (required fields, the id charset, JSON that does not parse) so the steward
 * is not round-tripping for typos, but the SERVER is the validator — its 400
 * message names the offending field and is rendered VERBATIM, never
 * reworded, summarized, or replaced with a friendlier guess.
 *
 * POST /metrics/propose files the document PENDING. There is no accept path
 * here by design: the steward accepts on the existing proposals queue, where
 * the computed formula diff is shown. The dialog shell is the shared
 * <ModalSurface/>; JSX only — no HTML-string sinks. */
import { useState, type CSSProperties, type ReactNode } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';

import { ApiError } from '../../../../api/client';
import type {
  MetricDefinitionInput,
  MetricDefinitionKind,
  MetricDefinitionProposeResult,
} from '../../../../api/types/vocabulary';
import { ModalSurface } from '../../../../shared/ui/ModalSurface';
import { MetricDryRunAction } from '../../../../views/architecture/metric-dry-run';
import { proposeMetricDefinitionMutationOptions } from '../../metric-queries';

export interface MetricDefinitionModalProps {
  onClose: () => void;
}

/** metric_definition.ID_PATTERN — mirrored ONLY to catch a typo before the
 * round-trip; the server re-checks it and owns the verdict. */
const ID_PATTERN = /^[a-z0-9][a-z0-9-]{0,63}$/;

/** metric_layer.Aggregation — a closed vocabulary, so the form offers a
 * select rather than free text. */
const AGGREGATIONS = ['sum', 'avg', 'count', 'min', 'max', 'latest'] as const;

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
  width: 'min(520px, calc(100vw - 2rem))',
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

/** Comma-separated synonyms → trimmed, de-blanked list (parseSynonyms idiom). */
export function parseMetricSynonyms(raw: string): string[] {
  return raw
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean);
}

export interface MetricDraft {
  id: string;
  label: string;
  kind: MetricDefinitionKind;
  synonyms: string;
  unit: string;
  concept: string;
  aggregation: string;
  formula: string;
}

/** PURE client-side pre-check. Returns '' when the draft is worth POSTing.
 * It is deliberately shallow: cross-reference rules (does the concept exist?
 * does the formula cycle?) need the server's universe, and re-deriving them
 * here would give the steward a second, drifting opinion. */
export function preflightMetricDraft(draft: MetricDraft): string {
  const id = draft.id.trim();
  if (!id) return 'ID is required';
  if (!ID_PATTERN.test(id)) {
    return 'ID must be lowercase letters, digits or hyphens, starting with a letter or digit';
  }
  if (!draft.label.trim()) return 'Label is required';
  if (draft.kind === 'direct') {
    if (!draft.concept.trim()) return 'Concept is required for a direct metric';
    if (!draft.aggregation.trim()) return 'Aggregation is required for a direct metric';
    return '';
  }
  const raw = draft.formula.trim();
  if (!raw) return 'Formula is required for a derived metric';
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch (err) {
    return `Formula is not valid JSON: ${err instanceof Error ? err.message : String(err)}`;
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    return 'Formula must be a JSON object';
  }
  const node = parsed as Record<string, unknown>;
  if (typeof node.op !== 'string' || !node.op) return "Formula is missing an 'op'";
  if (!Array.isArray(node.inputs) || node.inputs.length === 0) {
    return "Formula is missing a non-empty 'inputs' array";
  }
  return '';
}

/** The draft → wire document. Optional fields are omitted rather than sent
 * empty: the server's closed-vocabulary checks reject an empty unit or a
 * direct field on a derived document. */
export function metricDraftToInput(draft: MetricDraft): MetricDefinitionInput {
  const synonyms = parseMetricSynonyms(draft.synonyms);
  const base: MetricDefinitionInput = {
    id: draft.id.trim(),
    kind: draft.kind,
    label: draft.label.trim(),
    ...(synonyms.length ? { synonyms } : {}),
    ...(draft.unit.trim() ? { unit: draft.unit.trim() } : {}),
  };
  if (draft.kind === 'direct') {
    return {
      ...base,
      concept: draft.concept.trim(),
      aggregation: draft.aggregation.trim(),
    };
  }
  return { ...base, formula: JSON.parse(draft.formula.trim()) as unknown };
}

const EMPTY_DRAFT: MetricDraft = {
  id: '',
  label: '',
  kind: 'direct',
  synonyms: '',
  unit: '',
  concept: '',
  aggregation: 'sum',
  formula: '',
};

type SetDraft = <K extends keyof MetricDraft>(key: K, value: MetricDraft[K]) => void;

/** One labelled text input with optional help text (byte-identical markup to
 * the former inline fields). */
function TextField(props: {
  id: string;
  label: string;
  value: string;
  onChange: (value: string) => void;
  mono?: boolean;
  help?: ReactNode;
}) {
  return (
    <div style={fieldWrapStyle}>
      <label style={fieldLabelStyle} htmlFor={props.id}>
        {props.label}
      </label>
      <input
        id={props.id}
        type="text"
        style={props.mono ? { ...inputStyle, fontFamily: 'monospace' } : inputStyle}
        value={props.value}
        onChange={(e) => props.onChange(e.target.value)}
      />
      {props.help ? <p style={helpTextStyle}>{props.help}</p> : null}
    </div>
  );
}

/** One labelled select over a closed option list. */
function SelectField(props: {
  id: string;
  label: string;
  value: string;
  onChange: (value: string) => void;
  options: ReadonlyArray<{ value: string; label: string }>;
}) {
  return (
    <div style={fieldWrapStyle}>
      <label style={fieldLabelStyle} htmlFor={props.id}>
        {props.label}
      </label>
      <select
        id={props.id}
        style={inputStyle}
        value={props.value}
        onChange={(e) => props.onChange(e.target.value)}
      >
        {props.options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
    </div>
  );
}

const KIND_OPTIONS = [
  { value: 'direct', label: 'direct — one concept with an aggregation' },
  { value: 'derived', label: 'derived — a formula over other metrics' },
] as const;
const AGGREGATION_OPTIONS = AGGREGATIONS.map((aggregation) => ({
  value: aggregation,
  label: aggregation,
}));

/** The derived-metric formula textarea. */
function FormulaField({ value, onChange }: { value: string; onChange: (value: string) => void }) {
  return (
    <div style={fieldWrapStyle}>
      <label style={fieldLabelStyle} htmlFor="vocab-metric-formula">
        Formula (JSON)
      </label>
      <textarea
        id="vocab-metric-formula"
        rows={6}
        style={{ ...inputStyle, resize: 'vertical', fontFamily: 'monospace' }}
        placeholder={'{"op": "ratio", "inputs": [{"metric": "…"}, {"metric": "…"}]}'}
        value={value}
        onChange={(e) => onChange(e.target.value)}
      />
      <p style={helpTextStyle}>
        An <code>op</code> with an <code>inputs</code> array; leaves are{' '}
        <code>{'{"metric": id}'}</code>, <code>{'{"concept": id}'}</code> or{' '}
        <code>{'{"value": number}'}</code>.
      </p>
    </div>
  );
}

/** The kind-specific section: concept + aggregation, or a formula. */
function KindFields({ draft, set }: { draft: MetricDraft; set: SetDraft }) {
  if (draft.kind !== 'direct') {
    return <FormulaField value={draft.formula} onChange={(v) => set('formula', v)} />;
  }
  return (
    <>
      <TextField
        id="vocab-metric-concept"
        label="Concept"
        mono
        value={draft.concept}
        onChange={(v) => set('concept', v)}
        help="Must already exist in the vocabulary — the server refuses an unknown concept."
      />
      <SelectField
        id="vocab-metric-aggregation"
        label="Aggregation"
        value={draft.aggregation}
        onChange={(v) => set('aggregation', v)}
        options={AGGREGATION_OPTIONS}
      />
    </>
  );
}

/** The fields shared by both kinds: id, label, synonyms, unit, kind. */
function CommonFields({ draft, set }: { draft: MetricDraft; set: SetDraft }) {
  return (
    <>
      <TextField
        id="vocab-metric-id"
        label="ID"
        mono
        value={draft.id}
        onChange={(v) => set('id', v)}
        help="Lowercase letters, digits and hyphens. An id already used by a code-defined metric is refused — code always wins."
      />
      <TextField id="vocab-metric-label" label="Label" value={draft.label} onChange={(v) => set('label', v)} />
      <TextField
        id="vocab-metric-synonyms"
        label="Synonyms (comma-separated)"
        value={draft.synonyms}
        onChange={(v) => set('synonyms', v)}
      />
      <TextField id="vocab-metric-unit" label="Unit (optional)" value={draft.unit} onChange={(v) => set('unit', v)} />
      <SelectField
        id="vocab-metric-kind"
        label="Kind"
        value={draft.kind}
        onChange={(v) => set('kind', v as MetricDefinitionKind)}
        options={KIND_OPTIONS}
      />
    </>
  );
}

interface MetricDraftFormProps {
  draft: MetricDraft;
  set: SetDraft;
  error: string;
  pending: boolean;
  onSubmit: () => void;
  onClose: () => void;
}

/** The propose form: fields, the server's verbatim error, and the actions. */
function MetricDraftForm({ draft, set, error, pending, onSubmit, onClose }: MetricDraftFormProps) {
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        onSubmit();
      }}
    >
      <CommonFields draft={draft} set={set} />
      <KindFields draft={draft} set={set} />
      {error && (
        <div
          role="alert"
          className="vocab-metric-error"
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
          {pending ? 'Filing…' : 'File for review'}
        </button>
      </div>
    </form>
  );
}

/** The post-submit pane: the filed proposal id plus the ADR-0080 dry-run
 * preview of the still-PENDING definition. No accept path here by design. */
function MetricProposeResultPane({
  result,
  onClose,
}: {
  result: MetricDefinitionProposeResult;
  onClose: () => void;
}) {
  return (
    <div className="vocab-metric-result">
      <p style={{ fontSize: '0.85rem', color: 'var(--text)' }}>
        Filed for review — the definition is pending and resolves nothing yet. Accept or reject
        it on the discoveries review queue, where the formula diff is shown.
      </p>
      {result.mapping_id && (
        <p className="vocab-metric-mapping-id" style={{ fontSize: '0.8rem', color: 'var(--text2)' }}>
          Proposal: <strong>{result.mapping_id}</strong>
        </p>
      )}
      {result.mapping_id && <MetricDryRunAction mappingId={result.mapping_id} />}
      <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: '1rem' }}>
        <button type="button" style={saveBtnStyle} onClick={onClose}>
          Close
        </button>
      </div>
    </div>
  );
}

/** Draft/error/result state + the propose mutation, kept out of the shell. */
function useMetricProposeForm() {
  const queryClient = useQueryClient();
  const proposeMutation = useMutation(proposeMetricDefinitionMutationOptions(queryClient));
  const [draft, setDraft] = useState<MetricDraft>(EMPTY_DRAFT);
  const [error, setError] = useState('');
  const [result, setResult] = useState<MetricDefinitionProposeResult | null>(null);
  const set: SetDraft = (key, value) => setDraft((current) => ({ ...current, [key]: value }));
  const submit = async () => {
    setError('');
    const preflight = preflightMetricDraft(draft);
    if (preflight) {
      setError(preflight);
      return;
    }
    try {
      setResult(await proposeMutation.mutateAsync(metricDraftToInput(draft)));
    } catch (err) {
      // The server names the offending field — render it VERBATIM.
      setError(errorDetail(err));
    }
  };
  return { draft, set, error, result, submit, pending: proposeMutation.isPending };
}

export default function MetricDefinitionModal({ onClose }: MetricDefinitionModalProps) {
  const form = useMetricProposeForm();
  const title = 'Propose metric definition';
  return (
    <ModalSurface
      open
      onClose={onClose}
      label={title}
      closeOnOverlayClick
      className="vocab-modal-overlay"
      style={overlayStyle}
    >
      <div className="vocab-metric-modal" style={dialogStyle}>
        <h3 style={{ margin: '0 0 1rem', fontSize: '0.95rem', fontWeight: 600 }}>{title}</h3>
        {form.result ? (
          <MetricProposeResultPane result={form.result} onClose={onClose} />
        ) : (
          <MetricDraftForm
            draft={form.draft}
            set={form.set}
            error={form.error}
            pending={form.pending}
            onSubmit={() => void form.submit()}
            onClose={onClose}
          />
        )}
      </div>
    </ModalSurface>
  );
}
