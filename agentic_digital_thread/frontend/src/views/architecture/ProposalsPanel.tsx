// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Self-Learning panel components (jsx-migration W5.2) — render the proposals
 * store (./proposals.ts) into the former #self-learning-cards /
 * #learned-guidance-cards / #self-learning-count DOM (ids + classes are
 * byte-identical; loading/empty/error strings unchanged). The edit overlay
 * (former showEditDialog innerHTML) is <EditOverlay/> with controlled inputs
 * — same per-type field set, trim/include rules, saving state, and error
 * display as the old _submitEdit DOM-scrape. */
import { Fragment, useState } from 'react';

import type { BatchAdjudicationResult, ProposedMapping } from '../../api/types/proposals';
import { useStore } from '../../lib/store';
import {
  acceptMapping,
  closeEditDialog,
  clearSystemGuidance,
  editMapping,
  loadProposedMappings,
  pruneGuidanceNote,
  proposalsStore,
  rejectMapping,
  reopenMapping,
  requeueMapping,
  setProposalStatusFilter,
  setRuleLifecycle,
  sortMappingsByConsequence,
  submitEdit,
  type LoadPhase,
  type ProposalsState,
  type ProposalStatusFilter,
} from './proposals';
import {
  FamilyBatchHeader,
  familyGroups,
} from './proposal-families';
import {
  LgCard,
  mappingsForStatus,
  PROPOSAL_STATUS_FILTERS,
  sectionOf,
  SlCard,
  validMappings,
  type ProposalSection,
} from './SlCards';

/* ── Active-filter count badge (#self-learning-count) ───────────────────── */

/** Family batch settle handler: reload the queue ONLY when the server reports
 * a full success — zero `failed` and no per-id outcome with `ok === false`. On
 * any partial or total failure we skip the reload so the mounted
 * FamilyBatchHeader / FamilyOutcome keeps showing the honest per-id failure
 * lines for the steward to read. Success/eligibility are server-computed; this
 * never re-derives policy client-side. */
function reloadOnFullSuccess(result: BatchAdjudicationResult): void {
  const failedCount = Number(result.failed ?? 0);
  const anyRowFailed = (result.results ?? []).some((item) => item?.ok === false);
  if (failedCount === 0 && !anyRowFailed) loadProposedMappings(true);
}

function proposalCountsAvailable(phase: LoadPhase, mappings: ProposedMapping[]): boolean {
  // Numeric zero is meaningful only after a successful response. Preserve
  // non-empty prior data during a refresh/error, but never manufacture an
  // initial zero from the store's empty default.
  return phase === 'ready' || mappings.length > 0;
}

export function PendingBadge() {
  const state = useStore(proposalsStore);
  const valid = validMappings(state.mappings);
  const count = mappingsForStatus(valid, state.statusFilter).length;
  const countText = proposalCountsAvailable(state.phase, state.mappings) ? String(count) : '…';
  const label =
    PROPOSAL_STATUS_FILTERS.find((filter) => filter.value === state.statusFilter)?.label ??
    'Discoveries';
  return (
    <span id="self-learning-count" className="sl-count-badge">
      {countText} {label.toLowerCase()}
    </span>
  );
}

/* ── Proposal cards list (#self-learning-cards) ─────────────────────────── */

function confidenceValue(mapping: ProposedMapping): number | null {
  const raw = mapping.confidence;
  if (raw === undefined || raw === null || String(raw).trim() === '') return null;
  const confidence = Number(raw);
  return Number.isFinite(confidence) ? confidence : null;
}

/** Highest-confidence discoveries first; equal and missing confidences retain
 * API order, with missing/invalid confidence values placed at the end. */
export function sortMappingsByConfidence(mappings: ProposedMapping[]): ProposedMapping[] {
  return mappings
    .map((mapping, index) => ({ mapping, index, confidence: confidenceValue(mapping) }))
    .sort((a, b) => {
      if (a.confidence === null && b.confidence === null) return a.index - b.index;
      if (a.confidence === null) return 1;
      if (b.confidence === null) return -1;
      return b.confidence - a.confidence || a.index - b.index;
    })
    .map(({ mapping }) => mapping);
}

