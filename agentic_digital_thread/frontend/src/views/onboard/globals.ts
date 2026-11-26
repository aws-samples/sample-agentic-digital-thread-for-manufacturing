// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Window-global registration for the onboarding view (jsx-migration W5.1).
 * The inline `onclick="..."` handler strings are GONE — every interaction is
 * a React handler now. The `ob*` window globals remain registered as the
 * documented cross-module shim registry (globals-shim note in the migration
 * plan): the app shell / e2e probes / any external caller can still invoke
 * them, and each delegates to the owning module's store-driven action.
 *
 * Dropped (no cross-module callers — grep-verified; their behavior moved
 * into React component state): updateConnectForm (connect-form type switch),
 * showConceptPicker (per-row React picker), obSkipField (per-row React skip
 * button). startCustomOnboard now takes the ConnectNewModal's React state
 * values, so the bare window shim is no longer callable without them.
 * remapSystem / saveRemappings are gone with the Re-map screen (2026-10): its
 * Save was a full, destructive re-provision of an onboarded system, so no
 * shim may expose it (pinned by onboard-remap-removed.test.tsx).
 * Uses `(window as any)` assignment (no `declare global` merging) to avoid
 * Window-interface collisions with other view modules. */
import {
  obLoadCatalog,
  obRestoreFlow,
  showConnectNewForm,
  showSystemDetail,
  toggleConn,
} from './catalog';
import { obAttachFile } from './chat';
import {
  obAskAbout,
  obDisconnectSystem,
  obGuardStart,
  obReset,
  obResetAll,
  obResetAndConnect,
} from './flow';
import { flowStore, skipAllUnmapped } from './flow-store';
import { obJoinCandidateDecide } from './render';

/** Skip-all shim over the LATEST mapping checkpoint (the old window
 * obSkipAllUnmapped the wizard (re)assigned per checkpoint). */
function obSkipAllUnmappedShim(): void {
  const entries = flowStore.get().entries;
  for (let i = entries.length - 1; i >= 0; i--) {
    const e = entries[i];
    if (e.kind === 'checkpoint' && e.content.kind === 'mappingTable') {
      skipAllUnmapped(e.id);
      return;
    }
  }
}

export function registerOnboardGlobals(): void {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const w = window as any;
  // Catalog + detail
  w.obLoadCatalog = obLoadCatalog;
  w.toggleConn = toggleConn;
  w.showSystemDetail = showSystemDetail;
  w.obRestoreFlow = obRestoreFlow;
  w.showConnectNewForm = showConnectNewForm;
  // Wave 4 (item E): resolve-step join-candidates accept/reject — now the
  // (mappingId, decision) API call; <JoinCandidates/> owns the row state.
  w.obJoinCandidateDecide = obJoinCandidateDecide;
  // Flow lifecycle
  w.obGuardStart = obGuardStart;
  w.obReset = obReset;
  w.obResetAll = obResetAll;
  w.obResetAndConnect = obResetAndConnect;
  w.obDisconnectSystem = obDisconnectSystem;
  w.obAskAbout = obAskAbout;
  // Chat / attachments
  w.obAttachFile = obAttachFile;
  // Map-checkpoint helpers: the confirm gate is store-derived now, so
  // checkMappingState is a no-op shim; skip-all targets the latest mapping
  // checkpoint in the store.
  w.obSkipAllUnmapped = obSkipAllUnmappedShim;
  w.checkMappingState = () => {};
}
