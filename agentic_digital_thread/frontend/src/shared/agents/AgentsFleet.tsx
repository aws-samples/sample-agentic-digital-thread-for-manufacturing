// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* <AgentsFleet/>: the AgentCore agent fleet dashboard. Shared by the
 * index Agents view and the admin AgentCore Metrics tab. TanStack Query owns
 * the fleet, Neptune, model-catalog, and log-tail server state; display state
 * and the user-controlled log pause remain local. */

import { useQuery } from '@tanstack/react-query';
import { useEffect, useMemo, useRef, useState } from 'react';

import type { LogTailLine, NeptuneStats } from '../../api/types/agents';
import {
  fleetQueryOptions,
  logTailQueryOptions,
  mergeLogTailLines,
  modelsQueryOptions,
  neptuneQueryOptions,
} from './agent-queries';
import { AgentCard, LogLine, NeptuneCard, PricingTable } from './cards';
import {
  fleetSummary,
  formatTotalTokens,
  hoursLabel,
  pricingTableEntries,
  splitFleet,
} from './fleetMetrics';
import type { FetchLike } from './pricing';
import {
  buildStaticPricing,
  fmtCost,
  fmtNum,
  geoPrefixForFleet,
  mergeCatalogPricing,
  modelDisplayName,
} from './pricing';
import { SessionDrilldown } from './SessionDrilldown';

export interface AgentsFleetProps {
  /** Host container id — derived ids preserve the legacy DOM/e2e contract. */
  containerId: string;
  /** Injectable raw-response transport retained for focused tests. Production
   * queries use queryJson/requestJson when this seam is absent. */
  fetchFn?: FetchLike;
}

const HOURS_OPTIONS = [
  { hours: 8, label: '8h' },
  { hours: 24, label: '24h' },
  { hours: 168, label: '1 week' },
];

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

