// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
/* The checkpoint shows only the server's display-safe examples.
 *
 * Resolve's evidence rows and translation checks keep every value they
 * measured (`examples`) for the server's gates. A shared token of hundreds
 * of characters, or one holding a bidirectional override or a line break,
 * would make a checkpoint row very large or read differently from what it
 * holds. The server owns one display policy (resolve_evidence.prompt_values)
 * and sends its result as `shown_examples`; the rule row and the Value
 * Overlap Evidence table render that list as it is and never `examples`.
 * A translation the server checked is described by its check even when the
 * pair's tokens also overlap: that is what the gate judged.
 * Fixture names are neutral. */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import type { IdentityRule, ResolveEvidence, TransformCheck } from '../api/types/onboard';
import { ResolveExtras } from '../views/onboard/components/ResolveExtras';
import { ruleEvidenceText } from '../views/onboard/rule-evidence';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const LONG = `J-${'7'.repeat(300)}`;
const BIDI = 'J-9\u202eevil';

const RULE: IdentityRule = {
  concept: 'work-order',
  source_field: 'jobs.job_ref',
  target_field: 'orders.order_ref',
  source_pattern: '',
  target_pattern: '',
  target_system: 'cell-ops',
};

const ROW: ResolveEvidence = {
  concept: 'work-order',
  source_field: 'jobs.job_ref',
  target_system: 'cell-ops',
  target_field: 'orders.order_ref',
  overlap_count: 4,
  examples: ['J-1', 'J-2', LONG, BIDI],
  shown_examples: ['J-1', 'J-2'],
  ratio: 1,
};

const TRANSLATION: IdentityRule = {
  ...RULE,
  source_pattern: 'WO-{n}',
  target_pattern: 'MFG-{n}',
};

const CHECK: TransformCheck = {
  concept: 'work-order',
  source_field: 'jobs.job_ref',
  target_system: 'cell-ops',
  target_field: 'orders.order_ref',
  source_pattern: 'WO-{n}',
  target_pattern: 'MFG-{n}',
  overlap_count: 4,
  examples: ['MFG-1', `MFG-${'1'.repeat(300)}`, 'MFG-2', 'MFG-\u202e3'],
  shown_examples: ['MFG-1', 'MFG-2'],
};

describe('the rule row', () => {
  it('shows the display-safe examples and never the full list', () => {
    const text = ruleEvidenceText(RULE, [ROW]);
    expect(text).toBe('4 shared values, e.g. J-1, J-2');
    expect(text).not.toContain(LONG);
    expect(text).not.toContain('\u202e');
  });

  it('shows the count alone when no example is safe to show', () => {
    expect(ruleEvidenceText(RULE, [{ ...ROW, shown_examples: [] }])).toBe('4 shared values');
    expect(ruleEvidenceText(RULE, [{ ...ROW, shown_examples: undefined }])).toBe(
      '4 shared values',
    );
  });

  it('describes a checked translation by its check, with its display-safe examples', () => {
    const text = ruleEvidenceText(TRANSLATION, [{ ...ROW, overlap_count: 1 }], [CHECK]);
    expect(text).toBe('4 translated values found in the target, e.g. MFG-1, MFG-2');
    expect(text).not.toContain('\u202e');
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

describe('the Value Overlap Evidence table', () => {
  it('renders the display-safe examples only', () => {
    act(() => root.render(<ResolveExtras res={{ evidence: [ROW] }} />));
    const codes = [...container.querySelectorAll('table.ob-table tbody td code')].map(
      (c) => c.textContent,
    );
    expect(codes).toContain('J-1');
    expect(codes).toContain('J-2');
    expect(container.textContent).not.toContain(LONG);
    expect(container.textContent).not.toContain('\u202e');
  });
});
