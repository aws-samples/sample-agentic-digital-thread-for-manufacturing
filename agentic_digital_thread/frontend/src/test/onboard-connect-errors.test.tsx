// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
/* Connect New System (views/onboard/catalog.ts startCustomOnboard) never
 * uses the native alert() and never throws the form away on a problem: the
 * form stays open, keeping every typed value, until registration succeeds.
 * A missing required field or a failed registration is
 * reported INLINE in the form (catalog-store connectNewError, rendered as a
 * role=alert paragraph by ObModals) and persists until the next attempt; a
 * transient toast was too easy to miss once the form had closed. */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { clear as clearToasts, toastStore } from '../shared/ui/toast-store';
import type { ConnectNewValues } from '../views/onboard/catalog';
import { catalogStore, setConnectNewOpen } from '../views/onboard/catalog-store';

const obGuardStart = vi.fn();
vi.mock('../views/onboard/flow', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../views/onboard/flow')>();
  return { ...actual, obGuardStart: (...args: unknown[]) => obGuardStart(...args) };
});

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

function values(patch: Partial<ConnectNewValues>): ConnectNewValues {
  return {
    connType: 'postgresql',
    name: 'Test System',
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

const ATHENA_OK = values({
  connType: 'athena',
  athena: { ...values({}).athena, workgroup: 'primary', database: 'mes' },
});
const PG_OK = values({
  connType: 'postgresql',
  endpoint: 'postgresql://db.example.internal:5432/mes?options=-c%20search_path%3Dmes',
});
let nativeAlert: ReturnType<typeof vi.fn>;
let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  clearToasts();
  obGuardStart.mockReset();
  nativeAlert = vi.fn();
  vi.stubGlobal('alert', nativeAlert);
  fetchMock = vi.fn();
  vi.stubGlobal('fetch', fetchMock);
  setConnectNewOpen(true);
});

