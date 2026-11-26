// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
import { act, createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError, requestJson } from '../api/client';
import { createQueryClient } from '../api/query-client';
import { queryKeys } from '../api/query-keys';
import {
  getSystemsQueryClient,
  registerSystemsQueryClient,
  SYSTEMS_REFETCH_INTERVAL_MS,
  SYSTEMS_STALE_TIME_MS,
  systemsQueryOptions,
} from '../api/systems-query';
import type { ConnectedSystemInfo, SystemsResponse } from '../api/types/dashboard';
import { ToastRegion } from '../shared/ui/ToastRegion';
import { clear as clearToasts } from '../shared/ui/toast-store';
import { dashboardStore } from '../views/dashboard/store';
import {
  _resetSystemsCache,
  loadSystems,
  observeSystems,
} from '../views/dashboard/systems';
import { invalidateOnboardingComplete } from '../views/onboard/onboard-queries';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>();
  return { ...actual, requestJson: vi.fn() };
});

const requestJsonMock = vi.mocked(requestJson);
let toastRoot: Root | null = null;

const SYS_A: ConnectedSystemInfo[] = [
  { id: 'erp', name: 'ERP', type: 'ERP', plant: 'Plant A', status: 'connected' },
];
const SYS_B: ConnectedSystemInfo[] = [
  ...SYS_A,
  { id: 'mes', name: 'MES', type: 'MES', plant: 'Plant A', status: 'connected' },
];

function systemsPayload(systems: ConnectedSystemInfo[]): SystemsResponse {
  return { systems };
}

function resetDashboardSystems(): void {
  dashboardStore.update((state) => ({ ...state, systems: null, showEmptyHero: null }));
}

beforeEach(() => {
  vi.useFakeTimers();
  clearToasts();
  requestJsonMock.mockReset();
  _resetSystemsCache();
  resetDashboardSystems();
  document.body.replaceChildren();
});

