// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Unit tests for the agents-fleet logic (src/shared/agents/*).
 * jsx-migration W5.2: the former
 * string-builder assertions (renderAgentCard/pricingTableHtml/neptuneCardHtml
 * HTML strings) became rendered-DOM assertions over the ./cards.tsx
 * components (<AgentCard/>, <PricingTable/>, <NeptuneCard/>, <TokenBar/>,
 * <LogLine/>); the pure pricing/fleet-shaping helpers stay value-asserted.
 * escAttr is gone (JSX auto-escapes attributes) — its coverage became the
 * display_name-as-text assertions on <AgentCard/>. */
// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { queryKeys } from '../api/query-keys';

import type { AgentInfo } from '../api/types/agents';
import { AgentsFleet } from '../shared/agents/AgentsFleet';
import {
  AgentCard,
  colorizeLogMsg,
  LogLine,
  NeptuneCard,
  PricingTable,
  TokenBar,
} from '../shared/agents/cards';
import {
  fleetSummary,
  formatTotalTokens,
  hoursLabel,
  pricingTableEntries,
  splitFleet,
} from '../shared/agents/fleetMetrics';
import {
  buildStaticPricing,
  fmtCost,
  fmtLatency,
  fmtNum,
  geoPrefixForFleet,
  mergeCatalogPricing,
  modelDisplayName,
  refreshPricingFromCatalog,
  regionPrefixFromHostname,
  scoreColor,
} from '../shared/agents/pricing';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;
let queryClient: QueryClient;

beforeEach(() => {
  // A prior file in the same worker must not leave this real-time harness on
  // fake timers; polling-specific tests opt in again themselves.
  vi.useRealTimers();
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: Infinity, refetchOnWindowFocus: false },
    },
  });
});

