// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
/* Edit connection for a system that MAY take its credentials from a Secrets
 * Manager secret: the form never shows the inline key editor until the server
 * has said where the credentials come from. While that is unknown (the summary
 * is loading or failed) no field is editable and Save is disabled; a secret
 * marker from EITHER the catalog card or the summary picks the read-only form. */
import { QueryClientProvider } from '@tanstack/react-query';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { createQueryClient } from '../api/query-client';
import type { CatalogSystem } from '../api/types/onboard';
import { EditConnectionForm } from '../views/onboard/components/CatalogList';
import { ob } from '../views/onboard/state';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;
const savedFlag = ob.secretArnSourceEnabled;

const ARN = 'arn:aws:secretsmanager:us-east-1:111122223333:secret:example/conn-AbCdEf';
const TULIP: CatalogSystem = {
  id: 'mes-tulip',
  name: 'Tulip MES',
  connection_type: 'Tulip (Tables API)',
  status: 'onboarded',
  connection_config: { base_url: 'https://mes.example.com' },
};

function ok(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    headers: new Headers({ 'content-type': 'application/json' }),
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as Response;
}

function failing(): Response {
  return {
    ok: false,
    status: 500,
    statusText: 'Internal Server Error',
    headers: new Headers({ 'content-type': 'application/json' }),
    json: async () => ({ status: 'error', detail: 'unreadable' }),
    text: async () => '{"status":"error","detail":"unreadable"}',
  } as Response;
}

/** Answer the current-connection GET with *answer* (a body, 'fail', or a
 * promise that never settles for 'pending'). Other requests get {}. */
function stubSummary(answer: Record<string, unknown> | 'fail' | 'pending') {
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string) => {
      if (!String(url).includes('/api/admin/connection/')) return Promise.resolve(ok({}));
      if (answer === 'pending') return new Promise<Response>(() => {});
      if (answer === 'fail') return Promise.resolve(failing());
      return Promise.resolve(ok(answer));
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

const editable = () =>
  Array.from(container.querySelectorAll<HTMLInputElement>('input[data-field]')).filter(
    (i) => !i.readOnly,
  );
const fieldKeys = () =>
  Array.from(container.querySelectorAll<HTMLElement>('[data-field]')).map((i) => i.dataset.field);
const saveButton = () =>
  Array.from(container.querySelectorAll('button')).find((b) =>
    (b.textContent || '').includes('Save'),
  ) as HTMLButtonElement;

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  ob.secretArnSourceEnabled = true;
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  ob.secretArnSourceEnabled = savedFlag;
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('Edit connection: credential source not yet known', () => {
  it('shows no editable field and disables Save while the summary is loading', async () => {
    stubSummary('pending');
    await mount(TULIP);
    expect(editable()).toEqual([]);
    expect(fieldKeys()).not.toContain('api_key');
    expect(saveButton().disabled).toBe(true);
    expect(container.textContent).toContain('Checking where this connection');
  });

  it('stays locked with a message when the summary fails', async () => {
    stubSummary('fail');
    await mount(TULIP);
    await vi.waitFor(() => expect(container.textContent).toContain("Couldn't load"));
    expect(editable()).toEqual([]);
    expect(saveButton().disabled).toBe(true);
  });

  it('keeps the read-only form when the card is marked but the summary is blank', async () => {
    stubSummary({ system_id: 'mes-tulip', credential_source: '' });
    await mount({
      ...TULIP,
      connection_config: { base_url: 'https://mes.example.com', credential_source: 'secret_arn' },
      secret_arn: ARN,
    });
    await vi.waitFor(() => expect(fieldKeys()).toContain('secret_arn'));
    expect(editable()).toEqual([]);
    expect(fieldKeys()).not.toContain('api_key');
  });

  it('switches to the read-only form when the summary reports the secret source', async () => {
    stubSummary({ system_id: 'mes-tulip', credential_source: 'secret_arn', secret_arn: ARN });
    await mount(TULIP);
    await vi.waitFor(() => expect(fieldKeys()).toContain('secret_arn'));
    expect(editable()).toEqual([]);
    expect(saveButton().disabled).toBe(false);
  });

  it('shows the inline editor once the summary answers with no secret source', async () => {
    stubSummary({
      system_id: 'mes-tulip',
      connection_type: 'Tulip (Tables API)',
      base_url: 'https://mes.example.com',
      credential_source: '',
    });
    await mount(TULIP);
    await vi.waitFor(() => expect(fieldKeys()).toContain('api_key'));
    expect(saveButton().disabled).toBe(false);
  });

  it('leaves other connectors and a deploy without the option unchanged', async () => {
    ob.secretArnSourceEnabled = false;
    stubSummary('pending');
    await mount(TULIP);
    expect(fieldKeys()).toContain('api_key');
    expect(saveButton().disabled).toBe(false);
  });
});
