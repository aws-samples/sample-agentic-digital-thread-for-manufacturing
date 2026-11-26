// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
/* A system whose removal did not finish offers only "Remove again".
 *
 * A removal that fails part-way leaves a half-removed system: its runtime is
 * gone and part of its graph may be. The server reports it as status
 * `removal_failed` (also after a restart), and the UI's removal job reports
 * `error`. Either way the card must not open the system detail, must not
 * offer Test, Edit or Onboard, and offers exactly one action: remove it
 * again. A normal onboarded card is unchanged. */
import { QueryClientProvider } from '@tanstack/react-query';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { createQueryClient } from '../api/query-client';
import type { CatalogSystem } from '../api/types/onboard';
import { CatalogList } from '../views/onboard/components/CatalogList';
import { ObModals } from '../views/onboard/components/ObModals';
import { removingStore } from '../views/onboard/flow';
import { ob } from '../views/onboard/state';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;
let queryClient: ReturnType<typeof createQueryClient>;

const SYSTEM: CatalogSystem = {
  id: 'quality-lab',
  name: 'Quality Lab',
  system_type: 'LIMS',
  plant: 'Plant A',
  connection_type: 'PostgreSQL',
  status: 'onboarded',
};

function response(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as Response;
}

async function mount(systems: CatalogSystem[]) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => response({ systems })),
  );
  await act(async () => {
    root.render(
      <QueryClientProvider client={queryClient}>
        <CatalogList />
      </QueryClientProvider>,
    );
  });
  await vi.waitFor(() => expect(document.getElementById('card-quality-lab')).not.toBeNull());
}

function card(): HTMLElement {
  return document.getElementById('card-quality-lab') as HTMLElement;
}

function buttons(): string[] {
  return Array.from(card().querySelectorAll('button'))
    .map((b) => (b.textContent || '').trim())
    .filter((t) => t && !t.startsWith('connection'));
}

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  queryClient = createQueryClient();
  queryClient.setDefaultOptions({ queries: { retry: false } });
  ob.adminChecked = true;
  ob.isAdmin = true;
  removingStore.set({ ids: {}, errors: {} });
});

afterEach(() => {
  act(() => root.unmount());
  queryClient.clear();
  container.remove();
  removingStore.set({ ids: {}, errors: {} });
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('a system whose removal did not finish', () => {
  it('after a restart (status removal_failed) offers only Remove again', async () => {
    await mount([{ ...SYSTEM, status: 'removal_failed' }]);
    await vi.waitFor(() => expect(buttons()).toEqual(['Remove again']));
    expect(card().getAttribute('role')).toBeNull();
    expect(card().tabIndex).toBe(-1);
    expect(card().textContent).toContain('Removal incomplete');
    expect(card().querySelector('#edit-form-quality-lab')).toBeNull();
    const again = Array.from(card().querySelectorAll('button')).find(
      (b) => b.textContent?.trim() === 'Remove again',
    )!;
    expect(again.disabled).toBe(false);
  });

  it('after its removal job failed in this session offers only Remove again', async () => {
    await mount([SYSTEM]);
    await vi.waitFor(() => expect(buttons()).toContain('Remove system'));
    act(() => {
      removingStore.set({
        ids: { 'quality-lab': 'error' },
        errors: { 'quality-lab': 'the knowledge graph did not delete IdentityRule nodes' },
      });
    });
    await vi.waitFor(() => expect(buttons()).toEqual(['Remove again']));
    expect(card().getAttribute('role')).toBeNull();
    expect(card().textContent).toContain('did not delete IdentityRule nodes');
  });

  it('opens the removal dialog from Remove again', async () => {
    await mount([{ ...SYSTEM, status: 'removal_failed' }]);
    const { flowStore } = await import('../views/onboard/flow-store');
    await vi.waitFor(() => expect(buttons()).toEqual(['Remove again']));
    act(() => {
      Array.from(card().querySelectorAll('button'))
        .find((b) => b.textContent?.trim() === 'Remove again')!
        .click();
    });
    // It re-reads the catalog first, then opens the dialog.
    await vi.waitFor(() =>
      expect(flowStore.get().modal).toMatchObject({
        kind: 'disconnect',
        systemId: 'quality-lab',
        systemName: 'Quality Lab',
        retry: true,
      }),
    );
  });

  it('guard: a normal onboarded card keeps detail, Test, Edit and Remove system', async () => {
    await mount([SYSTEM]);
    await vi.waitFor(() => expect(buttons()).toEqual(['Test', 'Edit', 'Remove system']));
    expect(card().getAttribute('role')).toBe('button');
    expect(card().textContent).not.toContain('Removal incomplete');
  });

  it(
    "guard: a demo system's fresh Remove system keeps the registration by default",
    async () => {
      // The workshop's recovery (module 4): Remove system on an onboarded
      // demo card, "Also forget the connection registration" left unchecked.
      const posts: string[] = [];
      vi.stubGlobal(
        'fetch',
        vi.fn(async (url: string, init?: RequestInit) => {
          if (typeof url === 'string' && url.includes('/reset/') && init?.method === 'POST') {
            posts.push(url);
            return response({ status: 'started', system_id: 'quality-lab' });
          }
          if (typeof url === 'string' && url.includes('/catalog')) {
            return response({ systems: [{ ...SYSTEM, connection_origin: 'demo' }] });
          }
          return response({ state: 'running' });
        }),
      );
      await act(async () => {
        root.render(
          <QueryClientProvider client={queryClient}>
            <CatalogList />
            <ObModals workshopMode="off" />
          </QueryClientProvider>,
        );
      });
      await vi.waitFor(() => expect(buttons()).toEqual(['Test', 'Edit', 'Remove system']));
      act(() => {
        Array.from(card().querySelectorAll('button'))
          .find((b) => b.textContent?.trim() === 'Remove system')!
          .click();
      });
      await vi.waitFor(() => expect(document.querySelector('#ob-disc-forget')).not.toBeNull());
      const forget = document.querySelector('#ob-disc-forget') as HTMLInputElement;
      expect(forget.checked).toBe(false);
      expect(forget.disabled).toBe(false);
      expect(document.querySelector('#ob-disc-mode-note')).toBeNull();
      const input = document.querySelector('#ob-disc-confirm-input') as HTMLInputElement;
      act(() => {
        const setter = Object.getOwnPropertyDescriptor(
          window.HTMLInputElement.prototype,
          'value',
        )!.set!;
        setter.call(input, 'quality-lab');
        input.dispatchEvent(new Event('input', { bubbles: true }));
      });
      await act(async () => {
        (document.querySelector('#ob-disc-yes') as HTMLButtonElement).click();
      });
      await vi.waitFor(() => expect(posts.length).toBe(1));
      expect(posts[0]).toContain('/api/onboard/reset/quality-lab?wait=false');
      expect(posts[0]).not.toContain('full=true');
    },
  );
});
