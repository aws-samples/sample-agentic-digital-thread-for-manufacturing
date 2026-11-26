// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* <Checkpoint/> (jsx-migration W5.1) — the deterministic-pipeline checkpoint
 * card: structured content (schema table / MCP summary / mapping table with
 * concept tags + picker / identity rules with resolve extras), the confirm
 * gate (.ob-confirm-btn disabled semantics are e2e-load-bearing), Re-run,
 * "Skip All Unmapped", and the per-step "Ask about this step" chat thread.
 * All row state lives in the flow store — payload derivation reads React
 * state, never the DOM. */
import { useEffect, useRef, useState } from 'react';

import { Md } from '../../../shared/ui/Md';
import type {
  CatalogSystem,
  IdentityRule,
  RejectedIdentityRule,
  ResolveEvidence,
  TransformCheck,
} from '../../../api/types/onboard';
import { systemName, useSystemNamesVersion } from '../../../lib/system-names';
import { catalogSystems } from '../catalog';
import { obSendChat } from '../chat';
import {
  addRowConcept,
  confirmCheckpoint,
  rejectRule,
  removeRowTag,
  setRowPickerOpen,
  skipAllUnmapped,
  skipRow,
  toggleCheckpointChat,
  type CheckpointChatBubble,
  type CheckpointContent,
  type FlowEntry,
  type MappingRowVm,
} from '../flow-store';
import { patternOriginNote, ruleEvidenceText } from '../rule-evidence';
import { OB_CONCEPTS } from '../state';
import { PreflightBody } from './PreflightCard';
import { SchemaColumnsCell } from './SchemaColumnsCell';
import { BasisBadgeKey, ResolveExtras } from './ResolveExtras';

type CheckpointEntry = Extract<FlowEntry, { kind: 'checkpoint' }>;

export function Dots() {
  return (
    <span className="ob-dots">
      <span>.</span>
      <span>.</span>
      <span>.</span>
    </span>
  );
}

/** Floating concept picker (was showConceptPicker) — search + click-outside
 * close; selecting adds a manual tag through the store. */
function ConceptPicker({
  used,
  onPick,
  onClose,
}: {
  used: string[];
  onPick: (concept: string) => void;
  onClose: () => void;
}) {
  const [q, setQ] = useState('');
  const ref = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    inputRef.current?.focus();
    const close = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose();
    };
    const t = setTimeout(() => document.addEventListener('click', close), 0);
    return () => {
      clearTimeout(t);
      document.removeEventListener('click', close);
    };
  }, [onClose]);

  const available = OB_CONCEPTS.filter((c) => c !== 'unknown' && !used.includes(c));
  return (
    <div className="concept-picker" ref={ref}>
      <input
        ref={inputRef}
        className="concept-search"
        placeholder="Search concepts..."
        value={q}
        onChange={(e) => setQ(e.target.value)}
      />
      <div className="concept-list">
        {available.map((c) => (
          <div
            key={c}
            className="concept-option"
            data-concept={c}
            style={{ display: c.includes(q.toLowerCase()) ? '' : 'none' }}
            onClick={() => onPick(c)}
          >
            {c}
          </div>
        ))}
      </div>
    </div>
  );
}

function tagConfLabel(t: { confidence: number; manual?: boolean }): string {
  if (t.manual) return 'manual';
  const pct =
    t.confidence <= 1 ? Math.round(t.confidence * 100) : Math.round(t.confidence);
  return pct + '%';
}

