// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useState, type CSSProperties } from 'react';

import { requestJson } from '../../api/client';
import type {
  BatchAdjudicationResult,
  ProposedMapping,
} from '../../api/types/proposals';
import type {
  MetricDefinitionDiff,
  MetricDefinitionPreview,
} from '../../api/types/vocabulary';

export interface FamilyGroup {
  key: string;
  members: ProposedMapping[];
}

type FamilyAction = 'accept' | 'reject';

export function familyGroups(mappings: ProposedMapping[]): FamilyGroup[] {
  const groups: FamilyGroup[] = [];
  const byKey = new Map<string, FamilyGroup>();
  for (const mapping of mappings) {
    const key = String(mapping.family_key || '');
    const existing = key ? byKey.get(key) : undefined;
    if (existing) {
      existing.members.push(mapping);
      continue;
    }
    const group = { key, members: [mapping] };
    groups.push(group);
    if (key) byKey.set(key, group);
  }
  return groups;
}

const TYPE_LABELS: Record<string, string> = {
  join_path: 'Join Path',
  crosswalk: 'Crosswalk',
  value_mapping: 'Value Mapping',
  identity_rule: 'Identity Rule',
  field_annotation: 'Field Note',
  insight: 'Finding',
  schema_drift: 'Schema Drift',
  coverage_gap: 'Coverage Gap',
  metric_definition: 'Metric Definition',
};

export function familySummary(members: ProposedMapping[]): string {
  const head = members[0];
  if (!head) return '';
  const label = TYPE_LABELS[head.proposed_type || 'value_mapping'] ?? 'Proposal';
  // A metric definition has no from→to field pair — the metric id (to_value)
  // is what identifies it.
  const pair =
    head.proposed_type === 'metric_definition'
      ? String(head.to_value || '')
      : [head.from_field, head.to_field].filter(Boolean).join(' → ');
  const method = head.method ? ` · ${head.method}` : '';
  return `${label}${pair ? ` · ${pair}` : ''}${method}`;
}

export function eligibleFamilyIds(group: FamilyGroup, action: FamilyAction): string[] {
  return group.members
    .filter((mapping) => mapping.action_eligibility?.[action]?.eligible === true)
    .map((mapping) => mapping.mapping_id)
    .filter(Boolean);
}

async function postFamilyBatch(
  action: FamilyAction,
  mappingIds: string[],
): Promise<BatchAdjudicationResult> {
  return requestJson<BatchAdjudicationResult>('/api/proposed-mappings/adjudicate-batch', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action, mapping_ids: mappingIds }),
  });
}

const FAILURE_COPY: Readonly<Record<string, string>> = {
  not_found: 'Proposal is no longer available.',
  not_pending: 'Proposal is no longer pending.',
  specialized_accept_required: 'Use the dedicated review flow for this proposal.',
  non_actionable: 'This proposal is not actionable.',
  eligibility_unavailable: 'Action eligibility is unavailable.',
  backend_unavailable: 'Proposal service is unavailable.',
  action_rejected: 'The proposal action was not accepted.',
  action_failed: 'The proposal action could not be completed.',
};

export function familyFailureCopy(code: string | undefined): string {
  return FAILURE_COPY[code || ''] ?? 'The proposal action could not be completed.';
}

function FamilyOutcome({ result }: { result: BatchAdjudicationResult }) {
  const failures = (result.results || []).filter((item) => item?.ok === false);
  const succeeded = Number(result.succeeded ?? 0);
  const requested = Number(result.requested ?? 0);
  return (
    <div className="sl-family-outcome" data-testid="sl-family-outcome" style={{ flexBasis: '100%', fontSize: 11 }}>
      <span className="sl-family-tally">
        {succeeded} of {requested} {result.action === 'accept' ? 'accepted' : 'rejected'}
        {failures.length > 0 ? `, ${failures.length} failed` : ''}
      </span>
      {failures.map((failure) => (
        <div key={failure.mapping_id} className="sl-family-failure">
          {failure.mapping_id}: {familyFailureCopy(failure.error_code)}
        </div>
      ))}
    </div>
  );
}

interface FamilyActionsProps {
  ids: Record<FamilyAction, string[]>;
  busy: FamilyAction | null;
  run: (action: FamilyAction) => void;
}

