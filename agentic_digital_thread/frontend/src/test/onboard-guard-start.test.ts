// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Onboard on a system card while another system's wizard is on screen: the
 * "Cancel onboarding?" prompt is for a run that is still working, not for a
 * wizard left waiting on a failed step's Retry or one whose provision already
 * finished (Team 2/3 walks 2026-10-19: Provision "Failed to fetch" and the
 * Verify error both left the wizard "active"). */
import { beforeEach, describe, expect, it, vi } from 'vitest';

const obStart = vi.hoisted(() => vi.fn(async () => {}));

vi.mock('../views/onboard/wizard', () => ({ obStart }));
vi.mock('../views/onboard/catalog', () => ({ obLoadCatalog: vi.fn(async () => {}) }));
vi.mock('../views/onboard/chat', () => ({
  obWelcomeChat: vi.fn(),
  welcomeSubmit: vi.fn(),
}));

import type { CatalogSystem } from '../api/types/onboard';
import { obGuardStart, obRunInProgress } from '../views/onboard/flow';
import { closeModal, flowStore, resetToWelcome } from '../views/onboard/flow-store';
import { ob } from '../views/onboard/state';

const next: CatalogSystem = { id: 'sys-next', name: 'Next system', connection_type: 'REST' };

function runOn(systemId: string, state: { waitingOnRetry?: boolean; provisioned?: boolean }) {
  const controller = new AbortController();
  ob.active = systemId;
  ob.abort = controller;
  ob.currentStep = { step: 'provision', systemId, data: {}, history: [] };
  ob.waitingOnRetry = state.waitingOnRetry ?? false;
  ob.provisioned = state.provisioned ?? false;
  return controller;
}

beforeEach(() => {
  closeModal();
  resetToWelcome();
  ob.active = null;
  ob.abort = null;
  ob.currentStep = null;
  ob.waitingOnRetry = false;
  ob.provisioned = false;
  obStart.mockClear();
});

describe('Onboard while another wizard is on screen', () => {
  it('asks before cancelling a run that is still working', () => {
    const controller = runOn('sys-prev', {});

    obGuardStart(next);

    expect(flowStore.get().modal).toMatchObject({ kind: 'cancel' });
    expect(obStart).not.toHaveBeenCalled();
    expect(controller.signal.aborted).toBe(false);
    expect(ob.active).toBe('sys-prev');
  });

  it("starts the next system at once when the run waits on a failed step's Retry", () => {
    const controller = runOn('sys-prev', { waitingOnRetry: true });

    obGuardStart(next);

    expect(flowStore.get().modal).toBeNull();
    // The parked run is put away the way Cancel does it, then the next starts.
    expect(controller.signal.aborted).toBe(true);
    expect(ob.waitingOnRetry).toBe(false);
    expect(obStart).toHaveBeenCalledOnce();
    expect(obStart).toHaveBeenCalledWith(next);
  });

  it('starts the next system at once when the run already provisioned its system', () => {
    // A Verify error, or the read-only verify and agent test still running:
    // the system is onboarded, nothing Cancel would stop is left.
    const controller = runOn('sys-prev', { provisioned: true });

    obGuardStart(next);

    expect(flowStore.get().modal).toBeNull();
    expect(controller.signal.aborted).toBe(true);
    expect(ob.provisioned).toBe(false);
    expect(obStart).toHaveBeenCalledWith(next);
  });

  it('starts at once when no wizard run is active', () => {
    obGuardStart(next);

    expect(flowStore.get().modal).toBeNull();
    expect(obStart).toHaveBeenCalledWith(next);
  });

  it('counts a run as in progress only while it is working', () => {
    expect(obRunInProgress()).toBe(false);
    runOn('sys-prev', {});
    expect(obRunInProgress()).toBe(true);
    ob.waitingOnRetry = true;
    expect(obRunInProgress()).toBe(false);
    ob.waitingOnRetry = false;
    ob.provisioned = true;
    expect(obRunInProgress()).toBe(false);
  });
});
