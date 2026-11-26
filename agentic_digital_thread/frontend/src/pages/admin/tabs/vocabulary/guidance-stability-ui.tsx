// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useMemo, type CSSProperties } from 'react';
import { useQuery } from '@tanstack/react-query';

import { systemsQueryOptions } from '../../../../api/systems-query';
import type { GuidanceAcceptResult } from '../../../../api/types/proposals';

export const MUTED = 'var(--muted)';
export const RED = 'var(--red, #f87171)';
export const GREEN = 'var(--green, #4ade80)';
export const AMBER = 'var(--amber, #fbbf24)';
export const MONO = "'IBM Plex Mono', monospace";

export const panelStyle: CSSProperties = {
  border: '1px solid var(--border)',
  borderRadius: 6,
  background: 'var(--surface2)',
  padding: '0.75rem',
  marginBottom: '0.75rem',
};
export const compactStyle: CSSProperties = {
  fontSize: '0.72rem',
  color: MUTED,
  lineHeight: 1.45,
};
export const actionStyle: CSSProperties = {
  fontSize: '0.72rem',
  padding: '0.28rem 0.6rem',
  borderRadius: 4,
};

export type AuthorityState =
  | 'PREVIEW'
  | 'FOUND'
  | 'PENDING'
  | 'DELIVERED'
  | 'WOULD EVICT'
  | 'UNAVAILABLE'
  | 'ZERO';

const STATE_COLOR: Record<AuthorityState, string> = {
  PREVIEW: 'var(--accent)',
  FOUND: 'var(--accent)',
  PENDING: AMBER,
  DELIVERED: GREEN,
  'WOULD EVICT': RED,
  UNAVAILABLE: RED,
  ZERO: MUTED,
};

export function stateColor(state: AuthorityState): string {
  return STATE_COLOR[state];
}

/** Sentence-case words a tag shows when its caller passes no label. The
 * state ids stay ALL-CAPS in code and in `data-state` (tests, e2e); only the
 * visible word changes. */
const STATE_LABEL: Record<AuthorityState, string> = {
  PREVIEW: 'Preview',
  FOUND: 'Found',
  PENDING: 'Pending',
  DELIVERED: 'Delivered',
  'WOULD EVICT': 'Would remove notes',
  UNAVAILABLE: 'Unavailable',
  ZERO: 'None',
};

export function stateLabel(state: AuthorityState): string {
  return STATE_LABEL[state];
}

export function numberOrUnavailable(value: number | null): string {
  return value === null ? 'unavailable' : String(value);
}

export function isStrictGuidanceDelivery(result: GuidanceAcceptResult): boolean {
  return result.status === 'accepted'
    && result.proposed_type === 'guidance_note'
    && (result.outcome === 'append' || result.outcome === 'would_evict')
    && Number.isInteger(result.version);
}

/** Registry display names keyed by system id, from the same /api/systems read
 * the Connected Systems bar uses. Empty while loading or on error, so every
 * label falls back to the id. */
export function useSystemDisplayNames(): ReadonlyMap<string, string> {
  const systems = useQuery(systemsQueryOptions());
  return useMemo(() => {
    const names = new Map<string, string>();
    for (const system of systems.data?.systems ?? []) {
      const name = (system.name || '').trim();
      if (system.id && name) names.set(system.id, name);
    }
    return names;
  }, [systems.data]);
}

/** "AWS IoT SiteWise (sitewise-direct)": display name first, id second; the
 * bare id when no distinct name is known. */
export function systemDisplayLabel(systemId: string, names: ReadonlyMap<string, string>): string {
  const name = names.get(systemId);
  return name && name !== systemId ? `${name} (${systemId})` : systemId;
}

export function AuthorityTag({ state, label }: { state: AuthorityState; label?: string }) {
  const color = stateColor(state);
  return (
    <span
      className="guidance-authority-tag"
      data-state={state.toLowerCase().replace(' ', '-')}
      style={{
        display: 'inline-block',
        border: `1px solid ${color}`,
        color,
        borderRadius: 3,
        padding: '0.05rem 0.35rem',
        fontFamily: MONO,
        fontSize: '0.64rem',
        fontWeight: 700,
        letterSpacing: '0.04em',
      }}
    >
      {label ?? stateLabel(state)}
    </span>
  );
}
