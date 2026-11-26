// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useQuery } from '@tanstack/react-query';

import {
  guidanceProposalsQueryOptions,
  type GuidanceSystemWithBase,
} from '../../guidance-queries';
import { GuidanceProposalCard } from './GuidanceProposalCard';
import {
  AuthorityTag,
  compactStyle,
  RED,
} from './guidance-stability-ui';
import { PlacementControl } from './PlacementControl';
import { ReconciliationPanel } from './ReconciliationPanel';

function GuidanceQueue({ isAdmin }: { isAdmin: boolean }) {
  const proposals = useQuery(guidanceProposalsQueryOptions());
  const pending = (proposals.data ?? []).filter((mapping) => mapping.status === 'pending');
  const state = proposals.isError ? 'UNAVAILABLE' : pending.length === 0 ? 'ZERO' : 'PENDING';
  return (
    <section aria-label="Pending guidance delivery">
      <div style={{ display: 'flex', gap: '0.45rem', alignItems: 'center', margin: '0.8rem 0 0.4rem' }}>
        <strong style={{ fontSize: '0.82rem' }}>Guidance delivery queue</strong>
        <AuthorityTag state={state} />
      </div>
      {proposals.isPending && <div style={compactStyle}>Loading pending guidance proposals…</div>}
      {proposals.isError && (
        <div role="alert" style={{ ...compactStyle, color: RED }}>
          Unavailable — guidance proposals could not be loaded. Try again.
        </div>
      )}
      {!proposals.isPending && !proposals.isError && pending.length === 0 && (
        <div style={compactStyle}>None — no guidance proposals are pending delivery.</div>
      )}
      {pending.map((mapping) => (
        <GuidanceProposalCard key={mapping.mapping_id} mapping={mapping} isAdmin={isAdmin} />
      ))}
    </section>
  );
}

export function GuidanceStabilityControls({ systems, isAdmin }: {
  systems: GuidanceSystemWithBase[];
  isAdmin: boolean;
}) {
  return (
    <div id="guidance-stability-controls">
      <ReconciliationPanel enabled={isAdmin} />
      <PlacementControl systems={systems} enabled={isAdmin} />
      <GuidanceQueue isAdmin={isAdmin} />
    </div>
  );
}

export default GuidanceStabilityControls;
