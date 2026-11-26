// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
import { act, createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { queryKeys } from '../api/query-keys';
import type { CatalogSystem } from '../api/types/onboard';
import { obSubmitCredentials } from '../views/onboard/chat';
import { obTestConnection } from '../views/onboard/catalog';
import {
  catalogStore,
  toggleEditOpen,
} from '../views/onboard/catalog-store';
import { getOnboardQueryClient } from '../views/onboard/onboard-queries';
import { CatalogList } from '../views/onboard/components/CatalogList';
import { obDisconnectConfirmed } from '../views/onboard/flow';
import {
  appendCredentialForm,
  flowStore,
  resetToWelcome,
  startWizard,
} from '../views/onboard/flow-store';
import { obJoinCandidateDecide } from '../views/onboard/render';
import { ob } from '../views/onboard/state';
import { obStart } from '../views/onboard/wizard';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function setCatalogData(systems: CatalogSystem[], isAdmin = false): void {
  getOnboardQueryClient().setQueryData(queryKeys.catalog(), systems);
  ob.isAdmin = isAdmin;
  ob.adminChecked = true;
}

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  resetToWelcome();
  startWizard();
  getOnboardQueryClient().clear();
  setCatalogData([]);
  catalogStore.set({
    connValues: {},
    connOpen: {},
    testResults: {},
    editOpen: {},
    busy: {},
    connectNewOpen: false,
    connectNewBusy: false,
    connectNewError: null,
  });
  ob.active = null;
  ob.abort = null;
  ob.currentStep = null;
});

