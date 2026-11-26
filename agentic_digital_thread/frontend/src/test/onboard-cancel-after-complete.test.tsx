// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
/* The wizard's Cancel button after the run is past its provision: the
 * "Cancel onboarding? ... in progress" prompt is for a run still working.
 * A finished wizard (or one parked on a failed step's Retry) has nothing to
 * cancel, so Cancel puts it away at once, the same rule obGuardStart uses.
 * Cancel stays visible: it is the way back to the catalog. Walk 2026-11-12,
 * Team 2: "Onboarding complete" still asked to cancel an onboarding in
 * progress. */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../views/onboard/onboard-queries', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../views/onboard/onboard-queries')>()),
  invalidateOnboardingRemap: vi.fn(async () => {}),
  useConceptsQuery: () => ({ data: [], isError: false, refetch: vi.fn() }),
}));

import { ObFlow } from '../views/onboard/components/ObFlow';
import {
  closeModal,
  flowStore,
  patchProgress,
  resetToWelcome,
  startWizard,
} from '../views/onboard/flow-store';
import { ob } from '../views/onboard/state';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;

function runOn(systemId: string, state: { waitingOnRetry?: boolean; provisioned?: boolean }) {
  const controller = new AbortController();
  ob.active = systemId;
  ob.abort = controller;
  ob.currentStep = { step: 'provision', systemId, data: {}, history: [] };
  ob.waitingOnRetry = state.waitingOnRetry ?? false;
  ob.provisioned = state.provisioned ?? false;
  return controller;
}

function cancelButton(): HTMLButtonElement | null {
  return container.querySelector<HTMLButtonElement>('.ob-cancel-btn');
}

beforeEach(() => {
  closeModal();
  resetToWelcome();
  ob.active = null;
  ob.abort = null;
  ob.currentStep = null;
  ob.waitingOnRetry = false;
  ob.provisioned = false;
  // Putting the wizard away reloads the catalog: answer it with no systems.
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => new Response('[]', { status: 200, headers: { 'Content-Type': 'application/json' } })),
  );
  startWizard();
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  closeModal();
  resetToWelcome();
  ob.active = null;
  ob.abort = null;
  ob.currentStep = null;
  ob.waitingOnRetry = false;
  ob.provisioned = false;
  vi.unstubAllGlobals();
});

describe('wizard Cancel', () => {
  it('asks before cancelling a run that is still working', () => {
    const controller = runOn('sys-a', {});
    act(() => root.render(<ObFlow />));

    act(() => cancelButton()!.click());

    expect(flowStore.get().modal).toMatchObject({ kind: 'cancel' });
    expect(controller.signal.aborted).toBe(false);
    expect(ob.active).toBe('sys-a');
  });

  it('closes a finished wizard without the "in progress" prompt', () => {
    const controller = runOn('sys-a', { provisioned: true });
    act(() => patchProgress({ activeIdx: 5, allDone: true, statusText: '✓ Onboarding complete' }));
    act(() => root.render(<ObFlow />));

    // Still visible: Cancel is the way back to the catalog.
    expect(cancelButton()).not.toBeNull();
    act(() => cancelButton()!.click());

    expect(flowStore.get().modal).toBeNull();
    expect(controller.signal.aborted).toBe(true);
    expect(ob.active).toBeNull();
    expect(ob.provisioned).toBe(false);
  });

  it("closes a wizard parked on a failed step's Retry without the prompt", () => {
    const controller = runOn('sys-a', { waitingOnRetry: true });
    act(() => root.render(<ObFlow />));

    act(() => cancelButton()!.click());

    expect(flowStore.get().modal).toBeNull();
    expect(controller.signal.aborted).toBe(true);
    expect(ob.active).toBeNull();
  });
});
