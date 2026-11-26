// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Digital Thread panel components — the JSX form of the reimagined
 * explorer markup builders (topology map with gap callouts,
 * takeaway-led coverage, pipeline trace). Every id/class/data-attribute
 * mirrors the legacy markup (thread.css contracts: topo-*, cov-*, impact-*,
 * path-card, res-step, learn-*, raw-sys-*, unified-*, trace-*). Interactive
 * elements take React onClick handlers instead of the previous inline
 * onclick / data-attribute event wiring; JSX auto-escaping replaces the
 * builders' esc() interpolation (zero-innerHTML policy — no HTML-string
 * sinks). */

import { Fragment, useState } from 'react';

import { ConfirmDialog } from '../ui/ConfirmDialog';

import type {
  AuthorityRuleDetail,
  ConceptCoverage,
  CorrelationDetail,
  CoverageRole,
  DecisionBoundaryDetail,
  GraphSummary,
  IdentityRule,
  ImpactAnalysis,
  JoinPathDetail,
  LearnProposal,
  PathsResponse,
  RawSystemCard,
  ResolutionStep,
  UnifiedSection as UnifiedSectionData,
  ValueMappingDetail,
  VocabularyStats,
} from '../../api/types/thread';
import { isInactiveStatus } from '../../lib/rule-status';
import { systemName } from '../../lib/system-names';
import type { IdentityBinding, StepUiState, ThreadSystem, TopoPair } from './html';
import {
  activeRules,
  conceptSystemCounts,
  groupIdentityRules,
  heatmapGroups,
  levelTitle,
  nameById,
  pairGaps,
  pairRuleCount,
  sysClass,
  systemDisplay,
  topoPositions,
} from './html';

// ── Inline stroke-style SVG icons (_tIcon) ───────────────────────────────────

export type TIconName = 'check' | 'alert' | 'x' | 'bulb' | 'link' | 'nodes';

const T_ICON_PATHS: Record<TIconName, React.ReactNode> = {
  check: <polyline points="20 6 9 17 4 12" />,
  alert: (
    <>
      <path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z" />
      <line x1="12" y1="9" x2="12" y2="13" />
      <line x1="12" y1="17" x2="12.01" y2="17" />
    </>
  ),
  x: (
    <>
      <line x1="18" y1="6" x2="6" y2="18" />
      <line x1="6" y1="6" x2="18" y2="18" />
    </>
  ),
  bulb: <path d="M9 18h6M10 22h4M12 2a7 7 0 00-4 12.7c.6.5 1 1.4 1 2.3h6c0-.9.4-1.8 1-2.3A7 7 0 0012 2z" />,
  link: (
    <>
      <path d="M10 13a5 5 0 007.54.54l3-3a5 5 0 00-7.07-7.07l-1.72 1.71" />
      <path d="M14 11a5 5 0 00-7.54-.54l-3 3a5 5 0 007.07 7.07l1.71-1.71" />
    </>
  ),
  nodes: (
    <>
      <circle cx="12" cy="5" r="2" />
      <circle cx="5" cy="19" r="2" />
      <circle cx="19" cy="19" r="2" />
      <line x1="12" y1="7" x2="5" y2="17" />
      <line x1="12" y1="7" x2="19" y2="17" />
    </>
  ),
};

export function TIcon({ name, size = 14 }: { name: TIconName; size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {T_ICON_PATHS[name]}
    </svg>
  );
}

function StatusIcon({ status }: { status: 'matched' | 'fuzzy' | 'failed' }) {
  const name: TIconName = status === 'matched' ? 'check' : status === 'fuzzy' ? 'alert' : 'x';
  return (
    <span className={'res-status-icon ' + status}>
      <TIcon name={name} size={12} />
    </span>
  );
}

/** Small horizontal confidence bar with percentage (_confBar). */
export function ConfBar({ conf }: { conf: number | null | undefined }) {
  if (conf == null) return null;
  const pct = Math.max(0, Math.min(100, Math.round(conf * 100)));
  const cls = pct >= 80 ? 'hi' : pct >= 50 ? 'med' : 'lo';
  return (
    <div className="conf-row">
      <div className="conf-track">
        <div className={'conf-fill ' + cls} style={{ width: pct + '%' }} />
      </div>
      <span className="conf-pct">{pct}%</span>
    </div>
  );
}

// ── Empty state (_threadEmptyHtml) ───────────────────────────────────────────

export interface ThreadEmptyProps {
  /** What this view WILL show once systems are connected. */
  willShow: string;
  /** The index host switches views; default navigates to /#onboard. */
  onNavigate?: () => void;
}

export function ThreadEmpty({ willShow, onNavigate }: ThreadEmptyProps) {
  return (
    <div className="thread-empty-inner">
      <div className="thread-empty-icon">
        <TIcon name="nodes" size={28} />
      </div>
      <div className="thread-empty-title">No systems connected yet</div>
      <p>{willShow}</p>
      <a
        className="thread-onboard-link"
        href="/#onboard"
        data-nav-onboard=""
        onClick={(e) => {
          if (onNavigate) {
            e.preventDefault();
            onNavigate();
          }
        }}
      >
        Onboard a system →
      </a>
    </div>
  );
}

// ── Topology: headline stats, pair chips, gap callout (_renderTopoHeadline) ──

export function TopoHeadline({
  systemCount,
  totalRules,
  linkedCount,
  possible,
}: {
  systemCount: number;
  totalRules: number;
  linkedCount: number;
  possible: number;
}) {
  return (
    <>
      <div className="topo-stat">
        <div className="topo-stat-num">{systemCount}</div>
        <div className="topo-stat-label">Systems</div>
      </div>
      <div className="topo-stat">
        <div className="topo-stat-num">{totalRules}</div>
        <div className="topo-stat-label">Translation rules</div>
      </div>
      <div className="topo-stat">
        <div className="topo-stat-num">
          {linkedCount}
          <span className="topo-stat-of"> of {possible}</span>
        </div>
        <div className="topo-stat-label">System pairs linked</div>
      </div>
    </>
  );
}

export function TopoPairChips({
  systems,
  pairs,
  selectedPair,
  onOpenPair,
}: {
  systems: ThreadSystem[];
  pairs: Record<string, TopoPair>;
  selectedPair: string | null;
  onOpenPair: (key: string) => void;
}) {
  return (
    <>
      {Object.keys(pairs).map((key) => {
        const p = pairs[key];
        if (!pairRuleCount(p)) return null; // linked only by switched-off rules
        return (
          <button
            key={key}
            type="button"
            className={'topo-pair-chip' + (selectedPair === key ? ' sel' : '')}
            data-pair={key}
            onClick={() => onOpenPair(key)}
          >
            {nameById(systems, p.a)} <span className="topo-chip-link">↔</span> {nameById(systems, p.b)}
            <span className="topo-chip-count">{pairRuleCount(p)}</span>
          </button>
        );
      })}
    </>
  );
}

