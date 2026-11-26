// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Self-learning proposal-card + learned-guidance-card components
 * (jsx-migration W5.2) — the JSX form of the former pure string builders in
 * ./sl-cards.ts (originally verbatim ports:
 * _slChip/_slCardBody/_slCard/_lgCard). Every class is byte-identical to the
 * old markup (dashboard.css sl-* contract: sl-card[data-mapping-id],
 * sl-card-head, sl-type, sl-conf, sl-status, sl-usage, sl-card-actions,
 * sl-accept/sl-reject/sl-edit, sl-applied(--rejected), sl-relation, sl-chip,
 * sl-arrow, sl-anno-*, sl-impact, sl-block(-head/-proof), sl-verified,
 * sl-overlap-*, sl-ai, sl-why(-body), lg-note/lg-prune/lg-meter).
 *
 * The load-bearing rendering rules are unchanged: schema-verdict gating (a
 * definitive 'not_found' disables Accept and renders the ⛔ proof block;
 * 'verified' renders the ✅ line; indeterminate falls back to the free-text
 * ai_recommendation), pending-only actions, insight (Finding) cards with no
 * From→To chips + retitled actions + hidden Edit, and the accepted-card usage
 * badge. Interactivity moved from inline onclick="acceptMapping(...)" strings
 * to React onClick props; JSX auto-escaping replaces the slEsc interpolation. */
import { useState } from 'react';

import type {
  ProposedMapping,
  QueryGuidanceSystem,
} from '../../api/types/proposals';
import { systemName, useSystemNamesVersion } from '../../lib/system-names';
import type { ProposalStatusFilter } from './proposals';
import { MetricDefinitionCardBody } from './metric-definition-body';
import { TryItPanel, useStewardVisible } from './TryItPanel';
import { TrustChip } from './proposal-trust';

export const PROPOSAL_STATUS_FILTERS: ReadonlyArray<{
  value: ProposalStatusFilter;
  label: string;
}> = [
  { value: 'pending', label: 'Pending' },
  { value: 'accepted', label: 'Accepted' },
  { value: 'rejected', label: 'Rejected' },
  { value: 'superseded', label: 'History' },
];

/** Normalize legacy rows without a status to pending, matching SlCard. */
export function proposalStatus(mapping: ProposedMapping): ProposalStatusFilter | null {
  const status = String(mapping.status || 'pending').toLowerCase();
  const match = PROPOSAL_STATUS_FILTERS.find((filter) => filter.value === status);
  return match?.value ?? null;
}

export function mappingsForStatus(
  mappings: ProposedMapping[],
  status: ProposalStatusFilter,
): ProposedMapping[] {
  return mappings.filter((mapping) => proposalStatus(mapping) === status);
}

export const SL_TYPE_META: Record<string, { label: string; color: string }> = {
  join_path: { label: 'Join Path', color: '#2563eb' },
  crosswalk: { label: 'Crosswalk', color: '#4f46e5' },
  value_mapping: { label: 'Value Mapping', color: '#0e7490' },
  identity_rule: { label: 'Identity Rule', color: '#9333ea' },
  field_annotation: { label: 'Field Note', color: '#b45309' },
  insight: { label: 'Finding', color: '#0f766e' },
  schema_drift: { label: 'Schema Drift', color: '#b91c1c' },
  coverage_gap: { label: 'Coverage Gap', color: '#c2410c' },
  metric_definition: { label: 'Metric Definition', color: '#7c3aed' },
};

/* ── Review-panel sections (plan Design decision 6) ─────────────────────── */

export type ProposalSection = 'mapping' | 'annotation' | 'insight' | 'drift';

/** PURE classification of a proposal into its review-panel section:
 * field_annotation → annotation, insight → insight, schema_drift → drift, and
 * EVERYTHING ELSE rule-grade (value_mapping, crosswalk, join_path,
 * identity_rule, correlation, concept_mapping, field_concept_mint, and any
 * unknown future type) → mapping — so no proposal class can vanish from
 * review when the server adds a type the UI has not seen. */
export function sectionOf(m: ProposedMapping): ProposalSection {
  const pType = m.proposed_type || 'value_mapping';
  if (pType === 'field_annotation') return 'annotation';
  if (pType === 'insight') return 'insight';
  if (pType === 'schema_drift') return 'drift';
  return 'mapping';
}

/** Filter out garbage "detected_gap" entries with no real field info. */
export function validMappings(mappings: ProposedMapping[]): ProposedMapping[] {
  return mappings.filter((m) => m.from_field !== 'detected_gap' && m.to_field !== 'detected_gap');
}

