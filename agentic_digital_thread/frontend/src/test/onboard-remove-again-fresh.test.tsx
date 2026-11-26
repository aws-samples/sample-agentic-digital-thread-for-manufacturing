// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
/* "Remove again" acts on the system's current state, not this tab's copy.
 *
 * A tab that recorded a failed full removal keeps showing "Remove again",
 * fixed to full, until its next catalog read (up to 60 s; no refresh on
 * focus). If another tab or Reset All recovered the system meanwhile (it is
 * onboarded again, or back to Registered), a click opened the full-mode retry
 * and confirming fully removed the recovered system and scheduled its
 * credentials for deletion.
 *
 * "Remove again" now reads the catalog from the server before it opens the
 * dialog and again before it confirms (a new read, never one already in
 * flight); when the system is no longer half-removed it removes nothing, the
 * card shows the fresh state, the outcome is announced and focus moves to the
 * card's current action. Its POST says it is a retry (retry=true), so the
 * server refuses it if the system recovered after the last read. */
import { QueryClientProvider } from '@tanstack/react-query';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { createQueryClient } from '../api/query-client';
import type { CatalogSystem } from '../api/types/onboard';
import { CatalogList } from '../views/onboard/components/CatalogList';
import { ObModals } from '../views/onboard/components/ObModals';
import { removingStore } from '../views/onboard/flow';
import { flowStore } from '../views/onboard/flow-store';
import { removalNoticeStore, setRemovalNotice } from '../views/onboard/removal-store';
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
  status: 'removal_failed',
  connection_origin: 'user',
  removal_mode: 'full',
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

function refused(detail: string): Response {
  return {
    ok: false,
    status: 409,
    statusText: 'Conflict',
    json: async () => ({ detail }),
    text: async () => JSON.stringify({ detail }),
  } as Response;
}

/** The server: its current catalog entry for the system, the reset POSTs it
 * received, whether catalog reads fail, an optional catalog read held open
 * (answering with the entry as it was when the read arrived), and a
 * recovery that lands just before the next POST. */
const server = {
  entry: SYSTEM as CatalogSystem,
  posts: [] as string[],
  catalogReads: 0,
  failCatalog: false,
  holdNextCatalogRead: false,
  releaseHeld: null as null | (() => void),
  recoverBeforePost: false,
};

function fetchImpl(url: string, init?: RequestInit): Promise<Response> {
  if (typeof url === 'string' && url.includes('/reset/') && init?.method === 'POST') {
    if (server.recoverBeforePost) {
      server.entry = { ...SYSTEM, status: 'onboarded', removal_mode: undefined };
      server.recoverBeforePost = false;
    }
    server.posts.push(url);
    // The server's own precondition for a retry.
    if (url.includes('retry=true') && server.entry.status !== 'removal_failed') {
      return Promise.resolve(
        refused(
          `'quality-lab' is no longer half-removed (it is ${server.entry.status} now), so ` +
            'nothing was removed. Use its card in the Onboard view.',
        ),
      );
    }
    return Promise.resolve(response({ status: 'started', system_id: 'quality-lab' }));
  }
  if (typeof url === 'string' && url.includes('/catalog')) {
    server.catalogReads += 1;
    if (server.failCatalog) return Promise.reject(new TypeError('network down'));
    const snapshot = { systems: [server.entry] };
    if (server.holdNextCatalogRead) {
      server.holdNextCatalogRead = false;
      return new Promise((resolve) => {
        server.releaseHeld = () => resolve(response(snapshot));
      });
    }
    return Promise.resolve(response(snapshot));
  }
  return Promise.resolve(response({ state: 'running' }));
}

function card(): HTMLElement {
  return document.getElementById('card-quality-lab') as HTMLElement;
}

function buttons(): string[] {
  return Array.from(card().querySelectorAll('button'))
    .map((b) => (b.textContent || '').trim())
    .filter((t) => t && !t.startsWith('connection'));
}

function click(label: string): void {
  act(() => {
    Array.from(card().querySelectorAll('button'))
      .find((b) => b.textContent?.trim() === label)!
      .click();
  });
}

function typeId(): void {
  const input = document.querySelector('#ob-disc-confirm-input') as HTMLInputElement;
  act(() => {
    const setter = Object.getOwnPropertyDescriptor(
      window.HTMLInputElement.prototype,
      'value',
    )!.set!;
    setter.call(input, 'quality-lab');
    input.dispatchEvent(new Event('input', { bubbles: true }));
  });
}

