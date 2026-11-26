// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
/* Raw system ids still on screen on additional surfaces.
 *
 * Onboarding results named the systems they compared by id: Value Overlap
 * Evidence ("↔ cmms-ind"), Connections Discovered / Value Mappings rows, the
 * identity-rule checkpoint's Target column, its reject button's accessible
 * label and the withheld-rule list, and the resolve-result Target column.
 * A scan of the frontend found the same in the runtime questions chip, the
 * metric dry-run cost line, the learned-guidance card header and its clear
 * dialog, and the Admin placement check. Each now reads the catalog name
 * through the shared resolver (lib/system-names.ts); ids stay in keys,
 * request payloads and copyable field paths.
 *
 * The catalog is loaded through the REAL data path (systemsQueryOptions
 * with an injected fetch). Fixture names are neutral. */
import { QueryClientProvider } from '@tanstack/react-query';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../views/onboard/onboard-queries', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../views/onboard/onboard-queries')>()),
  getOnboardQueryClient: vi.fn(),
  invalidateOnboardingRemap: vi.fn(async () => {}),
  useConceptsQuery: () => ({ data: [], isError: false, refetch: vi.fn() }),
}));

import { createQueryClient } from '../api/query-client';
import { systemsQueryOptions } from '../api/systems-query';
import { PlacementControl } from '../pages/admin/tabs/vocabulary/PlacementControl';
import { costText } from '../views/architecture/MetricDryRunFrame';
import { clearSystemGuidance } from '../views/architecture/proposals';
import { LgCard } from '../views/architecture/SlCards';
import { Checkpoint, RulesEmptyState } from '../views/onboard/components/Checkpoint';
import { ObFlow } from '../views/onboard/components/ObFlow';
import { ConnectionsDiscovered, ResolveExtras } from '../views/onboard/components/ResolveExtras';
import {
  RUNTIME_QUESTIONS_URL,
  RuntimeQuestions,
  type RuntimeQuestion,
} from '../views/onboard/components/RuntimeQuestions';
import {
  appendChatCheckpoint,
  resetToWelcome,
  startWizard,
  type FlowEntry,
} from '../views/onboard/flow-store';
import { answerConfirm, confirmText, installConfirmHost } from './confirm-host';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

installConfirmHost();

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
    { id: 'erp', name: 'ERP', type: 'ERP', status: 'connected', plant: 'All Sites' },
    { id: 'mes-ind', name: 'Riverside MES', type: 'MES', status: 'connected', plant: 'Riverside' },
    {
      id: 'cmms-ind',
      name: 'Riverside Plant Maintenance',
      type: 'CMMS',
      status: 'connected',
      plant: 'Riverside',
    },
  ],
};

/** The query client every provider-backed surface below reads (systems
 * cached by the real query options, so useQuery does not refetch). */
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
  vi.unstubAllGlobals();
  resetToWelcome();
});

const text = () => container.textContent ?? '';

