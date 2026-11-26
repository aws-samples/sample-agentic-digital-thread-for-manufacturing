// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* The metric_definition branch of the proposal-card body (ADR-0065), split
 * out of SlCardBody so the dispatcher stays small. A metric definition says
 * what a NUMBER MEANS, so the body leads with the metric id and offers the
 * computed formula diff (current vs proposed) and — while PENDING — the
 * ADR-0080 dry-run preview. Neither touches the card's existing accept
 * action: the steward queue stays the only door (ADR-0066 D3). */
import type { ProposedMapping } from '../../api/types/proposals';
import { MetricDryRunAction } from './metric-dry-run';
import { MetricDiffExpander } from './proposal-families';

export function MetricDefinitionCardBody({ m }: { m: ProposedMapping }) {
  const metricId = String(m.to_value || '');
  const pending = (m.status || 'pending') === 'pending';
  return (
    <>
      <div className="sl-anno-lead">
        Define the metric <code>{metricId || 'unnamed'}</code>
      </div>
      <div className="sl-impact">
        <b>Impact:</b> once accepted, every answer that asks for this metric resolves through
        this definition. A code-defined metric of the same id always wins.
      </div>
      {m.mapping_id ? <MetricDiffExpander mappingId={m.mapping_id} /> : null}
      {/* ADR-0080: dry-run the PENDING definition as the card it would
          become; read-only, and the accept action is unchanged. */}
      {m.mapping_id && pending ? <MetricDryRunAction mappingId={m.mapping_id} /> : null}
    </>
  );
}
