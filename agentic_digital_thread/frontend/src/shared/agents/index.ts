// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Public surface of the shared agents-fleet module.
 * Hosts (index #agents-view, admin tab-agents)
 * mount <AgentsFleet containerId=... fetchFn=.../>. */

export { AgentsFleet } from './AgentsFleet';
export type { AgentsFleetProps } from './AgentsFleet';
export { SessionDrilldown } from './SessionDrilldown';
export type { SessionDrilldownProps } from './SessionDrilldown';
export {
  traceSessionDetailQueryOptions,
  traceSessionsQueryOptions,
} from './trace-queries';
export {
  barGeometry,
  fmtDuration,
  fmtK,
  isErrorStatus,
  kindClass,
  maxSessionTokens,
  relativeTime,
  sessionMeta,
  sessionStats,
  spanAriaLabel,
  spanWindow,
  timeTicks,
  tokenSplit,
} from './waterfall';
export type { BarGeometry, SessionMeta, SessionStats, WaterfallWindow } from './waterfall';
export {
  buildStaticPricing,
  EVAL_META,
  fmtCost,
  fmtLatency,
  fmtNum,
  geoPrefixForFleet,
  mergeCatalogPricing,
  modelDisplayName,
  refreshPricingFromCatalog,
  regionPrefixFromHostname,
  scoreColor,
} from './pricing';
export type { FetchLike, PriceEntry, PricingMap } from './pricing';
export {
  fleetSummary,
  formatTotalTokens,
  hoursLabel,
  pricingTableEntries,
  splitFleet,
} from './fleetMetrics';
