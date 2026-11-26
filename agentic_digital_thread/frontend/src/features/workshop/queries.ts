// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Workshop story server state — the ONE query options factory shared by the
 * admin Workshop tab and the header plant-clock chip (ADR-0091 D7: one key, one
 * 60 s poll, so both surfaces share a single request per minute).
 *
 * 403/404 mean "not a live workshop / not a steward": they never retry and
 * the consumers render nothing (clock.clockGate) — no toast, no console
 * noise. A transient failure keeps the last good body on screen. */
import { mutationOptions, queryOptions, type QueryClient } from '@tanstack/react-query';

import { ApiError, postJson } from '../../api/client';
import { queryJson } from '../../api/query-client';
import { queryKeys } from '../../api/query-keys';
import type {
  PlaceClockBody,
  PlaceClockPreview,
  PlaceClockReport,
  ResetClockPreview,
  WorkshopStory,
} from '../../api/types/workshop';

export const WORKSHOP_STORY_URL = '/api/admin/workshop/story';
export const WORKSHOP_CLOCK_URL = '/api/admin/workshop/story/clock';
export const WORKSHOP_STORY_POLL_MS = 60_000;

const MAX_RETRIES = 2;

/** Never retry a 4xx (403/404 = hidden, silent); a transient 5xx/network retries twice. */
export function workshopStoryRetry(failureCount: number, error: unknown): boolean {
  if (error instanceof ApiError && error.status >= 400 && error.status < 500) return false;
  return failureCount < MAX_RETRIES;
}

export function workshopStoryQueryOptions(enabled: boolean) {
  return queryOptions({
    queryKey: queryKeys.admin.workshopStory(),
    queryFn: ({ signal }) => queryJson<WorkshopStory>(WORKSHOP_STORY_URL, signal),
    enabled,
    staleTime: 30_000,
    refetchInterval: WORKSHOP_STORY_POLL_MS,
    refetchIntervalInBackground: false,
    refetchOnWindowFocus: false,
    retry: workshopStoryRetry,
  });
}

export type PlaceClockResult = PlaceClockPreview | ResetClockPreview | PlaceClockReport;

/** POST the placement or reset (preview when `dry_run`). An applied
 * placement/reset (including the first-sign-in auto-place, whose body omits
 * `dry_run`), or a 409 (in flight / stale anchor), re-reads the story so
 * every surface shows the anchor the server actually holds. */
export function placeClockMutationOptions(client: QueryClient) {
  return mutationOptions({
    mutationFn: (body: PlaceClockBody) => postJson<PlaceClockResult>(WORKSHOP_CLOCK_URL, body),
    onSettled: (_data, error, body) => {
      const conflict = error instanceof ApiError && error.status === 409;
      if (!body.dry_run || conflict) {
        return client.invalidateQueries({ queryKey: queryKeys.admin.workshopStory() });
      }
      return undefined;
    },
  });
}
