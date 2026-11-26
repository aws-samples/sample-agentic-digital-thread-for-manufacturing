// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Agent Analysis panel — the chat analysis surfaces, rendered
 * as REACT ELEMENTS (zero-innerHTML migration; was an HTML-string builder +
 * createElement assembly in analysis-panel.ts):
 * - the browser-side composition ("How this answer was assembled") with the
 *   neutral stacked distribution bar (buildDistributionSegments,
 *   lib/analysis-util.js);
 * - the LLM post-query review (agent_analysis metadata);
 * - the Self-Learning link (intelligence_summary metadata);
 * - assembled into ONE collapsible <details> panel (collapsed by default).
 *
 * Classes and inline styles match the established markup; every LLM-authored
 * field ({a.summary}, {w.reason}, {w.fix}, {seg.label}) renders as JSX text —
 * escaped by construction, no HTML-string interpolation. Navigation anchors
 * use `data-nav="self-learning"` + the delegated thread click handler
 * (CSP-safe; closeDataModal + switchView('architecture') + switchArchTab). */
import type { AgentAnalysis, IntelligenceSummary } from '../api/types/chat';
import { PLANNED_WITH_TIP, RULES_APPLIED_TIP } from '../lib/metric-copy';
import { useSystemNamesVersion } from '../lib/system-names';
import { buildDistributionSegments, type ToolSeqEntry } from './answer-segments';
import { systemLabelNow } from './tool-labels';
import './analysis-panel.css';

/** System calls carry the label the thread store baked at tool_result; a
 * catalog name that arrived since then would split one system into two
 * segments ("MES Ind" + the catalog name). Once the catalog knows a system
 * its label is derived again at render (chat/tool-labels.ts systemLabelNow). */
function currentLabels(seq: ToolSeqEntry[]): ToolSeqEntry[] {
  return seq.map((t) => (t.type === 'system' && t.tool ? { ...t, label: systemLabelNow(t) } : t));
}

export interface AnalysisCounts {
  toolCallCount: number;
  graphLookupCount: number;
  emptyGraphLookups: number;
}

/** The panel's render inputs, assembled by the thread store on `done`. */
export interface AgentAnalysisPanelData {
  toolCallSequence: ToolSeqEntry[];
  summary: IntelligenceSummary | null;
  agentAnalysis: AgentAnalysis | null;
  /** Browser-side composition sub-section renders (any tool activity). */
  hasBrowser: boolean;
  totalTools: number;
  graphLookups: number;
  noDataSteps: number;
}

/** Assemble the panel data on `done` (the old buildAgentAnalysisPanel gate:
 * null when there is nothing to show). */
export function buildAgentAnalysisPanelData(
  toolCallSequence: ToolSeqEntry[],
  counts: AnalysisCounts,
  summary: IntelligenceSummary | null,
  agentAnalysis: AgentAnalysis | null,
): AgentAnalysisPanelData | null {
  const sum = summary || {};
  const totalTools = sum.total_tool_calls || counts.toolCallCount || 0;
  const graphLookups = counts.graphLookupCount || sum.neptune_queries || 0;
  const noDataSteps = counts.emptyGraphLookups || 0;
  const hasBrowser = toolCallSequence.length > 0 || totalTools > 0;
  const hasIntelligence = !!(summary && (summary.new_proposed_mappings || 0) > 0);
  if (!hasBrowser && !agentAnalysis && !hasIntelligence) return null;
  return {
    toolCallSequence,
    summary,
    agentAnalysis,
    hasBrowser,
    totalTools,
    graphLookups,
    noDataSteps,
  };
}

/** Tooltip shared with the Query Intelligence trace surface (arch-store) —
 * the sanctioned rules-applied vocabulary ("used in this answer" — fired,
 * not merely loaded; single source: lib/metric-copy.ts, T1.6). */
const RULES_TIP = RULES_APPLIED_TIP;

