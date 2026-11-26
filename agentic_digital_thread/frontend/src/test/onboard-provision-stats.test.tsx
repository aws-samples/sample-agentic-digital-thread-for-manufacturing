// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../views/onboard/onboard-queries', () => ({
  getOnboardQueryClient: vi.fn(),
  invalidateOnboardingRemap: vi.fn(async () => {}),
  useConceptsQuery: () => ({ data: [], isError: false, refetch: vi.fn() }),
}));

import { ObFlow } from '../views/onboard/components/ObFlow';
import {
  appendChatCheckpoint,
  resetToWelcome,
  startWizard,
} from '../views/onboard/flow-store';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;

function renderProvision(result: Record<string, unknown>): void {
  appendChatCheckpoint('provision', result);
  act(() => root.render(<ObFlow />));
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

describe('provision statistics honesty', () => {
  it('explains catalog-confirmed completion when statistics could not be recovered', () => {
    renderProvision({
      status: 'done',
      stats_unavailable: true,
      tool_name: 'query_cmms',
    });

    expect(container.textContent).toContain(
      'Completion confirmed, but statistics could not be recovered.',
    );
    expect(container.textContent).not.toContain('0 graph nodes');
  });

  it('renders missing unflagged counts as unavailable rather than zero', () => {
    renderProvision({ status: 'done', tool_name: 'query_cmms' });

    expect(container.textContent).toContain(
      'Completion confirmed, but statistics could not be recovered.',
    );
    expect(container.textContent).not.toContain('0 graph nodes');
    expect(container.textContent).not.toContain('none written');
  });

  it('preserves real zero counts from a normal completion', () => {
    renderProvision({
      status: 'done',
      neptune_nodes_created: 0,
      identity_rules_created: 0,
      tool_name: 'query_cmms',
    });

    expect(container.textContent).toContain('Identity rules: none written.');
    expect(container.textContent).not.toContain('could not be recovered');
  });
});
