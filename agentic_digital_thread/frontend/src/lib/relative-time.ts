// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

export type RelativeTimeInput = string | number | Date | null | undefined;

export interface RelativeTimeOptions {
  /** Dashboard labels remain day-based; session labels roll seven days into weeks. */
  maxUnit?: 'day' | 'week';
  /** Value returned when the caller has no timestamp. */
  empty?: string;
}

/** Format compact elapsed time while allowing callers to retain their
 * historical day-only or week-bucket presentation. Numeric inputs are epoch
 * milliseconds, matching Date#getTime and SessionMenu's existing contract. */
export function formatRelativeTime(
  value: RelativeTimeInput,
  options: RelativeTimeOptions = {},
): string {
  if (value === null || value === undefined) return options.empty ?? '';
  const timestamp = value instanceof Date
    ? value.getTime()
    : typeof value === 'number'
      ? value
      : new Date(value).getTime();
  const elapsedSeconds = Math.floor((Date.now() - timestamp) / 1000);
  if (elapsedSeconds < 60) return 'just now';
  const minutes = Math.floor(elapsedSeconds / 60);
  if (minutes < 60) return minutes + 'm ago';
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return hours + 'h ago';
  const days = Math.floor(hours / 24);
  if (options.maxUnit === 'week' && days >= 7) return Math.floor(days / 7) + 'w ago';
  return days + 'd ago';
}
