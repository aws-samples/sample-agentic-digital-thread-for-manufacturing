// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
/* A steward's Deliver click must say it worked. On a real server the accept
 * removes the note from the pending queue, so the refetch the accept mutation
 * awaits unmounts the proposal card before it can show DELIVERED. The
 * confirmation therefore has to outlive the card: it goes to the shared
 * toast queue, naming the system. */
import { QueryClientProvider } from '@tanstack/react-query';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { createQueryClient } from '../api/query-client';
import { AuthProvider } from '../contexts/AuthContext';
import { setLang } from '../lib/i18n';
import GuidanceEditor from '../pages/admin/tabs/vocabulary/GuidanceEditor';
import { ToastRegion } from '../shared/ui/ToastRegion';
import { clear as clearToasts } from '../shared/ui/toast-store';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

type Call = { url: string; init?: RequestInit };
let container: HTMLDivElement;
let root: Root;
let calls: Call[];
let storage: Map<string, string>;

function response(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status >= 200 && status < 300 ? 'OK' : 'Failed',
    text: async () => JSON.stringify(body),
    json: async () => body,
  } as unknown as Response;
}

const system = {
  system_id: 'mes',
  base: 'Use canonical identifiers.',
  notes: [],
  learned_count: 0,
  length: 320,
  max_len: 4000,
};
const proposal = {
  mapping_id: 'guidance-1',
  proposed_type: 'guidance_note',
  status: 'pending',
  from_system: 'mes',
  to_value: 'REQUIRED: the answer must use canonical identifiers.',
  guidance_scope: 'answer_requirement',
};
const appendPreview = {
  outcome: 'append',
  current_digest: 'sha256:current',
  current_length: 320,
  note_digest: 'sha256:note',
  note_length: 54,
  scope: 'answer_requirement',
  result_digest: 'sha256:result',
  result_length: 376,
  evicted_note_digests: [],
};
const reconciliation = {
  status: 'ok',
  counts: { accepted: 1, graph: 1, delivered: 1 },
  sources: { accepted: 'ok', graph: 'ok', delivery: 'ok' },
  mismatches: [],
  mismatch_count: 0,
  truncated: false,
};

/** A server whose pending queue drops the note once the accept succeeds,
 * which is what the real `/api/proposed-mappings` does. `dropsNote` is false
 * for replies that leave the note pending. */
function stubServer(acceptBody: unknown, acceptStatus = 200, dropsNote = acceptStatus === 200) {
  let accepted = false;
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url, init });
    const method = init?.method || 'GET';
    if (url === '/api/auth/me') return response({ user_id: 'steward', email: 's@example.com', is_admin: true });
    if (url === '/api/query-guidance' && method === 'GET') return response({ systems: [system] });
    if (url === '/api/proposed-mappings?proposed_type=guidance_note' && method === 'GET') {
      return response({ mappings: accepted ? [] : [proposal] });
    }
    if (url === '/api/query-guidance/reconciliation' && method === 'GET') return response(reconciliation);
    if (url === '/api/systems' && method === 'GET') return response({ systems: [{ id: 'mes', name: 'Plant MES' }] });
    if (url.endsWith('/guidance-preview')) return response({ status: 'preview', mapping_id: 'guidance-1', preview: appendPreview });
    if (url.endsWith('/accept') && method === 'POST') {
      if (dropsNote) accepted = true;
      return response(acceptBody, acceptStatus);
    }
    throw new Error(`unexpected fetch ${method} ${url}`);
  }));
}