/** Gap callout — returns null when every pair is linked (_renderTopoGaps). */
export function TopoGaps({ systems, pairs }: { systems: ThreadSystem[]; pairs: Record<string, TopoPair> }) {
  const gaps = pairGaps(systems, pairs);
  if (!gaps.length) return null;
  return (
    <>
      <span className="topo-gaps-label">
        <TIcon name="alert" size={13} /> Gaps
      </span>
      <span className="topo-gaps-text">
        No translation rules link {gaps.map((g) => g[0] + ' ↔ ' + g[1]).join(', ')}. Questions spanning these systems
        cannot be answered yet.
      </span>
    </>
  );
}

// ── Topology: inline-SVG node-link map (_renderTopoSvg) ──────────────────────

export interface TopoSvgProps {
  systems: ThreadSystem[];
  pairs: Record<string, TopoPair>;
  selectedPair: string | null;
  selectedSys: string | null;
  onOpenPair: (key: string) => void;
  onOpenNode: (sysId: string) => void;
}

export function TopoSvg({ systems, pairs, selectedPair, selectedSys, onOpenPair, onOpenNode }: TopoSvgProps) {
  const W = 860,
    H = systems.length <= 2 ? 280 : 440;
  const pos = topoPositions(systems.length, W, H);
  const posOf: Record<string, { x: number; y: number }> = {};
  systems.forEach((s, i) => {
    posOf[String(s.id || '')] = pos[i];
  });
  return (
    <svg viewBox={'0 0 ' + W + ' ' + H} preserveAspectRatio="xMidYMid meet" role="img" aria-label="System topology map">
      {Object.entries(pairs).map(([key, p]) => {
        const A = posOf[p.a],
          B = posOf[p.b];
        if (!A || !B) return null;
        const count = pairRuleCount(p);
        if (!count) return null; // linked only by switched-off rules: no edge
        const w = Math.min(1.5 + count * 0.7, 7).toFixed(1);
        const op = (0.35 + 0.6 * p.maxConf).toFixed(2);
        const cls = p.maxConf >= 0.8 ? 'hi' : p.maxConf >= 0.5 ? 'med' : 'lo';
        const mx = (A.x + B.x) / 2,
          my = (A.y + B.y) / 2;
        const sel = selectedPair === key ? ' sel' : '';
        return (
          <Fragment key={key}>
            <line
              className={'topo-edge ' + cls + sel}
              data-pair={key}
              x1={A.x}
              y1={A.y}
              x2={B.x}
              y2={B.y}
              strokeWidth={w}
              opacity={op}
            />
            <line
              className="topo-edge-hit"
              data-pair={key}
              x1={A.x}
              y1={A.y}
              x2={B.x}
              y2={B.y}
              strokeWidth={18}
              onClick={() => onOpenPair(key)}
            />
            <g className={'topo-badge' + sel} data-pair={key} onClick={() => onOpenPair(key)}>
              <circle cx={mx} cy={my} r={11} />
              <text x={mx} y={my + 3.5}>
                {count}
              </text>
            </g>
          </Fragment>
        );
      })}
      {systems.map((s, i) => {
        const P = pos[i];
        const id = String(s.id || '');
        const label = systemDisplay(s);
        const short = label.length > 17 ? label.slice(0, 16) + '…' : label;
        return (
          <g
            key={id || i}
            className={'topo-node sys-' + sysClass(s.name) + (selectedSys === id ? ' sel' : '')}
            data-sys={id}
            onClick={() => onOpenNode(id)}
          >
            <title>{label}</title>
            <rect x={P.x - 68} y={P.y - 21} width={136} height={42} rx={10} />
            <text x={P.x} y={P.y + 4}>
              {short}
            </text>
          </g>
        );
      })}
    </svg>
  );
}

// ── Topology: detail drawer (pair or node) ───────────────────────────────────

export function DrawerHead({
  title,
  sub,
  onClose,
}: {
  title: React.ReactNode;
  sub: string;
  onClose: () => void;
}) {
  return (
    <div className="drawer-head">
      <div>
        <div className="drawer-title">{title}</div>
        <div className="drawer-sub">{sub}</div>
      </div>
      <button type="button" className="impact-close-btn" onClick={onClose} aria-label="Close detail panel">
        ✕
      </button>
    </div>
  );
}

export function RuleCardsSection<T>({
  title,
  items,
  renderCard,
}: {
  title: string;
  items: T[];
  renderCard: (item: T, i: number) => React.ReactNode;
}) {
  if (!items || !items.length) return null;
  return (
    <div className="rule-section">
      <div className="rule-section-title">
        {title} <span className="thread-count">{items.length}</span>
      </div>
      <div className="rule-cards">{items.map((item, i) => renderCard(item, i))}</div>
    </div>
  );
}

// ── Identity rules: concept-grouped binding cards (shared by BOTH drawers) ───

function bindingLabel(e: { system: string; field: string }): string {
  return e.field ? `${e.system}.${e.field}` : e.system;
}

/** Human phrase for a binding's match patterns (never a raw '*'). */
function patternPhrase(b: IdentityBinding): string | null {
  const r = b.rules.find((x) => (x.source_pattern && x.source_pattern !== '*') || (x.target_pattern && x.target_pattern !== '*'));
  if (!r) return null;
  const s = r.source_pattern && r.source_pattern !== '*' ? r.source_pattern : 'any value';
  const t = r.target_pattern && r.target_pattern !== '*' ? r.target_pattern : 'any value';
  return `${s} → ${t}`;
}

export function BindingConfidence({ conf }: { conf: number | null }) {
  if (typeof conf !== 'number') return null;
  const pct = Math.max(0, Math.min(100, Math.round(conf * 100)));
  return (
    <div className="conf-row identity-confidence">
      <div className="conf-track" aria-hidden="true">
        <div className={'conf-fill ' + (pct >= 80 ? 'hi' : 'med')} style={{ width: `${pct}%` }} />
      </div>
      <span className="conf-pct">{pct}%</span>
    </div>
  );
}

