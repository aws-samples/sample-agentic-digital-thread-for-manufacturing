// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tests for <AgentAnalysisPanel/> (zero-innerHTML migration of the
 * analysis-panel HTML-string builders): the single collapsed <details>
 * panel, the neutral distribution bar driven by buildDistributionSegments
 * (verbatim lib/analysis-util.js palette), the no-red rule, JSX-escaped
 * LLM fields, the N/A guard, and the buildAgentAnalysisPanelData gate. */
// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import {
  AgentAnalysisPanel,
  buildAgentAnalysisPanelData,
  compactTokens,
} from '../chat/analysis-panel';
import type { ToolSeqEntry } from '../chat/answer-segments';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const SEQ: ToolSeqEntry[] = [
  { tool: 'query_mes', label: 'MES Milwaukee', type: 'system', guided: true, summary: 'rows' },
  { tool: 'erp___query_erp', label: 'ERP', type: 'system', guided: false, summary: 'rows' },
];

const COUNTS = { toolCallCount: 3, graphLookupCount: 1, emptyGraphLookups: 1 };

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

function render(data: NonNullable<ReturnType<typeof buildAgentAnalysisPanelData>>) {
  act(() => root.render(<AgentAnalysisPanel data={data} />));
}

describe('buildAgentAnalysisPanelData gate', () => {
  it('returns null when there is nothing to show', () => {
    expect(
      buildAgentAnalysisPanelData([], { toolCallCount: 0, graphLookupCount: 0, emptyGraphLookups: 0 }, null, null),
    ).toBeNull();
  });

  it('computes totals with the intelligence-summary fallbacks', () => {
    const data = buildAgentAnalysisPanelData(SEQ, COUNTS, { total_tool_calls: 5, neptune_queries: 2 }, null)!;
    expect(data.totalTools).toBe(5); // summary wins over counts
    expect(data.graphLookups).toBe(1); // counts win when non-zero
    expect(data.noDataSteps).toBe(1);
    expect(data.hasBrowser).toBe(true);
  });
});

