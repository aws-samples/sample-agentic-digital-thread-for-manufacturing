// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* /api/version → #version-badge fill. AppShell supplies the app entry's
 * QueryClient; the ArchitectureView repeat can then reuse that same client. */
import type { QueryClient } from '@tanstack/react-query';

import { versionQueryOptions } from '../api/reference-queries';

let appQueryClient: QueryClient | null = null;

function setBadges(text: string): void {
  document.querySelectorAll('#version-badge').forEach((element) => {
    element.textContent = text;
  });
}

export function fillVersionBadge(queryClient?: QueryClient): void {
  // Clear the loading placeholder immediately so failure cannot be mistaken
  // for a real version. A no-client ArchitectureView call is a safe no-op;
  // AppShell registers and uses the entry client in its own mount effect.
  setBadges('');
  if (queryClient) appQueryClient = queryClient;
  const client = queryClient ?? appQueryClient;
  if (!client) return;

  void client
    .fetchQuery(versionQueryOptions())
    .then((info) => setBadges(info.version || ''))
    .catch(() => {
      // Version is best-effort: silence is honest; a placeholder is not.
      setBadges('');
    });
}