/** Browser-side composition sub-section (no heading — the panel summary is
 * the single heading). Neutral composition, not a good/bad score. */
function BrowserAnalysis({ data }: { data: AgentAnalysisPanelData }) {
  // Re-render when catalog names arrive (the legend names systems).
  useSystemNamesVersion();
  const { toolCallSequence, totalTools, graphLookups, noDataSteps } = data;
  // Server-computed intelligence counts (deploy-skew guarded by presence: an
  // old payload without rules_applied renders without the line rather than
  // claiming "0"). The legacy client-side "graph-guided" classifier is gone —
  // it matched bare tool names (explore_graph / get_system_schema) that the
  // gateway prefixes (e.g. digital-thread___explore_graph), so it always
  // rendered "✓ 0 graph-guided"; the rules-applied metric rework (see the
  // Query Intelligence panel) replaced that judgment framing.
  const rulesApplied = Array.isArray(data.summary?.rules_applied)
    ? data.summary!.rules_applied!.length
    : null;
  const systemQueries =
    typeof data.summary?.system_queries === 'number' ? data.summary.system_queries : null;
  const correlationWritebacks =
    typeof data.summary?.correlation_writebacks === 'number'
      ? data.summary.correlation_writebacks
      : null;
  // Additive determinism score — presence-gated (deploy-skew guard for
  // payloads from before the score existed) and typeof-guarded so a hostile non-numeric payload
  // value never renders.
  const groundedSteps =
    typeof data.summary?.grounded_steps === 'number' ? data.summary.grounded_steps : null;
  const deterministicPct =
    typeof data.summary?.deterministic_pct === 'number' ? data.summary.deterministic_pct : null;
  const segs = buildDistributionSegments(currentLabels(toolCallSequence), graphLookups);
  return (
    <div className="agent-analysis-box analysis-box">
      {segs.length > 0 && (
        <>
          {/* Caption — cross-system tool use is expected and good. */}
          <div
            title="The share of tool calls across the knowledge graph and each system queried. Effective cross-system tool use is how the assistant grounds an answer."
            className="analysis-caption"
          >
            How this answer was assembled
          </div>
          {/* Single horizontal stacked distribution bar. */}
          <div className="analysis-bar">
            {segs.map((seg, i) => (
              <div
                key={i}
                title={`${seg.label}: ${seg.count} tool call${seg.count === 1 ? '' : 's'} (${seg.pct}%)`}
                style={{ width: `${seg.pct}%`, background: seg.color }}
              />
            ))}
          </div>
          {/* Legend — source name + count per segment. */}
          <div className="analysis-legend">
            {segs.map((seg, i) => (
              <span key={i} className="analysis-legend-item">
                {/* Swatch colour is data (the segment's system colour). */}
                <span className="analysis-legend-swatch" style={{ background: seg.color }} />
                {seg.label} · {seg.count}
              </span>
            ))}
          </div>
        </>
      )}
      {/* Neutral counts caption — plain stats, no pass/fail coloring. */}
      <div className="analysis-stats">
        <span title="Total tool calls the assistant made to answer this question." className="analysis-help">
          📊 <strong>{totalTools}</strong> tool call{totalTools === 1 ? '' : 's'}
        </span>
        <span
          title="Direct lookups against the knowledge graph (concepts, join paths, value mappings)."
          className="analysis-help analysis-stat-graph"
        >
          🔮 {graphLookups} graph lookup{graphLookups === 1 ? '' : 's'}
        </span>
        {systemQueries !== null && (
          <span title="Direct queries to the connected systems." className="analysis-help">
            🗄 {systemQueries} system quer{systemQueries === 1 ? 'y' : 'ies'}
          </span>
        )}
        {rulesApplied !== null && (
          <span title={RULES_TIP} className="analysis-help">
            {rulesApplied} learned rule{rulesApplied === 1 ? '' : 's'} applied
          </span>
        )}
        {groundedSteps !== null && deterministicPct !== null && (
          <span
            title="Tool steps whose results mechanically ground the answer (computed, never model-judged), and the share of calls that were deterministic."
            className="analysis-help"
          >
            {groundedSteps} of {totalTools} grounded · {deterministicPct}% deterministic
          </span>
        )}
        {correlationWritebacks !== null && correlationWritebacks > 0 && (
          <span
            title="Cross-system correlation findings successfully saved for human review during this query; these are writes, not applied rules."
            className="analysis-help"
          >
            {correlationWritebacks} correlation finding{correlationWritebacks === 1 ? '' : 's'} saved
          </span>
        )}
        {noDataSteps > 0 && (
          <span
            title="Knowledge-graph lookups that returned no matching data — a normal part of exploring."
            className="analysis-help analysis-stat-muted"
          >
            ○ {noDataSteps} no-data lookup{noDataSteps === 1 ? '' : 's'}
          </span>
        )}
      </div>
    </div>
  );
}