describe('<AgentAnalysisPanel/> single collapsed panel', () => {
  it('renders ONE details.agent-analysis-details, collapsed by default', () => {
    render(buildAgentAnalysisPanelData(SEQ, COUNTS, null, null)!);
    const details = [...container.querySelectorAll<HTMLDetailsElement>('details.agent-analysis-details')];
    expect(details).toHaveLength(1);
    expect(details[0].open).toBe(false);
    const summary = details[0].querySelector('summary.agent-analysis-summary')!;
    expect(summary.textContent).toBe(
      '🔍 Agent Analysis — 3 tool calls · 1 graph lookup · 1 step found no matching data',
    );
  });

  it('renders the neutral distribution bar from the verbatim palette', () => {
    render(buildAgentAnalysisPanelData(SEQ, COUNTS, null, null)!);
    expect(container.textContent).toContain('How this answer was assembled');
    // Segments: Knowledge Graph (1) + MES Milwaukee (1) + ERP (1) — palette
    // colors come from lib/analysis-util.js DISTRIBUTION_PALETTE in order.
    const bar = [...container.querySelectorAll<HTMLElement>('div[title]')].filter((el) =>
      /: \d+ tool call/.test(el.title),
    );
    expect(bar.length).toBe(3);
    // KG anchors first; system ties break alphabetically (ERP before MES).
    expect(bar[0].title).toContain('Knowledge Graph');
    expect(bar[1].title).toContain('ERP');
    expect(bar[2].title).toContain('MES Milwaukee');
    expect(bar[0].style.background).toBe('rgb(88, 166, 255)'); // #58a6ff
    expect(bar[1].style.background).toBe('rgb(0, 216, 255)'); // #00d8ff
    expect(bar[2].style.background).toBe('rgb(63, 185, 80)'); // #3fb950
    // Neutral stats line — the legacy graph-guided judgment is gone.
    expect(container.textContent).not.toContain('graph-guided');
    expect(container.textContent).toContain('○ 1 no-data lookup');
  });

  it('never uses red anywhere in the panel (no red-for-low rule)', () => {
    const data = buildAgentAnalysisPanelData(
      SEQ,
      COUNTS,
      { total_tool_calls: 3, new_proposed_mappings: 2 },
      {
        summary: 'Low graph usage this turn.',
        wasted_calls: [{ reason: 'no match', fix: 'add mapping' }],
        recommendations: [{ system: 's', table: 't', field: 'f' }],
      },
    )!;
    render(data);
    expect(container.innerHTML).not.toContain('var(--red)');
    const styled = [...container.querySelectorAll<HTMLElement>('[style]')];
    expect(styled.some((el) => /(^|[^-\w])red\b/.test(el.getAttribute('style') || ''))).toBe(false);
  });

  it('escapes LLM-authored fields by construction (JSX text)', () => {
    const hostile = '<img src=x onerror="window.__pwned=1"> & <script>alert(1)</script>';
    render(
      buildAgentAnalysisPanelData(SEQ, COUNTS, null, {
        summary: hostile,
        wasted_calls: [{ reason: hostile, fix: hostile }],
      })!,
    );
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('script')).toBeNull();
    expect((window as { __pwned?: number }).__pwned).toBeUndefined();
    expect(container.textContent).toContain('<script>alert(1)</script>');
  });

  it('hides unusable N/A suggestions (naRe guard)', () => {
    render(
      buildAgentAnalysisPanelData(SEQ, COUNTS, null, {
        wasted_calls: [{ reason: 'nothing matched', fix: ' N/A ' }],
      })!,
    );
    expect(container.textContent).toContain('Exploration steps that found no matching data (1)');
    expect(container.textContent).toContain('nothing matched');
    expect(container.textContent).not.toContain('Suggestion:');
  });

  it('renders the rules-applied count from the intelligence summary (sanctioned vocabulary)', () => {
    render(
      buildAgentAnalysisPanelData(
        SEQ,
        COUNTS,
        {
          total_tool_calls: 5,
          neptune_queries: 2,
          system_queries: 4,
          rules_applied: [{ id: 'r1', label: 'Line id crosswalk' }, { id: 'r2' }],
        },
        null,
      )!,
    );
    expect(container.textContent).toContain('2 learned rules applied');
    expect(container.textContent).toContain('4 system queries');
    expect(container.textContent).not.toContain('graph-guided');
  });

  it('renders zero rules honestly, without the ✓ judgment framing', () => {
    render(
      buildAgentAnalysisPanelData(SEQ, COUNTS, { total_tool_calls: 3, rules_applied: [] }, null)!,
    );
    expect(container.textContent).toContain('0 learned rules applied');
    expect(container.textContent).not.toContain('✓');
    expect(container.textContent).not.toContain('graph-guided');
  });

  it('separates correlation write-backs from accepted learned-rule applications', () => {
    render(
      buildAgentAnalysisPanelData(
        SEQ,
        COUNTS,
        { total_tool_calls: 4, rules_applied: [], correlation_writebacks: 1 },
        null,
      )!,
    );
    expect(container.textContent).toContain('0 learned rules applied');
    expect(container.textContent).toContain('1 correlation finding saved');
  });

  it('singularizes one learned rule / one system query', () => {
    render(
      buildAgentAnalysisPanelData(
        SEQ,
        COUNTS,
        { system_queries: 1, rules_applied: [{ id: 'r1', label: 'Site alias' }] },
        null,
      )!,
    );
    expect(container.textContent).toContain('1 learned rule applied');
    expect(container.textContent).toContain('1 system query');
    expect(container.textContent).not.toContain('rules applied');
  });

  it('omits the rules/system lines when the summary lacks them (deploy-skew guard)', () => {
    // Old runtime payload without rules_applied/system_queries — render
    // without the lines rather than claiming "0".
    render(buildAgentAnalysisPanelData(SEQ, COUNTS, { total_tool_calls: 3 }, null)!);
    expect(container.textContent).not.toContain('learned rule');
    expect(container.textContent).not.toContain('system quer');
  });

  it('omits the rules line entirely when there is no intelligence summary', () => {
    render(buildAgentAnalysisPanelData(SEQ, COUNTS, null, null)!);
    expect(container.textContent).not.toContain('learned rule');
    expect(container.textContent).not.toContain('graph-guided');
  });

  it('renders the grounded/deterministic score line when the determinism fields are present', () => {
    render(
      buildAgentAnalysisPanelData(
        SEQ,
        COUNTS,
        { total_tool_calls: 3, grounded_steps: 2, deterministic_pct: 66 },
        null,
      )!,
    );
    expect(container.textContent).toContain('2 of 3 grounded · 66% deterministic');
  });

  it('omits the score line when the determinism fields are absent (deploy-skew guard)', () => {
    render(buildAgentAnalysisPanelData(SEQ, COUNTS, { total_tool_calls: 3 }, null)!);
    expect(container.textContent).not.toContain('grounded');
    expect(container.textContent).not.toContain('deterministic');
  });

  it('omits the score line when either field is missing or non-numeric (hostile payload)', () => {
    render(
      buildAgentAnalysisPanelData(SEQ, COUNTS, { total_tool_calls: 3, grounded_steps: 2 }, null)!,
    );
    expect(container.textContent).not.toContain('grounded');
    act(() => root.unmount());
    root = createRoot(container);
    const hostile = '<img src=x onerror="window.__pwned2=1">';
    render(
      buildAgentAnalysisPanelData(
        SEQ,
        COUNTS,
        {
          total_tool_calls: 3,
          grounded_steps: hostile as unknown as number,
          deterministic_pct: hostile as unknown as number,
        },
        null,
      )!,
    );
    expect(container.textContent).not.toContain('grounded');
    expect(container.querySelector('img')).toBeNull();
    expect((window as { __pwned2?: number }).__pwned2).toBeUndefined();
  });

  it('renders the Self-Learning links with the data-nav contract', () => {
    render(
      buildAgentAnalysisPanelData(
        SEQ,
        COUNTS,
        { new_proposed_mappings: 2 },
        {
          recommendations: [
            { system: 's', table: 't', field: 'f' },
            { system: 's2', table: 't2', field: 'f2' },
          ],
        },
      )!,
    );
    const navs = [...container.querySelectorAll<HTMLAnchorElement>('a[data-nav="self-learning"]')];
    expect(navs).toHaveLength(2); // schema-hint link + intelligence link
    expect(container.textContent).toContain('2 schema hints discovered');
    expect(container.textContent).toContain('2 new optimizations discovered');
  });
});

