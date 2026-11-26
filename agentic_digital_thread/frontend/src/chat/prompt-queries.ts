// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import {
  QueryClient,
  QueryClientContext,
  queryOptions,
  useMutation,
  useQuery,
} from '@tanstack/react-query';
import { useContext } from 'react';

import { requestJson } from '../api/client';
import { createQueryClient, queryJson } from '../api/query-client';
import { queryKeys } from '../api/query-keys';
import type {
  Prompt,
  SavePromptRequest,
  SavePromptResponse,
  ScheduleResponse,
} from '../api/types/prompts';

export const PROMPTS_STALE_TIME_MS = 10 * 60 * 1000;
export const PROMPTS_REFETCH_INTERVAL_MS = 5 * 60 * 1000;

export function promptQueryOptions() {
  return queryOptions({
    queryKey: queryKeys.prompts(),
    queryFn: ({ signal }) => queryJson<Prompt[]>('/api/prompts', signal),
    staleTime: PROMPTS_STALE_TIME_MS,
    refetchInterval: PROMPTS_REFETCH_INTERVAL_MS,
    refetchIntervalInBackground: false,
  });
}

/**
 * Isolated component tests historically mount chat surfaces without the app
 * provider. Production always replaces this fallback with the app entry's
 * QueryClient as soon as a prompt hook renders.
 */
const isolatedPromptQueryClient = createQueryClient();
let activePromptQueryClient: QueryClient = isolatedPromptQueryClient;

function usePromptQueryClient(): QueryClient {
  const providedClient = useContext(QueryClientContext);
  const queryClient = providedClient ?? isolatedPromptQueryClient;
  activePromptQueryClient = queryClient;
  return queryClient;
}

/** The QueryClient registered by the mounted app prompt surfaces. */
export function getPromptQueryClient(): QueryClient {
  return activePromptQueryClient;
}

export function usePrompts(enabled = true) {
  const queryClient = usePromptQueryClient();
  return useQuery({ ...promptQueryOptions(), enabled }, queryClient);
}

/** Shared non-hook read for imperative consumers such as dashboard demo mode. */
export function fetchPrompts(queryClient: QueryClient): Promise<Prompt[]> {
  return queryClient.fetchQuery(promptQueryOptions());
}

export function invalidatePrompts(queryClient: QueryClient): Promise<void> {
  return queryClient.invalidateQueries({ queryKey: queryKeys.prompts() });
}

export interface SavedPromptResponse extends SavePromptResponse {
  template_id?: string;
}

export interface UpdatePromptVariables {
  promptId: string;
  updates: Partial<SavePromptRequest>;
}

export interface DeletePromptVariables {
  promptId: string;
}

export interface SchedulePromptVariables {
  promptId: string;
  scheduleText: string;
}

async function savePrompt(payload: SavePromptRequest): Promise<SavedPromptResponse> {
  return requestJson<SavedPromptResponse>('/api/prompts', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
}

async function updatePrompt({ promptId, updates }: UpdatePromptVariables): Promise<{ success?: boolean }> {
  return requestJson<{ success?: boolean }>(`/api/prompts/${encodeURIComponent(promptId)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(updates),
  });
}

async function deletePrompt({ promptId }: DeletePromptVariables): Promise<{ success?: boolean }> {
  return requestJson<{ success?: boolean }>(`/api/prompts/${encodeURIComponent(promptId)}`, {
    method: 'DELETE',
  });
}

async function schedulePrompt({
  promptId,
  scheduleText,
}: SchedulePromptVariables): Promise<ScheduleResponse> {
  return requestJson<ScheduleResponse>(
    `/api/prompts/${encodeURIComponent(promptId)}/schedule`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ schedule_text: scheduleText }),
    },
  );
}

export function useSavePromptMutation() {
  const queryClient = usePromptQueryClient();
  return useMutation(
    {
      mutationFn: savePrompt,
      onSuccess: () => invalidatePrompts(queryClient),
    },
    queryClient,
  );
}

/** POST /api/prompts is both the create and save operation. */
export function useCreatePromptMutation() {
  return useSavePromptMutation();
}

export function useUpdatePromptMutation() {
  const queryClient = usePromptQueryClient();
  return useMutation(
    {
      mutationFn: updatePrompt,
      onSuccess: () => invalidatePrompts(queryClient),
    },
    queryClient,
  );
}

export function useDeletePromptMutation() {
  const queryClient = usePromptQueryClient();
  return useMutation(
    {
      mutationFn: deletePrompt,
      onSuccess: () => invalidatePrompts(queryClient),
    },
    queryClient,
  );
}

export function useSchedulePromptMutation() {
  const queryClient = usePromptQueryClient();
  return useMutation(
    {
      mutationFn: schedulePrompt,
      onSuccess: () => invalidatePrompts(queryClient),
    },
    queryClient,
  );
}
