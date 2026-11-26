// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Digital Thread pure helpers — the non-markup logic of the reimagined
 * explorer (classification, pair aggregation, topology layout, heatmap
 * grouping, meta text — topology map with gap callouts, takeaway-led
 * coverage, pipeline trace). The markup builders live as JSX components in
 * ./panels.tsx; everything here is plain data-in/data-out and unit-testable
 * in node. */

import type {
  ConceptCoverage,
  CoverageConcept,
  GraphSummary,
  IdentityRule,
  JoinPathDetail,
  LearnProposal,
  ValueMappingDetail,
} from '../../api/types/thread';
import { isInactiveStatus } from '../../lib/rule-status';
import { systemName } from '../../lib/system-names';

// ── System classification (_sysClass/_sysIcon) ───────────────────────────────

export type SysClass = 'erp' | 'mes' | 'cmms' | 'plm' | 'other';

export function sysClass(name: string | undefined): SysClass {
  const n = (name || '').toLowerCase();
  if (n.includes('erp') || n.includes('sap') || n.includes('oracle')) return 'erp';
  if (n.includes('mes') || n.includes('ignition') || n.includes('factorytalk')) return 'mes';
  if (n.includes('cmms') || n.includes('maximo') || n.includes('sap pm')) return 'cmms';
  if (n.includes('plm') || n.includes('teamcenter')) return 'plm';
  return 'other';
}

const SYS_ICONS: Record<SysClass, string> = { erp: '📦', mes: '⚙️', cmms: '🔧', plm: '📐', other: '🔌' };

/** Legacy emoji helper — kept for callers outside the redesigned views. */
export function sysIcon(name: string | undefined): string {
  return SYS_ICONS[sysClass(name)] || '🔌';
}

/** The POST /api/thread/learn body for a learn proposal: the proposal as the
 * server sent it, which carries the identifier it was learned from and the
 * value that identifier matched (`example_identifier` /
 * `example_matched_value`). When an older server's proposal carries no
 * identifier, the one typed into the trace (`entityValue`, `Type:id`) is
 * sent in its place. Neither: the proposal unchanged. */
export function learnRequestBody(proposal: LearnProposal, entityValue: string): LearnProposal {
  if (proposal.example_identifier) return proposal;
  const typed = entityValue.split(':').slice(1).join(':').trim();
  return typed ? { ...proposal, example_identifier: typed } : proposal;
}

/** Entity-type tag key: Batches→batch, Orders→order (showEntity). */
export function entityTypeKey(type: string): string {
  return type.toLowerCase().replace(/es$/, '').replace(/s$/, '');
}

// ── Overview helpers (loadThreadOverview / _loadTopology) ────────────────────

export interface ThreadSystem {
  id?: string;
  name?: string;
  status?: string;
  plant?: string;
  schema?: string;
  [k: string]: unknown;
}

export function onboardedSystems(systems: ThreadSystem[]): ThreadSystem[] {
  return systems.filter((s) => s.status === 'connected' || s.status === 'onboarded');
}

/** Identity rules + join paths + value mappings (the header-meta rule count).
 * A switched-off identity rule or JoinPath (lib/rule-status) is kept for
 * audit and is not counted. */
export function totalRuleCount(g: GraphSummary): number {
  return (
    activeRules(g.rules || []).length +
    activeRules(g.join_paths_detail || []).length +
    (g.value_mappings_detail || []).length
  );
}

/** The rules that are not switched off. */
export function activeRules<T extends { status?: unknown }>(items: T[]): T[] {
  return items.filter((item) => !isInactiveStatus(item.status));
}

export function metaText(onboardedCount: number, totalRules: number): string {
  return onboardedCount + ' systems · ' + totalRules + ' translation rules';
}

export function emptyMetaText(conceptCount: number): string {
  return conceptCount + ' concepts seeded · 0 systems connected';
}

// ── Topology pair aggregation (_aggregatePairs / _pairRuleCount) ─────────────

export const PAIR_SEP = '||';

export interface TopoPair {
  a: string;
  b: string;
  identity: IdentityRule[];
  joins: JoinPathDetail[];
  values: ValueMappingDetail[];
  maxConf: number;
}

/** Aggregate identity rules + join paths + value mappings into per-pair
 * buckets. Switched-off rules stay in their pair's lists (the drawers show
 * them for audit) but add nothing to its confidence or its count. */
