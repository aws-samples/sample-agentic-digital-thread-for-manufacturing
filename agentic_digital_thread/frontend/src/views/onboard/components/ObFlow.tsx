// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* <ObFlow/> (jsx-migration W5.1) — the #onboard-flow area rendered from the
 * flow store: ordered message entries (agent/user messages, loading dots,
 * tool pills, checkpoints, collapsed step groups, discovery/reconcile/
 * connections cards, the credential form, the post-verify CTA), the wizard
 * progress bar + persistent chat input, the welcome block (intro + seed
 * chips + welcome input), and the detail
 * screen that replaces the flow (the old innerHTML swap).
 *
 * Contract ids/classes preserved: #onboard-flow, #ob-welcome-chat,
 * #ob-welcome-send, #ob-scroll-btn, #ob-flow-chat, #ob-flow-send,
 * .ob-confirm-btn, .ob-seed-chips/.ob-seed-chip, .tool-pill,
 * .ob-collapsed-section, #ob-step-<i>/#ob-line-<i>/#ob-progress-status. */
import { memo, useEffect, useRef, useState } from 'react';

import { requestJson } from '../../../api/client';
import type {
  RejectedIdentityRule,
  RemapResponse,
  RemapSuggestion,
  VocabFit,
} from '../../../api/types/onboard';
import { Md } from '../../../shared/ui/Md';
import { ErrorState } from '../../../shared/ui/AsyncStates';
import { buildRemapBody } from '../payloads';
import { useI18n } from '../../../lib/i18n';
import { useStore } from '../../../lib/store';
import { systemName, useSystemNamesVersion } from '../../../lib/system-names';
import { obAttachFile, obSendChat, obSubmitCredentials, welcomeSubmit } from '../chat';
import {
  obAskAbout,
  obCancelActive,
  obConfirmCancel,
  obResetAndConnect,
  obRunInProgress,
} from '../flow';
import {
  getOnboardQueryClient,
  invalidateOnboardingRemap,
  useConceptsQuery,
} from '../onboard-queries';
import {
  confirmChatCheckpoint,
  decideDiscoveryRow,
  decideReconcileRow,
  flowStore,
  removeReconcileBanner,
  selectFlowAppendSeq,
  selectFlowBanners,
  selectFlowChips,
  selectFlowEntries,
  selectFlowMode,
  selectFlowProgress,
  selectFlowScreen,
  setReconcileRowError,
  type FlowEntry,
  type ProgressState,
  type ReconcileRowVm,
} from '../flow-store';
import { basisFor } from '../render';
import { provisionSummaryMd, resolveContextBefore } from '../provision-summary';
import { verifySummary } from '../verify-summary';
import { ob } from '../state';
import { catalogSystems } from '../catalog';
import { AgentTestLine } from './AgentTestLine';
import { Checkpoint, Dots, RulesEmptyState, WithheldRules, isFirstConnectedSystem } from './Checkpoint';
import { ConceptProposalCard, type ConceptProposalResult } from './ConceptProposalCard';
import { ConnectionsDiscovered, ResolveExtras } from './ResolveExtras';
import { ContextInterviewCard } from './ContextInterviewCard';
import { PreMapProbe } from './PreMapProbe';
import { RuntimeQuestions } from './RuntimeQuestions';
import { SchemaColumnsCell } from './SchemaColumnsCell';
import { SystemDetail } from './SystemDetail';

// ── Progress bar (wizard: status + steps + cancel; chat: dots only) ──────────

const OB_STEP_LABELS = ['Discover', 'Map', 'Resolve', 'Provision', 'Verify'];

function stepClass(i: number, p: ProgressState): string {
  if (p.allDone) return 'ob-step done';
  return 'ob-step' + (i < p.activeIdx ? ' done' : i === p.activeIdx ? ' active' : '');
}

function lineClass(i: number, p: ProgressState): string {
  if (p.allDone) return 'ob-step-line done';
  if (p.variant === 'chat') {
    return (
      'ob-step-line' + (i < p.activeIdx ? ' done' : i === p.activeIdx ? ' active' : '')
    );
  }
  return (
    'ob-step-line' +
    (i < p.activeIdx ? ' done' : i === p.activeIdx && p.activeIdx > 0 ? ' active' : '')
  );
}

