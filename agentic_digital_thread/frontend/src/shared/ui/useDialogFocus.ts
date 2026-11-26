// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useEffect, useRef, type RefObject } from 'react';

const FOCUSABLE_SELECTOR = [
  'a[href]',
  'area[href]',
  'button:not([disabled])',
  'input:not([disabled]):not([type="hidden"])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  'iframe',
  'object',
  'embed',
  '[contenteditable="true"]',
  '[tabindex]:not([tabindex="-1"])',
].join(',');

export type DialogFocusTarget = RefObject<HTMLElement> | (() => HTMLElement | null);

export interface DialogFocusOptions {
  open: boolean;
  onClose: () => void;
  initialFocus?: DialogFocusTarget;
}

function isAvailable(element: HTMLElement): boolean {
  if (element.closest('[hidden], [inert], [aria-hidden="true"]')) return false;
  const style = window.getComputedStyle(element);
  return style.display !== 'none' && style.visibility !== 'hidden';
}

function focusableElements(surface: HTMLElement): HTMLElement[] {
  return Array.from(surface.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)).filter(isAvailable);
}

function resolveTarget(target?: DialogFocusTarget): HTMLElement | null {
  if (!target) return null;
  return typeof target === 'function' ? target() : target.current;
}

/** Manage the keyboard focus lifecycle for a conditionally open dialog. */
export function useDialogFocus<T extends HTMLElement>({
  open,
  onClose,
  initialFocus,
}: DialogFocusOptions): RefObject<T> {
  const surfaceRef = useRef<T>(null);
  const closeRef = useRef(onClose);
  const initialFocusRef = useRef(initialFocus);
  closeRef.current = onClose;
  initialFocusRef.current = initialFocus;

  useEffect(() => {
    if (!open) return;

    const surface = surfaceRef.current;
    if (!surface) return;

    const previouslyFocused =
      document.activeElement instanceof HTMLElement ? document.activeElement : null;

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        event.stopPropagation();
        closeRef.current();
        return;
      }
      if (event.key !== 'Tab') return;

      const focusable = focusableElements(surface);
      if (focusable.length === 0) {
        event.preventDefault();
        surface.focus();
        return;
      }

      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const active = document.activeElement;
      const focusIsOutside = !(active instanceof Node) || !surface.contains(active);

      if (event.shiftKey && (active === first || focusIsOutside)) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && (active === last || focusIsOutside)) {
        event.preventDefault();
        first.focus();
      }
    };

    surface.addEventListener('keydown', handleKeyDown);
    const requested = resolveTarget(initialFocusRef.current);
    const target = requested && surface.contains(requested)
      ? requested
      : focusableElements(surface)[0] ?? surface;
    target.focus();
    if (!surface.contains(document.activeElement)) surface.focus();

    return () => {
      surface.removeEventListener('keydown', handleKeyDown);
      if (previouslyFocused?.isConnected) previouslyFocused.focus();
    };
  }, [open]);

  return surfaceRef;
}
