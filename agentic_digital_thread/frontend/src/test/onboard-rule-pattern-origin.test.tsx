// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
/* The Identity Rules checkpoint says where a rule's pattern came from.
 *
 * When the model's pattern pair cannot be stored but the values the two
 * fields share are the same text in both, Resolve lists the rule with no
 * pattern and marks it (`pattern_origin`). The checkpoint showed `* → *`
 * as if the model had written it. Now a short note under the rule says the
 * pattern came from the measured values. A rule kept on a translation the
 * server measured (`transform_checks`) says how many translated values it
 * found, instead of "not measured". A rule withheld because provision could
 * not store its pattern shows provision's reason in the withheld list.
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

import type {
  IdentityRule,
  RejectedIdentityRule,
  ResolveEvidence,
  TransformCheck,
} from '../api/types/onboard';
import { Checkpoint } from '../views/onboard/components/Checkpoint';
import type { FlowEntry } from '../views/onboard/flow-store';
import {
  COMPLETED_PATTERN_NOTE,
  PATTERN_ORIGIN_NOTE,
  patternOriginNote,
  ruleEvidenceText,
} from '../views/onboard/rule-evidence';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const REBUILT: IdentityRule = {
  concept: 'personnel',
  source_field: 'crew_log.entered_by',
  target_field: 'stops.cleared_by',
  source_pattern: '',
  target_pattern: '',
  target_system: 'cell-ops',
  pattern_origin: 'set from the measurement, not the model',
};
const AS_WRITTEN: IdentityRule = { ...REBUILT, pattern_origin: undefined };
const TRANSLATED: IdentityRule = {
  concept: 'work-order',
  source_field: 'jobs.job_ref',
  target_field: 'orders.order_ref',
  source_pattern: 'J-{n}',
  target_pattern: 'ORD-{n}',
  target_system: 'cell-ops',
};
const EVIDENCE: ResolveEvidence[] = [
  {
    concept: 'personnel',
    source_field: 'crew_log.entered_by',
    target_system: 'cell-ops',
    target_field: 'stops.cleared_by',
    overlap_count: 4,
    examples: ['tech_a1', 'tech_a2'],
    shown_examples: ['tech_a1', 'tech_a2'],
    ratio: 1,
  },
];
const CHECKS: TransformCheck[] = [
  {
    concept: 'work-order',
    source_field: 'jobs.job_ref',
    target_system: 'cell-ops',
    target_field: 'orders.order_ref',
    source_pattern: 'J-{n}',
    target_pattern: 'ORD-{n}',
    overlap_count: 3,
    examples: ['ORD-101', 'ORD-102', 'ORD-103', 'ORD-104'],
    shown_examples: ['ORD-101', 'ORD-102', 'ORD-103', 'ORD-104'],
  },
];
const REGEX_REASON =
  "pattern '^[a-z]+$' -> '^[A-Z]+$' is not a valid pattern: the source pattern names no " +
  '{placeholder} and the two sides differ, so it transforms nothing';
const WITHHELD: RejectedIdentityRule = {
  ...TRANSLATED,
  source_field: 'jobs.owner',
  source_pattern: '^[a-z]+$',
  target_pattern: '^[A-Z]+$',
  rejected_reason: REGEX_REASON,
};

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

function entry(rules: IdentityRule[], extras: Record<string, unknown>) {
  return {
    id: 12,
    kind: 'checkpoint',
    title: 'Identity Rules',
    content: { kind: 'rules', rules, extras },
    confirmLabel: 'Confirm & Provision →',
    confirmed: false,
    confirmDisabled: false,
    chatOpen: false,
    chatBubbles: [],
    stepContext: { step: 'resolve', systemId: 'line-upkeep', data: {}, history: [] },
    onConfirm: () => {},
  } as unknown as Extract<FlowEntry, { kind: 'checkpoint' }>;
}

function rowLines(): string[][] {
  const rows = [...container.querySelectorAll('table.ob-table')][0]!.querySelectorAll('tbody tr');
  return [...rows].map((tr) =>
    [...tr.querySelectorAll('.ob-rule-evidence')].map((el) => el.textContent || ''),
  );
}

describe('the pattern-origin note', () => {
  it('is shown under a rule whose pattern came from the measured values', () => {
    act(() =>
      root.render(<Checkpoint entry={entry([REBUILT, AS_WRITTEN], { evidence: EVIDENCE })} />),
    );
    expect(rowLines()).toEqual([
      ['4 shared values, e.g. tech_a1, tech_a2', PATTERN_ORIGIN_NOTE],
      ['4 shared values, e.g. tech_a1, tech_a2'],
    ]);
  });

  it('is generic and has no dash', () => {
    expect(PATTERN_ORIGIN_NOTE).toMatch(/measured/);
    expect(PATTERN_ORIGIN_NOTE).not.toMatch(/[\u2013\u2014]/);
  });

  it('says a pattern completed with the literal text its values share was completed', () => {
    const completed: IdentityRule = {
      ...TRANSLATED,
      source_pattern: 'WO{id}',
      target_pattern: 'MFG-WO{id}',
      pattern_origin: 'set from the measurement',
    };
    expect(patternOriginNote(completed)).toBe(COMPLETED_PATTERN_NOTE);
    expect(patternOriginNote(REBUILT)).toBe(PATTERN_ORIGIN_NOTE);
    expect(patternOriginNote(TRANSLATED)).toBeNull();
    expect(COMPLETED_PATTERN_NOTE).toMatch(/measured/);
    expect(COMPLETED_PATTERN_NOTE).not.toMatch(/[\u2013\u2014]/);
  });
});

describe('a translation the server measured', () => {
  it('says how many translated values were found, not that nothing was measured', () => {
    expect(ruleEvidenceText(TRANSLATED, [], CHECKS)).toBe(
      '3 translated values found in the target, e.g. ORD-101, ORD-102, ORD-103',
    );
    expect(ruleEvidenceText({ ...TRANSLATED, source_pattern: 'J{n}' }, [], CHECKS)).toBe(
      'not measured: both field names look like identifiers',
    );
  });

  it('renders that line on the row', () => {
    act(() =>
      root.render(
        <Checkpoint entry={entry([TRANSLATED], { evidence: [], transform_checks: CHECKS })} />,
      ),
    );
    expect(rowLines()).toEqual([
      ['3 translated values found in the target, e.g. ORD-101, ORD-102, ORD-103'],
    ]);
  });
});

describe('a rule withheld for its pattern', () => {
  it("shows provision's reason in the withheld list", () => {
    act(() =>
      root.render(
        <Checkpoint
          entry={entry([REBUILT], { evidence: EVIDENCE, rejected_rules: [WITHHELD] })}
        />,
      ),
    );
    const withheld = container.querySelector('[data-testid="rules-withheld"]')!;
    expect(withheld.textContent).toContain(REGEX_REASON);
  });
});
