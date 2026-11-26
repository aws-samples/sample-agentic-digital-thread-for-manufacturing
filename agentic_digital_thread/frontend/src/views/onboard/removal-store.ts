// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* System-removal job tracking for the catalog cards.
 *
 * Removal cascades graph deletion → AgentCore runtime/gateway-target deletion
 * → catalog removal, and can run for minutes (a synchronous request 504s at
 * the CloudFront 120 s origin timeout — the husk-record failure mode). The UI
 * starts the reset as a background job (?wait=false), tracks the system id in
 * removingStore, and polls the status endpoint (flow.ts); the catalog card
 * keeps that system's actions (including re-onboard) disabled until the job
 * reports done — i.e. the runtime and gateway target are confirmed ABSENT and
 * a re-onboard cannot race a still-DELETING runtime.
 *
 * A failed job is recorded here with a sequence number. The catalog read
 * (onboard-queries.ts) takes the current number before it fetches and, once
 * it succeeds, clears every failure recorded up to that number whose system
 * the server no longer reports as `removal_failed` (another tab, or Reset All,
 * recovered it). A read that started before a failure never clears it: its
 * answer predates the failure. Its own module so the catalog read can use it
 * without importing the onboarding flow. */
import type { CatalogSystem } from '../../api/types/onboard';
import { createStore } from '../../lib/store';
import { sysId } from './catalog-store';

export interface RemovingMode {
  /** Whether the removal was a FULL removal (?full=true). */
  full: boolean;
  /** Whether it was launched as a registration-only removal (a 'registered'
   * user card's "Remove registration"). */
  registration: boolean;
}

export interface RemovingState {
  ids: Record<string, 'running' | 'error'>;
  errors: Record<string, string>;
  /** Sticky removal intent per system, so "Remove again" after a failure
   * reopens in the SAME mode and never downgrades a full removal (whose secret
   * may already be scheduled for deletion) to a default one. Optional so a
   * caller that resets only ids/errors stays valid. */
  modes?: Record<string, RemovingMode>;
  /** When each `error` was recorded (recordRemovalError's sequence number). */
  errorSeq?: Record<string, number>;
}

export const removingStore = createStore<RemovingState>({ ids: {}, errors: {}, modes: {} });

let seq = 0;

/** The sequence number a catalog read takes before it fetches (shared by
 * failed removals and "Remove again" notices). */
export function removalSeq(): number {
  return seq;
}

/** Record a failed removal job: its error, and its mode (`full` from the
 * status record, the authoritative server intent; the registration flag is
 * kept from when it started). */
export function recordRemovalError(systemId: string, error: string, full?: boolean): void {
  seq += 1;
  const at = seq;
  removingStore.update((s) => {
    const prevModes = s.modes ?? {};
    const prior = prevModes[systemId];
    const modes =
      typeof full === 'boolean' || prior
        ? {
            ...prevModes,
            [systemId]: {
              full: typeof full === 'boolean' ? full : !!prior?.full,
              registration: !!prior?.registration,
            },
          }
        : prevModes;
    return {
      ids: { ...s.ids, [systemId]: 'error' as const },
      errors: { ...s.errors, [systemId]: error },
      modes,
      errorSeq: { ...s.errorSeq, [systemId]: at },
    };
  });
}

/** Forget a system's removal record (its job finished, or the server no
 * longer reports the system as half-removed). */
export function clearRemoval(systemIds: string[]): void {
  if (!systemIds.length) return;
  removingStore.update((s) => {
    const ids = { ...s.ids };
    const errors = { ...s.errors };
    const modes = { ...s.modes };
    const errorSeq = { ...s.errorSeq };
    for (const sid of systemIds) {
      delete ids[sid];
      delete errors[sid];
      delete modes[sid];
      delete errorSeq[sid];
    }
    return { ...s, ids, errors, modes, errorSeq };
  });
}

/** After a successful catalog read that started at `readStartedAt`: clear
 * each failure recorded no later than that whose system the catalog does not
 * report as `removal_failed`. Running removals are left alone. Stale
 * "Remove again" notices are dropped the same way (reconcileNotices). */