/** Human label for a rule's origin (method + provenance pipeline). */
function originLabel(r: IdentityRule): string {
  const method = String(r.method || '');
  if (method === 'onboarding-llm') return 'AI at onboarding — user-approved at the Identity Rules checkpoint';
  if (method === 'provision-request') {
    return 'Sent in a provision request, not reviewed at the Identity Rules checkpoint';
  }
  if (method === 'user_corrected') return 'User correction (chat)';
  if (method === 'user_proposed') return 'User proposal, accepted by a steward';
  if (method === 'user_confirmed') {
    const prov = String(r.provenance || '');
    if (prov === 'orchestrator') return 'Learned during a query — user confirmed';
    if (prov === 'analysis-llm') return 'Agent analysis — user confirmed';
    if (prov === 'agent') return 'Agent proposal — user confirmed';
    return 'Learned — user confirmed';
  }
  return method || 'Recorded before provenance tracking';
}

function shortWhen(iso: unknown): string {
  const s = String(iso || '');
  return s ? s.replace('T', ' ').slice(0, 16) + ' UTC' : '';
}

/** Rule lifecycle callback: deactivate/reactivate ALL rule ids in a binding. */
export type RuleLifecycleFn = (ruleIds: string[], action: 'deactivate' | 'reactivate') => void;

