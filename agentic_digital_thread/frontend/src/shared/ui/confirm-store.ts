// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* askConfirm() — the app's imperative confirmation prompt, replacing the
 * native browser confirm() (which blocked the page and looked nothing like
 * the app).
 *
 *   if (!(await askConfirm({ key, title, body, confirmLabel, tone: 'danger' }))) return;
 *
 * Requests queue in a createStore slice (lib/store.ts); <ConfirmHost/>
 * (ConfirmHost.tsx, mounted next to each <ToastRegion/>) renders the HEAD
 * request through the shared <ConfirmDialog/>, so every prompt gets the same
 * ModalSurface focus trap, Escape/backdrop cancel and focus return to the
 * element that opened it. Imperative non-React modules
 * (views/architecture/proposals.ts) call it the same way components do.
 *
 * Contract:
 *   - resolves true on the confirm button, false on Cancel / Escape /
 *     backdrop click;
 *   - a second request while one is open QUEUES and opens when the first
 *     settles;
 *   - a request whose `key` matches one already open or queued resolves
 *     false at once, so a double-click on the triggering button can never
 *     open two dialogs or run the action twice. The key names the ACTION
 *     AND ITS TARGET (`memory:remove:<recordId>`), never the visible copy:
 *     two different targets with identical wording queue normally;
 *   - unmounting the last host resolves every pending request false;
 *   - NO HOST MOUNTED (a page without one, a unit test that renders a single
 *     component): falls back to the native window.confirm() with the title
 *     and body joined by a blank line, so behavior never silently becomes
 *     "always cancel" or "always proceed". Without a window at all (node test
 *     env) it resolves false. This fallback is the ONE native confirm call
 *     left in src/ (allowlisted by src/test/no-native-dialogs.test.ts). */
import { createStore } from '../../lib/store';

export type ConfirmTone = 'danger' | 'default';

export interface ConfirmOptions {
  /** Identity of the action AND its target, e.g. `memory:remove:<recordId>`
   * or `proposals:accept-all`. A second request with the same key while one
   * is open or queued resolves false (the double-click guard); requests with
   * different keys queue even when their copy is identical. */
  key: string;
  title: string;
  /** Impact statement: what happens, and whether it can be undone. */
  body: string;
  /** Specific verb for the action ("Remove note"), never "OK". */
  confirmLabel: string;
  /** 'danger' for destructive acts (remove, delete, clear, retire, ...). */
  tone?: ConfirmTone;
  /** Defaults to ConfirmDialog's "Cancel". */
  cancelLabel?: string;
}

export interface ConfirmRequest extends ConfirmOptions {
  readonly id: number;
}

export interface ConfirmState {
  readonly queue: readonly ConfirmRequest[];
  /** The host that renders the head request (the first one mounted). */
  readonly primaryHost: number | null;
}

export const confirmStore = createStore<ConfirmState>({ queue: [], primaryHost: null });

const resolvers = new Map<number, (value: boolean) => void>();
const hosts: number[] = [];
let nextRequestId = 0;
let nextHostId = 0;

function sameRequest(a: ConfirmOptions, b: ConfirmOptions): boolean {
  return a.key === b.key;
}

function nativeConfirm({ title, body }: ConfirmOptions): boolean {
  if (typeof window === 'undefined' || typeof window.confirm !== 'function') return false;
  return Boolean(window.confirm(`${title}\n\n${body}`));
}

/** Ask the user to confirm an action. Resolves true only on the confirm button. */
export function askConfirm(options: ConfirmOptions): Promise<boolean> {
  if (hosts.length === 0) return Promise.resolve(nativeConfirm(options));
  if (confirmStore.get().queue.some((request) => sameRequest(request, options))) {
    return Promise.resolve(false);
  }
  const id = ++nextRequestId;
  return new Promise<boolean>((resolve) => {
    resolvers.set(id, resolve);
    confirmStore.update((state) => ({ ...state, queue: [...state.queue, { ...options, id }] }));
  });
}

/** Settle one request (idempotent: an unknown or already-settled id is a no-op). */
export function settleConfirm(id: number, value: boolean): void {
  const resolve = resolvers.get(id);
  if (!resolve) return;
  resolvers.delete(id);
  confirmStore.update((state) => ({
    ...state,
    queue: state.queue.filter((request) => request.id !== id),
  }));
  resolve(value);
}

function settleAll(value: boolean): void {
  for (const request of [...confirmStore.get().queue]) settleConfirm(request.id, value);
}

/** Allocate a host id (one per mounted <ConfirmHost/>). */
export function allocateHostId(): number {
  return ++nextHostId;
}

/** Register a mounted host; returns its unregister function. */
export function registerConfirmHost(hostId: number): () => void {
  if (!hosts.includes(hostId)) hosts.push(hostId);
  confirmStore.update((state) => ({ ...state, primaryHost: hosts[0] ?? null }));
  return () => {
    const index = hosts.indexOf(hostId);
    if (index !== -1) hosts.splice(index, 1);
    confirmStore.update((state) => ({ ...state, primaryHost: hosts[0] ?? null }));
    if (hosts.length === 0) settleAll(false);
  };
}
