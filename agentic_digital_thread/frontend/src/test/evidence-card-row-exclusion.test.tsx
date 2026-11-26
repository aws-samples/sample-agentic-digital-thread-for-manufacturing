// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* The source owner's row exclusion on the evidence card (<EvidenceCard/>
 * Tier 2). Pins:
 * - a record carrying `row_exclusions` shows a "Rows left out" section with
 *   the owner's label, each condition in plain words, and who set the rule,
 *   when and from what source — from the streamed record alone, with no
 *   fetch and nothing behind the admin-only Technical detail;
 * - an absent/empty key renders no such section (fail closed);
 * - a rule with a condition the card cannot state exactly is dropped whole,
 *   never shown in part;
 * - hostile strings render as literal text (zero-innerHTML);
 * - a reloaded turn replays the same record, rule included. */
// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { EvidenceCard } from '../chat/EvidenceCard';
import {
  rowExclusionViews,
  type ProvenanceRecord,
  type ProvenanceRowExclusion,
} from '../chat/provenance';
import { buildRehydrateThread } from '../chat/rehydrate';
import type { Citation } from '../chat/turn-model';
import type { MemoryEvent } from '../api/types/memory';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;
let fetchFn: ReturnType<typeof vi.fn>;

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  fetchFn = vi.fn();
  vi.stubGlobal('fetch', fetchFn);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const RULE: ProvenanceRowExclusion = {
  table: 'table_x',
  label: 'Test rows',
  conditions: [
    { column: 'status', op: 'in', values: ['draft', 'void'] },
    { column: 'kind', op: 'empty' },
  ],
  at: '2026-11-01T10:00:00Z',
  source: 'data team runbook',
};
const SET_LINE = 'Set by an administrator on 2026-11-01T10:00:00Z. Source: data team runbook';

function mkCitation(): Citation {
  return { tool: 'lake___query', label: 'Lake', nodeId: 'sys-lake', num: 1, findingNodeId: null };
}

function mkRecord(over: Partial<ProvenanceRecord> = {}): ProvenanceRecord {
  return {
    citation_num: 1,
    tool_id: 'toolu_rx',
    source_system: 'lake',
    captured_at: '2026-11-01T11:00:00Z',
    verification: 'partial',
    query_ref: 'q-0123456789ab',
    ...over,
  };
}

function render(record: ProvenanceRecord) {
  act(() => {
    root.render(<EvidenceCard record={record} citation={mkCitation()} onClose={() => {}} />);
  });
}

function section(): HTMLElement | null {
  return container.querySelector('.evidence-row-exclusions');
}

describe('rowExclusionViews — plain words for each applied rule', () => {
  it('states each condition, the label, the table, when it was set and its source', () => {
    expect(rowExclusionViews(mkRecord({ row_exclusions: [RULE] }))).toEqual([
      {
        title: 'Test rows (table_x)',
        intro: 'Rows where all of these hold were left out:',
        conditions: ['status is one of "draft", "void"', 'kind is empty or null'],
        provenance: SET_LINE,
      },
    ]);
  });

  it('quotes every value, so a comma or a null-like value cannot be misread', () => {
    const rule: ProvenanceRowExclusion = {
      label: 'Odd values',
      conditions: [
        { column: 'status', op: 'in', values: ['draft, void', 'final'] },
        { column: 'kind', op: 'in', values: ['empty or null'] },
        { column: 'grade', op: 'not_in', values: ['is not'] },
        { column: 'note', op: 'not_in', values: ['a, or is empty or null', 'b'] },
      ],
    };
    const [view] = rowExclusionViews(mkRecord({ row_exclusions: [rule] }));
    expect(view.conditions).toEqual([
      'status is one of "draft, void", "final"',
      'kind is "empty or null"',
      'grade is not "is not", or is empty or null',
      'note is none of "a, or is empty or null", "b", or is empty or null',
    ]);
  });

  it('drops a rule whose value holds a double quote, which it could not quote exactly', () => {
    const rule = {
      label: 'Quoted',
      conditions: [{ column: 'status', op: 'in', values: ['say "draft"'] }],
    };
    expect(rowExclusionViews(mkRecord({ row_exclusions: [rule] }))).toEqual([]);
  });

  it('says an administrator set it, whatever actor the record carries', () => {
    for (const actor of ['someone@example.com', 'ADT', 'unknown']) {
      const rule = { ...RULE, by: actor } as ProvenanceRowExclusion;
      const [view] = rowExclusionViews(mkRecord({ row_exclusions: [rule] }));
      expect(view.provenance).toBe(SET_LINE);
      expect(view.provenance).not.toContain(actor);
    }
    const bare = { label: 'Bare', conditions: [{ column: 'kind', op: 'empty' }] };
    expect(rowExclusionViews(mkRecord({ row_exclusions: [bare] }))[0].provenance).toBe(
      'Set by an administrator.',
    );
  });

  it('marks a shortened source with an ellipsis and a note', () => {
    const rule = { ...RULE, source: 'data team run', source_shortened: true };
    const [view] = rowExclusionViews(mkRecord({ row_exclusions: [rule] }));
    expect(view.provenance).toBe(
      'Set by an administrator on 2026-11-01T10:00:00Z. Source: data team run… (source shortened)',
    );
  });

  it('words every kind of condition, one value or several', () => {
    const rule: ProvenanceRowExclusion = {
      label: 'Old rows',
      conditions: [
        { column: 'status', op: 'in', values: ['draft'] },
        { column: 'kind', op: 'not_in', values: ['a'] },
        { column: 'grade', op: 'not_in', values: ['a', 'b'] },
        { column: 'note', op: 'not_empty' },
      ],
    };
    const [view] = rowExclusionViews(mkRecord({ row_exclusions: [rule] }));
    expect(view.title).toBe('Old rows');
    expect(view.conditions).toEqual([
      'status is "draft"',
      'kind is not "a", or is empty or null',
      'grade is none of "a", "b", or is empty or null',
      'note is not empty or null',
    ]);
    expect(view.provenance).toBe('Set by an administrator.');
  });

  it('a one-condition rule says which rows were left out in one line', () => {
    const rule = { label: 'Blank kind', conditions: [{ column: 'kind', op: 'empty' }] };
    const [view] = rowExclusionViews(mkRecord({ row_exclusions: [rule] }));
    expect(view.intro).toBe('Rows where this holds were left out:');
  });

  it('drops a rule it cannot state exactly, whole', () => {
    const odd = [
      { label: 'Bad op', conditions: [{ column: 'status', op: 'like', values: ['d%'] }] },
      { label: 'In with no values', conditions: [{ column: 'status', op: 'in' }] },
      { label: 'Empty with values', conditions: [{ column: 'kind', op: 'empty', values: ['x'] }] },
      { label: '', conditions: [{ column: 'kind', op: 'empty' }] },
      { label: 'No conditions', conditions: [] },
      { label: 'Not a list', conditions: 'kind' },
      'not a rule',
      null,
    ] as unknown as ProvenanceRowExclusion[];
    expect(rowExclusionViews(mkRecord({ row_exclusions: [...odd, RULE] }))).toHaveLength(1);
    expect(rowExclusionViews(mkRecord({ row_exclusions: odd }))).toEqual([]);
  });

  it('gives nothing for a record without rules', () => {
    expect(rowExclusionViews(null)).toEqual([]);
    expect(rowExclusionViews(mkRecord())).toEqual([]);
    expect(rowExclusionViews(mkRecord({ row_exclusions: [] }))).toEqual([]);
  });
});

