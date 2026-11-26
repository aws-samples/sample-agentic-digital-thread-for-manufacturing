// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Auto-place on first sign-in (Design C) — the DOM-free half: the decision
 * (`autoPlaceRequest`), the page-load status store the header chip reads,
 * and the one-shot session suppression a Workshop-tab reset sets.
 *
 * Suppression contract: a successful "Reset to the packaged default" sets
 * sessionStorage `adt.autoPlace.suppressed` to the anchor epoch it placed.
 * The NEXT app page load that would auto-place THAT anchor skips once and
 * clears the flag — so going back from the admin page to the app does not
 * immediately undo the reset; a flag for another anchor never suppresses,
 * and seeing a placed clock clears it. A reload
 * after that (or any later page load) auto-places again unless the packaged
 * `auto_place.enabled` flag is off; the reset is a failsafe, not an opt-out. */
import type { PlaceClockAutoBody, StoryClock } from '../../api/types/workshop';
import { createStore } from '../../lib/store';
import { effectiveAnchorSource, isDefaultAnchor } from './clock';

export const AUTO_PLACE_SUPPRESSED_KEY = 'adt.autoPlace.suppressed';

/** idle = nothing sent this page load (or a 409 re-read); starting = the POST
 * (and its story re-read) is in flight; placed = it succeeded; failed = any
 * non-409 error — the chip says "not started. Ask for help.". */
export type AutoPlaceStatus = 'idle' | 'starting' | 'placed' | 'failed';

export const autoPlaceStore = createStore<AutoPlaceStatus>('idle');

/** The auto-place body for this clock, or null when it must NOT fire: no
 * clock, `auto_place` absent/disabled, an anchor already placed (or unknown),
 * a live-only move stored (the alarm was placed with the keep-history card,
 * so a full placement would rewrite that history), or a malformed
 * minutes/anchor (never guess a number). */
export function autoPlaceRequest(clock: StoryClock | null | undefined): PlaceClockAutoBody | null {
  const auto = clock?.auto_place;
  if (!clock || !auto || auto.enabled !== true) return null;
  if (!isDefaultAnchor(effectiveAnchorSource(clock))) return null;
  if (!Number.isInteger(auto.minutes) || auto.minutes <= 0) return null;
  if (!Number.isFinite(clock.anchor_epoch)) return null;
  return { alarm_in_minutes: auto.minutes, expected_anchor_epoch: clock.anchor_epoch };
}

function session(): Storage | null {
  try {
    return typeof sessionStorage === 'undefined' ? null : sessionStorage;
  } catch {
    return null;
  }
}

/** Set by a successful reset, keyed to the anchor the reset placed: the next
 * would-be auto-place FOR THAT ANCHOR skips once. */
export function suppressAutoPlace(anchorEpoch: number): void {
  try {
    session()?.setItem(AUTO_PLACE_SUPPRESSED_KEY, String(anchorEpoch));
  } catch {
    /* storage unavailable: the in-page ref guard still holds */
  }
}

/** true (and the flag is cleared) when a reset asked to skip the auto-place
 * of exactly this anchor. A flag for any other anchor (the clock moved on
 * since the reset) is left alone and does not suppress. */
export function consumeAutoPlaceSuppression(anchorEpoch: number): boolean {
  try {
    const store = session();
    if (!store || store.getItem(AUTO_PLACE_SUPPRESSED_KEY) !== String(anchorEpoch)) return false;
    store.removeItem(AUTO_PLACE_SUPPRESSED_KEY);
    return true;
  } catch {
    return false;
  }
}

/** Drop any pending suppression — a placed clock makes it moot. */
export function clearAutoPlaceSuppression(): void {
  try {
    session()?.removeItem(AUTO_PLACE_SUPPRESSED_KEY);
  } catch {
    /* storage unavailable: nothing to clear */
  }
}