beforeEach(() => {
  storage = new Map<string, string>();
  vi.stubGlobal('localStorage', {
    getItem: (k: string) => storage.get(k) ?? null,
    setItem: (k: string, v: string) => void storage.set(k, String(v)),
    removeItem: (k: string) => void storage.delete(k),
  });
  setLang('en');
  clearToasts();
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  calls = [];
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  clearToasts();
  setLang('en');
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

async function renderQueue() {
  await act(async () => {
    root.render(
      <QueryClientProvider client={createQueryClient()}>
        <AuthProvider>
          <GuidanceEditor />
          <ToastRegion />
        </AuthProvider>
      </QueryClientProvider>,
    );
    await Promise.resolve();
  });
  await vi.waitFor(() => expect(container.querySelector('.guidance-proposal')).toBeTruthy());
  // The card names the system by display name once /api/systems resolves.
  await vi.waitFor(() => expect(container.querySelector('.guidance-proposal strong')?.textContent).toBe('Plant MES (mes)'));
}

async function click(element: Element | null | undefined) {
  expect(element).toBeTruthy();
  await act(async () => {
    (element as HTMLElement).click();
    await Promise.resolve();
  });
}

function cardButton(name: string) {
  return [...container.querySelectorAll<HTMLButtonElement>('.guidance-proposal button')]
    .find((button) => button.textContent?.trim() === name);
}

function toastTexts() {
  return [...container.querySelectorAll('.toast .toast-message')].map((node) => node.textContent);
}

async function previewThenDeliver() {
  await click(cardButton('Preview delivery'));
  await vi.waitFor(() => expect(cardButton('Deliver')).toBeTruthy());
  await click(cardButton('Deliver'));
}

describe('guidance delivery confirmation outlives the card', () => {
  it('confirms a delivery in a toast after the refetched queue drops the card', async () => {
    stubServer({ status: 'accepted', outcome: 'append', proposed_type: 'guidance_note', version: 7 });
    await renderQueue();
    await previewThenDeliver();

    // The real flow: the delivered note leaves the queue and its card unmounts.
    await vi.waitFor(() => expect(container.querySelector('.guidance-proposal')).toBeNull());
    expect(container.textContent).toContain('no guidance proposals are pending delivery');

    await vi.waitFor(() => expect(toastTexts()).toEqual([
      'Delivered to Plant MES (mes). The assistant reads this note from your next question.',
    ]));
    const toast = container.querySelector('.toast')!;
    expect(toast.getAttribute('data-toast-kind')).toBe('success');
    expect(toast.closest('[role="status"]')).toBeTruthy();
  });

  it('follows the language picker', async () => {
    setLang('de');
    stubServer({ status: 'accepted', outcome: 'append', proposed_type: 'guidance_note', version: 7 });
    await renderQueue();
    await previewThenDeliver();
    await vi.waitFor(() => expect(toastTexts()).toEqual([
      'An Plant MES (mes) übermittelt. Der Assistent liest diesen Hinweis ab Ihrer nächsten Frage.',
    ]));
  });

  it('raises no success toast when delivery is not proven', async () => {
    stubServer({ status: 'accepted', outcome: 'append', error: 'raw backend text' }, 200, false);
    await renderQueue();
    await previewThenDeliver();
    await vi.waitFor(() => expect(container.querySelector('.guidance-proposal-line')?.textContent).toContain('nothing was accepted'));
    expect(container.querySelector('.toast')).toBeNull();
  });

  it('raises no success toast for a WOULD EVICT reply; the card stays and asks for confirmation', async () => {
    stubServer({
      status: 'pending',
      outcome: 'would_evict',
      error: 'stale_preview',
      preview: { ...appendPreview, outcome: 'would_evict', evicted_note_digests: ['sha256:oldest'] },
    }, 409);
    await renderQueue();
    await previewThenDeliver();
    await vi.waitFor(() => expect(document.querySelector('[role="dialog"]')).toBeTruthy());
    expect(container.querySelector('.guidance-proposal')).toBeTruthy();
    expect(container.querySelector('.toast')).toBeNull();
  });
});

describe('a Deliver that could not set the note back to pending says so', () => {
  const stopped = { status: 'pending', outcome: 'append', error: 'guidance_update_failed' };

  it('warns that the note may show as accepted without being delivered', async () => {
    stubServer({ ...stopped, drift: { graph: false, delivery: false, proposal: true } }, 503, false);
    await renderQueue();
    await previewThenDeliver();
    const line = () => container.querySelector('.guidance-proposal-line')?.textContent || '';
    await vi.waitFor(() => expect(line()).toContain('could not be set back to pending'));
    expect(line()).toContain('may show as accepted without being delivered');
    expect(line()).not.toContain('nothing was accepted');
    expect(container.querySelector('.guidance-proposal-line')?.getAttribute('role')).toBe('alert');
    expect(container.querySelector('.toast')).toBeNull();
  });

  it('keeps the plain retry line when the status was put back', async () => {
    stubServer({ ...stopped, drift: { graph: false, delivery: false, proposal: false } }, 503, false);
    await renderQueue();
    await previewThenDeliver();
    await vi.waitFor(() => expect(container.querySelector('.guidance-proposal-line')?.textContent)
      .toBe('Delivery could not be checked right now, so nothing was accepted. Try again in a moment.'));
  });
});
