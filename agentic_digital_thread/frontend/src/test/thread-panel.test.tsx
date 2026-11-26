// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tests for the thread module (src/shared/thread/*) — the Digital Thread
 * explorer (topology map with gap callouts, takeaway-led
 * coverage, pipeline trace) + parseAnswerBlocks. Pure helpers are
 * string/data-asserted; markup components are rendered-DOM assertions over
 * ./panels.tsx (contractual classes: topo-*, cov-*, impact-*, path-card,
 * res-step, learn-*, raw-sys-*, unified-*, trace-*); <ThreadPanel/> is
 * integration-tested through its injectable fetchFn. */
// @vitest-environment jsdom
import { notifyManager } from '@tanstack/react-query';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterAll, afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { queryKeys } from '../api/query-keys';
import {
  _resetSystemsQueryClient,
  getSystemsQueryClient,
} from '../api/systems-query';
import { parseAnswerBlocks } from '../shared/thread/answer-blocks';
import { loadSystems } from '../views/dashboard/systems';
import {
  aggregatePairs,
  conceptSystemCounts,
  emptyMetaText,
  entityTypeKey,
  groupIdentityRules,
  heatmapGroups,
  levelTitle,
  matchEntityOption,
  metaText,
  nameById,
  onboardedSystems,
  pairGaps,
  pairRuleCount,
  sysClass,
  sysIcon,
  topoPositions,
  totalRuleCount,
  PAIR_SEP,
} from '../shared/thread/html';
import { ThreadPanel } from '../shared/thread/ThreadPanel';
import {
  ConfBar,
  CovBanner,
  CoverageHeatmap,
  CovLegend,
  GraphwideBadges,
  GraphwideBody,
  IdentityRulesSection,
  ImpactConcept,
  ImpactCorrelations,
  ImpactPaths,
  ImpactReachable,
  ImpactSystems,
  NodeDrawer,
  PairDrawer,
  RankedPaths,
  RawCards,
  ResolutionSteps,
  ThreadEmpty,
  TopoGaps,
  TopoHeadline,
  TopoPairChips,
  TopoSvg,
  UnifiedCard,
  VocabPanel,
} from '../shared/thread/panels';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
notifyManager.setNotifyFunction((callback) => act(callback));

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  _resetSystemsQueryClient();
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function render(el: React.ReactElement) {
  act(() => {
    root.render(el);
  });
}

async function renderAsync(el: React.ReactElement) {
  await act(async () => {
    root.render(el);
  });
  await vi.waitFor(() => {
    expect(document.getElementById('topo-loading')).toBeNull();
    expect(document.getElementById('cov-loading')).toBeNull();
  });
}

describe('parseAnswerBlocks', () => {
  it('strips proposal blocks and extracts followup/action chips', () => {
    const text =
      'The line is running.\n' +
      '---VALUE-MAPPING-PROPOSAL---\nsecret\n---END-PROPOSAL---\n' +
      '---CORRELATION-PROPOSAL---\nother\n---END-PROPOSAL---\n' +
      '---FOLLOWUPS---\nQ1: What is OEE?\nQ2: Show downtime\nA1: Create a report\n';
    const parsed = parseAnswerBlocks(text);
    expect(parsed.answer).toBe('The line is running.');
    expect(parsed.answer).not.toContain('secret');
    expect(parsed.followups).toEqual(['What is OEE?', 'Show downtime']);
    expect(parsed.actions).toEqual(['Create a report']);
  });

  it('passes through plain answers and handles null', () => {
    expect(parseAnswerBlocks('Just text')).toEqual({ answer: 'Just text', followups: [], actions: [] });
    expect(parseAnswerBlocks(null)).toEqual({ answer: '', followups: [], actions: [] });
  });
});

describe('pure helpers', () => {
  it('sysClass classifies systems by name substrings', () => {
    expect(sysClass('SAP ERP')).toBe('erp');
    expect(sysClass('Ignition MES')).toBe('mes');
    expect(sysClass('IBM Maximo')).toBe('cmms');
    expect(sysClass('Siemens Teamcenter PLM')).toBe('plm');
    expect(sysClass('HighByte')).toBe('other');
    expect(sysClass(undefined)).toBe('other');
  });

  it('sysIcon returns the class-matched icon character', () => {
    expect(sysIcon('SAP ERP')).toBe('📦');
    expect(sysIcon('unknown')).toBe('🔌');
  });

  it('entityTypeKey singularizes the group label (Batches→batch — new /es$/ pass)', () => {
    expect(entityTypeKey('Batches')).toBe('batch');
    expect(entityTypeKey('Orders')).toBe('order');
    expect(entityTypeKey('Products')).toBe('product');
    expect(entityTypeKey('Equipment')).toBe('equipment');
  });

  it('onboardedSystems filters to connected|onboarded', () => {
    const systems = [
      { name: 'A', status: 'connected' },
      { name: 'B', status: 'onboarded' },
      { name: 'C', status: 'pending' },
    ];
    expect(onboardedSystems(systems).map((s) => s.name)).toEqual(['A', 'B']);
  });

  it('totalRuleCount sums rules + join paths + value mappings', () => {
    expect(
      totalRuleCount({
        rules: [{}, {}],
        join_paths_detail: [{}],
        value_mappings_detail: [{}, {}, {}],
      }),
    ).toBe(6);
    expect(totalRuleCount({})).toBe(0);
  });

  it('metaText / emptyMetaText build the header meta', () => {
    expect(metaText(3, 12)).toBe('3 systems · 12 translation rules');
    expect(emptyMetaText(58)).toBe('58 concepts seeded · 0 systems connected');
  });

  it('levelTitle names ISA-95 levels', () => {
    expect(levelTitle(4)).toBe('Level 4 — Business');
    expect(levelTitle(0)).toBe('Level 0 — Process');
    expect(levelTitle('?')).toBe('Level ?');
  });

  it('matchEntityOption token-matches concept ids against option values', () => {
    const options = ['', 'Batches:B-1001', 'Equipment:MIX-01'];
    expect(matchEntityOption('batch', options)).toBe('Batches:B-1001');
    expect(matchEntityOption('equipment-id', options)).toBe('Equipment:MIX-01');
    expect(matchEntityOption('oee', options)).toBeNull(); // 'oee' not in any value
    expect(matchEntityOption('x-y', options)).toBeNull(); // all tokens too short
  });
});

const SYSTEMS = [
  { id: 'erp', name: 'SAP ERP', status: 'connected', plant: 'P1', schema: 'erp' },
  { id: 'mes', name: 'Ignition MES', status: 'connected' },
  { id: 'cmms', name: 'IBM Maximo CMMS', status: 'connected' },
];

const GRAPH = {
  rules: [
    {
      concept: 'batch',
      source_system: 'erp',
      target_system: 'mes',
      source_pattern: 'B-*',
      target_pattern: 'LOT*',
      confidence: 0.92,
    },
  ],
  join_paths_detail: [
    { from_system: 'SAP ERP', from_field: 'order_id', to_system: 'mes', to_field: 'wo_ref', description: 'order join' },
  ],
  value_mappings_detail: [
    { concept: 'uom', from_system: 'erp', from_value: 'KG', to_system: 'mes', to_value: 'kg', confidence: 0.6 },
  ],
  authority_rules_detail: [{ concept: 'batch', rule: 'ERP wins' }],
  correlations_detail: [{ pattern: 'temp↔scrap', confidence: 0.7 }],
  decision_boundaries_detail: [{ concept: 'oee', threshold: 0.6, action: 'alert' }],
};