/** One expandable binding card: the field-level claim a human can judge. */
export function IdentityBindingCard({
  binding,
  onLifecycle,
}: {
  binding: IdentityBinding;
  onLifecycle?: RuleLifecycleFn;
}) {
  const arrow = binding.bidirectional ? '↔' : '→';
  const hasFields = Boolean(binding.a.field || binding.b.field);
  const pattern = patternPhrase(binding);
  const claim = hasFields
    ? `${bindingLabel(binding.a)} ${arrow} ${bindingLabel(binding.b)}`
    : pattern || `All values · ${systemName(binding.a.system)} ${arrow} ${systemName(binding.b.system)}`;
  const first = binding.rules[0] || {};
  const deactivated = isInactiveStatus(first.status);
  const status = String(first.status || '');
  const transform = String(first.transform || '');
  const ruleIds = binding.rules.map((r) => String(r.id || '')).filter(Boolean);
  const [confirming, setConfirming] = useState(false);
  const detail = (label: string, value: React.ReactNode) => (
    <div className="identity-detail-row">
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
  return (
    <details className={'rule-card identity-rule-card' + (deactivated ? ' deactivated' : '')}>
      <summary>
        <span className="rule-pattern identity-rule-phrase">{claim}</span>
        <span className="rule-syspair">
          {systemName(binding.a.system)} {arrow} {systemName(binding.b.system)}
          {binding.rules.length > 1 && <span className="identity-dup-badge">{binding.rules.length} rules</span>}
        </span>
        <BindingConfidence conf={binding.maxConfidence} />
        <span className="identity-expand-hint">Full rule details</span>
      </summary>
      <dl className="identity-rule-details">
        {detail('Systems', `${systemName(binding.a.system)} ${arrow} ${systemName(binding.b.system)}`)}
        {detail('Concept', binding.concept)}
        {hasFields && detail('Match fields', `${binding.a.field || '—'} ${arrow} ${binding.b.field || '—'}`)}
        {detail('Value match', pattern || 'Any value (*) — identifiers pass through unchanged')}
        {transform && detail('Transform', transform)}
        {detail('Origin', originLabel(first))}
        {Boolean(first.approved_by) && detail('Approved by', String(first.approved_by))}
        {Boolean(first.created_at) && detail('Created', shortWhen(first.created_at))}
        {Boolean(first.origin_context) && detail('Context', String(first.origin_context))}
        {deactivated &&
          detail(
            'Deactivated',
            `${String(first.deactivated_by || 'unknown')} · ${shortWhen(first.deactivated_at) || 'unknown time'}`,
          )}
        {!deactivated && status && detail('Status', status)}
        {typeof binding.maxConfidence === 'number' && detail('Recorded confidence', `${Math.round(binding.maxConfidence * 100)}%`)}
        {detail(
          binding.rules.length > 1 ? `Rules (${binding.rules.length})` : 'Rule ID',
          binding.rules.length > 1 ? (
            <ul className="identity-rule-list">
              {binding.rules.map((r, i) => (
                <li key={i}>
                  <code>{String(r.id || '(no id)')}</code> {String(r.source_system || '')}.{String(r.source_field || '*')} →{' '}
                  {String(r.target_system || '')}.{String(r.target_field || '*')}
                  {typeof r.confidence === 'number' && ` · ${Math.round(r.confidence * 100)}%`}
                </li>
              ))}
            </ul>
          ) : (
            String(first.id || '(no id)')
          ),
        )}
        {onLifecycle && ruleIds.length > 0 && (
          <div className="identity-lifecycle-row">
            <button
              type="button"
              className={'identity-lifecycle-btn ' + (deactivated ? 'reactivate' : 'deactivate')}
              onClick={() =>
                deactivated ? onLifecycle(ruleIds, 'reactivate') : setConfirming(true)
              }
            >
              {deactivated
                ? 'Reactivate'
                : `Deactivate${ruleIds.length > 1 ? ` ${ruleIds.length} rules` : ''}`}
            </button>
            <ConfirmDialog
              open={confirming}
              title={
                ruleIds.length > 1
                  ? `Deactivate ${ruleIds.length} identity rules?`
                  : 'Deactivate this identity rule?'
              }
              confirmLabel={ruleIds.length > 1 ? `Deactivate ${ruleIds.length} rules` : 'Deactivate rule'}
              tone="danger"
              onCancel={() => setConfirming(false)}
              onConfirm={() => {
                setConfirming(false);
                onLifecycle(ruleIds, 'deactivate');
              }}
            >
              Cross-system questions will stop using{' '}
              <strong>{claim}</strong> to join{' '}
              <strong>{systemName(binding.a.system)}</strong> and <strong>{systemName(binding.b.system)}</strong> —
              answers that depended on this link may come back incomplete or
              split per system. The rule{ruleIds.length > 1 ? 's stay' : ' stays'} visible
              with an audit stamp and can be reactivated here at any time.
            </ConfirmDialog>
          </div>
        )}
      </dl>
    </details>
  );
}

function ConceptGroups({
  rules,
  onLifecycle,
}: {
  rules: IdentityRule[];
  onLifecycle?: RuleLifecycleFn;
}) {
  return (
    <>
      {groupIdentityRules(rules).map((g) => (
        <div className="identity-concept-group" key={g.concept}>
          <div className="identity-concept-heading">
            <span className="rule-concept">{g.concept}</span>
            <span className="identity-concept-count">
              {g.bindings.length} {g.bindings.length === 1 ? 'binding' : 'bindings'}
              {g.ruleCount > g.bindings.length ? ` · ${g.ruleCount} rules` : ''}
            </span>
          </div>
          <div className="rule-cards">
            {g.bindings.map((b, i) => (
              <IdentityBindingCard key={i} binding={b} onLifecycle={onLifecycle} />
            ))}
          </div>
        </div>
      ))}
    </>
  );
}

/** Identity-rules drawer section: confidence legend + concept-grouped binding
 * cards. Collapses exact duplicates and A→B/B→A mirrors (groupIdentityRules);
 * the count badge is the number of ACTIVE rules (raw, before collapsing), the
 * same count the header meta and the API's identity_rules use. Deactivated
 * and retired rules stay visible (audit) in a collapsed bucket at the end,
 * with their own count. */
export function IdentityRulesSection({
  rules,
  onLifecycle,
}: {
  rules: IdentityRule[];
  onLifecycle?: RuleLifecycleFn;
}) {
  if (!rules || !rules.length) return null;
  const active = activeRules(rules);
  const deactivated = rules.filter((r) => isInactiveStatus(r.status));
  return (
    <div className="rule-section identity-rule-section">
      <div className="rule-section-heading">
        <div className="rule-section-title">
          Identity rules <span className="thread-count">{active.length}</span>
        </div>
        <div
          className="identity-confidence-legend"
          title="Confidence was recorded when this in-use knowledge-graph rule was learned. It is provenance, not an active probability of whether the rule will work."
        >
          <span className="confidence-key high" /> ≥80%
          <span className="confidence-key medium" /> &lt;80%
          <span className="identity-confidence-help" aria-label="About rule confidence">?</span>
        </div>
      </div>
      <ConceptGroups rules={active} onLifecycle={onLifecycle} />
      {deactivated.length > 0 && (
        <details className="identity-deactivated-bucket">
          <summary>
            Deactivated <span className="thread-count">{deactivated.length}</span> — kept for audit, excluded from
            query routing
          </summary>
          <ConceptGroups rules={deactivated} onLifecycle={onLifecycle} />
        </details>
      )}
    </div>
  );
}

export function JoinCard({ j }: { j: JoinPathDetail }) {
  const deactivated = isInactiveStatus(j.status);
  return (
    <div className={'rule-card join-card' + (deactivated ? ' deactivated' : '')}>
      <div className="rule-concept">Join</div>
      <div className="rule-pattern">
        <code>{j.from_field || ''}</code>
        <span className="rule-arrow">→</span>
        <code>{j.to_field || ''}</code>
      </div>
      <div className="rule-syspair">{systemName(j.from_system) + ' → ' + systemName(j.to_system)}</div>
      {(j.description || j.transform) && <div className="rule-desc">{j.description || j.transform}</div>}
      {deactivated && (
        <div className="rule-desc join-deactivated-note">
          Deactivated (the join path or its identity rule) · kept for audit, not used as a join
        </div>
      )}
    </div>
  );
}

/** Join-paths drawer section: active joins as cards, counted in the badge;
 * switched-off ones (the JoinPath, or the identity rule it came from, was
 * deactivated or retired) in a collapsed audit bucket with their own count,
 * as deactivated identity rules are. */
export function JoinPathsSection({ joins }: { joins: JoinPathDetail[] }) {
  if (!joins || !joins.length) return null;
  const active = activeRules(joins);
  const deactivated = joins.filter((j) => isInactiveStatus(j.status));
  return (
    <div className="rule-section">
      <div className="rule-section-title">
        Join paths <span className="thread-count">{active.length}</span>
      </div>
      <div className="rule-cards">
        {active.map((j, i) => (
          <JoinCard key={i} j={j} />
        ))}
      </div>
      {deactivated.length > 0 && (
        <details className="identity-deactivated-bucket join-deactivated-bucket">
          <summary>
            Deactivated <span className="thread-count">{deactivated.length}</span> — kept for audit, not used as a
            join
          </summary>
          <div className="rule-cards">
            {deactivated.map((j, i) => (
              <JoinCard key={i} j={j} />
            ))}
          </div>
        </details>
      )}
    </div>
  );
}

export function ValueCard({ v }: { v: ValueMappingDetail }) {
  return (
    <div className="rule-card">
      <div className="rule-concept">{v.concept || ''}</div>
      <div className="rule-pattern">
        <code>{v.from_value || ''}</code>
        <span className="rule-arrow">→</span>
        <code>{v.to_value || ''}</code>
      </div>
      <div className="rule-syspair">{systemName(v.from_system) + ' → ' + systemName(v.to_system)}</div>
      <ConfBar conf={v.confidence} />
    </div>
  );
}

/** Rule sections shared by the pair and node drawers (_ruleSection triplet). */
export function DrawerRuleSections({
  identity,
  joins,
  values,
  onRuleLifecycle,
}: {
  identity: IdentityRule[];
  joins: JoinPathDetail[];
  values: ValueMappingDetail[];
  onRuleLifecycle?: RuleLifecycleFn;
}) {
  return (
    <>
      <IdentityRulesSection rules={identity} onLifecycle={onRuleLifecycle} />
      <JoinPathsSection joins={joins} />
      <RuleCardsSection title="Value mappings" items={values} renderCard={(v, i) => <ValueCard key={i} v={v} />} />
    </>
  );
}

/** Pair drawer body (_openPairDrawer). */
export function PairDrawer({
  pair,
  systems,
  onClose,
  onRuleLifecycle,
}: {
  pair: TopoPair;
  systems: ThreadSystem[];
  onClose: () => void;
  onRuleLifecycle?: RuleLifecycleFn;
}) {
  return (
    <>
      <DrawerHead
        title={
          <>
            {nameById(systems, pair.a)} <span className="topo-chip-link">↔</span> {nameById(systems, pair.b)}
          </>
        }
        sub={pairRuleCount(pair) + ' rules link this pair'}
        onClose={onClose}
      />
      <DrawerRuleSections identity={pair.identity} joins={pair.joins} values={pair.values} onRuleLifecycle={onRuleLifecycle} />
    </>
  );
}

/** Node drawer body (_openNodeDrawer). */
export function NodeDrawer({
  system,
  pairs,
  onClose,
  onRuleLifecycle,
}: {
  system: ThreadSystem;
  pairs: Record<string, TopoPair>;
  onClose: () => void;
  onRuleLifecycle?: RuleLifecycleFn;
}) {
  const sysId = String(system.id || '');
  const mine = Object.values(pairs).filter((p) => p.a === sysId || p.b === sysId);
  const identity = mine.flatMap((p) => p.identity);
  const joins = mine.flatMap((p) => p.joins);
  const values = mine.flatMap((p) => p.values);
  const total = mine.reduce((sum, p) => sum + pairRuleCount(p), 0); // switched-off rules are not counted
  const inactive = identity.length + joins.length - activeRules(identity).length - activeRules(joins).length;
  const sub = [system.plant, system.schema].filter(Boolean).join(' · ');
  return (
    <>
      <DrawerHead
        title={systemDisplay(system)}
        sub={(sub ? sub + ' · ' : '') + 'participates in ' + total + ' rules'}
        onClose={onClose}
      />
      {!total && (
        <div className="drawer-note">
          {inactive
            ? 'No active translation rules involve this system. Its deactivated rules are listed below for audit.'
            : 'No translation rules involve this system yet.'}
        </div>
      )}
      <DrawerRuleSections identity={identity} joins={joins} values={values} onRuleLifecycle={onRuleLifecycle} />
    </>
  );
}

// ── Topology: graph-wide intelligence (_renderGraphwide) ─────────────────────

export function graphwideCounts(g: GraphSummary): { ar: number; co: number; db: number } {
  return {
    ar: (g.authority_rules_detail || []).length,
    co: (g.correlations_detail || []).length,
    db: (g.decision_boundaries_detail || []).length,
  };
}

export function GraphwideBadges({ g }: { g: GraphSummary }) {
  const { ar, co, db } = graphwideCounts(g);
  return (
    <>
      {ar > 0 && <span className="thread-count">Authority {ar}</span>}
      {co > 0 && <span className="thread-count">Correlations {co}</span>}
      {db > 0 && <span className="thread-count">Boundaries {db}</span>}
    </>
  );
}

/** Authority-rule card with the same lifecycle affordance as identity rules
 * (T1.4): admins soft-deactivate a wrong rule (kept visible with its audit
 * stamp) or reactivate it. Reuses the identity-lifecycle button styling.
 * Deactivation is CONSEQUENTIAL (answers about this concept stop preferring
 * the named system), so it asks for confirmation with the impact stated in
 * the dialog body — the button itself stays short.
 * Reactivation is the non-destructive undo and needs no confirm. */
export function AuthorityRuleCard({
  a,
  onLifecycle,
}: {
  a: AuthorityRuleDetail;
  onLifecycle?: RuleLifecycleFn;
}) {
  const [confirming, setConfirming] = useState(false);
  const deactivated = String(a.status || '') === 'deactivated';
  const id = String(a.id || '');
  const concept = String(a.concept || 'this concept');
  const system = systemName(a.system_id) || 'the named system';
  return (
    <div className={'rule-card authority-rule-card' + (deactivated ? ' deactivated' : '')}>
      <div className="rule-concept">{a.concept || ''}</div>
      <div className="rule-desc">{a.rule || ''}</div>
      {deactivated && (
        <div className="rule-desc authority-deactivated-note">
          Deactivated · {String(a.deactivated_by || 'unknown')} · {shortWhen(a.deactivated_at) || 'unknown time'}
        </div>
      )}
      {onLifecycle && id && (
        <div className="identity-lifecycle-row">
          <button
            type="button"
            className={'identity-lifecycle-btn ' + (deactivated ? 'reactivate' : 'deactivate')}
            onClick={() =>
              deactivated ? onLifecycle([id], 'reactivate') : setConfirming(true)
            }
          >
            {deactivated ? 'Reactivate' : 'Deactivate'}
          </button>
          <ConfirmDialog
            open={confirming}
            title={`Deactivate the ${concept} authority rule?`}
            confirmLabel="Deactivate rule"
            tone="danger"
            onCancel={() => setConfirming(false)}
            onConfirm={() => {
              setConfirming(false);
              onLifecycle([id], 'deactivate');
            }}
          >
            Answers about <strong>{concept}</strong> will stop preferring{' '}
            <strong>{system}</strong> as the source of truth — when several
            systems hold this value, the assistant chooses on its own instead.
            The rule stays visible with an audit stamp, and you can reactivate
            it here at any time.
          </ConfirmDialog>
        </div>
      )}
    </div>
  );
}

export function GraphwideBody({
  g,
  onRuleLifecycle,
}: {
  g: GraphSummary;
  onRuleLifecycle?: RuleLifecycleFn;
}) {
  return (
    <>
      <RuleCardsSection
        title="Authority rules"
        items={g.authority_rules_detail || []}
        renderCard={(a: AuthorityRuleDetail, i) => (
          <AuthorityRuleCard key={i} a={a} onLifecycle={onRuleLifecycle} />
        )}
      />
      <RuleCardsSection
        title="Learned correlations"
        items={g.correlations_detail || []}
        renderCard={(c: CorrelationDetail, i) => (
          <div key={i} className="rule-card">
            <div className="rule-pattern">{c.pattern || ''}</div>
            <ConfBar conf={c.confidence} />
          </div>
        )}
      />
      <RuleCardsSection
        title="Decision boundaries"
        items={g.decision_boundaries_detail || []}
        renderCard={(d: DecisionBoundaryDetail, i) => (
          <div key={i} className="rule-card">
            <div className="rule-concept">{d.concept || ''}</div>
            <div className="rule-pattern">
              threshold <code>{String(d.threshold)}</code>
              <span className="rule-arrow">→</span>
              {d.action || ''}
            </div>
          </div>
        )}
      />
    </>
  );
}

// ── Coverage: takeaway banner + source bar (_renderCoverageBanner) ───────────

export function CovBanner({ vocab, cov }: { vocab: VocabularyStats; cov: ConceptCoverage }) {
  const total = vocab.total_concepts || 0;
  const mapped = vocab.mapped_concepts || 0;
  const pct = Math.round(vocab.coverage_pct || 0);
  const unmapped = cov.unmapped_count != null ? cov.unmapped_count : Math.max(0, total - mapped);
  const counts = conceptSystemCounts(cov);
  const cross = Object.values(counts).filter((n) => n >= 2).length;
  return (
    <>
      <div className="cov-hero">
        <div className="cov-hero-num">{cross}</div>
        <div className="cov-hero-label">
          Cross-system concepts<span>mapped in ≥2 systems — these power cross-system answers</span>
        </div>
      </div>
      <div className="cov-banner-stats">
        <span>
          <b>{mapped}</b> of <b>{total}</b> concepts mapped ({pct}%)
        </span>
        <span className="cov-dotsep">·</span>
        <span>
          <b>{unmapped}</b> unmapped
        </span>
      </div>
    </>
  );
}

/** Secondary core / packs / emergent source bar (renderVocabularyStats). */
export function VocabPanel({ vocab: v }: { vocab: VocabularyStats }) {
  const total = v.total_concepts || 0;
  const sources = v.by_source || [];
  const coreCount = sources.find((s) => s.source === 'core')?.concepts || 0;
  const packCount = sources.filter((s) => (s.source || '').startsWith('pack:')).reduce((a, s) => a + (s.concepts || 0), 0);
  const emergentCount = sources
    .filter((s) => s.source && s.source !== 'core' && !(s.source || '').startsWith('pack:'))
    .reduce((a, s) => a + (s.concepts || 0), 0);
  const w = (n: number) => (total ? (n / total) * 100 : 0);
  return (
    <>
      <div className="vocab-source-bar">
        <div className="vocab-source-seg core" style={{ width: w(coreCount) + '%' }} />
        <div className="vocab-source-seg pack" style={{ width: w(packCount) + '%' }} />
        <div className="vocab-source-seg emergent" style={{ width: w(emergentCount) + '%' }} />
      </div>
      <div className="vocab-source-legend">
        <span className="core">Core {coreCount}</span>
        {packCount > 0 && <span className="pack">Packs {packCount}</span>}
        {emergentCount > 0 && <span className="emergent">Emergent {emergentCount}</span>}
        <span className="vocab-fields">{(v.total_fields || 0) + ' fields'}</span>
      </div>
    </>
  );
}

// ── Coverage: ISA-95 heatmap (renderCoverageHeatmap) ─────────────────────────

export interface CoverageHeatmapProps {
  data: ConceptCoverage;
  activeConceptId: string | null;
  /** Row-label / cell click → impact analysis (showImpact). */
  onSelectConcept: (conceptId: string) => void;
  /** Row/col hover highlight (the legacy .hl delegation) — React-held. */
  hover?: { row: number | null; col: number | null };
  onHover?: (hover: { row: number | null; col: number | null }) => void;
}

export function CoverageHeatmap({ data, activeConceptId, onSelectConcept, hover, onHover }: CoverageHeatmapProps) {
  const concepts = data.concepts || [];
  const systems = data.systems || [];
  if (!concepts.length || !systems.length) {
    return <div className="drawer-note">No concept mappings yet — they appear as systems are onboarded.</div>;
  }
  const lookup: Record<string, CoverageRole[]> = {};
  (data.coverage || []).forEach((c) => {
    lookup[c.concept + '::' + c.system] = c.roles || [];
  });
  const sysCount = conceptSystemCounts(data);
  const { ordered, groups } = heatmapGroups(concepts, sysCount);
  const hlRow = hover?.row ?? null;
  const hlCol = hover?.col ?? null;
  const rows: React.ReactNode[] = [];
  let row = 0;
  for (const lvl of ordered) {
    rows.push(
      <div key={'lvl-' + lvl} className="cov-level-head">
        {levelTitle(lvl === -1 ? '?' : lvl)}
      </div>,
    );
    for (const c of groups.get(lvl)!) {
      const r = row;
      const isActive = activeConceptId === c.id;
      const nSys = sysCount[c.id] || 0;
      rows.push(
        <div
          key={'label-' + c.id}
          className={
            'cov-row-label' + (isActive ? ' active' : '') + (nSys >= 2 ? ' cross' : '') + (hlRow === r ? ' hl' : '')
          }
          data-row={r}
          data-concept={c.id}
          title={c.name || c.id}
          onClick={() => onSelectConcept(c.id)}
          onMouseOver={() => onHover?.({ row: r, col: null })}
        >
          {c.name || c.id}
          {nSys >= 2 && <span className="cov-cross-tag">×{nSys}</span>}
        </div>,
      );
      systems.forEach((s, si) => {
        rows.push(
          <CovCell
            key={'cell-' + c.id + '-' + s.id}
            roles={lookup[c.id + '::' + s.id] || []}
            row={r}
            col={si}
            conceptId={c.id}
            sysName={String(s.name || s.id)}
            hl={hlRow === r || hlCol === si}
            onSelect={onSelectConcept}
            onHover={onHover}
          />,
        );
      });
      row++;
    }
  }
  return (
    <div
      className="cov-grid"
      style={{
        // Flexible system columns: fill the panel width when there is room
        // (minmax 1fr), never shrink below the 34px dot cell.
        gridTemplateColumns: 'minmax(190px,300px) repeat(' + systems.length + ',minmax(34px,1fr))',
      }}
      onMouseLeave={() => onHover?.({ row: null, col: null })}
    >
      <div className="cov-corner" />
      {systems.map((s, si) => {
        const name = String(s.name || s.id);
        const slug = name.length > 14 ? name.slice(0, 13) + '…' : name;
        return (
          <div
            key={s.id}
            className={'cov-syshead' + (hlCol === si ? ' hl' : '')}
            data-col={si}
            title={name}
            onMouseOver={() => onHover?.({ row: null, col: si })}
          >
            <span>{slug}</span>
          </div>
        );
      })}
      {rows}
    </div>
  );
}

function CovCell({
  roles,
  row,
  col,
  conceptId,
  sysName,
  hl,
  onSelect,
  onHover,
}: {
  roles: CoverageRole[];
  row: number;
  col: number;
  conceptId: string;
  sysName: string;
  hl: boolean;
  onSelect: (conceptId: string) => void;
  onHover?: (hover: { row: number | null; col: number | null }) => void;
}) {
  const shared = {
    'data-row': row,
    'data-col': col,
    'data-concept': conceptId,
    onClick: () => onSelect(conceptId),
    onMouseOver: () => onHover?.({ row, col }),
  };
  if (!roles.length) {
    return (
      // Reviewed (react-props-spreading): `shared` is the local constant above
      // (fixed data-attrs + handlers), not caller-supplied attributes.
      // nosemgrep
      <div className={'cov-cell empty' + (hl ? ' hl' : '')} title={sysName + ': no mapping'} {...shared /* nosemgrep */}>
        <span className="cov-dot none" />
      </div>
    );
  }
  const title = sysName + ': ' + roles.map((r) => r.role + ' ' + Math.round((r.confidence || 0) * 100) + '%').join(', ');
  return (
    // Reviewed (react-props-spreading): same local `shared` constant as above.
    // nosemgrep
    <div className={'cov-cell' + (hl ? ' hl' : '')} title={title} {...shared /* nosemgrep */}>
      {roles.slice(0, 4).map((r, i) => (
        <span key={i} className={'cov-dot ' + r.role} style={{ opacity: Number((0.4 + 0.6 * (r.confidence || 0)).toFixed(2)) }} />
      ))}
    </div>
  );
}

/** Role-dot legend — rendered by the ThreadPanel host ABOVE the scrolling
 * heatmap (with CoverageStateLegend) so it stays visible while scrolling. */
export function CovLegend() {
  const item = (cls: string, label: string) => (
    <div className="coverage-legend-item">
      <span className={'cov-dot ' + cls} />
      {label}
    </div>
  );
  return (
    <div className="coverage-legend">
      {item('identifier', 'Identifier')}
      {item('measure', 'Measure')}
      {item('correlate', 'Correlate')}
      {item('derive', 'Derive')}
      {item('none', 'No mapping')}
      <div className="coverage-legend-item cov-legend-note">dot opacity = confidence · choose a concept for impact</div>
    </div>
  );
}

// ── Impact analysis (showImpact / _renderImpact) ─────────────────────────────

function plural(n: number, w: string): string {
  return n + ' ' + w + (n === 1 ? '' : 's');
}

/** Takeaway sentence: "X lives in N systems, joins across M paths, …". */
export function ImpactConcept({ d }: { d: ImpactAnalysis }) {
  const hs = d.hop_summary || {};
  const direct = d.direct_systems || [];
  const jp = d.join_paths || [];
  const rc = d.reachable_concepts || [];
  const k = hs.hop_1_systems != null ? hs.hop_1_systems : direct.length;
  const r = hs.hop_3_concepts != null ? hs.hop_3_concepts : rc.length;
  return (
    <>
      <span className="impact-name">{d.concept.name}</span> lives in <b>{plural(k, 'system')}</b>, joins across{' '}
      <b>{plural(jp.length, 'path')}</b>, and reaches{' '}
      <b>
        {r} more concept{r === 1 ? '' : 's'}
      </b>
    </>
  );
}

export function ImpactSystems({ d }: { d: ImpactAnalysis }) {
  const direct = d.direct_systems || [];
  return (
    <>
      <div className="impact-col-title">Lives in</div>
      {direct.length ? (
        direct.map((s, i) => (
          <div key={s.system_id ?? i} className="impact-node system">
            <div className="node-name">{s.system_name}</div>
            <div className="node-detail">{s.fields.map((f) => (f.role || 'i')[0].toUpperCase() + ':' + f.name).join(', ')}</div>
          </div>
        ))
      ) : (
        <div className="drawer-note">No direct systems</div>
      )}
    </>
  );
}

export function ImpactPaths({ d }: { d: ImpactAnalysis }) {
  const jp = d.join_paths || [];
  return (
    <>
      <div className="impact-col-title">Joins via</div>
      {jp.length ? (
        jp.map((p, i) => (
          <div key={i} className="impact-node path">
            <div className="node-name">{p.description || systemName(p.from_sys) + ' → ' + systemName(p.to_sys)}</div>
            <div className="node-detail">{p.from_field + ' → ' + p.to_field}</div>
          </div>
        ))
      ) : (
        <div className="drawer-note">No join paths from these systems</div>
      )}
    </>
  );
}

export function ImpactReachable({ d }: { d: ImpactAnalysis }) {
  const rc = d.reachable_concepts || [];
  return (
    <>
      <div className="impact-col-title">Unlocks</div>
      {rc.length ? (
        rc.map((c, i) => (
          <div key={i} className="impact-node concept">
            <div className="node-name">{c.concept_name}</div>
            <div className="node-detail">{'via ' + systemName(c.via_system) + ' · ' + c.role}</div>
          </div>
        ))
      ) : (
        <div className="drawer-note">No additional concepts reachable</div>
      )}
    </>
  );
}

/** True when the impact response has correlation/boundary cards to show
 * (drives the #impact-correlations display toggle in ThreadPanel). */
export function hasImpactCorrelations(d: ImpactAnalysis): boolean {
  return (d.correlations || []).length > 0 || (d.boundaries || []).length > 0;
}

export function ImpactCorrelations({ d }: { d: ImpactAnalysis }) {
  return (
    <>
      {(d.correlations || []).map((c, i) => (
        <div key={'c' + i} className="impact-corr-card">
          <div className="corr-label">Correlation</div>
          {c.pattern}
          <div className="corr-conf">confidence: {Math.round((c.confidence || 0) * 100)}%</div>
        </div>
      ))}
      {(d.boundaries || []).map((b, i) => (
        <div key={'b' + i} className="impact-boundary-card">
          <div className="boundary-label">Decision boundary</div>
          {b.rule + ' — threshold: ' + b.threshold + ' → ' + b.action}
        </div>
      ))}
    </>
  );
}

/** Ranked resolution paths between two systems (loadPathsForSystems —
 * replaces the "Joins via" column when both endpoints are known). */
export function RankedPaths({ fromSys, toSys, d }: { fromSys: string; toSys: string; d: PathsResponse }) {
  const total = d.total_paths || 0;
  return (
    <>
      <div className="impact-col-title">
        {'Joins via — ' + total + ' resolution path' + (total === 1 ? '' : 's')}
      </div>
      {(d.direct_paths || []).map((p, i) => {
        const confPct = Math.round((p.confidence || 0) * 100);
        const confClass = confPct >= 80 ? 'high' : confPct >= 50 ? 'med' : 'low';
        return (
          <div key={'d' + i} className="path-card">
            <div className="path-rank">{'Direct · ' + p.concept}</div>
            <div className="path-route">
              <span className="path-hop">{systemName(fromSys)}</span>
              <span className="path-arrow">→</span>
              <code>{(p.source_pattern || '*') + ' → ' + (p.target_pattern || '*')}</code>
              <span className="path-arrow">→</span>
              <span className="path-hop">{systemName(toSys)}</span>
            </div>
            <div className="path-conf-bar">
              <div className={'path-conf-fill ' + confClass} style={{ width: confPct + '%' }} />
            </div>
          </div>
        );
      })}
      {(d.indirect_paths || []).map((p, i) => {
        const confPct = Math.round((p.aggregate_confidence || 0) * 100);
        const confClass = confPct >= 60 ? 'high' : confPct >= 30 ? 'med' : 'low';
        const hops = p.hops || [];
        return (
          <div key={'i' + i} className="path-card">
            <div className="path-rank">{'Indirect · ' + hops.length + ' hops'}</div>
            <div className="path-route">
              <span className="path-hop">{systemName(fromSys)}</span>
              {hops.map((h, j) => (
                <Fragment key={j}>
                  <span className="path-arrow">→</span>
                  <span className="path-hop">{systemName(h.via_system)}</span> <span className="path-via">({h.concept})</span>
                </Fragment>
              ))}
            </div>
            <div className="path-conf-bar">
              <div className={'path-conf-fill ' + confClass} style={{ width: confPct + '%' }} />
            </div>
          </div>
        );
      })}
    </>
  );
}

// ── Trace: intro, raw cards, resolution steps, unified entity ────────────────

function ChevronRight({ size = 14 }: { size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <polyline points="9 18 15 12 9 6" />
    </svg>
  );
}

/** Rich empty state before any selection (#trace-intro). */
export function TraceIntro() {
  return (
    <div className="trace-intro-inner">
      <div className="trace-intro-title">One entity, every system, one answer</div>
      <p>
        Pick an entity above and watch it resolve across your connected systems: the same real-world thing usually
        carries a <em>different identifier in every system</em>. A trace shows the raw records side by side, the
        translation rules the knowledge graph applies to prove they are the same entity, and the unified cross-system
        view that results.
      </p>
      <div className="trace-intro-stages">
        <span>
          <b>1</b> Source records
        </span>
        <ChevronRight />
        <span>
          <b>2</b> Translation
        </span>
        <ChevronRight />
        <span>
          <b>3</b> Unified entity
        </span>
      </div>
    </div>
  );
}

export function RawCards({ raw }: { raw: RawSystemCard[] }) {
  return (
    <>
      {raw.map((s, i) => {
        const sysKey = sysClass(s.system);
        return (
          <div key={i} className="raw-sys-card" data-sys={sysKey}>
            <div className="raw-sys-header">
              <div className={'raw-sys-name ' + sysKey}>{systemName(s.system)}</div>
            </div>
            {s.fields.map((f, j) => (
              <div key={j} className="raw-field">
                <span className="label">{f.label}</span>
                <span className="value">{f.value}</span>
              </div>
            ))}
          </div>
        );
      })}
    </>
  );
}

export interface ResolutionStepsProps {
  resolution: ResolutionStep[];
  stepStates?: Record<number, StepUiState>;
  /** False when the signed-in user is known not to be a steward: saving a
   * rule (POST /api/thread/learn) is steward-only, so the Create Rule
   * button gives way to a note. Defaults to true (role unknown: the server
   * decides, and a 403 shows its message on the button). */
  canLearn?: boolean;
  onLearn: (proposal: LearnProposal, stepIdx: number) => void;
  onDismiss: (stepIdx: number) => void;
}

/** Whether to offer Create Rule: false only once the signed-in user is known
 * and is not a steward (admin). */
export function canLearnRules(auth: { user: { is_admin: boolean } | null; loading: boolean }): boolean {
  if (auth.loading || !auth.user) return true;
  return !!auth.user.is_admin;
}

export function ResolutionSteps({ resolution, stepStates = {}, canLearn = true, onLearn, onDismiss }: ResolutionStepsProps) {
  return (
    <>
      {resolution.map((r, i) => {
        const ui = stepStates[i] || {};
        const statusClass: 'matched' | 'fuzzy' | 'failed' =
          r.status === 'fuzzy' ? 'fuzzy' : r.status === 'failed' ? 'failed' : 'matched';
        const isBrute = (r.rule || '').toLowerCase().includes('brute');
        const p = r.proposal;
        return (
          <div
            key={i}
            className={
              'res-step' +
              (i === 0 ? ' active' : '') +
              (statusClass === 'matched' ? ' done' : '') +
              (ui.learned ? ' learned' : '')
            }
          >
            <div className="res-step-num">{i + 1}</div>
            <div className="res-step-body">
              <div className="res-rule-name">
                {r.rule}
                {/* A learned step drops its brute-force badge (learnRule removed it). */}
                {isBrute && !ui.learned && <span className="res-brute-badge">brute-force</span>}
              </div>
              <div className="res-transform">{r.transform}</div>
              <div className={'res-step-status ' + statusClass}>
                <StatusIcon status={statusClass} /> {r.check}
              </div>
              {p && !ui.dismissed && (
                <div className="learn-proposal" id={'proposal-' + i}>
                  <div className="learn-title">
                    <TIcon name="bulb" size={13} /> Learn this pattern?
                  </div>
                  <div className="learn-pattern">
                    <code>{p.source_pattern}</code>
                    <span className="learn-arrow">↔</span>
                    <code>{p.target_pattern}</code>
                  </div>
                  <div className="learn-conf">
                    Confidence: <span className={'conf ' + (p.confidence >= 0.8 ? 'high' : 'med')}>{Math.round(p.confidence * 100)}%</span>
                  </div>
                  <div className="learn-actions">
                    {canLearn ? (
                      <button
                        type="button"
                        className="learn-btn-create"
                        data-step-idx={i}
                        disabled={!!ui.learning}
                        onClick={() => onLearn(p, i)}
                      >
                        {ui.learning ? 'Creating...' : ui.failed ? 'Failed: ' + ui.failed : 'Create Rule'}
                      </button>
                    ) : (
                      <span className="learn-steward-note">Only a steward (admin) can save this pattern as a rule.</span>
                    )}
                    <button type="button" className="learn-btn-dismiss" data-step-idx={i} onClick={() => onDismiss(i)}>
                      Dismiss
                    </button>
                  </div>
                </div>
              )}
            </div>
          </div>
        );
      })}
    </>
  );
}

/** Unified entity card — the payoff panel (_unifiedHtml). */
export function UnifiedCard({ unified, entityId }: { unified: UnifiedSectionData[]; entityId: string }) {
  return (
    <div className="unified-entity-card">
      <div className="unified-entity-id">{entityId}</div>
      {unified.map((s, i) => (
        <div key={i} className="unified-section">
          <div className="unified-section-title">{s.title}</div>
          {s.fields.map((f, j) => (
            <div key={j} className="unified-field">
              <span className="label">{f.label}</span>
              <span className="value">{f.value}</span>
            </div>
          ))}
        </div>
      ))}
    </div>
  );
}
