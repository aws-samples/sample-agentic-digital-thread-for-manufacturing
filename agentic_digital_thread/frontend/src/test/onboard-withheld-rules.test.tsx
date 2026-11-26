// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
/* Withheld identity-rule candidates are listed when some rules were kept.
 *
 * The Identity Rules checkpoint listed the candidates the resolve gates
 * withheld, with each reason, only when it kept NO rule. A checkpoint that
 * kept one rule hid every withheld candidate: a site maintenance system's rule
 * to another plant's MES (withheld: different plants) disappeared without a
 * word, and the workshop's "what did the checkpoint withhold?" question had
 * nothing to read. Both renderers (the guided checkpoint and the resolve
 * result card) now list them under the kept rules. Fixture names are neutral. */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../views/onboard/onboard-queries', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../views/onboard/onboard-queries')>()),
  getOnboardQueryClient: vi.fn(),
  invalidateOnboardingRemap: vi.fn(async () => {}),
  useConceptsQuery: () => ({ data: [], isError: false, refetch: vi.fn() }),
}));

import type { RejectedIdentityRule } from '../api/types/onboard';
import { createQueryClient } from '../api/query-client';
import { systemsQueryOptions } from '../api/systems-query';
import { Checkpoint, WithheldRules } from '../views/onboard/components/Checkpoint';
import { ObFlow } from '../views/onboard/components/ObFlow';
import {
  appendChatCheckpoint,
  resetToWelcome,
  startWizard,
  type FlowEntry,
} from '../views/onboard/flow-store';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

function response(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as Response;
}

const CATALOG = {
  systems: [
    { id: 'site-cmms', name: 'North Maintenance', type: 'CMMS', status: 'connected', plant: 'North' },
    { id: 'mes-north', name: 'North MES', type: 'MES', status: 'connected', plant: 'North' },
    { id: 'mes-south', name: 'South MES', type: 'MES', status: 'connected', plant: 'South' },
  ],
};

const KEPT = {
  concept: 'operator',
  source_field: 'work_log.author',
  target_field: 'stops.resolved_by',
  source_pattern: '*',
  target_pattern: '*',
  target_system: 'mes-north',
};

const WITHHELD: RejectedIdentityRule[] = [
  {
    concept: 'operator',
    source_field: 'work_log.author',
    target_system: 'mes-south',
    target_field: 'stops.resolved_by',
    rejected_reason:
      "different plants (North, South): a value both plants use isn't evidence the two fields name the same thing",
  },
];

const client = createQueryClient();

beforeAll(async () => {
  await client.fetchQuery(systemsQueryOptions(async () => response(CATALOG)));
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
  resetToWelcome();
});

const withheldText = () =>
  [...container.querySelectorAll('[data-testid="rules-withheld"] .ob-rejected-rules li')].map(
    (li) => li.textContent,
  );

const EXPECTED_LINE =
  "work_log.author ↔ stops.resolved_by (South MES) — different plants (North, South): a value both plants use isn't evidence the two fields name the same thing";

describe('withheld identity-rule candidates show beside kept rules', () => {
  it('WithheldRules lists each candidate with the peer system and reason', () => {
    act(() => root.render(<WithheldRules rejected={WITHHELD} />));
    expect(container.textContent).toContain('1 more candidate was withheld:');
    expect(withheldText()).toEqual([EXPECTED_LINE]);
  });

  it('WithheldRules renders nothing when nothing was withheld', () => {
    act(() => root.render(<WithheldRules rejected={[]} />));
    expect(container.innerHTML).toBe('');
  });

  it('the guided checkpoint lists withheld candidates under the kept rule', () => {
    const entry = {
      id: 9,
      kind: 'checkpoint',
      title: 'Identity Rules',
      content: { kind: 'rules', rules: [KEPT], extras: { rejected_rules: WITHHELD } },
      confirmLabel: 'Confirm rules',
      confirmed: false,
      confirmDisabled: false,
      chatOpen: false,
      chatBubbles: [],
      stepContext: { step: 'resolve', systemId: 'site-cmms', data: {}, history: [] },
      onConfirm: () => {},
    } as unknown as Extract<FlowEntry, { kind: 'checkpoint' }>;
    act(() => root.render(<Checkpoint entry={entry} />));
    expect(container.querySelectorAll('table.ob-table tbody tr')).toHaveLength(1);
    expect(withheldText()).toEqual([EXPECTED_LINE]);
    expect(container.querySelector('[data-testid="rules-empty-gated"]')).toBeNull();
  });

  it('the resolve result card lists withheld candidates under the kept rule', () => {
    resetToWelcome();
    startWizard();
    appendChatCheckpoint('resolve', { rules: [KEPT], rejected_rules: WITHHELD });
    act(() => root.render(<ObFlow />));
    expect(withheldText()).toEqual([EXPECTED_LINE]);
  });
});
