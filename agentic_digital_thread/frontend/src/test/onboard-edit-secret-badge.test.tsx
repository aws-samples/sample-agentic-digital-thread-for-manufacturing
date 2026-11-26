// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
/* The Edit connection form badges a secret field "configured" only when the
 * server reports a stored value for THAT field (GET secret_fields). A REST
 * system with a secret blob holding one key used to badge every secret field
 * too. */
import { QueryClientProvider } from '@tanstack/react-query';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { createQueryClient } from '../api/query-client';
import type { CatalogSystem } from '../api/types/onboard';
import {
  EditConnectionForm,
  storedSecretKeys,
  sysCardMeta,
} from '../views/onboard/components/CatalogList';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;

const REST_SYS: CatalogSystem = {
  id: 'mes-api',
  name: 'Plant MES',
  connection_type: 'MCP Server',
  status: 'onboarded',
  secret_arn: 'arn:aws:secretsmanager:us-east-1:111122223333:secret:adt/conn/mes-api-AbCdEf',
  has_secret: true,
};

function response(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    headers: new Headers({ 'content-type': 'application/json' }),
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as Response;
}

function stubServer(described: Record<string, unknown>) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      if (String(url).includes('/api/admin/connection/')) return response(described);
      return response({});
    }),
  );
}

async function mount(sys: CatalogSystem) {
  const client = createQueryClient();
  client.setDefaultOptions({ queries: { retry: false } });
  await act(async () => {
    root.render(
      <QueryClientProvider client={client}>
        <EditConnectionForm systemId={String(sys.id)} sys={sys} />
      </QueryClientProvider>,
    );
  });
}

/** Label text of every field that carries the "configured" badge. */
function badgedLabels(): string[] {
  return Array.from(container.querySelectorAll('label'))
    .filter((l) => (l.textContent || '').includes('configured'))
    .map((l) => (l.textContent || '').replace('configured ✓', '').trim());
}

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('Edit connection: per-field "configured" badge', () => {
  it('badges only the secret fields the server reports as stored', async () => {
    stubServer({ system_id: 'mes-api', has_secret: true, secret_fields: ['token'] });
    await mount(REST_SYS);
    await vi.waitFor(() => expect(badgedLabels()).toEqual(['Token']));
    expect(container.textContent).toContain('MCP Server URL');
  });

  it('badges nothing when the secret blob holds no values, even with has_secret', async () => {
    stubServer({ system_id: 'mes-api', has_secret: true, secret_fields: [] });
    await mount(REST_SYS);
    await vi.waitFor(() => expect(container.textContent).toContain('Token'));
    await act(async () => {});
    expect(badgedLabels()).toEqual([]);
  });
});

describe('storedSecretKeys', () => {
  it('keeps only non-empty string keys', () => {
    expect([...storedSecretKeys(['client_secret', '', 7, null])]).toEqual(['client_secret']);
    expect(storedSecretKeys(undefined).size).toBe(0);
    expect(storedSecretKeys('token').size).toBe(0);
  });
});

describe('sysCardMeta', () => {
  it('skips blank segments instead of rendering an empty slot', () => {
    expect(sysCardMeta({ system_type: 'CMMS', plant: '', connection_type: 'REST API' })).toBe(
      'CMMS\u00a0 · \u00a0REST API',
    );
    expect(sysCardMeta({ system_type: 'MES', plant: 'Plant 7', connection_type: 'PostgreSQL' })).toBe(
      'MES\u00a0 · \u00a0Plant 7\u00a0 · \u00a0PostgreSQL',
    );
    expect(sysCardMeta({ connection_type: 'MCP Server' })).toBe('MCP Server');
  });
});
