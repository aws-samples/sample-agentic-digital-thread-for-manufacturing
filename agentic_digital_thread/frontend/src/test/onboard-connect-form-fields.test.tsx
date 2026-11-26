// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
/* What the Connect New System form tells the user, per connection type.
 * Athena: no app or runtime role of a deployed ADT can query Athena, so the
 * form must say a read-only role is required and how to get one (the server
 * refuses a blank role with the same instructions), not that the role is
 * optional. */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { catalogStore, setConnectNewOpen } from '../views/onboard/catalog-store';
import { ObModals } from '../views/onboard/components/ObModals';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;

beforeEach(async () => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  setConnectNewOpen(true);
  await act(async () => {
    root.render(<ObModals />);
  });
});

afterEach(async () => {
  await act(async () => {
    root.unmount();
  });
  container.remove();
  setConnectNewOpen(false);
});

async function chooseType(value: string): Promise<void> {
  const select = document.getElementById('new-conn-type') as HTMLSelectElement;
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value')!.set!;
    setter.call(select, value);
    select.dispatchEvent(new Event('change', { bubbles: true }));
  });
}

async function typeInto(id: string, value: string): Promise<void> {
  const input = document.getElementById(id) as HTMLInputElement;
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!;
    setter.call(input, value);
    input.dispatchEvent(new Event('input', { bubbles: true }));
  });
}

describe('Connect New System — Athena', () => {
  it('says a read-only role is required and how to get one', async () => {
    await chooseType('athena');
    const text = document.getElementById('conn-details')!.textContent || '';
    expect(text).toContain('A read-only IAM role is required');
    expect(text).toContain('stacks/cross-account/adt-data-readonly-role.yaml');
    expect(text).toContain('--cross-account-role convention');
    expect(text).toContain('TrustedAccountId');
    expect(text).toContain('DataAccountId defaults to the account you deploy the role in');
    expect(text).toContain('set S3TablesAccountId only when your tables are in Amazon S3 Tables');
    expect(text).toContain('set DataBucketName for a plain Amazon S3 source; left empty, the role gets no plain S3 access');
    expect(text).not.toMatch(/leave blank to use the app.s own/i);
    expect(text).toContain('Read-Only Role ARN *');
  });
});

describe('Connect New System — Plant', () => {
  it.each(['postgresql', 'mcp-server', 'athena', 'sitewise-direct', 'sitewise-i3x', 'tulip'])(
    '%s: offers one optional Plant field that keeps what was typed',
    async (type) => {
      await chooseType(type);
      const plant = document.getElementById('new-conn-plant') as HTMLInputElement | null;
      expect(plant).not.toBeNull();
      const label = plant!.closest('div')!.querySelector('label')!.textContent || '';
      expect(label).toBe('Plant (optional)');
      await typeInto('new-conn-plant', 'North Plant');
      expect((document.getElementById('new-conn-plant') as HTMLInputElement).value).toBe('North Plant');
    },
  );
});

describe('Connect New System — MCP Server', () => {
  it('a registration the server refuses keeps the form open with the URL and token', async () => {
    const fetchMock = vi.fn(async () => ({
      ok: true,
      status: 200,
      statusText: 'OK',
      headers: new Headers({ 'content-type': 'application/json' }),
      json: async () => ({
        error:
          'The connection credentials could not be stored in Secrets Manager, so the system was not registered. Try again.',
      }),
      text: async () => '',
    }));
    vi.stubGlobal('fetch', fetchMock);
    await chooseType('mcp-server');
    await typeInto('new-conn-name', 'Quality Lab');
    await typeInto('new-conn-endpoint', 'https://mcp.example.com/mcp');
    await typeInto('new-conn-token', 'tok-SECRET-1');
    const submit = Array.from(document.querySelectorAll('button')).find((b) =>
      (b.textContent || '').includes('Discover Schema'),
    )!;
    await act(async () => {
      submit.click();
    });
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    await vi.waitFor(() =>
      expect(document.querySelector('[role="alert"]')?.textContent || '').toContain(
        'could not be stored in Secrets Manager',
      ),
    );
    expect(catalogStore.get().connectNewOpen).toBe(true);
    expect((document.getElementById('new-conn-token') as HTMLInputElement).value).toBe('tok-SECRET-1');
    expect((document.getElementById('new-conn-endpoint') as HTMLInputElement).value).toBe(
      'https://mcp.example.com/mcp',
    );
    vi.unstubAllGlobals();
  });

  it('offers an optional token as a password field and says how it is sent', async () => {
    await chooseType('mcp-server');
    const token = document.getElementById('new-conn-token') as HTMLInputElement | null;
    expect(token).not.toBeNull();
    expect(token!.type).toBe('password');
    const text = document.getElementById('conn-details')!.textContent || '';
    expect(text).toContain('Token (optional)');
    expect(text).toContain('Authorization: Bearer');
    expect(text).toContain('never shown again');
  });
});
