// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
/* System-removal protection tests:
 * 1. The removal modal's danger button stays DISABLED until the user types the
 *    exact system id (typed-confirmation guard — the cascade deletes the
 *    system's graph node, edges, runtime, and catalog entry).
 * 2. Confirming starts the ASYNC reset job (?wait=false) — never the
 *    synchronous variant, which can outlive the CloudFront 120 s origin timeout.
 * 3. While a removal job is running, the system card renders a disabled
 *    "Removing…" action instead of Remove/Onboard, so a re-onboard cannot race
 *    a still-DELETING runtime. */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { flowStore, openModal } from '../views/onboard/flow-store';
import { removingStore } from '../views/onboard/flow';
import { ObModals } from '../views/onboard/components/ObModals';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;

function response(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: 'OK',
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as Response;
}

async function mountModals() {
  await act(async () => {
    root.render(<ObModals />);
  });
}

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  removingStore.set({ ids: {}, errors: {} });
});

afterEach(async () => {
  await act(async () => {
    root.unmount();
  });
  container.remove();
  flowStore.update((s) => ({ ...s, modal: null }));
  vi.restoreAllMocks();
});

describe('system removal — typed confirmation guard', () => {
  it('keeps the Remove button disabled until the exact system id is typed', async () => {
    await mountModals();
    act(() => {
      openModal({ kind: 'disconnect', systemId: 'mes-1', systemName: 'Plant MES' });
    });
    await act(async () => {});

    const btn = document.getElementById('ob-disc-yes') as HTMLButtonElement;
    const input = document.getElementById('ob-disc-confirm-input') as HTMLInputElement;
    expect(btn).toBeTruthy();
    expect(input).toBeTruthy();
    expect(btn.disabled).toBe(true);

    // Wrong text — still disabled.
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(
        window.HTMLInputElement.prototype, 'value')!.set!;
      setter.call(input, 'mes');
      input.dispatchEvent(new Event('input', { bubbles: true }));
    });
    expect(btn.disabled).toBe(true);

    // Exact id — armed.
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(
        window.HTMLInputElement.prototype, 'value')!.set!;
      setter.call(input, 'mes-1');
      input.dispatchEvent(new Event('input', { bubbles: true }));
    });
    expect(btn.disabled).toBe(false);
  });

  it('confirm starts the ASYNC reset job and marks the system as removing', async () => {
    const fetchMock = vi.fn(async () => response({ status: 'started', system_id: 'mes-1' }));
    vi.stubGlobal('fetch', fetchMock);

    await mountModals();
    act(() => {
      openModal({ kind: 'disconnect', systemId: 'mes-1', systemName: 'Plant MES' });
    });
    await act(async () => {});

    const input = document.getElementById('ob-disc-confirm-input') as HTMLInputElement;
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(
        window.HTMLInputElement.prototype, 'value')!.set!;
      setter.call(input, 'mes-1');
      input.dispatchEvent(new Event('input', { bubbles: true }));
    });
    const btn = document.getElementById('ob-disc-yes') as HTMLButtonElement;
    await act(async () => {
      btn.click();
    });

    const calls = fetchMock.mock.calls as unknown as [string, RequestInit?][];
    const calledUrl = String(calls[0]?.[0] ?? '');
    expect(calledUrl).toContain('/api/onboard/reset/mes-1');
    expect(calledUrl).toContain('wait=false');
    expect(removingStore.get().ids['mes-1']).toBe('running');
    // Modal closed on job start.
    expect(flowStore.get().modal).toBeNull();
  });
});
