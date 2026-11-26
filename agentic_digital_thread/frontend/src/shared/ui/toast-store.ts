// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { createStore } from '../../lib/store';

export type ToastKind = 'success' | 'info' | 'warning' | 'error';

export interface ToastOptions {
  durationMs?: number;
  urgent?: boolean;
  /** Makes the toast clickable: activating it (click / Enter / Space) runs
   * this callback and dismisses the toast. */
  onActivate?: () => void;
}

export interface Toast {
  readonly id: string;
  readonly kind: ToastKind;
  readonly message: string;
  readonly durationMs: number;
  readonly urgent: boolean;
  readonly onActivate?: () => void;
}

export interface ToastState {
  readonly toasts: readonly Toast[];
}

const DEFAULT_DURATION_MS = 4000;
export const MAX_TOASTS = 5;

export const toastStore = createStore<ToastState>({ toasts: [] });

interface TimerState {
  handle: ReturnType<typeof setTimeout> | null;
  remainingMs: number;
  expiresAt: number;
}

const timers = new Map<string, TimerState>();
let nextId = 0;

function removeToast(id: string): void {
  toastStore.update((state) => {
    const toasts = state.toasts.filter((toast) => toast.id !== id);
    return toasts.length === state.toasts.length ? state : { toasts };
  });
}

function schedule(id: string, timer: TimerState, delayMs: number): void {
  timer.remainingMs = delayMs;
  timer.expiresAt = Date.now() + delayMs;
  timer.handle = setTimeout(() => {
    timers.delete(id);
    removeToast(id);
  }, delayMs);
}

function cleanupTimer(id: string): void {
  const timer = timers.get(id);
  if (!timer) return;
  if (timer.handle !== null) clearTimeout(timer.handle);
  timers.delete(id);
}

/** Add a toast and return its stable queue id. */
export function push(kind: ToastKind, message: string, options: ToastOptions = {}): string {
  const requestedDuration = options.durationMs ?? DEFAULT_DURATION_MS;
  const durationMs = Number.isFinite(requestedDuration)
    ? Math.max(0, requestedDuration)
    : DEFAULT_DURATION_MS;
  const id = `toast-${++nextId}`;
  const toast: Toast = {
    id,
    kind,
    message,
    durationMs,
    urgent: options.urgent === true,
    ...(options.onActivate ? { onActivate: options.onActivate } : {}),
  };

  const current = toastStore.get().toasts;
  const retained = [...current, toast].slice(-MAX_TOASTS);
  const retainedIds = new Set(retained.map((item) => item.id));
  for (const item of current) {
    if (!retainedIds.has(item.id)) cleanupTimer(item.id);
  }
  toastStore.set({ toasts: retained });

  const timer: TimerState = { handle: null, remainingMs: durationMs, expiresAt: 0 };
  timers.set(id, timer);
  schedule(id, timer, durationMs);
  return id;
}

/** Remove one toast and cancel its pending expiry. */
export function dismiss(id: string): void {
  cleanupTimer(id);
  removeToast(id);
}

/** Remove all queued toasts and cancel every pending expiry. */
export function clear(): void {
  for (const id of [...timers.keys()]) cleanupTimer(id);
  toastStore.set({ toasts: [] });
}

/** Pause one toast's remaining lifetime. Used by the interactive region. */
export function pauseToast(id: string): void {
  const timer = timers.get(id);
  if (!timer || timer.handle === null) return;
  clearTimeout(timer.handle);
  timer.handle = null;
  timer.remainingMs = Math.max(0, timer.expiresAt - Date.now());
}

/** Resume one paused toast from its preserved remaining lifetime. */
export function resumeToast(id: string): void {
  const timer = timers.get(id);
  if (!timer || timer.handle !== null) return;
  if (timer.remainingMs <= 0) {
    dismiss(id);
    return;
  }
  schedule(id, timer, timer.remainingMs);
}