function MappingTable({ cpId, rows }: { cpId: number; rows: MappingRowVm[] }) {
  const pending = rows.filter((r) => r.tags.length === 0).length;
  if (rows.length === 0) {
    return (
      <div className="ob-mapping-status" id="ob-mapping-status" style={{ color: 'var(--muted)' }}>
        No fields were mapped, so there is nothing to confirm. Re-run the mapping.
      </div>
    );
  }
  return (
    <>
      <div
        className="ob-mapping-status"
        id="ob-mapping-status"
        style={
          pending > 0
            ? {
                fontSize: '0.8rem',
                padding: '0.5rem 0.75rem',
                marginBottom: '0.5rem',
                borderRadius: 6,
                background: 'rgba(210,153,34,0.1)',
                border: '1px solid rgba(210,153,34,0.3)',
                color: 'var(--amber)',
              }
            : { display: 'none' }
        }
      >
        {pending > 0 && (
          <>
            ⚠ {pending} field{pending > 1 ? 's' : ''} unmapped — assign a concept or{' '}
            <strong>skip</strong> fields that don&apos;t need one (e.g. surrogate keys)
          </>
        )}
      </div>
      <table className="ob-table">
        <thead>
          <tr>
            <th>Field</th>
            <th>Concept</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i} className={r.review ? 'ob-review-row' : ''} data-i={i}>
              <td>{r.field}</td>
              <td>
                <div className="concept-tags">
                  {r.tags.map((t, ti) =>
                    t.skip ? (
                      <span
                        key={ti}
                        className="concept-tag"
                        data-concept="skip"
                        style={{
                          opacity: 0.5,
                          borderColor: 'var(--muted)',
                          color: 'var(--muted)',
                        }}
                      >
                        skipped{' '}
                        <button
                          className="tag-remove"
                          onClick={() => removeRowTag(cpId, i, ti)}
                        >
                          ×
                        </button>
                      </span>
                    ) : (
                      <span key={ti} className="concept-tag" data-concept={t.concept}>
                        {t.concept} <span className="tag-conf">{tagConfLabel(t)}</span>
                        <BasisBadgeKey basis={t.basis} />
                        <button
                          className="tag-remove"
                          onClick={() => removeRowTag(cpId, i, ti)}
                        >
                          ×
                        </button>
                      </span>
                    ),
                  )}
                  <span style={{ position: 'relative' }}>
                    <button
                      className="add-concept-btn"
                      data-i={i}
                      onClick={() => setRowPickerOpen(cpId, i, !r.pickerOpen)}
                    >
                      + concept
                    </button>
                    {r.pickerOpen && (
                      <ConceptPicker
                        used={r.tags.map((t) => t.concept)}
                        onPick={(c) => addRowConcept(cpId, i, c)}
                        onClose={() => setRowPickerOpen(cpId, i, false)}
                      />
                    )}
                  </span>
                  {r.review && (
                    <button
                      className="add-concept-btn"
                      data-i={i}
                      style={{ color: 'var(--muted)' }}
                      onClick={() => skipRow(cpId, i)}
                    >
                      skip
                    </button>
                  )}
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}