async function typeIdAndConfirm(): Promise<void> {
  typeId();
  await act(async () => {
    (document.querySelector('#ob-disc-yes') as HTMLButtonElement).click();
  });
}

/** Mount with this tab's record of a failed full removal and the server's
 * removal_failed entry; then the catalog cache is left as it is (no
 * refresh comes before the next interval). */
async function mountStaleTab(): Promise<void> {
  removingStore.set({
    ids: { 'quality-lab': 'error' },
    errors: { 'quality-lab': 'graph delete failed' },
    modes: { 'quality-lab': { full: true, registration: false } },
  });
  vi.stubGlobal('fetch', vi.fn(fetchImpl));
  await act(async () => {
    root.render(
      <QueryClientProvider client={queryClient}>
        <CatalogList />
        <ObModals />
      </QueryClientProvider>,
    );
  });
  await vi.waitFor(() => expect(buttons()).toEqual(['Remove again']));
}

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  queryClient = createQueryClient();
  queryClient.setDefaultOptions({ queries: { retry: false } });
  ob.adminChecked = true;
  ob.isAdmin = true;
  server.entry = SYSTEM;
  server.posts = [];
  server.catalogReads = 0;
  server.failCatalog = false;
  server.holdNextCatalogRead = false;
  server.releaseHeld = null;
  server.recoverBeforePost = false;
  flowStore.set({ ...flowStore.get(), modal: null });
});

