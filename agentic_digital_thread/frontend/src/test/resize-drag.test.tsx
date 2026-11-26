// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tests — chat-sidebar resize drag (resize-drag.ts + ChatSidebar wiring).
 *
 * The original 1:1 mousedown port of the drag IIFE broke in the SPA for
 * environmental reasons (a capture-phase document mousedown listener that
 * stopPropagation()s, and chart iframes eating document mousemove); the fix
 * is a pointer-events drag. Covered here:
 *  - pointerdown+pointermove resizes the sidebar within the legacy clamps
 *    (min 260px, max 80vw);
 *  - pointerup detaches the move listeners and fires onRelease;
 *  - the drag still starts when a capture-phase document 'mousedown'
 *    listener swallows mouse events (the leaked demo-exit listener);
 *  - detach() tears the handler down;
 *  - ChatSidebar: collapsed state hides #resize-handle, and the drag handler
 *    survives collapse/expand cycles. */
// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import ChatSidebar from '../chat/ChatSidebar';
import { attachResizeDrag } from '../chat/resize-drag';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

/** Dispatch a pointer event; jsdom may not ship PointerEvent, so fall back
 * to MouseEvent (the listener only reads type/clientX/button/pointerId). */
function firePointer(
  target: EventTarget,
  type: 'pointerdown' | 'pointermove' | 'pointerup' | 'pointercancel',
  opts: { clientX?: number; button?: number } = {},
): void {
  const Ctor =
    (window as unknown as { PointerEvent?: typeof MouseEvent }).PointerEvent ?? MouseEvent;
  target.dispatchEvent(
    new Ctor(type, {
      bubbles: true,
      cancelable: true,
      clientX: opts.clientX ?? 0,
      button: opts.button ?? 0,
    }),
  );
}

/** Plain handle+sidebar pair with a stubbed offsetWidth (jsdom has no layout). */
function makePair(startWidth = 360): { handle: HTMLElement; sidebar: HTMLElement } {
  const handle = document.createElement('div');
  handle.id = 'resize-handle';
  const sidebar = document.createElement('div');
  sidebar.id = 'chat-sidebar';
  Object.defineProperty(sidebar, 'offsetWidth', { value: startWidth, configurable: true });
  document.body.append(handle, sidebar);
  return { handle, sidebar };
}

