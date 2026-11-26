// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* The AgentCore Memory extraction model switch: a background job the
 * Settings tab starts and polls (agents/memory_model_job.py). It is not part
 * of the LLM settings PUT because the switch takes about two minutes, longer
 * than one request may last. */
import { mutationOptions, queryOptions } from '@tanstack/react-query';

import { requestJson } from '../../api/client';
import { queryJson } from '../../api/query-client';
import { queryKeys } from '../../api/query-keys';
import type { MemoryModelJob } from '../../api/types/admin';

const SECOND = 1_000;
const MINUTE = 60 * SECOND;
const MEMORY_MODEL_SWITCH = '/api/admin/llm-settings/memory-model';

/** A switch job is polled this often while it runs, and for at most this
 * long after the page started watching it (the server reports a worker that
 * stopped after 15 minutes). */
export const MEMORY_JOB_POLL_MS = 3 * SECOND;
export const MEMORY_JOB_POLL_LIMIT_MS = 20 * MINUTE;
/** A job read that fails (a 5xx, or no answer) is tried again this many
 * times, MEMORY_JOB_POLL_MS apart, before the page shows why; a 4xx is not
 * retried. */
export const MEMORY_JOB_RETRIES = 2;

/** GET .../memory-model/job/{jobId}, or the latest switch for `null`. The
 * caller sets the poll cadence (it alone knows when it started watching). */
export function memoryModelJobQueryOptions(jobId: string | null) {
  return queryOptions({
    queryKey: queryKeys.admin.memoryModelJob(jobId ?? 'current'),
    queryFn: ({ signal }) =>
      queryJson<MemoryModelJob>(
        jobId ? `${MEMORY_MODEL_SWITCH}/job/${encodeURIComponent(jobId)}` : `${MEMORY_MODEL_SWITCH}/job`,
        signal,
      ),
    staleTime: 0,
    refetchOnWindowFocus: false,
    refetchIntervalInBackground: false,
  });
}

/** POST .../memory-model: starts the background switch (202, the running
 * job). A 400/409/503 surfaces through ApiError.detail (the server's
 * message). */
export function startMemoryModelSwitchMutationOptions() {
  return mutationOptions({
    mutationFn: async (memoryModel: string) => {
      const job = await requestJson<MemoryModelJob>(MEMORY_MODEL_SWITCH, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ memory_model: memoryModel }),
      });
      if (job.status !== 'running' || !job.job_id) {
        throw new Error(job.message || 'Server did not start the memory model switch');
      }
      return job;
    },
  });
}
