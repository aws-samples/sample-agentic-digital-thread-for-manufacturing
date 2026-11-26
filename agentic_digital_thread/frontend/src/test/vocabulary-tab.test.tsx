// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tests — Vocabulary admin tab (W3.1, ADR-0050): concept list rendering across
 * provenance shapes + usage text, retire gating (client disable when mapped,
 * graceful server 409 when not), add-modal contract body + evidence_score
 * surfacing, bulk-import counters, and 400 error paths — all against a mocked
 * fetch, no network. */
// @vitest-environment jsdom
import { QueryClientProvider } from '@tanstack/react-query';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { createQueryClient } from '../api/query-client';
import type { VocabularyConcept } from '../api/types/vocabulary';
import VocabularyTab from '../pages/admin/tabs/VocabularyTab';
import { answerConfirm, confirmDialog, confirmText, installConfirmHost } from './confirm-host';
import { parseSynonyms } from '../pages/admin/tabs/vocabulary/ConceptFormModal';
import { usageText } from '../pages/admin/tabs/vocabulary/ConceptTable';
import { validateImportPack } from '../pages/admin/tabs/vocabulary/ImportPanel';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

installConfirmHost();

const CONCEPTS: VocabularyConcept[] = [
  {
    id: 'work_order',
    name: 'Work Order',
    description: 'A unit of production work',
    synonyms: ['job', 'production order'],
    layer: 'core',
    source: 'core',
    provenance_label: 'core',
    mapped_field_count: 3,
    usage_30d: 12,
    last_used_at: '2026-07-20T10:00:00Z',
    retired: false,
  },
  {
    id: 'pack_concept',
    name: 'Pack Concept',
    description: 'From an installed industry pack',
    synonyms: [],
    layer: 'pack',
    source: 'pack:sample_pack',
    provenance_label: 'pack:sample_pack',
    mapped_field_count: 0,
    usage_30d: 0,
    last_used_at: null,
    retired: false,
  },
  {
    id: 'imported_concept',
    name: 'Imported Concept',
    description: 'From a custom bulk import',
    synonyms: [],
    layer: 'pack',
    source: 'pack:custom:my_pack',
    provenance_label: 'pack:custom:my_pack',
    mapped_field_count: 0,
    usage_30d: 1,
    last_used_at: '2026-07-19T08:00:00Z',
    retired: false,
  },
  {
    id: 'minted_concept',
    name: 'Minted Concept',
    description: 'Minted by self-learning',
    synonyms: [],
    layer: 'minted',
    source: 'minted',
    provenance_label: 'minted',
    mapped_field_count: 0,
    usage_30d: 0,
    last_used_at: null,
    retired: true,
  },
];

const LIST_RESPONSE = { concepts: CONCEPTS, total: CONCEPTS.length };

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
    if (result) return result;
    // The RetirementPanel (C8) is always mounted inside the tab and its query
    // sits on every vocabulary mutation's invalidation path — answer it here
    // so per-test handlers stay focused on the surface under test.
    if (url === '/api/admin/vocabulary/retirement-candidates' && (!init?.method || init.method === 'GET')) {
      return response({ candidates: [] });
    }
    throw new Error(`unexpected URL: ${url} (${init?.method || 'GET'})`);
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

async function renderTab() {
  const client = createQueryClient();
  await act(async () => {
    root.render(
      <QueryClientProvider client={client}>
        <VocabularyTab />
      </QueryClientProvider>,
    );
    await Promise.resolve();
  });
  await vi.waitFor(() => expect(container.querySelector('#vocab-concept-table')).toBeTruthy());
  return client;
}

async function click(el: Element | null) {
  expect(el).not.toBeNull();
  await act(async () => {
    (el as HTMLElement).dispatchEvent(new MouseEvent('click', { bubbles: true }));
    await Promise.resolve();
  });
}

const setInputValue = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!;
const setTextareaValue = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')!.set!;

async function type(el: HTMLInputElement | HTMLTextAreaElement, value: string) {
  await act(async () => {
    (el instanceof HTMLTextAreaElement ? setTextareaValue : setInputValue).call(el, value);
    el.dispatchEvent(new Event('input', { bubbles: true }));
    await Promise.resolve();
  });
}

function row(conceptId: string): HTMLTableRowElement {
  const tr = container.querySelector<HTMLTableRowElement>(`tr[data-concept-id="${conceptId}"]`);
  expect(tr).not.toBeNull();
  return tr!;
}

