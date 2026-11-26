// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* useAutoPlaceClock() — Design C "start the plant clock on first sign-in".
 * Mounted ONCE in AppShell (the app page only).
 *
 * It observes the SHARED story query (admins only — the GET is
 * steward-gated) and acts only once that GET has DATA: when
 * `clock.auto_place.enabled` and the anchor is still the packaged/deploy
 * default, it POSTs `{alarm_in_minutes: auto_place.minutes,
 * expected_anchor_epoch: clock.anchor_epoch}` exactly once per page load (a
 * ref guard). Seeing a placed clock (`anchor_source: 'placed'`, or a stored
 * live-only move: `effectiveAnchorSource`) closes the guard for the rest
 * of the page load. A 409 (someone else placed / a placement in flight) only
 * re-reads the story (the mutation's onSettled); any other error sets the
 * chip's failed state; success re-reads the story, then toasts. A reset's
 * one-shot session flag (auto-place.ts) skips it once — only for the anchor
 * that reset placed — and seeing `placed` clears that flag. */
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef } from 'react';

import { ApiError } from '../../api/client';
import { t } from '../../lib/i18n';
import { push } from '../../shared/ui/toast-store';
import {
  autoPlaceRequest,
  autoPlaceStore,
  clearAutoPlaceSuppression,
  consumeAutoPlaceSuppression,
} from './auto-place';
import { effectiveAnchorSource, fill } from './clock';
import { placeClockMutationOptions } from './queries';
import { useWorkshopStory } from './useWorkshopStory';

export function useAutoPlaceClock(): void {
  const client = useQueryClient();
  const { mutateAsync } = useMutation(placeClockMutationOptions(client));
  const { clock } = useWorkshopStory();
  const done = useRef(false);

  const placed = clock ? effectiveAnchorSource(clock) === 'placed' : false;
  const request = autoPlaceRequest(clock);
  const minutes = request?.alarm_in_minutes;
  const anchor = request?.expected_anchor_epoch;

  useEffect(() => {
    if (placed) {
      done.current = true;
      clearAutoPlaceSuppression();
      return;
    }
    if (done.current || minutes === undefined || anchor === undefined) return;
    done.current = true;
    if (consumeAutoPlaceSuppression(anchor)) return;
    autoPlaceStore.set('starting');
    // Resolves after the onSettled story re-read, so the chip goes straight
    // from "starting…" to the placed clock.
    mutateAsync({ alarm_in_minutes: minutes, expected_anchor_epoch: anchor }).then(
      () => {
        autoPlaceStore.set('placed');
        push('success', fill(t('plantClock.autoPlace.started'), { minutes }));
      },
      (error: unknown) => {
        autoPlaceStore.set(error instanceof ApiError && error.status === 409 ? 'idle' : 'failed');
      },
    );
  }, [placed, minutes, anchor, mutateAsync]);
}
