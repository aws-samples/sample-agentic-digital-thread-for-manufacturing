// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Pure dashboard logic — the DOM-free dashboard helpers
 * (the spec for Task 2.2). No DOM access here so every
 * function is unit-testable under vitest's node environment.
 *
 * The bigger render paths (renderLineState, renderOperationsSummary, the
 * briefing text pipeline) are split into a pure "compute" half here and a
 * thin DOM half in their owning modules. */
import type {
  CardMetric,
  CardMetricSite,
  DashboardCard,
  LineStatePayload,
  ThresholdBand,
} from '../../api/types/dashboard';
import { splitFollowups } from '../../chat/followups-marker';
import { formatRelativeTime } from '../../lib/relative-time';
import { parseToolIdentity } from '../../lib/tool-identity';
import { cutOpenedProposal, stripProposalBlocks } from '../../shared/thread/answer-blocks';

// ── Value formatting ──────────────────────────────────────────────────────────

/** Format a KPI card value: integers via locale grouping, floats to ≤2 decimals
 * (dashboard.js `_fmtCardValue`). Non-numeric passes through as String(v). */
export function fmtCardValue(v: unknown): string {
  if (typeof v !== 'number' || !isFinite(v)) return String(v);
  const r = Math.round(v * 100) / 100;
  return Number.isInteger(r)
    ? r.toLocaleString()
    : r.toLocaleString(undefined, { maximumFractionDigits: 2 });
}

/** Chip labels for a multi-site breakdown: the site, or the system name for an
 * unsited (all-sites) entry. When two entries share ONE site label (e.g. a
 * historian and an MES at the same plant) each label also names its system,
 * so the breakdown never reads as one site listed twice. */
export function siteChipLabels(sites: readonly CardMetricSite[]): string[] {
  const base = sites.map((s) => s.site || s.system_name || s.system_id || '');
  const counts = new Map<string, number>();
  for (const label of base) counts.set(label, (counts.get(label) || 0) + 1);
  return sites.map((s, i) => {
    const label = base[i];
    if (!s.site || (counts.get(label) || 0) < 2) return label;
    const system = s.system_name || s.system_id;
    return system ? label + ' \u00b7 ' + system : label;
  });
}

/** Map a semantic color token to a theme CSS var (`_bandColorVar`). */
export function bandColorVar(token: string): string {
  const map: Record<string, string> = {
    green: 'var(--green)',
    orange: 'var(--amber)',
    amber: 'var(--amber)',
    red: 'var(--red)',
    yellow: 'var(--amber)',
  };
  return map[token] || token;
}

/** Threshold band for a numeric value: first band whose cutoff v is <= (null
 * cutoff = catch-all). Returns the band's color token or null (`_thresholdColor`). */
export function thresholdColor(
  bands: ThresholdBand[] | undefined,
  v: unknown,
): string | null {
  if (!Array.isArray(bands) || !bands.length) return null;
  if (typeof v !== 'number' || !isFinite(v)) return null;
  for (const b of bands) {
    if (b.value === null || b.value === undefined || v <= b.value) return b.color || null;
  }
  return null;
}

// ── Machine-state bucketing + Line State rollup ───────────────────────────────

export type LineStateBucket = 'running' | 'idle' | 'down';

/** Bucket a raw machine-state string into running/idle/down (`_bucketMachineState`). */
export function bucketMachineState(state: unknown): LineStateBucket {
  const s = String(state || '').toLowerCase();
  if (/(run|active|producing|on)\b/.test(s) || s === 'running' || s === 'run') return 'running';
  if (/(down|stop|fault|fail|error|alarm|offline)/.test(s)) return 'down';
  if (/(idle|changeover|setup|standby|paused|wait|maint)/.test(s)) return 'idle';
  return 'idle';
}

export interface LineStateBuckets {
  running: number;
  idle: number;
  down: number;
}

export interface LineStateChip {
  text: string;
  bucket: LineStateBucket;
}

export interface LineStateSiteGroup {
  label: string;
  chips: LineStateChip[];
}

export type LineStateModel =
  | { kind: 'error'; badge: string; diag: string }
  | { kind: 'uncovered'; badge: string; diag: string }
  | { kind: 'empty'; badge: string; diag: string; headline: LineStateBuckets }
  | {
      kind: 'data';
      badge: string;
      diag: string;
      headline: LineStateBuckets;
      groups: LineStateSiteGroup[];
      siteCount: number;
    };