describe('concept list rendering', () => {
  it('renders every provenance shape with chip, muted id, mapped count, and usage text', async () => {
    stubFetch((url, init) => {
      if (url === '/api/admin/vocabulary/concepts' && (!init?.method || init.method === 'GET')) {
        return response(LIST_RESPONSE);
      }
      return undefined;
    });
    await renderTab();

    // All four provenance shapes render as chips.
    const chips = Array.from(container.querySelectorAll('.vocab-provenance-chip')).map(
      (c) => c.textContent,
    );
    expect(chips).toEqual(['core', 'pack:sample_pack', 'pack:custom:my_pack', 'minted']);

    // Name + muted id + synonyms in the concept cell.
    const workOrder = row('work_order');
    expect(workOrder.textContent).toContain('Work Order');
    expect(workOrder.textContent).toContain('work_order');
    expect(workOrder.textContent).toContain('aka job, production order');

    // Usage text: "used in K answers (30d)" when used, em dash when not.
    expect(workOrder.textContent).toContain('used in 12 answers (30d)');
    expect(row('pack_concept').textContent).toContain('—');

    // Retired badge only on the retired concept.
    expect(row('minted_concept').querySelector('.vocab-retired-badge')!.textContent).toBe('Retired');
    expect(workOrder.querySelector('.vocab-retired-badge')).toBeNull();

    // Total from the response envelope.
    expect(container.textContent).toContain('4 concepts');
  });

  it('usageText covers singular, plural, and unused shapes', () => {
    expect(usageText({ id: 'a', name: 'A', usage_30d: 1, last_used_at: null })).toBe(
      'used in 1 answer (30d)',
    );
    expect(usageText({ id: 'a', name: 'A', usage_30d: 2, last_used_at: '2026-07-20T10:00:00Z' })).toContain(
      'used in 2 answers (30d) · last',
    );
    expect(usageText({ id: 'a', name: 'A', usage_30d: 0 })).toBe('—');
  });

  it('search and layer filter narrow the visible rows', async () => {
    stubFetch((url) => (url === '/api/admin/vocabulary/concepts' ? response(LIST_RESPONSE) : undefined));
    await renderTab();

    await type(container.querySelector<HTMLInputElement>('#vocab-search')!, 'minted');
    expect(container.querySelectorAll('tr[data-concept-id]')).toHaveLength(1);
    expect(row('minted_concept')).toBeTruthy();

    await type(container.querySelector<HTMLInputElement>('#vocab-search')!, '');
    const filter = container.querySelector<HTMLSelectElement>('#vocab-layer-filter')!;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value')!.set!.call(filter, 'core');
      filter.dispatchEvent(new Event('change', { bubbles: true }));
      await Promise.resolve();
    });
    expect(container.querySelectorAll('tr[data-concept-id]')).toHaveLength(1);
    expect(row('work_order')).toBeTruthy();
  });
});

describe('retire gating', () => {
  it('disables retire with an explanatory tooltip when mapped_field_count > 0', async () => {
    stubFetch((url) => (url === '/api/admin/vocabulary/concepts' ? response(LIST_RESPONSE) : undefined));
    await renderTab();

    const blocked = row('work_order').querySelector<HTMLButtonElement>('.vocab-retire-btn')!;
    expect(blocked.disabled).toBe(true);
    expect(blocked.title).toContain('mapped to 3 fields');

    const open = row('pack_concept').querySelector<HTMLButtonElement>('.vocab-retire-btn')!;
    expect(open.disabled).toBe(false);

    // Retired concepts show no retire button at all.
    expect(row('minted_concept').querySelector('.vocab-retire-btn')).toBeNull();
  });

  it('handles a server 409 gracefully (message rendered, no throw)', async () => {
    stubFetch((url, init) => {
      if (url === '/api/admin/vocabulary/concepts' && (!init?.method || init.method === 'GET')) {
        return response(LIST_RESPONSE);
      }
      if (url === '/api/admin/vocabulary/concepts/pack_concept/retire' && init?.method === 'POST') {
        return response({ error: 'Cannot retire: concept is mapped to 2 fields' }, 409);
      }
      return undefined;
    });
    await renderTab();

    await click(row('pack_concept').querySelector('.vocab-retire-btn'));
    expect(confirmText().buttons).toEqual(['Cancel', 'Retire concept']);
    expect(confirmDialog()!.querySelector('.confirm-danger-btn')).not.toBeNull();
    await answerConfirm('confirm');
    await vi.waitFor(() =>
      expect(container.querySelector('#vocab-retire-status')!.textContent).toContain(
        'Cannot retire: concept is mapped to 2 fields',
      ),
    );
  });
});

