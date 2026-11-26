// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
/* T7 (C5 wiring): obApplyMutations rebuilds the mappingTable checkpoint
 * content on chat-driven row edits — the rebuild must spread the existing
 * content so the additive vocab_fit block survives (the flow-store
 * updateMappingRows idiom). Generic fixtures only. */
import { beforeEach, describe, expect, it } from 'vitest';

import type { VocabFit } from '../api/types/onboard';
import { obApplyMutations } from '../views/onboard/chat';
import {
  appendCheckpoint,
  getEntry,
  type MappingRowVm,
  resetToWelcome,
  startWizard,
} from '../views/onboard/flow-store';
import type { ObStepContext } from '../views/onboard/state';

const FIT: VocabFit = {
  total_columns: 10,
  rows_emitted: 10,
  mapped_confident: 7,
  needs_review: 3,
  reconciled: 1,
  distinct_concepts: 5,
  vocabulary_size: 40,
  coverage_pct: 70,
};

function mapContext(): ObStepContext {
  return {
    step: 'map',
    systemId: 'sys_a',
    data: { mappings: [{ concept: 'unknown', confidence: 0 }] },
    history: [],
  };
}

function mappingRow(): MappingRowVm {
  return {
    field: 'line_a.cell_3.temperature_c',
    tags: [{ concept: 'sensor-reading', confidence: 0.4 }],
    review: true,
  };
}

function appendMappingCheckpoint(
  ctx: ObStepContext,
  vocabFit?: VocabFit,
): number {
  return appendCheckpoint({
    title: 'CHECKPOINT — Confirm Mappings',
    content: { kind: 'mappingTable', rows: [mappingRow()], vocab_fit: vocabFit },
    confirmLabel: 'Confirm Mappings →',
    stepContext: ctx,
    onConfirm: () => {},
  });
}

function mappingContent(id: number): {
  rows: MappingRowVm[];
  vocab_fit?: VocabFit;
} {
  const entry = getEntry(id);
  if (entry?.kind !== 'checkpoint' || entry.content.kind !== 'mappingTable') {
    throw new Error('expected a mappingTable checkpoint');
  }
  return entry.content;
}

beforeEach(() => {
  resetToWelcome();
  startWizard();
});

describe('obApplyMutations mappingTable rebuild (update_mapping)', () => {
  it('preserves vocab_fit across a chat-driven row edit', () => {
    const ctx = mapContext();
    const id = appendMappingCheckpoint(ctx, FIT);

    obApplyMutations(
      [{ action: 'update_mapping', index: 0, concept: 'temperature', confidence: 0.9 }],
      id,
      ctx,
    );

    const content = mappingContent(id);
    expect(content.vocab_fit).toEqual(FIT);
    expect(content.rows[0].tags[0]).toMatchObject({
      concept: 'temperature',
      confidence: 0.9,
    });
    expect(content.rows[0].review).toBe(false);
  });

  it('leaves vocab_fit absent when the checkpoint never had one', () => {
    const ctx = mapContext();
    const id = appendMappingCheckpoint(ctx);

    obApplyMutations(
      [{ action: 'update_mapping', index: 0, concept: 'temperature' }],
      id,
      ctx,
    );

    expect(mappingContent(id).vocab_fit).toBeUndefined();
  });
});
