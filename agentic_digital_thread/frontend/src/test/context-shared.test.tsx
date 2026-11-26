// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* context-shared — the pure guided-context helpers.
 *
 * These helpers were extracted from the retired standalone guided-context
 * panel (its render tests died with it); the assertions that matter survive
 * here because more than one surface renders them (PreMapProbe today, the
 * conversational context cards next). The honesty rules are contractual:
 * a rejection surfaces the pinned server reason verbatim, a conflict reads
 * as a refusal rather than an error, a partial write never reads as success,
 * and the confirm cost states the real affected-system count. */
import { describe, expect, it } from 'vitest';

import {
  confirmResultLine,
  confirmWarningText,
  landingLabel,
  turnResultLine,
} from '../views/onboard/components/context-shared';

describe('turnResultLine', () => {
  it('reads a drafted answer as captured into the draft', () => {
    const line = turnResultLine({ outcome: 'draft', question_id: 'q' });
    expect(line.text).toMatch(/added to the context draft/i);
  });

  it('reads a proposed answer as a proposal for review, never as applied', () => {
    const line = turnResultLine({ outcome: 'proposed', question_id: 'q' });
    expect(line.text).toMatch(/proposal for review/i);
    expect(line.text).not.toMatch(/saved|applied/i);
  });

  it('reads a skip as skipped', () => {
    expect(turnResultLine({ outcome: 'skipped', question_id: 'q' }).text).toBe('Skipped.');
  });

  it('reads a conflict as a refusal to resolve, not an error to retry', () => {
    const line = turnResultLine({ outcome: 'conflict', question_id: 'q', conflict_with: 'system-a' });
    expect(line.text).toMatch(/system-a/);
    expect(line.text).toMatch(/deactivate/i);
  });

  it('names "unknown" when a conflict arrives without the holder', () => {
    const line = turnResultLine({ outcome: 'conflict', question_id: 'q' });
    expect(line.text).toMatch(/\(unknown\)/);
  });

  it('surfaces the pinned rejection detail verbatim', () => {
    const line = turnResultLine({
      outcome: 'rejected',
      question_id: 'q',
      reason: 'secret_like_content',
      detail: 'answer matches a secret-shaped pattern',
    });
    expect(line.text).toBe('answer matches a secret-shaped pattern');
  });

  it('falls back to the reason, then a generic line, when detail is absent', () => {
    expect(
      turnResultLine({ outcome: 'rejected', question_id: 'q', reason: 'too_long' }).text,
    ).toBe('too_long');
    expect(turnResultLine({ outcome: 'rejected', question_id: 'q' }).text).toBe('Not accepted.');
  });
});

describe('confirmResultLine is honest about partial writes', () => {
  it('a partial write does NOT read as success and keeps the draft', () => {
    const line = confirmResultLine({
      status: 'partial',
      systems: { 'system-a': { written: true }, 'system-b': { written: false, reason: 'write_failed' } },
    });
    expect(line.text).toMatch(/not saved for system-b \(write_failed\)/i);
    expect(line.text).toMatch(/draft has been kept/i);
    expect(line.text).not.toMatch(/^context saved/i);
  });

  it('an unknown system id is named rather than reported written', () => {
    const line = confirmResultLine({
      status: 'partial',
      systems: { nope: { written: false, reason: 'unknown_system' } },
    });
    expect(line.text).toMatch(/unknown_system/);
  });

  it('a failed system without a reason still reads as failed', () => {
    const line = confirmResultLine({
      status: 'partial',
      systems: { 'system-a': { written: false } },
    });
    expect(line.text).toMatch(/not saved for system-a \(failed\)/i);
  });

  it('a successful write with an unconfirmed roll is a caveat, not plain success', () => {
    const line = confirmResultLine({
      status: 'ok',
      systems: { 'system-a': { written: true, roll: 'unknown' } },
    });
    expect(line.text).toMatch(/restart could not be confirmed/i);
    expect(line.text).toMatch(/system-a/);
  });

  it('a fully successful write reads as saved', () => {
    const line = confirmResultLine({
      status: 'ok',
      systems: { 'system-a': { written: true, roll: 'ok' } },
    });
    expect(line.text).toBe('Context saved.');
  });
});

describe('confirmWarningText states the real count', () => {
  it('states the real affected-system count, never a hardcoded one', () => {
    expect(confirmWarningText(['a'])).toMatch(/1 system agent\b/);
    expect(confirmWarningText(['a', 'b', 'c'])).toMatch(/3 system agents/);
    expect(confirmWarningText([])).toMatch(/connect a system/i);
  });
});

describe('landingLabel names the destination honestly', () => {
  it('distinguishes the confirm-gated draft from steward-review proposals', () => {
    expect(landingLabel('business_context_draft')).toMatch(/saved on confirm/i);
    expect(landingLabel('concept_overlay')).toMatch(/pending steward review/i);
    expect(landingLabel('concept_retire')).toMatch(/pending steward review/i);
    expect(landingLabel('identifier_semantics')).toMatch(/pending steward review/i);
    expect(landingLabel('authority_intent')).toMatch(/pending steward review/i);
    expect(landingLabel('query_guidance')).toMatch(/pending steward review/i);
    expect(landingLabel('domain_note')).toMatch(/pending steward review/i);
  });

  it('passes an unknown landing through verbatim rather than inventing a label', () => {
    expect(landingLabel('some_future_landing')).toBe('some_future_landing');
  });
});
