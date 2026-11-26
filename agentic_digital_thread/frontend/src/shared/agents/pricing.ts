// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Pricing reference + formatting helpers for the agents fleet
 * (region-aware, on-demand, per 1K tokens).
 *
 * Pure module: no DOM access at import time so the logic is unit-testable in
 * vitest's node environment. The component resolves the region prefix from
 * window.location.hostname at mount. */

import { requestJson } from '../../api/client';
import type { CatalogModel, ModelCatalog } from '../../api/types/agents';

export interface PriceEntry {
  label: string;
  input: number;
  output: number;
}

export type PricingMap = Record<string, PriceEntry>;

/** Where the current rates live. Every price the fleet and the Admin model
 * selector show is an estimate: model prices are Amazon Bedrock on-demand list
 * prices from the curated catalog, and the Neptune Analytics estimate uses
 * approximate prices (agents_routes.py). */
export const BEDROCK_PRICING_URL = 'https://aws.amazon.com/bedrock/pricing/';
export const NEPTUNE_PRICING_URL = 'https://aws.amazon.com/neptune/pricing/';

/** Infer the geo inference-profile prefix from the hostname, as the catalog
 * spells it (model_catalog.region_prefix): eu-central-1 → eu,
 * ap-southeast-2 → apac, else us. Only the models without a global profile
 * (Nova Lite v1, Nova Pro) use it. */
export function regionPrefixFromHostname(hostname: string): 'us' | 'eu' | 'apac' {
  if (hostname.includes('eu-')) return 'eu';
  if (hostname.includes('ap-')) return 'apac';
  return 'us';
}

const GEO_PREFIXES = ['us', 'eu', 'apac'] as const;

/** The catalog models without a global inference profile: the only ids whose
 * prefix depends on the deployment Region (parity-tested against
 * agents/model_catalog.py). */
const GEO_ONLY_FAMILIES = ['amazon.nova-lite-v1:0', 'amazon.nova-pro-v1:0'];

const PROFILE_PREFIX = /^(global|us|eu|apac|au|jp|in|ap)\./;

/** Region-agnostic family of a model id (inference-profile prefix dropped). */
export function modelFamily(modelId: string): string {
  return modelId.replace(PROFILE_PREFIX, '');
}

/** The Price List bills a model that has a global profile this much more under
 * a geo profile (model_catalog.REGIONAL_PREMIUM, parity-tested). */
export const REGIONAL_PREMIUM = 1.1;

/** The price entry for *modelId*: its exact key, else the entry of the same
 * model under another inference-profile id. An older saved ``us.`` id is the
 * same model as the table's ``global.`` entry, billed at the geo profile's
 * rate (REGIONAL_PREMIUM), as model_catalog.pricing prices it. */
export function priceFor(pricing: PricingMap, modelId: string): PriceEntry | undefined {
  const exact = pricing[modelId];
  if (exact) return exact;
  const family = modelFamily(modelId);
  const key = Object.keys(pricing).find((k) => modelFamily(k) === family);
  if (!key) return undefined;
  const entry = pricing[key];
  const geoIdOfGlobalEntry = key.startsWith('global.') && PROFILE_PREFIX.test(modelId)
    && !modelId.startsWith('global.');
  if (!geoIdOfGlobalEntry) return entry;
  const scale = (v: number) => Math.round(v * REGIONAL_PREMIUM * 1e9) / 1e9;
  return { ...entry, input: scale(entry.input), output: scale(entry.output) };
}

/** The geo prefix for the static fallback. The app is normally served from a
 * CloudFront hostname, which names no Region, so the fleet's own model ids
 * (resolved server-side for the deployment Region) are read first. Only the
 * ids of models without a global profile count: their prefix is the Region's
 * (a lingering older ``us.`` Claude id says nothing about it). With no such
 * id, the hostname decides. */
export function geoPrefixForFleet(
  modelIds: Array<string | undefined>,
  hostname: string,
): 'us' | 'eu' | 'apac' {
  for (const id of modelIds) {
    if (!id || !GEO_ONLY_FAMILIES.includes(modelFamily(id))) continue;
    const prefix = id.split('.', 1)[0];
    const geo = GEO_PREFIXES.find((p) => p === prefix);
    if (geo) return geo;
  }
  return regionPrefixFromHostname(hostname);
}

