// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
/* Connect New System registers EVERY connection type through
 * POST /api/onboard/connect before the wizard starts, and onboards the id the
 * SERVER returned. PostgreSQL, REST API and MCP Server used to mint a
 * client-side `custom-<timestamp>` id the catalog never knew, so the wizard's
 * first step (preflight) answered 404 "Unknown system_id".
 *
 * PostgreSQL's connection string carries the password: it rides ONLY in
 * connection_config.dsn (which the server routes to Secrets Manager), never as
 * the catalog-visible connection_string, and the wizard state keeps no copy of
 * it (the server resolves the registered connection itself). */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { ConnectNewValues } from '../views/onboard/catalog';
import { catalogStore, setConnectNewOpen } from '../views/onboard/catalog-store';

const obGuardStart = vi.fn();
vi.mock('../views/onboard/flow', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../views/onboard/flow')>();
  return { ...actual, obGuardStart: (...args: unknown[]) => obGuardStart(...args) };
});

function values(patch: Partial<ConnectNewValues>): ConnectNewValues {
  return {
    connType: 'postgresql',
    name: 'Quality Lab',
    endpoint: '',
    athena: { workgroup: '', database: '', catalog: '', region: '', output: '', roleArn: '', externalId: '' },
    sitewise: { region: '', roleArn: '', externalId: '' },
    i3x: { endpoint: '', token: '' },
    tulip: { baseUrl: '', workspaceId: '', apiKey: '', apiSecret: '' },
    ...patch,
  };
}

function json(body: unknown, status = 200): Response {
  const text = JSON.stringify(body);
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: 'OK',
    headers: new Headers({ 'content-type': 'application/json' }),
    json: async () => body,
    text: async () => text,
  } as unknown as Response;
}

const DSN = 'postgresql://lab_reader:s3cr3t-pw@db.example.internal:5432/lims?options=-csearch_path%3Dlims';

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  obGuardStart.mockReset();
  fetchMock = vi.fn().mockResolvedValue(json({ status: 'created', system_id: 'quality-lab' }));
  vi.stubGlobal('fetch', fetchMock);
  setConnectNewOpen(true);
});

afterEach(() => {
  setConnectNewOpen(false);
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

async function connect(v: ConnectNewValues): Promise<void> {
  const { startCustomOnboard } = await import('../views/onboard/catalog');
  await startCustomOnboard(v);
}

function onlyCall(): { url: string; body: Record<string, unknown> } {
  expect(fetchMock).toHaveBeenCalledTimes(1);
  const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
  return { url, body: JSON.parse(String(init.body)) as Record<string, unknown> };
}

describe('Connect New System registers before the wizard starts', () => {
  it('PostgreSQL: registers via /connect with the DSN only in connection_config.dsn', async () => {
    await connect(values({ connType: 'postgresql', endpoint: DSN }));
    const { url, body } = onlyCall();
    expect(url).toBe('/api/onboard/connect');
    expect(body).toEqual({
      name: 'Quality Lab',
      system_type: 'Custom',
      connection_type: 'PostgreSQL',
      schema: 'lims',
      connection_config: { dsn: DSN },
    });
    expect(obGuardStart).toHaveBeenCalledTimes(1);
    const sys = obGuardStart.mock.calls[0][0] as Record<string, unknown>;
    expect(sys).toMatchObject({
      id: 'quality-lab',
      connection_type: 'PostgreSQL',
      schema: 'lims',
      status: 'registered',
    });
    // The password never lingers in wizard state: no DSN copy anywhere on it.
    expect(JSON.stringify(sys)).not.toContain('s3cr3t-pw');
    expect(catalogStore.get().connectNewOpen).toBe(false);
  });

  it.each([
    ['mcp-server', 'MCP Server', 'https://mcp.example.com/mcp'],
  ])('%s: registers via /connect with the URL as connection_string', async (connType, label, url) => {
    await connect(values({ connType, endpoint: url }));
    const { url: called, body } = onlyCall();
    expect(called).toBe('/api/onboard/connect');
    expect(body).toEqual({
      name: 'Quality Lab',
      system_type: 'Custom',
      connection_type: label,
      connection_string: url,
    });
    expect(obGuardStart).toHaveBeenCalledTimes(1);
    expect(obGuardStart.mock.calls[0][0]).toMatchObject({
      id: 'quality-lab',
      connection_type: label,
      connection_string: url,
      status: 'registered',
    });
  });

  it.each([
    ['postgresql', 'PostgreSQL', DSN],
    ['mcp-server', 'MCP Server', 'https://mcp.example.com/mcp'],
  ])('%s: a register {error} keeps the form open and starts nothing', async (connType, label, endpoint) => {
    fetchMock.mockResolvedValue(json({ error: 'System quality-lab already exists' }));
    await connect(values({ connType, endpoint }));
    expect(catalogStore.get().connectNewOpen).toBe(true);
    expect(catalogStore.get().connectNewError).toBe(
      `Failed to register ${label} system: System quality-lab already exists`,
    );
    expect(obGuardStart).not.toHaveBeenCalled();
  });

  it('MCP Server with a token: the token rides only in connection_config.token', async () => {
    const token = 'mcp-tok-SECRET-42';
    await connect(values({ connType: 'mcp-server', endpoint: 'https://mcp.example.com/mcp', mcpToken: `  ${token} ` }));
    const { body } = onlyCall();
    expect(body).toEqual({
      name: 'Quality Lab',
      system_type: 'Custom',
      connection_type: 'MCP Server',
      connection_string: 'https://mcp.example.com/mcp',
      connection_config: { token },
    });
    // Never echoed into the wizard's state.
    const sys = obGuardStart.mock.calls[0][0] as Record<string, unknown>;
    expect(JSON.stringify(sys)).not.toContain(token);
  });

  it('MCP Server with a blank token sends exactly what it sent before', async () => {
    await connect(values({ connType: 'mcp-server', endpoint: 'https://mcp.example.com/mcp', mcpToken: '   ' }));
    const { body } = onlyCall();
    expect(body).not.toHaveProperty('connection_config');
  });
});

describe('REST API (OpenAPI) is not offered until it can be provisioned', () => {
  it('a rest-api submit is refused inline and sends nothing', async () => {
    await connect(values({ connType: 'rest-api', endpoint: 'https://api.example.com/openapi.json' }));
    expect(fetchMock).not.toHaveBeenCalled();
    expect(obGuardStart).not.toHaveBeenCalled();
    expect(catalogStore.get().connectNewOpen).toBe(true);
    expect(catalogStore.get().connectNewError).toMatch(/^REST API \(OpenAPI\) systems are not supported yet/);
  });

  it('the Connection Type option is disabled and says so', async () => {
    const { ObModalLayer } = await import('../views/onboard/components/ObModals');
    const { createElement } = await import('react');
    const { renderToStaticMarkup } = await import('react-dom/server');
    const html = renderToStaticMarkup(createElement(ObModalLayer));
    expect(html).toMatch(/<option value="rest-api" disabled="">REST API \(OpenAPI\)[^<]*not supported yet<\/option>/);
  });
});