/** Sites with at most this many total lines render one chip PER LINE; larger
 * inventories collapse to compact "STATE · N" chips (dashboard.js EXPAND_CAP). */
export const LINE_STATE_EXPAND_CAP = 12;

/** Pure half of renderLineState — the per-site grouping, the expand-vs-compact
 * chip choice, and the headline rollup INCLUDING the all-sites fallback:
 * only line-grained (expanded) sites feed the headline counts; when none
 * contributed (e.g. the only source is a large machine inventory), fall back
 * to ALL sites so the headline reflects real state instead of 0. */
export function computeLineState(payload: LineStatePayload | null | undefined): LineStateModel {
  const sites = payload && Array.isArray(payload.sites) ? payload.sites : null;
  if (payload && payload.status === 'uncovered') {
    // Mapping gap (expected mid-onboarding) — honest needs-data, not error.
    const miss = (payload.missing_concepts || []).filter(Boolean).join(', ');
    return {
      kind: 'uncovered',
      badge: 'machine-state',
      diag:
        'Waiting for data \u2014 ' +
        (miss
          ? `no onboarded system maps ${miss} yet.`
          : 'the machine-state concept is not mapped by an onboarded system yet.'),
    };
  }
  if (!payload || payload.status === 'error' || !sites) {
    return {
      kind: 'error',
      badge: 'machine-state',
      diag:
        'Line State unavailable — ' +
        ((payload && payload.error) ||
          'the machine-state concept is not mapped on any onboarded system, or the resolve failed.'),
    };
  }

  const types = Array.isArray(payload.source_system_types) ? payload.source_system_types : [];
  const badge = types.length ? types.join(' + ') : 'machine-state';

  if (!sites.length) {
    return {
      kind: 'empty',
      badge,
      headline: { running: 0, idle: 0, down: 0 },
      diag: 'Line State — machine-state mapped but no lines returned.',
    };
  }

  const buckets: LineStateBuckets = { running: 0, idle: 0, down: 0 };
  // Every site — headline fallback when no line-grained site contributes.
  const allBuckets: LineStateBuckets = { running: 0, idle: 0, down: 0 };
  const seen: string[] = [];
  const groups: LineStateSiteGroup[] = [];

  sites.forEach((site) => {
    if (!site) return;
    const states = Array.isArray(site.states) ? site.states : [];
    const chips: LineStateChip[] = [];
    // Tally the site total first to choose expanded vs compact chips.
    let siteTotal = 0;
    states.forEach((st) => {
      if (st) siteTotal += Number(st.count) || 0;
    });
    const expand = siteTotal <= LINE_STATE_EXPAND_CAP;
    states.forEach((st) => {
      if (!st) return;
      const bucket = bucketMachineState(st.state);
      const count = Number(st.count) || 0;
      // Only line-grained (expanded) sites feed the headline totals; every site
      // feeds the all-sites fallback tally.
      allBuckets[bucket] += count;
      if (expand) buckets[bucket] += count;
      seen.push(String(st.state) + '\u2192' + bucket + '\u00d7' + count);
      if (expand) {
        for (let n = 0; n < count; n++) chips.push({ text: String(st.state), bucket });
      } else {
        chips.push({ text: String(st.state) + ' \u00b7 ' + count, bucket });
      }
    });
    groups.push({
      label: String(site.site || site.name || site.system_id || ''),
      chips,
    });
  });

  // Prefer line-grained (expanded) sites for the headline; fall back to all sites.
  const headline =
    buckets.running + buckets.idle + buckets.down > 0 ? buckets : allBuckets;

  return {
    kind: 'data',
    badge,
    headline,
    groups,
    siteCount: sites.length,
    diag:
      'Counts = production lines per current state across ' +
      sites.length +
      ' site(s), grouped into Running/Idle/Down. State \u2192 bucket: ' +
      (seen.join(', ') || 'none') +
      '.',
  };
}

// ── Honest-data predicates (Task 2B) ─────────────────────────────────────────

/** True when a performance ratio is explicitly speed-vs-target. */
export function isSpeedOfTargetCard(card: DashboardCard | null | undefined): boolean {
  const metrics = card && Array.isArray(card.metrics) ? card.metrics : [];
  return metrics[0]?.metric_id === 'performance-ratio';
}