describe('topology pair aggregation', () => {
  it('aggregatePairs buckets rules per canonical sorted pair (id OR name match)', () => {
    const pairs = aggregatePairs(GRAPH, SYSTEMS);
    const key = 'erp' + PAIR_SEP + 'mes';
    expect(Object.keys(pairs)).toEqual([key]);
    const p = pairs[key];
    expect(p.identity).toHaveLength(1); // by id
    expect(p.joins).toHaveLength(1); // 'SAP ERP' canonicalized by name
    expect(p.values).toHaveLength(1);
    expect(pairRuleCount(p)).toBe(3);
    expect(p.maxConf).toBe(0.92);
  });

  it('join paths carry no confidence — default 0.7 drives maxConf', () => {
    const pairs = aggregatePairs({ join_paths_detail: GRAPH.join_paths_detail }, SYSTEMS);
    expect(pairs['erp' + PAIR_SEP + 'mes'].maxConf).toBe(0.7);
  });

  it('skips unknown systems and self-pairs', () => {
    const pairs = aggregatePairs(
      {
        rules: [
          { source_system: 'ghost', target_system: 'erp' },
          { source_system: 'erp', target_system: 'SAP ERP' }, // self via alias
        ],
      },
      SYSTEMS,
    );
    expect(Object.keys(pairs)).toHaveLength(0);
  });

  it('groupIdentityRules collapses exact field-pair duplicates into one binding', () => {
    const groups = groupIdentityRules([
      { id: 'ir-1', concept: 'batch', source_system: 'sw', source_field: 'header_sourceId', target_system: 'ath', target_field: 'cell', confidence: 0.8 },
      { id: 'ir-2', concept: 'batch', source_system: 'sw', source_field: 'header_sourceId', target_system: 'ath', target_field: 'cell', confidence: 0.8 },
      { id: 'ir-3', concept: 'batch', source_system: 'sw', source_field: 'device_id', target_system: 'ath', target_field: 'cell', confidence: 0.8 },
    ]);
    expect(groups).toHaveLength(1);
    expect(groups[0].concept).toBe('batch');
    expect(groups[0].ruleCount).toBe(3);
    expect(groups[0].bindings).toHaveLength(2);
    const dup = groups[0].bindings.find((b) => b.rules.length === 2)!;
    expect(dup.a.field).toBe('header_sourceId');
    expect(dup.bidirectional).toBe(false);
  });

  it('groupIdentityRules collapses A→B / B→A mirrors as bidirectional', () => {
    const groups = groupIdentityRules([
      { id: 'f', concept: 'batch', source_system: 'ath', source_field: 'cell', target_system: 'sw', target_field: 'line_name', confidence: 1.0 },
      { id: 'r', concept: 'batch', source_system: 'sw', source_field: 'line_name', target_system: 'ath', target_field: 'cell', confidence: 0.8 },
    ]);
    expect(groups[0].bindings).toHaveLength(1);
    const b = groups[0].bindings[0];
    expect(b.bidirectional).toBe(true);
    expect(b.rules).toHaveLength(2);
    expect(b.maxConfidence).toBe(1.0);
    // Display keeps the first-seen rule's direction as side `a`.
    expect(b.a).toEqual({ system: 'ath', field: 'cell' });
  });

  it('groupIdentityRules groups by concept and degrades to system grain without fields', () => {
    const groups = groupIdentityRules([
      { concept: 'batch', source_system: 'erp', target_system: 'mes', confidence: 0.9 },
      { concept: 'batch', source_system: 'erp', target_system: 'mes', confidence: 0.7 },
      { concept: 'production-line', source_system: 'erp', target_system: 'mes', confidence: 0.6 },
    ]);
    expect(groups.map((g) => g.concept)).toEqual(['batch', 'production-line']); // rule-count desc
    expect(groups[0].bindings).toHaveLength(1); // system-grain collapse
    expect(groups[0].bindings[0].rules).toHaveLength(2);
    expect(groups[0].bindings[0].maxConfidence).toBe(0.9);
  });

  it('pairGaps lists unlinked pairs by display name', () => {
    const pairs = aggregatePairs(GRAPH, SYSTEMS);
    expect(pairGaps(SYSTEMS, pairs)).toEqual([
      ['SAP ERP', 'IBM Maximo CMMS'],
      ['Ignition MES', 'IBM Maximo CMMS'],
    ]);
  });

  it('nameById resolves display names', () => {
    expect(nameById(SYSTEMS, 'mes')).toBe('Ignition MES');
    // Unknown id: the shared resolver's readable fallback (lib/system-names.ts).
    expect(nameById(SYSTEMS, 'nope')).toBe('Nope');
  });

  it('topoPositions centers a single node and rings n nodes starting at top', () => {
    expect(topoPositions(1, 860, 440)).toEqual([{ x: 430, y: 220 }]);
    const pos = topoPositions(4, 860, 440);
    expect(pos).toHaveLength(4);
    // first node at -90° → top center (R = 440/2 - 64 = 156)
    expect(pos[0].x).toBeCloseTo(430);
    expect(pos[0].y).toBeCloseTo(220 - 156);
  });
});

describe('coverage grouping helpers', () => {
  const COV = {
    concepts: [
      { id: 'c2', name: 'Zeta', level: 2 },
      { id: 'c1', name: 'Alpha', level: 2 },
      { id: 'c1', name: 'Alpha', level: 2 }, // duplicate
      { id: 'c3', name: 'NoLevel' },
    ],
    systems: [
      { id: 's1', name: 'SAP ERP' },
      { id: 's2', name: 'Ignition MES' },
    ],
    coverage: [
      { concept: 'c1', system: 's1', roles: [{ role: 'identifier', confidence: 0.9 }] },
      { concept: 'c1', system: 's2', roles: [{ role: 'measure', confidence: 0.5 }] },
      { concept: 'c2', system: 's1', roles: [{ role: 'measure' }] },
      { concept: 'c3', system: 's1', roles: [] }, // empty roles → not counted
    ],
  };

  it('conceptSystemCounts counts distinct systems with non-empty roles', () => {
    expect(conceptSystemCounts(COV)).toEqual({ c1: 2, c2: 1 });
  });

  it('heatmapGroups dedupes, groups by level (non-finite → -1), cross-system first', () => {
    const { ordered, groups } = heatmapGroups(COV.concepts, conceptSystemCounts(COV));
    expect(ordered).toEqual([-1, 2]);
    expect(groups.get(-1)!.map((c) => c.id)).toEqual(['c3']);
    // within level 2: c1 (2 systems) before c2 (1 system) despite Alpha/Zeta alpha order
    expect(groups.get(2)!.map((c) => c.id)).toEqual(['c1', 'c2']);
  });
});