describe('KgContribution — Knowledge graph contribution section', () => {
  it('renders both parts; rule chips carry the kg-node nav contract', () => {
    render(
      buildAgentAnalysisPanelData(
        SEQ,
        COUNTS,
        {
          total_tool_calls: 3,
          rules_applied: [{ id: 'subj-aaaaaaaaaaaaaaaa', label: 'ERP order id ↔ MES work order' }],
          planned_with: { systems: 2, shared_concepts: ['work order'], join_paths: 1 },
        },
        null,
      )!,
    );
    expect(container.textContent).toContain('Knowledge graph contribution');
    expect(container.textContent).toContain('Planned with');
    expect(container.textContent).toContain('2 systems · 1 join path');
    expect(container.textContent).toContain('work order');
    expect(container.textContent).toContain('Bridged via');
    const chip = container.querySelector<HTMLAnchorElement>('a[data-nav="kg-node"]');
    expect(chip).not.toBeNull();
    expect(chip!.dataset.nodeId).toBe('subj-aaaaaaaaaaaaaaaa');
    expect(chip!.textContent).toBe('ERP order id ↔ MES work order');
  });

  it('id-less rules render as plain spans (non-navigating); label falls back to id', () => {
    render(
      buildAgentAnalysisPanelData(
        SEQ,
        COUNTS,
        { rules_applied: [{ label: 'unmatched rule' }, { id: 'vm-learned-12345678' }] },
        null,
      )!,
    );
    // First entry has no id — no anchor for it; second navigates with id as label.
    const anchors = [...container.querySelectorAll<HTMLAnchorElement>('a[data-nav="kg-node"]')];
    expect(anchors).toHaveLength(1);
    expect(anchors[0].dataset.nodeId).toBe('vm-learned-12345678');
    expect(anchors[0].textContent).toBe('vm-learned-12345678');
    expect(container.textContent).toContain('unmatched rule');
  });

  it('hostile rule labels and concepts render inert (JSX text, no element injection)', () => {
    const hostile = '<img src=x onerror="window.__pwned3=1"> & "quotes"';
    render(
      buildAgentAnalysisPanelData(
        SEQ,
        COUNTS,
        {
          rules_applied: [{ id: 'n1', label: hostile }],
          planned_with: { systems: 1, shared_concepts: [hostile] },
        },
        null,
      )!,
    );
    expect(container.querySelector('img')).toBeNull();
    expect((window as { __pwned3?: number }).__pwned3).toBeUndefined();
    expect(container.textContent).toContain(hostile);
  });

  it('section is ABSENT when neither field is present (old payloads — deploy-skew)', () => {
    render(buildAgentAnalysisPanelData(SEQ, COUNTS, { total_tool_calls: 3 }, null)!);
    expect(container.textContent).not.toContain('Knowledge graph contribution');
    expect(container.querySelector('.kg-contribution-box')).toBeNull();
  });

  it('section is ABSENT for empty rules_applied + empty planned_with (empty ⇒ absent)', () => {
    render(
      buildAgentAnalysisPanelData(
        SEQ,
        COUNTS,
        { total_tool_calls: 3, rules_applied: [], planned_with: {} },
        null,
      )!,
    );
    expect(container.textContent).not.toContain('Knowledge graph contribution');
    expect(container.querySelector('.kg-contribution-box')).toBeNull();
  });

  it('drops hostile non-numeric/non-string planned_with values; all-dropped ⇒ part absent', () => {
    render(
      buildAgentAnalysisPanelData(
        SEQ,
        COUNTS,
        {
          rules_applied: [{ id: 'n1', label: 'kept rule' }],
          planned_with: {
            systems: '<b>2</b>' as unknown as number,
            shared_concepts: [42, null] as unknown as string[],
          },
        },
        null,
      )!,
    );
    // Rules part still renders; the poisoned planned part is absent entirely.
    expect(container.textContent).toContain('Bridged via');
    expect(container.textContent).toContain('kept rule');
    expect(container.textContent).not.toContain('Planned with');
    expect(container.textContent).not.toContain('<b>');
    expect(container.querySelector('b')).toBeNull();
  });

  it('singularizes planned_with counts', () => {
    render(
      buildAgentAnalysisPanelData(
        SEQ,
        COUNTS,
        { planned_with: { systems: 1, value_mappings: 1, identity_rules: 2 } },
        null,
      )!,
    );
    expect(container.textContent).toContain('1 system · 1 value mapping · 2 identity rules');
  });
});