/** Human label for the producing pipeline. Uses the stored `provenance`
 * attribute when present; legacy rows (no attribute) fall back to the
 * proposed_type → source mapping: join_path/crosswalk come from the
 * join-discovery heuristics, field_annotation from the post-query analysis
 * LLM, and everything else from the orchestrator's own proposal blocks. */
export function provenanceLabel(m: ProposedMapping): string {
  const p = (m.provenance || '').toLowerCase();
  if (p === 'analysis-llm') return 'analysis LLM';
  if (p) return p;
  const t = m.proposed_type || 'value_mapping';
  if (t === 'join_path' || t === 'crosswalk') return 'heuristic';
  if (t === 'field_annotation') return 'analysis LLM';
  return 'orchestrator';
}

export function pendingCount(mappings: ProposedMapping[]): number {
  return mappings.filter((m) => m.status === 'pending').length;
}

/** One column reference as a code chip: system.table.field (optionally = value). */
function SlChip({
  sysId,
  field,
  value,
  sideLabel,
}: {
  sysId: unknown;
  field?: unknown;
  value?: unknown;
  sideLabel?: string;
}) {
  return (
    <span className="sl-chip">
      {sideLabel ? <span className="sl-side-label">{sideLabel}</span> : null}
      <span className="sys">{String(sysId ?? '')}</span>
      {field ? `.${String(field)}` : ''}
      {value ? (
        <>
          {' = '}
          <span className="val">{String(value)}</span>
        </>
      ) : null}
    </span>
  );
}

/** Field annotation: a single field + the domain note added to it. */
function FieldAnnotationBody({ m, toolCalls }: { m: ProposedMapping; toolCalls: string | number }) {
  const fieldRef = m.from_field ? `${m.from_system}.${m.from_field}` : m.from_system;
  const note = (m.query_context || '').replace(/^Auto-analysis:\s*/i, '').trim();
  return (
    <>
      <div className="sl-anno-lead">
        Add a domain note to <SlChip sysId={m.from_system} field={m.from_field} />
      </div>
      <div className="sl-anno-note">{note || 'No note text captured.'}</div>
      <div className="sl-impact">
        <b>Impact:</b> the AI will see this note attached to <code>{fieldRef}</code> in the
        schema, so it won&apos;t have to rediscover this
        {toolCalls ? (
          <>
            {' (it took '}
            <b>{toolCalls}</b>
            {' tool calls last time)'}
          </>
        ) : null}
        .
      </div>
    </>
  );
}

/** A FINDING — a narrative observation, not a graph rule. No From→To chips
 * (insights have no table.column fields); the narrative lives in query_context. */
function InsightBody({ m }: { m: ProposedMapping }) {
  const note = (m.query_context || '').replace(/^Auto-analysis:\s*/i, '').trim();
  const systems = [m.from_system, m.to_system].filter(Boolean);
  return (
    <>
      {systems.length > 0 && (
        <div className="sl-anno-lead">
          Finding about{' '}
          {systems.map((s, i) => (
            <span key={`${s}-${i}`}>
              {i > 0 ? ' and ' : ''}
              <SlChip sysId={s} />
            </span>
          ))}
        </div>
      )}
      <div className="sl-anno-note">{note || 'No finding text captured.'}</div>
      <div className="sl-impact">
        <b>Impact:</b> Informational — saved as context, never becomes a graph rule.
      </div>
    </>
  );
}

/** join_path / value_mapping / identity_rule — a cross-system relationship. */
function RelationBody({ m, pType, toolCalls }: { m: ProposedMapping; pType: string; toolCalls: string | number }) {
  const what =
    pType === 'value_mapping'
      ? 'translate values between these fields'
      : pType === 'identity_rule'
        ? 'treat these fields as the same concept'
        : 'join these fields directly';
  return (
    <>
      <div className="sl-relation">
        <SlChip sysId={m.from_system} field={m.from_field} value={m.from_value} sideLabel="From" />
        <span className="sl-arrow">→</span>
        <SlChip sysId={m.to_system} field={m.to_field} value={m.to_value} sideLabel="To" />
      </div>
      <div className="sl-impact">
        <b>Impact:</b> saving this lets the AI {what} instead of exploring
        {toolCalls ? (
          <>
            {' (~'}
            <b>{toolCalls}</b>
            {' tool calls last time)'}
          </>
        ) : null}
        .
      </div>
    </>
  );
}

/** Type-aware body dispatcher. Field annotations describe a single field +
 * the note added to it; metric definitions lead with the metric id
 * (ADR-0065/0073); insights are narrative Findings (no From→To chips);
 * relations describe a From → To traversal. */
