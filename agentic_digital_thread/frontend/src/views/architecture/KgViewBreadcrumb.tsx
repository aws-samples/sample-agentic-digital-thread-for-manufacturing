// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* KgViewBreadcrumb — tells the steward WHAT the Graph Explorer currently
 * shows (filtered overview vs neighborhood-of-node) with a one-click return
 * to the filtered view (UI improvement D). The back action restores the
 * in-memory filtered dataset through kg-view-store — no refetch.
 *
 * Strings are plain English matching the panel's hardcoded siblings ("GRAPH
 * EXPLORER", …) — this panel is deliberately outside the chrome-i18n scope.
 * The node label is graph-sourced text; JSX escaping applies. */
import { useStore } from '../../lib/store';
import { kgViewStore, returnToFilteredView } from './kg-view-store';

export function KgViewBreadcrumb() {
  const view = useStore(kgViewStore);
  return (
    <div className="kg-view-breadcrumb">
      {view.mode === 'filtered' ? (
        <span>Viewing: all nodes (filtered)</span>
      ) : (
        <>
          <span>
            Viewing: neighborhood of {view.nodeLabel} · {view.hops} hop
            {view.hops === 1 ? '' : 's'}
          </span>
          <button
            type="button"
            className="kg-breadcrumb-back"
            onClick={() => returnToFilteredView()}
            aria-label="Back to filtered view"
          >
            ← back to filtered view
          </button>
        </>
      )}
    </div>
  );
}
