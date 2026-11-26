// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Operations Summary panel (S5.4) — React half of the old summary.ts
 * (jsx-migration W2.1). Renders the #insights-content children from the pure
 * computeOperationsSummary over the store's cards snapshot + the derived
 * line-state headline (the old renderOperationsSummary call sites — snapshot
 * render and renderLineState — collapse into store subscriptions). Shows the
 * static "Loading…" placeholder until the first dashboard snapshot arrives. */
import { useStore } from '../../lib/store';
import { EmptyState, ErrorState, LoadingState, NoDataState } from '../../shared/ui/AsyncStates';
import { computeOperationsSummary, INSIGHT_GLYPHS } from './lib';
import { dashboardStore, deriveLineStateSummary } from './store';
import { loadDashboardData } from './stream';

/** Connected-systems / sites headline: the summary's primary content is WHAT
 * is connected and where (the coverage rollups below are secondary). Renders
 * nothing until /api/systems has loaded. */
function SystemsHeadline() {
  const systems = useStore(dashboardStore, (s) => s.systems);
  if (!systems) return null;
  const connected = systems.filter((s) => s.status === 'connected');
  if (!connected.length) return null;
  const types = [...new Set(connected.map((s) => s.type).filter(Boolean))];
  const sites = [...new Set(connected.map((s) => s.plant).filter(Boolean))];
  const parts = [
    `${connected.length} system${connected.length > 1 ? 's' : ''} connected`
      + (types.length ? ` (${types.join(', ')})` : ''),
  ];
  if (sites.length) parts.push(`${sites.length} site${sites.length > 1 ? 's' : ''}: ${sites.join(', ')}`);
  return <div className="insight-item insight-ok">{parts.join(' \u00b7 ')}</div>;
}

export function OpsSummaryContent() {
  const dashboardData = useStore(dashboardStore, (s) => s.dashboardData);
  const lineState = useStore(dashboardStore, (s) => s.lineState);
  const snapshotFailed = useStore(dashboardStore, (s) => s.snapshotFailed);

  if (dashboardData === null) {
    if (snapshotFailed) {
      return (
        <ErrorState
          message="Unable to load the operations summary."
          onRetry={() => void loadDashboardData()}
          compact
          className="insight-item"
        />
      );
    }
    return (
      <LoadingState
        label="Loading operations summary"
        compact
        className="insight-item"
      />
    );
  }

  const model = computeOperationsSummary(
    dashboardData.cards,
    deriveLineStateSummary(lineState),
  );

  if (model.kind === 'empty') {
    return (
      <EmptyState
        message="No metrics yet — onboard a system to populate the summary."
        compact
        className="insight-item"
      />
    );
  }

  if (model.kind === 'ok') {
    return (
      <>
        <SystemsHeadline />
        <div className="insight-item insight-ok">
          {'\u2713 All clear \u2014 no exceptions across ' + model.count + ' metrics.'}
        </div>
      </>
    );
  }

  return (
    <>
      <SystemsHeadline />
      {model.insights.map((ins, i) => {
        if (ins.text.includes('mapped, no rows yet')) {
          return (
            <NoDataState
              key={i}
              message={ins.text}
              compact
              className="insight-item"
            />
          );
        }
        if (ins.text.includes('unavailable')) {
          return (
            <ErrorState
              key={i}
              message={ins.text}
              onRetry={() => void loadDashboardData()}
              compact
              className="insight-item"
            />
          );
        }
        return (
          <div key={i} className={'insight-item insight-' + ins.level}>
            {(INSIGHT_GLYPHS[ins.level] || '') + ins.text}
          </div>
        );
      })}
    </>
  );
}
