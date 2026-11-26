// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tests — import preview dry-run (C7), apply-path collision report (C6), and
 * the retirement-candidates panel (C8): per-row preview actions + counters,
 * collision rows visually distinct from exact skips, retire through the
 * existing mutation with 409 detail rendering and cache invalidation, and the
 * empty state — all against a mocked fetch, no network. Generic concept ids
 * only. */
// @vitest-environment jsdom
import { QueryClientProvider } from '@tanstack/react-query';
import { act, type ReactElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { createQueryClient } from '../api/query-client';
import VocabularyTab from '../pages/admin/tabs/VocabularyTab';
import ImportPanel from '../pages/admin/tabs/vocabulary/ImportPanel';
import RetirementPanel from '../pages/admin/tabs/vocabulary/RetirementPanel';
import { answerConfirm, confirmText, installConfirmHost } from './confirm-host';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

installConfirmHost();

const VALID_PACK = JSON.stringify({
  metadata: { name: 'My pack' },
  concepts: [
    { id: 'alpha_concept', name: 'Alpha', description: 'a' },
    { id: 'work_order', name: 'Work Order', description: 'dup' },
    { id: 'flow_rate', name: 'Flow Rate', description: 'collides' },
  ],
});

const PREVIEW_RESPONSE = {
  status: 'preview',
  slug: 'my_pack',
  rows: [
    { id: 'alpha_concept', name: 'Alpha', action: 'insert', collides_with: null },
    { id: 'work_order', name: 'Work Order', action: 'skip_exact', collides_with: null },
    { id: 'flow_rate', name: 'Flow Rate', action: 'skip_collision', collides_with: 'flow-rate' },
  ],
  insert_count: 1,
  skip_count: 1,
  collision_count: 1,
};

const CANDIDATES = {
  candidates: [
    {
      id: 'unused_alpha_concept',
      name: 'Unused Alpha',
      source: 'pack:custom:my_pack',
      mapped_field_count: 0,
      usage_30d: 0,
      last_used_at: null,
      reasons: ['no mapped fields', 'no usage in 30 days'],
    },
    {
      id: 'unused_beta_concept',
      name: 'Unused Beta',
      source: 'minted',
      mapped_field_count: 0,
      usage_30d: 0,
      last_used_at: null,
      reasons: ['no mapped fields', 'no usage in 30 days'],
    },
  ],
};

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

type FetchCall = { url: string; init?: RequestInit };

function stubFetch(handler: (url: string, init?: RequestInit) => Response | undefined): FetchCall[] {
  const calls: FetchCall[] = [];
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url, init });
    const result = handler(url, init);
    if (!result) throw new Error(`unexpected URL: ${url} (${init?.method || 'GET'})`);
    return result;
  }));
  return calls;
}

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
  vi.restoreAllMocks();
});

async function render(element: ReactElement) {
  const client = createQueryClient();
  await act(async () => {
    root.render(<QueryClientProvider client={client}>{element}</QueryClientProvider>);
    await Promise.resolve();
  });
  return client;
}

async function click(el: Element | null) {
  expect(el).not.toBeNull();
  await act(async () => {
    (el as HTMLElement).dispatchEvent(new MouseEvent('click', { bubbles: true }));
    await Promise.resolve();
  });
}

const setTextareaValue = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')!.set!;

async function typeTextarea(el: HTMLTextAreaElement, value: string) {
  await act(async () => {
    setTextareaValue.call(el, value);
    el.dispatchEvent(new Event('input', { bubbles: true }));
    await Promise.resolve();
  });
}