/** A speed-vs-target percentage is computable only with a finite, positive
 * target reading. A reported zero without that denominator is not honest data. */
function speedOfTargetMissingTarget(card: DashboardCard): boolean {
  if (!isSpeedOfTargetCard(card)) return false;
  const target = (card.metrics || []).find((m) => m?.metric_id === 'target-machine-speed');
  return !(
    target?.status === 'ok' &&
    typeof target.value === 'number' &&
    isFinite(target.value) &&
    target.value > 0
  );
}

/** True when a card is covered but has NO data yet (Task 2A `no-data`):
 * either the card-level status says so, EVERY metric on the card resolved to
 * `no-data`, or a speed-vs-target percentage lacks its required target. */
export function cardHasNoDataYet(card: DashboardCard | null | undefined): boolean {
  if (!card) return false;
  if (card.status === 'no-data') return true;
  if (speedOfTargetMissingTarget(card)) return true;
  const metrics = Array.isArray(card.metrics) ? card.metrics.filter((m) => m) : [];
  return metrics.length > 0 && metrics.every((m) => m!.status === 'no-data');
}

/** Metric statuses that WITHHOLD a value until a steward picks something:
 * `ambiguous-binding` (the concept binds to several columns, ADR-0076) and
 * `unresolved-predicate` (a filter concept has no bound field, ADR-0075).
 * Not data faults — `invariant-violation` stays a tile. */
export const STEWARD_CHOICE_STATUSES: ReadonlySet<string> = new Set([
  'ambiguous-binding',
  'unresolved-predicate',
]);

/** True when EVERY metric on a card is a steward-choice withhold, so the card
 * has nothing to show but the reason. Such a card goes to the strip's
 * "Needs a steward choice" group instead of a dead tile; a card with any
 * other metric status (an ok value, an invariant violation, …) stays a tile. */
export function cardNeedsStewardChoice(card: DashboardCard | null | undefined): boolean {
  if (!card) return false;
  const metrics = Array.isArray(card.metrics) ? card.metrics.filter((m) => m) : [];
  return metrics.length > 0 && metrics.every((m) => STEWARD_CHOICE_STATUSES.has(m!.status || ''));
}

/** True when a dimension payload's rows carry at least one NON-NULL value
 * (row[1]). All-null rows are degenerate — the panel hides instead of
 * rendering dead em-dash bars. Zeros are honest data and count as non-null
 * (an all-zero windowed resolve keeps its panel visible). */
export function hasNonDegenerateRows(rows: unknown[] | undefined | null): boolean {
  if (!Array.isArray(rows) || !rows.length) return false;
  return rows.some((r) => Array.isArray(r) && r[1] !== null && r[1] !== undefined);
}

// ── Operations Summary (exceptions panel) ─────────────────────────────────────

export type InsightLevel = 'critical' | 'warning' | 'info';

export interface Insight {
  level: InsightLevel;
  text: string;
}

export type OpsSummaryModel =
  | { kind: 'empty' }
  | { kind: 'ok'; count: number }
  | { kind: 'insights'; insights: Insight[] };

/** Pure half of renderOperationsSummary — derives the exceptions list from the
 * SAME cards payload (threshold breaches, below-target, machines down, and
 * no-data/unavailable/steward-choice rollups; uncovered cards are owned by the awaiting-data
 * strip and produce no row here), sorted critical → warning → info. */
