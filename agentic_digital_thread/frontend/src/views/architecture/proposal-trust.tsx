// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import type { ProposedMapping } from '../../api/types/proposals';

export interface TrustChipView {
  label: string;
  title: string;
}

export function trustChipState(m: ProposedMapping): TrustChipView | null {
  if ((m.auto_accept_block || '').includes('hierarchy_position')) {
    return {
      label: 'positional guess — needs a human check',
      title: m.auto_accept_block || '',
    };
  }
  if (m.target_verified === true && m.verified_scope === 'system') {
    return {
      label: 'target checked',
      title: "A deterministic system-scoped graph check found this proposal's target value.",
    };
  }
  return null;
}

export function TrustChip({ m }: { m: ProposedMapping }) {
  const state = trustChipState(m);
  if (!state) return null;
  const positional = state.label.startsWith('positional');
  const color = positional ? '#b45309' : '#15803d';
  return (
    <span
      className={`sl-trust ${positional ? 'sl-trust--guess' : 'sl-trust--checked'}`}
      title={state.title}
      style={{
        fontSize: 10,
        fontWeight: 600,
        borderRadius: 3,
        padding: '1px 6px',
        whiteSpace: 'nowrap',
        border: `1px solid ${color}`,
        color,
      }}
    >
      {state.label}
    </span>
  );
}
