// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Workshop story hooks shared by the admin Workshop tab, the header chip and
 * the Onboard view's Reset All gate: one observer of the ONE story query (D7)
 * plus a local wall-clock tick. */
import { useEffect, useState } from 'react';
import { useQuery } from '@tanstack/react-query';

import { demoStatusQueryOptions } from '../../api/reference-queries';
import { useAuth } from '../../contexts/AuthContext';
import { clockGate, refreshFailed, workshopMode, type WorkshopGate, type WorkshopMode } from './clock';
import { workshopStoryQueryOptions } from './queries';

/** Whether to send the story GET for this viewer: admins only (the GET is
 * steward-gated), and not once /api/demo/status says workshop mode is off,
 * where the GET would only 404. `off` is that answer. A failed status, or one
 * without the field, keeps the GET. The status query is the shell's own
 * (same key), so a page that already loaded it sends no extra request. */
function useStoryGate(): { enabled: boolean; off: boolean } {
  const { user } = useAuth();
  const isAdmin = !!user?.is_admin;
  // Workshop mode is fixed per deploy, so this observer never polls.
  const status = useQuery({ ...demoStatusQueryOptions(), enabled: isAdmin, refetchInterval: false });
  const off = status.data?.workshop === false;
  return { enabled: isAdmin && !off && status.status !== 'pending', off };
}

/** The shared story query for this viewer and its gate: 'pending' until the
 * first response. */
export function useWorkshopStory() {
  const { user } = useAuth();
  const isAdmin = !!user?.is_admin;
  const { enabled, off } = useStoryGate();
  const query = useQuery(workshopStoryQueryOptions(enabled));
  const settled = clockGate(query.data, query.error);
  const gate: WorkshopGate = !isAdmin || off
    ? 'hidden'
    : settled === 'live' || query.status !== 'pending'
      ? settled
      : 'pending';
  return {
    query,
    gate,
    clock: gate === 'live' ? query.data?.clock ?? null : null,
    refreshFailed: gate === 'live' && refreshFailed(query.data, query.error),
  };
}

/** Whether this account runs a workshop story (clock.workshopMode), from the
 * SAME story query (D7: no extra request). 'unknown' while /api/auth/me is
 * pending or after it failed (no user: a failure is not "not an admin"), and
 * while the story GET is pending, so a control shown only on 'off' never
 * appears and then vanishes; a known non-admin is 'off' (the GET is
 * steward-gated, so it would 403 for them).
 *
 * A read-only subscriber: no poll, no refetch on mount, no retry on mount of
 * a cached error. The app shell's always-mounted header chip / auto-place
 * observers own the 60 s poll; a view mounted later (the Onboard view, on
 * first visit) would otherwise refetch a stale body on mount and re-phase
 * that poll, or refetch a base deploy's cached 404. An empty cache still
 * loads once. */
export function useWorkshopMode(): WorkshopMode {
  const { user, loading } = useAuth();
  const { enabled, off } = useStoryGate();
  const query = useQuery({
    ...workshopStoryQueryOptions(enabled),
    refetchInterval: false,
    refetchOnMount: false,
    retryOnMount: false,
  });
  if (loading || !user) return 'unknown';
  if (!user.is_admin || off) return 'off';
  return workshopMode(query.data, query.error);
}

/** `Date.now()` re-read every `intervalMs` (no timer when null). */
export function useNow(intervalMs: number | null, resetKey?: unknown): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    setNow(Date.now());
    if (intervalMs === null) return undefined;
    const id = window.setInterval(() => setNow(Date.now()), intervalMs);
    return () => window.clearInterval(id);
  }, [intervalMs, resetKey]);
  return now;
}
