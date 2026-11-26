// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
import { QueryClientProvider } from '@tanstack/react-query';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { createQueryClient } from '../api/query-client';
import { queryKeys } from '../api/query-keys';
import type { CatalogSystem } from '../api/types/onboard';
import { setBusy } from '../views/onboard/catalog-store';
import { CatalogList } from '../views/onboard/components/CatalogList';
import { ObFlow } from '../views/onboard/components/ObFlow';
import {
  appendCheckpoint,
  appendMsg,
  flowStore,
  openDetail,
  patchProgress,
  resetToWelcome,
  startWizard,
} from '../views/onboard/flow-store';
import { ob } from '../views/onboard/state';
import { obStart } from '../views/onboard/wizard';

vi.mock('../views/onboard/wizard', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../views/onboard/wizard')>();
  return { ...actual, obStart: vi.fn(async () => {}) };
});

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;
let queryClient: ReturnType<typeof createQueryClient>;

const SYSTEM: CatalogSystem = {
  id: 'mes-1',
  name: 'Plant MES',
  system_type: 'MES',
  plant: 'Plant A',
  connection_type: 'postgres',
  status: 'onboarded',
};

function response(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status >= 400 ? 'Request failed' : 'OK',
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
  ob.isAdmin = false;
});

afterEach(() => {
  act(() => root.unmount());
  queryClient.clear();
  container.remove();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('CatalogList async states', () => {
  it('uses the shared LoadingState while the first catalog request is pending', async () => {
    vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>(() => {})));
    await mount();

    expect(container.querySelector('.async-state--loading[role="status"]')?.textContent).toContain(
      'Loading systems',
    );
    expect(container.querySelector('.sys-card')).toBeNull();
  });

  it('shows a retryable ErrorState and Retry recovers the catalog', async () => {
    let attempts = 0;
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        if (url !== '/api/onboard/catalog') throw new Error(`unexpected URL: ${url}`);
        attempts += 1;
        return attempts === 1
          ? response({ detail: 'catalog offline' }, 503)
          : response({ systems: [SYSTEM] });
      }),
    );
    await mount();

    await vi.waitFor(() => {
      expect(container.querySelector('.async-state--error')?.textContent).toContain('catalog offline');
    });
    const retry = container.querySelector('.async-state--error button') as HTMLButtonElement;
    await act(async () => {
      retry.click();
    });

    await vi.waitFor(() => {
      expect(document.getElementById('card-mes-1')).not.toBeNull();
    });
    expect(attempts).toBe(2);
    expect(container.querySelector('.async-state--error')).toBeNull();
  });

  it('renders a shared EmptyState for a successful empty catalog', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response({ systems: [] })));
    await mount();

    await vi.waitFor(() => {
      expect(container.querySelector('.async-state--empty')?.textContent).toContain(
        'No systems are registered yet.',
      );
    });
    expect(container.querySelector('.async-state--error')).toBeNull();
  });

  it('retains existing system cards and marks refresh while refetching', async () => {
    let calls = 0;
    let resolveRefresh!: (value: Response) => void;
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        if (url !== '/api/onboard/catalog') throw new Error(`unexpected URL: ${url}`);
        calls += 1;
        if (calls === 1) return response({ systems: [SYSTEM] });
        return new Promise<Response>((resolve) => {
          resolveRefresh = resolve;
        });
      }),
    );
    await mount();
    await vi.waitFor(() => expect(document.getElementById('card-mes-1')).not.toBeNull());

    let refresh!: Promise<void>;
    act(() => {
      refresh = queryClient.invalidateQueries({ queryKey: queryKeys.catalog() });
    });
    await vi.waitFor(() => expect(calls).toBe(2));

    expect(document.getElementById('card-mes-1')).not.toBeNull();
    await vi.waitFor(() => {
      expect(
        container.querySelector('.async-state--loading.async-state--compact')?.textContent,
      ).toContain('Refreshing systems');
    });

    await act(async () => {
      resolveRefresh(response({ systems: [SYSTEM] }));
      await refresh;
    });
    expect(document.getElementById('card-mes-1')).not.toBeNull();
    await vi.waitFor(() => {
      expect(container.textContent).not.toContain('Refreshing systems');
    });
  });
});