describe('add-concept modal', () => {
  it('posts the contract-shaped body and surfaces the returned evidence_score', async () => {
    const calls = stubFetch((url, init) => {
      if (url === '/api/admin/vocabulary/concepts' && (!init?.method || init.method === 'GET')) {
        return response(LIST_RESPONSE);
      }
      if (url === '/api/admin/vocabulary/concepts' && init?.method === 'POST') {
        return response({ status: 'accepted', mapping_id: 'm-1', evidence_score: 0.82 });
      }
      return undefined;
    });
    await renderTab();

    await click(container.querySelector('#vocab-add-btn'));
    await type(document.querySelector<HTMLInputElement>('#vocab-form-name')!, 'Cycle Time');
    await type(
      document.querySelector<HTMLTextAreaElement>('#vocab-form-description')!,
      'Elapsed production time per unit',
    );
    await type(document.querySelector<HTMLInputElement>('#vocab-form-synonyms')!, 'takt, throughput time');
    await type(document.querySelector<HTMLInputElement>('#vocab-form-evidence-system')!, 'mes');
    await type(document.querySelector<HTMLInputElement>('#vocab-form-evidence-field')!, 'cycle_seconds');
    await click(document.querySelector('#vocab-form-override'));
    await click(document.querySelector('.vocab-concept-modal button[type="submit"]'));

    const post = calls.find((c) => c.init?.method === 'POST');
    expect(post).toBeTruthy();
    expect(JSON.parse(String(post!.init!.body))).toEqual({
      name: 'Cycle Time',
      description: 'Elapsed production time per unit',
      synonyms: ['takt', 'throughput time'],
      evidence_system: 'mes',
      evidence_field: 'cycle_seconds',
      override_evidence: true,
    });

    await vi.waitFor(() => {
      const score = document.querySelector('.vocab-evidence-score');
      expect(score).not.toBeNull();
      expect(score!.textContent).toContain('0.82');
    });
    expect(document.querySelector('.vocab-assert-result')!.textContent).toContain('Accepted');
  });

  it('renders a 400 error message without throwing', async () => {
    stubFetch((url, init) => {
      if (url === '/api/admin/vocabulary/concepts' && (!init?.method || init.method === 'GET')) {
        return response(LIST_RESPONSE);
      }
      if (url === '/api/admin/vocabulary/concepts' && init?.method === 'POST') {
        return response({ error: "'name' is required" }, 400);
      }
      return undefined;
    });
    await renderTab();

    await click(container.querySelector('#vocab-add-btn'));
    await type(document.querySelector<HTMLInputElement>('#vocab-form-name')!, 'X');
    await type(document.querySelector<HTMLTextAreaElement>('#vocab-form-description')!, 'Y');
    await click(document.querySelector('.vocab-concept-modal button[type="submit"]'));

    await vi.waitFor(() =>
      expect(document.querySelector('.vocab-form-error')!.textContent).toContain("'name' is required"),
    );
  });

  it('edit mode PATCHes exactly name/description/synonyms with real values — never the id, never fabricated fields', async () => {
    const calls = stubFetch((url, init) => {
      if (url === '/api/admin/vocabulary/concepts' && (!init?.method || init.method === 'GET')) {
        return response(LIST_RESPONSE);
      }
      if (url === '/api/admin/vocabulary/concepts/work_order' && init?.method === 'PATCH') {
        return response({ status: 'accepted', mapping_id: 'm-2', evidence_score: null });
      }
      return undefined;
    });
    await renderTab();

    await click(row('work_order').querySelector('.vocab-edit-btn'));
    await type(document.querySelector<HTMLInputElement>('#vocab-form-name')!, 'Work Order v2');
    await click(document.querySelector('.vocab-concept-modal button[type="submit"]'));

    const patch = calls.find((c) => c.init?.method === 'PATCH');
    expect(patch).toBeTruthy();
    expect(patch!.url).toBe('/api/admin/vocabulary/concepts/work_order');
    const body = JSON.parse(String(patch!.init!.body));
    // Pin the EXACT key set: the id travels in the URL only (a body carrying
    // `id` is a contract 400), and no fabricated fields ride along.
    expect(Object.keys(body).sort()).toEqual(['description', 'name', 'synonyms']);
    expect(body).not.toHaveProperty('id');
    // Edited field carries the edit; untouched fields carry the concept's
    // REAL current values (prefilled) — never placeholders or defaults.
    expect(body.name).toBe('Work Order v2');
    expect(body.description).toBe('A unit of production work');
    expect(body.synonyms).toEqual(['job', 'production order']);
  });

  it('parseSynonyms trims and drops blanks', () => {
    expect(parseSynonyms(' a, b ,, c ')).toEqual(['a', 'b', 'c']);
    expect(parseSynonyms('')).toEqual([]);
  });
});