function CheckpointBody({
  cpId,
  content,
  confirmed = false,
  systemId = '',
}: {
  cpId: number;
  content: CheckpointContent;
  confirmed?: boolean;
  /** The system being onboarded (tells "first system" from "no overlap"). */
  systemId?: string;
}) {
  // The rules table names each Target system by its catalog name.
  useSystemNamesVersion();
  if (content.kind === 'loading') {
    return (
      <div className="ob-loading">
        <span className="ob-loading-text">{content.label}</span>
        <Dots />
      </div>
    );
  }
  if (content.kind === 'error') {
    return <p style={{ color: 'var(--red)' }}>{content.text}</p>;
  }
  if (content.kind === 'mcpSummary') {
    return (
      <div
        style={{
          padding: '0.75rem',
          background: 'var(--surface2)',
          borderRadius: 6,
          fontSize: '0.85rem',
        }}
      >
        <div style={{ fontWeight: 600, marginBottom: '0.4rem' }}>{content.name}</div>
        <div style={{ color: 'var(--muted)' }}>
          {content.count} tag paths discovered from namespace
        </div>
        <div style={{ color: 'var(--muted)', fontSize: '0.75rem', marginTop: '0.25rem' }}>
          Tags will be mapped to manufacturing concepts in the next step
        </div>
      </div>
    );
  }
  if (content.kind === 'schemaTable') {
    return (
      <table className="ob-table">
        <thead>
          <tr>
            <th>Table</th>
            <th>Rows</th>
            <th>Columns</th>
          </tr>
        </thead>
        <tbody>
          {content.tables.map((t, i) => (
            <tr key={i}>
              <td>{t.name}</td>
              <td>{((t.row_count as number) || 0).toLocaleString()}</td>
              <td>
                <SchemaColumnsCell table={t} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    );
  }
  if (content.kind === 'mappingTable') {
    return <MappingTable cpId={cpId} rows={content.rows} />;
  }
  if (content.kind === 'preflight') {
    return <PreflightBody result={content.result} />;
  }
  // rules
  const rejected = content.extras?.rejected_rules || [];
  const evidenced = (content.extras?.evidence || []).length > 0;
  return (
    <>
      {content.rules.length === 0 ? (
        <RulesEmptyState
          rejected={rejected}
          evidenced={evidenced}
          firstSystem={isFirstConnectedSystem(systemId, catalogSystems())}
        />
      ) : (
        <table className="ob-table">
          <thead>
            <tr>
              <th>Concept</th>
              <th>Fields</th>
              <th>Pattern</th>
              <th>Target</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {content.rules.map((x, i) => (
              <tr key={i}>
                <td>{x.concept || ''}</td>
                <td style={{ fontSize: '0.78rem' }}>
                  <code>{x.source_field || ''}</code> ↔ <code>{x.target_field || ''}</code>
                  <RuleEvidenceLine
                    rule={x}
                    evidence={content.extras?.evidence}
                    checks={content.extras?.transform_checks}
                  />
                </td>
                <td>
                  <code>
                    {x.source_pattern || '*'} → {x.target_pattern || '*'}
                  </code>
                </td>
                <td>{systemName(x.target_system)}</td>
                <td style={{ textAlign: 'right' }}>
                  {!confirmed && (
                    <button
                      className="add-concept-btn ob-rule-reject-btn"
                      data-i={i}
                      style={{ color: 'var(--muted)' }}
                      title="Reject this rule — it will not be provisioned"
                      aria-label={`Reject identity rule ${x.concept || ''} → ${systemName(x.target_system)}`}
                      onClick={() => rejectRule(cpId, i)}
                    >
                      ✕ reject
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {content.rules.length > 0 && <WithheldRules rejected={rejected} />}
      <ResolveExtras res={content.extras} />
    </>
  );
}

/** What was measured for a proposed rule's field pair, as text under its
 * fields (rule-evidence.ts); no line when the evidence is ambiguous. A rule
 * whose pattern the Resolve gate set from the measured values gets a second
 * line that says so, in the same style. */
function RuleEvidenceLine({
  rule,
  evidence,
  checks,
}: {
  rule: IdentityRule;
  evidence: ResolveEvidence[] | undefined;
  checks?: TransformCheck[] | undefined;
}) {
  const lines = [ruleEvidenceText(rule, evidence, checks), patternOriginNote(rule)].filter(
    (line): line is string => line !== null,
  );
  if (lines.length === 0) return null;
  return (
    <>
      {lines.map((text) => (
        <div
          key={text}
          className="ob-rule-evidence"
          style={{ color: 'var(--muted)', fontSize: '0.74rem', marginTop: '0.15rem' }}
        >
          {text}
        </div>
      ))}
    </>
  );
}

/**
 * Empty identity-rules table, worded by WHY it is empty. "First system" is
 * only true when nothing else was measured against; when the resolve
 * evidence gates withheld every candidate (measures, dates, small integer
 * ranges read as identity) the attendee should see that judgement and its
 * reasons, not be told there was nothing to compare.
 */
/** True when the catalog is known and no system other than `systemId` is
 * connected yet, so an empty rules table really means "first system". An
 * unknown (empty) catalog is not a first system: the caller then says only
 * that nothing overlapped. */
export function isFirstConnectedSystem(
  systemId: string,
  systems: readonly Pick<CatalogSystem, 'id' | 'system_id' | 'status'>[],
): boolean {
  if (systems.length === 0) return false;
  return !systems.some(
    (s) =>
      (s.id || s.system_id) !== systemId &&
      (s.status === 'onboarded' || s.status === 'removal_failed'),
  );
}

export function RulesEmptyState({
  rejected,
  evidenced,
  firstSystem = false,
}: {
  rejected: RejectedIdentityRule[];
  evidenced: boolean;
  /** No other system is connected yet (see isFirstConnectedSystem). */
  firstSystem?: boolean;
}) {
  useSystemNamesVersion(); // withheld rules name the peer system
  const muted = { color: 'var(--muted)', fontSize: '0.82rem' } as const;
  if (rejected.length > 0) {
    return (
      <div data-testid="rules-empty-gated">
        <p style={muted}>
          No identity rule met the evidence bar — {rejected.length} candidate
          {rejected.length === 1 ? ' was' : 's were'} withheld:
        </p>
        <RejectedRulesList rejected={rejected} />
      </div>
    );
  }
  if (evidenced) {
    return (
      <p style={muted} data-testid="rules-empty-unevidenced">
        No cross-system identity rules were evidenced for this system — the
        sampled values did not line up with any connected system's identifiers.
      </p>
    );
  }
  if (firstSystem) {
    return (
      <p style={muted} data-testid="rules-empty-first">
        No cross-system identity rules needed. This is the first system.
      </p>
    );
  }
  return (
    <p style={muted} data-testid="rules-empty-no-overlap">
      No identity rules proposed: no other system shares identifier values with this one.
    </p>
  );
}

/** Each withheld candidate: its field pair, the peer system and why. */
function RejectedRulesList({ rejected }: { rejected: RejectedIdentityRule[] }) {
  const muted = { color: 'var(--muted)', fontSize: '0.82rem' } as const;
  return (
    <ul className="ob-rejected-rules" style={{ ...muted, margin: '0 0 0.5rem 1rem' }}>
      {rejected.map((r, i) => (
        <li key={i}>
          <code>{r.source_field || ''}</code> ↔ <code>{r.target_field || ''}</code>
          {r.target_system ? <> ({systemName(r.target_system)})</> : null}
          {r.rejected_reason ? <> — {r.rejected_reason}</> : null}
        </li>
      ))}
    </ul>
  );
}

/**
 * The candidates the resolve gates withheld, listed under the rules that were
 * kept. Without it a checkpoint that kept any rule hid every withheld one and
 * its reason (a rule to another plant's system, a measure read as identity).
 */
export function WithheldRules({ rejected }: { rejected: RejectedIdentityRule[] }) {
  useSystemNamesVersion(); // withheld rules name the peer system
  if (rejected.length === 0) return null;
  const muted = { color: 'var(--muted)', fontSize: '0.82rem' } as const;
  return (
    <div data-testid="rules-withheld">
      <p style={muted}>
        {rejected.length} more candidate{rejected.length === 1 ? ' was' : 's were'} withheld:
      </p>
      <RejectedRulesList rejected={rejected} />
    </div>
  );
}

function CheckpointChat({
  entry,
}: {
  entry: CheckpointEntry;
}) {
  const inputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    if (entry.chatOpen) inputRef.current?.focus();
  }, [entry.chatOpen]);

  const send = () => {
    const msg = inputRef.current?.value.trim();
    if (!msg || !inputRef.current) return;
    inputRef.current.value = '';
    void obSendChat(entry.stepContext, msg, entry.id);
  };

  return (
    <div className="ob-chat-section" style={{ display: entry.chatOpen ? '' : 'none' }}>
      <div className="ob-chat-thread">
        {entry.chatBubbles.map((b: CheckpointChatBubble) =>
          b.role === 'user' ? (
            <div key={b.id} className="ob-chat-bubble user">
              {b.text}
            </div>
          ) : (
            <div key={b.id} className="ob-chat-bubble agent">
              {b.error ? b.error : b.typing && !b.text ? null : <Md text={b.text} />}
            </div>
          ),
        )}
      </div>
      <div className="ob-chat-input-row">
        <input
          ref={inputRef}
          className="ob-chat-input"
          placeholder="Ask the onboarding agent..."
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault();
              send();
            }
          }}
        />
        <button className="ob-chat-send btn-send" onClick={send}>
          Send
        </button>
      </div>
    </div>
  );
}

export function Checkpoint({ entry }: { entry: CheckpointEntry }) {
  const pendingTitle =
    entry.content.kind === 'mappingTable'
      ? (() => {
          if (entry.content.rows.length === 0) return 'No fields were mapped. Re-run the mapping.';
          const pending = entry.content.rows.filter((r) => r.tags.length === 0).length;
          return pending > 0 ? pending + ' field(s) need a concept or skip' : '';
        })()
      : '';

  return (
    <div className="ob-checkpoint">
      <h4>✦ {entry.title}</h4>
      <div className="ob-checkpoint-content">
        <CheckpointBody
          cpId={entry.id}
          content={entry.content}
          confirmed={entry.confirmed}
          systemId={entry.stepContext?.systemId}
        />
      </div>
      <button
        className="ob-confirm-btn"
        disabled={entry.confirmDisabled || entry.confirmed}
        title={pendingTitle}
        onClick={() => confirmCheckpoint(entry.id)}
      >
        {entry.confirmLabel}
      </button>
      {entry.onRerun && (
        <button
          className="ob-rerun-btn"
          disabled={entry.confirmed}
          onClick={() => entry.onRerun?.(entry.id)}
        >
          ↻ Re-run
        </button>
      )}
      {entry.showSkipAll && !entry.confirmed && (
        <button
          className="ob-rerun-btn"
          style={{ marginLeft: '0.5rem' }}
          onClick={() => skipAllUnmapped(entry.id)}
        >
          Skip All Unmapped
        </button>
      )}
      {!entry.confirmed && (
        <button
          className="ob-chat-toggle"
          onClick={() => toggleCheckpointChat(entry.id)}
        >
          {entry.chatOpen ? 'Hide chat' : 'Ask about this step'}
        </button>
      )}
      {!entry.confirmed && <CheckpointChat entry={entry} />}
    </div>
  );
}
