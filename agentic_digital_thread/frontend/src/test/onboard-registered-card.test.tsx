// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
/* A 'registered' USER system can be forgotten from its catalog card.
 *
 * A user system registered through Connect New System (and never onboarded),
 * or one returned to 'registered' by a default removal, had no card action to
 * forget it — only an Onboard button — so its registration and its own
 * adt/conn/<id> secret could never be removed from the UI. The card now also
 * offers "Remove registration" for a user registration (connection_origin
 * 'user'), opening the removal dialog in registration mode (a full removal).
 * A demo/workshop registration (origin !== 'user') offers no such action: its
 * shared secret must survive. The action is accessible (an aria-label naming
 * the system). */
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

async function mount(systems: CatalogSystem[]) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => response({ systems })),
  );
  await act(async () => {
    root.render(
      <QueryClientProvider client={queryClient}>
        <CatalogList />
        <ObModals />
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

describe("a 'registered' user system card", () => {
  it('offers Remove registration alongside Onboard (plus admin Test/Edit)', async () => {
    await mount([USER_REG]);
    await vi.waitFor(() =>
      expect(buttons()).toEqual(['Test', 'Edit', 'Onboard', 'Remove registration']),
    );
  });

  it('the Remove registration action is accessible (aria-label names the system)', async () => {
    await mount([USER_REG]);
    const btn = Array.from(card().querySelectorAll('button')).find(
      (b) => b.textContent?.trim() === 'Remove registration',
    )!;
    expect(btn.getAttribute('aria-label')).toBe('Remove registration for Quality Lab');
  });

  it('opens the removal dialog in registration mode (a full removal)', async () => {
    await mount([USER_REG]);
    const { flowStore } = await import('../views/onboard/flow-store');
    act(() => {
      Array.from(card().querySelectorAll('button'))
        .find((b) => b.textContent?.trim() === 'Remove registration')!
        .click();
    });
    expect(flowStore.get().modal).toMatchObject({
      kind: 'disconnect',
      systemId: 'quality-lab',
      systemName: 'Quality Lab',
      registration: true,
    });
    // The dialog forces a full removal: no "also forget" checkbox, and its
    // confirm is a "Remove registration" button.
    await vi.waitFor(() =>
      expect(document.querySelector('#ob-disc-yes')?.textContent).toContain(
        'Remove registration',
      ),
    );
    expect(document.querySelector('#ob-disc-forget')).toBeNull();
  });

  it('guard: a demo registration offers no Remove registration (its secret must survive)', async () => {
    await mount([{ ...USER_REG, id: 'quality-lab', connection_origin: 'demo' }]);
    await vi.waitFor(() => expect(buttons()).toEqual(['Test', 'Edit', 'Onboard']));
    expect(buttons()).not.toContain('Remove registration');
  });

  it('guard: HighByte offers no Remove registration (its shared secret must survive)', async () => {
    await mount([
      USER_REG,
      { ...USER_REG, id: 'highbyte', name: 'HighByte', connection_type: 'MCP Server' },
    ]);
    await vi.waitFor(() => expect(document.getElementById('card-highbyte')).not.toBeNull());
    const hb = Array.from(document.getElementById('card-highbyte')!.querySelectorAll('button'))
      .map((b) => (b.textContent || '').trim());
    expect(hb).toContain('Onboard');
    expect(hb).not.toContain('Remove registration');
  });

  it('registration removal POSTs a full removal to the reset route', async () => {
    const fetchMock = vi.fn(async (url: string) => {
      if (typeof url === 'string' && url.includes('/catalog')) {
        return response({ systems: [USER_REG] });
      }
      return response({ status: 'started', system_id: 'quality-lab' });
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

    act(() => {
      Array.from(card().querySelectorAll('button'))
        .find((b) => b.textContent?.trim() === 'Remove registration')!
        .click();
    });
    // Type the id to arm the confirm, then confirm.
    await vi.waitFor(() => expect(document.querySelector('#ob-disc-confirm-input')).not.toBeNull());
    const input = document.querySelector('#ob-disc-confirm-input') as HTMLInputElement;
    act(() => {
      const setter = Object.getOwnPropertyDescriptor(
        window.HTMLInputElement.prototype,
        'value',
      )!.set!;
      setter.call(input, 'quality-lab');
      input.dispatchEvent(new Event('input', { bubbles: true }));
    });
    await vi.waitFor(() =>
      expect((document.querySelector('#ob-disc-yes') as HTMLButtonElement).disabled).toBe(false),
    );
    await act(async () => {
      (document.querySelector('#ob-disc-yes') as HTMLButtonElement).click();
    });
    await vi.waitFor(() =>
      expect(
        fetchMock.mock.calls.some(
          ([u]) =>
            typeof u === 'string' &&
            u.includes('/api/onboard/reset/quality-lab') &&
            u.includes('full=true'),
        ),
      ).toBe(true),
    );
  });
});