export function SlCardBody({ m }: { m: ProposedMapping }) {
  const pType = m.proposed_type || 'value_mapping';
  const toolCalls = m.tool_calls_before && m.tool_calls_before !== '0' ? m.tool_calls_before : '';
  if (pType === 'field_annotation') return <FieldAnnotationBody m={m} toolCalls={toolCalls} />;
  if (pType === 'metric_definition') return <MetricDefinitionCardBody m={m} />;
  if (pType === 'insight') return <InsightBody m={m} />;
  return <RelationBody m={m} pType={pType} toolCalls={toolCalls} />;
}

/* ── Impact-first review additions (B2, task 2.3) ───────────────────────── */

/** R3 provenance tiers (orchestrator._provenance_tier semantics, pinned by
 * the task 2.2 contract): 0 user/steward, 1 accepted proposal, 2 llm/unknown.
 * Colors follow the SL_TYPE_META inline-background style. */
export const SL_TIER_META: Record<number, { label: string; color: string }> = {
  0: { label: 'Steward', color: '#15803d' },
  1: { label: 'Accepted', color: '#2563eb' },
  2: { label: 'AI-proposed', color: '#b45309' },
};

function TierBadge({ tier }: { tier: number | undefined }) {
  const meta = tier === undefined ? undefined : SL_TIER_META[tier];
  if (!meta) return null;
  return (
    <span
      className="sl-tier"
      title="Provenance tier — who vouches for this artifact: a steward decision, an accepted proposal, or an unreviewed AI claim"
      style={{
        background: meta.color,
        color: '#fff',
        fontSize: 10,
        fontWeight: 600,
        borderRadius: 3,
        padding: '1px 6px',
        whiteSpace: 'nowrap',
      }}
    >
      {meta.label}
    </span>
  );
}

/** What accepting this proposal would touch (task 2.2 `touches` contract):
 * the systems whose routing seeds gain the rule, the shared concept, the
 * consequence sort score, and how many prior rows it supersedes. Renders
 * nothing when the enrichment is absent (pre-2.2 backend). */
function TouchesLine({ m }: { m: ProposedMapping }) {
  const systems = (m.touches?.systems || []).map((s) => String(s)).filter(Boolean);
  const concept = (m.touches?.concept || '').trim();
  const consequence = Number(m.consequence);
  const supersede = Number(m.touches?.supersede_count);
  const parts: string[] = [];
  if (systems.length > 0) parts.push(`Joins: ${systems.map((s) => systemName(s)).join(' ↔ ')}`);
  if (concept) parts.push(`concept ${concept}`);
  if (Number.isFinite(consequence)) parts.push(`impact ${consequence.toFixed(1)}`);
  if (Number.isFinite(supersede) && supersede > 0) parts.push(`replaces ${supersede} prior`);
  if (parts.length === 0) return null;
  return (
    <div
      className="sl-touches"
      style={{ fontSize: 11, color: 'var(--text2)' }}
      title="What accepting this touches — the systems whose routing seeds gain this rule, the shared concept, and the review-order score (evidence strength × blast radius)"
    >
      {parts.join(' · ')}
    </div>
  );
}

/** Conflict banner (task 2.2 `touches.conflict` — the P3 reverse-conflict
 * flag and B4's "vouched only by unverified authority <id>" reason ride
 * review_reason). Deduped: when the text equals the row's review_reason the
 * existing review banner already shows it — render nothing extra. */
function ConflictBanner({ m }: { m: ProposedMapping }) {
  const conflict = (m.touches?.conflict || '').trim();
  if (!conflict || conflict === (m.review_reason || '').trim()) return null;
  return (
    <div className="sl-review sl-conflict" role="note">
      <div className="sl-review-head">⚠ Conflict</div>
      <div className="sl-review-reason">{conflict}</div>
    </div>
  );
}

/* ── Tombstone flag (ADR-0053 E6, task 2.4) ─────────────────────────────── */

/** The pinned ADR-0053 E6 tombstone review_reason prefix — the backend's
 * exact wording contract is "re-proposal of a shape the steward rejected on
 * <YYYY-MM-DD> (mapping <id>)" (self_learning._tombstone_review_reason; the
 * audit probes key on it too). Matched by prefix so the date/id suffix stays
 * server-composed free text. */
export const TOMBSTONE_REASON_PREFIX = 're-proposal of a shape the steward rejected on ';

/** True when a review_reason carries the pinned E6 tombstone wording. */
export function isTombstoneReason(reason: unknown): boolean {
  return typeof reason === 'string' && reason.startsWith(TOMBSTONE_REASON_PREFIX);
}

/** Head + modifier class for the pending review banner (ADR-0042 D4 +
 * ADR-0053 E6). Precedence: quarantine (circuit breaker — needs human
 * adjudication) → tombstone (the pinned E6 flag; checked BEFORE the
 * prior_rejections flavour because tombstoned rows carry BOTH the flag
 * wording and the rejection history) → legacy rejection history → generic
 * review note. PURE — exported for tests. */