export function AgentsFleet({ containerId, fetchFn }: AgentsFleetProps) {
  const [hours, setHours] = useState(24);
  // These observers are created in the same render. Fleet rendering therefore
  // never waits for either Neptune metrics or the model catalog.
  const fleetQuery = useQuery(fleetQueryOptions(hours, fetchFn));
  const neptuneQuery = useQuery(neptuneQueryOptions(hours, fetchFn));
  const modelsQuery = useQuery(modelsQueryOptions(fetchFn));

  const agents = fleetQuery.data?.agents ?? [];
  const fleetModelKey = agents.map((a) => a.model_id ?? '').join('|');
  const staticPricing = useMemo(
    () => buildStaticPricing(geoPrefixForFleet(fleetModelKey.split('|'), window.location.hostname)),
    [fleetModelKey],
  );
  const catalogPricing = useMemo(
    () => (modelsQuery.data ? mergeCatalogPricing(modelsQuery.data) : null),
    [modelsQuery.data],
  );
  const pricing = catalogPricing ?? staticPricing;
  const pricingFallbackLabel = catalogPricing
    ? null
    : modelsQuery.isPending
      ? 'Estimated pricing — model catalog loading'
      : modelsQuery.isError
        ? 'Estimated pricing — model catalog unavailable'
        : 'Estimated pricing — model catalog returned no pricing';

  const { orch, sysAgents } = splitFleet(agents);
  const ready = !fleetQuery.isPending && !fleetQuery.isError && agents.length > 0 && sysAgents.length > 0;
  const neptuneCost = neptuneQuery.data?.cost_usd || 0;

  let tree: JSX.Element;
  if (fleetQuery.isPending) {
    tree = <div className="agents-loading">Loading agent data…</div>;
  } else if (fleetQuery.isError) {
    tree = (
      <div style={{ color: 'var(--red)', padding: '1rem' }}>
        Failed to load agents: {errorMessage(fleetQuery.error)}
      </div>
    );
  } else if (!agents.length) {
    tree = <div className="agents-loading">No AgentCore runtimes found</div>;
  } else if (!sysAgents.length) {
    tree = (
      <div style={{ padding: '2rem', textAlign: 'center', color: 'var(--muted)' }}>
        <div style={{ fontSize: '1.1rem', marginBottom: '0.5rem' }}>No systems onboarded yet</div>
        <div style={{ fontSize: '0.8rem' }}>
          Go to <strong>Onboard</strong> to connect manufacturing systems.
          <br />
          Agent metrics will appear here once systems are onboarded.
        </div>
      </div>
    );
  } else {
    const summary = fleetSummary(agents, orch);
    const orchModelLabel = orch?.model_id
      ? modelDisplayName(orch.model_id, pricing)
      : 'Claude Sonnet';
    tree = (
      <>
        <div className="fleet-summary agents-grid">
          <SummaryCell label="Fleet Sessions" value={fmtNum(summary.totalSessions)} />
          <SummaryCell label="Tool Calls" value={fmtNum(summary.totalToolCalls)} />
          <SummaryCell label="Total Tokens" value={formatTotalTokens(summary.fleetTotalTok)} />
          <SummaryCell label="System Agents" value={String(sysAgents.length)} />
          <div className="fleet-summary-cell">
            <div style={summaryLabelStyle}>Total Est. Cost</div>
            <div
              id={`${containerId}-total-cost`}
              data-agent-cost={summary.agentCost}
              style={{ fontSize: '1.3rem', fontWeight: 700, color: 'var(--accent)' }}
            >
              {fmtCost(summary.agentCost + neptuneCost)}
            </div>
          </div>
        </div>

        {orch && (
          <div className="fleet-primary-grid">
            <div className="fleet-primary-card">
              <div style={sectionLabelStyle}>
                <span style={dotStyle('var(--accent)')} />
                Orchestrator — {orchModelLabel}
              </div>
              <AgentCard agent={orch} pricing={pricing} />
            </div>
            <div className="fleet-primary-card">
              <div style={sectionLabelStyle}>
                <span style={dotStyle('#a78bfa')} />
                Neptune Analytics — Graph Database
              </div>
              <NeptuneMetrics
                containerId={containerId}
                hours={hours}
                stats={neptuneQuery.data}
                pending={neptuneQuery.isPending}
                error={neptuneQuery.error}
              />
            </div>
          </div>
        )}

        <div className="fleet-systems-tree">
          <div className="fleet-tree-connector" aria-hidden="true">
            <div className="fleet-tree-line" />
            <div className="fleet-tree-branch" />
          </div>
          <div className="fleet-systems-content">
            <div style={{ ...sectionLabelStyle, marginBottom: '0.5rem' }}>
              <span style={{ ...dotStyle('#60a5fa'), opacity: 0.7 }} />
              System Agents
              <span style={{ opacity: 0.5, fontSize: '0.72rem' }}>(dispatched by Orchestrator)</span>
            </div>
            <div className="fleet-systems-layout">
              <div className="agents-grid" style={{ flex: 1, minWidth: 0 }}>
                {sysAgents.map((agent, index) => (
                  <AgentCard
                    key={agent.runtime_name || agent.display_name || index}
                    agent={agent}
                    pricing={pricing}
                  />
                ))}
              </div>
              <div id={`${containerId}-log-panel`} className="fleet-log-placeholder" />
            </div>
          </div>
        </div>

        {pricingFallbackLabel && (
          <div
            data-testid={`${containerId}-pricing-degraded`}
            style={{ color: 'var(--amber)', fontSize: '0.72rem', marginTop: '0.75rem' }}
          >
            {pricingFallbackLabel}
          </div>
        )}
        <div className="fleet-table-scroll" role="region" aria-label="Agent model pricing">
          <PricingTable entries={pricingTableEntries(agents, pricing)} />
        </div>
        <SessionDrilldown containerId={containerId} hours={hours} fetchFn={fetchFn} />
      </>
    );
  }

  return (
    <div className="agents-fleet">
      <div className="agents-toolbar">
        <span className="agents-subtitle">Amazon Bedrock AgentCore agent fleet</span>
        <div className="agents-time-filter" id={`${containerId}-time-filter`}>
          {HOURS_OPTIONS.map((option) => (
            <button
              key={option.hours}
              type="button"
              className={hours === option.hours ? 'active' : undefined}
              data-hours={option.hours}
              onClick={() => setHours(option.hours)}
            >
              {option.label}
            </button>
          ))}
        </div>
      </div>
      <div id={`${containerId}-tree`}>{tree}</div>
      {ready && <LogTail containerId={containerId} fetchFn={fetchFn} />}
    </div>
  );
}

const summaryLabelStyle: React.CSSProperties = {
  fontSize: '0.78rem',
  color: 'var(--muted)',
  marginBottom: '0.2rem',
};

