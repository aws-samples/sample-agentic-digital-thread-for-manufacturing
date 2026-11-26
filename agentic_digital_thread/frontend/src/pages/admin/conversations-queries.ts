// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { infiniteQueryOptions } from '@tanstack/react-query';

import { queryJson } from '../../api/query-client';
import { queryKeys } from '../../api/query-keys';
import type { ConversationsPage } from '../../api/types/admin';
import { conversationsUrl } from './tabs/conversations-logic';

const MINUTE = 60_000;

/** GET /api/admin/conversations, one page per fetch, chained by the server's
 * opaque `next` cursor. No automatic retry: every fetch reads memory for many
 * users, so a failure is shown with a Retry button instead of being repeated. */
export function adminConversationsQueryOptions(days: number, user: string) {
  return infiniteQueryOptions({
    queryKey: queryKeys.admin.conversations(days, user),
    queryFn: ({ signal, pageParam }) =>
      queryJson<ConversationsPage>(conversationsUrl(days, user, pageParam), signal),
    initialPageParam: null as string | null,
    getNextPageParam: (last: ConversationsPage) => last.next ?? undefined,
    staleTime: MINUTE,
    retry: false,
    refetchOnWindowFocus: false,
    refetchIntervalInBackground: false,
  });
}