afterEach(() => {
  document.body.replaceChildren();
  document.body.style.cursor = '';
  document.body.style.userSelect = '';
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('attachResizeDrag (module)', () => {
  it('pointerdown + pointermove resizes the sidebar within the legacy clamps', () => {
    const { handle, sidebar } = makePair(360);
    const detach = attachResizeDrag(handle, sidebar);
    const maxW = Math.floor(window.innerWidth * 0.8);

    firePointer(handle, 'pointerdown', { clientX: 800 });
    expect(handle.classList.contains('dragging')).toBe(true);
    expect(document.body.style.cursor).toBe('col-resize');

    // Dragging left = wider: 360 + (800-700) = 460.
    firePointer(document, 'pointermove', { clientX: 700 });
    expect(sidebar.style.width).toBe('460px');

    // Far left → clamped to 80vw.
    firePointer(document, 'pointermove', { clientX: -2000 });
    expect(sidebar.style.width).toBe(`${maxW}px`);

    // Far right → clamped to the 260px minimum.
    firePointer(document, 'pointermove', { clientX: 5000 });
    expect(sidebar.style.width).toBe('260px');

    detach();
  });

  it('pointerup detaches the move listeners, restores body styles, fires onRelease', () => {
    const { handle, sidebar } = makePair(360);
    const onRelease = vi.fn();
    const detach = attachResizeDrag(handle, sidebar, { onRelease });

    firePointer(handle, 'pointerdown', { clientX: 800 });
    firePointer(document, 'pointermove', { clientX: 750 });
    expect(sidebar.style.width).toBe('410px');

    firePointer(document, 'pointerup');
    expect(onRelease).toHaveBeenCalledTimes(1);
    expect(handle.classList.contains('dragging')).toBe(false);
    expect(document.body.style.cursor).toBe('');
    expect(document.body.style.userSelect).toBe('');

    // Moves after release must be inert (listeners detached).
    firePointer(document, 'pointermove', { clientX: 100 });
    expect(sidebar.style.width).toBe('410px');
    // And a second pointerup must not re-fire onRelease.
    firePointer(document, 'pointerup');
    expect(onRelease).toHaveBeenCalledTimes(1);

    detach();
  });

  it('pointercancel also ends the drag', () => {
    const { handle, sidebar } = makePair(360);
    const detach = attachResizeDrag(handle, sidebar);

    firePointer(handle, 'pointerdown', { clientX: 800 });
    firePointer(document, 'pointercancel');
    firePointer(document, 'pointermove', { clientX: 100 });
    expect(sidebar.style.width).toBe(''); // no move applied after cancel

    detach();
  });

  it('starts the drag even when a capture-phase document mousedown listener stops propagation (leaked demo-exit listener regression)', () => {
    const { handle, sidebar } = makePair(360);
    const detach = attachResizeDrag(handle, sidebar);

    // Simulate the leaked demo-mode exit listener: capture-phase document
    // mousedown that swallows the event before any bubble handler runs.
    const swallow = (e: Event) => e.stopPropagation();
    document.addEventListener('mousedown', swallow, true);
    try {
      // Browser order: pointerdown fires first, then the compat mousedown.
      firePointer(handle, 'pointerdown', { clientX: 800 });
      handle.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, clientX: 800 }));

      firePointer(document, 'pointermove', { clientX: 700 });
      expect(sidebar.style.width).toBe('460px');
      firePointer(document, 'pointerup');
    } finally {
      document.removeEventListener('mousedown', swallow, true);
      detach();
    }
  });

  it('ignores non-primary buttons and detach() removes the handler', () => {
    const { handle, sidebar } = makePair(360);
    const detach = attachResizeDrag(handle, sidebar);

    firePointer(handle, 'pointerdown', { clientX: 800, button: 2 }); // right button
    expect(handle.classList.contains('dragging')).toBe(false);

    detach();
    firePointer(handle, 'pointerdown', { clientX: 800 });
    expect(handle.classList.contains('dragging')).toBe(false);
    firePointer(document, 'pointermove', { clientX: 700 });
    expect(sidebar.style.width).toBe('');
  });
});

describe('ChatSidebar wiring', () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    // Generic ok-JSON stub for the mount-time fetches (onboard status,
    // memory panel, prompts) — all callers tolerate an empty object.
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({ ok: true, status: 200, json: async () => ({}) }) as Response),
    );
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
  });

  async function mount(): Promise<void> {
    await act(async () => {
      root.render(<ChatSidebar />);
    });
  }

  function drag(handle: HTMLElement, sidebar: HTMLElement, fromX: number, toX: number): void {
    Object.defineProperty(sidebar, 'offsetWidth', { value: 360, configurable: true });
    firePointer(handle, 'pointerdown', { clientX: fromX });
    firePointer(document, 'pointermove', { clientX: toX });
    firePointer(document, 'pointerup');
  }

  it('collapsed state hides the handle; drag keeps working across collapse/expand cycles', async () => {
    await mount();
    const handle = document.getElementById('resize-handle')!;
    const sidebar = document.getElementById('chat-sidebar')!;
    expect(handle.style.display).toBe('');

    // Drag before any collapse: 360 + (800-700) = 460.
    drag(handle, sidebar, 800, 700);
    expect(sidebar.style.width).toBe('460px');

    // Collapse → the handle is hidden.
    act(() => window.toggleChatSidebar!());
    expect(handle.style.display).toBe('none');
    expect(sidebar.classList.contains('collapsed')).toBe(true);

    // Expand → visible again AND the drag handler still works (the handle
    // never unmounts; the attach-once effect must survive the cycle).
    act(() => window.toggleChatSidebar!());
    expect(handle.style.display).toBe('');
    expect(sidebar.classList.contains('collapsed')).toBe(false);

    drag(handle, sidebar, 800, 650);
    expect(sidebar.style.width).toBe('510px');
  });
});