function ObProgress({ p }: { p: ProgressState }) {
  const steps = OB_STEP_LABELS.flatMap((label, i) => {
    const parts = [] as React.ReactNode[];
    if (i > 0) {
      parts.push(<div key={`l${i}`} className={lineClass(i, p)} id={`ob-line-${i}`} />);
    }
    parts.push(
      <div key={`s${i}`} className={stepClass(i, p)} id={`ob-step-${i}`}>
        <div className="ob-step-dot" />
        <div className="ob-step-label">{label}</div>
      </div>,
    );
    return parts;
  });

  if (p.variant === 'chat') {
    return <div className="ob-progress">{steps}</div>;
  }
  // Layout: status text (left) → steps (centered/stretched) → Cancel (right).
  // See the 3-column grid in .ob-progress (onboard.css).
  return (
    <div className="ob-progress">
      <div className="ob-progress-status" id="ob-progress-status">
        {p.statusText}
      </div>
      <div className="ob-progress-steps">{steps}</div>
      {!p.cancelHidden && (
        <button
          className="ob-cancel-btn"
          title="Cancel onboarding"
          // Ask only while the run is still working; a finished or Retry-parked
          // wizard has nothing to cancel and is put away at once (the rule
          // obGuardStart uses). Cancel stays: it is the way back to the catalog.
          onClick={() => (obRunInProgress() ? obConfirmCancel() : obCancelActive())}
        >
          Cancel
        </button>
      )}
    </div>
  );
}

// ── Individual entry renderers ────────────────────────────────────────────────

const AgentBadge = () => {
  const { t } = useI18n();
  return <span className="ob-agent-badge">{t('onboard.agentBadge')}</span>;
};
const UserBadge = () => <span className="ob-user-badge">You</span>;

const AttachIcon = () => (
  <svg
    viewBox="0 0 20 20"
    fill="none"
    stroke="currentColor"
    strokeWidth="1.5"
    width="16"
    height="16"
  >
    <path d="M15.5 10l-6.4 6.4a3.5 3.5 0 01-5-5L10.6 5a2.3 2.3 0 013.3 3.3L7.5 14.7a1.2 1.2 0 01-1.7-1.7L12.2 6.6" />
  </svg>
);

function CredentialForm({
  entry,
}: {
  entry: Extract<FlowEntry, { kind: 'credentialForm' }>;
}) {
  const [values, setValues] = useState<Record<string, string>>({});

  if (entry.status === 'stored') {
    return (
      <div className="ob-credential-form">
        <div
          style={{ padding: '0.75rem', color: 'var(--green,#4ade80)', fontSize: '0.85rem' }}
        >
          Credentials stored securely.
        </div>
        {entry.note && (
          <div
            style={{ padding: '0 0.75rem 0.75rem', color: 'var(--muted)', fontSize: '0.8rem' }}
          >
            {entry.note}
          </div>
        )}
      </div>
    );
  }

  const submit = () => {
    const creds: Record<string, string> = {};
    entry.fields.forEach((f) => {
      if (values[f.name]) creds[f.name] = values[f.name];
    });
    void obSubmitCredentials(entry.id, entry.systemId, creds);
  };

  return (
    <div className="ob-credential-form">
      <div
        style={{
          padding: '1rem',
          background: 'var(--surface)',
          border: '1px solid var(--accent)',
          borderRadius: 8,
          margin: '0.5rem 0',
        }}
      >
        <div
          style={{
            fontSize: '0.75rem',
            color: 'var(--accent)',
            textTransform: 'uppercase',
            letterSpacing: '0.05em',
            marginBottom: '0.75rem',
          }}
        >
          <svg
            viewBox="0 0 20 20"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.5"
            width="14"
            height="14"
            style={{ verticalAlign: 'middle', marginRight: '0.25rem' }}
          >
            <rect x="3" y="9" width="14" height="8" rx="2" />
            <path d="M7 9V6a3 3 0 016 0v3" />
          </svg>
          Secure Credentials
        </div>
        {entry.fields.map((f) => (
          <span key={f.name}>
            <label
              style={{
                display: 'block',
                fontSize: '0.7rem',
                color: 'var(--muted)',
                marginBottom: '0.15rem',
                marginTop: '0.5rem',
              }}
            >
              {f.name}
            </label>
            <input
              name={f.name}
              type={f.type || 'text'}
              placeholder={f.name}
              value={values[f.name] || ''}
              onChange={(e) => setValues((v) => ({ ...v, [f.name]: e.target.value }))}
              style={{
                width: '100%',
                padding: '0.4rem',
                background: 'var(--bg)',
                border: '1px solid var(--border)',
                borderRadius: 4,
                color: 'var(--text)',
                fontSize: '0.8rem',
                boxSizing: 'border-box',
              }}
            />
          </span>
        ))}
        <button
          disabled={entry.status === 'storing'}
          onClick={submit}
          style={{
            marginTop: '0.75rem',
            padding: '0.5rem 1.25rem',
            background: 'var(--accent)',
            color: '#fff',
            border: 'none',
            borderRadius: 4,
            cursor: 'pointer',
            fontWeight: 600,
            fontSize: '0.8rem',
          }}
        >
          {entry.status === 'storing'
            ? 'Storing...'
            : entry.errorText || 'Store Securely'}
        </button>
      </div>
    </div>
  );
}

// ── Vocabulary-fit banner (Wave 2, contract C5) ───────────────────────────────

