// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
/* System names still shown by id on further surfaces.
 *
 * 1. Two more surfaces named systems by id: the Try It comparison's
 *    "Systems queried" chips and routing-delta strip (wire ids derived from
 *    tool names), and the header bell's system-removal rows (server label
 *    "Removing system <id>"). The bell now renders the catalog name and keeps
 *    the id after it; the server label stays the fallback.
 * 2. Surfaces that call the resolver must re-render when names arrive AFTER
 *    they mounted. Every test below mounts with an EMPTY resolver, asserts
 *    the id-derived fallback, then loads the catalog through the real data
 *    path (systemsQueryOptions with an injected fetch) and asserts the
 *    mounted text and ARIA labels now read the catalog name.
 *
 * Fixture names are neutral. */
import { QueryClientProvider } from '@tanstack/react-query';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../views/onboard/onboard-queries', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../views/onboard/onboard-queries')>()),
  getOnboardQueryClient: vi.fn(),
  invalidateOnboardingRemap: vi.fn(async () => {}),
  useConceptsQuery: () => ({ data: [], isError: false, refetch: vi.fn() }),
}));

import { createQueryClient } from '../api/query-client';
import { systemsQueryOptions } from '../api/systems-query';
import type { TryItResult } from '../api/types/learning';
import type { ProposedMapping } from '../api/types/proposals';
import type { MetricDefinitionDryRun, MetricDefinitionPreview } from '../api/types/vocabulary';
import { _resetSystemNames } from '../lib/system-names';
import NotificationBell from '../pages/app/NotificationBell';
import { notificationsStore, type NotificationItem } from '../pages/app/notifications-store';
import { PlacementControl } from '../pages/admin/tabs/vocabulary/PlacementControl';
import MetricDryRunFrame from '../views/architecture/MetricDryRunFrame';
import { LgCard, SlCard } from '../views/architecture/SlCards';
import { _resetStewardCheck, TryItPanel } from '../views/architecture/TryItPanel';
import { Checkpoint, RulesEmptyState } from '../views/onboard/components/Checkpoint';
import { ContextInterviewCard } from '../views/onboard/components/ContextInterviewCard';
import { ObFlow } from '../views/onboard/components/ObFlow';
import { PreMapProbe } from '../views/onboard/components/PreMapProbe';
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

/** GET /api/systems answering through the same query options the app uses. */
async function loadCatalog(): Promise<void> {
  await act(async () => {
    await createQueryClient().fetchQuery(systemsQueryOptions(async () => response(CATALOG)));
  });
}

/** fetch router: each test sets `route`; unknown URLs answer `{}`. */
type Route = (url: string, init?: RequestInit) => unknown;
let route: Route = () => undefined;

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  _resetSystemNames();
  _resetStewardCheck();
  route = () => undefined;
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => response((await route(String(url), init)) ?? {})),
  );
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  notificationsStore.set({ items: [], loaded: false });
  resetToWelcome();
});

const text = (sel: string) => container.querySelector(sel)?.textContent ?? '';

/* ── 1. Try It and the notification bell ─────────────────────── */

const MAPPING: ProposedMapping = {
  mapping_id: 'm-try-1',
  proposed_type: 'join_path',
  status: 'pending',
  confidence: '0.8',
  from_system: 'mes-ind',
  from_field: 'runs.equipment_id',
  to_system: 'cmms-ind',
  to_field: 'assets.asset_num',
  query_context: 'Which work orders touch line 2?',
};

const TRYIT_RESULT: TryItResult = {
  question: 'Which work orders touch line 2?',
  baseline: {
    systems_queried: ['mes-ind'],
    rules_applied: [],
    translation_steps: [],
    answer_excerpt: 'baseline answer',
    elapsed_seconds: 100,
    injected: false,
  },
  candidate: {
    systems_queried: ['mes-ind', 'cmms-ind'],
    rules_applied: [],
    translation_steps: [],
    answer_excerpt: 'candidate answer',
    elapsed_seconds: 110,
    injected: true,
  },
  diff: { systems_added: ['cmms-ind'], systems_removed: [], rules_added: [], rules_removed: [] },
  candidate_injected: true,
};

