// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Chat-sidebar resize drag (the legacy drag IIFE, hardened for the
 * SPA). Two environmental killers broke the 1:1 mousedown/mousemove port:
 *
 *  1. A capture-phase document 'mousedown' listener (demo-exit) that
 *     stopPropagation()s swallows the handle's bubble-phase mousedown, so a
 *     drag never starts. Pointer events are a SEPARATE event type — a
 *     'pointerdown' listener on the handle fires regardless of what any
 *     document-level mouse listener does, and preventDefault() on it
 *     suppresses the derived compat mousedown entirely.
 *
 *  2. Sandboxed chart iframes in the thread (#messages sits right next to the
 *     handle): while the cursor is over an iframe, the parent document gets
 *     NO mousemove events, so the drag freezes. setPointerCapture() on the
 *     handle retargets all pointer events to it for the drag's duration, so
 *     moves keep flowing even over iframes.
 *
 * Same clamps and side effects as the legacy IIFE: width ∈ [260px, 80vw],
 * inline width on the sidebar, .dragging class on the handle, body cursor +
 * user-select suppression, onRelease (window.cy.resize) after a real drag.
 * Move/up/cancel listeners are attached per-drag and detached on release. */

export interface ResizeDragOptions {
  /** Minimum sidebar width in px (legacy clamp: 260). */
  minWidth?: number;
  /** Maximum width as a fraction of window.innerWidth (legacy clamp: 0.8). */
  maxWidthFraction?: number;
  /** Called after a completed drag (legacy: window.cy?.resize()). */
  onRelease?: () => void;
}

/** Attach the drag behavior. Returns a detach function that removes the
 * pointerdown listener and tears down any in-flight drag. */
export function attachResizeDrag(
  handle: HTMLElement,
  sidebar: HTMLElement,
  opts: ResizeDragOptions = {},
): () => void {
  const minWidth = opts.minWidth ?? 260;
  const maxWidthFraction = opts.maxWidthFraction ?? 0.8;

  let dragging = false;
  let startX = 0;
  let startW = 0;
  let pointerId: number | undefined;

  const onPointerMove = (e: PointerEvent) => {
    if (!dragging) return;
    const delta = startX - e.clientX; // dragging left = wider
    const maxW = Math.floor(window.innerWidth * maxWidthFraction);
    const newW = Math.min(maxW, Math.max(minWidth, startW + delta));
    sidebar.style.width = newW + 'px';
  };

  const endDrag = () => {
    if (!dragging) return;
    dragging = false;
    handle.classList.remove('dragging');
    document.body.style.cursor = '';
    document.body.style.userSelect = '';
    document.removeEventListener('pointermove', onPointerMove);
    document.removeEventListener('pointerup', endDrag);
    document.removeEventListener('pointercancel', endDrag);
    if (pointerId !== undefined) {
      try {
        handle.releasePointerCapture?.(pointerId);
      } catch {
        /* capture already released (or unsupported in the test DOM) */
      }
      pointerId = undefined;
    }
    opts.onRelease?.();
  };

  const onPointerDown = (e: PointerEvent) => {
    // Left button / primary touch only (button is 0 for both; -1 never
    // occurs on pointerdown).
    if (e.button !== 0) return;
    dragging = true;
    startX = e.clientX;
    startW = sidebar.offsetWidth;
    handle.classList.add('dragging');
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
    // Keep receiving moves even when the cursor crosses chart iframes.
    if (typeof e.pointerId === 'number') {
      pointerId = e.pointerId;
      try {
        handle.setPointerCapture?.(e.pointerId);
      } catch {
        /* unsupported (jsdom) — document listeners still cover the drag */
      }
    }
    document.addEventListener('pointermove', onPointerMove);
    document.addEventListener('pointerup', endDrag);
    document.addEventListener('pointercancel', endDrag);
    // Suppress the derived compat mousedown (keeps the leaked demo-exit
    // mouse listener out of the drag entirely) and text selection.
    e.preventDefault();
  };

  handle.addEventListener('pointerdown', onPointerDown);
  return () => {
    handle.removeEventListener('pointerdown', onPointerDown);
    endDrag();
  };
}
