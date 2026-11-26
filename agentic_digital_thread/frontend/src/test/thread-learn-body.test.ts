// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* The "Learn this pattern?" request carries the identifiers the rule was
 * learned from, so POST /api/thread/learn can check that the route seed and
 * the resolver read them alike. The proposal carries the identifier the
 * person typed and the value it matched; the panel sends them back, and fills
 * in the typed identifier itself when an older server's proposal lacks it. A
 * proposal and trace without either send the body as before. Pure. */
import { describe, expect, it } from 'vitest';

import type { LearnProposal } from '../api/types/thread';
import { learnRequestBody } from '../shared/thread/html';

const BASE: LearnProposal = {
  source_pattern: 'WO-{id}',
  target_pattern: 'MFG-WO-{id}',
  confidence: 0.7,
  source_system: 'log-sys',
  target_system: 'line-sys',
};

describe('learnRequestBody', () => {
  it('sends the proposal with the identifier and the matched value it carries', () => {
    const proposal: LearnProposal = {
      ...BASE,
      example_identifier: 'WO-9921',
      example_matched_value: 'MFG-WO-9921',
    };
    expect(learnRequestBody(proposal, 'Orders:WO-9921')).toEqual(proposal);
  });

  it("fills in the identifier the person typed when the proposal doesn't carry it", () => {
    expect(learnRequestBody(BASE, 'Orders:WO-9921')).toEqual({ ...BASE, example_identifier: 'WO-9921' });
  });

  it("keeps the proposal's own identifier over the trace's", () => {
    const proposal: LearnProposal = { ...BASE, example_identifier: 'WO-9921' };
    expect(learnRequestBody(proposal, 'Orders:WO-1')?.example_identifier).toBe('WO-9921');
  });

  it('sends the proposal unchanged when neither carries an identifier', () => {
    expect(learnRequestBody(BASE, '')).toEqual(BASE);
    expect(learnRequestBody(BASE, 'Orders:')).toEqual(BASE);
  });
});