afterEach(() => {
  act(() => root.unmount());
  queryClient.clear();
  container.remove();
  if (vi.isFakeTimers()) vi.clearAllTimers();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

function render(el: React.ReactElement) {
  act(() => {
    root.render(<QueryClientProvider client={queryClient}>{el}</QueryClientProvider>);
  });
}

async function flushQueries() {
  await act(async () => {
    await Promise.resolve();
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    await Promise.resolve();
  });
}

function jsonResponse(value: unknown, init: Partial<Response> = {}): Response {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    json: async () => value,
    ...init,
  } as Response;
}

describe('regionPrefixFromHostname', () => {
  it('detects eu / apac and defaults to us (the catalog geo prefixes)', () => {
    expect(regionPrefixFromHostname('app.eu-central-1.example.com')).toBe('eu');
    expect(regionPrefixFromHostname('app.ap-southeast-2.example.com')).toBe('apac');
    expect(regionPrefixFromHostname('app.us-east-1.example.com')).toBe('us');
    expect(regionPrefixFromHostname('d111111abcdef8.cloudfront.net')).toBe('us');
  });
});

describe('geoPrefixForFleet', () => {
  it('reads the geo prefix from the fleet model ids before the hostname', () => {
    const cf = 'd111111abcdef8.cloudfront.net';
    expect(geoPrefixForFleet(['global.anthropic.claude-sonnet-5', 'apac.amazon.nova-pro-v1:0'], cf)).toBe('apac');
    expect(geoPrefixForFleet([undefined, 'eu.amazon.nova-lite-v1:0'], cf)).toBe('eu');
    // Only global / Mantle ids: no geo evidence, so the hostname decides.
    expect(geoPrefixForFleet(['global.anthropic.claude-haiku-5-5', 'openai.gpt-5.6-sol'], cf)).toBe('us');
    expect(geoPrefixForFleet([], 'app.eu-west-1.example.com')).toBe('eu');
  });

  it('takes the prefix only from the models without a global profile', () => {
    // A lingering older us. Claude id (saved before the global switch) says
    // nothing about the Region the Nova geo profiles need.
    const cf = 'd111111abcdef8.cloudfront.net';
    expect(geoPrefixForFleet(['us.anthropic.claude-sonnet-4-6', 'apac.amazon.nova-pro-v1:0'], cf)).toBe('apac');
    expect(geoPrefixForFleet(['us.anthropic.claude-sonnet-4-6'], 'app.eu-west-1.example.com')).toBe('eu');
    expect(geoPrefixForFleet(['us.custom.model-z', 'eu.amazon.nova-pro-v1:0'], cf)).toBe('eu');
  });
});

describe('static pricing fallback', () => {
  it('keys global-profile models global. and the rest with the region prefix', () => {
    const p = buildStaticPricing('eu');
    expect(p['global.anthropic.claude-sonnet-4-6']).toEqual({
      label: 'Claude Sonnet 4.6',
      input: 0.003,
      output: 0.015,
    });
    expect(p['global.anthropic.claude-sonnet-5']).toEqual({
      label: 'Claude Sonnet 5',
      input: 0.002,
      output: 0.010,
    });
    expect(p['global.anthropic.claude-sonnet-5-5']).toEqual({
      label: 'Claude Sonnet 5.5',
      input: 0.002,
      output: 0.010,
    });
    expect(p['global.anthropic.claude-opus-5-5'].label).toBe('Claude Opus 5.5');
    expect(p['global.anthropic.claude-haiku-5-5'].label).toBe('Claude Haiku 5.5');
    expect(p['global.amazon.nova-2-lite-v1:0'].input).toBe(0.0003);
    expect(p['eu.anthropic.claude-sonnet-5']).toBeUndefined();
    // No global profile: the geo profile of the Region.
    expect(p['eu.amazon.nova-pro-v1:0'].label).toBe('Nova Pro');
    expect(buildStaticPricing('apac')['apac.amazon.nova-lite-v1:0'].label).toBe('Nova Lite v1');
    expect(Object.keys(p)).toHaveLength(15);
  });

  it('names a 5.5 model by its own version, not the 5 prefix it shares', () => {
    expect(modelDisplayName('us.anthropic.claude-sonnet-5-5-preview', {})).toBe('Claude Sonnet 5.5');
    expect(modelDisplayName('us.anthropic.claude-sonnet-5-preview', {})).toBe('Claude Sonnet 5');
    expect(modelDisplayName('us.anthropic.claude-opus-5-5-preview', {})).toBe('Claude Opus 5.5');
    expect(modelDisplayName('us.anthropic.claude-haiku-5-5-preview', {})).toBe('Claude Haiku 5.5');
  });

  it('keys Bedrock Mantle models without a region prefix, as the catalog does', () => {
    const p = buildStaticPricing('eu');
    expect(p['openai.gpt-5.6-luna']).toEqual({ label: 'GPT-5.6 Luna (Mantle)', input: 0.001, output: 0.006 });
    expect(p['eu.openai.gpt-5.6-luna']).toBeUndefined();
  });

  it('lists no model the catalog does not offer', () => {
    expect(Object.keys(buildStaticPricing('us')).filter((id) => id.includes('claude-sonnet-4-7'))).toEqual([]);
  });
});

describe('mergeCatalogPricing', () => {
  it('merges orchestrator + agent models keyed by id', () => {
    const merged = mergeCatalogPricing({
      orchestrator: [{ id: 'us.m1', label: 'M1', input_per_1k: 0.003, output_per_1k: 0.015 }],
      agent: [{ id: 'us.m2', label: 'M2', input_per_1k: 0.001, output_per_1k: 0.005 }],
    });
    expect(merged).toEqual({
      'us.m1': { label: 'M1', input: 0.003, output: 0.015 },
      'us.m2': { label: 'M2', input: 0.001, output: 0.005 },
    });
  });

  it('returns null for an empty catalog (caller keeps the static fallback)', () => {
    expect(mergeCatalogPricing({})).toBeNull();
    expect(mergeCatalogPricing({ orchestrator: [], agent: [] })).toBeNull();
  });
});

describe('refreshPricingFromCatalog', () => {
  const fallback = buildStaticPricing('us');

  it('returns the merged catalog when the endpoint responds', async () => {
    const fetchFn = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ orchestrator: [{ id: 'x', label: 'X', input_per_1k: 1, output_per_1k: 2 }] }),
    } as unknown as Response);
    const p = await refreshPricingFromCatalog(fallback, fetchFn);
    expect(fetchFn).toHaveBeenCalledWith('/api/admin/models');
    expect(p).toEqual({ x: { label: 'X', input: 1, output: 2 } });
  });

  it('keeps the static fallback on non-OK, null (401 redirect), empty, or thrown fetch', async () => {
    const notOk = vi.fn().mockResolvedValue({ ok: false } as Response);
    expect(await refreshPricingFromCatalog(fallback, notOk)).toBe(fallback);

    const nullResp = vi.fn().mockResolvedValue(null);
    expect(await refreshPricingFromCatalog(fallback, nullResp)).toBe(fallback);

    const empty = vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) } as unknown as Response);
    expect(await refreshPricingFromCatalog(fallback, empty)).toBe(fallback);

    const boom = vi.fn().mockRejectedValue(new Error('network'));
    expect(await refreshPricingFromCatalog(fallback, boom)).toBe(fallback);
  });
});

describe('formatters', () => {
  it('fmtNum: em-dash for null/0, localized otherwise', () => {
    expect(fmtNum(null)).toBe('\u2014');
    expect(fmtNum(0)).toBe('\u2014');
    expect(fmtNum(1234567)).toBe(Number(1234567).toLocaleString());
  });

  it('fmtCost: precision tiers', () => {
    expect(fmtCost(0)).toBe('\u2014');
    expect(fmtCost(null)).toBe('\u2014');
    expect(fmtCost(0.0004)).toBe('$0.000400');
    expect(fmtCost(0.004)).toBe('$0.0040');
    expect(fmtCost(1.5)).toBe('$1.50');
  });

  it('fmtLatency + scoreColor thresholds', () => {
    expect(fmtLatency(undefined)).toBe('\u2014');
    expect(fmtLatency(2.4)).toBe('2.4s');
    expect(scoreColor(0.8)).toBe('green');
    expect(scoreColor(0.5)).toBe('amber');
    expect(scoreColor(0.49)).toBe('red');
  });
});

