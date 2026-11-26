// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
/* Each proposed identity rule shows its measured evidence on its own row.
 *
 * The Identity Rules checkpoint listed each proposed rule's concept, fields,
 * pattern and target, while the value overlap that justified it sat in a
 * separate "Value Overlap Evidence" table further down. A person reviewing a
 * row had to find its pair in that table by eye. The row now says, in words,
 * what was measured for its field pair, paired the way the resolve gate pairs
 * them (resolve_gates.evidence_for_rule): the exact table-qualified pair
 * first, then the column names only. Every existing label stays as it was.
 * Fixture names are neutral. */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../views/onboard/onboard-queries', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../views/onboard/onboard-queries')>()),
  getOnboardQueryClient: vi.fn(),
  invalidateOnboardingRemap: vi.fn(async () => {}),
  useConceptsQuery: () => ({ data: [], isError: false, refetch: vi.fn() }),
}));

import type { IdentityRule, ResolveEvidence } from '../api/types/onboard';
import { systemName } from '../lib/system-names';
import { Checkpoint } from '../views/onboard/components/Checkpoint';
import type { FlowEntry } from '../views/onboard/flow-store';
import { evidenceForRule, ruleEvidenceText } from '../views/onboard/rule-evidence';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const AUTHOR: IdentityRule = {
  concept: 'operator',
  source_field: 'work_log.author',
  target_field: 'stops.resolved_by',
  source_pattern: '*',
  target_pattern: '*',
  target_system: 'mes-north',
};

const BARE: IdentityRule = {
  concept: 'equipment-id',
  source_field: 'asset_tag',
  target_field: 'equipment_id',
  target_system: 'mes-north',
};

const GUESSED: IdentityRule = {
  concept: 'work-order',
  source_field: 'orders.wo_number',
  target_field: 'jobs.order_ref',
  target_system: 'mes-north',
};

const EVIDENCE: ResolveEvidence[] = [
  {
    concept: 'operator',
    source_field: 'work_log.author',
    target_system: 'mes-north',
    target_field: 'stops.resolved_by',
    overlap_count: 12,
    examples: ['maint_01', 'maint_04'],
    shown_examples: ['maint_01', 'maint_04'],
    ratio: 0.8,
  },
  {
    concept: 'equipment-id',
    source_field: 'assets.asset_tag',
    target_system: 'mes-north',
    target_field: 'machines.equipment_id',
    overlap_count: 5,
    examples: ['EQ-201', 'EQ-202', 'EQ-207', 'EQ-210', 'EQ-211'],
    shown_examples: ['EQ-201', 'EQ-202', 'EQ-207', 'EQ-210', 'EQ-211'],
    ratio: 0.6,
  },
];