/** Chip count for one bucket. The store holds ONLY the selected bucket's rows
 * (status-scoped fetch), so the selected chip counts them — the same number
 * PendingBadge shows — and every other chip uses the server `status_counts`
 * (falling back to the local rows when the server predates the key). */
function chipCount(state: ProposalsState, valid: ProposedMapping[], status: ProposalStatusFilter) {
  const server = state.statusCounts?.[status];
  if (status !== state.statusFilter && typeof server === 'number') return server;
  return mappingsForStatus(valid, status).length;
}

function ProposalStatusFilters({ mappings }: { mappings: ProposedMapping[] }) {
  const state = useStore(proposalsStore);
  const countsAvailable = proposalCountsAvailable(state.phase, state.mappings);
  return (
    <div className="sl-status-filters" role="group" aria-label="Filter discoveries by status">
      {PROPOSAL_STATUS_FILTERS.map((filter) => {
        const count = chipCount(state, mappings, filter.value);
        const countText = countsAvailable ? String(count) : '…';
        const selected = state.statusFilter === filter.value;
        return (
          <button
            type="button"
            key={filter.value}
            className={`sl-status-filter${selected ? ' active' : ''}`}
            data-status-filter={filter.value}
            aria-pressed={selected}
            onClick={() => setProposalStatusFilter(filter.value)}
          >
            {filter.label} {countText}
          </button>
        );
      })}
    </div>
  );
}

/** Cards rendered per section before a "Show more" step. */
export const SECTION_RENDER_STEP = 100;

/** Leading family groups whose cumulative member count first reaches
 * ``limit`` — whole groups only, so a family's batch header always sits
 * above ALL of its rendered members. */
export function limitFamilyGroups<G extends { members: unknown[] }>(
  groups: G[],
  limit: number,
): G[] {
  const out: G[] = [];
  let shown = 0;
  for (const group of groups) {
    if (shown >= limit) break;
    out.push(group);
    shown += group.members.length;
  }
  return out;
}

function SectionCards(props: {
  section: ProposedMapping[];
  grouped: boolean;
  card: (m: ProposedMapping) => JSX.Element;
}) {
  const { section, grouped, card } = props;
  const [limit, setLimit] = useState(SECTION_RENDER_STEP);
  let rendered: JSX.Element[];
  let shownCount: number;
  if (grouped) {
    const groups = limitFamilyGroups(familyGroups(section), limit);
    shownCount = groups.reduce((n, g) => n + g.members.length, 0);
    rendered = groups.map((group) => (
      <Fragment key={group.key || group.members[0].mapping_id}>
        <FamilyBatchHeader group={group} onSettled={reloadOnFullSuccess} />
        {group.members.map(card)}
      </Fragment>
    ));
  } else {
    const rows = section.slice(0, limit);
    shownCount = rows.length;
    rendered = rows.map(card);
  }
  const remaining = section.length - shownCount;
  return (
    <>
      {rendered}
      {remaining > 0 && (
        <button
          type="button"
          className="sl-show-more"
          onClick={() => setLimit((n) => n + SECTION_RENDER_STEP)}
        >
          Show {Math.min(remaining, SECTION_RENDER_STEP)} more ({remaining} not shown)
        </button>
      )}
    </>
  );
}

/** "No discoveries yet" means NO row in ANY bucket. The store only holds the
 * selected bucket, so an empty bucket defers to the server `status_counts`
 * (an empty Accepted tab beside a full Pending queue reads "No accepted
 * discoveries.", as it did before the fetch was scoped). */
function noDiscoveriesAnywhere(state: ProposalsState): boolean {
  if (state.mappings.length > 0) return false;
  const counts = Object.values(state.statusCounts ?? {});
  return !counts.some((n) => typeof n === 'number' && n > 0);
}