export function reconcileRemovals(systems: CatalogSystem[], readStartedAt: number): void {
  const st = removingStore.get();
  const status = new Map(systems.map((s) => [sysId(s), s.status]));
  const stale = Object.keys(st.ids).filter(
    (sid) =>
      st.ids[sid] === 'error' &&
      (st.errorSeq?.[sid] ?? 0) <= readStartedAt &&
      status.get(sid) !== 'removal_failed',
  );
  clearRemoval(stale);
  reconcileNotices(systems, readStartedAt);
}

/* ── Notices from "Remove again"'s click-time check ─────────────────────────
 * When "Remove again" finds the system no longer half-removed, or cannot read
 * its state, it opens nothing. The outcome is a notice, said once by the
 * catalog's live region (CatalogList: one region outside every card, since an
 * onboarded card is itself role="button") and, when the clicked button
 * disappeared, focus moves to the card's current action. A notice is:
 *
 * - announced once: the region takes every unsaid notice (several cards can
 *   finish their checks in the same tick) and marks exactly those said, and
 *   only while it is on screen, so a catalog mounted later does not say them
 *   again and none is lost;
 * - kept visible while the state it describes holds, and dropped when a
 *   catalog read that started after it shows another state (reconcileNotices),
 *   or when a new action on that system starts (an onboarding, a removal
 *   dialog, another "Remove again"). Kept here, outside the card, because the
 *   card remounts when the system moves to another catalog section. */
export interface RemovalNotice {
  text: string;
  /** Focus should move to the card's current action (or the catalog region). */
  focus: boolean;
  /** What the check found: the system's status, `gone` (not in the catalog)
   * or `unreadable` (the read failed). */
  found: string;
  /** Said by the live region already. */
  announced: boolean;
  seq: number;
}

export const removalNoticeStore = createStore<Record<string, RemovalNotice>>({});

export function setRemovalNotice(
  systemId: string,
  text: string,
  focus: boolean,
  found: string,
): void {
  seq += 1;
  const at = seq;
  removalNoticeStore.update((s) => ({
    ...s,
    [systemId]: { text, focus, found, announced: false, seq: at },
  }));
}

export function clearRemovalNotice(systemId: string): void {
  if (!(systemId in removalNoticeStore.get())) return;
  removalNoticeStore.update((s) => {
    const next = { ...s };
    delete next[systemId];
    return next;
  });
}

/** Every notice not yet said, oldest first. */
export function pendingRemovalNotices(
  notices: Record<string, RemovalNotice>,
): Array<[string, RemovalNotice]> {
  return Object.entries(notices)
    .filter(([, n]) => !n.announced)
    .sort((a, b) => a[1].seq - b[1].seq);
}

/** The live region now says these notices: mark exactly those said (each
 * `[systemId, seq]` still current), so none is said again, also by a catalog
 * mounted later. A notice recorded after them stays unsaid. Text stays. */
export function markRemovalNoticesAnnounced(said: Array<[string, number]>): void {
  const notices = removalNoticeStore.get();
  const hit = said.filter(([sid, at]) => notices[sid]?.seq === at && !notices[sid].announced);
  if (hit.length === 0) return;
  const next = { ...notices };
  for (const [sid] of hit) next[sid] = { ...next[sid], announced: true };
  removalNoticeStore.set(next);
}

/** After a successful catalog read that started at `readStartedAt`: drop each
 * notice recorded no later than that whose result no longer holds (the system
 * is back when it was gone, has another status, or is readable again). */
function reconcileNotices(systems: CatalogSystem[], readStartedAt: number): void {
  const status = new Map(systems.map((s) => [sysId(s), s.status || '']));
  for (const [sid, n] of Object.entries(removalNoticeStore.get())) {
    if (n.seq > readStartedAt) continue;
    const holds =
      n.found === 'gone'
        ? !status.has(sid)
        : n.found !== 'unreadable' && status.get(sid) === n.found;
    if (!holds) clearRemovalNotice(sid);
  }
}
