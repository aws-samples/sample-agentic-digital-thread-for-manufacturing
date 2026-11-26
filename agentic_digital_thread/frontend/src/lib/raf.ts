// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Shared rAF scheduling pair — extracted from chat/thread-store.ts so the
 * data-modal follow-up stream can coalesce its per-token store patches the
 * same way the chat thread store does (a full
 * re-parse per token is ~15x the old marked cost and O(n²) over the answer). */

export interface RafPair {
  schedule: (cb: () => void) => number | null;
  cancel: (id: number | null) => void;
}

/** rAF pair, with a synchronous fallback where rAF does not exist (node). */
export function rafPair(): RafPair {
  if (typeof requestAnimationFrame === 'function' && typeof cancelAnimationFrame === 'function') {
    return {
      schedule: (cb) => requestAnimationFrame(cb),
      cancel: (id) => {
        if (id != null) cancelAnimationFrame(id);
      },
    };
  }
  return {
    schedule: (cb) => {
      cb();
      return null;
    },
    cancel: () => {},
  };
}
