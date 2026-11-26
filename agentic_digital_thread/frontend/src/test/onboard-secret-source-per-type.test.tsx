// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
/* The Connect New System credential choice is offered per connection type.
 *
 * /api/auth/me lists the types this deployment lets take a named Secrets
 * Manager secret (secret_arn_source_types). Each form shows the "Credentials"
 * choice only when its own type is listed. A server that sends only the flag
 * (secret_arn_source_enabled) offers it for Tulip alone, its original
 * meaning. */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { catalogStore, setConnectNewOpen } from '../views/onboard/catalog-store';
import { ObModals } from '../views/onboard/components/ObModals';
import { ob, obCheckAdmin, secretArnSourceOffered } from '../views/onboard/state';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const TULIP = 'Tulip (Tables API)';
const OTHER = 'Other Type';

let container: HTMLDivElement;
let root: Root | null = null;

async function mount(): Promise<void> {
  container = document.createElement('div');
  document.body.appendChild(container);
  const created = createRoot(container);
  root = created;
  setConnectNewOpen(true);
  await act(async () => {
    created.render(<ObModals />);
  });
}

async function choose(connType: string): Promise<void> {
  const select = document.getElementById('new-conn-type') as HTMLSelectElement;
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value')!.set!;
    setter.call(select, connType);
    select.dispatchEvent(new Event('change', { bubbles: true }));
  });
}

async function choosers(): Promise<{ tulip: boolean }> {
  await choose('tulip');
  const tulip = document.getElementById('tulip-cred-source') !== null;
  return { tulip };
}

function offer(types: string[] | null, enabled = true): void {
  ob.secretArnSourceEnabled = enabled;
  ob.secretArnSourceTypes = types;
}

afterEach(async () => {
  const mounted = root;
  if (mounted) {
    await act(async () => {
      mounted.unmount();
    });
    container.remove();
    root = null;
  }
  setConnectNewOpen(false);
  offer(null, false);
  ob.adminChecked = false;
  catalogStore.set({ ...catalogStore.get(), connectNewError: null });
});

describe('Connect New System — credential choice per connection type', () => {
  it('a build that lists Tulip offers it on the Tulip form', async () => {
    offer([TULIP]);
    await mount();
    expect(await choosers()).toEqual({ tulip: true });
  });

  it('a build that lists only another type does not offer it on Tulip', async () => {
    offer([OTHER]);
    await mount();
    expect(await choosers()).toEqual({ tulip: false });
  });

  it('a server that sends only the flag offers it for Tulip alone', async () => {
    offer(null, true);
    await mount();
    expect(await choosers()).toEqual({ tulip: true });
  });

  it('nothing is offered when the deployment has not enabled the source', async () => {
    offer([], false);
    await mount();
    expect(await choosers()).toEqual({ tulip: false });
  });
});

describe('secretArnSourceOffered', () => {
  it('reads the listed types', () => {
    offer([OTHER]);
    expect(secretArnSourceOffered(OTHER)).toBe(true);
    expect(secretArnSourceOffered(TULIP)).toBe(false);
  });

  it('keeps the list /api/auth/me sends', async () => {
    const body = {
      is_admin: true,
      secret_arn_source_enabled: true,
      secret_arn_source_types: [OTHER],
    };
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        new Response(JSON.stringify(body), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      ),
    );
    try {
      await obCheckAdmin();
    } finally {
      vi.unstubAllGlobals();
    }
    expect(ob.secretArnSourceTypes).toEqual([OTHER]);
    expect(secretArnSourceOffered(OTHER)).toBe(true);
    expect(secretArnSourceOffered(TULIP)).toBe(false);
  });
});
