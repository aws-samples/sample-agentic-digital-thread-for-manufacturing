// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Pre-map probe step (T-H). Pauses the deterministic pipeline BETWEEN the
 * schema checkpoint and /map. Deliberately NOT a gate: the component is
 * failure-open and skippable; /map is never touched. The map job, once
 * started, is never paused.
 *
 * Settle/cleanup discipline mirrors wizard.ts::waitForCheckpoint — one
 * outcome, the abort listener always detaches, the flow entry is always
 * removed (a lingering probe entry would remount and re-fire its fetch). */
import type { DiscoveredTable } from '../../api/types/onboard';
import { appendProbe, removeEntry } from './flow-store';

export function runProbeStep(
  systemId: string,
  tables: DiscoveredTable[],
  signal: AbortSignal,
): Promise<void> {
  return new Promise<void>((resolve, reject) => {
    let settled = false;
    let entryId = 0;
    const cleanup = (): void => signal.removeEventListener('abort', onAbort);
    const finish = (fn: () => void): void => {
      if (settled) return;
      settled = true;
      cleanup();
      removeEntry(entryId);
      fn();
    };
    const onAbort = (): void =>
      finish(() => reject(new DOMException('Onboarding canceled', 'AbortError')));
    if (signal.aborted) {
      onAbort();
      return;
    }
    signal.addEventListener('abort', onAbort, { once: true });
    entryId = appendProbe(systemId, tables, () => finish(resolve));
  });
}