function FamilyActions({ ids, busy, run }: FamilyActionsProps) {
  return (
    <>
      {(['accept', 'reject'] as const).map((action) => (
        <button
          key={action}
          className={`sl-${action} sl-family-${action}`}
          disabled={busy !== null || ids[action].length === 0}
          title={`${action === 'accept' ? 'Accept' : 'Reject'} eligible rows through their single-row checks`}
          onClick={() => run(action)}
        >
          {busy === action
            ? `${action === 'accept' ? 'Accepting' : 'Rejecting'}…`
            : `${action === 'accept' ? 'Accept' : 'Reject'} ${ids[action].length} eligible in this family`}
        </button>
      ))}
    </>
  );
}

export interface FamilyBatchHeaderProps {
  group: FamilyGroup;
  onBatch?: (action: FamilyAction, mappingIds: string[]) => Promise<BatchAdjudicationResult>;
  onSettled?: (result: BatchAdjudicationResult) => void;
}

function useFamilyBatch({ group, onBatch, onSettled }: FamilyBatchHeaderProps) {
  const [busy, setBusy] = useState<FamilyAction | null>(null);
  const [result, setResult] = useState<BatchAdjudicationResult | null>(null);
  const [failed, setFailed] = useState(false);
  const ids = {
    accept: eligibleFamilyIds(group, 'accept'),
    reject: eligibleFamilyIds(group, 'reject'),
  };
  const run = async (action: FamilyAction) => {
    setBusy(action);
    setFailed(false);
    try {
      const response = await (onBatch ?? postFamilyBatch)(action, ids[action]);
      setResult(response);
      onSettled?.(response);
    } catch {
      setFailed(true);
      setResult(null);
    } finally {
      setBusy(null);
    }
  };
  return { busy, failed, ids, result, run };
}

const HEAD_STYLE: CSSProperties = {
  display: 'flex',
  flexWrap: 'wrap',
  alignItems: 'center',
  gap: 8,
  margin: '0.5rem 0 0.25rem',
  fontSize: 12,
  color: 'var(--text2)',
};

export function FamilyBatchHeader(props: FamilyBatchHeaderProps) {
  const { group } = props;
  const batch = useFamilyBatch(props);
  if (!group.key || group.members.length < 2) return null;
  return (
    <div className="sl-family-head" data-family-key={group.key} data-testid="sl-family-head" style={HEAD_STYLE}>
      <span className="sl-family-label">
        {group.members.length} in this family
        <span className="sl-family-grain"> — {familySummary(group.members)}</span>
      </span>
      <span className="sl-head-spacer" />
      <FamilyActions ids={batch.ids} busy={batch.busy} run={batch.run} />
      {batch.failed ? (
        <div className="sl-family-failure" data-testid="sl-family-error">
          Family action unavailable. Try again.
        </div>
      ) : null}
      {batch.result ? <FamilyOutcome result={batch.result} /> : null}
    </div>
  );
}

/* ── metric_definition review diff (ADR-0065) ───────────────────────────────
 * The steward reviews a formula BEFORE it can define what a number means, so
 * a `metric_definition` row carries a read-only diff expander fed by
 * GET /api/admin/vocabulary/metrics/{mapping_id}/preview. Every judgement in
 * it — the rendered formula, the changed-field list, the validation verdict —
 * is COMPUTED server-side and rendered here verbatim as React text nodes;
 * there is no HTML-string sink and no client-side re-derivation. Accepting
 * still happens through this card's EXISTING accept action (one door). */

/** Stable, human-readable text for one diff value. Objects (a formula tree)
 * render as canonical JSON TEXT inside a <code> element — never as markup. */