describe('topology components', () => {
  const pairs = aggregatePairs(GRAPH, SYSTEMS);
  const pairKey = 'erp' + PAIR_SEP + 'mes';

  it('<TopoHeadline/> renders the three stats with "of possible"', () => {
    render(<TopoHeadline systemCount={3} totalRules={3} linkedCount={1} possible={3} />);
    const nums = Array.from(container.querySelectorAll('.topo-stat-num')).map((e) => e.textContent);
    expect(nums).toEqual(['3', '3', '1 of 3']);
    const labels = Array.from(container.querySelectorAll('.topo-stat-label')).map((e) => e.textContent);
    expect(labels).toEqual(['Systems', 'Translation rules', 'System pairs linked']);
    expect(container.querySelector('.topo-stat-of')!.textContent).toBe(' of 3');
  });

  it('<TopoPairChips/> renders chips with counts; click opens the pair', () => {
    const onOpenPair = vi.fn();
    render(<TopoPairChips systems={SYSTEMS} pairs={pairs} selectedPair={pairKey} onOpenPair={onOpenPair} />);
    const chip = container.querySelector('.topo-pair-chip') as HTMLButtonElement;
    expect(chip.className).toBe('topo-pair-chip sel');
    expect(chip.getAttribute('data-pair')).toBe(pairKey);
    expect(chip.textContent).toBe('SAP ERP ↔ Ignition MES3');
    expect(chip.querySelector('.topo-chip-count')!.textContent).toBe('3');
    act(() => chip.click());
    expect(onOpenPair).toHaveBeenCalledWith(pairKey);
  });

  it('<TopoGaps/> lists unlinked pairs; null when fully linked', () => {
    render(<TopoGaps systems={SYSTEMS} pairs={pairs} />);
    expect(container.querySelector('.topo-gaps-label')!.textContent).toContain('Gaps');
    expect(container.querySelector('.topo-gaps-text')!.textContent).toBe(
      'No translation rules link SAP ERP ↔ IBM Maximo CMMS, Ignition MES ↔ IBM Maximo CMMS. Questions spanning these systems cannot be answered yet.',
    );
    const two = SYSTEMS.slice(0, 2);
    render(<TopoGaps systems={two} pairs={pairs} />);
    expect(container.querySelector('.topo-gaps-label')).toBeNull();
  });

  it('<TopoSvg/> renders nodes, edges, badges; clicks route to pair/node handlers', () => {
    const onOpenPair = vi.fn();
    const onOpenNode = vi.fn();
    render(
      <TopoSvg
        systems={SYSTEMS}
        pairs={pairs}
        selectedPair={pairKey}
        selectedSys="cmms"
        onOpenPair={onOpenPair}
        onOpenNode={onOpenNode}
      />,
    );
    const svg = container.querySelector('svg')!;
    expect(svg.getAttribute('viewBox')).toBe('0 0 860 440'); // 3 systems → H 440
    expect(svg.getAttribute('aria-label')).toBe('System topology map');
    const nodes = container.querySelectorAll('.topo-node');
    expect(nodes).toHaveLength(3);
    expect(nodes[0].classList.contains('sys-erp')).toBe(true);
    expect(nodes[0].querySelector('title')!.textContent).toBe('SAP ERP');
    expect(nodes[2].classList.contains('sel')).toBe(true); // selectedSys=cmms
    const edge = container.querySelector('.topo-edge') as SVGElement;
    expect(edge.classList.contains('hi')).toBe(true); // maxConf 0.92
    expect(edge.classList.contains('sel')).toBe(true);
    expect(edge.getAttribute('stroke-width')).toBe('3.6'); // 1.5 + 3*0.7
    const badge = container.querySelector('.topo-badge')!;
    expect(badge.querySelector('text')!.textContent).toBe('3');
    act(() => badge.dispatchEvent(new MouseEvent('click', { bubbles: true })));
    expect(onOpenPair).toHaveBeenCalledWith(pairKey);
    act(() => nodes[1].dispatchEvent(new MouseEvent('click', { bubbles: true })));
    expect(onOpenNode).toHaveBeenCalledWith('mes');
  });

  it('<TopoSvg/> truncates long node labels at 17 chars', () => {
    const longSys = [{ id: 'a', name: 'Very Long System Name Indeed', status: 'connected' }];
    render(
      <TopoSvg systems={longSys} pairs={{}} selectedPair={null} selectedSys={null} onOpenPair={() => {}} onOpenNode={() => {}} />,
    );
    expect(container.querySelector('.topo-node text')!.textContent).toBe('Very Long System…');
    expect(container.querySelector('svg')!.getAttribute('viewBox')).toBe('0 0 860 280'); // ≤2 systems
  });

  it('<PairDrawer/> renders head + identity/join/value rule sections with conf bars', () => {
    const onClose = vi.fn();
    render(<PairDrawer pair={pairs[pairKey]} systems={SYSTEMS} onClose={onClose} />);
    expect(container.querySelector('.drawer-title')!.textContent).toBe('SAP ERP ↔ Ignition MES');
    expect(container.querySelector('.drawer-sub')!.textContent).toBe('3 rules link this pair');
    const sections = Array.from(container.querySelectorAll('.rule-section-title')).map((e) => e.textContent);
    expect(sections).toEqual(['Identity rules 1', 'Join paths 1', 'Value mappings 1']);
    // Identity rules render concept-grouped expandable binding cards.
    const group = container.querySelector('.identity-concept-group')!;
    expect(group.querySelector('.rule-concept')!.textContent).toBe('batch');
    expect(group.querySelector('.identity-concept-count')!.textContent).toBe('1 binding');
    const idCard = group.querySelector('.identity-rule-card')!;
    expect(idCard.querySelector('.identity-rule-phrase')!.textContent).toBe('B-* → LOT*');
    // System ids read as names (no catalog loaded here: the id fallback).
    expect(idCard.querySelector('.rule-syspair')!.textContent).toBe('ERP → MES');
    const fill = idCard.querySelector('.conf-fill') as HTMLElement;
    expect(fill.className).toBe('conf-fill hi');
    expect(fill.style.width).toBe('92%');
    expect(idCard.querySelector('.conf-pct')!.textContent).toBe('92%');
    // The legend explains confidence-as-provenance in BOTH drawers now.
    expect(container.querySelector('.identity-confidence-legend')).not.toBeNull();
    act(() => (container.querySelector('.impact-close-btn') as HTMLButtonElement).click());
    expect(onClose).toHaveBeenCalled();
  });

  it('<NodeDrawer/> aggregates the system\'s rules; notes when none', () => {
    render(<NodeDrawer system={SYSTEMS[0]} pairs={pairs} onClose={() => {}} />);
    expect(container.querySelector('.drawer-title')!.textContent).toBe('SAP ERP');
    expect(container.querySelector('.drawer-sub')!.textContent).toBe('P1 · erp · participates in 3 rules');
    expect(container.querySelector('.drawer-note')).toBeNull();

    render(<NodeDrawer system={SYSTEMS[2]} pairs={pairs} onClose={() => {}} />);
    expect(container.querySelector('.drawer-sub')!.textContent).toBe('participates in 0 rules');
    expect(container.querySelector('.drawer-note')!.textContent).toBe('No translation rules involve this system yet.');
  });

  it('<GraphwideBadges/> renders only non-zero badges', () => {
    render(<GraphwideBadges g={GRAPH} />);
    const badges = Array.from(container.querySelectorAll('.thread-count')).map((e) => e.textContent);
    expect(badges).toEqual(['Authority 1', 'Correlations 1', 'Boundaries 1']);
    render(<GraphwideBadges g={{}} />);
    expect(container.querySelectorAll('.thread-count')).toHaveLength(0);
  });

  it('<GraphwideBody/> authority rows gain deactivate/reactivate actions (T1.4)', () => {
    const onLifecycle = vi.fn();
    const g = {
      authority_rules_detail: [
        { id: 'ar-sys-a-site', concept: 'site', rule: 'sys-a wins', system_id: 'sys-a' },
        {
          id: 'ar-sys-b-site', concept: 'site', rule: 'sys-b wins', system_id: 'sys-b',
          status: 'deactivated', deactivated_by: 'reviewer@example.com',
          deactivated_at: '2026-07-29T00:00:00Z',
        },
      ],
    };
    render(<GraphwideBody g={g} onRuleLifecycle={onLifecycle} />);
    const cards = container.querySelectorAll('.authority-rule-card');
    expect(cards).toHaveLength(2);
    // Active rule → Deactivate action firing the shared lifecycle callback.
    const active = cards[0];
    expect(active.classList.contains('deactivated')).toBe(false);
    const deactivateBtn = active.querySelector('.identity-lifecycle-btn.deactivate') as HTMLButtonElement;
    act(() => deactivateBtn.click());
    // Consequential action: the click opens a confirmation dialog stating the
    // impact — nothing fires until the explicit confirm.
    expect(onLifecycle).not.toHaveBeenCalled();
    const confirmBtn = Array.from(document.querySelectorAll('button')).find(
      (b) => (b.textContent || '').trim() === 'Deactivate rule',
    ) as HTMLButtonElement;
    expect(confirmBtn).toBeTruthy();
    act(() => confirmBtn.click());
    expect(onLifecycle).toHaveBeenCalledWith(['ar-sys-a-site'], 'deactivate');
    // Deactivated rule → bucketed visibly with its audit stamp + Reactivate.
    const inactive = cards[1];
    expect(inactive.classList.contains('deactivated')).toBe(true);
    expect(inactive.textContent).toContain('reviewer@example.com');
    const reactivateBtn = inactive.querySelector('.identity-lifecycle-btn.reactivate') as HTMLButtonElement;
    act(() => reactivateBtn.click());
    expect(onLifecycle).toHaveBeenCalledWith(['ar-sys-b-site'], 'reactivate');
  });

  it('<GraphwideBody/> without a lifecycle callback renders no authority actions', () => {
    render(<GraphwideBody g={GRAPH} />);
    expect(container.querySelector('.identity-lifecycle-btn')).toBeNull();
  });

  it('<ConfBar/> renders nothing for null and clamps/classifies percentages', () => {
    render(<ConfBar conf={null} />);
    expect(container.querySelector('.conf-row')).toBeNull();
    render(<ConfBar conf={0.4} />);
    expect((container.querySelector('.conf-fill') as HTMLElement).className).toBe('conf-fill lo');
    render(<ConfBar conf={0.6} />);
    expect((container.querySelector('.conf-fill') as HTMLElement).className).toBe('conf-fill med');
  });
});

describe('empty state', () => {
  it('<ThreadEmpty/> renders title, description, and the /#onboard link', () => {
    render(<ThreadEmpty willShow="This view will draw a live map." />);
    expect(container.querySelector('.thread-empty-title')!.textContent).toBe('No systems connected yet');
    expect(container.querySelector('.thread-empty-inner p')!.textContent).toBe('This view will draw a live map.');
    const link = container.querySelector('a.thread-onboard-link') as HTMLAnchorElement;
    expect(link.getAttribute('href')).toBe('/#onboard');
    expect(link.textContent).toBe('Onboard a system →');
    expect(container.querySelector('.thread-empty-icon svg')).not.toBeNull();
  });

  it('<ThreadEmpty/> onNavigate intercepts the link click', () => {
    const onNavigate = vi.fn();
    render(<ThreadEmpty willShow="w" onNavigate={onNavigate} />);
    const link = container.querySelector('a.thread-onboard-link') as HTMLAnchorElement;
    act(() => {
      link.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
    });
    expect(onNavigate).toHaveBeenCalledTimes(1);
  });
});