export function ProposalsList() {
  const state = useStore(proposalsStore);
  const { phase, mappings, error, optimistic, ruleLifecycle, edit, statusFilter } = state;

  const valid = validMappings(mappings);
  let filters: JSX.Element | null =
    phase === 'loading' || phase === 'ready' ? <ProposalStatusFilters mappings={valid} /> : null;
  let body: JSX.Element | JSX.Element[] | null = null;
  if (phase === 'loading') {
    body = (
      <div className="sl-loading">
        <div className="sl-spinner" />
        <div>Loading discoveries…</div>
      </div>
    );
  } else if (phase === 'error') {
    body = <div className="sl-error">Failed to load discoveries: {error}</div>;
  } else if (phase === 'ready') {
    if (noDiscoveriesAnywhere(state)) {
      body = (
        <div className="sl-empty">
          No discoveries yet. As the AI answers cross-system questions, the relationships and
          domain context it works out will appear here for you to review.
        </div>
      );
    } else if (mappings.length > 0 && valid.length === 0) {
      body = (
        <div className="sl-empty">
          Cross-system queries detected, but no specific field discoveries yet. Try questions that
          join data across systems — e.g. lot traceability or supplier quality.
        </div>
      );
    } else {
      const visible = mappingsForStatus(valid, statusFilter);
      if (visible.length === 0) {
        const label =
          PROPOSAL_STATUS_FILTERS.find((filter) => filter.value === statusFilter)?.label ??
          'matching';
        body = <div className="sl-empty">No {label.toLowerCase()} discoveries.</div>;
      } else {
        // Pending is the actionable review queue — impact-first (consequence
        // desc, the task 2.2 evidence×blast score) with the confidence order
        // as the stable fallback/tiebreak, so pre-enrichment payloads keep
        // today's ordering. Other tabs stay confidence-ordered, unchanged.
        const byConfidence = sortMappingsByConfidence(visible);
        const ordered =
          statusFilter === 'pending' ? sortMappingsByConsequence(byConfidence) : byConfidence;
        // Class-sectioned review (plan Design decision 6): the ordered list is
        // partitioned by sectionOf into four fixed sections — mapping decisions
        // → field annotations → insights (collapsed) → schema drift — applied
        // WITHIN the selected status chip. Partitioning preserves the sort
        // order inside each section; empty sections render nothing. The
        // SlCard elements themselves are byte-identical to the flat list.
        const bySection: Record<ProposalSection, ProposedMapping[]> = {
          mapping: [],
          annotation: [],
          insight: [],
          drift: [],
        };
        for (const m of ordered) bySection[sectionOf(m)].push(m);
        const card = (m: ProposedMapping) => (
          <SlCard
            key={m.mapping_id}
            m={m}
            optimistic={optimistic[m.mapping_id]}
            onAccept={acceptMapping}
            onReject={rejectMapping}
            onEdit={editMapping}
            lifecycle={m.neptune_node_id ? ruleLifecycle[m.neptune_node_id] : undefined}
            onLifecycle={setRuleLifecycle}
            onRequeue={requeueMapping}
            onReopen={reopenMapping}
          />
        );
        // ADR-0063 D4: on the actionable PENDING queue, rows are shown by
        // server-computed family (proposal_scoring.family_key) with a group
        // action for families of 2+. The action posts the EXPLICIT ids of the
        // members rendered here — never the family key. Other status tabs and
        // single-row families render the flat list, byte-identical to before.
        // Bounded render (a live queue reached 1,216 pending rows / 6.6 MB and
        // took >30 s to paint): each section renders SECTION_RENDER_STEP cards
        // and a "Show more" control; family groups are never split.
        const cards = (section: ProposedMapping[]) => (
          <SectionCards
            key={statusFilter}
            section={section}
            grouped={statusFilter === 'pending'}
            card={card}
          />
        );
        body = (
          <>
            {bySection.mapping.length > 0 && (
              <section className="sl-section sl-section--mapping">
                <h5 className="sl-section-head">Mapping decisions</h5>
                {cards(bySection.mapping)}
              </section>
            )}
            {bySection.annotation.length > 0 && (
              <section className="sl-section sl-section--annotation">
                <h5 className="sl-section-head">Field annotations</h5>
                {cards(bySection.annotation)}
              </section>
            )}
            {bySection.insight.length > 0 && (
              // Native <details>, collapsed by default — informational
              // findings never become rules, so they stay out of the way.
              <details className="sl-insights-collapse">
                <summary>Insights — informational, no action needed</summary>
                {cards(bySection.insight)}
              </details>
            )}
            {bySection.drift.length > 0 && (
              <section className="sl-section sl-section--drift">
                <h5 className="sl-section-head">Schema drift</h5>
                <div className="sl-section-hint">
                  Resolves automatically when the system is re-onboarded.
                </div>
                {cards(bySection.drift)}
              </section>
            )}
          </>
        );
      }
    }
  }

  return (
    <>
      <div id="self-learning-cards" className="sl-cards">
        {filters}
        {body}
      </div>
      {edit && <EditOverlay key={edit.mapping_id} m={edit} />}
    </>
  );
}