describe('modelDisplayName', () => {
  const pricing = buildStaticPricing('us');

  it('prefers the pricing-table label', () => {
    expect(modelDisplayName('global.anthropic.claude-sonnet-4-6', pricing)).toBe('Claude Sonnet 4.6');
  });

  it('falls back to id parsing, then prefix-stripped id', () => {
    expect(modelDisplayName('eu.anthropic.claude-haiku-4-5-x', pricing)).toBe('Claude Haiku 4.5');
    expect(modelDisplayName('eu.amazon.nova-2-lite-v9', pricing)).toBe('Nova Lite v2');
    expect(modelDisplayName('ap.some.custom-model', pricing)).toBe('some.custom-model');
    expect(modelDisplayName('global.some.custom-model', pricing)).toBe('some.custom-model');
    expect(modelDisplayName('apac.some.custom-model', pricing)).toBe('some.custom-model');
    expect(modelDisplayName('us.anthropic.claude-sonnet-4-7-x', {})).toBe('Claude Sonnet');
    expect(modelDisplayName(undefined, pricing)).toBe('Unknown');
  });
});

const ORCH: AgentInfo = {
  runtime_name: 'adt_orchestrator',
  display_name: 'Orchestrator',
  status: 'READY',
  model_id: 'global.anthropic.claude-sonnet-4-6',
  invocations_24h: 42,
  tool_calls_24h: 100,
  input_tokens_24h: 900_000,
  output_tokens_24h: 300_000,
  cost_usd_24h: 1.25,
};
const SYS_A: AgentInfo = {
  runtime_name: 'adt_erp',
  display_name: 'ERP Agent',
  status: 'READY',
  model_id: 'us.amazon.nova-lite-v1:0',
  cost_usd_24h: 0.25,
  observability_url: 'https://console.example/obs',
  logs_url: 'https://console.example/logs',
};

describe('fleet shaping', () => {
  it('splitFleet finds the orchestrator by runtime_name or display_name', () => {
    expect(splitFleet([SYS_A, ORCH]).orch).toBe(ORCH);
    expect(splitFleet([SYS_A, ORCH]).sysAgents).toEqual([SYS_A]);
    const byName = { display_name: 'Orchestrator' } as AgentInfo;
    expect(splitFleet([byName]).orch).toBe(byName);
    expect(splitFleet([SYS_A]).orch).toBeUndefined();
  });

  it('fleetSummary totals cost across the fleet, sessions/tools/tokens from the orchestrator', () => {
    const s = fleetSummary([ORCH, SYS_A], ORCH);
    expect(s.agentCost).toBeCloseTo(1.5);
    expect(s.totalSessions).toBe(42);
    expect(s.totalToolCalls).toBe(100);
    expect(s.orchTotalTok).toBe(1_200_000);
    const none = fleetSummary([SYS_A], undefined);
    expect(none.totalSessions).toBe(0);
    expect(none.orchTotalTok).toBe(0);
  });

  it('fleetSummary Total Tokens counts system agents too, not only the orchestrator', () => {
    const sysWithTokens = { ...SYS_A, input_tokens_24h: 40_000, output_tokens_24h: 2_000 };
    const s = fleetSummary([ORCH, sysWithTokens], ORCH);
    expect(s.orchTotalTok).toBe(1_200_000);
    expect(s.fleetTotalTok).toBe(1_242_000);
    expect(fleetSummary([SYS_A], undefined).fleetTotalTok).toBe(0);
  });

  it('fleetSummary uses the measured fleet totals when the payload has them', () => {
    // One model in both roles, 1,000 in + 100 out at $0.5: it shows in both
    // rows, but the measured totals count it once.
    const fleet = { fleet_input_tokens_24h: 1_000, fleet_output_tokens_24h: 100, fleet_cost_usd_24h: 0.5 };
    const orch = {
      ...ORCH, input_tokens_24h: 1_000, output_tokens_24h: 100, cost_usd_24h: 0.5,
      model_shared_across_roles: true, ...fleet,
    };
    const sys = {
      ...SYS_A, input_tokens_24h: 1_000, output_tokens_24h: 100, cost_usd_24h: 0.5,
      model_shared_across_roles: true, ...fleet,
    };
    const s = fleetSummary([orch, sys], orch);
    expect(s.fleetTotalTok).toBe(1_100);
    expect(s.agentCost).toBeCloseTo(0.5);
    // Idle system agents: the measured total is never erased.
    const idle = fleetSummary([orch, { ...SYS_A, ...fleet }], orch);
    expect(idle.fleetTotalTok).toBe(1_100);
  });

  it('formatTotalTokens: >1M as N.nM, else localized', () => {
    expect(formatTotalTokens(1_200_000)).toBe('1.2M');
    expect(formatTotalTokens(500)).toBe(Number(500).toLocaleString());
    expect(formatTotalTokens(0)).toBe('\u2014');
  });

  it('hoursLabel tiers', () => {
    expect(hoursLabel(8)).toBe('8h');
    expect(hoursLabel(24)).toBe('24h');
    expect(hoursLabel(168)).toBe('1 week');
  });
});

