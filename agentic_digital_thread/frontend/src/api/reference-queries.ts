// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { queryOptions, useQuery } from '@tanstack/react-query';

import type { AuthMe } from './types/auth';
import type { Branding, DemoStatus, VersionInfo } from './types/core';
import { queryJson } from './query-client';
import { queryKeys } from './query-keys';

export const REFERENCE_STALE_TIME_MS = 10 * 60 * 1000;
export const STATUS_STALE_TIME_MS = 5 * 60 * 1000;
export const STATUS_REFETCH_INTERVAL_MS = 60 * 1000;

export interface OnboardStatus {
  setup_complete?: boolean;
  suggested_questions?: string[];
  systems?: unknown;
  [key: string]: unknown;
}

export function brandingQueryOptions() {
  return queryOptions({
    queryKey: queryKeys.branding(),
    queryFn: ({ signal }) => queryJson<Branding>('/api/branding', signal),
    staleTime: REFERENCE_STALE_TIME_MS,
    refetchIntervalInBackground: false,
  });
}

export function authMeQueryOptions() {
  return queryOptions({
    queryKey: queryKeys.authMe(),
    queryFn: ({ signal }) => queryJson<AuthMe>('/api/auth/me', signal),
    staleTime: REFERENCE_STALE_TIME_MS,
    refetchIntervalInBackground: false,
  });
}

export function versionQueryOptions() {
  return queryOptions({
    queryKey: queryKeys.version(),
    queryFn: ({ signal }) => queryJson<VersionInfo>('/api/version', signal),
    staleTime: REFERENCE_STALE_TIME_MS,
    refetchIntervalInBackground: false,
  });
}

export function demoStatusQueryOptions() {
  return queryOptions({
    queryKey: queryKeys.demoStatus(),
    queryFn: ({ signal }) => queryJson<DemoStatus>('/api/demo/status', signal),
    staleTime: STATUS_STALE_TIME_MS,
    refetchInterval: STATUS_REFETCH_INTERVAL_MS,
    refetchIntervalInBackground: false,
  });
}

export function onboardStatusQueryOptions() {
  return queryOptions({
    queryKey: queryKeys.onboardStatus(),
    queryFn: ({ signal }) => queryJson<OnboardStatus>('/api/onboard/status', signal),
    staleTime: STATUS_STALE_TIME_MS,
    refetchInterval: STATUS_REFETCH_INTERVAL_MS,
    refetchIntervalInBackground: false,
  });
}

export function useBrandingQuery() {
  return useQuery(brandingQueryOptions());
}

export function useAuthMeQuery() {
  return useQuery(authMeQueryOptions());
}

export function useVersionQuery() {
  return useQuery(versionQueryOptions());
}

export function useDemoStatusQuery() {
  return useQuery(demoStatusQueryOptions());
}

export function useOnboardStatusQuery() {
  return useQuery(onboardStatusQueryOptions());
}