describe('ruleEvidenceText', () => {
  it('says how many values the pair shares, with examples', () => {
    expect(ruleEvidenceText(AUTHOR, EVIDENCE)).toBe('12 shared values, e.g. maint_01, maint_04');
  });

  /* The gate keeps a rule that reads values unchanged on the shared values
   * that are the same whole value in both fields (`unchanged_count`), not on
   * the shared tokens. When fewer of them are whole values, the row says so,
   * so it never claims more support than the gate counted. */
  it('names how many shared values are the same whole value when fewer are', () => {
    const rows = EVIDENCE.map((r) => (r.concept === 'equipment-id' ? { ...r, unchanged_count: 3 } : r));
    expect(ruleEvidenceText(BARE, rows)).toBe(
      '5 shared values, 3 the same whole value in both fields, e.g. EQ-201, EQ-202, EQ-207',
    );
  });

  it('keeps the plain count when every shared value is the same whole value', () => {
    const rows = EVIDENCE.map((r) => (r.concept === 'operator' ? { ...r, unchanged_count: 12 } : r));
    expect(ruleEvidenceText(AUTHOR, rows)).toBe('12 shared values, e.g. maint_01, maint_04');
  });

  it('says one shared value is the same whole value in the singular', () => {
    const rows = EVIDENCE.map((r) => (r.concept === 'equipment-id' ? { ...r, unchanged_count: 1 } : r));
    expect(ruleEvidenceText(BARE, rows)).toBe(
      '5 shared values, 1 the same whole value in both fields, e.g. EQ-201, EQ-202, EQ-207',
    );
  });

  it('never names a whole-value count on a translation rule, which is judged on its check', () => {
    const translated: IdentityRule = { ...BARE, source_pattern: 'EQ-{n}', target_pattern: 'MCH-{n}' };
    const rows = EVIDENCE.map((r) => (r.concept === 'equipment-id' ? { ...r, unchanged_count: 0 } : r));
    expect(ruleEvidenceText(translated, rows)).toBe('5 shared values, e.g. EQ-201, EQ-202, EQ-207');
  });

  it('pairs on the column names when the rule has no table prefix, and shows three examples', () => {
    expect(ruleEvidenceText(BARE, EVIDENCE)).toBe('5 shared values, e.g. EQ-201, EQ-202, EQ-207');
  });

  it('says a rule with no evidence row was not measured', () => {
    expect(ruleEvidenceText(GUESSED, EVIDENCE)).toBe(
      'not measured: both field names look like identifiers',
    );
    expect(ruleEvidenceText(GUESSED, [])).toBe(
      'not measured: both field names look like identifiers',
    );
  });

  it('names what an affinity rule with no evidence row matched', () => {
    expect(
      ruleEvidenceText({ ...GUESSED, method: 'value_exact_match', shared_count: 7 }, []),
    ).toBe('7 shared values, checked exactly in both systems');
    expect(ruleEvidenceText({ ...GUESSED, method: 'value_core_match' }, [])).toBe(
      'sampled values share an identifier core',
    );
  });

  it('prefers the exact table-qualified pair, and never takes another table pair for a qualified rule', () => {
    const loose: ResolveEvidence = {
      ...EVIDENCE[0],
      source_field: 'other_log.author',
      target_field: 'other.resolved_by',
      overlap_count: 3,
    };
    expect(evidenceForRule(AUTHOR, [loose, EVIDENCE[0]])).toBe(EVIDENCE[0]);
    expect(evidenceForRule(AUTHOR, [loose])).toBeNull();
    expect(evidenceForRule({ ...AUTHOR, target_system: 'mes-south' }, EVIDENCE)).toBeNull();
  });

  it('matches without case or padding, like the gate', () => {
    const shouty = { ...AUTHOR, source_field: ' Work_Log.Author ', target_system: 'MES-North' };
    expect(evidenceForRule(shouty, EVIDENCE)).toBe(EVIDENCE[0]);
  });

  it('counts one shared value in the singular', () => {
    expect(
      ruleEvidenceText(AUTHOR, [
        { ...EVIDENCE[0], overlap_count: 1, examples: ['a'], shown_examples: ['a'] },
      ]),
    ).toBe(
      '1 shared value, e.g. a',
    );
  });
});

/* Two measured pairs in different tables whose columns share their names. */
const STRONG: ResolveEvidence = {
  concept: 'work-order',
  source_field: 'jobs.code',
  target_system: 'mes-north',
  target_field: 'orders.code',
  overlap_count: 4,
  examples: ['J-101', 'J-102'],
  shown_examples: ['J-101', 'J-102'],
  ratio: 1,
};
const WEAK: ResolveEvidence = {
  concept: 'work-order',
  source_field: 'tasks.code',
  target_system: 'mes-north',
  target_field: 'steps.code',
  overlap_count: 1,
  examples: ['T-9'],
  shown_examples: ['T-9'],
  ratio: 0.1,
};
const codeRule = (source: string, target: string, concept = 'work-order'): IdentityRule => ({
  concept,
  source_field: source,
  target_field: target,
  target_system: 'mes-north',
});