describe('pricingTableEntries', () => {
  const pricing = buildStaticPricing('us');

  it('filters to models in use', () => {
    const entries = pricingTableEntries([ORCH, SYS_A], pricing);
    const ids = entries.map(([id]) => id);
    expect(ids).toContain('global.anthropic.claude-sonnet-4-6');
    expect(ids).toContain('us.amazon.nova-lite-v1:0');
    // In use and priced from the table, not appended as a zero-priced extra.
    expect(entries.find(([id]) => id === 'global.anthropic.claude-sonnet-4-6')![1].input).toBe(0.003);
    expect(ids).not.toContain('us.amazon.nova-pro-v1:0');
  });

  it('shows all pricing entries when no agent has a model_id', () => {
    const entries = pricingTableEntries([{ display_name: 'X' }], pricing);
    expect(entries).toHaveLength(Object.keys(pricing).length);
  });

  it('appends configured models missing from pricing with zero prices', () => {
    const entries = pricingTableEntries([{ model_id: 'us.custom.model-z' }], pricing);
    const custom = entries.find(([id]) => id === 'us.custom.model-z');
    expect(custom).toBeDefined();
    expect(custom![1]).toEqual({ label: 'custom.model-z', input: 0, output: 0 });
  });

  it('prices a model saved under another inference-profile id at that profile\'s rate', () => {
    // An older saved us. id is the same model as the table's global. entry,
    // billed at the geo profile's rate (10% over global, as the Price List).
    const entries = pricingTableEntries([{ model_id: 'us.anthropic.claude-sonnet-5' }], pricing);
    expect(entries).toEqual([
      ['us.anthropic.claude-sonnet-5', { label: 'Claude Sonnet 5', input: 0.0022, output: 0.011 }],
    ]);
  });

  it('lists a model shared by several agents once', () => {
    const haiku = 'us.anthropic.claude-haiku-4-5-20251001-v1:0';
    const custom = 'us.custom.model-z';
    const entries = pricingTableEntries(
      [{ model_id: haiku }, { model_id: haiku }, { model_id: haiku },
       { model_id: custom }, { model_id: custom }],
      pricing,
    );
    expect(entries.map(([id]) => id)).toEqual([haiku, custom]);
    expect(entries[0][1]).toEqual({ label: 'Claude Haiku 4.5', input: 0.0011, output: 0.0055 });
  });
});

