// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Fit-safety guards — _canFit / _safeFit.
 * Cytoscape's fit()/boundingBox throws when the instance is missing/destroyed,
 * the container is hidden (tab not visible) or has no layout dimensions, or the
 * graph is empty ("Cannot read properties of null (reading isHeadless)"). */

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Cy = any;

/** Returns true only when a fit is safe. */
export function canFit(cyInstance: Cy): boolean {
  try {
    if (!cyInstance) return false;
    if (typeof cyInstance.destroyed === 'function' && cyInstance.destroyed()) return false;
    const container = typeof cyInstance.container === 'function' ? cyInstance.container() : null;
    if (!container) return false;
    // offsetParent === null means an ancestor is display:none (tab hidden).
    if (container.offsetParent === null) return false;
    if (!container.clientWidth || !container.clientHeight) return false;
    if (cyInstance.elements().length === 0) return false; // nothing to fit
    return true;
  } catch {
    return false;
  }
}

/** Safe wrapper for cy.fit(): skips (never throws) when a fit isn't currently
 * possible. `eles` may be null/undefined (fit all) or a collection. */
export function safeFit(cyInstance: Cy, eles: Cy | null | undefined, padding?: number): void {
  if (!canFit(cyInstance)) return;
  try {
    if (eles && eles.length) cyInstance.fit(eles, padding);
    else if (eles && !eles.length) return; // empty collection — nothing to fit
    else cyInstance.fit(undefined, padding);
  } catch {
    /* headless/hidden — skip */
  }
}
