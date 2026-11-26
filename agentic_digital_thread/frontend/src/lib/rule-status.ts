// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Which rule statuses switch a rule off — the frontend twin of
 * agents/rule_status.py. A switched-off identity rule or JoinPath stays
 * visible for audit and never counts as a connection. The server reports a
 * JoinPath whose identity rule is switched off with the effective status
 * `deactivated` (graph summary) or `effective_status` (KG view). */

export const INACTIVE_RULE_STATUSES: readonly string[] = ['deactivated', 'retired'];

export function isInactiveStatus(status: unknown): boolean {
  return typeof status === 'string' && INACTIVE_RULE_STATUSES.includes(status);
}