export function aggregatePairs(g: GraphSummary, systems: ThreadSystem[]): Record<string, TopoPair> {
  const canon: Record<string, string> = {};
  systems.forEach((s) => {
    canon[String(s.id || '').toLowerCase()] = String(s.id || '');
    canon[String(s.name || '').toLowerCase()] = String(s.id || '');
  });
  const find = (v: unknown): string | null => canon[String(v || '').toLowerCase()] || null;
  const pairs: Record<string, TopoPair> = {};
  const add = (
    a: unknown,
    b: unknown,
    kind: 'identity' | 'joins' | 'values',
    item: IdentityRule | JoinPathDetail | ValueMappingDetail,
    conf: number | null | undefined,
  ) => {
    const ca = find(a),
      cb = find(b);
    if (!ca || !cb || ca === cb) return;
    const [x, y] = [ca, cb].sort();
    const key = x + PAIR_SEP + y;
    const p = pairs[key] || (pairs[key] = { a: x, b: y, identity: [], joins: [], values: [], maxConf: 0 });
    (p[kind] as unknown[]).push(item);
    if (isInactiveStatus((item as { status?: unknown }).status)) return;
    p.maxConf = Math.max(p.maxConf, conf != null ? conf : 0.7); // join paths carry no confidence
  };
  (g.rules || []).forEach((r) => add(r.source_system, r.target_system, 'identity', r, r.confidence));
  (g.join_paths_detail || []).forEach((j) => add(j.from_system, j.to_system, 'joins', j, null));
  (g.value_mappings_detail || []).forEach((v) => add(v.from_system, v.to_system, 'values', v, v.confidence));
  return pairs;
}

/** The rules linking a pair: switched-off ones are not counted. */
export function pairRuleCount(p: TopoPair): number {
  return activeRules(p.identity).length + activeRules(p.joins).length + p.values.length;
}

/** Keys of the pairs at least one active rule links. */
export function linkedPairKeys(pairs: Record<string, TopoPair>): string[] {
  return Object.keys(pairs).filter((key) => pairRuleCount(pairs[key]) > 0);
}

// ── Identity-rule grouping (concept groups + bidirectional collapse) ─────────

/** One rendered card: a distinct (concept, endpoint-pair) binding. Exact
 * duplicates and A→B / B→A mirrors collapse into a single binding, keeping
 * every underlying rule for the expandable audit detail. */
export interface IdentityBinding {
  concept: string;
  /** Display endpoints — `a` keeps the first-seen rule's source side. */
  a: { system: string; field: string };
  b: { system: string; field: string };
  /** True when both directions (a→b AND b→a) exist among the rules. */
  bidirectional: boolean;
  /** Every underlying graph rule collapsed into this binding (≥1). */
  rules: IdentityRule[];
  /** Highest recorded confidence across the underlying rules (null if none). */
  maxConfidence: number | null;
}

export interface IdentityConceptGroup {
  concept: string;
  bindings: IdentityBinding[];
  /** Total underlying rules in this group (≥ bindings.length). */
  ruleCount: number;
}

function endpointKey(system: unknown, field: unknown): string {
  return String(system || '').toLowerCase() + '.' + String(field || '').toLowerCase();
}

/** Group identity rules by concept, collapsing exact field-pair duplicates and
 * bidirectional mirrors into one binding each. Rules without field metadata
 * (pre-fix API payloads) degrade gracefully to system-grain bindings. */
export function groupIdentityRules(rules: IdentityRule[]): IdentityConceptGroup[] {
  const groups = new Map<string, Map<string, IdentityBinding>>();
  for (const r of rules || []) {
    const concept = String(r.concept || '') || 'other';
    const src = { system: String(r.source_system || ''), field: String(r.source_field || '') };
    const tgt = { system: String(r.target_system || ''), field: String(r.target_field || '') };
    const kSrc = endpointKey(src.system, src.field);
    const kTgt = endpointKey(tgt.system, tgt.field);
    const key = [kSrc, kTgt].sort().join('>>');
    const byBinding = groups.get(concept) || new Map<string, IdentityBinding>();
    groups.set(concept, byBinding);
    const existing = byBinding.get(key);
    if (!existing) {
      byBinding.set(key, {
        concept,
        a: src,
        b: tgt,
        bidirectional: false,
        rules: [r],
        maxConfidence: typeof r.confidence === 'number' ? r.confidence : null,
      });
      continue;
    }
    existing.rules.push(r);
    if (typeof r.confidence === 'number') {
      existing.maxConfidence = existing.maxConfidence == null ? r.confidence : Math.max(existing.maxConfidence, r.confidence);
    }
    // Mirror direction (this rule's source is the binding's target side)?
    if (endpointKey(src.system, src.field) !== endpointKey(existing.a.system, existing.a.field)) {
      existing.bidirectional = true;
    }
  }
  return Array.from(groups.entries())
    .map(([concept, byBinding]) => {
      const bindings = Array.from(byBinding.values()).sort(
        (x, y) => (y.maxConfidence ?? 0) - (x.maxConfidence ?? 0) || endpointKey(x.a.system, x.a.field).localeCompare(endpointKey(y.a.system, y.a.field)),
      );
      return { concept, bindings, ruleCount: bindings.reduce((n, b) => n + b.rules.length, 0) };
    })
    .sort((x, y) => y.ruleCount - x.ruleCount || x.concept.localeCompare(y.concept));
}

