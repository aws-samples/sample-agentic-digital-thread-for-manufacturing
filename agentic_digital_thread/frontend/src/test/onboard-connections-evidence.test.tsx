// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
/* The Provisioned card's connections: each join path opens to its evidence,
 * the column-name guess and older join paths are labelled honestly, the chat
 * flow's provision checkpoint shows the same summary as the wizard, and "See
 * all connections" opens the system's detail page, which lists them. Neutral
 * fixtures; the network is a stub. */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../views/onboard/onboard-queries', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../views/onboard/onboard-queries')>()),
  invalidateOnboardingRemap: vi.fn(async () => {}),
  useConceptsQuery: () => ({ data: [], isError: false, refetch: vi.fn() }),
}));

import type { ProvisionStatusResponse, SystemConnectionsResponse } from '../api/types/onboard';
import { ObFlow } from '../views/onboard/components/ObFlow';
import { ConnectionsDiscovered } from '../views/onboard/components/ResolveExtras';
import { SystemDetail } from '../views/onboard/components/SystemDetail';
import {
  appendChatCheckpoint,
  appendMsg,
  flowStore,
  openDetail,
  resetToWelcome,
  restoreSaved,
  startWizard,
  type DetailState,
} from '../views/onboard/flow-store';
import { provisionSummaryMd } from '../views/onboard/provision-summary';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;

function json(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  });
}

const RESULT: ProvisionStatusResponse = {
  status: 'done',
  system_id: 'sys-a',
  tool_name: 'query_sys_a',
  neptune_nodes_created: 9,
  identity_rules_created: 0,
  identity_rules_rejected: 0,
  mapped_fields: 4,
  join_paths: [
    {
      from_system: 'sys-a',
      from_table: 'schema_a.table_x',
      from_field: 'key_b_id',
      to_system: 'sys-a',
      to_table: 'schema_a.table_b',
      to_field: 'key_b_id',
      method: 'foreign_key_match',
      basis: 'declared_foreign_key',
      constraint_name: 'fk_table_x_key_b',
    },
    {
      from_system: 'sys-a',
      from_table: 'schema_a.table_x',
      from_field: 'item_id',
      to_system: 'sys-a',
      to_table: 'schema_a.items',
      to_field: 'item_id',
      method: 'foreign_key_match',
      basis: 'column_name_pattern',
    },
    {
      from_system: 'sys-b',
      from_table: 'schema_b.table_z',
      from_field: 'ref_z',
      to_system: 'sys-a',
      to_table: 'schema_a.table_x',
      to_field: 'ref_a',
      method: 'foreign_key_match',
    },
  ],
};

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
  vi.unstubAllGlobals();
});

function evidenceCells(): HTMLTableCellElement[] {
  return [...container.querySelectorAll<HTMLTableCellElement>('tbody td:nth-child(4)')];
}

describe('Connections Discovered evidence', () => {
  it('opens each join path to what proved it', () => {
    act(() => root.render(<ConnectionsDiscovered result={RESULT} />));
    const cells = evidenceCells();
    expect(cells).toHaveLength(3);
    const declared = cells[0].querySelector('details')!;
    expect(declared.querySelector('summary')!.textContent).toBe('declared foreign key');
    expect(declared.textContent).toContain('Declared foreign key fk_table_x_key_b.');
    expect(container.querySelector('thead')!.textContent).toContain('Evidence');
  });

  it('gives the naming guess its own label', () => {
    act(() => root.render(<ConnectionsDiscovered result={RESULT} />));
    const guess = evidenceCells()[1].querySelector('details')!;
    expect(guess.querySelector('summary')!.textContent).toBe('column name match');
    expect(guess.textContent).toContain('The data was not checked.');
  });

  it('labels an older join path without evidence neutrally, never as declared', () => {
    act(() => root.render(<ConnectionsDiscovered result={RESULT} />));
    const old = evidenceCells()[2].querySelector('details')!;
    expect(old.querySelector('summary')!.textContent).toBe('key match');
    expect(old.textContent).not.toMatch(/declared/i);
    expect(old.textContent).toContain('Recorded before the evidence for each connection was kept.');
  });
});

