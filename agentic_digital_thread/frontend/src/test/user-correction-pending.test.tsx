// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tests: the two other user-correction surfaces tell a person whose request
 * was filed for steward review that it is waiting, rather than claiming the
 * graph changed or reporting a failure. The server files a non-steward's
 * user-define or correct-mapping request as a PENDING proposal and answers
 * {status: 'pending'}; a steward's request still answers 'accepted'. */
// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { Lineage } from '../api/types/dashboard';
import { submitUserDefinedMapping } from '../views/architecture/proposals';
import { LineageNode } from '../views/dashboard/DetailModal';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

function response(body: unknown, status = 200): Response {
  const text = JSON.stringify(body);
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status >= 200 && status < 300 ? 'OK' : 'Failed',
    json: async () => body,
    text: async () => text,
  } as unknown as Response;
}

function stubFetch(handler: (url: string, init?: RequestInit) => Response) {
  const calls: { url: string; init?: RequestInit }[] = [];
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url, init });
    return handler(url, init);
  }));
  return calls;
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  document.body.innerHTML = '';
});

describe('Architecture view user-define form', () => {
  const UD_FIELDS: Record<string, string> = {
    'ud-from-system': 'mes',
    'ud-from-field': 'orders.status',
    'ud-from-value': 'WIP',
    'ud-to-system': 'erp',
    'ud-to-field': 'work_orders.state',
    'ud-to-value': 'in_progress',
    'ud-concept': 'order_status',
    'ud-description': '',
    'ud-proposed-type': 'value_mapping',
  };

  function mountForm() {
    for (const [id, value] of Object.entries(UD_FIELDS)) {
      const input = document.createElement('input');
      input.id = id;
      input.value = value;
      document.body.appendChild(input);
    }
    const msg = document.createElement('div');
    msg.id = 'ud-message';
    document.body.appendChild(msg);
    return msg;
  }

  function userDefineReturns(body: unknown) {
    return stubFetch((url, init) => {
      if (url === '/api/proposed-mappings/user-define' && init?.method === 'POST') return response(body);
      return response({ mappings: [], count: 0 });
    });
  }

  it("says a pending proposal is waiting for a steward and clears the form", async () => {
    const msg = mountForm();
    userDefineReturns({ status: 'pending', mapping_id: 'ud-9', proposed_type: 'value_mapping', version: 1 });

    await submitUserDefinedMapping();

    expect(msg.textContent).toContain('steward');
    expect(msg.textContent).not.toContain('failed');
    expect(msg.textContent).not.toContain('added to the knowledge graph');
    expect((document.getElementById('ud-from-system') as HTMLInputElement).value).toBe('');
  });

  it('keeps the accepted message for a steward', async () => {
    const msg = mountForm();
    userDefineReturns({ status: 'accepted', neptune_node_id: 'n', version: 1 });

    await submitUserDefinedMapping();

    expect(msg.textContent).toContain('added to the knowledge graph');
  });
});

describe('dashboard lineage "correct this mapping"', () => {
  const LINEAGE: Lineage = {
    kind: 'direct',
    metric_id: 'mro-on-hand',
    concept: 'mro-inventory',
    authority: { system_id: 'cmms' },
    mapping: { table: 'parts', column: 'qty' },
  } as Lineage;

  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
  });

  async function submitWith(body: unknown) {
    stubFetch(() => response(body));
    await act(async () => {
      root.render(<LineageNode lin={LINEAGE} opts={{ correctable: true }} />);
      await Promise.resolve();
    });
    await act(async () => {
      container.querySelector<HTMLButtonElement>('.lineage-correct-toggle')!.click();
      await Promise.resolve();
    });
    await act(async () => {
      container.querySelector<HTMLButtonElement>('.lineage-correct-submit')!.click();
      await Promise.resolve();
    });
  }

  it("tells a non-steward the correction is waiting for a steward", async () => {
    await submitWith({ status: 'pending', mapping_id: 'cm-1', concept: 'mro-inventory', affected_cards: [] });
    await vi.waitFor(() =>
      expect(container.querySelector('.lineage-correct-status')!.textContent).toContain('steward'),
    );
  });

  it('confirms a steward correction', async () => {
    await submitWith({ status: 'accepted', version: 2, concept: 'mro-inventory', affected_cards: ['oee'] });
    await vi.waitFor(() =>
      expect(container.querySelector('.lineage-correct-status')!.textContent).toContain('Mapping corrected'),
    );
  });

  it('shows the server error text', async () => {
    await submitWith({ error: "to_field 'parts.nope' does not exist in cmms schema" });
    await vi.waitFor(() =>
      expect(container.querySelector('.lineage-correct-status')!.textContent).toContain('does not exist'),
    );
  });
});
