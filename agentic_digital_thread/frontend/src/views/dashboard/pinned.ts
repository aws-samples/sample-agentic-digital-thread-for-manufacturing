// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Pinned prompt cards logic — the fetch half of the old pinned.ts
 * (jsx-migration W2.1): GET /api/dashboard-cards seeds the store slice,
 * POST /api/dashboard-cards/{id}/refresh replaces one card in place (the old
 * card.replaceWith). Rendering (incl. the sanitized result markdown through
 * <Md/>) lives in PinnedInsights.tsx. */
import { requestJson } from '../../api/client';
import type { PinnedCard } from '../../api/types/dashboard';
import { clientTimeContext } from '../../lib/answer-times';
import { setPinned, setPinnedError, updatePinnedCard } from './store';

export async function loadPinnedCards(): Promise<void> {
  try {
    const cards = await requestJson<PinnedCard[]>('/api/dashboard-cards');
    setPinned(cards || []);
  } catch (e) {
    // Fail-soft (container stays hidden when never loaded) — but never silent.
    console.error('Failed to load pinned cards:', e);
  }
}

export async function refreshCard(cardId: string): Promise<void> {
  try {
    // The browser's zone and locale, as a chat question sends them, so the
    // run's time line resolves "today" against the user's clock.
    const updated = await requestJson<PinnedCard>(
      `/api/dashboard-cards/${cardId}/refresh`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(clientTimeContext()),
      },
    );
    updatePinnedCard(cardId, updated);
  } catch (e) {
    setPinnedError(cardId, (e as Error)?.message || String(e));
  }
}