/** Displayed counters — the block only renders when all of them are honest
 * numbers (a partial/malformed block hides, same as an absent one). */
const VOCAB_FIT_DISPLAY_KEYS = [
  'coverage_pct',
  'mapped_confident',
  'total_columns',
  'needs_review',
  'reconciled',
  'vocabulary_size',
] as const;

/** Defensive read of the additive `vocab_fit` block from an untyped map
 * result (older backends omit the key ⇒ null ⇒ no banner). */
function vocabFitFrom(result: Record<string, unknown>): VocabFit | null {
  const raw = result.vocab_fit;
  if (!raw || typeof raw !== 'object') return null;
  const fit = raw as Record<string, unknown>;
  const complete = VOCAB_FIT_DISPLAY_KEYS.every((k) => typeof fit[k] === 'number');
  return complete ? (raw as VocabFit) : null;
}

/** Shared tone styling for both fit banners (plain vs warning). */
function fitBannerStyle(low: boolean): React.CSSProperties {
  return {
    fontSize: '0.8rem',
    padding: '0.5rem 0.75rem',
    margin: '0.5rem 0',
    borderRadius: 6,
    background: low ? 'rgba(210,153,34,0.1)' : 'var(--surface)',
    border: low ? '1px solid rgba(210,153,34,0.3)' : '1px solid var(--border)',
    color: low ? 'var(--amber)' : 'var(--muted)',
  };
}

/** Compact vocabulary-fit banner above the Field Mappings block. Coverage
 * below 50% switches to the warning tone and points the user at the Admin →
 * Vocabulary tab (plain text — no new routing). Non-blocking guidance only. */
function VocabFitBanner({ fit }: { fit: VocabFit }) {
  const low = fit.coverage_pct < 50;
  return (
    <div
      className={'ob-vocab-fit' + (low ? ' ob-vocab-fit-low' : '')}
      style={fitBannerStyle(low)}
    >
      <span>
        Vocabulary fit: {fit.coverage_pct}% coverage — {fit.mapped_confident} of{' '}
        {fit.total_columns} columns mapped confidently · {fit.needs_review} need
        review · {fit.reconciled} reconciled · {fit.vocabulary_size} concepts in
        vocabulary
      </span>
      {low && (
        <div style={{ marginTop: '0.25rem' }}>
          The vocabulary may not fit this system — consider importing a domain
          concept pack in the Admin → Vocabulary tab.
        </div>
      )}
    </div>
  );
}

// ── Pre-map vocabulary-fit checkpoint (ADR-0068, `vocab_fit` step) ───────────

/** Displayed counters of the PRE-map fit report (`assess_vocabulary_fit`) —
 * the banner renders only when all of them are honest numbers plus a string
 * suggestion (a partial/malformed result hides, same as an absent one). */
const PRE_MAP_FIT_KEYS = [
  'fit_pct',
  'columns_with_candidate',
  'total_columns',
  'vocabulary_size',
] as const;

type PreMapFit = {
  fit_pct: number;
  columns_with_candidate: number;
  total_columns: number;
  vocabulary_size: number;
  suggestion: string;
};

function preMapFitFrom(result: Record<string, unknown>): PreMapFit | null {
  const complete =
    PRE_MAP_FIT_KEYS.every((k) => typeof result[k] === 'number') &&
    typeof result.suggestion === 'string';
  return complete ? (result as unknown as PreMapFit) : null;
}

/** Pre-map variant of the fit banner: candidate coverage BEFORE any mappings
 * exist. A non-"ok" computed suggestion switches to the warning tone;
 * "consider_pack" adds the domain-pack pointer (plain text, no routing). */
function PreMapVocabFitBanner({ fit }: { fit: PreMapFit }) {
  const low = fit.suggestion !== 'ok';
  return (
    <div
      className={'ob-vocab-fit' + (low ? ' ob-vocab-fit-low' : '')}
      style={fitBannerStyle(low)}
    >
      <span>
        Vocabulary fit: {fit.fit_pct}% — {fit.columns_with_candidate} of{' '}
        {fit.total_columns} columns have a candidate concept ·{' '}
        {fit.vocabulary_size} concepts in vocabulary
      </span>
      {fit.suggestion === 'consider_pack' && (
        <div style={{ marginTop: '0.25rem' }}>
          The vocabulary may not fit this system — consider importing a domain
          concept pack in the Admin → Vocabulary tab.
        </div>
      )}
    </div>
  );
}

/** `vocab_fit` checkpoint body: the pre-map fit banner plus — when the tool
 * reports context questions worth asking or a non-"ok" fit — the inline
 * business-context interview card (its transport is the guided endpoints;
 * question text never rides the tool result). */