describe('bulk import panel', () => {
  const VALID_PACK = JSON.stringify({
    metadata: { name: 'My pack' },
    concepts: [
      { id: 'alpha_concept', name: 'Alpha', description: 'a' },
      { id: 'beta_concept', name: 'Beta', description: 'b' },
      { id: 'work_order', name: 'Work Order', description: 'dup' },
    ],
  });

  it('renders the three counters and skipped_ids from a mocked 200', async () => {
    stubFetch((url, init) => {
      if (url === '/api/admin/vocabulary/concepts' && (!init?.method || init.method === 'GET')) {
        return response(LIST_RESPONSE);
      }
      if (url === '/api/admin/vocabulary/import' && init?.method === 'POST') {
        return response({
          status: 'ok',
          slug: 'my_pack',
          inserted: 2,
          skipped: 1,
          failed: 0,
          skipped_ids: ['work_order'],
        });
      }
      return undefined;
    });
    await renderTab();

    await type(container.querySelector<HTMLTextAreaElement>('#vocab-import-text')!, VALID_PACK);
    await click(container.querySelector('#vocab-import-btn'));

    await vi.waitFor(() => expect(container.querySelector('#vocab-import-result')).toBeTruthy());
    expect(container.querySelector('.vocab-import-inserted')!.textContent).toContain('2');
    expect(container.querySelector('.vocab-import-skipped')!.textContent).toContain('1');
    expect(container.querySelector('.vocab-import-failed')!.textContent).toContain('0');
    expect(container.querySelector('.vocab-import-skipped-ids')!.textContent).toContain('work_order');
  });

  it('client-side validation blocks bad shapes before any request', async () => {
    const calls = stubFetch((url, init) => {
      if (url === '/api/admin/vocabulary/concepts' && (!init?.method || init.method === 'GET')) {
        return response(LIST_RESPONSE);
      }
      return undefined;
    });
    await renderTab();

    await type(container.querySelector<HTMLTextAreaElement>('#vocab-import-text')!, '{"concepts": []}');
    await click(container.querySelector('#vocab-import-btn'));

    expect(container.querySelector('.vocab-import-error')!.textContent).toContain('metadata.name');
    expect(calls.some((c) => c.url === '/api/admin/vocabulary/import')).toBe(false);
  });

  it('renders a server 400 message without throwing', async () => {
    stubFetch((url, init) => {
      if (url === '/api/admin/vocabulary/concepts' && (!init?.method || init.method === 'GET')) {
        return response(LIST_RESPONSE);
      }
      if (url === '/api/admin/vocabulary/import' && init?.method === 'POST') {
        return response({ error: "'metadata.name' yields an empty slug" }, 400);
      }
      return undefined;
    });
    await renderTab();

    await type(container.querySelector<HTMLTextAreaElement>('#vocab-import-text')!, VALID_PACK);
    await click(container.querySelector('#vocab-import-btn'));

    await vi.waitFor(() =>
      expect(container.querySelector('.vocab-import-error')!.textContent).toContain('empty slug'),
    );
  });

  it('validateImportPack covers the contract shapes', () => {
    expect(validateImportPack('nope').error).toBe('Not valid JSON.');
    expect(validateImportPack('[]').error).toContain('metadata');
    expect(validateImportPack('{"metadata":{"name":"p"},"concepts":[]}').error).toContain('non-empty');
    expect(validateImportPack('{"metadata":{"name":"p"},"concepts":[{"id":"","name":"x"}]}').error).toContain(
      'needs both',
    );
    expect(
      validateImportPack('{"metadata":{"name":"p"},"concepts":[{"id":"a","name":"A"}]}').pack,
    ).toBeTruthy();
  });
});