describe('<EvidenceCard/> — rows left out by the source owner', () => {
  it('shows the rule from the record itself, with no fetch', () => {
    render(mkRecord({ row_exclusions: [RULE] }));
    const shown = section();
    expect(shown).not.toBeNull();
    expect(shown!.textContent).toContain('Rows left out');
    expect(shown!.textContent).toContain('Test rows (table_x)');
    expect(shown!.textContent).toContain('Rows where all of these hold were left out:');
    const items = [...shown!.querySelectorAll('li')].map((li) => li.textContent);
    expect(items).toEqual(['status is one of "draft", "void"', 'kind is empty or null']);
    expect(shown!.textContent).toContain(SET_LINE);
    // Outside the admin-only Technical detail, and nothing was fetched.
    expect(shown!.closest('.evidence-tier3')).toBeNull();
    expect(fetchFn).not.toHaveBeenCalled();
  });

  it('shows no such section without rules', () => {
    render(mkRecord());
    expect(section()).toBeNull();
    render(mkRecord({ row_exclusions: [] }));
    expect(section()).toBeNull();
  });

  it('renders hostile text as literal text', () => {
    const hostile = '<img src=x onerror="alert(1)">';
    const value = '<img src=x onerror=alert(1)>';
    render(
      mkRecord({
        row_exclusions: [
          {
            label: hostile,
            conditions: [{ column: 'status', op: 'in', values: [value] }],
            source: hostile,
          },
        ],
      }),
    );
    expect(section()!.textContent).toContain(hostile);
    expect(section()!.textContent).toContain(`"${value}"`);
    expect(container.querySelector('img')).toBeNull();
  });

  it('says a rule left rows out when its details did not fit', () => {
    render(mkRecord({ row_exclusions_omitted: true }));
    expect(section()!.textContent).toContain(
      "A source owner's rule left rows out of this result; its details didn't fit on this card.",
    );
    render(mkRecord({ row_exclusions: [RULE], row_exclusions_omitted: true }));
    expect(section()!.textContent).toContain('Test rows (table_x)');
    expect(section()!.textContent).toContain("its details didn't fit on this card.");
    render(mkRecord({ row_exclusions_omitted: 'yes' as unknown as boolean }));
    expect(section()).toBeNull();
  });
});

describe('a reloaded turn shows the same rule', () => {
  it('replays the kept record with its rule', () => {
    const record = mkRecord({ row_exclusions: [RULE], row_exclusions_omitted: true });
    const events: MemoryEvent[] = [
      {
        role: 'user',
        content: 'How many?',
        provenance: { records: [record], truncated: false },
      } as MemoryEvent,
      {
        role: 'assistant',
        content: '',
        blocks: [
          { type: 'tool_use', tool: 'lake___query', tool_id: 'toolu_rx', input: {} },
        ],
      } as MemoryEvent,
      {
        role: 'assistant',
        content: '',
        blocks: [
          { type: 'tool_result', tool: 'lake___query', tool_id: 'toolu_rx', summary: '7' },
        ],
      } as MemoryEvent,
      { role: 'assistant', content: 'There are 7 [1].' } as MemoryEvent,
    ];
    const { turns } = buildRehydrateThread(events);
    expect(turns).toHaveLength(1);
    const [reloaded] = turns[0].assistant.provenance;
    expect(reloaded.row_exclusions).toEqual([RULE]);
    expect(reloaded.row_exclusions_omitted).toBe(true);
    expect(rowExclusionViews(reloaded)).toEqual(
      rowExclusionViews(mkRecord({ row_exclusions: [RULE] })),
    );
  });
});