function VocabFitCheckpointBody({ result }: { result: Record<string, unknown> }) {
  const fit = preMapFitFrom(result);
  const askContext =
    (typeof result.context_questions === 'number' && result.context_questions > 0) ||
    result.suggestion !== 'ok';
  return (
    <>
      <h4>✦ Vocabulary Fit</h4>
      {fit && <PreMapVocabFitBanner fit={fit} />}
      {askContext && typeof result.system_id === 'string' && (
        <ContextInterviewCard
          systemId={result.system_id}
          firstLaunch={result.first_launch === true}
        />
      )}
    </>
  );
}

/** LLM welcome-path checkpoint card (obStreamChat renderCheckpoint) —
 * summary tables per step + Confirm/Re-run that send follow-up messages
 * through the welcome chat. */
function ChatCheckpoint({
  entry,
}: {
  entry: Extract<FlowEntry, { kind: 'chatCheckpoint' }>;
}) {
  // Result cards name systems by catalog name (lib/system-names.ts).
  useSystemNamesVersion();
  const { step } = entry;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const result = entry.result as Record<string, any>;
  const stepLabels: Record<string, string> = {
    discover: 'Schema',
    map: 'Mappings',
    resolve: 'Rules',
    provision: 'Provisioning',
    verify: 'Verification',
    vocab_fit: 'Vocabulary fit',
    concept_proposal: 'Proposed concepts',
  };

  let body: React.ReactNode = null;
  if (step === 'discover' && result.tables) {
    body = (
      <>
        <h4>✦ Schema Discovery</h4>
        <table className="ob-table">
          <thead>
            <tr>
              <th>Table</th>
              <th>Rows</th>
              <th>Columns</th>
            </tr>
          </thead>
          <tbody>
            {/* eslint-disable-next-line @typescript-eslint/no-explicit-any */}
            {(result.tables as any[]).map((t, i) => (
              <tr key={i}>
                <td>{t.name}</td>
                <td>{(t.row_count || 0).toLocaleString()}</td>
                <td>
                  <SchemaColumnsCell table={t} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </>
    );
  } else if (step === 'map' && result.mappings) {
    const fit = vocabFitFrom(result);
    body = (
      <>
        <h4>✦ Field Mappings</h4>
        {fit && <VocabFitBanner fit={fit} />}
        <table className="ob-table">
          <thead>
            <tr>
              <th>Field</th>
              <th>Concept</th>
              <th>Confidence</th>
            </tr>
          </thead>
          <tbody>
            {/* eslint-disable-next-line @typescript-eslint/no-explicit-any */}
            {(result.mappings as any[]).map((m, i) => {
              const pct =
                m.confidence <= 1
                  ? Math.round(m.confidence * 100)
                  : Math.round(m.confidence);
              const isReview = m.concept === 'needs_review';
              const conf = isReview
                ? 'review'
                : pct >= 90
                  ? 'high'
                  : pct >= 70
                    ? 'med'
                    : 'low';
              const b = basisFor(m, m.concept);
              return (
                <tr key={i} className={isReview ? 'ob-review-row' : ''}>
                  <td>{m.field}</td>
                  <td>
                    {m.concept}
                    {b && (
                      <span className={`ob-basis-badge basis-${b.basis}`} title={b.title}>
                        {b.label}
                      </span>
                    )}
                  </td>
                  <td>
                    {isReview ? (
                      <span className="conf review">! review</span>
                    ) : (
                      <span className={`conf ${conf}`}>{pct}%</span>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </>
    );
  } else if (step === 'resolve' && result.rules) {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const rules = result.rules as any[];
    body = (
      <>
        <h4>✦ Identity Rules</h4>
        {rules.length === 0 ? (
          <RulesEmptyState
            rejected={(result.rejected_rules as RejectedIdentityRule[]) || []}
            evidenced={((result.evidence as unknown[]) || []).length > 0}
            firstSystem={isFirstConnectedSystem(ob.active || '', catalogSystems())}
          />
        ) : (
          <table className="ob-table">
            <thead>
              <tr>
                <th>Concept</th>
                <th>Pattern</th>
                <th>Target</th>
              </tr>
            </thead>
            <tbody>
              {rules.map((r, i) => (
                <tr key={i}>
                  <td>{r.concept || ''}</td>
                  <td>
                    <code>
                      {r.source_pattern || '*'} → {r.target_pattern || '*'}
                    </code>
                  </td>
                  <td>{systemName(r.target_system)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {rules.length > 0 && (
          <WithheldRules rejected={(result.rejected_rules as RejectedIdentityRule[]) || []} />
        )}
        <ResolveExtras res={result} />
      </>
    );
  } else if (step === 'provision') {
    const systemId = String(result.system_id || ob.active || '');
    // The shared summary opens with "✓ Provisioned." itself (no h4 here).
    body = (
      <>
        <Md
          text={provisionSummaryMd(result, resolveContextBefore(flowStore.get().entries, entry.id))}
        />
        <ConnectionsDiscovered result={result} systemId={systemId || undefined} />
      </>
    );
  } else if (step === 'verify') {
    const { ok, text } = verifySummary(result);
    body = (
      <>
        <h4>✦ {ok ? 'Verification Passed' : 'Verification Issue'}</h4>
        <p>{text}</p>
        {typeof result.system_id === 'string' && result.system_id ? (
          <AgentTestLine runKey={`verify-${entry.id}`} systemId={result.system_id} />
        ) : null}
      </>
    );
  } else if (step === 'vocab_fit' && typeof result.fit_pct === 'number') {
    body = <VocabFitCheckpointBody result={result} />;
  } else if (step === 'concept_proposal' && result.proposals) {
    body = <ConceptProposalCard result={result as ConceptProposalResult} />;
  }
  if (!body) return null;

  return (
    <div className="ob-checkpoint">
      {body}
      <div style={{ marginTop: '0.75rem', display: 'flex', gap: '0.5rem' }}>
        <button
          className="ob-confirm-btn"
          disabled={entry.confirmed}
          onClick={() => {
            confirmChatCheckpoint(entry.id);
            welcomeSubmit('Confirmed. Proceed to the next step.');
          }}
        >
          Confirm {stepLabels[step] || step} →
        </button>
        <button
          className="ob-rerun-btn"
          onClick={() => welcomeSubmit('Re-run the ' + step + ' step.')}
        >
          ↻ Re-run
        </button>
      </div>
    </div>
  );
}

function DiscoveryCard({ entry }: { entry: Extract<FlowEntry, { kind: 'discovery' }> }) {
  return (
    <div className="ob-discovery-card">
      <div className="ob-discovery-header">New concepts discovered</div>
      <div className="ob-discovery-list">
        {entry.rows.map((r, i) => (
          <div
            key={i}
            className="ob-discovery-row"
            data-i={i}
            style={r.decided ? { opacity: r.decided === 'created' ? 0.5 : 0.4 } : undefined}
          >
            <div className="ob-discovery-info">
              <strong>{r.proposal.name || r.proposal.id}</strong>
              {r.proposal.description ? (
                <span className="ob-discovery-desc"> — {r.proposal.description}</span>
              ) : null}
            </div>
            <div className="ob-discovery-actions">
              <button
                className="ob-disc-create"
                data-i={i}
                disabled={!!r.decided}
                onClick={() => {
                  entry.onCreate(i);
                  decideDiscoveryRow(entry.id, i, 'created');
                }}
              >
                {r.decided === 'created' ? '✓ Added' : 'Create'}
              </button>
              <button
                className="ob-disc-skip"
                data-i={i}
                disabled={!!r.decided}
                onClick={() => decideDiscoveryRow(entry.id, i, 'skipped')}
              >
                {r.decided === 'skipped' ? 'Skipped' : 'Skip'}
              </button>
            </div>
          </div>
        ))}
      </div>
      <button className="ob-confirm-btn" id="ob-disc-done" onClick={() => entry.onDone()}>
        Continue →
      </button>
    </div>
  );
}

function reconcileConceptLabel(s: RemapSuggestion): string {
  const id = s.concept_id || '';
  return s.concept_name && s.concept_name !== id ? `${s.concept_name} (${id})` : id;
}

function reconcileCurrentTag(s: RemapSuggestion): string | null {
  if (!s.current_tags) return null;
  if (s.current_tags.length === 0) return 'Now: untagged';
  return (
    'Now: ' +
    s.current_tags
      .map((t) => (t.role ? `${t.concept_id} (${t.role})` : t.concept_id))
      .join(', ')
  );
}

function ReconcileBanner({
  banner,
}: {
  banner: { id: number; rows: ReconcileRowVm[]; total: number };
}) {
  const [pending, setPending] = useState<ReadonlySet<number>>(() => new Set());
  const startPending = (i: number) =>
    setPending((prev) => new Set(prev).add(i));
  const clearPending = (i: number) =>
    setPending((prev) => {
      const next = new Set(prev);
      next.delete(i);
      return next;
    });

  const accept = async (rowIdx: number) => {
    // POST this row only; the row is done only when the server reports its
    // field written. Each in-flight Accept tracks its OWN row, so finishing
    // one does not re-enable another whose request is still outstanding.
    const row = flowStore.get().banners.find((b) => b.id === banner.id)?.rows[rowIdx];
    const s = row?.suggestion;
    if (!s?.field_id) return;
    startPending(rowIdx);
    try {
      const result = await requestJson<RemapResponse>('/api/onboard/remap', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(
          buildRemapBody([
            {
              field_id: s.field_id,
              concept_id: s.concept_id,
              role: s.role,
              confidence: s.confidence,
            },
          ]),
        ),
      });
      if (result.error) {
        setReconcileRowError(
          banner.id,
          rowIdx,
          'Not tagged: the graph did not take the change. Try again.',
        );
      } else if (!(result.written || []).includes(s.field_id)) {
        setReconcileRowError(
          banner.id,
          rowIdx,
          'Not tagged: this field or concept is no longer in the graph.',
        );
      } else {
        decideReconcileRow(banner.id, rowIdx, 'accepted');
        try {
          await invalidateOnboardingRemap(getOnboardQueryClient());
        } catch {
          // A cache refresh failure does not undo a tag the server wrote.
        }
      }
    } catch (error) {
      setReconcileRowError(banner.id, rowIdx, 'Not tagged: ' + (error as Error).message);
    } finally {
      clearPending(rowIdx);
    }
  };

  return (
    <div className="ob-reconcile-banner">
      <div className="ob-reconcile-header">
        <span>
          {banner.total} field{banner.total > 1 ? 's' : ''} may match new concepts
        </span>
        <button
          className="ob-reconcile-close"
          aria-label="Close"
          onClick={() => removeReconcileBanner(banner.id)}
        >
          ✕
        </button>
      </div>
      <p className="ob-reconcile-help">
        Optional: fields in other systems that may belong to a concept this system added.
        Accept tags the field; Skip leaves it.
      </p>
      <div
        className="ob-reconcile-rows"
        role="list"
        aria-label="Suggested concept tags"
        tabIndex={0}
      >
        {banner.rows.map((r, i) => {
          const s = r.suggestion;
          const confidence = s.confidence ?? 0;
          const conf =
            confidence <= 1 ? Math.round(confidence * 100) : Math.round(confidence);
          const system = s.system_name || (s.system_id ? systemName(s.system_id) : '');
          const where = s.column ? (s.table ? `${s.table}.${s.column}` : s.column) : s.field_id;
          const current = reconcileCurrentTag(s);
          return (
            <div
              key={i}
              className="ob-reconcile-row"
              role="listitem"
              data-i={i}
              style={
                r.decided
                  ? { opacity: r.decided === 'accepted' ? 0.5 : 0.4 }
                  : undefined
              }
            >
              <div className="ob-reconcile-main">
                <div className="ob-reconcile-info">
                  {system ? <span className="ob-reconcile-system">{system}</span> : null}
                  <code title={s.field_id}>{where}</code>
                  <span className="ob-reconcile-arrow" aria-hidden="true">
                    →
                  </span>
                  <span className="ob-reconcile-concept">{reconcileConceptLabel(s)}</span>
                  <span className="ob-reconcile-role">{s.role || ''}</span>
                  <span
                    className={`conf ${conf >= 90 ? 'high' : conf >= 70 ? 'med' : 'low'}`}
                  >
                    {conf}%
                  </span>
                </div>
                {current ? <div className="ob-reconcile-current">{current}</div> : null}
                {s.reasoning ? (
                  <details className="ob-reconcile-why">
                    <summary>Why this suggestion</summary>
                    <p>{s.reasoning}</p>
                  </details>
                ) : null}
                {r.error ? (
                  <p className="ob-reconcile-error" role="alert">
                    {r.error}
                  </p>
                ) : null}
              </div>
              <div className="ob-reconcile-btns">
                <button
                  className="ob-rec-accept"
                  data-i={i}
                  disabled={!!r.decided || pending.has(i)}
                  onClick={() => void accept(i)}
                >
                  {r.decided === 'accepted' ? '✓ Tagged' : 'Accept'}
                </button>
                <button
                  className="ob-rec-skip"
                  data-i={i}
                  disabled={!!r.decided || pending.has(i)}
                  onClick={() => decideReconcileRow(banner.id, i, 'skipped')}
                >
                  {r.decided === 'skipped' ? 'Skipped' : 'Skip'}
                </button>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function EntryBody({ entry }: { entry: FlowEntry }) {
  switch (entry.kind) {
    case 'msg':
      return (
        <div className="ob-msg-wrap">
          <AgentBadge />
          <Md className="ob-msg" text={entry.md} />
        </div>
      );
    case 'user':
      return (
        <div className="ob-msg-wrap ob-msg-user">
          <UserBadge />
          <div className={entry.variant === 'bubble' ? 'ob-chat-bubble user' : 'ob-msg user'}>
            {entry.text}
          </div>
        </div>
      );
    case 'file':
      return (
        <div className="ob-msg-wrap ob-msg-user">
          <UserBadge />
          <div className="ob-chat-bubble user">
            <div className="ob-file-badge">
              <svg
                viewBox="0 0 20 20"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.5"
                width="14"
                height="14"
              >
                <path d="M15.5 10l-6.4 6.4a3.5 3.5 0 01-5-5L10.6 5a2.3 2.3 0 013.3 3.3L7.5 14.7a1.2 1.2 0 01-1.7-1.7L12.2 6.6" />
              </svg>{' '}
              <strong>{entry.name}</strong>{' '}
              <span style={{ color: 'var(--muted)', fontSize: '0.75rem' }}>
                {entry.sizeFmt}
              </span>
            </div>
          </div>
        </div>
      );
    case 'loading':
      return (
        <div className="ob-msg-wrap ob-loading">
          <AgentBadge />
          <div className="ob-msg">
            <span className="ob-loading-text">
              {entry.label}
              {entry.strong ? <strong>{entry.strong}</strong> : null}
            </span>
            <Dots />
          </div>
        </div>
      );
    case 'error':
      // Retryable step errors (obAppendRetryError) carry the Retry button;
      // plain errors keep the legacy '⚠ text' rendering untouched.
      return entry.retry ? (
        <div className="ob-msg error">
          <span>⚠ {entry.text} </span>
          <button className="ob-rerun-btn" title="Retry this step" onClick={entry.retry}>
            Retry
          </button>
        </div>
      ) : (
        <div className="ob-msg error">⚠ {entry.text}</div>
      );
    case 'stream':
      return (
        <div className="ob-msg-wrap">
          <AgentBadge />
          <div className="ob-msg">
            {entry.errorText ? (
              entry.errorText
            ) : entry.typing && !entry.md ? (
              <Dots />
            ) : (
              <Md text={entry.md} />
            )}
          </div>
        </div>
      );
    case 'toolPill':
      return (
        <div
          className={'tool-pill' + (entry.done ? ' done' : '')}
          id={'ob-tool-' + entry.toolId}
        >
          {entry.label}
        </div>
      );
    case 'collapsed':
      return (
        <details className="ob-collapsed-section">
          <summary className="ob-collapsed-header">✓ {entry.label}</summary>
          {entry.children.map((c) => (
            <Entry key={c.id} entry={c} />
          ))}
        </details>
      );
    case 'checkpoint':
      // Map-step fit banner (C5) sits above the Confirm Mappings block when
      // the store slice carries the additive vocab_fit stats.
      return entry.content.kind === 'mappingTable' && entry.content.vocab_fit ? (
        <>
          <VocabFitBanner fit={entry.content.vocab_fit} />
          <Checkpoint entry={entry} />
        </>
      ) : (
        <Checkpoint entry={entry} />
      );
    case 'discovery':
      return <DiscoveryCard entry={entry} />;
    case 'connections':
      return (
        <div className="ob-checkpoint">
          <ConnectionsDiscovered result={entry.result} systemId={entry.systemId} />
        </div>
      );
    case 'chatCheckpoint':
      return <ChatCheckpoint entry={entry} />;
    case 'credentialForm':
      return <CredentialForm entry={entry} />;
    case 'cta':
      return (
        <div className="ob-seed-chips" style={{ padding: '0.5rem 0' }}>
          <button className="ob-seed-chip" onClick={() => void obResetAndConnect()}>
            ＋ Onboard another system
          </button>
          <button
            className="ob-seed-chip"
            id="ob-ask-about-chip"
            onClick={() => obAskAbout(entry.sysName)}
          >
            Ask about {entry.sysName} connections
          </button>
        </div>
      );
    case 'probe':
      return (
        <PreMapProbe
          systemId={entry.systemId}
          tables={entry.tables}
          onContinue={entry.onContinue}
        />
      );
    case 'runtimeQuestions':
      return <RuntimeQuestions openSeq={entry.openSeq} />;
    default:
      return null;
  }
}

const Entry = memo(EntryBody);

// ── Inputs ────────────────────────────────────────────────────────────────────

function WelcomeInput({ chips }: { chips: string[] | null }) {
  const { t } = useI18n();
  const inputRef = useRef<HTMLInputElement | null>(null);

  const send = () => {
    const msg = inputRef.current?.value || '';
    if (inputRef.current) inputRef.current.value = '';
    welcomeSubmit(msg);
  };

  return (
    <div className="ob-welcome-input">
      {chips && chips.length > 0 && (
        <div className="ob-seed-chips">
          {chips?.map((t) => (
            <button key={t} className="ob-seed-chip" onClick={() => welcomeSubmit(t)}>
              {t}
            </button>
          ))}
        </div>
      )}
      <div className="ob-chat-input-row">
        <input
          className="ob-chat-input"
          id="ob-welcome-chat"
          placeholder={t('onboard.inputPlaceholder')}
          ref={inputRef}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault();
              send();
            }
          }}
        />
        {/* Between input and Send (steer 2026-07-22): the attach flow seeds
            REST onboarding from an OpenAPI spec file. */}
        <button
          className="ob-attach-btn"
          title="Attach a file — e.g. an OpenAPI spec to onboard a REST API"
          onClick={() => obAttachFile()}
        >
          <AttachIcon />
        </button>
        <button className="ob-chat-send btn-send" id="ob-welcome-send" onClick={send}>
          Send
        </button>
      </div>
    </div>
  );
}

function FlowInput() {
  const inputRef = useRef<HTMLInputElement | null>(null);

  const send = () => {
    const msg = inputRef.current?.value.trim();
    if (!msg || !inputRef.current) return;
    inputRef.current.value = '';
    if (ob.currentStep) void obSendChat(ob.currentStep, msg, null);
  };

  return (
    <div className="ob-flow-input">
      <div className="ob-chat-input-row">
        <input
          className="ob-chat-input"
          id="ob-flow-chat"
          placeholder="Ask the onboarding agent..."
          ref={inputRef}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault();
              send();
            }
          }}
        />
        <button className="ob-chat-send btn-send" id="ob-flow-send" onClick={send}>
          Send
        </button>
      </div>
    </div>
  );
}

// ── Root ──────────────────────────────────────────────────────────────────────

type FlowRef = React.RefObject<HTMLDivElement | null>;

function AppendScrollFollower({ flowRef }: { flowRef: FlowRef }) {
  const appendSeq = useStore(flowStore, selectFlowAppendSeq);
  useEffect(() => {
    const el = flowRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [appendSeq, flowRef]);
  return null;
}

function EntryList({ flowRef }: { flowRef: FlowRef }) {
  const entries = useStore(flowStore, selectFlowEntries);

  // Token/entry updates follow only when near the bottom (<120px, the old
  // obStreamChat token behavior).
  useEffect(() => {
    const el = flowRef.current;
    if (el && el.scrollHeight - el.scrollTop - el.clientHeight < 120) {
      el.scrollTop = el.scrollHeight;
    }
  }, [entries, flowRef]);

  return entries.map((entry) => <Entry key={entry.id} entry={entry} />);
}

function BannerList() {
  const banners = useStore(flowStore, selectFlowBanners);
  return banners.map((banner) => <ReconcileBanner key={banner.id} banner={banner} />);
}

function ProgressSlot() {
  const progress = useStore(flowStore, selectFlowProgress);
  return progress ? <ObProgress p={progress} /> : null;
}

function WelcomeComposer() {
  const chips = useStore(flowStore, selectFlowChips);
  return <WelcomeInput chips={chips} />;
}

function DetailScreen() {
  const detail = useStore(flowStore, (state) => state.detail);
  return detail ? <SystemDetail detail={detail} /> : null;
}

function FlowScreen({
  flowRef,
  showScrollBtn,
}: {
  flowRef: FlowRef;
  showScrollBtn: boolean;
}) {
  const { t } = useI18n();
  const mode = useStore(flowStore, selectFlowMode);
  if (mode === 'welcome') {
    return (
      <>
        <BannerList />
        <div className="ob-welcome">
          <div className="ob-msg-wrap">
            <AgentBadge />
            <div className="ob-msg">{t('onboard.welcome')}</div>
          </div>
          <button
            className="ob-scroll-bottom"
            id="ob-scroll-btn"
            style={{ display: showScrollBtn ? 'flex' : 'none' }}
            onClick={() => {
              const el = flowRef.current;
              if (el) el.scrollTop = el.scrollHeight;
            }}
          >
            ↓
          </button>
          <EntryList flowRef={flowRef} />
          <ProgressSlot />
          <WelcomeComposer />
        </div>
      </>
    );
  }

  // The stepper is `position: sticky; top: 0` inside the scroll container. It
  // MUST be the first child: a reconcile banner rendered above it (the
  // post-completion "N fields may match new concepts" check) would otherwise
  // shove the stepper down and become its new sticky edge, which reads as the
  // stepper coming unstuck the moment onboarding finishes.
  return (
    <>
      <ProgressSlot />
      <BannerList />
      <EntryList flowRef={flowRef} />
      <FlowInput />
    </>
  );
}

function ScreenBody({
  flowRef,
  showScrollBtn,
}: {
  flowRef: FlowRef;
  showScrollBtn: boolean;
}) {
  const screen = useStore(flowStore, selectFlowScreen);
  if (screen === 'detail') return <DetailScreen />;
  return <FlowScreen flowRef={flowRef} showScrollBtn={showScrollBtn} />;
}

export function ObFlow() {
  const conceptsQuery = useConceptsQuery();
  const flowRef = useRef<HTMLDivElement | null>(null);
  const [showScrollBtn, setShowScrollBtn] = useState(false);

  const onScroll = () => {
    const el = flowRef.current;
    if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
    setShowScrollBtn(!atBottom);
  };

  return (
    <div className="onboard-flow" id="onboard-flow" ref={flowRef} onScroll={onScroll}>
      {conceptsQuery.isError && conceptsQuery.data === undefined ? (
        <ErrorState
          message="Could not load onboarding concepts."
          onRetry={() => void conceptsQuery.refetch()}
        />
      ) : (
        <>
          <AppendScrollFollower flowRef={flowRef} />
          <ScreenBody flowRef={flowRef} showScrollBtn={showScrollBtn} />
        </>
      )}
    </div>
  );
}
