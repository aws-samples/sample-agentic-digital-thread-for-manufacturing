// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
/* "Remove again" finishes a removal the way it was asked.
 *
 * A full removal can fail after its secret is already scheduled for
 * deletion. The card then shows "Remove again". Reopening it must stay a FULL
 * removal, never a default one that keeps a registration whose credentials
 * are pending deletion. The mode comes from this session's removal job
 * (removingStore) and from the server's recorded mode on the catalog entry
 * (removal_mode), which survives an app restart and a page reload. */
import { QueryClientProvider } from '@tanstack/react-query';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { createQueryClient } from '../api/query-client';
import type { CatalogSystem } from '../api/types/onboard';
import { CatalogList } from '../views/onboard/components/CatalogList';
import { ObModals } from '../views/onboard/components/ObModals';
import { obDisconnectConfirmed, removingStore } from '../views/onboard/flow';
import { flowStore } from '../views/onboard/flow-store';
import { ob } from '../views/onboard/state';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;
let queryClient: ReturnType<typeof createQueryClient>;

const USER_REG: CatalogSystem = {
  id: 'quality-lab',
  name: 'Quality Lab',
  system_type: 'LIMS',
  plant: 'Plant A',
  connection_type: 'PostgreSQL',
  status: 'registered',
  connection_origin: 'user',
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

function card(): HTMLElement {
  return document.getElementById('card-quality-lab') as HTMLElement;
}

/** Mount the catalog serving ``entry``; returns the reset POST URLs seen. */
async function mountWith(entry: CatalogSystem): Promise<string[]> {
  const posts: string[] = [];
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    if (typeof url === 'string' && url.includes('/catalog')) {
      return response({ systems: [entry] });
    }
    if (typeof url === 'string' && url.includes('/reset/') && init?.method === 'POST') {
      posts.push(url);
      return response({ status: 'started', system_id: 'quality-lab' });
    }
    return response({ state: 'running' });
  });
  vi.stubGlobal('fetch', fetchMock);
  await act(async () => {
    root.render(
      <QueryClientProvider client={queryClient}>
        <CatalogList />
        <ObModals />
      </QueryClientProvider>,
    );
  });
  await vi.waitFor(() => expect(document.getElementById('card-quality-lab')).not.toBeNull());
  return posts;
}

async function clickRemoveAgain(): Promise<void> {
  await vi.waitFor(() =>
    expect(
      Array.from(card().querySelectorAll('button')).some(
        (b) => b.textContent?.trim() === 'Remove again',
      ),
    ).toBe(true),
  );
  act(() => {
    Array.from(card().querySelectorAll('button'))
      .find((b) => b.textContent?.trim() === 'Remove again')!
      .click();
  });
  await vi.waitFor(() => expect(document.querySelector('#ob-disc-confirm-input')).not.toBeNull());
}

async function confirmRemoval(posts: string[]): Promise<string> {
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
  return posts[0];
}

function forgetBox(): HTMLInputElement {
  return document.querySelector('#ob-disc-forget') as HTMLInputElement;
}

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  queryClient = createQueryClient();
  queryClient.setDefaultOptions({ queries: { retry: false } });
  ob.adminChecked = true;
  ob.isAdmin = true;
  removingStore.set({ ids: {}, errors: {}, modes: {} });
});

afterEach(() => {
  act(() => root.unmount());
  queryClient.clear();
  container.remove();
  removingStore.set({ ids: {}, errors: {}, modes: {} });
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('a failed removal retried from the card', () => {
  it('obDisconnectConfirmed records the full/registration mode in removingStore', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => response({ status: 'started' })));
    await obDisconnectConfirmed('quality-lab', true, true);
    expect(removingStore.get().modes?.['quality-lab']).toEqual({
      full: true,
      registration: true,
    });
  });

  it('reopens a failed registration removal in registration mode and re-POSTs full=true', async () => {
    removingStore.set({
      ids: { 'quality-lab': 'error' },
      errors: { 'quality-lab': 'graph delete failed' },
      modes: { 'quality-lab': { full: true, registration: true } },
    });
    const posts = await mountWith({ ...USER_REG, status: 'removal_failed' });
    await clickRemoveAgain();
    expect(flowStore.get().modal).toMatchObject({
      kind: 'disconnect',
      systemId: 'quality-lab',
      registration: true,
    });
    expect(await confirmRemoval(posts)).toContain('full=true');
  });

  it("finishes this session's failed full removal of an onboarded system as full", async () => {
    removingStore.set({
      ids: { 'quality-lab': 'error' },
      errors: { 'quality-lab': 'graph delete failed' },
      modes: { 'quality-lab': { full: true, registration: false } },
    });
    const posts = await mountWith({ ...USER_REG, status: 'removal_failed' });
    await clickRemoveAgain();
    expect(forgetBox().checked).toBe(true);
    expect(forgetBox().disabled).toBe(true);
    expect(await confirmRemoval(posts)).toContain('full=true');
  });
});

describe('after a restart (no removal job in this session)', () => {
  it("uses the server's recorded full mode: fixed on, re-POSTs full=true", async () => {
    const posts = await mountWith({
      ...USER_REG,
      status: 'removal_failed',
      removal_mode: 'full',
    });
    await clickRemoveAgain();
    expect(flowStore.get().modal).toMatchObject({ kind: 'disconnect', retryFull: true });
    expect(forgetBox().checked).toBe(true);
    expect(forgetBox().disabled).toBe(true);
    expect(document.querySelector('#ob-disc-mode-note')?.textContent).toContain(
      'forgets it too',
    );
    expect(await confirmRemoval(posts)).toContain('full=true');
  });

  it("uses the server's recorded default mode: fixed off, re-POSTs without full", async () => {
    const posts = await mountWith({
      ...USER_REG,
      status: 'removal_failed',
      removal_mode: 'default',
    });
    await clickRemoveAgain();
    expect(forgetBox().checked).toBe(false);
    expect(forgetBox().disabled).toBe(true);
    expect(await confirmRemoval(posts)).not.toContain('full=true');
  });

  it('full wins when the job and the recorded mode disagree', async () => {
    removingStore.set({
      ids: { 'quality-lab': 'error' },
      errors: { 'quality-lab': 'failed' },
      modes: { 'quality-lab': { full: false, registration: false } },
    });
    const posts = await mountWith({
      ...USER_REG,
      status: 'removal_failed',
      removal_mode: 'full',
    });
    await clickRemoveAgain();
    expect(await confirmRemoval(posts)).toContain('full=true');
  });

  it('leaves the choice open when no mode was recorded', async () => {
    await mountWith({ ...USER_REG, status: 'removal_failed' });
    await clickRemoveAgain();
    expect(forgetBox().checked).toBe(false);
    expect(forgetBox().disabled).toBe(false);
    expect(document.querySelector('#ob-disc-mode-note')).toBeNull();
  });
});
