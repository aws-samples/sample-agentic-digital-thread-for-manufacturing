// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
/* The Edit form of a user-registered MCP system offers "Authorization
 * header" (Bearer token / X-API-Key / Token as entered). It shows the scheme
 * the server reports for the system (GET /api/admin/connection/{id}) and
 * saving sends it as the existing PUT /api/admin/connection `auth_scheme`
 * field. HighByte and the bundled demo MCP systems keep their form. */
import { QueryClientProvider } from '@tanstack/react-query';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { createQueryClient } from '../api/query-client';
import type { CatalogSystem } from '../api/types/onboard';
import { EditConnectionForm } from '../views/onboard/components/CatalogList';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;
let puts: { url: string; body: Record<string, unknown> }[];

const USER_MCP: CatalogSystem = {
  id: 'quality-lab',
  name: 'Quality Lab',
  connection_type: 'MCP Server',
  connection_string: 'https://mcp.example.com/mcp',
  status: 'registered',
  connection_origin: 'user',
  secret_arn: 'arn:aws:secretsmanager:us-east-1:111122223333:secret:adt/conn/quality-lab-AbCdEf',
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
    vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method === 'PUT') {
        puts.push({ url, body: JSON.parse(String(init.body || '{}')) });
        return response({ status: 'ok', reconnected: true });
      }
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

function select(): HTMLSelectElement | null {
  return container.querySelector('select[data-field="auth_scheme"]');
}

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  puts = [];
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('Edit connection: Authorization header for a user MCP system', () => {
  it('offers the three headers and shows the stored scheme', async () => {
    stubServer({ system_id: 'quality-lab', auth_scheme: 'bearer_literal', has_secret: true });
    await mount(USER_MCP);
    expect(container.textContent).toContain('Authorization header');
    await vi.waitFor(() => expect(select()?.value).toBe('bearer_literal'));
    const labels = Array.from(select()!.options).map((o) => [o.value, o.textContent]);
    expect(labels).toEqual([
      ['bearer', 'Bearer token'],
      ['api_key', 'X-API-Key'],
      ['bearer_literal', 'Token as entered'],
    ]);
  });

  it('saving sends the chosen scheme as auth_scheme and never a blank token', async () => {
    stubServer({ system_id: 'quality-lab', auth_scheme: 'bearer', has_secret: true });
    await mount(USER_MCP);
    await vi.waitFor(() => expect(select()?.value).toBe('bearer'));
    await act(async () => {
      const s = select()!;
      const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value')!.set!;
      setter.call(s, 'api_key');
      s.dispatchEvent(new Event('change', { bubbles: true }));
    });
    const save = Array.from(container.querySelectorAll('button')).find((b) =>
      (b.textContent || '').includes('Save'),
    )!;
    await act(async () => {
      save.click();
    });
    await vi.waitFor(() => expect(puts).toHaveLength(1));
    expect(puts[0].url).toContain('/api/admin/connection/quality-lab');
    expect(puts[0].body.auth_scheme).toBe('api_key');
    expect('token' in puts[0].body).toBe(false);
  });

  it.each([
    ['HighByte', { ...USER_MCP, id: 'highbyte', name: 'HighByte' }],
    ['a demo MCP system', { ...USER_MCP, id: 'demo-mcp', connection_origin: 'demo' }],
    ['an MCP system without an origin', { ...USER_MCP, connection_origin: undefined }],
  ])('%s keeps its form without the selector', async (_name, sys) => {
    stubServer({ system_id: String(sys.id), auth_scheme: 'bearer_literal' });
    await mount(sys as CatalogSystem);
    expect(container.textContent).toContain('MCP Server URL');
    expect(select()).toBeNull();
    expect(container.textContent).not.toContain('Authorization header');
  });
});