describe('a registered card while its onboarding runs', () => {
  const HUB: CatalogSystem = {
    id: 'hub-1',
    name: 'Plant Hub',
    system_type: 'UNS',
    plant: 'Plant 1',
    connection_type: 'mcp',
    status: 'registered',
  };
  const DETAIL_SYS: CatalogSystem = { ...SYSTEM, id: 'mes-2', name: 'Plant MES 2' };

  function hubButton(): HTMLButtonElement {
    return document.getElementById('btn-hub-1') as HTMLButtonElement;
  }

  async function mountHub() {
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      if (url !== '/api/onboard/catalog') throw new Error(`unexpected URL: ${url}`);
      return response({ systems: [HUB, DETAIL_SYS] });
    }));
    await mount();
    await vi.waitFor(() => expect(hubButton()).not.toBeNull());
  }

  afterEach(() => {
    setBusy('hub-1', false);
    ob.active = null;
    resetToWelcome();
  });

  it('reads Onboarding… (not Onboard) and stays clickable', async () => {
    await mountHub();
    expect(hubButton().textContent).toBe('Onboard');

    act(() => setBusy('hub-1', true));

    expect(hubButton().textContent).toBe('Onboarding…');
    expect(hubButton().title).toBe('Onboarding is in progress');
    expect(hubButton().disabled).toBe(false);
    expect(document.getElementById('card-hub-1')!.textContent).not.toMatch(/\bOnboard\b/);
  });

  it('a click from another system\'s detail screen returns to the running wizard, never obStart', async () => {
    await mountHub();
    // The run for this system: wizard mode, then the user opened another
    // card's detail screen (which snapshots the running wizard).
    ob.active = 'hub-1';
    act(() => {
      startWizard();
      setBusy('hub-1', true);
      openDetail(DETAIL_SYS);
    });
    expect(flowStore.get().screen).toBe('detail');

    await act(async () => {
      hubButton().click();
    });

    expect(flowStore.get().screen).toBe('flow');
    expect(flowStore.get().mode).toBe('wizard');
    expect(flowStore.get().modal).toBeNull();
    expect(vi.mocked(obStart)).not.toHaveBeenCalled();
  });

  it('a click while the wizard is already showing changes nothing (no restart, no cancel dialog)', async () => {
    await mountHub();
    ob.active = 'hub-1';
    act(() => {
      startWizard();
      setBusy('hub-1', true);
    });
    const before = flowStore.get();

    await act(async () => {
      hubButton().click();
    });

    expect(flowStore.get().mode).toBe('wizard');
    expect(flowStore.get().entries).toBe(before.entries);
    expect(flowStore.get().modal).toBeNull();
    expect(vi.mocked(obStart)).not.toHaveBeenCalled();
  });

  it('the active run waiting on a failed step (busy cleared) still reads Onboarding…', async () => {
    await mountHub();
    ob.active = 'hub-1';
    act(() => {
      startWizard();
      setBusy('hub-1', false);
    });
    expect(hubButton().textContent).toBe('Onboarding…');
  });

  it('progress made while another card\'s detail is open survives the click and its checkpoint stays actionable', async () => {
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      if (url === '/api/onboard/catalog') return response({ systems: [HUB, DETAIL_SYS] });
      if (url === '/api/onboard/concepts') return response({ concepts: [] });
      throw new Error(`unexpected URL: ${url}`);
    }));
    await act(async () => {
      root.render(
        <QueryClientProvider client={queryClient}>
          <CatalogList />
          <ObFlow />
        </QueryClientProvider>,
      );
    });
    await vi.waitFor(() => expect(hubButton()).not.toBeNull());

    ob.active = 'hub-1';
    act(() => {
      startWizard();
      setBusy('hub-1', true);
      appendMsg('Found 3 tables.');
      openDetail(DETAIL_SYS);
    });

    // The run keeps going while the detail screen is open: a step finishes
    // and its checkpoint appears.
    const onConfirm = vi.fn();
    act(() => {
      appendMsg('Mapped 12 fields.');
      patchProgress({ activeIdx: 1, statusText: 'Mapping concepts...' });
      appendCheckpoint({
        title: 'Review the mapping',
        content: { kind: 'mcpSummary', name: 'Plant Hub', count: 3 },
        confirmLabel: 'Confirm mapping',
        stepContext: { step: 'map', systemId: 'hub-1', data: {}, history: [] },
        onConfirm,
      });
    });
    expect(flowStore.get().screen).toBe('detail');

    await act(async () => {
      hubButton().click();
    });

    const flow = flowStore.get();
    expect(flow.screen).toBe('flow');
    expect(flow.mode).toBe('wizard');
    expect(flow.saved).toBeNull();
    expect(flow.detail).toBeNull();
    expect(flow.progress?.activeIdx).toBe(1);
    expect(flow.progress?.statusText).toBe('Mapping concepts...');
    const text = document.getElementById('onboard-flow')!.textContent;
    expect(text).toContain('Found 3 tables.');
    expect(text).toContain('Mapped 12 fields.');
    const confirm = Array.from(document.querySelectorAll<HTMLButtonElement>('.ob-confirm-btn'))
      .find((b) => b.textContent === 'Confirm mapping');
    expect(confirm).toBeTruthy();
    expect(confirm!.disabled).toBe(false);
    await act(async () => {
      confirm!.click();
    });
    expect(onConfirm).toHaveBeenCalledTimes(1);
    expect(vi.mocked(obStart)).not.toHaveBeenCalled();
  });
});