describe('Try It names the systems each run queried', () => {
  it('chips and the routing delta read the catalog name, also when it arrives after the result', async () => {
    vi.useFakeTimers();
    route = (url) => {
      if (url === '/api/admin/learning/tryit') return { job_id: 'j1' };
      if (url === '/api/admin/learning/tryit/j1') return { status: 'done', result: TRYIT_RESULT };
      return undefined;
    };
    await act(async () => {
      root.render(<TryItPanel m={MAPPING} onClose={() => {}} />);
    });
    await act(async () => {
      (container.querySelector('.tryit-launch') as HTMLButtonElement).click();
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000);
    });
    vi.useRealTimers();
    const chips = () =>
      [...container.querySelectorAll('.tryit-col')].map((col) =>
        [...col.querySelectorAll('.sl-chip')].map((c) => [c.textContent, c.getAttribute('title')]),
      );

    // Before the catalog: the id-derived fallback, never the raw id.
    expect(chips()).toEqual([
      [['MES Ind', 'mes-ind']],
      [
        ['MES Ind', 'mes-ind'],
        ['CMMS Ind', 'cmms-ind'],
      ],
    ]);
    expect(text('.tryit-diff-strip')).toBe('＋ systems: CMMS Ind');

    await loadCatalog();
    expect(chips()).toEqual([
      [['Riverside MES', 'mes-ind']],
      [
        ['Riverside MES', 'mes-ind'],
        ['Riverside Plant Maintenance', 'cmms-ind'],
      ],
    ]);
    expect(text('.tryit-diff-strip')).toBe('＋ systems: Riverside Plant Maintenance');
  });
});

describe('notification bell names a system being removed', () => {
  const FEED: { items: NotificationItem[] } = {
    items: [
      { kind: 'removal', label: 'Removing system cmms-ind', state: 'running', system_id: 'cmms-ind', href: '/#onboard' },
      { kind: 'removal', label: 'Removal of mes-ind finished', state: 'done', system_id: 'mes-ind', href: '/#onboard' },
      { kind: 'removal', label: 'Removal of qms-west failed', state: 'error', system_id: 'qms-west', href: '/#onboard' },
      { kind: 'import', label: 'Graph snapshot import finished', state: 'done', href: '/admin.html' },
    ],
  };

  async function mountAndOpen(): Promise<void> {
    route = (url) => (url === '/api/notifications' ? FEED : undefined);
    await act(async () => {
      root.render(<NotificationBell />);
    });
    await act(async () => {});
    await act(async () => {
      document
        .getElementById('notification-bell-trigger')!
        .dispatchEvent(new MouseEvent('click', { bubbles: true }));
    });
    await act(async () => {});
  }

  const labels = () =>
    [...document.querySelectorAll('#notification-dropdown .notif-item-label')].map((n) => n.textContent);

  it('shows "<name> (<id>)" once the catalog knows the system; the server label otherwise', async () => {
    await mountAndOpen();
    // Nothing known yet: the server's own labels.
    expect(labels()).toEqual([
      'Removing system cmms-ind',
      'Removal of mes-ind finished',
      'Removal of qms-west failed',
      'Graph snapshot import finished',
    ]);

    await loadCatalog();
    expect(labels()).toEqual([
      'Removing Riverside Plant Maintenance (cmms-ind)',
      'Removal of Riverside MES (mes-ind) finished',
      // Not in the catalog: the server label stays.
      'Removal of qms-west failed',
      'Graph snapshot import finished',
    ]);
  });
});

/* ── 2. Surfaces mounted before the catalog ──────────────────── */

function dryRun(): MetricDefinitionDryRun {
  return {
    preview: true,
    metric_id: 'widget-count',
    label: 'Widget Count',
    unit: 'widgets',
    status: 'ok',
    value: 42,
    sample_count: 42,
    system_id: 'mes-ind',
    lineage: { kind: 'direct' },
    window: '24h',
    computed_at: '2026-09-25T12:00:00Z',
    cost: {
      query_count: 3,
      systems: [
        { system_id: 'mes-ind', query_count: 2 },
        { system_id: 'cmms-ind', query_count: 1, scanned_bytes: 2048 },
      ],
    },
  };
}

