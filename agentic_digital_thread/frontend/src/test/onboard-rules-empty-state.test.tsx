// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import type { RejectedIdentityRule } from '../api/types/onboard';
import { RulesEmptyState, isFirstConnectedSystem } from '../views/onboard/components/Checkpoint';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;

function render(rejected: RejectedIdentityRule[], evidenced: boolean, firstSystem = false): void {
  act(() =>
    root.render(
      <RulesEmptyState rejected={rejected} evidenced={evidenced} firstSystem={firstSystem} />,
    ),
  );
}

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

describe('identity-rules empty state is worded by why it is empty', () => {
  it('says "first system" only when no other system is connected', () => {
    render([], false, true);

    expect(container.querySelector('[data-testid="rules-empty-first"]')).not.toBeNull();
    expect(container.textContent).toContain('This is the first system');
    expect(container.textContent).not.toContain('withheld');
  });

  it('says no other system shares values when peers exist but nothing overlapped', () => {
    // The ninth system onboarded with no shared identifier values used to
    // read "this is the first system".
    render([], false);

    expect(container.querySelector('[data-testid="rules-empty-no-overlap"]')).not.toBeNull();
    expect(container.textContent).toContain('no other system shares identifier values');
    expect(container.textContent).not.toContain('first system');
    expect(container.textContent).not.toContain('withheld');
  });

  it('says rules were not evidenced when sampling ran but nothing lined up', () => {
    render([], true);

    expect(container.querySelector('[data-testid="rules-empty-unevidenced"]')).not.toBeNull();
    expect(container.textContent).toContain('did not line up');
    expect(container.textContent).not.toContain('first system');
  });

  it('lists every gated candidate with its field pair and reason', () => {
    // Shapes seen live on the event-18 module-8 solo onboard: measure and
    // temporal concepts the resolve gates now withhold.
    render(
      [
        {
          concept: 'cost',
          source_field: 'unit_cost',
          target_system: 'erp-plant',
          target_field: 'standard_cost',
          rejected_reason: 'measure concept',
        },
        {
          concept: 'schedule',
          source_field: 'scheduled_date',
          target_system: 'mes-ind',
          target_field: 'planned_start',
          rejected_reason: 'shared values are all dates',
        },
      ],
      true,
    );

    const gated = container.querySelector('[data-testid="rules-empty-gated"]');
    expect(gated).not.toBeNull();
    expect(container.textContent).toContain('2 candidates were withheld');
    expect(container.querySelectorAll('.ob-rejected-rules li')).toHaveLength(2);
    expect(container.textContent).toContain('unit_cost');
    expect(container.textContent).toContain('standard_cost');
    // Peer system named through the shared resolver (fallback: no catalog).
    expect(container.textContent).toContain('(ERP Plant)');
    expect(container.textContent).toContain('measure concept');
    expect(container.textContent).toContain('shared values are all dates');
    // Gated wins over the other two wordings even though evidence was present.
    expect(container.textContent).not.toContain('first system');
    expect(container.textContent).not.toContain('did not line up');
  });

  it('uses singular grammar for one withheld candidate', () => {
    render([{ source_field: 'a', target_field: 'b', rejected_reason: 'r' }], false);

    expect(container.textContent).toContain('1 candidate was withheld');
  });
});

describe('isFirstConnectedSystem', () => {
  it('is true only when the catalog is known and no other system is connected', () => {
    expect(isFirstConnectedSystem('sys-a', [])).toBe(false);
    expect(isFirstConnectedSystem('sys-a', [{ id: 'sys-a', status: 'onboarding' }])).toBe(true);
    expect(
      isFirstConnectedSystem('sys-a', [
        { id: 'sys-a', status: 'onboarding' },
        { id: 'sys-b', status: 'available' },
      ]),
    ).toBe(true);
    expect(
      isFirstConnectedSystem('sys-a', [
        { id: 'sys-a', status: 'onboarding' },
        { system_id: 'sys-b', status: 'onboarded' },
      ]),
    ).toBe(false);
    expect(
      isFirstConnectedSystem('sys-a', [{ id: 'sys-b', status: 'removal_failed' }]),
    ).toBe(false);
  });
});