afterEach(() => {
  act(() => {
    flowStore.set({ ...flowStore.get(), modal: null });
    root.unmount();
  });
  queryClient.clear();
  container.remove();
  removingStore.set({ ids: {}, errors: {}, modes: {} });
  removalNoticeStore.set({});
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('Remove again in a tab whose catalog is stale', () => {
  it('opens nothing when the system was recovered elsewhere', async () => {
    await mountStaleTab();
    const readsBefore = server.catalogReads;
    // Another tab re-onboarded it; this tab's cache still says removal_failed.
    server.entry = { ...SYSTEM, status: 'onboarded', removal_mode: undefined };
    click('Remove again');
    await vi.waitFor(() => expect(buttons()).toContain('Remove system'));
    expect(server.catalogReads).toBeGreaterThan(readsBefore);
    expect(flowStore.get().modal).toBeNull();
    expect(server.posts).toEqual([]);
    expect(removingStore.get().ids['quality-lab']).toBeUndefined();
  });

  it('opens nothing when Reset All put it back to Registered', async () => {
    await mountStaleTab();
    server.entry = { ...SYSTEM, status: 'registered', removal_mode: undefined };
    click('Remove again');
    await vi.waitFor(() => expect(buttons()).toContain('Remove registration'));
    expect(flowStore.get().modal).toBeNull();
    expect(server.posts).toEqual([]);
  });

  it('removes nothing when the system is recovered while the dialog is open', async () => {
    await mountStaleTab();
    click('Remove again');
    await vi.waitFor(() => expect(document.querySelector('#ob-disc-confirm-input')).not.toBeNull());
    server.entry = { ...SYSTEM, status: 'onboarded', removal_mode: undefined };
    await typeIdAndConfirm();
    await vi.waitFor(() =>
      expect(document.querySelector('#ob-disc-state-changed')?.textContent).toContain(
        'Nothing was removed',
      ),
    );
    expect(server.posts).toEqual([]);
    expect((document.querySelector('#ob-disc-yes') as HTMLButtonElement).disabled).toBe(true);
  });

  it('opens nothing when the current state cannot be read', async () => {
    await mountStaleTab();
    server.failCatalog = true;
    click('Remove again');
    // Said (and shown) by the catalog's live region, not inside the card.
    await vi.waitFor(() =>
      expect(document.getElementById('ob-catalog-status')?.textContent).toContain(
        'Could not check',
      ),
    );
    expect(flowStore.get().modal).toBeNull();
    expect(server.posts).toEqual([]);
  });

  it('guard: a system that is still half-removed is retried in its full mode', async () => {
    await mountStaleTab();
    click('Remove again');
    await vi.waitFor(() => expect(document.querySelector('#ob-disc-confirm-input')).not.toBeNull());
    expect(flowStore.get().modal).toMatchObject({ kind: 'disconnect', retryFull: true });
    await typeIdAndConfirm();
    await vi.waitFor(() => expect(server.posts.length).toBe(1));
    expect(server.posts[0]).toContain('full=true');
    expect(server.posts[0]).toContain('retry=true');
  });
});

describe('Remove again against an older catalog read and a late recovery', () => {
  it('does not trust a catalog read already in flight before the confirm', async () => {
    await mountStaleTab();
    click('Remove again');
    await vi.waitFor(() => expect(document.querySelector('#ob-disc-confirm-input')).not.toBeNull());
    // The periodic read starts while the system is still half-removed and is
    // answered late; the system is recovered meanwhile.
    server.holdNextCatalogRead = true;
    void queryClient.refetchQueries({ queryKey: ['onboard', 'catalog'] });
    await vi.waitFor(() => expect(server.releaseHeld).not.toBeNull());
    server.entry = { ...SYSTEM, status: 'onboarded', removal_mode: undefined };
    typeId();
    await act(async () => {
      (document.querySelector('#ob-disc-yes') as HTMLButtonElement).click();
      // The older read answers (half-removed, as it was when it started)
      // while the confirm checks the state.
      await Promise.resolve();
      server.releaseHeld?.();
    });
    // Settled: either a removal was posted or the dialog said why not.
    await vi.waitFor(() =>
      expect(server.posts.length > 0 || !!document.querySelector('#ob-disc-state-changed')).toBe(
        true,
      ),
    );
    expect(server.posts).toEqual([]);
    expect(document.querySelector('#ob-disc-state-changed')?.textContent).toContain(
      'Nothing was removed',
    );
  });

  it('a recovery after the last read is refused by the server and shown', async () => {
    await mountStaleTab();
    click('Remove again');
    await vi.waitFor(() => expect(document.querySelector('#ob-disc-confirm-input')).not.toBeNull());
    server.recoverBeforePost = true;
    await typeIdAndConfirm();
    await vi.waitFor(() => expect(server.posts.length).toBe(1));
    expect(server.posts[0]).toContain('retry=true');
    await vi.waitFor(() =>
      expect(document.querySelector('#ob-disc-state-changed')?.textContent).toContain(
        'Nothing was removed',
      ),
    );
    expect((document.querySelector('#ob-disc-yes') as HTMLButtonElement).disabled).toBe(true);
    expect(removingStore.get().ids['quality-lab']).not.toBe('running');
  });
});

describe('the outcome of the click-time check is announced and focus is kept', () => {
  /** The catalog's one live region, outside every card (an onboarded card is
   * itself role="button", whose content is not exposed as a live region). */
  function region(): HTMLElement | null {
    return document.getElementById('ob-catalog-status');
  }

  /** Every live region in the document that says ``text``. */
  function liveRegionsSaying(text: string): HTMLElement[] {
    return Array.from(document.querySelectorAll<HTMLElement>('[role="status"]')).filter((el) =>
      (el.textContent || '').includes(text),
    );
  }

  async function refetchCatalog(): Promise<void> {
    await act(async () => {
      await queryClient.refetchQueries({ queryKey: ['onboard', 'catalog'] });
    });
  }

  function expectAnnouncedOutsideAnyButton(): void {
    const r = region();
    expect(r).not.toBeNull();
    expect(r!.getAttribute('role')).toBe('status');
    expect(r!.closest('[role="button"]')).toBeNull();
  }

  it('a recovered (onboarded) system: announced by the catalog region, focus on its card',
    async () => {
      await mountStaleTab();
      // Mounted before the outcome: its text changes after mount.
      expect(region()).not.toBeNull();
      expect(region()!.textContent).toBe('');
      server.entry = { ...SYSTEM, status: 'onboarded', removal_mode: undefined };
      click('Remove again');
      await vi.waitFor(() => expect(region()?.textContent).toContain('Its state changed'));
      expect(region()!.textContent).toContain('onboarded');
      expectAnnouncedOutsideAnyButton();
      expect(liveRegionsSaying('Its state changed')).toEqual([region()]);
      await vi.waitFor(() => expect(document.activeElement).toBe(card()));
    });

  it('a system back to Registered: announced, focus on Onboard', async () => {
    await mountStaleTab();
    server.entry = { ...SYSTEM, status: 'registered', removal_mode: undefined };
    click('Remove again');
    await vi.waitFor(() => expect(region()?.textContent).toContain('Its state changed'));
    expect(liveRegionsSaying('Its state changed')).toEqual([region()]);
    await vi.waitFor(() =>
      expect(document.activeElement).toBe(document.getElementById('btn-quality-lab')),
    );
  });

  it('a failed read: announced, focus stays on Remove again', async () => {
    await mountStaleTab();
    server.failCatalog = true;
    const again = Array.from(card().querySelectorAll('button')).find(
      (b) => b.textContent?.trim() === 'Remove again',
    )!;
    again.focus();
    click('Remove again');
    await vi.waitFor(() => expect(region()?.textContent).toContain('Could not check'));
    expect(liveRegionsSaying('Could not check')).toEqual([region()]);
    expect(document.activeElement?.textContent?.trim()).toBe('Remove again');
  });

  it('a system no longer in the catalog: announced in the list, focus on it', async () => {
    await mountStaleTab();
    server.entry = { ...SYSTEM, id: 'another-system', name: 'Another' };
    click('Remove again');
    await vi.waitFor(() => expect(region()?.textContent).toContain('no longer in the catalog'));
    expect(region()?.getAttribute('role')).toBe('status');
    await vi.waitFor(() => expect(document.activeElement).toBe(region()));
  });

  it('keeps the feedback visible while the state it describes holds', async () => {
    await mountStaleTab();
    server.entry = { ...SYSTEM, status: 'registered', removal_mode: undefined };
    click('Remove again');
    await vi.waitFor(() => expect(region()?.textContent).toContain('it is registered now'));
    await refetchCatalog();              // the same state again
    expect(region()!.textContent).toContain('it is registered now');
  });
});

describe('a removal notice is said once and does not outlive its state', () => {
  function anywhereSaying(text: string): boolean {
    return (document.body.textContent || '').includes(text);
  }

  /** Some live region says ``text`` (wherever this build announces it). */
  function announced(text: string): boolean {
    return Array.from(document.querySelectorAll('[role="status"]')).some((el) =>
      (el.textContent || '').includes(text),
    );
  }

  async function refetchCatalog(): Promise<void> {
    await act(async () => {
      await queryClient.refetchQueries({ queryKey: ['onboard', 'catalog'] });
    });
  }

  it('a recovered-to-Registered notice is not said again once the system is onboarded',
    async () => {
      await mountStaleTab();
      server.entry = { ...SYSTEM, status: 'registered', removal_mode: undefined };
      click('Remove again');
      await vi.waitFor(() => expect(announced('it is registered now')).toBe(true));
      // The user onboards it; the card remounts in the Onboarded section.
      server.entry = { ...SYSTEM, status: 'onboarded', removal_mode: undefined };
      await refetchCatalog();
      await vi.waitFor(() => expect(card().getAttribute('role')).toBe('button'));
      expect(anywhereSaying('it is registered now')).toBe(false);
      expect(removalNoticeStore.get()['quality-lab']).toBeUndefined();
    });

  it('starting an onboarding clears the notice of that system', async () => {
    await mountStaleTab();
    server.entry = { ...SYSTEM, status: 'registered', removal_mode: undefined };
    click('Remove again');
    await vi.waitFor(() => expect(announced('it is registered now')).toBe(true));
    await vi.waitFor(() => expect(document.getElementById('btn-quality-lab')).not.toBeNull());
    act(() => {
      (document.getElementById('btn-quality-lab') as HTMLButtonElement).click();
    });
    await vi.waitFor(() => expect(anywhereSaying('it is registered now')).toBe(false));
    expect(removalNoticeStore.get()['quality-lab']).toBeUndefined();
  });

  it('a "no longer in the catalog" notice is not shown on a later same-id registration',
    async () => {
      await mountStaleTab();
      server.entry = { ...SYSTEM, id: 'another-system', name: 'Another' };
      click('Remove again');
      await vi.waitFor(() => expect(announced('no longer in the catalog')).toBe(true));
      // Someone registers a system with the same id again.
      server.entry = { ...SYSTEM, status: 'registered', removal_mode: undefined };
      await refetchCatalog();
      await vi.waitFor(() => expect(document.getElementById('card-quality-lab')).not.toBeNull());
      expect(anywhereSaying('no longer in the catalog')).toBe(false);
    });

  it('is not said again when the catalog is mounted again', async () => {
    await mountStaleTab();
    server.entry = { ...SYSTEM, status: 'registered', removal_mode: undefined };
    click('Remove again');
    await vi.waitFor(() => expect(announced('it is registered now')).toBe(true));
    // Leave the Onboard view and come back: a fresh mount of the catalog.
    act(() => root.unmount());
    root = createRoot(container);
    await act(async () => {
      root.render(
        <QueryClientProvider client={queryClient}>
          <CatalogList />
          <ObModals />
        </QueryClientProvider>,
      );
    });
    await vi.waitFor(() => expect(document.getElementById('card-quality-lab')).not.toBeNull());
    const live = Array.from(document.querySelectorAll('[role="status"]')).filter((el) =>
      (el.textContent || '').includes('it is registered now'),
    );
    expect(live).toEqual([]);
  });
});

describe('two outcomes that arrive before the region updates', () => {
  function regionText(): string {
    return document.getElementById('ob-catalog-status')?.textContent || '';
  }

  async function mountCatalog(): Promise<void> {
    vi.stubGlobal('fetch', vi.fn(fetchImpl));
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

  it('says both, and marks each said only once it is in the region', async () => {
    // Two cards' checks settle in the same tick: one system is registered
    // again, the other has left the catalog. Both notices are unsaid when the
    // region next updates.
    server.entry = { ...SYSTEM, status: 'registered', removal_mode: undefined };
    await mountCatalog();
    act(() => {
      setRemovalNotice('quality-lab', 'Quality Lab is registered now.', false, 'registered');
      setRemovalNotice('other-lab', 'Other Lab is no longer in the catalog.', false, 'gone');
    });
    await vi.waitFor(() => {
      expect(regionText()).toContain('Quality Lab is registered now.');
      expect(regionText()).toContain('Other Lab is no longer in the catalog.');
    });
    const notices = removalNoticeStore.get();
    expect(notices['quality-lab'].announced).toBe(true);
    expect(notices['other-lab'].announced).toBe(true);
  });

  it('a notice that arrives while the catalog is still loading is said once it renders', async () => {
    server.entry = { ...SYSTEM, status: 'registered', removal_mode: undefined };
    server.holdNextCatalogRead = true;
    vi.stubGlobal('fetch', vi.fn(fetchImpl));
    await act(async () => {
      root.render(
        <QueryClientProvider client={queryClient}>
          <CatalogList />
          <ObModals />
        </QueryClientProvider>,
      );
    });
    act(() => {
      setRemovalNotice('quality-lab', 'Quality Lab is registered now.', false, 'registered');
    });
    expect(removalNoticeStore.get()['quality-lab'].announced).toBe(false);
    await act(async () => {
      server.releaseHeld?.();
    });
    await vi.waitFor(() => expect(regionText()).toContain('Quality Lab is registered now.'));
    expect(removalNoticeStore.get()['quality-lab'].announced).toBe(true);
  });

  it('a later outcome is added to the region without taking out the ones still current',
    async () => {
      server.entry = { ...SYSTEM, status: 'registered', removal_mode: undefined };
      await mountCatalog();
      act(() => {
        setRemovalNotice('quality-lab', 'Quality Lab is registered now.', false, 'registered');
        setRemovalNotice('other-lab', 'Other Lab is no longer in the catalog.', false, 'gone');
      });
      await vi.waitFor(() =>
        expect(regionText()).toContain('Other Lab is no longer in the catalog.'),
      );
      // A third card's check settles in the next tick.
      act(() => {
        setRemovalNotice('third-lab', 'Third Lab is no longer in the catalog.', false, 'gone');
      });
      await vi.waitFor(() =>
        expect(regionText()).toContain('Third Lab is no longer in the catalog.'),
      );
      expect(regionText()).toContain('Quality Lab is registered now.');
      expect(regionText()).toContain('Other Lab is no longer in the catalog.');
    });

  it('outcomes said a while ago give way to a new one, so the region does not grow', async () => {
    server.entry = { ...SYSTEM, status: 'registered', removal_mode: undefined };
    await mountCatalog();
    let now = 1_000_000;
    vi.spyOn(Date, 'now').mockImplementation(() => now);
    act(() => {
      setRemovalNotice('gone-a', 'A is no longer in the catalog.', false, 'gone');
    });
    await vi.waitFor(() => expect(regionText()).toContain('A is no longer in the catalog.'));
    now += 60_000;
    act(() => {
      setRemovalNotice('gone-b', 'B is no longer in the catalog.', false, 'gone');
    });
    await vi.waitFor(() => expect(regionText()).toContain('B is no longer in the catalog.'));
    expect(regionText()).not.toContain('A is no longer in the catalog.');
  });

  it('keeps at most a few earlier outcomes next to a new one', async () => {
    server.entry = { ...SYSTEM, status: 'registered', removal_mode: undefined };
    await mountCatalog();
    for (const id of ['a', 'b', 'c', 'd', 'e']) {
      act(() => {
        setRemovalNotice(`gone-${id}`, `${id.toUpperCase()} is gone.`, false, 'gone');
      });
      await vi.waitFor(() => expect(regionText()).toContain(`${id.toUpperCase()} is gone.`));
    }
    const said = ['A', 'B', 'C', 'D', 'E'].filter((x) => regionText().includes(`${x} is gone.`));
    expect(said).toEqual(['C', 'D', 'E']);
  });
});