describe('surfaces mounted before the catalog relabel when it arrives', () => {
  it('metric dry-run cost line', async () => {
    const onFetch = vi.fn(async (): Promise<MetricDefinitionPreview> => ({ dry_run: dryRun() }));
    await act(async () => {
      root.render(<MetricDryRunFrame mappingId="p-1" withIds={[]} onClose={() => {}} onFetch={onFetch} />);
    });
    await vi.waitFor(() => expect(text('.sl-metric-dryrun-cost')).not.toBe(''));
    expect(text('.sl-metric-dryrun-cost')).toBe(
      'Measured cost: 3 value queries (MES Ind: 2; CMMS Ind: 1, 2048 bytes scanned).',
    );
    await loadCatalog();
    expect(text('.sl-metric-dryrun-cost')).toBe(
      'Measured cost: 3 value queries (Riverside MES: 2; Riverside Plant Maintenance: 1, 2048 bytes scanned).',
    );
  });

  it('learned-guidance card header', async () => {
    act(() => root.render(<LgCard sys={{ system_id: 'cmms-ind', notes: ['n'], length: 1, max_len: 4000 }} />));
    expect(text('.sl-type')).toBe('CMMS Ind');
    await loadCatalog();
    expect(text('.sl-type')).toBe('Riverside Plant Maintenance');
  });

  it('proposal card: "Joins:" line and the schema-check sentence', async () => {
    const m: ProposedMapping = {
      ...MAPPING,
      touches: { systems: ['mes-ind', 'cmms-ind'], concept: 'equipment-id' },
      schema_verdict: 'not_found',
      schema_checked: 'runs.equipment_id',
    };
    await act(async () => {
      root.render(<SlCard m={m} />);
    });
    expect(text('.sl-touches')).toBe('Joins: MES Ind ↔ CMMS Ind · concept equipment-id');
    expect(text('.sl-block-proof')).toContain('the onboarded MES Ind schema');
    await loadCatalog();
    expect(text('.sl-touches')).toBe(
      'Joins: Riverside MES ↔ Riverside Plant Maintenance · concept equipment-id',
    );
    expect(text('.sl-block-proof')).toContain('the onboarded Riverside MES schema');
  });

  it('runtime question chip and the promote button label', async () => {
    const question: RuntimeQuestion = {
      id: 'q-1',
      system: 'cmms-ind',
      shape: 'context_not_reflected',
      prompt: 'Answers mention hours where the context says minutes.',
      filed_at: '2026-08-24T10:00:00Z',
      requirement_terms: { asserted: 'minutes', negated: 'hours' },
    };
    route = (url) => (url === RUNTIME_QUESTIONS_URL ? { questions: [question] } : undefined);
    await act(async () => {
      root.render(
        <QueryClientProvider client={createQueryClient()}>
          <RuntimeQuestions openSeq={1} />
        </QueryClientProvider>,
      );
    });
    await act(async () => {});
    const aria = () => container.querySelector('.rq-promote-requirement')!.getAttribute('aria-label');
    expect(text('.rq-system')).toBe('CMMS Ind');
    expect(aria()).toBe('Promote structured correction for CMMS Ind to answer requirement');
    await loadCatalog();
    expect(text('.rq-system')).toBe('Riverside Plant Maintenance');
    expect(aria()).toBe(
      'Promote structured correction for Riverside Plant Maintenance to answer requirement',
    );
  });

  it('Value Overlap Evidence and Connections Discovered', async () => {
    act(() =>
      root.render(
        <>
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
          />
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
            }}
          />
        </>,
      ),
    );
    const cells = () => [...container.querySelectorAll('tbody td')].map((td) => td.textContent);
    expect(cells()).toContain('equipment_id ↔ CMMS Ind asset_num');
    expect(cells()).toContain('MES Ind runs.equipment_id');
    await loadCatalog();
    expect(cells()).toContain('equipment_id ↔ Riverside Plant Maintenance asset_num');
    expect(cells()).toContain('Riverside MES runs.equipment_id');
    expect(cells()).toContain('Riverside Plant Maintenance assets.asset_num');
  });

  it('identity-rule checkpoint Target, reject label and the withheld list', async () => {
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
    act(() =>
      root.render(
        <>
          <Checkpoint entry={entry} />
          <RulesEmptyState
            rejected={[{ concept: 'schedule', source_field: 'a', target_field: 'b', target_system: 'cmms-ind' }]}
            evidenced
          />
        </>,
      ),
    );
    const target = () => container.querySelectorAll('tbody tr td')[3]!.textContent;
    const aria = () => container.querySelector('.ob-rule-reject-btn')!.getAttribute('aria-label');
    expect(target()).toBe('MES Ind');
    expect(aria()).toBe('Reject identity rule work_order → MES Ind');
    expect(text('.ob-rejected-rules li')).toBe('a ↔ b (CMMS Ind)');
    await loadCatalog();
    expect(target()).toBe('Riverside MES');
    expect(aria()).toBe('Reject identity rule work_order → Riverside MES');
    expect(text('.ob-rejected-rules li')).toBe('a ↔ b (Riverside Plant Maintenance)');
  });

  it('the resolve-step result card', async () => {
    startWizard();
    appendChatCheckpoint('resolve', {
      rules: [{ concept: 'asset', source_pattern: '*', target_pattern: '*', target_system: 'cmms-ind' }],
    });
    act(() => root.render(<ObFlow />));
    const target = () => {
      const table = [...container.querySelectorAll('table.ob-table')].find(
        (t) => t.querySelector('th')?.textContent === 'Concept',
      )!;
      return [...table.querySelectorAll('tbody td')].map((td) => td.textContent)[2];
    };
    expect(target()).toBe('CMMS Ind');
    await loadCatalog();
    expect(target()).toBe('Riverside Plant Maintenance');
  });

  it('context interview card title and save button', async () => {
    route = (url) =>
      url.startsWith('/api/onboard/guided/state')
        ? { first_launch: false, dismissed: false, answered_topics: [], draft_business_context: 'x' }
        : { system_id: 'cmms-ind', results: [], derived: [], questions: [], draft_business_context: 'x' };
    await act(async () => {
      root.render(<ContextInterviewCard systemId="cmms-ind" firstLaunch={false} />);
    });
    await act(async () => {});
    const save = () =>
      [...container.querySelectorAll('button')].find((b) => /save context/i.test(b.textContent ?? ''))
        ?.textContent;
    expect(text('h4')).toBe('✦ Business context — CMMS Ind');
    expect(save()).toBe('Save context for CMMS Ind');
    await loadCatalog();
    expect(text('h4')).toBe('✦ Business context — Riverside Plant Maintenance');
    expect(save()).toBe('Save context for Riverside Plant Maintenance');
  });

  it('pre-map probe header', async () => {
    route = (url) =>
      url.startsWith('/api/onboard/guided/probe/')
        ? {
            system_id: 'cmms-ind',
            questions: [{ id: 'retry-semantics', prompt: 'When work is retried, what happens?', evidence: 'e' }],
          }
        : undefined;
    await act(async () => {
      root.render(
        <PreMapProbe
          systemId="cmms-ind"
          tables={[{ name: 'work_orders', columns: [{ name: 'state' }] }]}
          onContinue={() => {}}
        />,
      );
    });
    await act(async () => {});
    expect(container.textContent).toContain('Before mapping CMMS Ind');
    await loadCatalog();
    expect(container.textContent).toContain('Before mapping Riverside Plant Maintenance');
  });

  it('Admin placement check reads the names once /api/systems answers', async () => {
    let release!: () => void;
    const gate = new Promise<void>((r) => (release = r));
    route = (url) => (url === '/api/systems' ? gate.then(() => CATALOG) : undefined);
    await act(async () => {
      root.render(
        <QueryClientProvider client={createQueryClient()}>
          <PlacementControl systems={[{ system_id: 'cmms-ind' }, { system_id: 'erp' }]} enabled />
        </QueryClientProvider>,
      );
    });
    const options = () => [...container.querySelectorAll('select option')].map((o) => o.textContent);
    expect(options()).toEqual(['cmms-ind', 'erp']);
    await act(async () => {
      release();
      await gate;
    });
    await vi.waitFor(() =>
      expect(options()).toEqual(['Riverside Plant Maintenance (cmms-ind)', 'ERP (erp)']),
    );
  });
});
