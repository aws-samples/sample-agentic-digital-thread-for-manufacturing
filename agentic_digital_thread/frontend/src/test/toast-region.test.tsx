// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ToastRegion } from '../shared/ui/ToastRegion';
import { clear, MAX_TOASTS, push } from '../shared/ui/toast-store';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  vi.useFakeTimers();
  clear();
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => root.render(<ToastRegion />));
});

afterEach(() => {
  act(() => root.unmount());
  clear();
  container.remove();
  vi.useRealTimers();
});

function toastById(id: string): HTMLElement | null {
  return document.querySelector(`[data-toast-id="${id}"]`);
}

describe('<ToastRegion/>', () => {
  it('renders queued text in the polite status region and expires it', () => {
    let id = '';
    act(() => {
      id = push('success', 'Saved successfully', { durationMs: 1000 });
    });
    const status = document.querySelector('[role="status"]') as HTMLElement;
    expect(status.getAttribute('aria-live')).toBe('polite');
    expect(status.textContent).toContain('Saved successfully');

    act(() => vi.advanceTimersByTime(999));
    expect(toastById(id)).not.toBeNull();
    act(() => vi.advanceTimersByTime(1));
    expect(toastById(id)).toBeNull();
  });

  it('bounds the immutable queue and drops the oldest toast', () => {
    const ids: string[] = [];
    act(() => {
      for (let i = 0; i < MAX_TOASTS + 1; i += 1) {
        ids.push(push('info', `Notice ${i}`, { durationMs: 60_000 }));
      }
    });
    expect(document.querySelectorAll('[data-toast-id]')).toHaveLength(MAX_TOASTS);
    expect(toastById(ids[0])).toBeNull();
    expect(toastById(ids.at(-1)!)).not.toBeNull();
  });

  it('pauses expiry while hovered and resumes with the remaining duration', () => {
    let id = '';
    act(() => {
      id = push('warning', 'Inspect this warning', { durationMs: 1000 });
      vi.advanceTimersByTime(400);
    });
    const toast = toastById(id)!;
    act(() => {
      toast.dispatchEvent(new MouseEvent('mouseover', { bubbles: true }));
      vi.advanceTimersByTime(2000);
    });
    expect(toastById(id)).not.toBeNull();

    act(() => {
      toast.dispatchEvent(new MouseEvent('mouseout', { bubbles: true, relatedTarget: document.body }));
      vi.advanceTimersByTime(599);
    });
    expect(toastById(id)).not.toBeNull();
    act(() => vi.advanceTimersByTime(1));
    expect(toastById(id)).toBeNull();
  });

  it('separates urgent errors into an assertive alert region', () => {
    act(() => {
      push('info', 'Background update', { durationMs: 60_000 });
      push('error', 'Immediate failure', { durationMs: 60_000, urgent: true });
    });
    const status = document.querySelector('[role="status"]') as HTMLElement;
    const alert = document.querySelector('[role="alert"]') as HTMLElement;
    expect(status.textContent).toContain('Background update');
    expect(status.textContent).not.toContain('Immediate failure');
    expect(alert.getAttribute('aria-live')).toBe('assertive');
    expect(alert.textContent).toContain('Immediate failure');
    expect(alert.querySelector('[data-toast-kind="error"]')).not.toBeNull();
  });

  it('dismisses a toast from the keyboard-operable close button', () => {
    let id = '';
    act(() => {
      id = push('error', 'Dismiss me', { durationMs: 60_000 });
    });
    const button = toastById(id)!.querySelector('button') as HTMLButtonElement;
    button.focus();
    act(() => {
      button.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
    });
    expect(toastById(id)).toBeNull();
  });

  it('activatable toast: click runs onActivate and dismisses', () => {
    const onActivate = vi.fn();
    let id = '';
    act(() => {
      id = push('info', 'Response ready. Choose to view it.', { durationMs: 60_000, onActivate });
    });
    const card = toastById(id)!;
    expect(card.getAttribute('role')).toBe('button');
    expect(card.getAttribute('tabindex')).toBe('0');
    expect(card.classList.contains('toast-activatable')).toBe(true);
    act(() => {
      card.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    });
    expect(onActivate).toHaveBeenCalledTimes(1);
    expect(toastById(id)).toBeNull();
  });

  it('activatable toast: Enter activates; a plain toast is not interactive', () => {
    const onActivate = vi.fn();
    let activatableId = '';
    let plainId = '';
    act(() => {
      activatableId = push('info', 'Open me', { durationMs: 60_000, onActivate });
      plainId = push('info', 'Just a notice', { durationMs: 60_000 });
    });
    const card = toastById(activatableId)!;
    act(() => {
      card.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
    });
    expect(onActivate).toHaveBeenCalledTimes(1);
    expect(toastById(activatableId)).toBeNull();

    const plain = toastById(plainId)!;
    expect(plain.getAttribute('role')).toBeNull();
    expect(plain.classList.contains('toast-activatable')).toBe(false);
  });

  it('activatable toast: the dismiss × does NOT run onActivate', () => {
    const onActivate = vi.fn();
    let id = '';
    act(() => {
      id = push('info', 'Open me', { durationMs: 60_000, onActivate });
    });
    const button = toastById(id)!.querySelector('button.toast-dismiss') as HTMLButtonElement;
    act(() => {
      button.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    });
    expect(onActivate).not.toHaveBeenCalled();
    expect(toastById(id)).toBeNull();
  });
});