afterEach(() => {
  if (toastRoot) {
    act(() => toastRoot?.unmount());
    toastRoot = null;
  }
  clearToasts();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe('loadSystems — shared Query cache', () => {
  it('centralizes the five-minute stale and one-minute visible-page polling policy', () => {
    const options = systemsQueryOptions();
    expect(options.queryKey).toEqual(queryKeys.systems());
    expect(options.staleTime).toBe(SYSTEMS_STALE_TIME_MS);
    expect(options.refetchInterval).toBe(SYSTEMS_REFETCH_INTERVAL_MS);
    expect(options.refetchIntervalInBackground).toBe(false);
  });

  it('publishes interval refreshes only while the dashboard observer is active', async () => {
    requestJsonMock
      .mockResolvedValueOnce(systemsPayload(SYS_A))
      .mockResolvedValueOnce(systemsPayload(SYS_B));

    const deactivate = observeSystems();
    await vi.advanceTimersByTimeAsync(0);
    expect(requestJsonMock).toHaveBeenCalledTimes(1);
    expect(dashboardStore.get().systems).toEqual(SYS_A);

    await vi.advanceTimersByTimeAsync(SYSTEMS_REFETCH_INTERVAL_MS);
    expect(requestJsonMock).toHaveBeenCalledTimes(2);
    expect(dashboardStore.get().systems).toEqual(SYS_B);

    deactivate();
    await vi.advanceTimersByTimeAsync(SYSTEMS_REFETCH_INTERVAL_MS * 2);
    expect(requestJsonMock).toHaveBeenCalledTimes(2);
    expect(dashboardStore.get().systems).toEqual(SYS_B);
  });

  it('first load requests and populates the dashboard stores', async () => {
    requestJsonMock.mockResolvedValue(systemsPayload(SYS_A));

    await loadSystems();

    expect(requestJsonMock).toHaveBeenCalledTimes(1);
    expect(dashboardStore.get().systems).toEqual(SYS_A);
    expect(window._systemsById?.erp?.name).toBe('ERP');
  });

  it('reuses fresh Query data without another request inside staleTime', async () => {
    requestJsonMock.mockResolvedValue(systemsPayload(SYS_A));

    await loadSystems();
    resetDashboardSystems();
    await vi.advanceTimersByTimeAsync(SYSTEMS_STALE_TIME_MS - 1);
    await loadSystems();

    expect(requestJsonMock).toHaveBeenCalledTimes(1);
    expect(dashboardStore.get().systems).toEqual(SYS_A);
  });

  it('refetches stale data after the shared five-minute staleTime', async () => {
    requestJsonMock
      .mockResolvedValueOnce(systemsPayload(SYS_A))
      .mockResolvedValueOnce(systemsPayload(SYS_B));

    await loadSystems();
    await vi.advanceTimersByTimeAsync(SYSTEMS_STALE_TIME_MS + 1);
    await loadSystems();

    expect(requestJsonMock).toHaveBeenCalledTimes(2);
    expect(dashboardStore.get().systems).toEqual(SYS_B);
  });

  it('deduplicates concurrent dashboard callers through fetchQuery', async () => {
    let resolveRequest!: (response: SystemsResponse) => void;
    requestJsonMock.mockImplementation(
      () => new Promise<SystemsResponse>((resolve) => { resolveRequest = resolve; }),
    );

    const first = loadSystems();
    const second = loadSystems();
    expect(requestJsonMock).toHaveBeenCalledTimes(1);

    resolveRequest(systemsPayload(SYS_A));
    await Promise.all([first, second]);
    expect(dashboardStore.get().systems).toEqual(SYS_A);
  });

  it('an explicit systems invalidation drives the next imperative refresh', async () => {
    requestJsonMock
      .mockResolvedValueOnce(systemsPayload(SYS_A))
      .mockResolvedValueOnce(systemsPayload(SYS_B));
    const queryClient = getSystemsQueryClient();

    await loadSystems();
    await queryClient.invalidateQueries({ queryKey: queryKeys.systems(), refetchType: 'none' });
    await loadSystems();

    expect(requestJsonMock).toHaveBeenCalledTimes(2);
    expect(dashboardStore.get().systems).toEqual(SYS_B);
  });

  it('onboarding invalidation reaches the app-registered dashboard consumer', async () => {
    const appClient = createQueryClient();
    registerSystemsQueryClient(appClient);
    requestJsonMock
      .mockResolvedValueOnce(systemsPayload(SYS_A))
      .mockResolvedValueOnce(systemsPayload(SYS_B));

    await loadSystems();
    expect(getSystemsQueryClient()).toBe(appClient);

    await invalidateOnboardingComplete(appClient);
    await loadSystems();

    expect(requestJsonMock).toHaveBeenCalledTimes(2);
    expect(dashboardStore.get().systems).toEqual(SYS_B);
  });

  it('keeps rendered systems when an invalidated refresh fails', async () => {
    requestJsonMock
      .mockResolvedValueOnce(systemsPayload(SYS_A))
      .mockRejectedValueOnce(new ApiError(400, '/api/systems', 'refresh rejected'));
    const queryClient = getSystemsQueryClient();
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {});

    await loadSystems();
    await queryClient.invalidateQueries({ queryKey: queryKeys.systems(), refetchType: 'none' });
    await loadSystems();

    expect(requestJsonMock).toHaveBeenCalledTimes(2);
    expect(dashboardStore.get().systems).toEqual(SYS_A);
    expect(queryClient.getQueryData(queryKeys.systems())).toEqual(systemsPayload(SYS_A));
    expect(errorSpy).toHaveBeenCalled();
  });

  it('surfaces a failed first load and can refetch successfully afterward', async () => {
    requestJsonMock
      .mockRejectedValueOnce(new ApiError(400, '/api/systems', 'bad request'))
      .mockResolvedValueOnce(systemsPayload(SYS_A));
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    const toastContainer = document.createElement('div');
    document.body.appendChild(toastContainer);
    toastRoot = createRoot(toastContainer);
    act(() => toastRoot?.render(createElement(ToastRegion)));

    await act(async () => {
      await loadSystems();
    });
    expect(dashboardStore.get().systems).toBeNull();
    expect(document.querySelector('[role="status"]')?.textContent).toContain(
      'Failed to load connected systems',
    );

    await loadSystems();
    expect(requestJsonMock).toHaveBeenCalledTimes(2);
    expect(dashboardStore.get().systems).toEqual(SYS_A);
    expect(errorSpy).toHaveBeenCalled();
  });
});