describe('same column names in two tables', () => {
  it('gives each qualified rule its own pair and never the other', () => {
    expect(ruleEvidenceText(codeRule('jobs.code', 'orders.code'), [STRONG, WEAK])).toBe(
      '4 shared values, e.g. J-101, J-102',
    );
    expect(ruleEvidenceText(codeRule('tasks.code', 'steps.code'), [STRONG, WEAK])).toBe(
      '1 shared value, e.g. T-9',
    );
    expect(evidenceForRule(codeRule('tasks.code', 'steps.code'), [STRONG])).toBeNull();
    expect(evidenceForRule(codeRule('tasks.code', 'orders.code'), [STRONG, WEAK])).toBeNull();
  });

  it('shows no evidence line for a rule whose dropped table leaves two candidates', () => {
    expect(evidenceForRule(codeRule('code', 'code'), [STRONG, WEAK])).toBeNull();
    expect(ruleEvidenceText(codeRule('code', 'code'), [STRONG, WEAK])).toBeNull();
  });

  it('pairs a rule that adds the system id in front of the measured field', () => {
    expect(evidenceForRule(codeRule('new-sys.jobs.code', 'orders.code'), [STRONG, WEAK])).toBe(
      STRONG,
    );
    expect(evidenceForRule(codeRule('new-sys.tasks.code', 'orders.code'), [STRONG, WEAK])).toBeNull();
  });

  it('pairs a half-qualified rule on the end it qualified', () => {
    expect(evidenceForRule(codeRule('tasks.code', 'code'), [STRONG, WEAK])).toBe(WEAK);
    expect(evidenceForRule(codeRule('code', 'orders.code'), [STRONG, WEAK])).toBe(STRONG);
  });

  it('needs the same concept, or the row declared related, for a column-only pair', () => {
    expect(evidenceForRule(codeRule('code', 'code', 'batch'), [STRONG])).toBeNull();
    const related: ResolveEvidence = { ...STRONG, concept: 'personnel', related_concept: 'batch' };
    expect(evidenceForRule(codeRule('code', 'code', 'batch'), [related])).toBe(related);
  });

  it('renders no evidence line on the ambiguous row, and its own line on the others', () => {
    const rules = [codeRule('code', 'code'), codeRule('jobs.code', 'orders.code')];
    act(() => root.render(<Checkpoint entry={entry(rules, [STRONG, WEAK])} />));
    const rows = [...container.querySelectorAll('table.ob-table')][0]!.querySelectorAll('tbody tr');
    expect(rows).toHaveLength(2);
    expect(rows[0]!.querySelector('.ob-rule-evidence')).toBeNull();
    expect(rows[1]!.querySelector('.ob-rule-evidence')!.textContent).toBe(
      '4 shared values, e.g. J-101, J-102',
    );
    expect(rows[0]!.querySelector('.ob-rule-reject-btn')!.textContent).toBe('✕ reject');
  });
});

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

function entry(rules: IdentityRule[], evidence: ResolveEvidence[]) {
  return {
    id: 11,
    kind: 'checkpoint',
    title: 'Identity Rules',
    content: { kind: 'rules', rules, extras: { evidence } },
    confirmLabel: 'Confirm & Provision →',
    confirmed: false,
    confirmDisabled: false,
    chatOpen: false,
    chatBubbles: [],
    stepContext: { step: 'resolve', systemId: 'site-cmms', data: {}, history: [] },
    onConfirm: () => {},
  } as unknown as Extract<FlowEntry, { kind: 'checkpoint' }>;
}

describe('the Identity Rules checkpoint row', () => {
  it('shows each rule its evidence as text and keeps every label', () => {
    act(() => root.render(<Checkpoint entry={entry([AUTHOR, GUESSED], EVIDENCE)} />));
    const rows = [...container.querySelectorAll('table.ob-table')][0]!.querySelectorAll('tbody tr');
    expect(rows).toHaveLength(2);
    const lines = [...rows].map((tr) => tr.querySelector('.ob-rule-evidence')?.textContent);
    expect(lines).toEqual([
      '12 shared values, e.g. maint_01, maint_04',
      'not measured: both field names look like identifiers',
    ]);
    // The evidence sits in the Fields cell; the Target column is unchanged.
    expect(rows[0]!.querySelectorAll('td')[1]!.textContent).toContain(
      'work_log.author ↔ stops.resolved_by',
    );
    expect(rows[0]!.querySelectorAll('td')[3]!.textContent).toBe(systemName('mes-north'));
    const reject = rows[0]!.querySelector('.ob-rule-reject-btn')!;
    expect(reject.textContent).toBe('✕ reject');
    expect(container.querySelector('.ob-confirm-btn')!.textContent).toBe('Confirm & Provision →');
  });
});
