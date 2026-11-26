// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* A switched-off rule is shown for audit, never counted as a connection.
 *
 * A JoinPath whose identity rule was deactivated (the server reports its
 * effective status) and a deactivated or retired identity rule stay visible
 * in the topology drawers in the inactive/audit style, and never count as a
 * rule linking a pair: not in the pair count, "rules link this pair",
 * "participates in N rules", the header's translation rules, the linked
 * pairs, the topology edges or the gap callout. The section badges count
 * active rules (the audit bucket counts the inactive ones), and a system
 * linked only by inactive rules is not described as having none. The KG view
 * draws a switched-off JoinPath in the inactive style. GENERIC: synthetic
 * systems. */
// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { isInactiveStatus } from '../lib/rule-status';
import {
  aggregatePairs,
  linkedPairKeys,
  pairGaps,
  pairRuleCount,
  totalRuleCount,
  PAIR_SEP,
} from '../shared/thread/html';
import {
  IdentityRulesSection,
  JoinPathsSection,
  NodeDrawer,
  PairDrawer,
  TopoPairChips,
  TopoSvg,
} from '../shared/thread/panels';
import { ruleNodeColors } from '../views/architecture/neptune-graph';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

function render(el: React.ReactElement) {
  act(() => {
    root.render(el);
  });
}

const SYSTEMS = [
  { id: 'sys-a', name: 'System A', status: 'connected' },
  { id: 'sys-b', name: 'System B', status: 'connected' },
  { id: 'sys-c', name: 'System C', status: 'connected' },
];
const AB = 'sys-a' + PAIR_SEP + 'sys-b';
const AC = 'sys-a' + PAIR_SEP + 'sys-c';

const GRAPH = {
  rules: [
    { id: 'ir-1', concept: 'lot', source_system: 'sys-a', target_system: 'sys-b', confidence: 0.6 },
    { id: 'ir-2', concept: 'line', source_system: 'sys-a', target_system: 'sys-b', confidence: 0.95, status: 'retired' },
  ],
  join_paths_detail: [
    { id: 'jp-1', from_system: 'sys-a', from_field: 'lot', to_system: 'sys-b', to_field: 'lot_no' },
    { id: 'jp-2', from_system: 'sys-a', from_field: 'site_code', to_system: 'sys-b', to_field: 'line_name', status: 'deactivated' },
    { id: 'jp-3', from_system: 'sys-a', from_field: 'site_code', to_system: 'sys-c', to_field: 'cell', status: 'deactivated' },
  ],
};

describe('rule status', () => {
  it('deactivated and retired are inactive', () => {
    expect(isInactiveStatus('deactivated')).toBe(true);
    expect(isInactiveStatus('retired')).toBe(true);
    for (const s of [undefined, null, '', 'active']) expect(isInactiveStatus(s)).toBe(false);
  });
});

describe('pair aggregation', () => {
  it('counts only active rules; inactive ones stay listed for audit', () => {
    const pairs = aggregatePairs(GRAPH, SYSTEMS);
    expect(pairs[AB].joins).toHaveLength(2);
    expect(pairs[AB].identity).toHaveLength(2);
    expect(pairRuleCount(pairs[AB])).toBe(2);
    expect(pairs[AB].maxConf).toBe(0.7); // the retired 0.95 rule does not raise it
    expect(pairRuleCount(pairs[AC])).toBe(0);
    expect(linkedPairKeys(pairs)).toEqual([AB]);
    expect(totalRuleCount(GRAPH)).toBe(2);
  });

  it('a pair linked only by switched-off rules is a gap', () => {
    const pairs = aggregatePairs(GRAPH, SYSTEMS);
    expect(pairGaps(SYSTEMS, pairs)).toEqual([
      ['System A', 'System C'],
      ['System B', 'System C'],
    ]);
  });
});

