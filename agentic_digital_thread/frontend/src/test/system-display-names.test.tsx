// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
/* One display name per system. In a workshop dry run the plant
 * MES (`mes-ind`) read as "MES Ind" (chat activity line and call row),
 * "mes-ind" (Sources chip), "Mes Ind" (investigation graph, HUD, citation
 * fallback) and its catalog name (Onboard, Threads) depending on where you
 * looked. Fixture names are neutral; the shape is the workshop catalog's.
 *
 * Every surface a person reads now resolves a system id through ONE
 * resolver (lib/system-names.ts): the catalog display name from
 * GET /api/systems, else a readable fallback built from the id. The catalog
 * is loaded here through the REAL data path (systemsQueryOptions with an
 * injected fetch), so these tests exercise the same wiring the app uses.
 * Identifiers a person types or copies (field paths like
 * `mes-ind.work_order_ref`, the Remove dialog's type-to-confirm) keep the
 * system id. */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

import { requestJson } from '../api/client';
import { createQueryClient } from '../api/query-client';
import { systemsQueryOptions } from '../api/systems-query';
import { ChatThread } from '../chat/ChatThread';
import { createThreadStore, type ThreadStore } from '../chat/thread-store';
import { callRowLabel, describeCalls } from '../chat/tool-activity';
import {
  getCitationLabel,
  getSystemLabel,
  getToolLabel,
  systemDisplayName,
} from '../chat/tool-labels';
import type { ToolPill, ToolTrace } from '../chat/turn-model';
import { deriveInvNode, getSystemLabel as archSystemLabel } from '../lib/arch-store';
import { setLang } from '../lib/i18n';
import { nameById, pairGaps } from '../shared/thread/html';
import {
  AuthorityRuleCard,
  IdentityBindingCard,
  JoinCard,
  RankedPaths,
  ValueCard,
} from '../shared/thread/panels';
import { PreMapProbe } from '../views/onboard/components/PreMapProbe';
import { ContextInterviewCard } from '../views/onboard/components/ContextInterviewCard';

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>();
  return { ...actual, requestJson: vi.fn() };
});

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

function response(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: 'OK',
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as Response;
}

/** The shape GET /api/systems returns on a workshop account (ids as in the
 * workshop manifest, neutral names), plus one system a user registered by
 * hand. */
const CATALOG = {
  systems: [
    { id: 'erp', name: 'ERP', type: 'ERP', status: 'connected', plant: 'All Sites' },
    { id: 'mes-ind', name: 'Riverside MES', type: 'MES', status: 'connected', plant: 'Riverside' },
    {
      id: 'cmms-ind',
      name: 'Riverside Plant Maintenance',
      type: 'CMMS',
      status: 'registered',
      plant: 'Riverside',
    },
    { id: 'sitewise-direct', name: 'AWS IoT SiteWise', type: 'Historian', status: 'connected', plant: 'Riverside' },
    { id: 'mes-mil', name: 'Lakeside MES', type: 'MES', status: 'connected', plant: 'Lakeside' },
    // User-registered (Connect New System): the catalog name is what was typed.
    { id: 'quality-lab', name: 'Example Corp Quality Lab', type: 'QMS', status: 'connected', plant: 'Riverside' },
  ],
};

beforeAll(async () => {
  await createQueryClient().fetchQuery(systemsQueryOptions(async () => response(CATALOG)));
});

const pill = (toolId: string, tool: string, extra: Partial<ToolPill> = {}): ToolPill => ({
  toolId,
  tool,
  label: tool,
  kgq: false,
  done: true,
  segBoundary: 0,
  ...extra,
});

const trace = (toolId: string, tool: string, extra: Partial<ToolTrace> = {}): ToolTrace => ({
  toolId,
  tool,
  toolInput: null,
  resultMd: '',
  visible: true,
  ...extra,
});

describe('chat activity lines and call rows use the catalog name', () => {
  it('fold / activity text names the plant systems as the catalog does', () => {
    const pills = [
      pill('a', 'mes-ind___query_mes_ind'),
      pill('b', 'cmms-ind___query_cmms_ind'),
    ];
    expect(describeCalls(pills, 'past')).toBe('Queried Riverside MES and Riverside Plant Maintenance');
    expect(describeCalls(pills.slice(0, 1), 'present')).toBe('Querying Riverside MES');
  });

  it('a SiteWise call reads with its catalog name', () => {
    expect(describeCalls([pill('s', 'sitewise-direct___get_asset_property_history')], 'past')).toBe(
      'Queried AWS IoT SiteWise',
    );
  });

  it('a user-registered system reads with the name the user entered', () => {
    expect(describeCalls([pill('q', 'quality-lab___query_quality_lab')], 'past')).toBe(
      'Queried Example Corp Quality Lab',
    );
  });

  it('the call row reads "<catalog name> · <question>"', () => {
    const p = pill('a', 'mes-ind___query_mes_ind');
    const t = trace('a', 'mes-ind___query_mes_ind', { toolInput: { question: 'runs for RUN-1' } });
    expect(callRowLabel(p, t)).toBe('Riverside MES · runs for RUN-1');
  });

  it('a local query_* tool resolves its underscore id to the same name', () => {
    expect(describeCalls([pill('l', 'query_mes_ind')], 'past')).toBe('Queried Riverside MES');
  });

  it('systemDisplayName returns the catalog name for a known id', () => {
    expect(systemDisplayName('mes-ind')).toBe('Riverside MES');
    expect(systemDisplayName('cmms-ind')).toBe('Riverside Plant Maintenance');
  });

  it('an id the catalog does not know keeps the readable fallback', () => {
    expect(systemDisplayName('qms-west')).toBe('QMS West');
    expect(describeCalls([pill('u', 'qms-west___query_qms_west')], 'past')).toBe('Queried QMS West');
  });
});