describe('import preview (C7)', () => {
  it('POSTs /import/preview and renders per-row actions + the three counters — no apply call', async () => {
    const calls = stubFetch((url, init) => {
      if (url === '/api/admin/vocabulary/import/preview' && init?.method === 'POST') {
        return response(PREVIEW_RESPONSE);
      }
      return undefined;
    });
    await render(<ImportPanel />);

    await typeTextarea(container.querySelector<HTMLTextAreaElement>('#vocab-import-text')!, VALID_PACK);
    await click(container.querySelector('#vocab-import-preview-btn'));
    await vi.waitFor(() => expect(container.querySelector('#vocab-import-preview')).toBeTruthy());

    // The preview body is the same validated pack the apply path would send.
    const post = calls.find((c) => c.url === '/api/admin/vocabulary/import/preview');
    expect(post).toBeTruthy();
    expect(JSON.parse(String(post!.init!.body))).toEqual(JSON.parse(VALID_PACK));
    // Dry-run only — the apply route was never called.
    expect(calls.some((c) => c.url === '/api/admin/vocabulary/import')).toBe(false);

    // Counters.
    expect(container.querySelector('.vocab-preview-insert-count')!.textContent).toContain('1');
    expect(container.querySelector('.vocab-preview-skip-count')!.textContent).toContain('1');
    expect(container.querySelector('.vocab-preview-collision-count')!.textContent).toContain('1');

    // Per-row actions in request-body order, tagged with the basis action.
    const rows = Array.from(container.querySelectorAll('tr.vocab-preview-row'));
    expect(rows.map((r) => r.getAttribute('data-action'))).toEqual([
      'insert',
      'skip_exact',
      'skip_collision',
    ]);
    expect(rows[0].textContent).toContain('Insert');
    expect(rows[1].textContent).toContain('already present');
    expect(rows[2].textContent).toContain('id collision');
    expect(rows[2].textContent).toContain('flow-rate');
  });

  it('renders collision rows visually distinct from exact skips (different class + label)', async () => {
    stubFetch((url, init) =>
      url === '/api/admin/vocabulary/import/preview' && init?.method === 'POST'
        ? response(PREVIEW_RESPONSE)
        : undefined,
    );
    await render(<ImportPanel />);

    await typeTextarea(container.querySelector<HTMLTextAreaElement>('#vocab-import-text')!, VALID_PACK);
    await click(container.querySelector('#vocab-import-preview-btn'));
    await vi.waitFor(() => expect(container.querySelector('#vocab-import-preview')).toBeTruthy());

    const exact = container.querySelector('tr.vocab-preview-skip_exact')!;
    const collision = container.querySelector('tr.vocab-preview-skip_collision')!;
    expect(exact).toBeTruthy();
    expect(collision).toBeTruthy();
    expect(collision.className).not.toBe(exact.className);
    // The exact skip shows no collision target; the collision row does.
    expect(exact.textContent).toContain('—');
    expect(collision.textContent).toContain('flow-rate');
  });

  it('client-side validation blocks a bad pack before any preview request', async () => {
    const calls = stubFetch(() => undefined);
    await render(<ImportPanel />);

    await typeTextarea(container.querySelector<HTMLTextAreaElement>('#vocab-import-text')!, '{"concepts": []}');
    await click(container.querySelector('#vocab-import-preview-btn'));

    expect(container.querySelector('.vocab-import-error')!.textContent).toContain('metadata.name');
    expect(calls).toHaveLength(0);
  });
});

describe('import apply collision report (C6)', () => {
  it('renders the collisions array separately from exact-id skips', async () => {
    stubFetch((url, init) => {
      if (url === '/api/admin/vocabulary/import' && init?.method === 'POST') {
        return response({
          status: 'ok',
          slug: 'my_pack',
          inserted: 1,
          skipped: 2,
          failed: 0,
          skipped_ids: ['work_order'],
          collisions: [
            { id: 'flow_rate', existing_id: 'flow-rate', reason: 'normalized-id collision' },
          ],
        });
      }
      return undefined;
    });
    await render(<ImportPanel />);

    await typeTextarea(container.querySelector<HTMLTextAreaElement>('#vocab-import-text')!, VALID_PACK);
    await click(container.querySelector('#vocab-import-btn'));
    await vi.waitFor(() => expect(container.querySelector('#vocab-import-result')).toBeTruthy());

    const collisions = container.querySelector('.vocab-import-collisions')!;
    expect(collisions.textContent).toContain('flow_rate');
    expect(collisions.textContent).toContain('collides with flow-rate');
    // Exact-id skips keep their own block — the two reports stay separate.
    const skipped = container.querySelector('.vocab-import-skipped-ids')!;
    expect(skipped.textContent).toContain('work_order');
    expect(skipped.textContent).not.toContain('flow_rate');
  });

  it('omits the collision block when the server does not send one (old servers)', async () => {
    stubFetch((url, init) =>
      url === '/api/admin/vocabulary/import' && init?.method === 'POST'
        ? response({ status: 'ok', slug: 'my_pack', inserted: 3, skipped: 0, failed: 0, skipped_ids: [] })
        : undefined,
    );
    await render(<ImportPanel />);

    await typeTextarea(container.querySelector<HTMLTextAreaElement>('#vocab-import-text')!, VALID_PACK);
    await click(container.querySelector('#vocab-import-btn'));
    await vi.waitFor(() => expect(container.querySelector('#vocab-import-result')).toBeTruthy());

    expect(container.querySelector('.vocab-import-collisions')).toBeNull();
  });
});