/* ── Learned guidance list (#learned-guidance-cards) ────────────────────── */

export function LearnedGuidanceList() {
  const phase = useStore(proposalsStore, (s) => s.guidancePhase);
  const guidance = useStore(proposalsStore, (s) => s.guidance);
  const error = useStore(proposalsStore, (s) => s.guidanceError);

  let body: JSX.Element | JSX.Element[] | null = null;
  if (phase === 'loading') {
    body = (
      <div className="sl-loading">
        <div className="sl-spinner" />
        <div>Loading learned guidance…</div>
      </div>
    );
  } else if (phase === 'error') {
    body = <div className="sl-error">Failed to load learned guidance: {error}</div>;
  } else if (phase === 'ready') {
    if (guidance.length === 0) {
      body = (
        <div className="sl-empty">
          No learned guidance yet. As the AI answers questions, recurring lessons it works out are
          accumulated here as durable, per-system guidance.
        </div>
      );
    } else {
      body = guidance.map((sys, i) => (
        <LgCard
          key={sys.system_id ?? i}
          sys={sys}
          onPrune={pruneGuidanceNote}
          onClearLearned={clearSystemGuidance}
        />
      ));
    }
  }

  return (
    <>
      <div className="sl-toolbar" style={{ marginTop: '1.5rem' }}>
        <h4>Query Guidance</h4>
      </div>
      <p className="sl-intro">
        Auto-learned, per-system hints applied to future queries. These notes are distinct from
        reviewed proposals in the Accepted filter above; prune any that are stale or unhelpful.
      </p>
      <div id="learned-guidance-cards" className="sl-cards">
        {body}
      </div>
    </>
  );
}

/* ── Edit overlay (former showEditDialog / _submitEdit) ─────────────────── */

interface EditField {
  id: string;
  key: string;
  label: string;
  /** 'always' — include in updates even when blank (values/description);
   * 'nonempty' — include only when the trimmed value is non-empty
   * (systems/fields/concept), matching the old _submitEdit rules. */
  include: 'always' | 'nonempty';
  textarea?: boolean;
  /** Row-pair grouping for the relation layout (two fields per .sl-edit-row). */
  row?: number;
}

const ANNOTATION_FIELDS: EditField[] = [
  { id: 'edit-from-system', key: 'from_system', label: 'System', include: 'nonempty' },
  { id: 'edit-from-field', key: 'from_field', label: 'Field', include: 'nonempty' },
  { id: 'edit-description', key: 'description', label: 'Note / Description', include: 'always', textarea: true },
];