export function reviewBannerFlavour(
  m: ProposedMapping,
  isQuarantined: boolean,
): { head: string; headCls: string } {
  if (isQuarantined) return { head: '⚖ Needs adjudication', headCls: ' sl-review--quarantine' };
  if (isTombstoneReason(m.review_reason)) {
    return { head: '🪦 Steward rejected this shape', headCls: ' sl-review--tombstone' };
  }
  const priorRejCount = Array.isArray(m.prior_rejections) ? m.prior_rejections.length : 0;
  if (priorRejCount > 0) {
    return {
      head: `↩ Re-proposed after rejection (${priorRejCount} prior rejection${priorRejCount === 1 ? '' : 's'})`,
      headCls: ' sl-review--rejected',
    };
  }
  return { head: '⚠ Review note', headCls: '' };
}

/** Focus this rule's node on the Knowledge Graph tab — the thread-clicks
 * navKgNode pattern minus the view switch (we are already inside the
 * Architecture view). neptune-graph is imported dynamically so the chip never
 * forces cytoscape into a parent chunk; focusKgNode already resolves the
 * property-id vs ~id distinction and returning false (graph not rendered /
 * node absent) leaves the tab open with NO selection — the honest fallback. */
function focusRuleNode(nodeId: string): void {
  window.switchArchTab?.('neptune');
  setTimeout(() => {
    import('./neptune-graph')
      .then((mod) => mod.focusKgNode(nodeId))
      .catch(() => {}); // chunk-load failure (deploy skew/offline) — tab stays open, no selection
  }, 300);
}

/** KgContribution-style chip linking a card to its graph node. */
function KgFocusChip({ nodeId }: { nodeId: string }) {
  return (
    <a
      href="#"
      className="sl-kg-link"
      data-nav="kg-node"
      data-node-id={nodeId}
      title="Show this rule's node on the Knowledge Graph tab"
      style={{ fontSize: 11, color: 'var(--accent2)', cursor: 'pointer' }}
      onClick={(e) => {
        e.preventDefault();
        focusRuleNode(nodeId);
      }}
    >
      ⤴ View in graph
    </a>
  );
}

/** Requeue action on ACCEPTED cards: sends the row back to pending for
 * re-review; the server undoes the graph artifact the accept created. */
function RequeueButton({
  mappingId,
  onRequeue,
}: {
  mappingId: string;
  onRequeue: (mappingId: string) => void;
}) {
  return (
    <button
      className="sl-requeue"
      onClick={() => onRequeue(mappingId)}
      title="Send back to pending for re-review — removes or deactivates the applied rule in the knowledge graph"
    >
      ↩ Requeue
    </button>
  );
}

/** Re-open action on REJECTED cards (ADR-0053 E6 tombstone escape hatch):
 * flips the row back to pending — a pending row no longer matches the
 * tombstone predicate (rejected-status rows only), so the shape becomes
 * reviewable and proposable again. RequeueButton idiom. */
function ReopenButton({
  mappingId,
  onReopen,
}: {
  mappingId: string;
  onReopen: (mappingId: string) => void;
}) {
  return (
    <button
      className="sl-reopen"
      onClick={() => onReopen(mappingId)}
      title="Re-open for review — moves this back to pending and clears the tombstone, so the shape can be proposed again"
    >
      ↩ Re-open
    </button>
  );
}

/** Modify-loop actions on ACCEPTED cards: soft deactivate / reactivate of an
 * identity rule's graph node through the existing lifecycle routes (the
 * session-local mark in the proposals store `ruleLifecycle` flips the
 * button), plus the Requeue action available on every accepted card. */
function LifecycleActions({
  nodeId,
  lifecycle,
  onLifecycle,
  mappingId,
  onRequeue,
}: {
  nodeId?: string;
  lifecycle?: 'deactivated';
  onLifecycle?: (nodeId: string, action: 'deactivate' | 'reactivate') => void;
  mappingId?: string;
  onRequeue?: (mappingId: string) => void;
}) {
  const deactivated = lifecycle === 'deactivated';
  return (
    <div className="sl-card-actions">
      {nodeId && deactivated ? (
        <button
          className="sl-lifecycle reactivate"
          onClick={() => onLifecycle?.(nodeId, 'reactivate')}
          title="Resume routing through this rule"
        >
          ⟲ Reactivate
        </button>
      ) : null}
      {nodeId && !deactivated ? (
        <button
          className="sl-lifecycle deactivate"
          onClick={() => onLifecycle?.(nodeId, 'deactivate')}
          title="Soft-deactivate — routing skips this rule until reactivated (kept for audit, never deleted)"
        >
          ⏻ Deactivate
        </button>
      ) : null}
      {mappingId && onRequeue ? <RequeueButton mappingId={mappingId} onRequeue={onRequeue} /> : null}
    </div>
  );
}