describe('coverage components', () => {
  const VOCAB = {
    total_concepts: 10,
    mapped_concepts: 6,
    coverage_pct: 60,
    total_fields: 40,
    by_source: [
      { source: 'core', concepts: 5 },
      { source: 'pack:pharma', concepts: 3 },
      { source: 'emergent', concepts: 2 },
    ],
  };
  const COV = {
    concepts: [
      { id: 'c1', name: 'Batch', level: 4 },
      { id: 'c2', name: 'Temperature', level: 1 },
    ],
    systems: [
      { id: 's1', name: 'SAP ERP' },
      { id: 's2', name: 'A Very Long MES Name' },
    ],
    coverage: [
      { concept: 'c1', system: 's1', roles: [{ role: 'identifier', confidence: 0.9 }] },
      { concept: 'c1', system: 's2', roles: [{ role: 'measure', confidence: 0.5 }, { role: 'derive', confidence: 0.4 }] },
    ],
  };

  it('<CovBanner/> shows the cross-system hero + mapped/unmapped stats', () => {
    render(<CovBanner vocab={VOCAB} cov={COV} />);
    expect(container.querySelector('.cov-hero-num')!.textContent).toBe('1'); // c1 in 2 systems
    expect(container.querySelector('.cov-hero-label')!.textContent).toContain('Cross-system concepts');
    const stats = container.querySelector('.cov-banner-stats')!;
    expect(stats.textContent).toContain('6 of 10 concepts mapped (60%)');
    expect(stats.textContent).toContain('4 unmapped'); // fallback total - mapped
  });

  it('<CovBanner/> prefers an explicit unmapped_count', () => {
    render(<CovBanner vocab={VOCAB} cov={{ ...COV, unmapped_count: 7 }} />);
    expect(container.querySelector('.cov-banner-stats')!.textContent).toContain('7 unmapped');
  });

  it('<VocabPanel/> renders the source bar segments and legend (new form)', () => {
    render(<VocabPanel vocab={VOCAB} />);
    expect((container.querySelector('.vocab-source-seg.core') as HTMLElement).style.width).toBe('50%');
    expect((container.querySelector('.vocab-source-seg.pack') as HTMLElement).style.width).toBe('30%');
    expect((container.querySelector('.vocab-source-seg.emergent') as HTMLElement).style.width).toBe('20%');
    const legend = container.querySelector('.vocab-source-legend')!;
    expect(legend.querySelector('.core')!.textContent).toBe('Core 5');
    expect(legend.querySelector('.pack')!.textContent).toBe('Packs 3');
    expect(legend.querySelector('.emergent')!.textContent).toBe('Emergent 2');
    expect(legend.querySelector('.vocab-fields')!.textContent).toBe('40 fields');
  });

  it('<VocabPanel/> hides zero pack/emergent legend entries', () => {
    render(<VocabPanel vocab={{ total_concepts: 5, by_source: [{ source: 'core', concepts: 5 }] }} />);
    const legend = container.querySelector('.vocab-source-legend')!;
    expect(legend.querySelector('.pack')).toBeNull();
    expect(legend.querySelector('.emergent')).toBeNull();
  });

  it('<CoverageHeatmap/> groups by ISA-95 level with cross tags, dots, legend', () => {
    const onSelect = vi.fn();
    // CovLegend moved out of CoverageHeatmap: the ThreadPanel host renders it
    // ABOVE the scrolling heatmap so it stays visible — mirror that here.
    render(
      <>
        <CovLegend />
        <CoverageHeatmap data={COV} activeConceptId="c1" onSelectConcept={onSelect} />
      </>,
    );
    const grid = container.querySelector('.cov-grid') as HTMLElement;
    expect(grid.style.gridTemplateColumns).toBe('minmax(190px,300px) repeat(2,minmax(34px,1fr))');
    // rotated system headers with slug truncation at 14
    const heads = Array.from(container.querySelectorAll('.cov-syshead span')).map((e) => e.textContent);
    expect(heads).toEqual(['SAP ERP', 'A Very Long M…']);
    // level heads: L1 (Sensing) before L4 (Business)
    const levels = Array.from(container.querySelectorAll('.cov-level-head')).map((e) => e.textContent);
    expect(levels).toEqual(['Level 1 — Sensing', 'Level 4 — Business']);
    // row labels: Temperature (L1) first; Batch cross-tagged ×2 + active
    const rows = container.querySelectorAll('.cov-row-label');
    expect(rows[0].textContent).toBe('Temperature');
    expect(rows[1].textContent).toBe('Batch×2');
    expect(rows[1].className).toContain('active');
    expect(rows[1].className).toContain('cross');
    expect(rows[1].querySelector('.cov-cross-tag')!.textContent).toBe('×2');
    // cells: identifier dot with confidence opacity; empty cell 'none' dot
    const cells = container.querySelectorAll('.cov-cell');
    expect(cells).toHaveLength(4); // 2 concepts × 2 systems
    const idCell = Array.from(cells).find((c) => c.getAttribute('title') === 'SAP ERP: identifier 90%')!;
    const dot = idCell.querySelector('.cov-dot.identifier') as HTMLElement;
    expect(dot.style.opacity).toBe('0.94');
    const multiCell = Array.from(cells).find((c) => (c.getAttribute('title') || '').includes('measure 50%'))!;
    expect(multiCell.getAttribute('title')).toBe('A Very Long MES Name: measure 50%, derive 40%');
    expect(multiCell.querySelectorAll('.cov-dot')).toHaveLength(2);
    const emptyCell = Array.from(cells).find((c) => (c.getAttribute('title') || '').endsWith('no mapping'))!;
    expect(emptyCell.querySelector('.cov-dot.none')).not.toBeNull();
    // legend: 5 role items + note
    expect(container.querySelectorAll('.coverage-legend-item')).toHaveLength(6);
    expect(container.querySelector('.cov-legend-note')!.textContent).toContain('dot opacity = confidence');
    // click a row label → impact
    act(() => rows[1].dispatchEvent(new MouseEvent('click', { bubbles: true })));
    expect(onSelect).toHaveBeenCalledWith('c1');
  });

  it('<CoverageHeatmap/> hover applies .hl to the row/col and reports via onHover', () => {
    const onHover = vi.fn();
    render(
      <CoverageHeatmap data={COV} activeConceptId={null} onSelectConcept={() => {}} hover={{ row: 0, col: null }} onHover={onHover} />,
    );
    const rows = container.querySelectorAll('.cov-row-label');
    expect(rows[0].className).toContain('hl'); // row 0 = Temperature
    expect(rows[1].className).not.toContain('hl');
    act(() => rows[1].dispatchEvent(new MouseEvent('mouseover', { bubbles: true })));
    expect(onHover).toHaveBeenCalledWith({ row: 1, col: null });
  });

  it('<CoverageHeatmap/> empty case renders the drawer note', () => {
    render(<CoverageHeatmap data={{}} activeConceptId={null} onSelectConcept={() => {}} />);
    expect(container.textContent).toContain('No concept mappings yet — they appear as systems are onboarded.');
    expect(container.querySelector('.cov-grid')).toBeNull();
  });
});

describe('impact components', () => {
  const IMPACT = {
    concept: { name: 'Batch' },
    hop_summary: { hop_1_systems: 2, hop_2_systems: 1, hop_3_concepts: 4 },
    direct_systems: [{ system_id: 's1', system_name: 'ERP', fields: [{ name: 'batch_no', role: 'identifier' }] }],
    join_paths: [],
    reachable_concepts: [{ concept_name: 'OEE', via_system: 'MES', role: 'derive' }],
    correlations: [{ pattern: 'x↔y', confidence: 0.66 }],
    boundaries: [{ rule: 'r', threshold: 5, action: 'alert' }],
  };

  it('<ImpactConcept/> renders the takeaway sentence with plurals', () => {
    render(<ImpactConcept d={IMPACT} />);
    expect(container.textContent).toBe('Batch lives in 2 systems, joins across 0 paths, and reaches 4 more concepts');
    expect(container.querySelector('.impact-name')!.textContent).toBe('Batch');
    render(
      <ImpactConcept
        d={{ concept: { name: 'X' }, hop_summary: { hop_1_systems: 1, hop_3_concepts: 1 }, join_paths: [{ from_sys: 'a' }] }}
      />,
    );
    expect(container.textContent).toBe('X lives in 1 system, joins across 1 path, and reaches 1 more concept');
  });

  it('<ImpactConcept/> falls back to list lengths without hop_summary', () => {
    render(<ImpactConcept d={{ concept: { name: 'Y' }, direct_systems: IMPACT.direct_systems, reachable_concepts: [] }} />);
    expect(container.textContent).toBe('Y lives in 1 system, joins across 0 paths, and reaches 0 more concepts');
  });

  it('columns carry the takeaway titles: Lives in / Joins via / Unlocks', () => {
    render(<ImpactSystems d={IMPACT} />);
    expect(container.querySelector('.impact-col-title')!.textContent).toBe('Lives in');
    expect(container.querySelector('.impact-node.system .node-name')!.textContent).toBe('ERP');
    expect(container.querySelector('.impact-node.system .node-detail')!.textContent).toBe('I:batch_no');

    render(<ImpactPaths d={IMPACT} />);
    expect(container.querySelector('.impact-col-title')!.textContent).toBe('Joins via');
    expect(container.querySelector('.drawer-note')!.textContent).toBe('No join paths from these systems');

    render(<ImpactReachable d={IMPACT} />);
    expect(container.querySelector('.impact-col-title')!.textContent).toBe('Unlocks');
    expect(container.querySelector('.impact-node.concept .node-detail')!.textContent).toBe('via MES · derive');

    render(<ImpactSystems d={{ concept: { name: 'Z' } }} />);
    expect(container.querySelector('.drawer-note')!.textContent).toBe('No direct systems');
  });

  it('<ImpactCorrelations/> renders correlation and boundary cards', () => {
    render(<ImpactCorrelations d={IMPACT} />);
    const corr = container.querySelector('.impact-corr-card')!;
    expect(corr.querySelector('.corr-label')!.textContent).toBe('Correlation');
    expect(corr.querySelector('.corr-conf')!.textContent).toBe('confidence: 66%');
    const boundary = container.querySelector('.impact-boundary-card')!;
    expect(boundary.querySelector('.boundary-label')!.textContent).toBe('Decision boundary');
    expect(boundary.textContent).toContain('threshold: 5 → alert');
  });

  it('<RankedPaths/> replaces "Joins via" with ranked resolution paths', () => {
    render(
      <RankedPaths
        fromSys="erp"
        toSys="mes"
        d={{
          total_paths: 2,
          direct_paths: [{ concept: 'batch', source_pattern: 'B-*', target_pattern: 'LOT*', confidence: 0.9 }],
          indirect_paths: [{ aggregate_confidence: 0.4, hops: [{ via_system: 'cmms', concept: 'equipment' }] }],
        }}
      />,
    );
    expect(container.querySelector('.impact-col-title')!.textContent).toBe('Joins via — 2 resolution paths');
    const cards = container.querySelectorAll('.path-card');
    expect(cards).toHaveLength(2);
    expect(cards[0].querySelector('.path-rank')!.textContent).toBe('Direct · batch');
    expect(cards[0].querySelector('code')!.textContent).toBe('B-* → LOT*');
    const directFill = cards[0].querySelector('.path-conf-fill') as HTMLElement;
    expect(directFill.className).toBe('path-conf-fill high');
    expect(directFill.style.width).toBe('90%');
    expect(cards[1].querySelector('.path-rank')!.textContent).toBe('Indirect · 1 hops');
    expect(cards[1].querySelector('.path-via')!.textContent).toBe('(equipment)');
    expect((cards[1].querySelector('.path-conf-fill') as HTMLElement).className).toBe('path-conf-fill med');
  });

  it('<RankedPaths/> singularizes one path', () => {
    render(<RankedPaths fromSys="a" toSys="b" d={{ total_paths: 1, direct_paths: [] }} />);
    expect(container.querySelector('.impact-col-title')!.textContent).toBe('Joins via — 1 resolution path');
  });
});