/** Display name for a system id: the onboarded list's catalog name, else the
 * shared resolver (lib/system-names.ts — catalog name, else a readable
 * fallback built from the id). */
export function nameById(systems: ThreadSystem[], id: string): string {
  const s = systems.find((x) => x.id === id);
  return (s && String(s.name || '')) || systemName(id) || id;
}

/** Unlinked system pairs as display-name tuples (_renderTopoGaps). */
export function pairGaps(systems: ThreadSystem[], pairs: Record<string, TopoPair>): [string, string][] {
  const gaps: [string, string][] = [];
  for (let i = 0; i < systems.length; i++) {
    for (let j = i + 1; j < systems.length; j++) {
      const key = [String(systems[i].id || ''), String(systems[j].id || '')].sort().join(PAIR_SEP);
      if (!pairs[key] || pairRuleCount(pairs[key]) === 0) {
        gaps.push([systemDisplay(systems[i]), systemDisplay(systems[j])]);
      }
    }
  }
  return gaps;
}

/** A system row's display name: its own `name`, else the shared resolver. */
export function systemDisplay(s: ThreadSystem): string {
  return String(s.name || '') || systemName(String(s.id || '')) || String(s.id || '');
}

/** Node positions on a circle (_topoPositions). */
export function topoPositions(n: number, W: number, H: number): { x: number; y: number }[] {
  const cx = W / 2,
    cy = H / 2;
  if (n === 1) return [{ x: cx, y: cy }];
  const R = Math.min(W, H) / 2 - 64;
  return Array.from({ length: n }, (_, i) => {
    const th = -Math.PI / 2 + (i * 2 * Math.PI) / n;
    return { x: cx + R * Math.cos(th), y: cy + R * Math.sin(th) };
  });
}

// ── Coverage helpers (_conceptSystemCounts / _heatmapGroups / _levelTitle) ───

/** conceptId → number of distinct systems it is mapped in. */
export function conceptSystemCounts(cov: ConceptCoverage): Record<string, number> {
  const bySys: Record<string, Set<string>> = {};
  (cov.coverage || []).forEach((c) => {
    if ((c.roles || []).length) (bySys[c.concept] = bySys[c.concept] || new Set()).add(c.system);
  });
  const counts: Record<string, number> = {};
  Object.keys(bySys).forEach((k) => {
    counts[k] = bySys[k].size;
  });
  return counts;
}

export const ISA95_NAMES: Record<number, string> = {
  0: 'Process',
  1: 'Sensing',
  2: 'Control',
  3: 'Operations',
  4: 'Business',
};

export function levelTitle(level: number | string): string {
  const name = ISA95_NAMES[Number(level)];
  return name ? 'Level ' + level + ' — ' + name : 'Level ' + level;
}

export interface HeatmapGroups {
  ordered: number[];
  groups: Map<number, CoverageConcept[]>;
}

/** Dedupe + group concepts by ISA-95 level; cross-system first within a group. */
export function heatmapGroups(concepts: CoverageConcept[], sysCount: Record<string, number>): HeatmapGroups {
  const seen = new Set<string>();
  const unique = concepts.filter((c) => !seen.has(c.id) && !!seen.add(c.id));
  const groups = new Map<number, CoverageConcept[]>();
  unique.forEach((c) => {
    const lvl = Number.isFinite(c.level) ? (c.level as number) : -1;
    if (!groups.has(lvl)) groups.set(lvl, []);
    groups.get(lvl)!.push(c);
  });
  const ordered = [...groups.keys()].sort((a, b) => a - b);
  ordered.forEach((lvl) =>
    groups
      .get(lvl)!
      .sort((a, b) => (sysCount[b.id] || 0) - (sysCount[a.id] || 0) || (a.name || '').localeCompare(b.name || '')),
  );
  return { ordered, groups };
}

// ── Trace helpers ─────────────────────────────────────────────────────────────

/** Per-step UI state driven by the learn/dismiss actions (React-held). */
export interface StepUiState {
  learning?: boolean;
  failed?: string;
  learned?: boolean;
  dismissed?: boolean;
}

/** Token match a concept id against entity-select option values (traceFromImpact). */
export function matchEntityOption(conceptId: string, optionValues: string[]): string | null {
  const tokens = String(conceptId)
    .toLowerCase()
    .split(/[^a-z0-9]+/)
    .filter((t) => t.length > 2);
  for (const value of optionValues) {
    if (!value) continue;
    const v = value.toLowerCase();
    if (tokens.some((t) => v.includes(t))) return value;
  }
  return null;
}