afterEach(() => {
  setConnectNewOpen(false);
  clearToasts();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

async function connect(v: ConnectNewValues): Promise<void> {
  const { startCustomOnboard } = await import('../views/onboard/catalog');
  await startCustomOnboard(v);
}

const state = () => catalogStore.get();

describe('Connect New System — validation stops inline and sends nothing', () => {
  it.each([
    [values({ connType: 'athena' }), 'Athena workgroup is required'],
    [values({ connType: 'athena', athena: { ...values({}).athena, workgroup: 'primary' } }), 'Athena database/schema is required'],
    [values({ connType: 'sitewise-i3x' }), 'i3X endpoint URL is required'],
    [values({ connType: 'tulip' }), 'Tulip base URL is required'],
    [values({ connType: 'postgresql' }), 'Enter a connection endpoint'],
    [values({ connType: 'postgresql', endpoint: 'postgresql://db.example.internal:5432/mes' }), 'Connection string must include search_path parameter'],
  ])('%#: %s', async (v, message) => {
    await connect(v);
    expect(state().connectNewOpen).toBe(true);
    expect(state().connectNewError).toBe(message);
    expect(state().connectNewBusy).toBe(false);
    expect(fetchMock).not.toHaveBeenCalled();
    expect(toastStore.get().toasts).toEqual([]);
    expect(nativeAlert).not.toHaveBeenCalled();
  });
});

describe('Connect New System — a failed request keeps the form open with the reason', () => {
  it('a register response carrying {error} names the system type and the detail', async () => {
    fetchMock.mockResolvedValue(json({ error: 'AccessDenied: not authorized' }));
    await connect(ATHENA_OK);
    expect(state().connectNewOpen).toBe(true);
    expect(state().connectNewError).toBe('Failed to register Athena system: AccessDenied: not authorized');
    expect(state().connectNewBusy).toBe(false);
    expect(obGuardStart).not.toHaveBeenCalled();
    expect(nativeAlert).not.toHaveBeenCalled();
  });

  it('a thrown register request surfaces its message', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    fetchMock.mockRejectedValue(new Error('network down'));
    await connect(values({ connType: 'tulip', tulip: { baseUrl: 'https://tulip.example.com', workspaceId: '', apiKey: '', apiSecret: '' } }));
    expect(state().connectNewOpen).toBe(true);
    expect(state().connectNewError).toMatch(/^Failed to register Tulip system: /);
    expect(state().connectNewError).toContain('network down');
  });

  it('a failed PostgreSQL registration says so', async () => {
    fetchMock.mockResolvedValue(json({ error: 'secret write denied' }));
    await connect(PG_OK);
    expect(state().connectNewOpen).toBe(true);
    expect(state().connectNewError).toBe('Failed to register PostgreSQL system: secret write denied');
  });
});

describe('Connect New System — success closes the form and starts onboarding', () => {
  it('stays open and busy while the request is in flight, then closes', async () => {
    let finish!: (r: Response) => void;
    fetchMock.mockReturnValue(new Promise<Response>((resolve) => (finish = resolve)));
    const pending = connect(ATHENA_OK);
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(state().connectNewOpen).toBe(true);
    expect(state().connectNewBusy).toBe(true);
    // A second submit while busy is ignored (no second registration).
    await connect(ATHENA_OK);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    finish(json({ system_id: 'sys-1' }));
    await pending;
    expect(state().connectNewOpen).toBe(false);
    expect(obGuardStart).toHaveBeenCalledTimes(1);
    expect(obGuardStart.mock.calls[0][0]).toMatchObject({ id: 'sys-1', connection_type: 'Athena' });
  });

  it('a new attempt clears the previous reason', async () => {
    await connect(values({ connType: 'athena' }));
    expect(state().connectNewError).toBe('Athena workgroup is required');
    fetchMock.mockResolvedValue(json({ status: 'created', system_id: 'test-system' }));
    await connect(PG_OK);
    expect(state().connectNewError).toBeNull();
    expect(state().connectNewOpen).toBe(false);
    expect(obGuardStart).toHaveBeenCalledTimes(1);
  });
});

describe('Connect New System — closing mid-request abandons that attempt', () => {
  /** fetch replies resolved by hand, in call order. */
  function deferredFetch() {
    const pending: Array<(r: Response) => void> = [];
    const rejecters: Array<(e: Error) => void> = [];
    fetchMock.mockImplementation(
      () =>
        new Promise<Response>((resolve, reject) => {
          pending.push(resolve);
          rejecters.push(reject);
        }),
    );
    return { pending, rejecters };
  }

  it("an old attempt's late success neither closes the reopened form nor starts onboarding", async () => {
    const { pending } = deferredFetch();
    const first = connect(ATHENA_OK);
    await vi.waitFor(() => expect(pending).toHaveLength(1));
    setConnectNewOpen(false); // the user closes the form mid-request
    setConnectNewOpen(true); // ...reopens it
    expect(state().connectNewBusy).toBe(false);
    const second = connect(values({ ...ATHENA_OK, name: 'Second' }));
    await vi.waitFor(() => expect(pending).toHaveLength(2));
    expect(state().connectNewBusy).toBe(true);

    pending[0](json({ system_id: 'sys-old' }));
    await first;
    expect(state().connectNewOpen).toBe(true); // the new form is untouched
    expect(state().connectNewBusy).toBe(true);
    expect(obGuardStart).not.toHaveBeenCalled();

    pending[1](json({ system_id: 'sys-new' }));
    await second;
    expect(state().connectNewOpen).toBe(false);
    expect(obGuardStart).toHaveBeenCalledTimes(1);
    expect(obGuardStart.mock.calls[0][0]).toMatchObject({ id: 'sys-new', name: 'Second' });
  });

  it("an old attempt's late failure does not write its error into the reopened form", async () => {
    const { pending } = deferredFetch();
    const first = connect(ATHENA_OK);
    await vi.waitFor(() => expect(pending).toHaveLength(1));
    setConnectNewOpen(false);
    setConnectNewOpen(true);
    pending[0](json({ error: 'stale failure' }));
    await first;
    expect(state().connectNewOpen).toBe(true);
    expect(state().connectNewError).toBeNull();
    expect(state().connectNewBusy).toBe(false);
  });

  it('a closed form stays closed when its attempt later fails or succeeds', async () => {
    const { pending, rejecters } = deferredFetch();
    const first = connect(ATHENA_OK);
    await vi.waitFor(() => expect(pending).toHaveLength(1));
    setConnectNewOpen(false);
    vi.spyOn(console, 'error').mockImplementation(() => {});
    rejecters[0](new Error('network down'));
    await first;
    expect(state().connectNewOpen).toBe(false);
    expect(state().connectNewError).toBeNull();
    expect(obGuardStart).not.toHaveBeenCalled();
  });
});

describe('<ObModals/> renders the reason inline, keeps the values, and shows busy', () => {
  let container: HTMLDivElement;
  let root: Root;
  afterEach(() => {
    act(() => root.unmount());
    container.remove();
  });

  it('shows the reason as an alert under the form without clearing typed values', async () => {
    const { ObModals } = await import('../views/onboard/components/ObModals');
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
    act(() => root.render(<ObModals />));
    const nameInput = document.body.querySelector<HTMLInputElement>('input')!;
    expect(nameInput).not.toBeNull();
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!;
    act(() => {
      setter.call(nameInput, 'Plant MES');
      nameInput.dispatchEvent(new Event('input', { bubbles: true }));
    });
    const submit = [...document.body.querySelectorAll('button')].find((b) =>
      b.textContent?.includes('Discover Schema'),
    )!;
    await act(async () => {
      submit.click();
    });
    const alert = document.body.querySelector('.connect-new-error[role="alert"]');
    expect(alert?.textContent).toBe('Enter a connection endpoint');
    expect(document.body.querySelector<HTMLInputElement>('input')!.value).toBe('Plant MES');
  });
});