describe('See all connections', () => {
  it('opens the system detail page, which lists its connections', async () => {
    const connections: SystemConnectionsResponse = {
      system_id: 'sys-a',
      join_paths: RESULT.join_paths,
      join_paths_total: 3,
      value_mappings: [{ from_system: 'sys-a', from_value: 'A1', to_system: 'sys-b', to_value: 'B1' }],
      value_mappings_total: 450,
      identity_rules: [],
      identity_rules_total: 0,
    };
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === '/api/onboard/connections/sys-a') return json(connections);
      if (url === '/api/onboard/discover') return json({ tables: [] });
      if (url.startsWith('/api/onboard/mappings/')) return json({ mappings: [] });
      return json({});
    });
    vi.stubGlobal('fetch', fetchMock);
    act(() => root.render(<ConnectionsDiscovered result={RESULT} systemId="sys-a" />));
    const button = [...container.querySelectorAll('button')].find(
      (b) => b.textContent === 'See all connections',
    )!;
    expect(button).toBeTruthy();
    await act(async () => {
      button.click();
    });
    const s = flowStore.get();
    expect(s.screen).toBe('detail');
    expect(s.detail?.sys.id).toBe('sys-a');

    await act(async () => root.render(<SystemDetail detail={s.detail!} />));
    await act(async () => {});
    expect(fetchMock.mock.calls.map(([u]) => String(u))).toContain('/api/onboard/connections/sys-a');
    const section = container.querySelector('section[aria-labelledby="ob-system-connections-title"]')!;
    expect(section.textContent).toContain('Connections');
    expect(section.querySelector('[data-testid="connections-counts"]')!.textContent).toBe(
      '3 join paths, 0 identity rules, first 1 of 450 value mappings',
    );
    expect(section.querySelectorAll('details.ob-join-evidence')).toHaveLength(3);
  });

  it('shows the button when nothing was written, so the detail page is still one click away', () => {
    act(() =>
      root.render(
        <ConnectionsDiscovered result={{ ...RESULT, join_paths: [] }} systemId="sys-a" />,
      ),
    );
    expect(container.textContent).toBe('See all connections');
  });

  it('shows the server error when the connections read fails', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => json({ system_id: 'sys-a', error: 'The knowledge graph query failed.' })),
    );
    const detail: DetailState = {
      phase: 'ready',
      sys: { id: 'sys-a', name: 'System A' },
      tables: [],
      mappings: [],
    };
    await act(async () => root.render(<SystemDetail detail={detail} />));
    await act(async () => {});
    expect(container.querySelector('[role="alert"]')!.textContent).toBe(
      'The knowledge graph query failed.',
    );
  });
});

describe('Back from the detail page keeps what happened meanwhile', () => {
  it('a step finished while the detail was open is still there', () => {
    appendMsg('first');
    openDetail({ id: 'sys-a', name: 'System A' });
    appendMsg('verification passed'); // the run keeps going behind the detail page
    expect(restoreSaved()).toBe(true);
    const mds = flowStore
      .get()
      .entries.flatMap((e) => (e.kind === 'msg' ? [e.md] : []));
    expect(mds).toEqual(['first', 'verification passed']);
    expect(flowStore.get().screen).toBe('flow');
  });
});

describe('chat flow provision checkpoint', () => {
  it('shows the same summary as the wizard, with the Resolve reason and the button', () => {
    appendChatCheckpoint('resolve', { rules: [], rejected_rules: [{}, {}] });
    appendChatCheckpoint('provision', { ...RESULT });
    act(() => root.render(<ObFlow />));
    const text = container.textContent || '';
    expect(text).toContain('4 fields mapped to concepts');
    expect(text).toContain('3 join paths: 1 declared foreign key, 1 matched by column name only, 1 key match');
    expect(text).toContain('The Resolve step held back 2 candidates that did not meet the evidence bar');
    expect(text).not.toMatch(/graph nodes/);
    const buttons = [...container.querySelectorAll('button')].map((b) => b.textContent);
    expect(buttons).toContain('See all connections');
    // The wizard renders the same markdown: the two flows cannot drift.
    const wizardMd = provisionSummaryMd(RESULT, { withheldRules: 2, proposedRules: 0 });
    expect(wizardMd).toContain('The Resolve step held back 2 candidates');
  });
});
