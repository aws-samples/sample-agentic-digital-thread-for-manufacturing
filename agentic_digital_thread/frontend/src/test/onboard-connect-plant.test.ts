// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
/* The plant a Connect New System registration carries.
 *
 * Every type used to hand the wizard the placeholder plant 'Custom', and the
 * wizard sent it to provision. Provision writes it onto the System node and
 * retires competing authority claims of every other system at the SAME plant.
 * So a second CMMS system retired the first one's CMMS
 * authority rules: both sat at 'Custom', although they serve different sites.
 *
 * Now the form has one optional Plant field for every type. A typed plant rides
 * in the register body (so the catalog entry and card onboarding see it) and
 * into the wizard; a blank one is no plant at all, never a placeholder. */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { ConnectNewValues } from '../views/onboard/catalog';
import { catalogStore, setConnectNewOpen } from '../views/onboard/catalog-store';

const obGuardStart = vi.fn();
vi.mock('../views/onboard/flow', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../views/onboard/flow')>();
  return { ...actual, obGuardStart: (...args: unknown[]) => obGuardStart(...args) };
});

const DSN = 'postgresql://lab_reader:pw@db.example.com:5432/lims?options=-csearch_path%3Dlims';

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

// One valid form per connection type the form offers.
const TYPES: [string, Partial<ConnectNewValues>][] = [
  ['postgresql', { connType: 'postgresql', endpoint: DSN }],
  ['mcp-server', { connType: 'mcp-server', endpoint: 'https://mcp.example.com/mcp' }],
  [
    'athena',
    {
      connType: 'athena',
      athena: {
        workgroup: 'primary', database: 'lab', catalog: '', region: '', output: '',
        roleArn: 'arn:aws:iam::111122223333:role/adt-data-readonly', externalId: '',
      },
    },
  ],
  ['sitewise-direct', { connType: 'sitewise-direct' }],
  ['sitewise-i3x', { connType: 'sitewise-i3x', i3x: { endpoint: 'https://i3x.example.com', token: '' } }],
  ['tulip', { connType: 'tulip', tulip: { baseUrl: 'https://tulip.example.com', workspaceId: '', apiKey: '', apiSecret: '' } }],
];

function json(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    headers: new Headers({ 'content-type': 'application/json' }),
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as unknown as Response;
}

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

async function connect(v: ConnectNewValues): Promise<{ body: Record<string, unknown>; sys: Record<string, unknown> }> {
  const { startCustomOnboard } = await import('../views/onboard/catalog');
  await startCustomOnboard(v);
  expect(fetchMock).toHaveBeenCalledTimes(1);
  const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
  expect(obGuardStart).toHaveBeenCalledTimes(1);
  return {
    body: JSON.parse(String(init.body)) as Record<string, unknown>,
    sys: obGuardStart.mock.calls[0][0] as Record<string, unknown>,
  };
}

describe('Connect New System: the plant the system is registered with', () => {
  it.each(TYPES)('%s: a typed plant rides in the register body and into the wizard', async (_t, patch) => {
    const { body, sys } = await connect(values({ ...patch, plant: '  North Plant ' }));
    expect(body.plant).toBe('North Plant');
    expect(sys.plant).toBe('North Plant');
  });

  it.each(TYPES)('%s: a blank plant is no plant, never the placeholder', async (_t, patch) => {
    const { body, sys } = await connect(values({ ...patch, plant: '   ' }));
    // Byte-identical register body to before the field existed.
    expect(body).not.toHaveProperty('plant');
    expect(sys.plant).toBe('');
  });

  it('Tulip: the wizard copy carries the same system_type the server registered', async () => {
    const { body, sys } = await connect(values(TYPES.find(([t]) => t === 'tulip')![1]));
    expect(body.system_type).toBe('MES');
    expect(sys.system_type).toBe(body.system_type);
  });

  it('a form without the field at all behaves as blank', async () => {
    const { body, sys } = await connect(values({ connType: 'mcp-server', endpoint: 'https://mcp.example.com/mcp' }));
    expect(body).not.toHaveProperty('plant');
    expect(sys.plant).toBe('');
  });

  it.each(['Custom', ' custom ', 'CUSTOM'])(
    'refuses the retired placeholder %j and sends nothing',
    async (plant) => {
      const { startCustomOnboard } = await import('../views/onboard/catalog');
      await startCustomOnboard(values({ connType: 'mcp-server', endpoint: 'https://mcp.example.com/mcp', plant }));
      expect(fetchMock).not.toHaveBeenCalled();
      expect(obGuardStart).not.toHaveBeenCalled();
      expect(catalogStore.get().connectNewOpen).toBe(true);
      expect(catalogStore.get().connectNewError).toBe(
        "Plant can't be 'Custom'. Leave it blank if the system isn't tied to one site.",
      );
    },
  );
});