/** Unusable "N/A" suggestions never render. */
const naRe = /^\s*n\/?a\s*$/i;

/** Tooltip for the "Planned with" sub-part — the Mode A route seed ("loaded
 * at planning" — available to the planner, not necessarily used; single
 * source: lib/metric-copy.ts, T1.6). */
const PLANNED_TIP = PLANNED_WITH_TIP;

/* Chips (concept + rule chips, the cost chip) are `.analysis-chip`; the
 * "Planned with" / "Bridged via" labels are `.analysis-part-label`
 * (analysis-panel.css). */

/** Formatted count phrases + concept chips from s.planned_with. Null when
 * the key is absent or carries nothing renderable — old payloads, local mode
 * (no route-seed prelude) and Mode B render NO "Planned with" part. Every
 * field is typeof-guarded so hostile non-numeric / non-string payload values
 * are dropped rather than rendered. */
function plannedWithParts(s: IntelligenceSummary): { counts: string[]; concepts: string[] } | null {
  const p = s.planned_with;
  if (!p || typeof p !== 'object') return null;
  const counts: string[] = [];
  const add = (v: unknown, singular: string, plural: string) => {
    if (typeof v === 'number' && Number.isFinite(v) && v >= 0) {
      counts.push(`${v} ${v === 1 ? singular : plural}`);
    }
  };
  add(p.systems, 'system', 'systems');
  add(p.join_paths, 'join path', 'join paths');
  add(p.value_mappings, 'value mapping', 'value mappings');
  add(p.identity_rules, 'identity rule', 'identity rules');
  const concepts = Array.isArray(p.shared_concepts)
    ? p.shared_concepts.filter((c): c is string => typeof c === 'string' && c.trim().length > 0)
    : [];
  if (counts.length === 0 && concepts.length === 0) return null;
  return { counts, concepts };
}

/** Applied rules with string-guarded fields; entries without a usable label
 * or id are dropped (nothing honest to render). */
function bridgedViaRules(s: IntelligenceSummary): { id: string; label: string }[] {
  if (!Array.isArray(s.rules_applied)) return [];
  return s.rules_applied
    .map((r) => ({
      id: typeof r?.id === 'string' ? r.id : '',
      label: typeof r?.label === 'string' ? r.label : '',
    }))
    .filter((r) => r.id !== '' || r.label !== '');
}