describe('trace components', () => {
  it('<RawCards/> renders per-system cards (header holds the name only)', () => {
    render(<RawCards raw={[{ system: 'SAP ERP', fields: [{ label: 'Batch', value: 'B-1001' }] }]} />);
    const card = container.querySelector('.raw-sys-card') as HTMLElement;
    expect(card.getAttribute('data-sys')).toBe('erp');
    const header = card.querySelector('.raw-sys-header')!;
    expect(header.querySelector('.raw-sys-name')!.className).toBe('raw-sys-name erp');
    expect(header.textContent).toBe('SAP ERP'); // reimagined header drops the emoji icon
    const field = card.querySelector('.raw-field')!;
    expect(field.querySelector('.label')!.textContent).toBe('Batch');
    expect(field.querySelector('.value')!.textContent).toBe('B-1001');
  });

  it('<ResolutionSteps/>: numbered pipeline steps, SVG status icons, brute badge, callbacks', () => {
    const onLearn = vi.fn();
    const onDismiss = vi.fn();
    const proposal = { source_pattern: 'B-{n}', target_pattern: 'LOT{n}', confidence: 0.85 };
    render(
      <ResolutionSteps
        resolution={[
          { rule: 'Brute-force scan', transform: 'B-* → LOT*', check: 'matched 3 rows', status: 'fuzzy', proposal },
          { rule: 'Direct', transform: 'id', check: 'ok', status: 'matched' },
        ]}
        onLearn={onLearn}
        onDismiss={onDismiss}
      />,
    );
    const steps = container.querySelectorAll('.res-step');
    expect(steps).toHaveLength(2);
    expect(steps[0].className).toBe('res-step active');
    expect(steps[1].className).toBe('res-step done');
    expect(steps[0].querySelector('.res-step-num')!.textContent).toBe('1');
    expect(steps[1].querySelector('.res-step-num')!.textContent).toBe('2');
    expect(steps[0].querySelector('.res-step-body')).not.toBeNull();
    expect(steps[0].querySelector('.res-brute-badge')!.textContent).toBe('brute-force');
    const status = steps[0].querySelector('.res-step-status')!;
    expect(status.className).toBe('res-step-status fuzzy');
    expect(status.querySelector('.res-status-icon.fuzzy svg')).not.toBeNull();
    expect(status.textContent).toContain('matched 3 rows');
    const learn = steps[0].querySelector('.learn-proposal') as HTMLElement;
    expect(learn.id).toBe('proposal-0');
    expect(learn.querySelector('.learn-title svg')).not.toBeNull(); // bulb icon
    expect(learn.querySelector('.learn-conf .conf')!.className).toBe('conf high');
    const create = learn.querySelector('.learn-btn-create') as HTMLButtonElement;
    act(() => create.click());
    expect(onLearn).toHaveBeenCalledWith(proposal, 0);
    act(() => (learn.querySelector('.learn-btn-dismiss') as HTMLButtonElement).click());
    expect(onDismiss).toHaveBeenCalledWith(0);
  });

  it('<ResolutionSteps/>: learned drops the brute badge; dismissed hides proposal; learning disables', () => {
    const steps = [
      {
        rule: 'Brute-force scan',
        transform: 't',
        check: 'c',
        status: 'matched' as const,
        proposal: { source_pattern: 'a', target_pattern: 'b', confidence: 0.5 },
      },
    ];
    const noop = () => {};
    render(<ResolutionSteps resolution={steps} stepStates={{ 0: { learned: true } }} onLearn={noop} onDismiss={noop} />);
    expect(container.querySelector('.res-step')!.className).toBe('res-step active done learned');
    expect(container.querySelector('.res-brute-badge')).toBeNull();

    render(<ResolutionSteps resolution={steps} stepStates={{ 0: { dismissed: true } }} onLearn={noop} onDismiss={noop} />);
    expect(container.querySelector('.learn-proposal')).toBeNull();

    render(<ResolutionSteps resolution={steps} stepStates={{ 0: { learning: true } }} onLearn={noop} onDismiss={noop} />);
    const btn = container.querySelector('.learn-btn-create') as HTMLButtonElement;
    expect(btn.textContent).toBe('Creating...');
    expect(btn.disabled).toBe(true);

    render(<ResolutionSteps resolution={steps} stepStates={{ 0: { failed: 'boom' } }} onLearn={noop} onDismiss={noop} />);
    expect((container.querySelector('.learn-btn-create') as HTMLButtonElement).textContent).toBe('Failed: boom');
  });

  it('<UnifiedCard/> renders the entity id headline + sections', () => {
    render(
      <UnifiedCard
        entityId="B-1001"
        unified={[{ title: 'Identity', fields: [{ label: 'ERP', value: 'B-1001' }] }]}
      />,
    );
    const card = container.querySelector('.unified-entity-card')!;
    expect(card.querySelector('.unified-entity-id')!.textContent).toBe('B-1001');
    const section = card.querySelector('.unified-section')!;
    expect(section.querySelector('.unified-section-title')!.textContent).toBe('Identity');
    expect(section.querySelector('.unified-field .value')!.textContent).toBe('B-1001');
  });
});

// ── <ThreadPanel/> integration through the injectable fetchFn ────────────────

type RouteMap = Record<string, unknown>;

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: '',
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as Response;
}

function fetchStub(routes: RouteMap, calls?: string[]) {
  return vi.fn(async (url: string, _options?: RequestInit) => {
    calls?.push(url);
    const key = Object.keys(routes).find((k) => url.startsWith(k));
    if (!key) throw new Error('Unmocked URL: ' + url);
    return jsonResponse(routes[key]);
  });
}