/** Human label for the strongest B3 usage-evidence class on a row (ledger
 * classes: relationship-proof > tool-text > route-intersection). */
export function usageEvidenceLabel(classes: unknown): string {
  if (!Array.isArray(classes)) return '';
  const set = new Set(classes.map((c) => String(c)));
  if (set.has('relationship-proof')) return 'verified use';
  if (set.has('tool-text')) return 'observed use';
  if (set.has('route-intersection')) return 'routing evidence';
  return '';
}

export interface SlCardProps {
  m: ProposedMapping;
  /** Optimistic accept/reject mark (slOptimisticMark port): the card renders
   * as applied ("✓ Applied" / "✗ Removed") until the silent reconcile lands
   * or the API call fails (mark removed → buttons return). */
  optimistic?: 'accept' | 'reject';
  onAccept?: (mappingId: string) => void;
  onReject?: (mappingId: string) => void;
  onEdit?: (mappingId: string) => void;
  /** Session-local rule-lifecycle mark for this row's graph node (accepted
   * identity rules) — 'deactivated' flips the Deactivate button to Reactivate. */
  lifecycle?: 'deactivated';
  /** Fires the deactivate/reactivate POST for the row's neptune_node_id. */
  onLifecycle?: (nodeId: string, action: 'deactivate' | 'reactivate') => void;
  /** Fires the requeue POST — moves an accepted row back to pending and
   * undoes the graph artifact its accept created. */
  onRequeue?: (mappingId: string) => void;
  /** Fires the reopen POST — moves a REJECTED row back to pending, clearing
   * its tombstone (ADR-0053 E6 steward escape hatch). */
  onReopen?: (mappingId: string) => void;
}

/** One proposal card (.sl-card) — the schema-verdict gating is the load-bearing
 * behavior: a definitive 'not_found' disables Accept and renders the ⛔ proof
 * block; 'verified' renders the ✅ line; indeterminate falls back to the
 * free-text ai_recommendation. */