export function computeOperationsSummary(
  cards: DashboardCard[] | undefined | null,
  lineStateSummary: LineStateBuckets | null,
): OpsSummaryModel {
  const list = Array.isArray(cards) ? cards : [];
  if (!list.length) return { kind: 'empty' };

  const insights: Insight[] = [];
  let noData = 0;
  let unavailable = 0;
  let stewardChoice = 0;

  list.forEach((card) => {
    if (!card) return;
    const title = card.title || card.card_id || 'Metric';
    // Uncovered (not yet mapped) cards produce NO insight row: the
    // awaiting-data strip at the bottom of the dashboard owns the uncovered
    // story (one source of truth). They still must not count as unavailable.
    if (card.status === 'uncovered') {
      return;
    }
    // Every metric withheld pending a steward choice — its own rollup (the
    // strip's "Needs a steward choice" group owns the detail), checked before
    // no-data so a withhold is never counted as an empty source.
    if (cardNeedsStewardChoice(card)) {
      stewardChoice++;
      return;
    }
    // Covered but zero samples (Task 2B) — an INFO "need data" rollup, never
    // an "unavailable" warning: nothing is broken, the source is just empty.
    if (cardHasNoDataYet(card)) {
      noData++;
      return;
    }
    const metric = card.metrics && card.metrics.length ? card.metrics[0] : null;
    const ok = metric && metric.status === 'ok';
    if (!ok) {
      // A scalar/gauge metric that didn't resolve (gauge with no ok component counts too).
      if (!card.metrics || !card.metrics.some((m) => m && m.status === 'ok')) unavailable++;
      return;
    }
    const valTxt = fmtCardValue(metric.value) + (metric.unit ? ' ' + metric.unit : '');
    const band = thresholdColor(card.thresholds, metric.value);
    if (band === 'red') {
      insights.push({ level: 'critical', text: title + ' ' + valTxt + ' — critical' });
    } else if (band === 'orange' || band === 'amber') {
      insights.push({ level: 'warning', text: title + ' ' + valTxt + ' — warning' });
    }
    if (
      typeof card.target === 'number' &&
      isFinite(card.target) &&
      typeof metric.value === 'number' &&
      metric.value < card.target
    ) {
      insights.push({
        level: 'warning',
        text: title + ' ' + valTxt + ' — below target ' + fmtCardValue(card.target),
      });
    }
  });

  if (lineStateSummary && lineStateSummary.down > 0) {
    insights.push({
      level: 'critical',
      text:
        lineStateSummary.down +
        ' machine' +
        (lineStateSummary.down > 1 ? 's' : '') +
        ' down' +
        (lineStateSummary.idle > 0 ? ', ' + lineStateSummary.idle + ' idle' : ''),
    });
  }
  if (noData) {
    insights.push({
      level: 'info',
      text:
        noData + ' metric' + (noData > 1 ? 's' : '') + ' need data (mapped, no rows yet)',
    });
  }
  if (unavailable) {
    insights.push({
      level: 'warning',
      text: unavailable + ' metric' + (unavailable > 1 ? 's' : '') + ' unavailable',
    });
  }
  if (stewardChoice) {
    insights.push({
      level: 'warning',
      text:
        stewardChoice +
        ' metric' +
        (stewardChoice > 1 ? 's' : '') +
        ' need' +
        (stewardChoice > 1 ? '' : 's') +
        ' a steward choice',
    });
  }

  if (!insights.length) return { kind: 'ok', count: list.length };
  const rank: Record<InsightLevel, number> = { critical: 0, warning: 1, info: 2 };
  insights.sort((a, b) => rank[a.level] - rank[b.level]);
  return { kind: 'insights', insights };
}

export const INSIGHT_GLYPHS: Record<InsightLevel, string> = {
  critical: '\u26a0 ',
  warning: '\u25b2 ',
  info: '\u2022 ',
};

// ── Gauge helpers ─────────────────────────────────────────────────────────────

/** Normalize to a 0-100 percentage (0-1 ratios scaled ×100); null for non-finite. */
export function toPercent(v: unknown): number | null {
  if (typeof v !== 'number' || !isFinite(v)) return null;
  return v <= 1 ? v * 100 : v;
}

/** Metric entry → percentage honoring the per-metric fail-soft contract. */
export function gaugePercent(metric: CardMetric | null | undefined): number | null {
  if (!metric || metric.status !== 'ok') return null;
  return toPercent(metric.value);
}

/** Threshold color for a percentage: >=85 green, >=60 amber, else red. */
export function gaugeColor(pct: number | null): string {
  if (pct === null) return 'var(--muted)';
  if (pct >= 85) return 'var(--green)';
  if (pct >= 60) return 'var(--amber)';
  return 'var(--red)';
}

/** Status CSS class for the gauge card's left border. */
export function gaugeStatusClass(pct: number | null): string {
  if (pct === null) return 'status-warning';
  if (pct >= 85) return 'status-good';
  if (pct >= 60) return 'status-warning';
  return 'status-error';
}

/** Short sub-reading label for a gauge component (`_gaugeSubLabel`).
 * Prefers the backend-supplied `metric.label` (the contract after the OEE
 * relabel — "Availability" / "Performance" / "Quality"), falling back to the
 * id-derived prettifier for legacy payloads. Either way a trailing "rate"
 * WORD is stripped case-insensitively ("Quality Rate" → "Quality",
 * 'quality-rate' → 'Quality') — word-boundary only, so ids like 'flow-rate'
 * strip but words merely ENDING in "rate" (e.g. 'operate') do not. */