/** Pinned E7 tooltip (ADR-0053) — must match the component copy verbatim. */
const COST_TIP =
  'Measured orchestrator model usage only — system-agent model calls are not included. No cost estimate is derived.';

const COST = {
  input_tokens: 5230,
  output_tokens: 812,
  total_tokens: 6042,
  latency_ms: 41210,
  scope: 'orchestrator-model-only',
};

describe('cost chip — measured orchestrator tokens (E7)', () => {
  it('renders the chip beside the KgContribution header when cost is present', () => {
    render(
      buildAgentAnalysisPanelData(
        SEQ,
        COUNTS,
        { total_tool_calls: 3, rules_applied: [{ id: 'r1', label: 'rule' }], cost: COST },
        null,
      )!,
    );
    const box = container.querySelector('.kg-contribution-box')!;
    const chip = box.querySelector<HTMLElement>('.cost-chip');
    expect(chip).not.toBeNull();
    expect(chip!.textContent).toBe('≈6.0k tokens · orchestrator model');
  });

  it('renders the chip alone when cost is present without KG-contribution data', () => {
    render(buildAgentAnalysisPanelData(SEQ, COUNTS, { total_tool_calls: 3, cost: COST }, null)!);
    // No KG data ⇒ no section header (honesty pin unchanged) — chip still shows.
    expect(container.textContent).not.toContain('Knowledge graph contribution');
    expect(container.querySelector('.kg-contribution-box')).toBeNull();
    const chip = container.querySelector<HTMLElement>('.cost-chip-row .cost-chip');
    expect(chip).not.toBeNull();
    expect(chip!.textContent).toBe('≈6.0k tokens · orchestrator model');
  });

  it('renders NO chip when the cost key is absent (honest absence — never zeros)', () => {
    render(
      buildAgentAnalysisPanelData(
        SEQ,
        COUNTS,
        { total_tool_calls: 3, rules_applied: [{ id: 'r1', label: 'rule' }] },
        null,
      )!,
    );
    expect(container.querySelector('.cost-chip')).toBeNull();
    expect(container.textContent).not.toContain('tokens · orchestrator model');
    expect(container.textContent).not.toContain('≈');
  });

  it('names the orchestrator-only scope in the title tooltip (pinned copy)', () => {
    render(buildAgentAnalysisPanelData(SEQ, COUNTS, { total_tool_calls: 3, cost: COST }, null)!);
    expect(container.querySelector<HTMLElement>('.cost-chip')!.title).toBe(COST_TIP);
  });

  it('drops hostile non-numeric total_tokens (no chip, no injection)', () => {
    render(
      buildAgentAnalysisPanelData(
        SEQ,
        COUNTS,
        {
          total_tool_calls: 3,
          cost: { ...COST, total_tokens: '<img src=x>' as unknown as number },
        },
        null,
      )!,
    );
    expect(container.querySelector('.cost-chip')).toBeNull();
    expect(container.querySelector('img')).toBeNull();
  });

  it('compactTokens formats sub-1k, k and M ranges (pure helper)', () => {
    expect(compactTokens(950)).toBe('950');
    expect(compactTokens(6042)).toBe('6.0k');
    expect(compactTokens(41210)).toBe('41.2k');
    expect(compactTokens(2_400_000)).toBe('2.4M');
    expect(compactTokens(-1)).toBe('');
    expect(compactTokens(NaN)).toBe('');
  });
});
