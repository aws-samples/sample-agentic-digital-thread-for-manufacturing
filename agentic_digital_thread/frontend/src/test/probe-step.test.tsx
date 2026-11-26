// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tests — runProbeStep (probe-step.ts), the T-H pre-map pause.
 *
 * No DOM — the component's Continue is simulated by invoking the store
 * entry's onContinue callback, exactly what <PreMapProbe/> does. The
 * load-bearing properties: the pause resolves and cleans up its entry, abort
 * propagates as AbortError (the wizard's existing cancel path), and a settle
 * is final. */
import { beforeEach, describe, expect, it } from 'vitest';

import { flowStore, startWizard, type FlowEntry } from '../views/onboard/flow-store';
import { runProbeStep } from '../views/onboard/probe-step';

type ProbeEntry = Extract<FlowEntry, { kind: 'probe' }>;

const probeEntries = (): ProbeEntry[] =>
  flowStore.get().entries.filter((e): e is ProbeEntry => e.kind === 'probe');

beforeEach(() => {
  startWizard(); // clean entries
});

describe('runProbeStep', () => {
  it('resolves and removes the entry when the component continues', async () => {
    // Kills dropping removeEntry (the entry would linger and remount) or
    // dropping resolve (the wizard would hang before /map).
    const promise = runProbeStep('sys-1', [], new AbortController().signal);
    const entry = probeEntries()[0];
    expect(entry).toBeDefined();
    expect(entry.systemId).toBe('sys-1');
    entry.onContinue();
    await promise;
    expect(probeEntries()).toEqual([]);
  });

  it('rejects with AbortError and removes the entry on cancel', async () => {
    // Kills dropping the abort listener: the wizard would hang on cancel.
    const ctrl = new AbortController();
    const promise = runProbeStep('sys-1', [], ctrl.signal);
    expect(probeEntries()).toHaveLength(1);
    ctrl.abort();
    await expect(promise).rejects.toMatchObject({ name: 'AbortError' });
    expect(probeEntries()).toEqual([]);
  });

  it('rejects immediately when the signal is already aborted', async () => {
    const ctrl = new AbortController();
    ctrl.abort();
    await expect(runProbeStep('sys-1', [], ctrl.signal)).rejects.toMatchObject({
      name: 'AbortError',
    });
    expect(probeEntries()).toEqual([]);
  });

  it('a second onContinue after abort is a no-op', async () => {
    // Kills removing the settled guard: continue-after-abort must not throw
    // or resurrect the already-rejected promise.
    const ctrl = new AbortController();
    const promise = runProbeStep('sys-1', [], ctrl.signal);
    const entry = probeEntries()[0];
    ctrl.abort();
    await expect(promise).rejects.toMatchObject({ name: 'AbortError' });
    expect(() => entry.onContinue()).not.toThrow();
    expect(probeEntries()).toEqual([]);
  });
});