describe('card components', () => {
  const pricing = buildStaticPricing('us');

  it('TokenBar: no-data placeholder vs stacked split', () => {
    render(<TokenBar inputTok={0} outputTok={0} />);
    expect(container.textContent).toContain('No token data');
    render(<TokenBar inputTok={750} outputTok={250} />);
    expect(container.textContent).toContain('Tokens: 1,000');
    expect(container.textContent).toContain('▪ In 750');
    expect(container.textContent).toContain('▪ Out 250');
    // Stacked split: two flex-weighted segments, 75/25.
    const segments = container.querySelectorAll<HTMLElement>('div > div > div > div');
    const seg = [...segments].filter((el) => el.style.flexGrow || el.style.flex);
    expect(seg.length).toBe(2);
    expect(seg[0].style.flexGrow || seg[0].style.flex).toContain('75');
    expect(seg[1].style.flexGrow || seg[1].style.flex).toContain('25');
  });

  it('AgentCard: orchestrator gets extra metrics, scores, token bar', () => {
    render(<AgentCard agent={ORCH} pricing={pricing} />);
    expect(container.querySelector('.agent-card-name')!.textContent).toBe('Orchestrator');
    expect(container.textContent).toContain('Tool Calls');
    expect(container.textContent).toContain('Quality Scores (last 7d)');
    expect(container.querySelector('.agent-no-scores')!.textContent).toBe(
      'Evaluation not configured',
    );
    expect(container.querySelector('.agent-status-dot.ready')).not.toBeNull();
    expect(container.textContent).toContain('Claude Sonnet 4.6');
    expect(container.textContent).toContain('Tokens: 1,200,000');
  });

  it('AgentCard: a score without a rate yet (figure check, only no-figure answers) gets no bar', () => {
    const scored: AgentInfo = {
      ...ORCH,
      has_eval: true,
      eval_scores: {
        Correctness: { avg: 0.87, count: 42 },
        FigureCheck: { avg: null, count: 0, no_figures: 3 },
      },
    };
    render(<AgentCard agent={scored} pricing={pricing} />);
    const names = [...container.querySelectorAll('.agent-score-name')].map((el) => el.textContent);
    expect(names).toEqual(['Correctness']);
    expect(container.textContent).toContain('1 metrics');
    expect(container.textContent).not.toContain('NaN');
  });

  it('AgentCard: system agent gets deep-links row and no scores section', () => {
    render(<AgentCard agent={SYS_A} pricing={pricing} />);
    const links = container.querySelectorAll<HTMLAnchorElement>('.agent-card-links a');
    expect(links.length).toBe(2);
    expect(links[0].textContent).toContain('Observability ↗');
    expect(links[0].getAttribute('href')).toBe('https://console.example/obs');
    expect(links[1].textContent).toContain('Logs ↗');
    expect(container.textContent).not.toContain('Quality Scores');
  });

  it('AgentCard: says when its model is shared with the other role, and only then', () => {
    render(<AgentCard agent={{ ...SYS_A, model_shared_across_roles: true }} pricing={pricing} />);
    expect(container.textContent).toContain('Includes tokens from a model the other role also uses');
    render(<AgentCard agent={SYS_A} pricing={pricing} />);
    expect(container.textContent).not.toContain('other role also uses');
  });

  it('AgentCard: hostile display_name renders as text, never as markup (JSX auto-escape)', () => {
    const hostile = { ...SYS_A, display_name: `A "B" <C> & D <img src=x onerror=alert(1)>` };
    render(<AgentCard agent={hostile} pricing={pricing} />);
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('.agent-card-name')!.textContent).toBe(
      `A "B" <C> & D <img src=x onerror=alert(1)>`,
    );
    // The accessible label carries the raw name as an attribute value.
    const obs = container.querySelector('.agent-card-links a')!;
    expect(obs.getAttribute('aria-label')).toBe(
      `Open CloudWatch GenAI Observability for A "B" <C> & D <img src=x onerror=alert(1)>`,
    );
  });

  it('PricingTable renders one row per entry with 5-decimal prices', () => {
    render(<PricingTable entries={[['us.m', { label: 'M', input: 0.003, output: 0.015 }]]} />);
    expect(container.textContent).toContain('Pricing Reference');
    const cells = container.querySelectorAll('tbody td');
    expect(cells.length).toBe(4);
    expect(cells[0].textContent).toBe('M');
    expect(cells[1].textContent).toBe('us.m');
    expect(cells[2].textContent).toBe('0.00300');
    expect(cells[3].textContent).toBe('0.01500');
  });

  it('NeptuneCard renders status, metrics, and pricing footnote', () => {
    render(
      <NeptuneCard
        d={{
          status: 'AVAILABLE',
          graph_id: 'g-123',
          ncu_count: 16,
          provisioned_memory_gb: 16,
          replica_count: 1,
          mncu_price_per_hour: 0.03,
          cost_usd: 23.04,
          cost_ncu: 23.04,
          query_rate_avg: 0.5,
          query_rate_peak: 2,
          cpu_avg: 42.5,
          cpu_peak: 80.1,
          graph_size_mb: 2048,
          edges: 1234,
        }}
        hoursLbl="24h"
      />,
    );
    expect(container.textContent).toContain('Neptune Analytics');
    expect(container.textContent).toContain('AVAILABLE');
    expect(container.textContent).toContain('2.0 GB');
    expect(container.textContent).toContain('g-123');
    expect(container.textContent).toContain('1 replica');
    expect(container.textContent).not.toContain('1 replicas');
    expect(container.textContent).toContain('$0.03/m-NCU-hr · no storage or I/O charge');
    expect(container.textContent).toContain('m-NCUs');
    expect(container.textContent).not.toContain('NCU $0.12/hr');
    expect(container.textContent).toContain('Est. Cost (24h)');
  });

  it('NeptuneCard says so when the region has no price instead of guessing', () => {
    render(<NeptuneCard d={{ status: 'AVAILABLE', ncu_count: 16, mncu_price_per_hour: null }} hoursLbl="24h" />);
    expect(container.textContent).toContain('No price for this region');
  });

  it('NeptuneCard labels a stopped graph priced at the stopped rate', () => {
    render(
      <NeptuneCard
        d={{ status: 'STOPPED', ncu_count: 16, mncu_price_per_hour: 0.003, billing_state: 'stopped' }}
        hoursLbl="24h"
      />,
    );
    expect(container.textContent).toContain('$0.003/m-NCU-hr (stopped rate, whole window)');
  });

  it('colorizeLogMsg severity → color mapping', () => {
    expect(colorizeLogMsg('something failed')).toBe('#f87171');
    expect(colorizeLogMsg('WARNING: slow')).toBe('#fbbf24');
    expect(colorizeLogMsg('task complete')).toBe('#4ade80');
    expect(colorizeLogMsg('Tool #3 called')).toBe('#60a5fa');
    expect(colorizeLogMsg('new Session started')).toBe('#c084fc');
    expect(colorizeLogMsg('plain line')).toBe('#a3e635');
  });

  it('LogLine renders HH:MM:SS + the colorized message as text (no markup injection)', () => {
    const ts = Date.UTC(2026, 6, 14, 12, 30, 45);
    render(<LogLine ts={ts} msg={'ERROR: <b>boom</b> failed'} />);
    const spans = container.querySelectorAll<HTMLElement>('span');
    expect(spans.length).toBe(2);
    expect(spans[0].textContent).toMatch(/^\d{2}:\d{2}:\d{2}$/);
    // Angle brackets stay text — never parsed as elements.
    expect(container.querySelector('b')).toBeNull();
    expect(spans[1].textContent).toBe('ERROR: <b>boom</b> failed');
    // Colorized by severity (jsdom normalizes hex to rgb).
    expect(['#f87171', 'rgb(248, 113, 113)']).toContain(spans[1].style.color);
  });
});