export function metricValueText(value: unknown): string {
  if (value === null || value === undefined) return '—';
  if (typeof value === 'string') return value || '—';
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

const DIFF_BODY_STYLE: CSSProperties = { fontSize: 11, lineHeight: 1.5, marginTop: 6 };
const DIFF_LIST_STYLE: CSSProperties = { margin: '4px 0 0', paddingLeft: 16 };

function FormulaLines({ diff }: { diff: MetricDefinitionDiff }) {
  return (
    <div className="sl-metric-formula">
      <div className="sl-metric-formula-from">
        <span className="lbl">Current formula:</span>{' '}
        {diff.is_new || diff.formula?.from == null ? (
          <em>none — this proposal adds a new metric</em>
        ) : (
          <code>{diff.formula.from}</code>
        )}
      </div>
      <div className="sl-metric-formula-to">
        <span className="lbl">Proposed formula:</span> <code>{diff.formula?.to ?? ''}</code>
      </div>
    </div>
  );
}

function ChangedFields({ diff }: { diff: MetricDefinitionDiff }) {
  const changed = Object.entries(diff.changed || {});
  const added = Object.entries(diff.added || {});
  const removed = Object.entries(diff.removed || {});
  if (changed.length === 0 && added.length === 0 && removed.length === 0) {
    return (
      <div className="sl-metric-fields-empty">
        No field-level change — the proposed document matches the current one.
      </div>
    );
  }
  return (
    <ul className="sl-metric-fields" style={DIFF_LIST_STYLE}>
      {changed.map(([field, change]) => (
        <li key={`changed-${field}`} className="sl-metric-field-changed">
          <span className="lbl">{field}</span>: <code>{metricValueText(change?.from)}</code>
          {' → '}
          <code>{metricValueText(change?.to)}</code>
        </li>
      ))}
      {added.map(([field, value]) => (
        <li key={`added-${field}`} className="sl-metric-field-added">
          <span className="lbl">{field}</span> added: <code>{metricValueText(value)}</code>
        </li>
      ))}
      {removed.map(([field, value]) => (
        <li key={`removed-${field}`} className="sl-metric-field-removed">
          <span className="lbl">{field}</span> removed: <code>{metricValueText(value)}</code>
        </li>
      ))}
    </ul>
  );
}

export function MetricDefinitionDiffBody({ preview }: { preview: MetricDefinitionPreview }) {
  const diff = preview.diff;
  const validation = preview.validation;
  return (
    <div className="sl-metric-diff-body" style={DIFF_BODY_STYLE}>
      {preview.collision ? (
        <div className="sl-metric-collision" role="alert">
          This id already belongs to a code-defined metric — the code definition wins, so an
          accepted graph row with this id stays shadowed.
        </div>
      ) : null}
      {diff ? (
        <>
          <FormulaLines diff={diff} />
          <ChangedFields diff={diff} />
        </>
      ) : (
        <div className="sl-metric-unparseable" role="alert">
          The stored proposal no longer parses as a metric definition, so no diff can be computed.
        </div>
      )}
      {validation ? (
        <div
          className="sl-metric-validation"
          role={validation.valid ? 'status' : 'alert'}
          style={{ marginTop: 4 }}
        >
          {validation.valid
            ? 'Validates against the current vocabulary.'
            : `Does not validate: ${validation.error}`}
        </div>
      ) : null}
    </div>
  );
}

async function fetchMetricPreview(mappingId: string): Promise<MetricDefinitionPreview> {
  return requestJson<MetricDefinitionPreview>(
    `/api/admin/vocabulary/metrics/${encodeURIComponent(mappingId)}/preview`,
  );
}

export interface MetricDiffExpanderProps {
  mappingId: string;
  /** Injected in tests; production uses the preview route. */
  onFetch?: (mappingId: string) => Promise<MetricDefinitionPreview>;
}

/** Keyboard-reachable disclosure (button + aria-expanded/aria-controls) that
 * lazily reads the preview the first time it is opened. */
export function MetricDiffExpander({ mappingId, onFetch }: MetricDiffExpanderProps) {
  const [open, setOpen] = useState(false);
  const [preview, setPreview] = useState<MetricDefinitionPreview | null>(null);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  const regionId = `sl-metric-diff-${mappingId}`;

  const toggle = async () => {
    const next = !open;
    setOpen(next);
    if (!next || preview || busy) return;
    setBusy(true);
    setFailed(false);
    try {
      setPreview(await (onFetch ?? fetchMetricPreview)(mappingId));
    } catch {
      setFailed(true);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="sl-metric-diff" data-testid="sl-metric-diff">
      <button
        type="button"
        className="sl-metric-diff-toggle"
        aria-expanded={open}
        aria-controls={regionId}
        onClick={() => void toggle()}
      >
        {open ? 'Hide formula diff' : 'Show formula diff'}
      </button>
      <div id={regionId} hidden={!open}>
        {open ? (
          busy ? (
            <div className="sl-metric-diff-busy">Reading formula diff…</div>
          ) : failed ? (
            <div className="sl-metric-diff-error" role="alert">
              Formula diff unavailable. Nothing was accepted.
            </div>
          ) : preview ? (
            <MetricDefinitionDiffBody preview={preview} />
          ) : null
        ) : null}
      </div>
    </div>
  );
}