export function SlCard({ m, optimistic, onAccept, onReject, onEdit, lifecycle, onLifecycle, onRequeue, onReopen }: SlCardProps) {
  // The "Joins:" line and the schema-check sentence name systems: re-render
  // when catalog names arrive.
  useSystemNamesVersion();
  // Try-it validation drawer (B2, task 3.2): steward-visible only (the
  // module-cached /api/auth/me check — one fetch per page, not per card).
  const stewardTools = useStewardVisible();
  const [tryItOpen, setTryItOpen] = useState(false);
  const pType = m.proposed_type || 'value_mapping';
  const meta = SL_TYPE_META[pType] || { label: pType, color: '#475569' };
  const status = m.status || 'pending';
  // Optimistic mark overrides the displayed status (accept → accepted,
  // reject → rejected) exactly like the old in-place DOM patch.
  const applied = optimistic ? (optimistic === 'accept' ? 'accepted' : 'rejected') : null;
  const effStatus = applied ?? status;
  const statusColor =
    effStatus === 'accepted'
      ? 'var(--green)'
      : effStatus === 'rejected'
        ? 'var(--muted)'
        : 'var(--amber)';
  const conf = m.confidence ? (parseFloat(String(m.confidence)) * 100).toFixed(0) + '%' : '—';

  // Read-only usage signal (item C — utility loop): on ACCEPTED cards, show how
  // often the AI actually applied this saved rule ("used n× · last <date>") so
  // unused rules can be spotted and retired. Omitted entirely when the rule has
  // never been used (usage_count absent/0) or the card is not accepted.
  let usage: JSX.Element | null = null;
  if (status === 'accepted' && m.usage_count != null) {
    const usedN = parseInt(String(m.usage_count), 10);
    if (!isNaN(usedN) && usedN > 0) {
      const lastDate = m.last_used_at ? new Date(m.last_used_at) : null;
      const lastText =
        lastDate && !isNaN(lastDate.getTime()) ? ` · last ${lastDate.toLocaleDateString()}` : '';
      // B3 evidence class (task 2.3): say HOW the use was proven — a
      // relationship proof, tool-text extraction, or the weaker
      // route-intersection grounding. Absent on pre-B3 rows → no segment.
      const evidence = usageEvidenceLabel(m.evidence_classes);
      const evidenceText = evidence ? ` · ${evidence}` : '';
      usage = (
        <span
          className="sl-usage"
          title="Times the AI applied this saved rule while answering questions"
          style={{ fontSize: 11, color: 'var(--text2)', whiteSpace: 'nowrap' }}
        >
          used {usedN}×{lastText}
          {evidenceText}
        </span>
      );
    }
  }

  // Gate acceptance on the server's live schema-existence verdict: a definitive
  // "column not found" means accepting would write an unusable mapping.
  const acceptBlocked = (m.schema_verdict || '') === 'not_found';

  // Shared review-reason banner (ADR-0042 D4 + ADR-0053 E6): every held
  // proposal states WHY it needs review. One additive attribute
  // (review_reason) carries all four flavours emitted by the self_learning
  // lifecycle — quarantine, the pinned E6 tombstone flag, legacy rejection
  // history, and the generic ambiguity note; head/class selection lives in
  // reviewBannerFlavour (pure, exported). Pending cards only (raw status —
  // an optimistic mark keeps the banner, like the legacy in-place actions
  // patch). review_reason is server-composed; JSX auto-escaping replaces the
  // legacy _slEsc discipline.
  const isQuarantined = status === 'pending' && m.quarantined === 'true';
  let review: JSX.Element | null = null;
  if (status === 'pending' && m.review_reason) {
    const { head, headCls } = reviewBannerFlavour(m, isQuarantined);
    review = (
      <div className={`sl-review${headCls}`} role="note">
        <div className="sl-review-head">{head}</div>
        <div className="sl-review-reason">{m.review_reason}</div>
      </div>
    );
  }

  // Value overlap signal for join_path/crosswalk types.
  let overlap: JSX.Element | null = null;
  if ((pType === 'join_path' || pType === 'crosswalk') && status === 'pending') {
    const overlapSig = m.value_overlap || m.overlap_ratio || m.overlap_count;
    const ratio = m.overlap_ratio || m.overlap_ratio_confidence;
    if (overlapSig || ratio) {
      let badge = 'not measured';
      let badgeColor = 'var(--text2)';
      if (ratio !== undefined) {
        const r = parseFloat(String(ratio));
        if (r >= 0.5) {
          badge = 'High';
          badgeColor = 'var(--green)';
        } else if (r >= 0.2) {
          badge = 'Med';
          badgeColor = 'var(--amber)';
        } else {
          badge = 'Low';
          badgeColor = 'var(--muted)';
        }
      }
      const overlapText = overlapSig ? `${overlapSig} shared` : '';
      const ratioText = ratio ? `ratio ${(parseFloat(String(ratio)) * 100).toFixed(0)}%` : '';
      const signal = [overlapText, ratioText].filter(Boolean).join(' · ');
      overlap = (
        <div className="sl-overlap">
          <span className="sl-overlap-label">Impact:</span>{' '}
          <span className="sl-overlap-badge" style={{ color: badgeColor }}>
            {badge}
          </span>{' '}
          <span className="sl-overlap-signal">({signal})</span>
        </div>
      );
    } else {
      overlap = (
        <div className="sl-overlap">
          <span className="sl-overlap-label">Impact:</span>{' '}
          <span className="sl-overlap-none">not measured</span>
        </div>
      );
    }
  }

  // Insight (Finding) cards: Accept just marks reviewed (its accept path writes
  // nothing to the graph), Reject dismisses, and Edit is HIDDEN — edit validates
  // table.column fields which insights do not have.
  const isInsight = pType === 'insight';
  const isGuidance = pType === 'guidance_note';
  let actions: JSX.Element | null = null;
  if (applied) {
    // Optimistic in-place patch: actions replaced with the applied marker.
    actions = (
      <div className="sl-card-actions">
        {optimistic === 'accept' ? (
          <span className="sl-applied">✓ Applied</span>
        ) : (
          <span className="sl-applied sl-applied--rejected">✗ Removed</span>
        )}
      </div>
    );
  } else if (status === 'pending' && isGuidance) {
    actions = (
      <div className="sl-card-actions">
        <a className="sl-guidance-preview" href="/admin.html#tab-vocabulary" title="Preview delivery and confirm any exact loss in the Guidance editor">
          PREVIEW in Guidance editor
        </a>
        <button className="sl-reject" onClick={() => onReject?.(m.mapping_id)} title="Discard this proposal">
          ✗ Reject
        </button>
      </div>
    );
  } else if (status === 'pending') {
    actions = (
      <div className="sl-card-actions">
        {acceptBlocked ? (
          <button
            className="sl-accept"
            disabled
            aria-disabled="true"
            title="Blocked — the column was not found in the onboarded schema, so this cannot be accepted"
          >
            ✓ Accept
          </button>
        ) : (
          <button
            className="sl-accept"
            onClick={() => onAccept?.(m.mapping_id)}
            title={
              isInsight
                ? 'Mark this finding reviewed — informational only, writes nothing to the knowledge graph'
                : 'Save to the knowledge graph'
            }
          >
            ✓ Accept
          </button>
        )}
        <button
          className="sl-reject"
          onClick={() => onReject?.(m.mapping_id)}
          title={isInsight ? 'Dismiss this finding' : 'Discard this proposal'}
        >
          ✗ Reject
        </button>
        {isInsight ? null : (
          <button
            className="sl-edit"
            onClick={() => onEdit?.(m.mapping_id)}
            title="Edit this proposal before accepting"
          >
            ✎ Edit
          </button>
        )}
        {/* Try-it (B2, task 3.2): steward-only impact preview for relation
          * proposals — insights never become rules and field annotations are
          * schema notes, so neither can be injected into a route seed. */}
        {stewardTools && !isInsight && pType !== 'field_annotation' ? (
          <button
            className="sl-tryit"
            disabled={tryItOpen}
            onClick={() => setTryItOpen(true)}
            title="Preview impact — answers one real question twice (with and without this rule); takes a few minutes and incurs normal model cost"
          >
            ▶ Try it
          </button>
        ) : null}
      </div>
    );
  } else if (status === 'accepted') {
    // Modify loop (task 2.3) + requeue: accepted identity rules expose the
    // existing soft deactivate/reactivate lifecycle routes, and EVERY
    // accepted card gets a Requeue action (back to pending for re-review,
    // undoing the graph artifact the accept created).
    const ruleNodeId = pType === 'identity_rule' ? m.neptune_node_id : undefined;
    if (ruleNodeId || (onRequeue && m.mapping_id)) {
      actions = (
        <LifecycleActions
          nodeId={ruleNodeId}
          lifecycle={lifecycle}
          onLifecycle={onLifecycle}
          mappingId={m.mapping_id}
          onRequeue={onRequeue}
        />
      );
    }
  } else if (status === 'rejected' && onReopen && m.mapping_id) {
    // Tombstone escape hatch (ADR-0053 E6, task 2.4): every rejected card
    // gets a Re-open action — back to pending, clearing the tombstone.
    actions = (
      <div className="sl-card-actions">
        <ReopenButton mappingId={m.mapping_id} onReopen={onReopen} />
      </div>
    );
  }

  // Structured schema verdict WITH proof of what was checked; a definitive
  // block suppresses the discovery rationale (it argued to accept).
  const verdict = m.schema_verdict || '';
  const checked = m.schema_checked || '';
  let verdictEl: JSX.Element | null = null;
  let suppressWhyAnalysis = false;
  if (verdict === 'not_found') {
    verdictEl = (
      <div className="sl-block" role="alert">
        <div className="sl-block-head">⛔ Blocked — do not accept</div>
        <div className="sl-block-proof">
          Checked <code>{checked}</code> against the onboarded <b>{systemName(m.from_system)}</b> schema — the
          column is not present.
        </div>
      </div>
    );
    suppressWhyAnalysis = true; // the discovery rationale argued to accept — hide it
  } else if (verdict === 'verified') {
    verdictEl = (
      <div className="sl-verified">
        ✅ Verified — <code>{checked}</code> exists in the onboarded schema.
      </div>
    );
  }
  const aiConf = m.ai_confidence || '';
  const aiColor =
    aiConf === 'high' ? 'var(--green)' : aiConf === 'medium' ? 'var(--amber)' : 'var(--red)';
  const ai =
    !verdict && m.ai_recommendation ? (
      <div className="sl-ai" style={{ color: aiColor }}>
        ⚠ {m.ai_recommendation}
      </div>
    ) : null;

  // Collapsible "why" — the discovery's own evidence (method/reason, #3
  // threading) + query context (provenance) + analysis (rationale).
  const ctx = (m.query_context || '').replace(/^Auto-analysis:\s*/i, '').trim();
  const method = (m.method || '').trim();
  const reason = (m.reason || '').trim();
  const showAnalysis = m.analysis && !suppressWhyAnalysis;
  const why =
    ctx || showAnalysis || method || reason ? (
      <details className="sl-why">
        <summary>Why was this discovered?</summary>
        <div className="sl-why-body">
          {method ? (
            <>
              <span className="lbl">Method:</span> {method}
              <br />
            </>
          ) : null}
          {reason ? (
            <>
              <span className="lbl">Reason:</span> {reason}
              <br />
            </>
          ) : null}
          {ctx ? (
            <>
              <span className="lbl">Trigger:</span> {ctx}
              <br />
            </>
          ) : null}
          {showAnalysis ? (
            <>
              <span className="lbl">Analysis:</span> {m.analysis}
            </>
          ) : null}
          {suppressWhyAnalysis ? (
            <>
              <span className="lbl">Note:</span> earlier rationale suppressed — superseded by the
              live schema check above.
            </>
          ) : null}
        </div>
      </details>
    ) : null;

  return (
    <div
      className={`sl-card ${effStatus}${verdict === 'not_found' ? ' blocked' : ''}${isQuarantined ? ' quarantined' : ''}`}
      data-mapping-id={m.mapping_id}
      style={
        applied && optimistic === 'reject'
          ? { opacity: 0.6 }
          : lifecycle === 'deactivated'
            ? { opacity: 0.75 }
            : undefined
      }
    >
      <div className="sl-card-head">
        <span className="sl-type" style={{ background: meta.color }}>
          {meta.label}
        </span>
        {status === 'pending' ? <TierBadge tier={m.provenance_tier} /> : null}
        <span
          className="sl-prov"
          title="Which pipeline produced this proposal"
          style={{ fontSize: 10, color: 'var(--text2)', whiteSpace: 'nowrap' }}
        >
          via {provenanceLabel(m)}
        </span>
        {/* Two DISTINCTLY-LABELED confidences (a bare "conf 90%"
          * next to ai_confidence:low read as a contradiction): "proposed" is
          * the proposer's own stated confidence at discovery time; "AI review"
          * is the independent reviewer's calibrated verdict. */}
        <span
          className="sl-conf"
          title="Confidence stated by the proposer at discovery time — not an independent assessment"
        >
          proposed <b>{conf}</b>
        </span>
        {aiConf ? (
          <span
            className="sl-conf sl-conf--ai"
            title="Independent AI reviewer's confidence in this proposal"
            style={{ color: aiColor }}
          >
            AI review <b>{aiConf}</b>
          </span>
        ) : null}
        <span className="sl-status">
          <span className="dot" style={{ color: statusColor }}>
            ●
          </span>
          {effStatus}
        </span>
        {usage}
        {/* Server-computed degeneracy verdict (plan Design decision 5):
          * rendered VERBATIM from the payload — the predicate lives in
          * agents/proposal_scoring.degeneracy_flags and is NEVER re-derived
          * client-side. Advisory only: Accept/Reject/Edit stay untouched. */}
        {m.likely_low_value === true ? (
          <span className="sl-low-value" title={m.degeneracy?.join(', ')}>
            likely low-value
          </span>
        ) : null}
        {/* ADR-0063 D2: the trust signals the engine already computes, made
          * visible. Server-composed; renders nothing when absent. */}
        <TrustChip m={m} />
        <span className="sl-head-spacer" />
        {actions}
      </div>
      <div className="sl-body">
        <SlCardBody m={m} />
        {status === 'pending' ? <TouchesLine m={m} /> : null}
        {verdictEl}
        {review}
        <ConflictBanner m={m} />
        {overlap}
        {ai}
        {why}
        {m.neptune_node_id ? <KgFocusChip nodeId={m.neptune_node_id} /> : null}
        {tryItOpen ? <TryItPanel m={m} onClose={() => setTryItOpen(false)} /> : null}
      </div>
    </div>
  );
}

