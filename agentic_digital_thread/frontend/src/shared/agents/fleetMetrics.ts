// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Fleet metric shaping — pure logic extracted from the fleet loader
 * (loadAgentsInto _load()). Unit-testable in node. */

import type { AgentInfo } from '../../api/types/agents';
import type { PricingMap, PriceEntry } from './pricing';
import { modelDisplayName, priceFor } from './pricing';

/** Orchestrator/system-agent split — the exact original predicate. */
export function splitFleet(agents: AgentInfo[]): {
  orch: AgentInfo | undefined;
  sysAgents: AgentInfo[];
} {
  const orch = agents.find(
    (a) => a.runtime_name === 'adt_orchestrator' || a.display_name === 'Orchestrator',
  );
  const sysAgents = agents.filter((a) => a !== orch);
  return { orch, sysAgents };
}

export interface FleetSummary {
  /** Sum of every runtime's cost (Neptune added separately in the header). */
  agentCost: number;
  totalSessions: number;
  totalToolCalls: number;
  orchInputTok: number;
  orchOutputTok: number;
  orchTotalTok: number;
  /** Every runtime's input + output tokens (orchestrator AND system agents). */
  fleetTotalTok: number;
}

/** Summary-header totals (_load()). */
export function fleetSummary(agents: AgentInfo[], orch: AgentInfo | undefined): FleetSummary {
  // Bedrock reports tokens per model, not per runtime, so a model bucket in
  // both roles' pools shows in both roles' rows. The cloud fleet sends the
  // measured model-level totals (each bucket counted once) on every row; use
  // them, and sum the rows only for a payload without them (the local fleet).
  const measured = agents.find((a) => a.fleet_input_tokens_24h != null);
  const agentCost = measured
    ? measured.fleet_cost_usd_24h || 0
    : agents.reduce((s, a) => s + (a.cost_usd_24h || 0), 0);
  const fleetTotalTok = measured
    ? (measured.fleet_input_tokens_24h || 0) + (measured.fleet_output_tokens_24h || 0)
    : agents.reduce((s, a) => s + (a.input_tokens_24h || 0) + (a.output_tokens_24h || 0), 0);
  const totalSessions = orch ? orch.invocations_24h || 0 : 0;
  const totalToolCalls = orch ? orch.tool_calls_24h || 0 : 0;
  const orchInputTok = orch ? orch.input_tokens_24h || 0 : 0;
  const orchOutputTok = orch ? orch.output_tokens_24h || 0 : 0;
  return {
    agentCost,
    totalSessions,
    totalToolCalls,
    orchInputTok,
    orchOutputTok,
    orchTotalTok: orchInputTok + orchOutputTok,
    fleetTotalTok,
  };
}

/** Total-tokens header cell: > 1M shown as `N.nM`, else localized number. */
export function formatTotalTokens(total: number): string {
  if (total > 1e6) return (total / 1e6).toFixed(1) + 'M';
  if (total == null || total === 0) return '\u2014';
  return Number(total).toLocaleString();
}

/** Time-filter label (loadNeptuneMetrics hoursLabel). */
export function hoursLabel(hours: number): string {
  return hours >= 168 ? '1 week' : hours >= 24 ? '24h' : hours + 'h';
}

/** Pricing-table rows: only models actually in use by the fleet (all pricing
 * entries when none matched), plus configured models missing from the pricing
 * map appended (priced by family when possible, else with zero prices). */
export function pricingTableEntries(
  agents: AgentInfo[],
  pricing: PricingMap,
): Array<[string, PriceEntry]> {
  const modelsInUse = new Set<string>();
  if (agents && agents.length) {
    agents.forEach((a) => {
      if (a.model_id) modelsInUse.add(a.model_id);
    });
  }
  const entries: Array<[string, PriceEntry]> =
    modelsInUse.size > 0
      ? Object.entries(pricing).filter(([id]) => modelsInUse.has(id))
      : Object.entries(pricing);
  // If configured models aren't in the pricing map, still show them, once per
  // model id: priced by family when the map holds the same model under
  // another profile id, else with zero prices.
  modelsInUse.forEach((id) => {
    if (!pricing[id]) {
      entries.push([
        id,
        priceFor(pricing, id) ?? { label: modelDisplayName(id, pricing), input: 0, output: 0 },
      ]);
    }
  });
  return entries;
}
