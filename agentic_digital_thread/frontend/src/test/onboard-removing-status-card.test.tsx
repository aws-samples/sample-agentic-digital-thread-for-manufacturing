// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
/* A system whose removal is RUNNING is not available.
 *
 * The card knew a removal in flight only from this tab's removingStore, so a
 * removal started from another tab or from the onboarding chat left the card
 * as the server's catalog status said: onboarded (clickable, a Remove that
 * 409s) and, once the cascade's catalog step ran, Registered with Onboard. On
 * test event 31 a system was removed and another onboarded eighteen seconds
 * later, while the removed system's graph cleanup still ran. The server now
 * reports `removing` for the whole cascade; this tab renders it as the same
 * disabled Removing… card it renders for its own removal job, and keeps it
 * with the connected systems rather than offering Onboard. */
import { QueryClientProvider } from '@tanstack/react-query';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { createQueryClient } from '../api/query-client';
import type { CatalogSystem } from '../api/types/onboard';
import { CatalogList } from '../views/onboard/components/CatalogList';
import { removingStore } from '../views/onboard/flow';
import { ob } from '../views/onboard/state';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;
let queryClient: ReturnType<typeof createQueryClient>;

const REMOVING: CatalogSystem = {
  id: 'mes-site',
  name: 'Site MES',
  system_type: 'MES',
  plant: 'Plant 1',
  connection_type: 'REST API',
  status: 'removing',
};

const REGISTERED: CatalogSystem = {
  id: 'plant-cmms',
  name: 'Plant CMMS',
  system_type: 'CMMS',
  plant: 'Plant 1',
  connection_type: 'postgres',
  status: 'registered',
};

function response(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: 'OK',
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as Response;
}

async function mount() {
  await act(async () => {
    root.render(
      <QueryClientProvider client={queryClient}>
        <CatalogList />
      </QueryClientProvider>,
    );
  });
}

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  queryClient = createQueryClient();
  queryClient.setDefaultOptions({ queries: { retry: false } });
  ob.adminChecked = true;
  ob.isAdmin = true;
  // This tab never saw the removal start.
  removingStore.set({ ids: {}, errors: {}, modes: {} });
});

afterEach(() => {
  act(() => root.unmount());
  queryClient.clear();
  container.remove();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('a system the server reports as removing', () => {
  it('renders the disabled Removing… card with no Onboard or Remove action', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response({ systems: [REMOVING, REGISTERED] })));
    await mount();

    await vi.waitFor(() => {
      expect(document.getElementById('card-mes-site')).not.toBeNull();
    });
    const card = document.getElementById('card-mes-site') as HTMLElement;
    const buttons = Array.from(card.querySelectorAll('button')).map((b) => ({
      text: b.textContent?.trim() ?? '',
      disabled: b.disabled,
    }));
    const removing = buttons.find((b) => b.text === 'Removing…');
    expect(removing, JSON.stringify(buttons)).toBeDefined();
    expect(removing?.disabled).toBe(true);
    expect(buttons.some((b) => b.text === 'Onboard')).toBe(false);
    expect(buttons.some((b) => b.text === 'Remove system')).toBe(false);
    expect(buttons.some((b) => b.text === 'Remove again')).toBe(false);
    // Not an onboarded card either: it opens no detail.
    expect(card.getAttribute('role')).toBeNull();
    expect(card.classList.contains('onboarded')).toBe(false);
  });

  it('stays with the connected systems while the registered one still offers Onboard', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response({ systems: [REMOVING, REGISTERED] })));
    await mount();

    await vi.waitFor(() => {
      expect(document.getElementById('card-plant-cmms')).not.toBeNull();
    });
    const other = document.getElementById('card-plant-cmms') as HTMLElement;
    expect(Array.from(other.querySelectorAll('button')).some((b) => b.textContent?.trim() === 'Onboard')).toBe(
      true,
    );
    // The removing card is rendered before the registered one: the connected
    // group comes first in the list.
    const ids = Array.from(container.querySelectorAll('.sys-card')).map((c) => c.id);
    expect(ids.indexOf('card-mes-site')).toBeLessThan(ids.indexOf('card-plant-cmms'));
  });
});