describe('graph, HUD and citation labels use the catalog name', () => {
  it('chat getSystemLabel (graph nodes, Agent Analysis bar)', () => {
    expect(getSystemLabel('mes-ind___query_mes_ind')).toBe('Riverside MES');
    expect(getSystemLabel('mes-mil___query_mes_mil')).toBe('Lakeside MES');
  });

  it('arch-store getSystemLabel and the investigation node (graph, HUD, trace steps)', () => {
    expect(archSystemLabel('cmms-ind___query_cmms_ind')).toBe('Riverside Plant Maintenance');
    expect(deriveInvNode('mes-ind___query_mes_ind').label).toBe('Riverside MES');
  });

  it('the Sources fallback label (no table on the record)', () => {
    expect(getCitationLabel('cmms-ind___query_cmms_ind')).toBe('Riverside Plant Maintenance');
  });

  it('the tool-pill label leads with the catalog name, not the raw id', () => {
    expect(getToolLabel('mes-ind___query_mes_ind').startsWith('Riverside MES — ')).toBe(true);
  });

  it('unknown ids keep the existing fallbacks', () => {
    expect(getSystemLabel('lake-athena___athena_run_query')).toBe('Lake Athena');
    expect(archSystemLabel('lake-athena___athena_run_query')).toBe('Lake Athena');
  });
});

describe('rendered chat turn: fold summary and Sources chips', () => {
  let container: HTMLDivElement;
  let root: Root;
  let store: ThreadStore;

  beforeEach(() => {
    const ls = new Map<string, string>();
    vi.stubGlobal('localStorage', {
      getItem: (k: string) => ls.get(k) ?? null,
      setItem: (k: string, v: string) => void ls.set(k, String(v)),
      removeItem: (k: string) => void ls.delete(k),
    });
    setLang('en');
    vi.mocked(requestJson).mockResolvedValue({ status: 'success' });
    container = document.createElement('div');
    container.id = 'messages';
    document.body.appendChild(container);
    root = createRoot(container);
    store = createThreadStore();
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  });

  function replay(records: unknown[]) {
    act(() => {
      store.startTurn('what maintenance was done on the CMX-441?');
      store.applyEvent('token', { text: 'Checking maintenance.' });
      store.applyEvent('tool_start', {
        tool: 'cmms-ind___query_cmms_ind',
        tool_id: 't1',
        input: { question: 'work orders for 1001-CMX-441' },
      });
      store.applyEvent('tool_result', { tool: 'cmms-ind___query_cmms_ind', tool_id: 't1', input: 'x', summary: '3 rows' });
      store.applyEvent('tool_end', { tool: 'cmms-ind___query_cmms_ind', tool_id: 't1' });
      store.applyEvent('metadata', { provenance: { records } });
      store.applyEvent('token', { text: 'Three work orders [1].' });
      store.applyEvent('done', { status: 'complete' });
    });
    act(() => root.render(<ChatThread store={store} />));
  }

  const record = (extra: Record<string, unknown> = {}) => ({
    citation_num: 1,
    tool_id: 't1',
    source_system: 'cmms-ind',
    captured_at: '',
    verification: 'unverified',
    ...extra,
  });

  it('the Reasoning fold summary names the catalog system', () => {
    replay([record({ entity: { table: 'work_order_log' } })]);
    expect(container.querySelector('.reasoning-fold-text')?.textContent).toBe(
      'Queried Riverside Plant Maintenance',
    );
  });

  it('a Sources chip with a table reads "[1] <catalog name> · <table>"', () => {
    replay([record({ entity: { table: 'work_order_log' } })]);
    const chip = container.querySelector('a.source-chip[data-cite-num="1"]');
    expect(chip?.textContent).toBe('[1] Riverside Plant Maintenance · work_order_log');
  });

  it('a Sources chip without enrichment reads "[1] <catalog name>"', () => {
    replay([record()]);
    const chip = container.querySelector('a.source-chip[data-cite-num="1"]');
    expect(chip?.textContent).toBe('[1] Riverside Plant Maintenance');
  });
});