describe('AgentsFleet layout — live log placement (parity with static renderLogTail)', () => {
  /* In the legacy static UI, renderLogTail(containerId) appended the
   * "Orchestrator Live Log" box to the HOST container — AFTER the tree div,
   * full width at the bottom of the dashboard. The 280px `-log-panel` div
   * beside the agents grid was vestigial (nothing rendered into it). These
   * assertions pin that ordering. */
  const mockFleetFetch = () =>
    vi.fn(async (url: string): Promise<Response | null> => {
      const json = (() => {
        if (url.startsWith('/api/admin/models')) return {};
        if (url.startsWith('/api/admin/agents/neptune'))
          return { status: 'AVAILABLE', graph_id: 'g-1', cost_usd: 0 };
        if (url.startsWith('/api/admin/agents/log-tail'))
          return { lines: [{ ts: 1, msg: 'session started' }] };
        if (url.startsWith('/api/admin/agents')) return { agents: [ORCH, SYS_A] };
        throw new Error(`unexpected url: ${url}`);
      })();
      return { ok: true, json: async () => json } as unknown as Response;
    });

  async function renderFleet() {
    const fetchFn = mockFleetFetch();
    render(<AgentsFleet containerId="t" fetchFn={fetchFn} />);
    // Initial fleet completion mounts LogTail, whose own Query and effect need
    // additional turns under parallel suite load. Wait for the rendered data,
    // not a fixed pair of scheduler turns.
    for (let attempt = 0; attempt < 20; attempt += 1) {
      await flushQueries();
      if (container.querySelector('#t-log-body')?.textContent?.includes('session started')) break;
    }
  }

  it('renders the live log BELOW the tree (bottom of the dashboard), never inside the side panel', async () => {
    await renderFleet();

    const tree = container.querySelector('#t-tree');
    expect(tree).not.toBeNull();
    const grid = tree!.querySelector('.agents-grid');
    expect(grid).not.toBeNull();

    const logBody = container.querySelector('#t-log-body');
    expect(logBody).not.toBeNull();
    expect(container.textContent).toContain('Orchestrator Live Log');

    // Not inside the vestigial 280px side panel — and not inside the tree at all.
    expect(container.querySelector('#t-log-panel #t-log-body')).toBeNull();
    expect(tree!.contains(logBody)).toBe(false);

    // The live-log section is a sibling that FOLLOWS the tree div — i.e. it
    // sits below the system-agent cards grid and the pricing table.
    const logSection = logBody!.parentElement!;
    expect(logSection.parentElement).toBe(tree!.parentElement);
    expect(tree!.compareDocumentPosition(logSection) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(grid!.compareDocumentPosition(logBody!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();

    // Pricing table stays inside the tree, above the live log.
    expect(tree!.textContent).toContain('Pricing Reference');
  });

  it('polls the log tail and renders lines (functionality preserved after the move)', async () => {
    await renderFleet();
    const logBody = container.querySelector('#t-log-body')!;
    expect(logBody.textContent).toContain('session started');
    // Pause toggle still wired.
    const pause = container.querySelector<HTMLButtonElement>('#t-log-pause')!;
    expect(pause.textContent).toBe('Pause');
    act(() => pause.click());
    expect(pause.textContent).toBe('Resume');
  });
});


describe('AgentsFleet Query behavior', () => {
  function basePayload(url: string): unknown {
    if (url.startsWith('/api/admin/agents/neptune')) {
      return { status: 'AVAILABLE', graph_id: 'g-1', cost_usd: 2 };
    }
    if (url.startsWith('/api/admin/agents/log-tail')) return { lines: [] };
    if (url.startsWith('/api/admin/agents')) return { agents: [ORCH, SYS_A] };
    if (url.startsWith('/api/admin/models')) return {};
    throw new Error(`unexpected url: ${url}`);
  }

  async function advance(ms: number) {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(ms);
      await Promise.resolve();
    });
  }

  it('starts fleet, Neptune, and pricing concurrently and renders agents before pricing resolves', async () => {
    let resolveModels!: (response: Response) => void;
    const modelsPending = new Promise<Response>((resolve) => {
      resolveModels = resolve;
    });
    const fetchFn = vi.fn((url: string): Promise<Response | null> => {
      if (url === '/api/admin/models') return modelsPending;
      return Promise.resolve(jsonResponse(basePayload(url)));
    });

    render(<AgentsFleet containerId="concurrent" fetchFn={fetchFn} />);
    await flushQueries();
    await flushQueries();

    const started = fetchFn.mock.calls.map(([url]) => url);
    expect(started).toContain('/api/admin/agents?hours=24');
    expect(started).toContain('/api/admin/agents/neptune?hours=24');
    expect(started).toContain('/api/admin/models');
    expect(container.textContent).toContain('ERP Agent');
    expect(container.textContent).toContain('Neptune Analytics');
    expect(container.textContent).toContain('Estimated pricing — model catalog loading');

    await act(async () => {
      resolveModels(
        jsonResponse({
          orchestrator: [
            {
              id: ORCH.model_id,
              label: 'Catalog Sonnet',
              input_per_1k: 0.004,
              output_per_1k: 0.02,
            },
          ],
          agent: [
            {
              id: SYS_A.model_id,
              label: 'Catalog Nova',
              input_per_1k: 0.001,
              output_per_1k: 0.002,
            },
          ],
        }),
      );
      await modelsPending;
    });
    await flushQueries();

    expect(container.textContent).toContain('Catalog Sonnet');
    expect(container.textContent).not.toContain('Estimated pricing');
  });

  it('prices an Asia Pacific Nova Pro agent from the fallback on a regionless hostname', async () => {
    // The app is served from CloudFront (no Region in the hostname; jsdom's is
    // localhost), so the fallback's geo prefix comes from the fleet's own ids.
    const apacNova: AgentInfo = { ...SYS_A, model_id: 'apac.amazon.nova-pro-v1:0' };
    // An older us. Claude id listed first must not decide the Nova prefix.
    const legacyOrch: AgentInfo = { ...ORCH, model_id: 'us.anthropic.claude-sonnet-4-6' };
    const fetchFn = vi.fn((url: string): Promise<Response | null> => {
      if (url === '/api/admin/models') {
        return Promise.resolve(
          jsonResponse({}, { ok: false, status: 503, statusText: 'Service Unavailable' }),
        );
      }
      if (url.startsWith('/api/admin/agents?')) {
        return Promise.resolve(jsonResponse({ agents: [legacyOrch, apacNova] }));
      }
      return Promise.resolve(jsonResponse(basePayload(url)));
    });

    render(<AgentsFleet containerId="apac" fetchFn={fetchFn} />);
    await flushQueries();
    await flushQueries();

    const table = container.querySelector('[aria-label="Agent model pricing"]')!;
    expect(table.textContent).toContain('apac.amazon.nova-pro-v1:0');
    expect(table.textContent).toContain('Nova Pro');
    expect(table.textContent).toContain('0.00080');
    // The older us. Sonnet 4.6 id is priced by its family, at the geo rate.
    expect(table.textContent).toContain('us.anthropic.claude-sonnet-4-6');
    expect(table.textContent).toContain('0.00330');
  });

  it('shows an explicit estimated-pricing label when the catalog fails', async () => {
    const fetchFn = vi.fn((url: string): Promise<Response | null> => {
      if (url === '/api/admin/models') {
        return Promise.resolve(
          jsonResponse({}, { ok: false, status: 503, statusText: 'Service Unavailable' }),
        );
      }
      return Promise.resolve(jsonResponse(basePayload(url)));
    });

    render(<AgentsFleet containerId="degraded" fetchFn={fetchFn} />);
    await flushQueries();
    await flushQueries();

    expect(container.textContent).toContain('ERP Agent');
    expect(container.textContent).toContain('Estimated pricing — model catalog unavailable');
    expect(container.querySelector('[data-testid="degraded-pricing-degraded"]')).not.toBeNull();
  });

  it('uses new typed fleet and Neptune keys and starts both requests on an hours change', async () => {
    const fetchFn = vi.fn((url: string): Promise<Response | null> =>
      Promise.resolve(jsonResponse(basePayload(url))),
    );
    render(<AgentsFleet containerId="hours" fetchFn={fetchFn} />);
    await flushQueries();
    await flushQueries();

    fetchFn.mockClear();
    const eightHours = container.querySelector<HTMLButtonElement>('[data-hours="8"]')!;
    act(() => eightHours.click());
    await flushQueries();

    const requested = fetchFn.mock.calls.map(([url]) => url);
    expect(requested).toContain('/api/admin/agents?hours=8');
    expect(requested).toContain('/api/admin/agents/neptune?hours=8');
    expect(queryClient.getQueryCache().find({ queryKey: queryKeys.admin.fleet(8) })).toBeDefined();
    expect(queryClient.getQueryCache().find({ queryKey: queryKeys.admin.neptune(8) })).toBeDefined();
  });

  it('polls every 10s, pauses while hidden or user-paused, and refetches once on resume', async () => {
    vi.useFakeTimers();
    let visibility: DocumentVisibilityState = 'visible';
    vi.spyOn(document, 'visibilityState', 'get').mockImplementation(() => visibility);
    let logCalls = 0;
    const fetchFn = vi.fn((url: string): Promise<Response | null> => {
      if (url.startsWith('/api/admin/agents/log-tail')) {
        logCalls += 1;
        return Promise.resolve(jsonResponse({ lines: [{ ts: logCalls, msg: `poll-${logCalls}` }] }));
      }
      return Promise.resolve(jsonResponse(basePayload(url)));
    });

    render(<AgentsFleet containerId="poll" fetchFn={fetchFn} />);
    await advance(0);
    await advance(0);
    expect(logCalls).toBe(1);

    await advance(10_000);
    expect(logCalls).toBe(2);

    visibility = 'hidden';
    act(() => document.dispatchEvent(new Event('visibilitychange')));
    await advance(20_000);
    expect(logCalls).toBe(2);

    visibility = 'visible';
    act(() => document.dispatchEvent(new Event('visibilitychange')));
    await advance(10_000);
    expect(logCalls).toBe(3);

    const pause = container.querySelector<HTMLButtonElement>('#poll-log-pause')!;
    act(() => pause.click());
    expect(pause.textContent).toBe('Resume');
    await advance(20_000);
    expect(logCalls).toBe(3);

    act(() => pause.click());
    await advance(0);
    expect(logCalls).toBe(4);
    await advance(0);
    expect(logCalls).toBe(4);
  });

  it('does not overlap log-tail requests for the shared key', async () => {
    vi.useFakeTimers();
    let logCalls = 0;
    let resolveSecond!: (response: Response) => void;
    const secondPending = new Promise<Response>((resolve) => {
      resolveSecond = resolve;
    });
    const fetchFn = vi.fn((url: string): Promise<Response | null> => {
      if (url.startsWith('/api/admin/agents/log-tail')) {
        logCalls += 1;
        if (logCalls === 2) return secondPending;
        return Promise.resolve(jsonResponse({ lines: [{ ts: logCalls, msg: `poll-${logCalls}` }] }));
      }
      return Promise.resolve(jsonResponse(basePayload(url)));
    });

    render(<AgentsFleet containerId="overlap" fetchFn={fetchFn} />);
    await advance(0);
    await advance(0);
    expect(logCalls).toBe(1);

    await advance(10_000);
    expect(logCalls).toBe(2);
    await advance(30_000);
    expect(logCalls).toBe(2);

    await act(async () => {
      resolveSecond(jsonResponse({ lines: [{ ts: 2, msg: 'poll-2' }] }));
      await secondPending;
    });
    await advance(10_000);
    expect(logCalls).toBe(3);
  });

  it('merges duplicate polling snapshots and caps the rendered tail at 100 lines', async () => {
    vi.useFakeTimers();
    let logCalls = 0;
    const first = Array.from({ length: 80 }, (_, index) => ({
      ts: index + 1,
      msg: `line-${index + 1}`,
    }));
    const second = Array.from({ length: 60 }, (_, index) => ({
      ts: index + 61,
      msg: `line-${index + 61}`,
    }));
    const fetchFn = vi.fn((url: string): Promise<Response | null> => {
      if (url.startsWith('/api/admin/agents/log-tail')) {
        logCalls += 1;
        return Promise.resolve(jsonResponse({ lines: logCalls === 1 ? first : second }));
      }
      return Promise.resolve(jsonResponse(basePayload(url)));
    });

    render(<AgentsFleet containerId="bounded" fetchFn={fetchFn} />);
    await advance(0);
    await advance(0);
    await advance(10_000);
    await advance(1);
    await advance(0);
    expect(logCalls).toBe(2);

    const rendered = container.querySelectorAll('#bounded-log-body > div');
    expect(rendered).toHaveLength(100);
    expect(container.querySelector('#bounded-log-body')!.textContent).not.toContain('line-20');
    expect(container.querySelector('#bounded-log-body')!.textContent).toContain('line-21');
    expect(container.querySelector('#bounded-log-body')!.textContent).toContain('line-120');
  });
});