const RELATION_FIELDS: EditField[] = [
  { id: 'edit-from-system', key: 'from_system', label: 'From System', include: 'nonempty', row: 0 },
  { id: 'edit-from-field', key: 'from_field', label: 'From Field', include: 'nonempty', row: 0 },
  { id: 'edit-from-value', key: 'from_value', label: 'From Value', include: 'always', row: 1 },
  { id: 'edit-to-system', key: 'to_system', label: 'To System', include: 'nonempty', row: 2 },
  { id: 'edit-to-field', key: 'to_field', label: 'To Field', include: 'nonempty', row: 2 },
  { id: 'edit-to-value', key: 'to_value', label: 'To Value', include: 'always', row: 3 },
  { id: 'edit-concept', key: 'concept', label: 'Concept', include: 'nonempty' },
];

function initialValues(m: ProposedMapping, fields: EditField[]): Record<string, string> {
  const record = m as unknown as Record<string, unknown>;
  const init: Record<string, string> = {};
  for (const f of fields) {
    // The annotation note seeds from query_context, falling back to
    // description (same as the old showEditDialog textarea).
    if (f.key === 'description') init[f.key] = String(m.query_context || m.description || '');
    else init[f.key] = String(record[f.key] || '');
  }
  return init;
}

export function EditOverlay({ m }: { m: ProposedMapping }) {
  const pType = m.proposed_type || 'value_mapping';
  const fields = pType === 'field_annotation' ? ANNOTATION_FIELDS : RELATION_FIELDS;
  // Reviewed (react-props-in-state): deliberate seed-once editable draft —
  // the call site keys this overlay by m.mapping_id, so a different proposal
  // remounts and reseeds the form.
  // nosemgrep
  const [values, setValues] = useState<Record<string, string>>(() => initialValues(m, fields));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  const onSave = async () => {
    const updates: Record<string, string> = {};
    for (const f of fields) {
      const v = (values[f.key] ?? '').trim();
      if (f.include === 'always' || v) updates[f.key] = v;
    }
    setSaving(true);
    setError('');
    const err = await submitEdit(m.mapping_id, updates);
    // On success submitEdit closed the overlay (component unmounts).
    if (err !== null) {
      setSaving(false);
      setError(err);
    }
  };

  const renderField = (f: EditField) => (
    <div className="sl-edit-field" key={f.id}>
      <label htmlFor={f.id}>{f.label}</label>
      {f.textarea ? (
        <textarea
          id={f.id}
          value={values[f.key]}
          onChange={(e) => setValues((v) => ({ ...v, [f.key]: e.target.value }))}
        />
      ) : (
        <input
          type="text"
          id={f.id}
          value={values[f.key]}
          onChange={(e) => setValues((v) => ({ ...v, [f.key]: e.target.value }))}
        />
      )}
    </div>
  );

  // Relation layout groups row-paired fields into .sl-edit-row wrappers.
  let bodyFields: React.ReactNode;
  if (pType === 'field_annotation') {
    bodyFields = fields.map(renderField);
  } else {
    const rows = new Map<number, EditField[]>();
    const loose: EditField[] = [];
    for (const f of fields) {
      if (f.row !== undefined) {
        const bucket = rows.get(f.row) ?? [];
        bucket.push(f);
        rows.set(f.row, bucket);
      } else {
        loose.push(f);
      }
    }
    bodyFields = (
      <>
        {[...rows.entries()].map(([rowIdx, rowFields]) => (
          <div className="sl-edit-row" key={rowIdx}>
            {rowFields.map(renderField)}
          </div>
        ))}
        {loose.map(renderField)}
      </>
    );
  }

  return (
    <div className="sl-edit-overlay">
      <div className="sl-edit-modal">
        <div className="sl-edit-header">
          <h3>Edit Proposal</h3>
          <button className="sl-edit-close" onClick={closeEditDialog}>
            ×
          </button>
        </div>
        <div className="sl-edit-body">{bodyFields}</div>
        <div className="sl-edit-footer">
          <button className="sl-edit-cancel" onClick={closeEditDialog}>
            Cancel
          </button>
          <button className="sl-edit-save" disabled={saving} onClick={onSave}>
            {saving ? 'Saving...' : 'Save Changes'}
          </button>
        </div>
        <div className="sl-edit-error" style={{ display: error ? 'block' : 'none' }}>
          {error}
        </div>
      </div>
    </div>
  );
}
