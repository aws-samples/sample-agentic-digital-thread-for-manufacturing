// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* The ONE system display-name resolver for the web UI.
 *
 * Every surface where a person reads a system's name (chat activity lines,
 * call rows, Sources chips, the investigation graph and HUD, Threads rule
 * cards, onboarding card headers) resolves it here, keyed by system_id:
 *
 *   1. the catalog display name (`GET /api/systems` / `GET /api/onboard/catalog`
 *      `name` — for a user-registered system, the name the user entered);
 *   2. else, for an id-shaped value the catalog does not know, a readable
 *      fallback built from the id (`mes-ind` → `MES Ind`);
 *   3. else the value verbatim (it already reads as a name).
 *
 * Display only. Nothing here is sent to the server or read by an agent:
 * tool names, wire ids and API fields stay exactly as they are. The catalog
 * is filled by whoever fetches it (systems-query.ts, onboard-queries.ts), so
 * this module makes no requests of its own. Subscribe with
 * `useSystemNamesVersion()` to re-render when names arrive. */
import { createStore, useStore } from './store';

interface CatalogNames {
  /** Exact system_id → display name. */
  exact: ReadonlyMap<string, string>;
  /** Normalised id (trimmed, lower-case, `_` → `-`) → display name. */
  loose: ReadonlyMap<string, string>;
  /** Every known display name, so a name passed in is returned as-is. */
  names: ReadonlySet<string>;
  /** Bumped on every change (React subscription token). */
  version: number;
}

const EMPTY: CatalogNames = { exact: new Map(), loose: new Map(), names: new Set(), version: 0 };

const store = createStore<CatalogNames>(EMPTY);

const looseKey = (id: string): string => id.trim().toLowerCase().replace(/_/g, '-');

/** Catalog-shaped entries (`{id, name}`; extra keys ignored). */
export interface SystemNameEntry {
  id?: unknown;
  system_id?: unknown;
  name?: unknown;
}

/** Merge catalog names. Entries without a string id or a non-empty name
 * are skipped. Names are only ever added or renamed, never dropped, so an
 * older chat turn that cites a since-removed system keeps its name. */
export function rememberSystemNames(entries: Iterable<SystemNameEntry> | null | undefined): void {
  if (!entries) return;
  const prev = store.get();
  let exact: Map<string, string> | null = null;
  for (const entry of entries) {
    if (!entry || typeof entry !== 'object') continue;
    const rawId = typeof entry.id === 'string' && entry.id ? entry.id : entry.system_id;
    const id = typeof rawId === 'string' ? rawId.trim() : '';
    const name = typeof entry.name === 'string' ? entry.name.trim() : '';
    if (!id || !name) continue;
    if ((exact ?? prev.exact).get(id) === name) continue;
    exact ??= new Map(prev.exact);
    exact.set(id, name);
  }
  if (!exact) return;
  const loose = new Map<string, string>();
  for (const [id, name] of exact) if (!loose.has(looseKey(id))) loose.set(looseKey(id), name);
  // An exact id wins its own loose slot over a sibling that normalises the same.
  for (const [id, name] of exact) if (looseKey(id) === id) loose.set(id, name);
  store.set({ exact, loose, names: new Set(exact.values()), version: prev.version + 1 });
}

/** The catalog display name for a system id, or null when the catalog has
 * none (exact id first, then case / `_`-vs-`-` insensitive). */
export function catalogSystemName(systemId: string | null | undefined): string | null {
  const id = String(systemId ?? '').trim();
  if (!id) return null;
  const s = store.get();
  return s.exact.get(id) ?? s.loose.get(looseKey(id)) ?? null;
}

/** Display names for known ids with no catalog entry yet (before the
 * catalog loads). Generic system types only: a plant's name comes from the
 * catalog, never from this table. */
const KNOWN_FALLBACK_NAMES: Record<string, string> = {
  erp: 'ERP',
  cmms: 'CMMS',
  plm: 'PLM',
  highbyte: 'HighByte',
  'sitewise-direct': 'SiteWise',
};

const capWords = (s: string, sep: string): string =>
  s.split(sep).map((w) => w.charAt(0).toUpperCase() + w.slice(1)).join(' ');

/** Generic system-type acronyms (no plant or customer names) that the
 * fallback spells in capitals: "mes-ind" → "MES Ind", "cmms_main" →
 * "CMMS Main". Any other word is title-cased ("lake-athena" → "Lake Athena"). */
const SYSTEM_TYPE_ACRONYMS = new Set([
  'aps', 'bi', 'cmms', 'crm', 'dcs', 'eam', 'edm', 'ehs', 'erp', 'hmi', 'iot', 'lims', 'mes',
  'mom', 'mrp', 'ot', 'pdm', 'plc', 'plm', 'qms', 'sap', 'scada', 'srm', 'wms',
]);

/** Readable name built from an id the catalog does not know ("mes-ind" /
 * "mes_ind" → "MES Ind"): system-type acronyms in capitals, other words
 * title-cased. Generic: no plant names. */
export function fallbackSystemName(systemId: string): string {
  const id = String(systemId || '').trim();
  if (!id) return '';
  if (KNOWN_FALLBACK_NAMES[id]) return KNOWN_FALLBACK_NAMES[id];
  if (KNOWN_FALLBACK_NAMES[looseKey(id)]) return KNOWN_FALLBACK_NAMES[looseKey(id)];
  const words = id.split(/[-_]/).filter(Boolean);
  if (!words.length) return id;
  return words
    .map((w) => (SYSTEM_TYPE_ACRONYMS.has(w.toLowerCase()) ? w.toUpperCase() : capWords(w, ' ')))
    .join(' ');
}

/** True for an id-shaped value (letters, digits, `-`, `_`; no spaces). */
const ID_SHAPE = /^[A-Za-z0-9][A-Za-z0-9_-]*$/;

/** THE resolver: the name a person reads for `systemId`. Catalog display
 * name first; a value that already is a catalog name, or does not look like
 * an id, comes back verbatim; otherwise the id-derived fallback. */
export function systemName(systemId: string | null | undefined): string {
  const id = String(systemId ?? '').trim();
  if (!id) return '';
  const known = catalogSystemName(id);
  if (known) return known;
  if (store.get().names.has(id) || !ID_SHAPE.test(id)) return id;
  return fallbackSystemName(id);
}

/** React: re-render when catalog names change. Returns a change token
 * (call `systemName()` during render as usual). */
export function useSystemNamesVersion(): number {
  return useStore(store, (s) => s.version);
}

/** Non-React stores that keep labels built from these names (the
 * investigation graph) subscribe here to relabel when names change. The
 * listener runs only when the names actually changed. */
export function subscribeSystemNames(listener: () => void): () => void {
  return store.subscribe(listener);
}

/** Test-only reset. */
export function _resetSystemNames(): void {
  store.set(EMPTY);
}