describe('onboarding results name the systems they compare', () => {
  it('Value Overlap Evidence names the peer system; the fields stay as code', () => {
    act(() =>
      root.render(
        <ResolveExtras
          res={{
            evidence: [
              {
                concept: 'asset',
                source_field: 'equipment_id',
                target_system: 'cmms-ind',
                target_field: 'asset_num',
                overlap_count: 3,
                examples: ['A-1'],
                ratio: 0.8,
              },
            ],
          }}
        />,
      ),
    );
    const fields = container.querySelector('tbody td:nth-child(2)')!;
    expect(fields.textContent).toBe('equipment_id ↔ Riverside Plant Maintenance asset_num');
    expect([...fields.querySelectorAll('code')].map((c) => c.textContent)).toEqual([
      'equipment_id',
      'asset_num',
    ]);
  });

  it('Connections Discovered and Value Mappings name both systems', () => {
    act(() =>
      root.render(
        <ConnectionsDiscovered
          result={{
            join_paths: [
              {
                from_system: 'mes-ind',
                from_table: 'runs',
                from_field: 'equipment_id',
                to_system: 'cmms-ind',
                to_table: 'assets',
                to_field: 'asset_num',
                method: 'foreign_key_match',
              },
            ],
            value_mappings: [
              {
                from_system: 'cmms-ind',
                from_value: 'A-1',
                to_system: 'mes-ind',
                to_value: 'L2-M1',
                concept: 'asset',
                confidence: 0.95,
                method: 'value_format_match',
              },
            ],
          }}
        />,
      ),
    );
    const rows = [...container.querySelectorAll('tbody tr')].map((tr) =>
      [...tr.querySelectorAll('td')].slice(0, 3).map((td) => td.textContent),
    );
    expect(rows).toEqual([
      ['Riverside MES runs.equipment_id', '→', 'Riverside Plant Maintenance assets.asset_num'],
      ['Riverside Plant Maintenance A-1', '→', 'Riverside MES L2-M1'],
    ]);
    expect(text()).not.toMatch(/mes-ind|cmms-ind/);
  });

  it('identity-rule checkpoint: Target column and the reject button label', () => {
    const entry = {
      id: 7,
      kind: 'checkpoint',
      title: 'Identity Rules',
      content: {
        kind: 'rules',
        rules: [
          {
            concept: 'work_order',
            source_field: 'wo_ref',
            target_field: 'work_order_number',
            source_pattern: '*',
            target_pattern: '*',
            target_system: 'mes-ind',
          },
        ],
        extras: null,
      },
      confirmLabel: 'Confirm rules',
      confirmed: false,
      confirmDisabled: false,
      chatOpen: false,
      chatBubbles: [],
      stepContext: { step: 'resolve', systemId: 'erp', data: {}, history: [] },
      onConfirm: () => {},
    } as unknown as Extract<FlowEntry, { kind: 'checkpoint' }>;
    act(() => root.render(<Checkpoint entry={entry} />));
    const cells = [...container.querySelectorAll('tbody tr td')].map((td) => td.textContent);
    expect(cells[3]).toBe('Riverside MES');
    const reject = container.querySelector('.ob-rule-reject-btn')!;
    expect(reject.getAttribute('aria-label')).toBe('Reject identity rule work_order → Riverside MES');
  });

  it('withheld identity rules name the peer system', () => {
    act(() =>
      root.render(
        <RulesEmptyState
          rejected={[
            {
              concept: 'schedule',
              source_field: 'scheduled_date',
              target_system: 'cmms-ind',
              target_field: 'planned_start',
              rejected_reason: 'shared values are all dates',
            },
          ]}
          evidenced
        />,
      ),
    );
    expect(container.querySelector('.ob-rejected-rules li')!.textContent).toBe(
      'scheduled_date ↔ planned_start (Riverside Plant Maintenance) — shared values are all dates',
    );
  });

  it('the resolve-step result card names the Target system', () => {
    resetToWelcome();
    startWizard();
    appendChatCheckpoint('resolve', {
      rules: [{ concept: 'asset', source_pattern: '*', target_pattern: '*', target_system: 'cmms-ind' }],
    });
    act(() => root.render(<ObFlow />));
    const table = [...container.querySelectorAll('table.ob-table')].find((t) =>
      t.querySelector('th')?.textContent === 'Concept',
    )!;
    const cells = [...table.querySelectorAll('tbody td')].map((td) => td.textContent);
    expect(cells).toEqual(['asset', '* → *', 'Riverside Plant Maintenance']);
  });

  it('the runtime question chip and its promote button name the system', async () => {
    const question: RuntimeQuestion = {
      id: 'q-1',
      system: 'cmms-ind',
      shape: 'context_not_reflected',
      prompt: 'Answers mention hours where the context says minutes.',
      filed_at: '2026-08-24T10:00:00Z',
      requirement_terms: { asserted: 'minutes', negated: 'hours' },
    };
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        if (url === RUNTIME_QUESTIONS_URL) return response({ questions: [question] });
        throw new Error(`unexpected fetch: ${url}`);
      }),
    );
    await act(async () => {
      root.render(
        <QueryClientProvider client={client}>
          <RuntimeQuestions openSeq={1} />
        </QueryClientProvider>,
      );
    });
    await act(async () => {});
    expect(container.querySelector('.rq-system')!.textContent).toBe('Riverside Plant Maintenance');
    expect(container.querySelector('.rq-promote-requirement')!.getAttribute('aria-label')).toBe(
      'Promote structured correction for Riverside Plant Maintenance to answer requirement',
    );
  });
});

describe('other surfaces the frontend scan turned up', () => {
  it('metric dry-run cost line names each system', () => {
    expect(
      costText({
        query_count: 3,
        systems: [
          { system_id: 'mes-ind', query_count: 2 },
          { system_id: 'erp', query_count: 1, scanned_bytes: 2048 },
        ],
      }),
    ).toBe('Measured cost: 3 value queries (Riverside MES: 2; ERP: 1, 2048 bytes scanned).');
  });

  it('learned-guidance card header names the system', () => {
    act(() =>
      root.render(<LgCard sys={{ system_id: 'cmms-ind', notes: ['use asset_num'], length: 13, max_len: 4000 }} />),
    );
    expect(container.querySelector('.sl-type')!.textContent).toBe('Riverside Plant Maintenance');
  });

  it('the Architecture clear-learned dialog names the system', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    let pending!: Promise<void>;
    await act(async () => {
      pending = clearSystemGuidance('mes-ind');
    });
    expect(confirmText().title).toBe('Remove all learned notes for Riverside MES?');
    await answerConfirm('cancel');
    await act(async () => {
      await pending;
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('Admin placement check: option text and confirm name the system with its id', async () => {
    await act(async () => {
      root.render(
        <QueryClientProvider client={client}>
          <PlacementControl systems={[{ system_id: 'cmms-ind' }, { system_id: 'erp' }]} enabled />
        </QueryClientProvider>,
      );
    });
    const options = [...container.querySelectorAll('select option')].map((o) => [
      (o as HTMLOptionElement).value,
      o.textContent,
    ]);
    expect(options).toEqual([
      ['cmms-ind', 'Riverside Plant Maintenance (cmms-ind)'],
      ['erp', 'ERP (erp)'],
    ]);
    const run = [...container.querySelectorAll('button')].find((b) => b.textContent === 'Run scoped check')!;
    await act(async () => run.click());
    expect(document.body.textContent).toContain(
      'This reads context and schema for Riverside Plant Maintenance (cmms-ind) and related onboarded systems.',
    );
  });
});