export function gaugeSubLabel(metric: CardMetric | null | undefined): string {
  const label = metric && metric.label ? String(metric.label).trim() : '';
  if (label) {
    const stripped = label.replace(/[\s_-]+rate$/i, '').trim();
    return stripped || label;
  }
  const id = metric && metric.metric_id ? String(metric.metric_id) : '';
  if (!id) return '';
  return id
    .replace(/[-_]rate$/i, '')
    .replace(/[-_]/g, ' ')
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

/** Client-side OEE reconciliation: A×P×Q from a gauge card's SUB-readings
 * (metrics[1..3], same slice GaugeBody renders), each normalized to a
 * percentage via the gaugePercent/toPercent contract, returned as 0–100.
 * Null when the card has fewer than 3 sub-readings or ANY of them fails to
 * resolve (status !== 'ok' / non-finite value) — presence-driven, so the
 * reconciliation line simply doesn't render for partial data. */
export function oeeComponentsPercent(
  metrics: Array<CardMetric | null | undefined> | null | undefined,
): number | null {
  const subs = Array.isArray(metrics) ? metrics.slice(1, 4) : [];
  if (subs.length < 3) return null;
  let product = 1;
  for (const m of subs) {
    const pct = gaugePercent(m);
    if (pct === null) return null;
    product *= pct / 100;
  }
  return product * 100;
}

const OEE_FORMULA_TEXT = 'OEE = Availability \u00d7 Performance \u00d7 Quality';
export const OEE_SOURCE_REPORTED_TEXT =
  'OEE as reported by the source system; contributing readings shown below';
const OEE_FORMULA_EPSILON_PERCENT = 0.5;

/** Formula copy is safe only when the displayed composite actually agrees
 * with A×P×Q. Missing components are unverifiable and therefore source-reported. */
export function oeeDisclosureText(
  metrics: Array<CardMetric | null | undefined> | null | undefined,
): string {
  const primary = Array.isArray(metrics) ? metrics[0] : null;
  const displayed = gaugePercent(primary);
  const components = oeeComponentsPercent(metrics);
  if (
    displayed !== null &&
    components !== null &&
    Math.abs(displayed - components) <= OEE_FORMULA_EPSILON_PERCENT
  ) {
    return OEE_FORMULA_TEXT;
  }
  return OEE_SOURCE_REPORTED_TEXT;
}

/** Replace resolver internals in chart headlines while retaining exact detail
 * for a disclosure owned by the rendering component. */
export function panelErrorSummary(raw: unknown, fallback = 'Panel unavailable.'): string {
  const detail = String(raw || '');
  if (/ambiguous join|JoinPaths? connect/i.test(detail)) {
    return "This chart's data mapping needs attention";
  }
  return fallback;
}

// ── Friendly errors / labels ──────────────────────────────────────────────────

/** Map raw resolver/authority error strings to an operator-facing reason. */
export function friendlyMetricError(raw: unknown): string {
  const s = String(raw || '');
  if (!s) return 'could not be resolved';
  if (/AuthorityRule|systems map it|set_authority|not mapped|no .*map/i.test(s)) {
    return 'not mapped on any onboarded system yet';
  }
  if (/NotImplemented/i.test(s)) return 'not supported for this source';
  const clean = s.split(/[;(]/)[0].trim();
  return clean || 'could not be resolved';
}

/** 'production-line' → 'Production Line' (`_prettyConcept`). */
export function prettyConcept(c: unknown): string {
  const s = c ? String(c) : '';
  return s.replace(/[-_]/g, ' ').replace(/\b\w/g, (ch) => ch.toUpperCase());
}

/** Closed severity token → heatmap cell CSS class (`_heatmapSeverityClass`). */
export function heatmapSeverityClass(sev: unknown): string {
  if (sev === 'critical') return 'severity-critical';
  if (sev === 'warning') return 'severity-warning';
  if (sev === 'ok') return 'severity-ok';
  return 'severity-uncovered';
}

/** Heatmap cell label — the reading, or em-dash for absent (`_heatmapCellText`). */
export function heatmapCellText(value: unknown): string {
  if (value === null || value === undefined || value === '') return '\u2014';
  return fmtCardValue(value);
}

/** Closed severity token → accent color for issue rows (`_issueSeverityColor`). */
export function issueSeverityColor(sev: unknown): string {
  if (sev === 'critical') return 'var(--red)';
  if (sev === 'warning') return 'var(--amber)';
  return 'var(--muted)';
}

// ── Tool labels (cross-file global: chat.js consumes window.getToolLabel) ─────

const TOOL_LABELS: Record<string, string> = {
  explore_graph: 'Knowledge Graph',
  get_system_schema: 'Get Schema',
};

/** Tool name → human-readable label (dashboard.js `getToolLabel`). */
export function getToolLabel(toolName: string): string {
  if (TOOL_LABELS[toolName]) return TOOL_LABELS[toolName];
  const identity = parseToolIdentity(toolName);
  // Gateway format: "mes-mil___query_mes_mil"
  if (identity.shape === 'gateway') {
    const systemId = identity.systemId ?? '';
    const systemMap: Record<string, string> = {
      erp: 'Query ERP',
      'mes-mil': 'Query MES · Milwaukee',
      'mes-det': 'Query MES · Detroit',
      'mes-cr': 'Query MES · Cedar Rapids',
      cmms: 'Query CMMS',
      plm: 'Query PLM',
    };
    return systemMap[systemId] || 'Query ' + systemId;
  }
  if (identity.shape === 'query') {
    const name = identity.systemId ?? '';
    if (name === 'erp') return 'Query ERP';
    if (name.startsWith('mes_')) {
      return (
        'Query MES · ' +
        name
          .slice(4)
          .split('_')
          .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
          .join(' ')
      );
    }
    return (
      'Query ' +
      name
        .split('_')
        .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
        .join(' ')
    );
  }
  return toolName;
}

// ── Misc ──────────────────────────────────────────────────────────────────────

/** '3m ago' style relative time (`_relativeTime`). */
export function relativeTime(iso: string): string {
  return formatRelativeTime(iso, { maxUnit: 'day' });
}

// (splitIntoPages is gone — its only consumer was the demo pager overlay,
// removed in the demo-through-chat uniformity refactor.)

// ── AI Ops Briefing text pipeline (pure half of requestOpsBriefing) ───────────

/** Choose the briefing raw text: prefer the clean `done` final answer (intro
 * line stripped); else the token stream with its machine payload (follow-ups,
 * proposal blocks, the internal counter) taken off. Both then go through the
 * same card / note split (`splitBriefing`, after `cleanBriefingText`). */
export function extractBriefingRaw(doneText: string, tokenText: string): string {
  doneText = (doneText || '').replace(/\r\n?/g, '\n');
  tokenText = (tokenText || '').replace(/\r\n?/g, '\n');
  if (doneText && doneText.length > 10) {
    return doneText
      .replace(/^Here is your[^\n]*\n/i, '')
      .replace(/^Here['\u2019]s your[^\n]*\n/i, '')
      .trim();
  }
  // Follow-up suggestions and proposal blocks are machine payload, never the
  // briefing. They come off first: their `---` markers would otherwise read
  // as rule lines (rehearsal, 2026-10-15: the card kept only the follow-up Q/A).
  const body = cutOpenedProposal(stripProposalBlocks(splitFollowups(tokenText).body));
  const lastBriefingIdx = body.lastIndexOf('Here is your');
  if (lastBriefingIdx >= 0) {
    return body.slice(lastBriefingIdx).replace(/^Here is your[^\n]*\n/i, '').trim();
  }
  // A briefing with any card: the whole body; splitBriefing drops the text
  // before the first card and the rule lines. With no card anywhere (the
  // block fallback), the text after the last `---`.
  if (body.split('\n').some(isBriefingCardLine)) return body.trim();
  const parts = body
    .split(/---+/)
    .map((part) => part.trim())
    .filter(Boolean);
  return parts[parts.length - 1] ?? '';
}

// ── Briefing cards and notes: one rule, the ONE home for it ─────────────────

/** The most cards a briefing shows (the prompt asks for five bullets). */
export const BRIEFING_MAX_CARDS = 5;

/** A line wholly in single-star italics (`**bold**` allowed inside), with an
 * optional period after the closing star. */
const BRIEFING_ITALIC_STAR_LINE = /^\*(?![*\s])((?:\*\*[^*]+\*\*|[^*])*?[^\s*])\*(\.?)$/;
/** The caveat words a note label may use. */
const BRIEFING_NOTE_WORDS = '(?:note|notes|caveat|footnote|disclosure|data note)';
/** A line that opens with an explicit caveat label. Italic: `*Note:*`,
 * `*Note*:`, `_Caveat:_` (the colon optional). Bold: `**Note:**`,
 * `**Caveat**:`, `__Note:__` (the colon required, inside or just outside the
 * bold, so a bold-title card such as `**Line 2** is down` stays a card). */
const BRIEFING_NOTE_LABEL = new RegExp(
  `^(?:([*_])(?!\\1)\\s*${BRIEFING_NOTE_WORDS}\\s*:?\\s*\\1\\s*:?` +
    `|(\\*\\*|__)\\s*${BRIEFING_NOTE_WORDS}\\s*(?::\\s*\\2|\\2\\s*:))(?:\\s|$)`,
  'i',
);

/** One list marker at the start of a line: `•`, `-` or a single `*`, then a space. */
const BRIEFING_LIST_MARKER = /^(?:[•\-]|\*(?!\*))\s+/;
/** A rule line (`---`, `***`, `___`): never a card or a note. */
const BRIEFING_RULE_LINE = /^(?:-{3,}|\*{3,}|_{3,})$/;

/** A card line: a list item (`•`, `-`, `*`, with or without a space) or a
 * bold-title line, with a letter or digit after its marker, that is NOT
 * wholly italic and does NOT open with a note label (with or without one list
 * marker before it). `*Line 2* is down` and `- *All normal.*` are cards;
 * `*All normal.*`, `**Note:** ...` and `- **Note:** ...` are not; a marker
 * alone is not. */
function isBriefingCardLine(line: string): boolean {
  const text = line.trim();
  if (!/^[•\-*]/.test(text) || BRIEFING_RULE_LINE.test(text)) return false;
  if (BRIEFING_ITALIC_STAR_LINE.test(text) || BRIEFING_NOTE_LABEL.test(text)) return false;
  const item = text.replace(BRIEFING_LIST_MARKER, '');
  if (item !== text && BRIEFING_NOTE_LABEL.test(item)) return false;
  return /[\p{L}\p{N}]/u.test(text.replace(/^(?:[•\-]|\*(?!\*))\s*/, ''));
}

/** Split a cleaned briefing into its cards and notes.
 *
 * - CARDS: the first `BRIEFING_MAX_CARDS` card lines (`isBriefingCardLine`), in order.
 * - NOTES: every other line AFTER the first card, in order and verbatim
 *   (indentation they all share removed, the rest kept so a child item stays
 *   under its parent): a caveat, a footnote over several lines, a sixth bullet, a
 *   closing sentence. Rule lines and lines with no letter or digit (a marker
 *   alone) are left out.
 * - Text before the first card (an intro) is dropped. No card: both empty
 *   (the caller renders the text as one block).
 *
 * Machine payload (follow-ups, proposal blocks, the internal counter) is
 * already gone (`extractBriefingRaw`, `cleanBriefingText`). */
export function splitBriefing(clean: string): { cards: string[]; notes: string[] } {
  const cards: string[] = [];
  const notes: string[] = [];
  let started = false;
  for (const raw of (clean || '').split('\n')) {
    const line = raw.trim();
    if (!line) continue;
    const card = isBriefingCardLine(line);
    if (!started && !card) continue;
    started = true;
    if (card && cards.length < BRIEFING_MAX_CARDS) {
      cards.push(line);
    } else if (!BRIEFING_RULE_LINE.test(line) && /[\p{L}\p{N}]/u.test(line)) {
      // Kept as written (trailing space off), so a child item or continuation
      // keeps its place under its parent in the note block.
      notes.push(raw.replace(/\s+$/, ''));
    }
  }
  // Indentation every note line shares is not structure: remove it.
  const shared = Math.min(...notes.map((n) => n.length - n.trimStart().length));
  return { cards, notes: shared > 0 ? notes.map((n) => n.slice(shared)) : notes };
}

/** Strip agent housekeeping/noise from the briefing raw text. */
export function cleanBriefingText(rawText: string): string {
  const housekept = (rawText || '')
    .replace(/\r\n?/g, '\n')
    .replace(/^I['\u2019]ll[^\n]*\n/gm, '')
    .replace(/^I will[^\n]*\n/gm, '')
    .replace(/^Now I have[^\n]*/gm, '')
    .replace(/^I now have[^\n]*/gm, '')
    .replace(/^Good —[^\n]*\n/gm, '');
  // FOLLOWUPS chips: the ONE tolerant marker matcher (variants included).
  // Proposal blocks (closed, opened, or an orphan closing sentinel) are
  // machine payload — they leaked into the briefing as the bullets
  // "--VALUE-MAPPING-PROPOSAL---" / "--END-PROPOSAL---" (2026-09-14); the one
  // tolerant stripper lives with the chat parser so the two never drift.
  return cutOpenedProposal(stripProposalBlocks(splitFollowups(housekept).body))
    .replace(/^[ \t]*-{2,}[ \t]*$\n?/gm, '') // bare horizontal rules are not bullets
    .replace(/\[\d+\]/g, '') // strip citation numbers [1][2][3]
    .replace(/\n{3,}/g, '\n\n')
    .trim();
}

export const BRIEFING_STATUS_COLORS: Record<string, string> = {
  line: '#3fb950',
  quality: '#f0883e',
  maintenance: '#d29922',
  order: '#58a6ff',
  action: '#bc8cff',
  default: '#58a6ff',
};

export interface BriefingBullet {
  title: string;
  detail: string;
  color: string;
}

/** Parse one briefing bullet line: strip the list marker (but NOT a markdown
 * bold opener), split "**Title:** detail" / " — " / ": ", pick the keyword color. */
export function parseBriefingBullet(line: string): BriefingBullet {
  // The list marker comes off: `•`, an en dash, `-`, or `*` before a space.
  // A lone `*` before text comes off only when it does not open an italic
  // span closed later on the line (`*item` -> item; `*Line 2* is down` keeps
  // its italic word). A `**` bold opener is never a marker.
  let text = line.trim();
  if (/^(?:[•\u2013-]|\*\s)/.test(text)) {
    text = text.replace(/^(?:[•\u2013-]|\*)\s*/, '');
  } else if (/^\*(?!\*)/.test(text) && !/^\*[^*\s][^*]*\*/.test(text)) {
    text = text.slice(1);
  }
  text = text.trim();
  let title = '';
  let detail = text;
  const boldPrefix = text.match(/^\*\*([^*]+?)\*\*\s*[:\u2014-]?\s*/);
  if (boldPrefix) {
    title = boldPrefix[1].replace(/:\s*$/, '').trim();
    detail = text.slice(boldPrefix[0].length).trim();
  } else {
    const dashIdx = text.indexOf(' — ');
    const colonIdx = text.indexOf(': ');
    const splitIdx = dashIdx > 0 && (colonIdx < 0 || dashIdx < colonIdx) ? dashIdx : colonIdx;
    if (splitIdx > 0) {
      title = text.slice(0, splitIdx);
      detail = text.slice(
        splitIdx + (text[splitIdx] === ' ' && text[splitIdx + 1] === '—' ? 3 : 2),
      );
    }
  }
  const key =
    Object.keys(BRIEFING_STATUS_COLORS).find((k) => title.toLowerCase().includes(k)) ||
    'default';
  return { title, detail, color: BRIEFING_STATUS_COLORS[key] };
}

/** The card lines of a cleaned briefing (>=3 → styled cards): `splitBriefing`'s
 * cards, at most five (rehearsal, 2026-10-17: a caveat became a sixth card). */
export function briefingBulletLines(clean: string): string[] {
  return splitBriefing(clean).cards;
}

/** The note lines shown under the cards as ONE markdown block (joined with
 * newlines): `splitBriefing`'s notes. */
export function briefingNoteLines(clean: string): string[] {
  return splitBriefing(clean).notes;
}

/** The briefing text from its first card on, for the block render (fewer
 * than three cards). The text before the first card is dropped, the same as
 * `splitBriefing` drops it, so narration written before a rule never shows.
 * With no card at all, the text unchanged (the no-card fallback). */
export function briefingFromFirstCard(clean: string): string {
  const lines = (clean || '').split('\n');
  const first = lines.findIndex((l) => l.trim() && isBriefingCardLine(l.trim()));
  if (first < 0) return clean;
  return lines
    .slice(first)
    .filter((l) => !BRIEFING_RULE_LINE.test(l.trim()))
    .join('\n')
    .trim();
}
