// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Onboarding workflow singleton. Cacheable server state (concepts) is owned by
 * onboard-queries.ts; this module retains only active workflow/admin state. */
import { requestJson } from '../../api/client';
import type { ChatTurn } from '../../api/types/onboard';
import {
  conceptsQueryOptions,
  getCachedConcepts,
  getOnboardQueryClient,
  setConcepts,
} from './onboard-queries';

/** Step-chat context ("current step" for the persistent flow input). */
export interface ObStepContext {
  step: string;
  systemId: string;
  data: Record<string, unknown>;
  history: ChatTurn[];
  onRerun?: (el: HTMLElement) => void;
}

interface ObState {
  active: string | null;
  abort: AbortController | null;
  currentStep: ObStepContext | null;
  /** The active wizard run waits on the user to retry a failed step (its
   * request or job has ended; nothing of it is running). */
  waitingOnRetry: boolean;
  /** The active wizard run's provision has finished: its system is onboarded
   * and only the read-only verify and agent test are left. */
  provisioned: boolean;
  isAdmin: boolean;
  adminChecked: boolean;
  /** GENERIC deploy capability from /api/auth/me: this deployment accepts a
   * named Secrets Manager secret as a connection's credential source for at
   * least one connection type. No workshop/seat wording. */
  secretArnSourceEnabled: boolean;
  /** The connection types it accepts one for (/api/auth/me
   * ``secret_arn_source_types``), or ``null`` when the server sends only the
   * flag. Read through {@link secretArnSourceOffered}. */
  secretArnSourceTypes: string[] | null;
}

export const ob: ObState = {
  active: null,
  abort: null,
  currentStep: null,
  waitingOnRetry: false,
  provisioned: false,
  isAdmin: false,
  adminChecked: false,
  secretArnSourceEnabled: false,
  secretArnSourceTypes: null,
};

/** True when the Connect New System form offers the "Secrets Manager secret"
 * credential choice for ``connectionType``: the server lists that type. A
 * server that sends only the flag offers it for Tulip alone, the flag's
 * original meaning. */
export function secretArnSourceOffered(connectionType: string): boolean {
  if (ob.secretArnSourceTypes) return ob.secretArnSourceTypes.includes(connectionType);
  return ob.secretArnSourceEnabled && connectionType === 'Tulip (Tables API)';
}

export function getObConcepts(): string[] {
  return getCachedConcepts(getOnboardQueryClient());
}

export function setObConcepts(concepts: string[]): void {
  setConcepts(getOnboardQueryClient(), concepts);
}

/** Compatibility live array view for untouched mapping/remap render helpers.
 * Values are read from Query on every operation; this is not a second cache. */
export const OB_CONCEPTS = new Proxy([] as string[], {
  get(_target, property) {
    const concepts = getObConcepts();
    const value = Reflect.get(concepts, property, concepts) as unknown;
    return typeof value === 'function'
      ? (value as (...args: unknown[]) => unknown).bind(concepts)
      : value;
  },
});

/** Imperative mount compatibility; the request is deduped with useConceptsQuery. */
export function loadConcepts(): void {
  void getOnboardQueryClient().prefetchQuery(conceptsQueryOptions());
}

// Admin gate remains workflow/UI state rather than shared server reference data.
export async function obCheckAdmin(): Promise<boolean> {
  if (ob.adminChecked) return ob.isAdmin;
  try {
    const me = await requestJson<{
      is_admin?: boolean;
      secret_arn_source_enabled?: boolean;
      secret_arn_source_types?: unknown;
    }>('/api/auth/me');
    ob.isAdmin = !!me.is_admin;
    ob.secretArnSourceEnabled = !!me.secret_arn_source_enabled;
    ob.secretArnSourceTypes = Array.isArray(me.secret_arn_source_types)
      ? me.secret_arn_source_types.map(String)
      : null;
  } catch (error) {
    console.warn('Failed to check onboarding admin access:', error);
  }
  ob.adminChecked = true;
  return ob.isAdmin;
}