const sectionLabelStyle: React.CSSProperties = {
  fontSize: '0.82rem',
  fontWeight: 600,
  color: 'var(--text2)',
  marginBottom: '0.4rem',
  display: 'flex',
  alignItems: 'center',
  gap: '0.5rem',
};

function dotStyle(background: string): React.CSSProperties {
  return { display: 'inline-block', width: 8, height: 8, borderRadius: '50%', background };
}

function SummaryCell({ label, value }: { label: string; value: string }) {
  return (
    <div className="fleet-summary-cell">
      <div style={summaryLabelStyle}>{label}</div>
      <div style={{ fontSize: '1.3rem', fontWeight: 700, color: 'var(--text)' }}>{value}</div>
    </div>
  );
}

function NeptuneMetrics({
  containerId,
  hours,
  stats,
  pending,
  error,
}: {
  containerId: string;
  hours: number;
  stats: NeptuneStats | undefined;
  pending: boolean;
  error: unknown;
}) {
  if (error) {
    return (
      <div id={`${containerId}-neptune-card`} style={neptuneMessageStyle}>
        <span style={{ fontSize: '0.7rem', color: 'var(--muted)' }}>
          Neptune error: {errorMessage(error)}
        </span>
      </div>
    );
  }
  if (pending || !stats) {
    return (
      <div id={`${containerId}-neptune-card`} style={neptuneMessageStyle}>
        <span style={{ fontSize: '0.7rem', color: 'var(--muted)' }}>Loading Neptune metrics…</span>
      </div>
    );
  }
  return (
    <div id={`${containerId}-neptune-card`} style={{ flex: 1 }}>
      <NeptuneCard d={stats} hoursLbl={hoursLabel(hours)} />
    </div>
  );
}

const neptuneMessageStyle: React.CSSProperties = {
  flex: 1,
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
};

function LogTail({ containerId, fetchFn }: { containerId: string; fetchFn?: FetchLike }) {
  const [paused, setPaused] = useState(false);
  const [lines, setLines] = useState<LogTailLine[]>([]);
  const bodyRef = useRef<HTMLDivElement | null>(null);
  const lastDataRef = useRef<unknown>();
  const logQuery = useQuery({
    ...logTailQueryOptions(fetchFn),
    enabled: !paused,
  });

  useEffect(() => {
    if (!logQuery.data || logQuery.data === lastDataRef.current) return;
    lastDataRef.current = logQuery.data;
    setLines((previous) => mergeLogTailLines(previous, logQuery.data.lines || []));
  }, [logQuery.data]);

  useEffect(() => {
    const body = bodyRef.current;
    if (body) body.scrollTop = body.scrollHeight;
  }, [lines]);

  return (
    <div className="fleet-log">
      <div className="fleet-log-header">
        <div className="fleet-log-title">
          <span
            id={`${containerId}-log-dot`}
            style={{
              display: 'inline-block',
              width: 8,
              height: 8,
              borderRadius: '50%',
              background: paused ? '#f97316' : '#22c55e',
            }}
          />
          <span
            style={{
              fontSize: '0.78rem',
              fontWeight: 600,
              color: 'var(--text2)',
            }}
          >
            Orchestrator Live Log
          </span>
        </div>
        <button
          type="button"
          id={`${containerId}-log-pause`}
          onClick={() => setPaused((value) => !value)}
          style={{
            fontSize: '0.72rem',
            color: 'var(--muted)',
            background: 'none',
            border: '1px solid var(--border)',
            borderRadius: 3,
            padding: '0.15rem 0.5rem',
            cursor: 'pointer',
          }}
        >
          {paused ? 'Resume' : 'Pause'}
        </button>
      </div>
      <div
        id={`${containerId}-log-body`}
        ref={bodyRef}
        className="fleet-log-body"
        style={{
          background: '#0a0a0a',
          fontFamily: "'IBM Plex Mono',monospace",
          fontSize: '0.72rem',
          lineHeight: 1.6,
          padding: '0.6rem 0.75rem',
          height: 180,
          overflowY: 'auto',
          color: '#a3e635',
        }}
      >
        {logQuery.isError && lines.length === 0 && (
          <div style={{ color: '#f87171' }}>Log tail unavailable</div>
        )}
        {lines.map((line) => (
          <LogLine key={`${line.ts}\u0000${line.msg}`} ts={line.ts} msg={line.msg} />
        ))}
      </div>
    </div>
  );
}