describe('Threads rule cards name systems; field paths keep the id', () => {
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

  it('identity binding: pair line + Systems row read names, the claim keeps the id path', () => {
    act(() =>
      root.render(
        <IdentityBindingCard
          binding={{
            concept: 'work_order',
            a: { system: 'mes-ind', field: 'work_order_ref' },
            b: { system: 'erp', field: 'work_order_number' },
            bidirectional: false,
            rules: [{ id: 'r1', source_system: 'mes-ind', target_system: 'erp', method: 'value_exact_match' }],
            maxConfidence: 0.9,
          }}
        />,
      ),
    );
    expect(container.querySelector('.rule-syspair')?.textContent).toBe('Riverside MES → ERP');
    expect(container.querySelector('.identity-rule-phrase')?.textContent).toBe(
      'mes-ind.work_order_ref → erp.work_order_number',
    );
    const systemsRow = [...container.querySelectorAll('.identity-detail-row')].find(
      (r) => r.querySelector('dt')?.textContent === 'Systems',
    );
    expect(systemsRow?.querySelector('dd')?.textContent).toBe('Riverside MES → ERP');
  });

  it('join and value-mapping cards name both systems', () => {
    act(() =>
      root.render(
        <>
          <JoinCard j={{ from_system: 'mes-ind', from_field: 'equipment_id', to_system: 'cmms-ind', to_field: 'asset_num' }} />
          <ValueCard v={{ from_system: 'cmms-ind', from_value: '1001-CMX-441', to_system: 'mes-ind', to_value: 'L3-M1' }} />
        </>,
      ),
    );
    const pairs = [...container.querySelectorAll('.rule-syspair')].map((n) => n.textContent);
    expect(pairs).toEqual([
      'Riverside MES → Riverside Plant Maintenance',
      'Riverside Plant Maintenance → Riverside MES',
    ]);
  });

  it('authority card names the system in its deactivate dialog', () => {
    act(() =>
      root.render(
        <AuthorityRuleCard
          a={{ id: 'ar1', concept: 'asset', system_id: 'cmms-ind', rule: 'CMMS owns assets' }}
          onLifecycle={() => {}}
        />,
      ),
    );
    act(() => (container.querySelector('.identity-lifecycle-btn') as HTMLButtonElement).click());
    expect(document.body.textContent).toContain('stop preferring Riverside Plant Maintenance as the source');
  });

  it('ranked resolution paths name both end systems', () => {
    act(() =>
      root.render(
        <RankedPaths
          fromSys="mes-ind"
          toSys="cmms-ind"
          d={{ total_paths: 1, direct_paths: [{ concept: 'asset', confidence: 0.9, source_pattern: '*', target_pattern: '*' }] }}
        />,
      ),
    );
    const hops = [...container.querySelectorAll('.path-hop')].map((n) => n.textContent);
    expect(hops).toEqual(['Riverside MES', 'Riverside Plant Maintenance']);
  });

  it('a pair endpoint outside the onboarded list still resolves to its catalog name', () => {
    expect(nameById([], 'cmms-ind')).toBe('Riverside Plant Maintenance');
    expect(pairGaps([{ id: 'mes-ind' }, { id: 'cmms-ind' }], {})).toEqual([
      ['Riverside MES', 'Riverside Plant Maintenance'],
    ]);
  });
});

const PROBE_TABLES = [{ name: 'work_orders', columns: [{ name: 'state' }] }];

describe('onboarding cards name the system', () => {
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
  });

  it('pre-map probe header', async () => {
    // PreMapProbe posts through requestJson (mocked for this file).
    vi.mocked(requestJson).mockResolvedValueOnce({
      system_id: 'cmms-ind',
      questions: [{ id: 'retry-semantics', prompt: 'When work is retried, what happens?', evidence: 'e' }],
    });
    await act(async () => {
      root.render(<PreMapProbe systemId="cmms-ind" tables={PROBE_TABLES} onContinue={() => {}} />);
    });
    await act(async () => {});
    expect(container.textContent).toContain('Before mapping Riverside Plant Maintenance');
  });

  it('context interview card title and save button', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        if (url.startsWith('/api/onboard/guided/state')) {
          return response({ first_launch: false, dismissed: false, answered_topics: [], draft_business_context: 'x' });
        }
        return response({ system_id: 'cmms-ind', results: [], derived: [], questions: [], draft_business_context: 'x' });
      }),
    );
    await act(async () => {
      root.render(<ContextInterviewCard systemId="cmms-ind" firstLaunch={false} />);
    });
    await act(async () => {});
    expect(container.querySelector('h4')?.textContent).toBe('✦ Business context — Riverside Plant Maintenance');
    const save = [...container.querySelectorAll('button')].find((b) => /save context/i.test(b.textContent ?? ''));
    expect(save?.textContent).toBe('Save context for Riverside Plant Maintenance');
  });
});