describe('drawers and topology', () => {
  it('<PairDrawer/> buckets the switched-off join and does not count it', () => {
    const pairs = aggregatePairs(GRAPH, SYSTEMS);
    render(<PairDrawer pair={pairs[AB]} systems={SYSTEMS} onClose={() => {}} />);
    expect(container.querySelector('.drawer-sub')!.textContent).toBe('2 rules link this pair');
    const bucket = container.querySelector('.join-deactivated-bucket')!;
    expect(bucket.querySelector('summary')!.textContent).toContain('Deactivated');
    expect(bucket.querySelector('summary')!.textContent).toContain('not used as a join');
    const dead = bucket.querySelector('.join-card.deactivated')!;
    expect(dead.textContent).toContain('site_code');
    const live = Array.from(container.querySelectorAll('.join-card:not(.deactivated)'));
    expect(live).toHaveLength(1);
    expect(live[0].textContent).toContain('lot_no');
    // The retired identity rule sits in the identity rules' deactivated bucket.
    expect(container.querySelector('.identity-deactivated-bucket')).not.toBeNull();
  });

  it('<NodeDrawer/> counts only active rules', () => {
    const pairs = aggregatePairs(GRAPH, SYSTEMS);
    render(<NodeDrawer system={SYSTEMS[0]} pairs={pairs} onClose={() => {}} />);
    expect(container.querySelector('.drawer-sub')!.textContent).toBe('participates in 2 rules');
    render(<NodeDrawer system={SYSTEMS[2]} pairs={pairs} onClose={() => {}} />);
    expect(container.querySelector('.drawer-sub')!.textContent).toBe('participates in 0 rules');
    expect(container.querySelector('.join-deactivated-bucket')).not.toBeNull();
  });

  it('<TopoSvg/> and <TopoPairChips/> draw no link for a pair with no active rule', () => {
    const pairs = aggregatePairs(GRAPH, SYSTEMS);
    render(
      <TopoSvg systems={SYSTEMS} pairs={pairs} selectedPair={null} selectedSys={null}
        onOpenPair={() => {}} onOpenNode={() => {}} />,
    );
    const edges = Array.from(container.querySelectorAll('.topo-edge')).map((e) => e.getAttribute('data-pair'));
    expect(edges).toEqual([AB]);
    render(<TopoPairChips systems={SYSTEMS} pairs={pairs} selectedPair={null} onOpenPair={() => {}} />);
    const chips = Array.from(container.querySelectorAll('.topo-pair-chip')).map((e) => e.getAttribute('data-pair'));
    expect(chips).toEqual([AB]);
  });

  it('<IdentityRulesSection/> buckets a retired rule with the deactivated ones', () => {
    render(<IdentityRulesSection rules={GRAPH.rules} />);
    const bucket = container.querySelector('.identity-deactivated-bucket')!;
    expect(bucket.querySelector('summary')!.textContent).toContain('1');
  });

  it('section badges count active rules; the audit bucket counts the inactive ones', () => {
    render(<IdentityRulesSection rules={GRAPH.rules} />);
    expect(container.querySelector('.rule-section-title .thread-count')!.textContent).toBe('1');
    expect(container.querySelector('.identity-deactivated-bucket summary .thread-count')!.textContent).toBe('1');
    const pairs = aggregatePairs(GRAPH, SYSTEMS);
    render(<JoinPathsSection joins={pairs[AB].joins} />);
    expect(container.querySelector('.rule-section-title .thread-count')!.textContent).toBe('1');
    expect(container.querySelector('.join-deactivated-bucket summary .thread-count')!.textContent).toBe('1');
  });

  it('a section holding only inactive rules shows 0 active and its audit bucket', () => {
    const pairs = aggregatePairs(GRAPH, SYSTEMS);
    render(<JoinPathsSection joins={pairs[AC].joins} />);
    expect(container.querySelector('.rule-section-title .thread-count')!.textContent).toBe('0');
    expect(container.querySelector('.join-deactivated-bucket summary .thread-count')!.textContent).toBe('1');
  });

  it('<NodeDrawer/> says when only inactive rules involve the system', () => {
    const pairs = aggregatePairs(GRAPH, SYSTEMS);
    render(<NodeDrawer system={SYSTEMS[2]} pairs={pairs} onClose={() => {}} />);
    const note = container.querySelector('.drawer-note')!.textContent!;
    expect(note).not.toContain('No translation rules involve this system yet');
    expect(note).toContain('No active translation rules involve this system');
    expect(note).toContain('deactivated');
    render(<NodeDrawer system={{ id: 'sys-d', name: 'System D', status: 'connected' }} pairs={pairs} onClose={() => {}} />);
    expect(container.querySelector('.drawer-note')!.textContent).toBe('No translation rules involve this system yet.');
  });
});

describe('KG view', () => {
  const jp = (props: Record<string, unknown>) => ({ id: 'g', label: 'jp', type: 'JoinPath', props });

  it('draws a switched-off JoinPath in the inactive style', () => {
    const off = ruleNodeColors(jp({ from_system: 'sys-a', to_system: 'sys-b', effective_status: 'deactivated' }), false);
    expect(off.inactive).toBe(true);
    expect(off.tooltipPrefix).toContain('not used as a join');
    const own = ruleNodeColors(jp({ from_system: 'sys-a', to_system: 'sys-b', status: 'deactivated' }), true);
    expect(own.inactive).toBe(true);
  });

  it('keeps the cross-system highlight for an active JoinPath', () => {
    const live = ruleNodeColors(jp({ from_system: 'sys-a', to_system: 'sys-b' }), false);
    expect(live.inactive).toBe(false);
    expect(live.border).toBe('#f59e0b');
  });

  it('draws a switched-off identity rule in the inactive style', () => {
    const ir = ruleNodeColors({ id: 'g', label: 'ir', type: 'IdentityRule', props: { status: 'retired' } }, false);
    expect(ir.inactive).toBe(true);
    const live = ruleNodeColors({ id: 'g', label: 'ir', type: 'IdentityRule', props: {} }, false);
    expect(live.inactive).toBe(false);
  });
});