describe('retirement candidates panel (C8)', () => {
  it('renders candidates with their reasons', async () => {
    stubFetch((url, init) =>
      url === '/api/admin/vocabulary/retirement-candidates' && (!init?.method || init.method === 'GET')
        ? response(CANDIDATES)
        : undefined,
    );
    await render(<RetirementPanel />);
    await vi.waitFor(() => expect(container.querySelector('#vocab-retirement-table')).toBeTruthy());

    const alpha = container.querySelector('tr[data-candidate-id="unused_alpha_concept"]')!;
    expect(alpha.textContent).toContain('Unused Alpha');
    expect(alpha.textContent).toContain('pack:custom:my_pack');
    const reasons = Array.from(alpha.querySelectorAll('.vocab-candidate-reasons li')).map(
      (li) => li.textContent,
    );
    expect(reasons).toEqual(['no mapped fields', 'no usage in 30 days']);
    expect(container.querySelectorAll('tr[data-candidate-id]')).toHaveLength(2);
  });

  it('retire fires the existing mutation, shows success, and refetches the sweep', async () => {
    let sweepCalls = 0;
    stubFetch((url, init) => {
      if (url === '/api/admin/vocabulary/retirement-candidates' && (!init?.method || init.method === 'GET')) {
        sweepCalls += 1;
        return response(sweepCalls > 1 ? { candidates: [] } : CANDIDATES);
      }
      if (url === '/api/admin/vocabulary/concepts/unused_alpha_concept/retire' && init?.method === 'POST') {
        return response({ status: 'retired' });
      }
      return undefined;
    });
    await render(<RetirementPanel />);
    await vi.waitFor(() => expect(container.querySelector('#vocab-retirement-table')).toBeTruthy());

    await click(
      container
        .querySelector('tr[data-candidate-id="unused_alpha_concept"]')!
        .querySelector('.vocab-candidate-retire-btn'),
    );
    expect(confirmText()).toEqual({
      title: 'Retire "Unused Alpha"?',
      body: 'The concept stays in the graph, marked retired.',
      buttons: ['Cancel', 'Retire concept'],
    });
    await answerConfirm('confirm');

    await vi.waitFor(() =>
      expect(container.querySelector('#vocab-retirement-status')!.textContent).toContain(
        '✓ Retired Unused Alpha',
      ),
    );
    // The retire mutation invalidates the sweep — the panel refetched and now
    // shows the empty state.
    await vi.waitFor(() => expect(sweepCalls).toBeGreaterThan(1));
    await vi.waitFor(() => expect(container.querySelector('.vocab-retirement-empty')).toBeTruthy());
  });

  it('renders a 409 error detail without throwing (fail-safe at retire time)', async () => {
    stubFetch((url, init) => {
      if (url === '/api/admin/vocabulary/retirement-candidates' && (!init?.method || init.method === 'GET')) {
        return response(CANDIDATES);
      }
      if (url === '/api/admin/vocabulary/concepts/unused_beta_concept/retire' && init?.method === 'POST') {
        return response({ error: 'Cannot retire: concept is mapped to 1 field' }, 409);
      }
      return undefined;
    });
    await render(<RetirementPanel />);
    await vi.waitFor(() => expect(container.querySelector('#vocab-retirement-table')).toBeTruthy());

    await click(
      container
        .querySelector('tr[data-candidate-id="unused_beta_concept"]')!
        .querySelector('.vocab-candidate-retire-btn'),
    );
    await answerConfirm('confirm');

    await vi.waitFor(() =>
      expect(container.querySelector('#vocab-retirement-status')!.textContent).toContain(
        'Cannot retire: concept is mapped to 1 field',
      ),
    );
  });

  it('shows the empty state when the sweep returns no candidates', async () => {
    stubFetch((url) =>
      url === '/api/admin/vocabulary/retirement-candidates' ? response({ candidates: [] }) : undefined,
    );
    await render(<RetirementPanel />);

    await vi.waitFor(() => expect(container.querySelector('.vocab-retirement-empty')).toBeTruthy());
    expect(container.querySelector('.vocab-retirement-empty')!.textContent).toContain(
      'No retirement candidates',
    );
    expect(container.querySelector('#vocab-retirement-table')).toBeNull();
  });
});

describe('VocabularyTab composition', () => {
  it('mounts the retirement panel below the artifacts panel', async () => {
    stubFetch((url, init) => {
      if (url === '/api/admin/vocabulary/concepts' && (!init?.method || init.method === 'GET')) {
        return response({ concepts: [], total: 0 });
      }
      if (url === '/api/admin/vocabulary/retirement-candidates') return response({ candidates: [] });
      if (url === '/api/proposed-mappings') return response({ mappings: [] });
      if (url === '/api/query-guidance') return response({ systems: [] });
      return undefined;
    });
    await render(<VocabularyTab />);
    await vi.waitFor(() => expect(container.querySelector('#vocab-retirement-panel')).toBeTruthy());

    const artifacts = container.querySelector('#vocab-artifacts-panel')!;
    const retirement = container.querySelector('#vocab-retirement-panel')!;
    expect(
      artifacts.compareDocumentPosition(retirement) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });
});