/** Static fallback used until the curated catalog (/api/admin/models) loads:
 * the same models, labels and list prices as agents/model_catalog.py, keyed
 * by the exact id the catalog resolves each to (pinned by
 * tests/unit/test_frontend_static_pricing_matches_catalog.py): ``global.``
 * for every model with a global inference profile, the Region's geo prefix
 * for the two Nova models without one, and no prefix for the Bedrock Mantle
 * (openai.*) ids, which are not inference profiles. */
export function buildStaticPricing(prefix: string): PricingMap {
  return {
    [`global.anthropic.claude-sonnet-5-5`]:                  { label: 'Claude Sonnet 5.5', input: 0.002, output: 0.010 },
    [`global.anthropic.claude-opus-5-5`]:                    { label: 'Claude Opus 5.5', input: 0.004, output: 0.020 },
    [`global.anthropic.claude-sonnet-5`]:                    { label: 'Claude Sonnet 5', input: 0.002, output: 0.010 },
    [`global.anthropic.claude-sonnet-4-6`]:                  { label: 'Claude Sonnet 4.6', input: 0.003, output: 0.015 },
    [`global.anthropic.claude-sonnet-4-5-20250929-v1:0`]:    { label: 'Claude Sonnet 4.5', input: 0.003, output: 0.015 },
    [`global.anthropic.claude-opus-4-7`]:                    { label: 'Claude Opus 4.7', input: 0.005, output: 0.025 },
    [`global.anthropic.claude-opus-4-8`]:                    { label: 'Claude Opus 4.8', input: 0.005, output: 0.025 },
    ['openai.gpt-5.6-sol']:                                  { label: 'GPT-5.6 Sol (Mantle)', input: 0.005, output: 0.030 },
    ['openai.gpt-5.6-terra']:                                { label: 'GPT-5.6 Terra (Mantle)', input: 0.0025, output: 0.015 },
    [`global.anthropic.claude-haiku-5-5`]:                   { label: 'Claude Haiku 5.5', input: 0.0001, output: 0.0005 },
    [`global.anthropic.claude-haiku-4-5-20251001-v1:0`]:     { label: 'Claude Haiku 4.5', input: 0.001, output: 0.005 },
    [`global.amazon.nova-2-lite-v1:0`]:                      { label: 'Nova Lite v2', input: 0.0003, output: 0.0025 },
    [`${prefix}.amazon.nova-lite-v1:0`]:                     { label: 'Nova Lite v1', input: 0.00006, output: 0.00024 },
    [`${prefix}.amazon.nova-pro-v1:0`]:                      { label: 'Nova Pro', input: 0.0008, output: 0.0032 },
    ['openai.gpt-5.6-luna']:                                 { label: 'GPT-5.6 Luna (Mantle)', input: 0.001, output: 0.006 },
  };
}

/** Merge the curated catalog into a PricingMap. Returns null when the catalog
 * yields no entries (caller keeps the static fallback — exact semantics of
 * refreshPricingFromCatalog). */
export function mergeCatalogPricing(cat: ModelCatalog): PricingMap | null {
  const merged: PricingMap = {};
  const models: CatalogModel[] = [...(cat.orchestrator || []), ...(cat.agent || [])];
  models.forEach((m) => {
    merged[m.id] = { label: m.label, input: m.input_per_1k ?? 0, output: m.output_per_1k ?? 0 };
  });
  return Object.keys(merged).length ? merged : null;
}

/** Injectable raw-response transport retained as a component/test seam. */
export type FetchLike = (url: string, options?: RequestInit) => Promise<Response | null>;

/** Refresh pricing from the curated catalog (single source: /api/admin/models).
 * Production calls use the checked typed JSON request layer; an injected raw
 * transport remains available for focused component tests. */
