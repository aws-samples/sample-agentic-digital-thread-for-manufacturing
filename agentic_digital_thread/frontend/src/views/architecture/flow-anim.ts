// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Marching-ants edge animation shared by the system map (cyMap) and the
 * investigation graph (cy) — _startMarchingAnts and the edge-id sets.
 * Both graph modules register instance getters
 * so a single requestAnimationFrame loop drives all flowing edges. */

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Cy = any;

export const mapActiveEdges = new Set<string>();
export const mapTraversedEdges = new Set<string>();
export const invActiveEdges = new Set<string>();

let _frame: number | null = null;
let _getCyMap: () => Cy = () => null;
let _getCyInv: () => Cy = () => null;

export function registerMapInstance(getter: () => Cy): void {
  _getCyMap = getter;
}

export function registerInvInstance(getter: () => Cy): void {
  _getCyInv = getter;
}

export function antsRunning(): boolean {
  return _frame !== null;
}

/** Any node still carrying the 'querying' pulse class on either instance —
 * keeps the ants loop alive for the Task 1H idle-but-live question pulse,
 * which can be active with NO flowing edges (the between-batches gap). */
function anyQuerying(): boolean {
  const cyMap = _getCyMap();
  const cyInv = _getCyInv();
  return !!(
    (cyMap && cyMap.$('node.querying').length > 0) ||
    (cyInv && cyInv.$('node.querying').length > 0)
  );
}

/** Port of _startMarchingAnts — animates line-dash-offset on flowing edges.
 * The same rAF loop also pulses `.querying` nodes (in-flight tool calls: a
 * tool_start with no tool_result yet — and, Task 1H, the live question node
 * between tool batches) with a breathing overlay glow; the inline
 * overlay-opacity is removed by the graph sync when the class clears. */
export function startMarchingAnts(): void {
  if (_frame) return;
  let offset = 0;
  let phase = 0;
  const tick = () => {
    if (
      mapActiveEdges.size === 0 &&
      mapTraversedEdges.size === 0 &&
      invActiveEdges.size === 0 &&
      !anyQuerying()
    ) {
      _frame = null;
      return;
    }
    offset = (offset + 1) % 28;
    phase += 0.07;
    const pulseOpacity = 0.1 + 0.15 * (0.5 + 0.5 * Math.sin(phase));
    const cyMap = _getCyMap();
    if (cyMap) {
      mapActiveEdges.forEach((id) => {
        const e = cyMap.getElementById(id);
        if (e.length) e.style('line-dash-offset', -offset);
      });
      mapTraversedEdges.forEach((id) => {
        const e = cyMap.getElementById(id);
        if (e.length) e.style('line-dash-offset', -offset);
      });
      cyMap.$('node.querying').style('overlay-opacity', pulseOpacity);
    }
    const cy = _getCyInv();
    invActiveEdges.forEach((id) => {
      const e = cy && cy.getElementById(id);
      if (e && e.length) e.style('line-dash-offset', -offset);
    });
    if (cy) cy.$('node.querying').style('overlay-opacity', pulseOpacity);
    _frame = requestAnimationFrame(tick);
  };
  _frame = requestAnimationFrame(tick);
}

export function stopMarchingAnts(): void {
  if (_frame) {
    cancelAnimationFrame(_frame);
    _frame = null;
  }
}

/** Stop when nothing is left animating (tool_result bookkeeping). */
export function maybeStopAnts(): void {
  if (invActiveEdges.size === 0 && mapActiveEdges.size === 0 && !anyQuerying() && _frame) {
    cancelAnimationFrame(_frame);
    _frame = null;
  }
}
