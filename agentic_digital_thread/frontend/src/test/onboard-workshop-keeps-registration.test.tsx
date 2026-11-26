// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
/* On a workshop account a seeded system's removal keeps its registration.
 *
 * An attendee who removed a seeded system with "Also forget the connection
 * registration" checked could not bring it back: they cannot register it
 * again. So for a system the user did not connect (catalog
 * connection_origin other than 'user'), the Remove system dialog offers no
 * "also forget" checkbox while the account runs a workshop story
 * (workshopMode 'on') or that is still unknown, and the removal keeps the
 * registration: the system goes back to the catalog ready to onboard again
 * (module 4's recovery). A system the user connected (origin 'user', e.g.
 * a REST CMMS) can be connected again, so its dialog and its Remove
 * registration stay as before. Outside workshop mode ('off') nothing
 * changes. The dialog is opened from the card, so the origin is the card's. */
import { QueryClientProvider } from '@tanstack/react-query';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { createQueryClient } from '../api/query-client';
import type { CatalogSystem } from '../api/types/onboard';
import type { WorkshopMode } from '../features/workshop/clock';
import { CatalogList } from '../views/onboard/components/CatalogList';
import { ObModals } from '../views/onboard/components/ObModals';
import { removingStore } from '../views/onboard/flow';
import { closeModal } from '../views/onboard/flow-store';
import { ob } from '../views/onboard/state';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;
let queryClient: ReturnType<typeof createQueryClient>;

const SEEDED: CatalogSystem = {
  id: 'cmms-ind',
  name: 'Plant Maintenance',
  system_type: 'CMMS',
  plant: 'Plant A',
  connection_type: 'PostgreSQL',
  status: 'onboarded',
  connection_origin: 'workshop',
};

const USER_ONBOARDED: CatalogSystem = {
  id: 'rest-cmms',
  name: 'REST CMMS',
  system_type: 'CMMS',
  plant: 'Plant A',
  connection_type: 'REST',
  status: 'onboarded',
  connection_origin: 'user',
};

const USER_REG: CatalogSystem = { ...USER_ONBOARDED, status: 'registered' };

function response(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as Response;
}

/** Mount the catalog + dialogs in ``mode``; returns the reset POST URLs seen. */
async function mount(mode: WorkshopMode, system: CatalogSystem): Promise<string[]> {
  const posts: string[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      if (typeof url === 'string' && url.includes('/reset/') && init?.method === 'POST') {
        posts.push(url);
        return response({ status: 'started', system_id: system.id });
      }
      if (typeof url === 'string' && url.includes('/catalog')) return response({ systems: [system] });
      return response({ state: 'running' });
    }),
  );
  await act(async () => {
    root.render(
      <QueryClientProvider client={queryClient}>
        <CatalogList />
        <ObModals workshopMode={mode} />
      </QueryClientProvider>,
    );
  });
  await vi.waitFor(() => expect(document.getElementById(`card-${system.id}`)).not.toBeNull());
  return posts;
}

function cardButtons(id: string): HTMLButtonElement[] {
  return Array.from(document.getElementById(`card-${id}`)!.querySelectorAll('button'));
}

/** Remove system on the card, as an attendee does. */
async function openRemoveSystem(id: string): Promise<void> {
  await vi.waitFor(() =>
    expect(cardButtons(id).some((b) => b.textContent?.trim() === 'Remove system')).toBe(true),
  );
  act(() => cardButtons(id).find((b) => b.textContent?.trim() === 'Remove system')!.click());
  await vi.waitFor(() => expect(document.querySelector('#ob-disc-confirm-input')).not.toBeNull());
}

async function confirm(id: string, posts: string[]): Promise<string> {
  const input = document.querySelector('#ob-disc-confirm-input') as HTMLInputElement;
  act(() => {
    Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')!.set!.call(input, id);
    input.dispatchEvent(new Event('input', { bubbles: true }));
  });
  await act(async () => {
    (document.querySelector('#ob-disc-yes') as HTMLButtonElement).click();
  });
  await vi.waitFor(() => expect(posts.length).toBe(1));
  return posts[0];
}

const forgetBox = () => document.querySelector('#ob-disc-forget') as HTMLInputElement | null;
const dialogText = () => document.querySelector('.ob-modal')?.textContent ?? '';

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
  act(() => closeModal());
  act(() => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('Remove system of a seeded system', () => {
  it('on a workshop account: no "also forget" checkbox, and the removal keeps the registration', async () => {
    const posts = await mount('on', SEEDED);
    await openRemoveSystem('cmms-ind');
    expect(forgetBox()).toBeNull();
    expect(dialogText()).not.toContain('Also forget the connection registration');
    const url = await confirm('cmms-ind', posts);
    expect(url).toContain('/api/onboard/reset/cmms-ind');
    expect(url).not.toContain('full=true');
  });

  it('on a workshop account: the dialog says plainly that the registration is kept', async () => {
    // Before: "By default the connection registration is kept", though there
    // is no other choice to make.
    await mount('on', SEEDED);
    await openRemoveSystem('cmms-ind');
    expect(dialogText()).not.toContain('By default');
    expect(dialogText()).toContain('The connection registration is kept, so the system returns');
  });

  it('while workshop mode is still unknown: no checkbox', async () => {
    await mount('unknown', SEEDED);
    await openRemoveSystem('cmms-ind');
    expect(forgetBox()).toBeNull();
    expect(dialogText()).not.toContain('By default');
  });

  it('outside workshop mode: unchanged, the checkbox shows and can forget', async () => {
    const posts = await mount('off', SEEDED);
    await openRemoveSystem('cmms-ind');
    expect(dialogText()).toContain('By default the connection registration is kept');
    const box = forgetBox()!;
    expect(box).not.toBeNull();
    expect(box.checked).toBe(false);
    act(() => box.click());
    expect(await confirm('cmms-ind', posts)).toContain('full=true');
  });
});

describe('workshop mode turning on while the dialog is open', () => {
  it('a box checked before the mode turned on is not sent', async () => {
    const posts = await mount('off', SEEDED);
    await openRemoveSystem('cmms-ind');
    act(() => forgetBox()!.click());
    expect(forgetBox()!.checked).toBe(true);
    await act(async () => {
      root.render(
        <QueryClientProvider client={queryClient}>
          <CatalogList />
          <ObModals workshopMode="on" />
        </QueryClientProvider>,
      );
    });
    expect(forgetBox()).toBeNull();
    const url = await confirm('cmms-ind', posts);
    expect(url).toContain('/api/onboard/reset/cmms-ind');
    expect(url).not.toContain('full=true');
  });
});

describe('a system the user connected, on a workshop account', () => {
  it('Remove system keeps the checkbox and can forget the registration', async () => {
    const posts = await mount('on', USER_ONBOARDED);
    await openRemoveSystem('rest-cmms');
    const box = forgetBox()!;
    expect(box).not.toBeNull();
    expect(box.checked).toBe(false);
    act(() => box.click());
    expect(await confirm('rest-cmms', posts)).toContain('full=true');
  });

  it('Remove system without the box still keeps the registration', async () => {
    const posts = await mount('on', USER_ONBOARDED);
    await openRemoveSystem('rest-cmms');
    expect(await confirm('rest-cmms', posts)).not.toContain('full=true');
  });

  it('a registered card still offers Remove registration', async () => {
    await mount('on', USER_REG);
    expect(cardButtons('rest-cmms').map((b) => b.textContent?.trim())).toContain('Remove registration');
  });
});