afterEach(() => {
  ob.abort?.abort();
  ob.active = null;
  ob.abort = null;
  ob.currentStep = null;
  act(() => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('onboarding request layer', () => {
  it('posts credential payloads and surfaces checked server detail instead of stored success', async () => {
    const entryId = appendCredentialForm([{ name: 'token', type: 'password' }], 'rest-1');
    const fetchMock = vi.fn(async () =>
      jsonResponse({ detail: 'credential rejected by policy' }, 422),
    );
    vi.stubGlobal('fetch', fetchMock);

    await obSubmitCredentials(entryId, 'rest-1', { token: 'example-token' });

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/onboard/credentials',
      expect.objectContaining({
        method: 'POST',
        credentials: 'same-origin',
        body: JSON.stringify({
          system_id: 'rest-1',
          credentials: { token: 'example-token' },
        }),
      }),
    );
    const entry = flowStore.get().entries.find((candidate) => candidate.id === entryId);
    expect(entry).toMatchObject({
      kind: 'credentialForm',
      status: 'idle',
      errorText: 'Failed: credential rejected by policy',
    });
  });

  it('keeps the note a stored answer carries, so the form can show it', async () => {
    const note =
      "'rest-1' is provisioned, and its agent keeps the credentials it was started with " +
      'until it is provisioned again (Provision on its card in the Onboard view).';
    const withNote = appendCredentialForm([{ name: 'token', type: 'password' }], 'rest-1');
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => jsonResponse({ status: 'stored', secret_arn: 'arn', note })),
    );
    await obSubmitCredentials(withNote, 'rest-1', { token: 'example-token' });
    const stored = flowStore.get().entries.find((candidate) => candidate.id === withNote);
    expect(stored).toMatchObject({ kind: 'credentialForm', status: 'stored', note });

    const plain = appendCredentialForm([{ name: 'token', type: 'password' }], 'rest-1');
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => jsonResponse({ status: 'stored', secret_arn: 'arn' })),
    );
    await obSubmitCredentials(plain, 'rest-1', { token: 'example-token' });
    const bare = flowStore.get().entries.find((candidate) => candidate.id === plain);
    expect(bare).toMatchObject({ kind: 'credentialForm', status: 'stored' });
    expect(bare && 'note' in bare ? bare.note : undefined).toBeUndefined();
  });

  it('uses the encoded proposed-mapping endpoint and rejects non-2xx server detail', async () => {
    const fetchMock = vi.fn(async () =>
      jsonResponse({ error: 'mapping decision conflict' }, 409),
    );
    vi.stubGlobal('fetch', fetchMock);

    await expect(obJoinCandidateDecide('map/id', 'accept')).rejects.toThrow(
      'mapping decision conflict',
    );
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/proposed-mappings/map%2Fid/accept',
      expect.objectContaining({ method: 'POST', credentials: 'same-origin' }),
    );
  });

  it('forwards the active abort signal and exact discover body through requestJson', async () => {
    vi.useFakeTimers();
    try {
      const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        // Step 0 (E1): a ready preflight lets the pipeline reach discover.
        if (url === '/api/onboard/preflight') {
          return Promise.resolve(jsonResponse({ job_id: 'pf-1' }));
        }
        if (url === '/api/onboard/preflight/pf-1') {
          return Promise.resolve(
            jsonResponse({
              status: 'done',
              result: { verdict: 'ready', connection: { status: 'pass' }, tables: [] },
            }),
          );
        }
        return new Promise<Response>((_resolve, reject) => {
          const rejectAbort = () =>
            reject(new DOMException('The operation was aborted', 'AbortError'));
          if (init?.signal?.aborted) rejectAbort();
          else init?.signal?.addEventListener('abort', rejectAbort, { once: true });
        });
      });
      vi.stubGlobal('fetch', fetchMock);

      const run = obStart({
        id: 'cmms',
        name: 'CMMS',
        connection_type: 'PostgreSQL',
        connection_string: 'postgresql://example.test/maintenance',
      });
      expect(fetchMock).toHaveBeenCalledOnce();

      // The preflight start POST carries the pinned body and the run signal.
      const [preflightUrl, preflightInit] = fetchMock.mock.calls[0];
      expect(preflightUrl).toBe('/api/onboard/preflight');
      expect(preflightInit).toEqual(
        expect.objectContaining({
          method: 'POST',
          credentials: 'same-origin',
          signal: ob.abort!.signal,
        }),
      );
      expect(JSON.parse(String(preflightInit?.body))).toEqual({ system_id: 'cmms' });

      // After the ready verdict, discover fires with the exact legacy body.
      await vi.advanceTimersByTimeAsync(3000);
      const discoverCall = fetchMock.mock.calls.find(
        ([url]) => String(url) === '/api/onboard/discover',
      );
      expect(discoverCall).toBeDefined();
      expect(discoverCall![1]).toEqual(
        expect.objectContaining({
          method: 'POST',
          credentials: 'same-origin',
          signal: ob.abort!.signal,
        }),
      );
      expect(JSON.parse(String(discoverCall![1]?.body))).toEqual({
        system_id: 'cmms',
        connection_string: 'postgresql://example.test/maintenance',
        connection_type: 'PostgreSQL',
        connection_config: {},
      });

      const cancelled = run.catch((error: unknown) => error);
      ob.abort!.abort();
      await expect(cancelled).resolves.toMatchObject({ name: 'AbortError' });
    } finally {
      vi.useRealTimers();
    }
  });

  it('returns disconnect server detail through the modal error-string contract', async () => {
    const fetchMock = vi.fn(async () =>
      jsonResponse({ detail: 'runtime is still deleting' }, 409),
    );
    vi.stubGlobal('fetch', fetchMock);

    await expect(obDisconnectConfirmed('cmms')).resolves.toBe(
      'runtime is still deleting',
    );
    // Removal runs as a background job (?wait=false) so the request returns
    // before the CloudFront 120 s origin timeout; the UI polls reset status.
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/onboard/reset/cmms?wait=false',
      expect.objectContaining({ method: 'POST', credentials: 'same-origin' }),
    );
  });

  it('passes full=true only when the forget-registration option is chosen', async () => {
    const fetchMock = vi.fn(async () =>
      jsonResponse({ error: 'nope' }),
    );
    vi.stubGlobal('fetch', fetchMock);
    await obDisconnectConfirmed('cmms', true);
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/onboard/reset/cmms?wait=false&full=true',
      expect.objectContaining({ method: 'POST' }),
    );
    fetchMock.mockClear();
    await obDisconnectConfirmed('cmms', false);
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/onboard/reset/cmms?wait=false',
      expect.objectContaining({ method: 'POST' }),
    );
  });

  it('routes admin connection tests through the checked POST endpoint', async () => {
    setCatalogData(
      [{ id: 'cmms', name: 'CMMS', connection_type: 'PostgreSQL' }],
      true,
    );
    const fetchMock = vi.fn(async () =>
      jsonResponse({ status: 'pass', latency_ms: 17 }),
    );
    vi.stubGlobal('fetch', fetchMock);

    await obTestConnection('cmms');

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/admin/connection/cmms/test',
      expect.objectContaining({ method: 'POST', credentials: 'same-origin' }),
    );
    expect(catalogStore.get().testResults.cmms).toMatchObject({
      state: 'done',
      text: '✓ Connected · 17 ms',
    });
  });

  it('loads connection and scan-budget JSON, then PUTs the exact budget payload', async () => {
    setCatalogData(
      [
        {
          id: 'athena-1',
          name: 'Data Lake',
          connection_type: 'Athena',
          status: 'registered',
        },
      ],
      true,
    );
    toggleEditOpen('athena-1');
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === '/api/admin/connection/athena-1') {
        return jsonResponse({ scheme: 'athena', has_secret: true });
      }
      if (url === '/api/admin/scan-budget/athena-1' && init?.method === 'PUT') {
        return jsonResponse({ status: 'ok', max_scanned_bytes: 4096 });
      }
      if (url === '/api/admin/scan-budget/athena-1') {
        return jsonResponse({ effective_max_scanned_bytes: 1024, max_scanned_bytes: 1024 });
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);

    await act(async () => {
      root.render(createElement(CatalogList));
      await Promise.resolve();
    });
    await act(async () => {
      await Promise.resolve();
    });

    const input = container.querySelector(
      'input[placeholder="bytes (blank = no per-system cap)"]',
    ) as HTMLInputElement | null;
    expect(input).not.toBeNull();
    act(() => {
      const setter = Object.getOwnPropertyDescriptor(
        HTMLInputElement.prototype,
        'value',
      )?.set;
      setter?.call(input, '4096');
      input!.dispatchEvent(new Event('input', { bubbles: true }));
    });
    const saveButton = Array.from(container.querySelectorAll('button')).find(
      (button) => button.textContent === 'Save budget',
    ) as HTMLButtonElement | undefined;
    expect(saveButton).toBeDefined();
    await act(async () => {
      saveButton!.click();
      await Promise.resolve();
    });

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/admin/scan-budget/athena-1',
      expect.objectContaining({
        method: 'PUT',
        credentials: 'same-origin',
        body: JSON.stringify({ max_scanned_bytes: 4096 }),
      }),
    );
    expect(container.textContent).toContain('Budget set to 4096 bytes.');
  });
});
