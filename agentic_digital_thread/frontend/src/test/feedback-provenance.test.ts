// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Review-queue provenance for feedback-exploration proposals (plan M35):
 * `provenanceLabel` passes a stored `provenance` attribute through verbatim,
 * so a user-feedback exploration row shows "via feedback-exploration" in the
 * self-learning cards — and the legacy fallback for an `insight` row with no
 * provenance stays 'orchestrator'. Also compile-pins the additive
 * `source?: string` on ProposedMapping. */
import { describe, expect, it } from 'vitest';

import type { ProposedMapping } from '../api/types/proposals';
import { provenanceLabel } from '../views/architecture/SlCards';

describe('provenanceLabel — feedback-exploration rows (M35)', () => {
  it('passes the stored provenance through verbatim', () => {
    const m: ProposedMapping = {
      mapping_id: 'm-1',
      proposed_type: 'insight',
      provenance: 'feedback-exploration',
      source: 'user-feedback',
    };
    expect(provenanceLabel(m)).toBe('feedback-exploration');
  });

  it('legacy insight rows (no provenance) fall back to orchestrator', () => {
    const m: ProposedMapping = { mapping_id: 'm-2', proposed_type: 'insight' };
    expect(provenanceLabel(m)).toBe('orchestrator');
  });

  it('analysis-llm mapping is unchanged (passthrough guard)', () => {
    const m: ProposedMapping = { mapping_id: 'm-3', provenance: 'analysis-llm' };
    expect(provenanceLabel(m)).toBe('analysis LLM');
  });
});