describe('<ThreadPanel/> integration', () => {
  it('exposes linked tab semantics and roves selection and focus with Arrow/Home/End keys', async () => {
    const fetchFn = fetchStub({
      '/api/systems': { systems: [{ name: 'X', status: 'pending' }] },
      '/api/onboard/concepts': { concepts: [] },
    });
    await renderAsync(<ThreadPanel fetchFn={fetchFn} />);

    const tablist = document.querySelector('[role="tablist"]')!;
    expect(tablist.getAttribute('aria-label')).toBe('Digital Thread views');
    const tabs = Array.from(tablist.querySelectorAll<HTMLButtonElement>('[role="tab"]'));
    const panels = Array.from(document.querySelectorAll<HTMLElement>('[role="tabpanel"]'));
    expect(tabs).toHaveLength(3);
    expect(panels).toHaveLength(3);
    tabs.forEach((tab, index) => {
      expect(tab.getAttribute('aria-controls')).toBe(panels[index].id);
      expect(panels[index].getAttribute('aria-labelledby')).toBe(tab.id);
    });

    const expectSelected = (selectedIndex: number) => {
      expect(tabs.filter((tab) => tab.getAttribute('aria-selected') === 'true')).toHaveLength(1);
      tabs.forEach((tab, index) => {
        expect(tab.getAttribute('aria-selected')).toBe(index === selectedIndex ? 'true' : 'false');
        expect(tab.tabIndex).toBe(index === selectedIndex ? 0 : -1);
      });
      expect(panels[selectedIndex].style.display).not.toBe('none');
    };
    const press = (tab: HTMLButtonElement, key: string) => {
      act(() => {
        tab.dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true }));
      });
    };

    tabs[0].focus();
    expectSelected(0);
    press(tabs[0], 'ArrowRight');
    expectSelected(1);
    expect(document.activeElement).toBe(tabs[1]);
    press(tabs[1], 'End');
    expectSelected(2);
    expect(document.activeElement).toBe(tabs[2]);
    press(tabs[2], 'Home');
    expectSelected(0);
    expect(document.activeElement).toBe(tabs[0]);
    press(tabs[0], 'ArrowLeft');
    expectSelected(2);
    expect(document.activeElement).toBe(tabs[2]);
  });

  it('empty state: no systems → per-tab empty blocks + seeded meta; picker hidden', async () => {
    const fetchFn = fetchStub({
      '/api/systems': { systems: [{ name: 'X', status: 'pending' }] },
      '/api/onboard/concepts': { concepts: new Array(58).fill({}) },
    });
    await renderAsync(<ThreadPanel fetchFn={fetchFn} />);
    expect(document.getElementById('thread-meta')!.textContent).toBe('58 concepts seeded · 0 systems connected');
    expect(document.getElementById('topo-loading')).toBeNull();
    const topoEmpty = document.getElementById('topo-empty')!;
    expect(topoEmpty.style.display).not.toBe('none');
    expect(topoEmpty.textContent).toContain('58 manufacturing concepts are already seeded');
    expect(document.getElementById('cov-empty')!.textContent).toContain('grouped by ISA-95 level');
    expect(document.getElementById('trace-empty')!.textContent).toContain('trace a single entity');
    expect(document.getElementById('trace-picker-bar')!.style.display).toBe('none');
    expect(document.getElementById('trace-intro')!.style.display).toBe('none');
    // no graph-summary call was made
    expect(fetchFn.mock.calls.map((c) => c[0])).not.toContain('/api/thread/graph-summary');
  });


  it('deduplicates /api/systems across ThreadPanel and the imperative dashboard consumer', async () => {
    let resolveSystems!: (response: Response) => void;
    const calls: string[] = [];
    const fetchFn = vi.fn((url: string) => {
      calls.push(url);
      if (url === '/api/systems') {
        return new Promise<Response>((resolve) => { resolveSystems = resolve; });
      }
      if (url === '/api/onboard/concepts') return Promise.resolve(jsonResponse({ concepts: [{}] }));
      if (url === '/api/thread/graph-summary') return Promise.resolve(jsonResponse(GRAPH));
      if (url === '/api/thread/entities') return Promise.resolve(jsonResponse({ Batches: [] }));
      if (url === '/api/thread/analytics/vocabulary') return Promise.resolve(jsonResponse({}));
      if (url === '/api/thread/analytics/concept-coverage') return Promise.resolve(jsonResponse({}));
      return Promise.reject(new Error('Unmocked URL: ' + url));
    });

    act(() => root.render(<ThreadPanel fetchFn={fetchFn} />));
    await vi.waitFor(() => expect(calls.filter((url) => url === '/api/systems')).toHaveLength(1));
    const dashboardLoad = loadSystems();
    expect(calls.filter((url) => url === '/api/systems')).toHaveLength(1);

    resolveSystems(jsonResponse({ systems: SYSTEMS }));
    await dashboardLoad;
    await vi.waitFor(() => {
      expect(document.querySelectorAll('#topo-map .topo-node')).toHaveLength(3);
    });
    expect(calls.filter((url) => url === '/api/systems')).toHaveLength(1);
  });

  it('retains active topology with an aria-busy refresh affordance after systems invalidation', async () => {
    const expandedSystems = [
      ...SYSTEMS,
      { id: 'plm', name: 'Siemens Teamcenter PLM', status: 'connected' },
    ];
    let systemsCalls = 0;
    let graphCalls = 0;
    let resolveSystemsRefresh!: (response: Response) => void;
    const fetchFn = vi.fn((url: string) => {
      if (url === '/api/systems') {
        systemsCalls += 1;
        if (systemsCalls === 1) return Promise.resolve(jsonResponse({ systems: SYSTEMS }));
        return new Promise<Response>((resolve) => {
          resolveSystemsRefresh = resolve;
        });
      }
      if (url === '/api/onboard/concepts') return Promise.resolve(jsonResponse({ concepts: [{}] }));
      if (url === '/api/thread/graph-summary') {
        graphCalls += 1;
        return Promise.resolve(jsonResponse(GRAPH));
      }
      if (url === '/api/thread/entities') return Promise.resolve(jsonResponse({ Batches: [] }));
      if (url === '/api/thread/analytics/vocabulary') return Promise.resolve(jsonResponse({}));
      if (url === '/api/thread/analytics/concept-coverage') return Promise.resolve(jsonResponse({}));
      return Promise.reject(new Error('Unmocked URL: ' + url));
    });

    await renderAsync(<ThreadPanel fetchFn={fetchFn} />);
    expect(document.querySelectorAll('#topo-map .topo-node')).toHaveLength(3);

    let invalidation!: Promise<void>;
    act(() => {
      invalidation = getSystemsQueryClient().invalidateQueries({ queryKey: queryKeys.systems() });
    });
    await vi.waitFor(() => expect(systemsCalls).toBe(2));
    expect(document.querySelectorAll('#topo-map .topo-node')).toHaveLength(3);
    await vi.waitFor(() => {
      expect(document.getElementById('topo-content')?.getAttribute('aria-busy')).toBe('true');
      expect(document.querySelector('#topo-content .thread-refreshing')?.textContent).toContain(
        'Refreshing topology',
      );
    });

    await act(async () => {
      resolveSystemsRefresh(jsonResponse({ systems: expandedSystems }));
      await invalidation;
    });

    await vi.waitFor(() => {
      expect(document.querySelectorAll('#topo-map .topo-node')).toHaveLength(4);
      expect(document.getElementById('topo-content')?.getAttribute('aria-busy')).toBe('false');
    });
    expect(systemsCalls).toBe(2);
    expect(graphCalls).toBe(2);
  });
  it('overview error replaces loading with Retry and retry reaches ready', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    let systemsAttempts = 0;
    const fetchFn = vi.fn(async (url: string, _options?: RequestInit) => {
      if (url === '/api/systems') {
        systemsAttempts += 1;
        return systemsAttempts === 1 ? jsonResponse({ detail: 'temporarily unavailable' }, 503) : jsonResponse({ systems: SYSTEMS });
      }
      if (url === '/api/onboard/concepts') return jsonResponse({ concepts: [{}] });
      if (url === '/api/thread/graph-summary') return jsonResponse(GRAPH);
      if (url === '/api/thread/entities') return jsonResponse({ Batches: [{ id: 'B-1001', label: 'B-1001' }] });
      if (url === '/api/thread/analytics/vocabulary') return jsonResponse({ total_concepts: 1 });
      if (url === '/api/thread/analytics/concept-coverage') return jsonResponse({ concepts: [], systems: [], coverage: [] });
      throw new Error('Unmocked URL: ' + url);
    });

    vi.stubGlobal('fetch', fetchFn);
    await renderAsync(<ThreadPanel />);
    expect(fetchFn.mock.calls[0][1]).toMatchObject({ credentials: 'same-origin' });
    expect(document.getElementById('topo-loading')).toBeNull();
    expect(document.getElementById('topo-content')!.style.display).toBe('none');
    expect(document.querySelector('#thread-tab-graph [role="alert"]')!.textContent).toContain('Error loading knowledge graph');

    await act(async () => {
      (document.querySelector('#thread-tab-graph .async-state--error button') as HTMLButtonElement).click();
    });

    expect(systemsAttempts).toBe(2);
    await vi.waitFor(() => {
      expect(document.querySelector('#thread-tab-graph [role="alert"]')).toBeNull();
      expect(document.getElementById('topo-content')!.style.display).not.toBe('none');
    });
    expect(document.querySelectorAll('#topo-map .topo-node')).toHaveLength(3);
  });

  it('analytics failure keeps ready topology and coverage Retry recovers independently', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    let vocabAttempts = 0;
    const fetchFn = vi.fn(async (url: string) => {
      if (url === '/api/systems') return jsonResponse({ systems: SYSTEMS });
      if (url === '/api/onboard/concepts') return jsonResponse({ concepts: [{}] });
      if (url === '/api/thread/graph-summary') return jsonResponse(GRAPH);
      if (url === '/api/thread/entities') return jsonResponse({ Batches: [] });
      if (url === '/api/thread/analytics/vocabulary') {
        vocabAttempts += 1;
        return vocabAttempts === 1
          ? jsonResponse({ error: 'analytics unavailable' }, 500)
          : jsonResponse({ total_concepts: 10, mapped_concepts: 6, coverage_pct: 60 });
      }
      if (url === '/api/thread/analytics/concept-coverage') {
        return jsonResponse({
          concepts: [{ id: 'batch', name: 'Batch', level: 4 }],
          systems: [{ id: 'erp', name: 'SAP ERP' }],
          coverage: [{ concept: 'batch', system: 'erp', roles: [{ role: 'identifier', confidence: 0.9 }] }],
        });
      }
      throw new Error('Unmocked URL: ' + url);
    });

    await renderAsync(<ThreadPanel fetchFn={fetchFn} />);
    expect(document.getElementById('topo-content')!.style.display).not.toBe('none');
    expect(document.querySelectorAll('#topo-map .topo-node')).toHaveLength(3);

    await act(async () => {
      (document.querySelector('.thread-tab[data-tab="thread-tab-coverage"]') as HTMLButtonElement).click();
    });
    expect(document.getElementById('cov-loading')).toBeNull();
    expect(document.querySelector('#thread-tab-coverage [role="alert"]')!.textContent).toContain('Error loading concept coverage');
    expect(document.getElementById('topo-content')!.style.display).not.toBe('none');

    await act(async () => {
      (document.querySelector('#thread-tab-coverage .async-state--error button') as HTMLButtonElement).click();
    });

    expect(vocabAttempts).toBe(2);
    await vi.waitFor(() => {
      expect(document.querySelector('#thread-tab-coverage .async-state--error')).toBeNull();
      expect(document.getElementById('cov-content')!.style.display).not.toBe('none');
    });
    expect(document.querySelector('#cov-banner .cov-hero-num')).not.toBeNull();
    expect(document.querySelectorAll('#topo-map .topo-node')).toHaveLength(3);
  });

  it('entity non-2xx shows Retry, retry repopulates the select, and resolve non-2xx is an error', async () => {
    let entityAttempts = 0;
    const fetchFn = vi.fn(async (url: string) => {
      if (url === '/api/systems') return jsonResponse({ systems: SYSTEMS });
      if (url === '/api/onboard/concepts') return jsonResponse({ concepts: [{}] });
      if (url === '/api/thread/graph-summary') return jsonResponse(GRAPH);
      if (url === '/api/thread/entities') {
        entityAttempts += 1;
        return entityAttempts === 1
          ? jsonResponse({ detail: 'entity index unavailable' }, 503)
          : jsonResponse({ Batches: [{ id: 'B-1001', label: 'B-1001 — Aspirin' }] });
      }
      if (url === '/api/thread/analytics/vocabulary') return jsonResponse({});
      if (url === '/api/thread/analytics/concept-coverage') return jsonResponse({});
      if (url === '/api/thread/resolve') {
        return jsonResponse({ raw: [{ system: 'Should not render' }] }, 422);
      }
      throw new Error('Unmocked URL: ' + url);
    });

    await renderAsync(<ThreadPanel fetchFn={fetchFn} />);
    await act(async () => {
      (document.querySelector('.thread-tab[data-tab="thread-tab-trace"]') as HTMLButtonElement).click();
    });
    const select = document.getElementById('entity-select') as HTMLSelectElement;
    expect(select.options[0].textContent).toBe('Failed to load entities');

    await act(async () => {
      (document.querySelector('#trace-picker-bar .async-state--error button') as HTMLButtonElement).click();
    });

    expect(entityAttempts).toBe(2);
    await vi.waitFor(() => {
      expect(document.querySelector('#trace-picker-bar .async-state--error')).toBeNull();
      expect(select.querySelector('option[value="Batches:B-1001"]')).not.toBeNull();
    });

    await act(async () => {
      select.value = 'Batches:B-1001';
      select.dispatchEvent(new Event('change', { bubbles: true }));
    });
    expect(document.querySelector('#raw-cards .raw-sys-card')).toBeNull();
    expect(document.getElementById('raw-cards')!.textContent).toContain('Error loading entity');
  });

  it('ready state: topology headline/pairbar/gaps/map render; drawer opens on chip click', async () => {
    const fetchFn = fetchStub({
      '/api/systems': { systems: SYSTEMS },
      '/api/onboard/concepts': { concepts: [{}] },
      '/api/thread/graph-summary': GRAPH,
      '/api/thread/entities': { Batches: [{ id: 'B-1001', label: 'B-1001 — Aspirin' }] },
      '/api/thread/analytics/vocabulary': { total_concepts: 10, mapped_concepts: 6, coverage_pct: 60 },
      '/api/thread/analytics/concept-coverage': {
        concepts: [{ id: 'c1', name: 'Batch', level: 4 }],
        systems: [{ id: 's1', name: 'SAP ERP' }],
        coverage: [{ concept: 'c1', system: 's1', roles: [{ role: 'identifier', confidence: 0.9 }] }],
      },
    });
    await renderAsync(<ThreadPanel fetchFn={fetchFn} />);
    expect(document.getElementById('thread-meta')!.textContent).toBe('3 systems · 3 translation rules');
    // Topology tab is the default active tab
    expect(document.querySelector('.thread-tab.active')!.textContent).toBe('Topology');
    expect(document.getElementById('topo-content')!.style.display).not.toBe('none');
    expect(document.querySelectorAll('#topo-headline .topo-stat')).toHaveLength(3);
    expect(document.querySelectorAll('#topo-pairbar .topo-pair-chip')).toHaveLength(1);
    expect(document.getElementById('topo-gaps')!.style.display).not.toBe('none');
    expect(document.querySelectorAll('#topo-map .topo-node')).toHaveLength(3);
    expect(document.getElementById('topo-drawer')!.style.display).toBe('none');
    expect(document.getElementById('topo-graphwide')!.style.display).not.toBe('none');
    // chip click → pair drawer with rule cards
    await act(async () => {
      (document.querySelector('.topo-pair-chip') as HTMLButtonElement).click();
    });
    const drawer = document.getElementById('topo-drawer')!;
    expect(drawer.style.display).not.toBe('none');
    expect(drawer.querySelector('.drawer-title')!.textContent).toBe('SAP ERP ↔ Ignition MES');
    // close
    await act(async () => {
      (drawer.querySelector('.impact-close-btn') as HTMLButtonElement).click();
    });
    expect(document.getElementById('topo-drawer')!.style.display).toBe('none');
  });

  it('renders wildcard identity rules as expandable concept-grain details with confidence semantics', async () => {
    const wildcardGraph = {
      rules: [{
        id: 'ir-product-any',
        concept: 'product',
        source_system: 'erp',
        target_system: 'mes',
        source_field: 'products.product_id',
        target_field: 'items.item_id',
        source_pattern: '*',
        target_pattern: '*',
        transform: 'identity',
        confidence: 0.7,
        status: 'accepted',
      }],
    };
    const fetchFn = fetchStub({
      '/api/systems': { systems: SYSTEMS },
      '/api/onboard/concepts': { concepts: [{}] },
      '/api/thread/graph-summary': wildcardGraph,
      '/api/thread/entities': { Products: [] },
      '/api/thread/analytics/vocabulary': {},
      '/api/thread/analytics/concept-coverage': {},
    });
    await renderAsync(<ThreadPanel fetchFn={fetchFn} />);
    await act(async () => {
      (document.querySelector('.topo-pair-chip') as HTMLButtonElement).click();
    });

    const card = document.querySelector('.identity-rule-card') as HTMLDetailsElement;
    expect(card.open).toBe(false);
    // Field-level claim a human can judge — never a raw '*'.
    expect(card.querySelector('.identity-rule-phrase')!.textContent).toBe('erp.products.product_id → mes.items.item_id');
    expect(card.querySelector('summary')!.textContent).not.toContain('* ↔ *');
    expect(card.querySelector('.conf-pct')!.textContent).toBe('70%');
    // Concept is the group heading.
    const group = document.querySelector('.identity-concept-group')!;
    expect(group.querySelector('.rule-concept')!.textContent).toBe('product');
    const legend = document.querySelector('.identity-confidence-legend') as HTMLElement;
    expect(legend.textContent).toContain('≥80%');
    expect(legend.textContent).toContain('<80%');
    expect(legend.title).toContain('provenance, not an active probability');

    act(() => (card.querySelector('summary') as HTMLElement).click());
    expect(card.open).toBe(true);
    // The panel's /api/systems read named erp/mes; the Systems row uses those names.
    expect(card.querySelector('.identity-rule-details')!.textContent).toContain('SAP ERP → Ignition MES');
    expect(card.querySelector('.identity-rule-details')!.textContent).toContain('products.product_id → items.item_id');
    expect(card.querySelector('.identity-rule-details')!.textContent).toContain('Any value (*)');
    expect(card.querySelector('.identity-rule-details')!.textContent).toContain('identity');
    expect(card.querySelector('.identity-rule-details')!.textContent).toContain('accepted');
    expect(card.querySelector('.identity-rule-details')!.textContent).toContain('ir-product-any');
  });

  it('<IdentityRulesSection/> names a rule sent in a provision request as not reviewed', () => {
    const rules = [
      {
        id: 'ir-sent-1', concept: 'operator', source_system: 'log', source_field: 'notes.signed_by',
        target_system: 'line', target_field: 'stops.closed_by', confidence: 0.7,
        method: 'provision-request',
        origin_context: 'Sent in a provision request that did not come from the Identity Rules checkpoint',
      },
    ];
    render(<IdentityRulesSection rules={rules} onLifecycle={() => {}} />);
    const details = container.querySelector('.identity-rule-details')!.textContent!;
    expect(details).toContain('Sent in a provision request, not reviewed at the Identity Rules checkpoint');
    expect(details).not.toContain('user-approved');
  });

  it('<IdentityRulesSection/> shows provenance rows and buckets deactivated rules', () => {
    const rules = [
      {
        id: 'ir-live-1', concept: 'batch', source_system: 'erp', source_field: 'orders.batch_id',
        target_system: 'mes', target_field: 'lots.lot_ref', confidence: 0.9,
        method: 'user_confirmed', provenance: 'orchestrator',
        approved_by: 'reviewer@example.com', created_at: '2026-07-16T20:11:00Z',
        origin_context: 'Which line produced batch B-1001?', proposal_id: 'map-123',
      },
      {
        id: 'ir-dead-1', concept: 'batch', source_system: 'erp', source_field: 'orders.site',
        target_system: 'mes', target_field: 'lots.site_code', confidence: 0.6,
        method: 'onboarding-llm', status: 'deactivated',
        deactivated_by: 'reviewer@example.com', deactivated_at: '2026-07-17T01:00:00Z',
      },
    ];
    const calls: Array<[string[], string]> = [];
    render(<IdentityRulesSection rules={rules} onLifecycle={(ids, action) => calls.push([ids, action])} />);

    // Active card carries the audit trail.
    const activeCard = container.querySelector('.identity-rule-card:not(.deactivated)')!;
    const details = activeCard.querySelector('.identity-rule-details')!.textContent!;
    expect(details).toContain('Learned during a query — user confirmed');
    expect(details).toContain('reviewer@example.com');
    expect(details).toContain('2026-07-16 20:11 UTC');
    expect(details).toContain('Which line produced batch B-1001?');

    // Deactivated rule is bucketed, struck through, and reactivatable.
    const bucket = container.querySelector('.identity-deactivated-bucket')!;
    expect(bucket.querySelector('summary')!.textContent).toContain('Deactivated');
    expect(bucket.querySelector('summary')!.textContent).toContain('excluded from query routing');
    const deadCard = bucket.querySelector('.identity-rule-card.deactivated')!;
    expect(deadCard.querySelector('.identity-rule-details')!.textContent).toContain(
      'AI at onboarding — user-approved at the Identity Rules checkpoint',
    );

    // Lifecycle buttons: active → deactivate (confirm-gated: the dialog states
    // the impact and nothing fires until the explicit confirm); bucketed →
    // reactivate (non-destructive undo, no confirm).
    act(() => (activeCard.querySelector('.identity-lifecycle-btn.deactivate') as HTMLButtonElement).click());
    expect(calls).toEqual([]);
    const confirmDeactivate = Array.from(document.querySelectorAll('button')).find(
      (b) => (b.textContent || '').trim() === 'Deactivate rule',
    ) as HTMLButtonElement;
    act(() => confirmDeactivate.click());
    act(() => (deadCard.querySelector('.identity-lifecycle-btn.reactivate') as HTMLButtonElement).click());
    expect(calls).toEqual([
      [['ir-live-1'], 'deactivate'],
      [['ir-dead-1'], 'reactivate'],
    ]);
  });

  it('<IdentityRulesSection/> without onLifecycle renders no lifecycle buttons', () => {
    render(<IdentityRulesSection rules={[{ id: 'ir-1', concept: 'batch', source_system: 'a', target_system: 'b' }]} />);
    expect(container.querySelector('.identity-lifecycle-btn')).toBeNull();
  });

  it('deactivate button in the pair drawer POSTs per rule id and refetches the summary', async () => {
    const posts: string[] = [];
    let summaryFetches = 0;
    const graphWithIds = {
      rules: [
        { id: 'ir-a', concept: 'batch', source_system: 'erp', source_field: 'o.b', target_system: 'mes', target_field: 'l.r', confidence: 0.9 },
        { id: 'ir-b', concept: 'batch', source_system: 'mes', source_field: 'l.r', target_system: 'erp', target_field: 'o.b', confidence: 0.8 },
      ],
    };
    const stub = fetchStub({
      '/api/systems': { systems: SYSTEMS },
      '/api/onboard/concepts': { concepts: [{}] },
      '/api/thread/graph-summary': graphWithIds,
      '/api/thread/entities': {},
      '/api/thread/analytics/vocabulary': {},
      '/api/thread/analytics/concept-coverage': {},
    });
    const fetchFn: typeof fetch = async (input, init) => {
      const url = String(input);
      if (url.includes('/api/thread/rules/')) {
        posts.push(url.split('/api/thread/rules/')[1]);
        return new Response(JSON.stringify({ status: 'deactivated' }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      if (url.includes('/api/thread/graph-summary')) summaryFetches += 1;
      return stub(input as never, init);
    };
    await renderAsync(<ThreadPanel fetchFn={fetchFn} />);
    await act(async () => {
      (document.querySelector('.topo-pair-chip') as HTMLButtonElement).click();
    });
    const card = document.querySelector('.identity-rule-card') as HTMLDetailsElement;
    act(() => (card.querySelector('summary') as HTMLElement).click());
    const before = summaryFetches;
    await act(async () => {
      (card.querySelector('.identity-lifecycle-btn.deactivate') as HTMLButtonElement).click();
    });
    // Confirm-gated: the dialog names BOTH rules; nothing POSTs until confirm.
    expect(posts).toEqual([]);
    await act(async () => {
      (document.querySelector('.confirm-danger-btn') as HTMLButtonElement).click();
    });
    // The two rules are one bidirectional binding — BOTH ids get the POST.
    expect(posts).toEqual(['ir-a/deactivate', 'ir-b/deactivate']);
    expect(summaryFetches).toBeGreaterThan(before); // refetched after lifecycle
  });

  it('coverage: banner + heatmap render; concept click loads impact with takeaway', async () => {
    const impactCalls: string[] = [];
    const fetchFn = fetchStub(
      {
        '/api/systems': { systems: SYSTEMS },
        '/api/onboard/concepts': { concepts: [{}] },
        '/api/thread/graph-summary': GRAPH,
        '/api/thread/entities': { Batches: [{ id: 'B-1001', label: 'B-1001' }] },
        '/api/thread/analytics/vocabulary': { total_concepts: 10, mapped_concepts: 6, coverage_pct: 60 },
        '/api/thread/analytics/concept-coverage': {
          concepts: [{ id: 'batch', name: 'Batch', level: 4 }],
          systems: [{ id: 's1', name: 'SAP ERP' }],
          coverage: [{ concept: 'batch', system: 's1', roles: [{ role: 'identifier', confidence: 0.9 }] }],
        },
        '/api/thread/analytics/impact/batch': {
          concept: { name: 'Batch' },
          hop_summary: { hop_1_systems: 1, hop_3_concepts: 0 },
          direct_systems: [{ system_id: 's1', system_name: 'SAP ERP', fields: [{ name: 'batch_no', role: 'identifier' }] }],
          join_paths: [],
          reachable_concepts: [],
        },
      },
      impactCalls,
    );
    await renderAsync(<ThreadPanel fetchFn={fetchFn} />);
    await act(async () => {
      (document.querySelector('.thread-tab[data-tab="thread-tab-coverage"]') as HTMLButtonElement).click();
    });
    expect(document.getElementById('cov-content')!.style.display).not.toBe('none');
    expect(document.querySelector('#cov-banner .cov-hero-num')).not.toBeNull();
    const coverageLegend = document.querySelector('.coverage-state-legend')!;
    expect(coverageLegend.textContent).toContain('Mapped');
    expect(coverageLegend.textContent).toContain('Partial');
    expect(coverageLegend.textContent).toContain('Unmapped');
    expect(document.getElementById('impact-panel')!.style.display).toBe('none');
    await act(async () => {
      (document.querySelector('.cov-row-label') as HTMLElement).dispatchEvent(new MouseEvent('click', { bubbles: true }));
    });
    expect(document.getElementById('impact-panel')!.style.display).not.toBe('none');
    expect(document.getElementById('impact-concept')!.textContent).toBe(
      'Batch lives in 1 system, joins across 0 paths, and reaches 0 more concepts',
    );
    expect(document.getElementById('impact-trace-btn')!.style.display).not.toBe('none');
    // single direct system → no paths call
    expect(impactCalls.some((u) => u.includes('/analytics/paths'))).toBe(false);
  });

  it('trace: intro shows until an entity resolves across multiple systems through the 3-stage pipeline', async () => {
    const fetchFn = fetchStub({
      '/api/systems': { systems: SYSTEMS },
      '/api/onboard/concepts': { concepts: [{}] },
      '/api/thread/graph-summary': GRAPH,
      '/api/thread/entities': { Batches: [{ id: 'B-1001', label: 'B-1001 — Aspirin' }] },
      '/api/thread/analytics/vocabulary': {},
      '/api/thread/analytics/concept-coverage': {},
      '/api/thread/resolve': {
        raw: [
          { system: 'SAP ERP', fields: [{ label: 'Batch', value: 'B-1001' }] },
          { system: 'Ignition MES', fields: [{ label: 'Lot', value: 'LOT-1001' }] },
        ],
        resolution: [{ rule: 'Direct', transform: 'id', check: 'ok', status: 'matched' }],
        unified: [
          { title: 'SAP ERP', fields: [{ label: 'Batch', value: 'B-1001' }] },
          { title: 'Ignition MES', fields: [{ label: 'Lot', value: 'LOT-1001' }] },
        ],
      },
    });
    await renderAsync(<ThreadPanel fetchFn={fetchFn} />);
    await act(async () => {
      (document.querySelector('.thread-tab[data-tab="thread-tab-trace"]') as HTMLButtonElement).click();
    });
    expect(document.getElementById('trace-intro')!.style.display).not.toBe('none');
    expect(document.querySelector('.trace-intro-title')!.textContent).toBe('One entity, every system, one answer');
    expect(document.getElementById('thread-resolve-section')!.style.display).toBe('none');
    // entity options populated from /api/thread/entities
    const select = document.getElementById('entity-select') as HTMLSelectElement;
    expect(select.querySelectorAll('optgroup')).toHaveLength(1);
    expect(select.querySelector('option[value="Batches:B-1001"]')).not.toBeNull();
    // pick the entity (controlled select → change event)
    await act(async () => {
      select.value = 'Batches:B-1001';
      select.dispatchEvent(new Event('change', { bubbles: true }));
    });
    expect(document.getElementById('trace-intro')!.style.display).toBe('none');
    expect(document.getElementById('thread-resolve-section')!.style.display).not.toBe('none');
    const tag = document.getElementById('entity-type-tag')!;
    expect(tag.textContent).toBe('batch'); // Batches → batch via the new /es$/ pass
    expect(tag.className).toBe('entity-type-tag batch');
    // 3-stage pipeline with heads and content
    const heads = Array.from(document.querySelectorAll('.trace-stage-head .panel-title')).map((e) => e.textContent);
    expect(heads).toEqual(['Source records', 'Translation', 'Unified entity']);
    expect(document.querySelector('#raw-cards .raw-sys-card')).not.toBeNull();
    expect(document.querySelector('#resolution-steps .res-step')).not.toBeNull();
    expect(document.querySelector('#unified-card .unified-entity-id')!.textContent).toBe('B-1001');
    expect(document.querySelector('.trace-scope-banner')).toBeNull();
    // surprise button exists
    expect(document.querySelector('.trace-surprise-btn')!.textContent).toContain('Surprise me');
  });

  it('trace reports a single-system result honestly and does not label it unified', async () => {
    const fetchFn = fetchStub({
      '/api/systems': { systems: SYSTEMS },
      '/api/onboard/concepts': { concepts: [{}] },
      '/api/thread/graph-summary': GRAPH,
      '/api/thread/entities': { Orders: [{ id: 'WO-20260713-93701', label: 'WO-20260713-93701' }] },
      '/api/thread/analytics/vocabulary': {},
      '/api/thread/analytics/concept-coverage': {},
      '/api/thread/resolve': {
        raw: [{ system: 'SAP ERP', fields: [{ label: 'Work order', value: 'WO-20260713-93701' }] }],
        resolution: [{ rule: 'Direct match in SAP ERP', transform: 'id', check: 'erp ✓', status: 'matched' }],
        unified: [{ title: 'SAP ERP', fields: [{ label: 'Work order', value: 'WO-20260713-93701' }] }],
      },
    });
    await renderAsync(<ThreadPanel fetchFn={fetchFn} />);
    await act(async () => {
      (document.querySelector('.thread-tab[data-tab="thread-tab-trace"]') as HTMLButtonElement).click();
    });
    const select = document.getElementById('entity-select') as HTMLSelectElement;
    await act(async () => {
      select.value = 'Orders:WO-20260713-93701';
      select.dispatchEvent(new Event('change', { bubbles: true }));
    });

    const banner = document.querySelector('.trace-scope-banner')!;
    expect(banner.textContent).toContain('Found in 1 of 3 systems (SAP ERP).');
    expect(banner.textContent).toContain('No cross-system matches resolved for this entity.');
    expect(banner.textContent).toContain('SAP ERP, Ignition MES, IBM Maximo CMMS');
    expect(document.querySelector('#thread-unified .panel-title')!.textContent).toBe('Single-system record');
    expect(document.querySelector('#thread-unified .trace-stage-sub')!.textContent).toBe('No cross-system unification');
    expect(document.querySelector('#unified-card .unified-entity-card')).toBeNull();
    expect(document.querySelector('#unified-card .raw-sys-card')!.textContent).toContain('SAP ERP');
  });

});

afterAll(() => {
  notifyManager.setNotifyFunction((callback) => callback());
});