export interface LgCardProps {
  sys: QueryGuidanceSystem;
  onPrune?: (systemId: string, note: string) => void;
  onClearLearned?: (systemId: string) => void;
}

/** One system's learned notes as a card, each note with a prune (✗) control. */
export function LgCard({ sys, onPrune, onClearLearned }: LgCardProps) {
  useSystemNamesVersion(); // the header names the system
  const notes = Array.isArray(sys.notes) ? sys.notes : [];
  const sid = sys.system_id || '';
  const len = sys.length || 0;
  const max = sys.max_len || 0;
  return (
    <div className="sl-card">
      <div className="sl-card-head">
        <span className="sl-type" style={{ background: '#0e7490' }}>
          {systemName(sid)}
        </span>
        <span className="sl-conf">
          {notes.length} learned
          {/* No notes, no meter: the server's length counts the notes
            * document's own JSON, so an empty one still reads 108 chars. */}
          {max && notes.length > 0 ? (
            <>
              {' · '}
              <span className="lg-meter">
                {len}/{max} chars
              </span>
            </>
          ) : null}
        </span>
        <span className="sl-head-spacer" />
        {notes.length > 0 && (
          <button
            className="sl-reject"
            onClick={() => onClearLearned?.(sid)}
            title="Remove all learned notes (keeps the onboarded base guidance)"
          >
            Clear learned
          </button>
        )}
      </div>
      <div className="sl-body">
        {notes.length > 0 ? (
          notes.map((n, i) => (
            <div key={i} className="lg-note">
              <button
                className="lg-prune"
                data-note={n}
                onClick={() => onPrune?.(sid, n)}
                title="Remove this learned note"
              >
                ✗
              </button>
              <span className="lg-note-text">{n}</span>
            </div>
          ))
        ) : (
          <div className="sl-empty">No learned notes yet — only the onboarded base guidance.</div>
        )}
      </div>
    </div>
  );
}