/** "Planned with" sub-part — route-seed counts line + shared-concept chips. */
function PlannedWith({ counts, concepts }: { counts: string[]; concepts: string[] }) {
  return (
    <div className="analysis-part">
      <span title={PLANNED_TIP} className="analysis-part-label">
        Planned with
      </span>
      {counts.length > 0 && (
        <span className="analysis-part-counts">{counts.join(' · ')}</span>
      )}
      {concepts.length > 0 && (
        <div className="analysis-chips">
          {concepts.map((c, i) => (
            <span key={i} className="analysis-chip" title="Shared concept from the route seed">
              {c}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

/** "Bridged via" sub-part — one chip per applied rule. Chips with a node id
 * navigate to the KG view (data-nav="kg-node" + the delegated thread click
 * handler — same anchor convention as data-nav="self-learning"); id-less
 * entries render as plain, non-navigating spans. Labels are JSX text. */
function BridgedVia({ rules }: { rules: { id: string; label: string }[] }) {
  return (
    <div>
      <span title={RULES_TIP} className="analysis-part-label">
        Bridged via
      </span>
      <div className="analysis-chips">
        {rules.map((r, i) =>
          r.id !== '' ? (
            <a
              key={i}
              href="#"
              data-nav="kg-node"
              data-node-id={r.id}
              title={RULES_TIP}
              className="analysis-chip analysis-chip-link"
            >
              {r.label || r.id}
            </a>
          ) : (
            <span key={i} className="analysis-chip" title={RULES_TIP}>
              {r.label}
            </span>
          ),
        )}
      </div>
    </div>
  );
}

/** Pinned E7 scope tooltip (ADR-0053 measurement honesty). */
const COST_TIP =
  'Measured orchestrator model usage only — system-agent model calls are not included. No cost estimate is derived.';

/** Compact token-count formatter (pure, exported for tests): 6042 → "6.0k",
 * 950 → "950", 2_400_000 → "2.4M". Empty string for unusable input. */
export function compactTokens(n: number): string {
  if (typeof n !== 'number' || !Number.isFinite(n) || n < 0) return '';
  if (n < 1000) return String(Math.round(n));
  if (n < 1_000_000) return `${(n / 1000).toFixed(1)}k`;
  return `${(n / 1_000_000).toFixed(1)}M`;
}

/** Chip text from summary.cost — null unless the key is present with a
 * usable measured total (honest absence; typeof-guarded like planned_with
 * so a hostile non-numeric payload value never renders). */
function costChipText(s: IntelligenceSummary): string | null {
  const c = s.cost;
  if (!c || typeof c !== 'object') return null;
  const compact = compactTokens(c.total_tokens);
  if (compact === '') return null;
  return `≈${compact} tokens · orchestrator model`;
}

/** The measured-cost chip (E7). Presence is gated ONLY on summary.cost —
 * absent key ⇒ no chip, never zeros. */
function CostChip({ text }: { text: string }) {
  return (
    <span className="cost-chip analysis-chip analysis-help" title={COST_TIP}>
      {text}
    </span>
  );
}

/** Per-answer "Knowledge graph contribution" section — how the knowledge
 * graph shaped THIS answer: "Planned with" (Mode A route seed) and "Bridged
 * via" (accepted self-learning rules applied). HONESTY: both parts are
 * presence-gated on same-turn evidence; when neither has data the KG header
 * is absent (never an empty header or zero-padding). The E7 cost chip sits
 * beside the header when the section renders, and alone otherwise. */
function KgContribution({ s }: { s: IntelligenceSummary }) {
  const planned = plannedWithParts(s);
  const rules = bridgedViaRules(s);
  const chip = costChipText(s);
  if (!planned && rules.length === 0) {
    // Honesty pin: no same-turn KG data ⇒ no section header. The measured
    // cost chip (E7) still renders on its own row — its presence is gated
    // ONLY on summary.cost, not on KG contribution.
    if (chip === null) return null;
    return (
      <div className="cost-chip-row analysis-box">
        <CostChip text={chip} />
      </div>
    );
  }
  return (
    <div className="kg-contribution-box analysis-box">
      <div className="analysis-kg-header">
        <div
          title="What the knowledge graph contributed to this answer — routing metadata fetched up front and learned rules applied while answering."
          className="analysis-caption"
        >
          Knowledge graph contribution
        </div>
        {chip !== null ? <CostChip text={chip} /> : null}
      </div>
      {planned ? <PlannedWith counts={planned.counts} concepts={planned.concepts} /> : null}
      {rules.length > 0 ? <BridgedVia rules={rules} /> : null}
    </div>
  );
}

/** metadata.agent_analysis — LLM post-query review. Untrusted fields render
 * as JSX text; unusable "N/A" hidden. */
function AgentAnalysisReview({ a }: { a: AgentAnalysis }) {
  return (
    <div className="agent-analysis-box analysis-box">
      {/* Summary (qualitative) */}
      {a.summary ? <div className="analysis-review-summary">{a.summary}</div> : null}
      {/* Exploration steps that found no matching data (was "Wasted Calls"). */}
      {a.wasted_calls && a.wasted_calls.length > 0 ? (
        <div className="analysis-review-steps">
          <div className="analysis-review-steps-title">
            Exploration steps that found no matching data ({a.wasted_calls.length})
          </div>
          {a.wasted_calls.slice(0, 5).map((w, i) => (
            <div key={i} className="analysis-review-step">
              {w.reason ? w.reason : 'No matching data found for this lookup.'}
              {w.fix && !naRe.test(w.fix) ? (
                <>
                  <br />
                  <span className="analysis-suggestion-label">Suggestion:</span> {w.fix}
                </>
              ) : null}
            </div>
          ))}
        </div>
      ) : null}
      {/* Schema hints — demoted to a single link (detail lives in Self-Learning). */}
      {a.recommendations && a.recommendations.length > 0 ? (
        <div className="analysis-callout analysis-callout-hints">
          💡 <strong>{a.recommendations.length}</strong> schema hint
          {a.recommendations.length > 1 ? 's' : ''} discovered →{' '}
          <a href="#" data-nav="self-learning" className="analysis-callout-link">
            Review in Self-Learning Discoveries
          </a>
        </div>
      ) : null}
    </div>
  );
}

/** metadata.intelligence_summary — only the actionable Self-Learning link
 * survives (counts live in the unified panel). */
function IntelligenceLink({ s }: { s: IntelligenceSummary }) {
  const n = s.new_proposed_mappings || 0;
  if (n <= 0) return null;
  return (
    <div
      className="intelligence-box analysis-callout analysis-callout-optimizations"
    >
      ⚡ <strong>{n}</strong> new optimization{n > 1 ? 's' : ''} discovered →{' '}
      <a href="#" data-nav="self-learning" className="analysis-callout-link">
        Review in Self-Learning
      </a>
    </div>
  );
}

/** The single collapsible Agent Analysis panel (rendered by <AssistantTurn/>
 * on done). Collapsed by default — the details carries no open prop. The
 * summary line is data-only JSX text. */
export function AgentAnalysisPanel({ data }: { data: AgentAnalysisPanelData }) {
  const { totalTools, graphLookups, noDataSteps } = data;
  let summaryText = `🔍 Agent Analysis — ${totalTools} tool call${totalTools === 1 ? '' : 's'} · ${graphLookups} graph lookup${graphLookups === 1 ? '' : 's'}`;
  if (noDataSteps > 0) {
    summaryText += ` · ${noDataSteps} step${noDataSteps === 1 ? '' : 's'} found no matching data`;
  }
  const intelligenceActionable = !!(data.summary && (data.summary.new_proposed_mappings || 0) > 0);
  return (
    <details className="agent-analysis-details">
      <summary className="agent-analysis-summary">{summaryText}</summary>
      <div className="agent-analysis-body">
        {data.hasBrowser ? (
          <div>
            <BrowserAnalysis data={data} />
          </div>
        ) : null}
        {/* KG-contribution section — self-gating (null when neither part has
         * same-turn data), so no wrapper div that could render empty. */}
        {data.summary ? <KgContribution s={data.summary} /> : null}
        {data.agentAnalysis ? (
          <div>
            <AgentAnalysisReview a={data.agentAnalysis} />
          </div>
        ) : null}
        {intelligenceActionable ? (
          <div>
            <IntelligenceLink s={data.summary!} />
          </div>
        ) : null}
      </div>
    </details>
  );
}
