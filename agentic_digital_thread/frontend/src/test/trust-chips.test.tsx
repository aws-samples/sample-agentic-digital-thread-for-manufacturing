// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tests — ADR-0063 D2 trust chip on the proposal review card.
 *
 * The chip is DISPLAY of policy the server already computes: it renders the
 * plain-language distinction between "this is a positional guess" and "the
 * target was actually checked", and it renders NOTHING when the signal is
 * absent (absent ≠ false — a missing target_verified means nobody could look).
 * It must never re-derive the policy client-side, so the positional-guess state
 * keys off the server-composed auto_accept_block text.
 *
 * Engine-neutral: sys-a / sys-b, generic placeholder values (ADR-0044). */
// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import type { ProposedMapping } from '../api/types/proposals';
import { SlCard } from '../views/architecture/SlCards';
import { trustChipState } from '../views/architecture/proposal-trust';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

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

const base: ProposedMapping = {
  mapping_id: 'm-1',
  proposed_type: 'value_mapping',
  status: 'pending',
  confidence: '0.9',
  from_system: 'sys-a',
  from_field: 'table_a.col_a',
  from_value: 'source-value-1',
  to_system: 'sys-b',
  to_field: 'table_b.col_b',
  to_value: 'target-name-1',
};

function renderCard(m: ProposedMapping) {
  act(() => {
    root.render(<SlCard m={m} />);
  });
  return container.querySelector('.sl-trust');
}

describe('trustChipState (pure)', () => {
  it('names a positional guess in plain language when the server blocked it', () => {
    expect(
      trustChipState({
        ...base,
        auto_accept_block: 'method=hierarchy_position is a positional guess — human review required',
      }),
    ).toEqual({
      label: 'positional guess — needs a human check',
      title: 'method=hierarchy_position is a positional guess — human review required',
    });
  });

  it('reports a checked target only when a system-scoped check passed', () => {
    expect(
      trustChipState({ ...base, target_verified: true, verified_scope: 'system' })?.label,
    ).toBe('target checked');
  });

  it.each([
    { target_verified: true },
    { target_verified: true, verified_scope: 'field' },
    { target_verified: true, verified_scope: '' },
  ])('is silent for unscoped or non-system legacy verification: %o', (legacy) => {
    expect(trustChipState({ ...base, ...legacy })).toBeNull();
  });

  it('is silent when the signal is ABSENT — absent is not a negative', () => {
    expect(trustChipState(base)).toBeNull();
  });

  it('is silent for a recorded false: unverified is a reason to look, not a chip', () => {
    expect(trustChipState({ ...base, target_verified: false })).toBeNull();
  });

  it('prefers the positional-guess state — the weaker signal is what a steward needs', () => {
    expect(
      trustChipState({
        ...base,
        target_verified: true,
        verified_scope: 'system',
        auto_accept_block: 'method=hierarchy_position is a positional guess — human review required',
      })?.label,
    ).toBe('positional guess — needs a human check');
  });

  it('never re-derives the policy: a hierarchy_position row with no server block is silent', () => {
    expect(trustChipState({ ...base, method: 'hierarchy_position' })).toBeNull();
  });
});

describe('SlCard trust chip rendering', () => {
  it('renders the positional-guess chip', () => {
    const chip = renderCard({
      ...base,
      auto_accept_block: 'method=hierarchy_position is a positional guess — human review required',
    });
    expect(chip?.textContent).toBe('positional guess — needs a human check');
    expect(chip?.className).toContain('sl-trust--guess');
  });

  it('renders the target-checked chip', () => {
    const chip = renderCard({ ...base, target_verified: true, verified_scope: 'system' });
    expect(chip?.textContent).toBe('target checked');
    expect(chip?.className).toContain('sl-trust--checked');
  });

  it('renders no chip when the signals are absent', () => {
    expect(renderCard(base)).toBeNull();
  });

  it('leaves the existing card DOM intact', () => {
    renderCard({ ...base, target_verified: true });
    expect(container.querySelector('.sl-card')?.getAttribute('data-mapping-id')).toBe('m-1');
    expect(container.querySelector('.sl-relation')).not.toBeNull();
  });
});