export async function refreshPricingFromCatalog(
  fallback: PricingMap,
  fetchFn?: FetchLike,
): Promise<PricingMap> {
  try {
    let cat: ModelCatalog;
    if (fetchFn) {
      const response = await fetchFn('/api/admin/models');
      if (!response || !response.ok) return fallback;
      cat = (await response.json()) as ModelCatalog;
    } else {
      cat = await requestJson<ModelCatalog>('/api/admin/models');
    }
    return mergeCatalogPricing(cat) ?? fallback;
  } catch {
    return fallback;
  }
}

// ── Formatting helpers (fmtNum/fmtCost/fmtLatency/scoreColor) ──

export function fmtNum(n: number | null | undefined): string {
  if (n == null || n === 0) return '\u2014';
  return Number(n).toLocaleString();
}

export function fmtCost(n: number | null | undefined): string {
  if (!n) return '\u2014';
  if (n < 0.001) return '$' + Number(n).toFixed(6);
  if (n < 0.01) return '$' + Number(n).toFixed(4);
  return '$' + Number(n).toFixed(2);
}

export function fmtLatency(s: number | null | undefined): string {
  if (!s) return '\u2014';
  return s + 's';
}

export function scoreColor(v: number): 'green' | 'amber' | 'red' {
  if (v >= 0.8) return 'green';
  if (v >= 0.5) return 'amber';
  return 'red';
}

// ── Model display name helper ────────────────────────────────────────────────

export function modelDisplayName(modelId: string | undefined, pricing: PricingMap): string {
  if (!modelId) return 'Unknown';
  // Check the pricing table first (by family for another profile id)
  const entry = priceFor(pricing, modelId);
  if (entry) return entry.label;
  // Fallback: parse from model ID string. Longer versions first: the Sonnet 5
  // id is a prefix of the Sonnet 5.5 id.
  if (modelId.includes('claude-opus-5-5')) return 'Claude Opus 5.5';
  if (modelId.includes('claude-sonnet-5-5')) return 'Claude Sonnet 5.5';
  if (modelId.includes('claude-sonnet-5')) return 'Claude Sonnet 5';
  if (modelId.includes('claude-sonnet-4-6')) return 'Claude Sonnet 4.6';
  if (modelId.includes('claude-sonnet-4-5')) return 'Claude Sonnet 4.5';
  if (modelId.includes('claude-sonnet')) return 'Claude Sonnet';
  if (modelId.includes('claude-haiku-5-5')) return 'Claude Haiku 5.5';
  if (modelId.includes('claude-haiku-4-5')) return 'Claude Haiku 4.5';
  if (modelId.includes('claude-haiku')) return 'Claude Haiku';
  if (modelId.includes('nova-2-lite')) return 'Nova Lite v2';
  if (modelId.includes('nova-lite')) return 'Nova Lite v1';
  if (modelId.includes('nova-pro')) return 'Nova Pro';
  // Strip region prefix and return as-is
  return modelId.replace(/^(global|us|eu|apac|au|jp|in|ap)\./, '');
}

// ── Eval score metadata ──────────────────────────────────────────────────────

export const EVAL_META: Record<string, { label: string; desc: string }> = {
  Conciseness:           { label: 'Conciseness',           desc: 'Response is brief without missing key info' },
  Correctness:           { label: 'Correctness',           desc: 'Factually accurate information' },
  FigureCheck:           { label: 'Figure check',          desc: 'Share of sampled answers whose stated totals and counts are in what the system agents returned (checked in code, no model); answers with no figure to check are counted apart' },
  GoalSuccessRate:       { label: 'Goal Success',          desc: 'Conversation meets user goals' },
  Helpfulness:           { label: 'Helpfulness',           desc: 'Useful and valuable from user perspective' },
  InstructionFollowing:  { label: 'Instruction Following', desc: 'Follows provided system instructions' },
  ResponseRelevance:     { label: 'Response Relevance',    desc: 'Response addresses the user query' },
  ToolParameterAccuracy: { label: 'Tool Params',           desc: 'Accurately extracts parameters from queries' },
  ToolSelectionAccuracy: { label: 'Tool Selection',        desc: 'Selected the appropriate tool for the task' },
};
