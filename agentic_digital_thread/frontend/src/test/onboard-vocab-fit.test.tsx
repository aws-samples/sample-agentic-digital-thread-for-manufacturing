// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
/* Wave 2 (contract C5) — the map-step vocabulary-fit banner: renders compact
 * counts when the additive vocab_fit block is present, switches to the
 * warning tone below 50% coverage, and stays hidden for absent or malformed
 * blocks (older backends). Generic fixture data only. */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../views/onboard/onboard-queries', () => ({
  getOnboardQueryClient: vi.fn(),
  getCachedConcepts: () => [],
  setConcepts: vi.fn(),
  invalidateOnboardingRemap: vi.fn(async () => {}),
  useConceptsQuery: () => ({ data: [], isError: false, refetch: vi.fn() }),
}));

import type { VocabFit } from '../api/types/onboard';
import { ObFlow } from '../views/onboard/components/ObFlow';
import {
  appendChatCheckpoint,
  appendCheckpoint,
  getEntry,
  resetToWelcome,
  skipRow,
  startWizard,
} from '../views/onboard/flow-store';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const GUIDANCE = 'consider importing a domain concept pack';

function fit(overrides: Partial<VocabFit> = {}): VocabFit {
  return {
    total_columns: 20,
    rows_emitted: 20,
    mapped_confident: 15,
    needs_review: 5,
    reconciled: 2,
    distinct_concepts: 9,
    vocabulary_size: 42,
    coverage_pct: 75,
    ...overrides,
  };
}

const MAPPINGS = [
  {
    field: 'device_registry.line_a.cell_3.temperature_c',
    concept: 'sensor_reading',
    confidence: 0.9,
  },
];

let container: HTMLDivElement;
let root: Root;

function renderMapChatCheckpoint(result: Record<string, unknown>): void {
  appendChatCheckpoint('map', { mappings: MAPPINGS, ...result });
  act(() => root.render(<ObFlow />));
}

function appendMappingCheckpoint(vocabFit?: VocabFit): number {
  return appendCheckpoint({
    title: 'CHECKPOINT — Confirm Mappings',
    content: {
      kind: 'mappingTable',
      rows: [
        {
          field: 'device_registry.line_a.cell_3.temperature_c',
          tags: [{ concept: 'sensor_reading', confidence: 0.9 }],
          review: false,
        },
      ],
      vocab_fit: vocabFit,
    },
    confirmLabel: 'Confirm Mappings →',
    stepContext: { step: 'map', systemId: 'sys_a', data: {}, history: [] },
    onConfirm: () => {},
  });
}

beforeEach(() => {
  resetToWelcome();
  startWizard();
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  resetToWelcome();
});

describe('vocabulary-fit banner (chat map checkpoint)', () => {
  it('renders coverage and counts when vocab_fit is present', () => {
    renderMapChatCheckpoint({ vocab_fit: fit() });

    expect(container.querySelector('.ob-vocab-fit')).not.toBeNull();
    expect(container.textContent).toContain('Vocabulary fit: 75% coverage');
    expect(container.textContent).toContain('15 of 20 columns mapped confidently');
    expect(container.textContent).toContain('5 need review');
    expect(container.textContent).toContain('2 reconciled');
    expect(container.textContent).toContain('42 concepts in vocabulary');
    expect(container.querySelector('.ob-vocab-fit-low')).toBeNull();
    expect(container.textContent).not.toContain(GUIDANCE);
  });

  it('renders nothing when the block is absent (older backend)', () => {
    renderMapChatCheckpoint({});

    expect(container.querySelector('.ob-vocab-fit')).toBeNull();
    expect(container.textContent).not.toContain('Vocabulary fit:');
    // The Field Mappings table itself still renders.
    expect(container.textContent).toContain('Field Mappings');
  });

  it('renders nothing when the block is malformed (non-numeric counter)', () => {
    renderMapChatCheckpoint({
      vocab_fit: { ...fit(), coverage_pct: '75' } as unknown as VocabFit,
    });

    expect(container.querySelector('.ob-vocab-fit')).toBeNull();
  });

  it('stays in the plain tone at exactly 50% coverage', () => {
    renderMapChatCheckpoint({ vocab_fit: fit({ coverage_pct: 50 }) });

    expect(container.textContent).toContain('Vocabulary fit: 50% coverage');
    expect(container.querySelector('.ob-vocab-fit-low')).toBeNull();
    expect(container.textContent).not.toContain(GUIDANCE);
  });

  it('switches to the warning tone with pack guidance below 50% coverage', () => {
    renderMapChatCheckpoint({ vocab_fit: fit({ coverage_pct: 49 }) });

    expect(container.querySelector('.ob-vocab-fit-low')).not.toBeNull();
    expect(container.textContent).toContain(GUIDANCE);
    expect(container.textContent).toContain('Admin → Vocabulary');
  });
});

describe('vocabulary-fit banner (wizard mapping checkpoint)', () => {
  it('renders above the Confirm Mappings checkpoint when the slice carries vocab_fit', () => {
    appendMappingCheckpoint(fit({ coverage_pct: 40 }));
    act(() => root.render(<ObFlow />));

    const banner = container.querySelector('.ob-vocab-fit');
    expect(banner).not.toBeNull();
    expect(container.textContent).toContain(GUIDANCE);
    // Banner precedes the checkpoint card in document order.
    const checkpoint = container.querySelector('.ob-checkpoint');
    expect(checkpoint).not.toBeNull();
    expect(
      banner!.compareDocumentPosition(checkpoint!) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it('renders no banner when the checkpoint slice has no vocab_fit', () => {
    appendMappingCheckpoint(undefined);
    act(() => root.render(<ObFlow />));

    expect(container.querySelector('.ob-vocab-fit')).toBeNull();
    expect(container.querySelector('.ob-checkpoint')).not.toBeNull();
  });

  it('row actions preserve the vocab_fit slice (store carries it immutably)', () => {
    const id = appendMappingCheckpoint(fit());
    skipRow(id, 0);

    const entry = getEntry(id);
    if (!entry || entry.kind !== 'checkpoint') throw new Error('entry');
    if (entry.content.kind !== 'mappingTable') throw new Error('content');
    expect(entry.content.vocab_fit).toEqual(fit());
    expect(entry.content.rows[0].tags[0].skip).toBe(true);
  });
});
