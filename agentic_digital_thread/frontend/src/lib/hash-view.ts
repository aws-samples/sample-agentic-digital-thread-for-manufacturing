// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Hash-driven view switching — the app's URL semantics
 * (switchView / boot hash restore):
 * - switching a view writes `history.replaceState(null, '', '#' + view)`
 *   (replaceState, NOT location.hash assignment, so no hashchange fires);
 * - on load, a matching `#<view>` hash restores that view; otherwise the
 *   default (dashboard) stays active and no hash is written. */

export const VIEWS = ['dashboard', 'architecture', 'threads', 'onboard'] as const;
export type ViewName = (typeof VIEWS)[number];

export function isViewName(v: string): v is ViewName {
  return (VIEWS as readonly string[]).includes(v);
}

/** The view encoded in location.hash, or null when absent/unknown. The view
 * is the FIRST hash segment, so a sub-path like `#onboard/questions` still
 * resolves to `onboard` (the sub-path names a surface inside the view — see
 * readHashSubview). */
export function readHashView(): ViewName | null {
  const base = window.location.hash.slice(1).split('/')[0] ?? '';
  return isViewName(base) ? base : null;
}

/** The sub-path after the view segment (`#onboard/questions` → `questions`),
 * or null when the hash carries no sub-path. Consumed by in-view surfaces
 * that mount off a deep link (e.g. the runtime steward-questions panel). */
export function readHashSubview(): string | null {
  const parts = window.location.hash.slice(1).split('/');
  return parts.length > 1 && parts[1] ? parts[1] : null;
}

/** Update the URL hash without triggering hashchange (dashboard.js:61). */
export function writeHashView(view: ViewName): void {
  history.replaceState(null, '', '#' + view);
}
