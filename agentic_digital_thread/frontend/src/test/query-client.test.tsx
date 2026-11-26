// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
import { QueryClientProvider, useQueryClient } from '@tanstack/react-query';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from '../api/client';
import { createQueryClient, queryJson } from '../api/query-client';
import { queryKeys } from '../api/query-keys';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

type RetryPolicy = (failureCount: number, error: unknown) => boolean;

const roots: Root[] = [];
const containers: HTMLDivElement[] = [];

function mount(element: React.ReactElement): Root {
  const container = document.createElement('div');
  document.body.appendChild(container);
  const root = createRoot(container);
  roots.push(root);
  containers.push(container);
  act(() => root.render(element));
  return root;
}

afterEach(() => {
  for (const root of roots.splice(0)) act(() => root.unmount());
  for (const container of containers.splice(0)) container.remove();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('Query client', () => {
  it('keeps the provider client identity across re-renders while factories isolate entries', () => {
    const client = createQueryClient();
    const otherEntryClient = createQueryClient();
    const observed: unknown[] = [];

    function Probe({ renderId }: { renderId: number }) {
      observed.push(useQueryClient());
      return <span>{renderId}</span>;
    }

    const root = mount(
      <QueryClientProvider client={client}>
        <Probe renderId={1} />
      </QueryClientProvider>,
    );
    act(() => {
      root.render(
        <QueryClientProvider client={client}>
          <Probe renderId={2} />
        </QueryClientProvider>,
      );
    });

    expect(otherEntryClient).not.toBe(client);
    expect(observed.length).toBeGreaterThanOrEqual(2);
    expect(observed.every((value) => value === client)).toBe(true);
  });

  it('does not retry 4xx ApiError and retries 5xx or network errors three times', () => {
    const client = createQueryClient();
    const retry = client.getDefaultOptions().queries?.retry as RetryPolicy;

    expect(retry(0, new ApiError(400, '/api/test', 'bad request'))).toBe(false);
    expect(retry(0, new ApiError(404, '/api/test', 'not found'))).toBe(false);
    expect(retry(0, new ApiError(499, '/api/test', 'closed'))).toBe(false);

    for (const error of [new ApiError(500, '/api/test', 'server error'), new TypeError('offline')]) {
      expect(retry(0, error)).toBe(true);
      expect(retry(1, error)).toBe(true);
      expect(retry(2, error)).toBe(true);
      expect(retry(3, error)).toBe(false);
    }
  });

  it('passes the query AbortSignal through requestJson to fetch', async () => {
    const client = createQueryClient();
    let querySignal: AbortSignal | undefined;
    let fetchSignal: AbortSignal | null | undefined;

    vi.stubGlobal(
      'fetch',
      vi.fn((_url: string, init?: RequestInit) => {
        fetchSignal = init?.signal;
        return new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener('abort', () => {
            reject(new DOMException('Aborted', 'AbortError'));
          });
        });
      }),
    );

    const pending = client.fetchQuery({
      queryKey: ['abort-test'],
      queryFn: ({ signal }) => {
        querySignal = signal;
        return queryJson<unknown>('/api/abort-test', signal);
      },
    });

    await vi.waitFor(() => expect(fetch).toHaveBeenCalledOnce());
    expect(querySignal).toBeInstanceOf(AbortSignal);
    expect(fetchSignal).toBe(querySignal);

    await client.cancelQueries({ queryKey: ['abort-test'] });
    expect(querySignal?.aborted).toBe(true);
    await pending.catch(() => undefined);
  });
});

describe('queryKeys', () => {
  it('is stable for equal inputs and unique across resources', () => {
    expect(queryKeys.admin.fleet(24)).toEqual(queryKeys.admin.fleet(24));
    expect(queryKeys.admin.fleet(24)).not.toEqual(queryKeys.admin.fleet(48));
    expect(queryKeys.admin.neptune(24)).toEqual(queryKeys.admin.neptune(24));

    const keys = [
      queryKeys.admin.settings(),
      queryKeys.admin.models(),
      queryKeys.admin.liveUpdates(),
      queryKeys.admin.llmSettings(),
      queryKeys.admin.conceptPacks(),
      queryKeys.admin.quickLinks(),
      queryKeys.admin.fleet(24),
      queryKeys.admin.neptune(24),
      queryKeys.admin.logTail(),
      queryKeys.systems(),
      queryKeys.catalog(),
      queryKeys.concepts(),
      queryKeys.prompts(),
      queryKeys.branding(),
      queryKeys.version(),
      queryKeys.demoStatus(),
      queryKeys.onboardStatus(),
      queryKeys.authMe(),
      queryKeys.thread.entities(),
      queryKeys.thread.graphSummary(),
      queryKeys.thread.analytics(),
    ];

    expect(new Set(keys.map((key) => JSON.stringify(key))).size).toBe(keys.length);
  });
});
