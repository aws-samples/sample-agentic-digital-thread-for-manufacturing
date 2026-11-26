// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tiny external-store helper (jsx-migration W1.1). No new dependencies —
 * built over the React useSyncExternalStore API.
 *
 * Powers the migration's store slices (chat thread store, dashboard store,
 * onboard flow store, …): imperative event handlers (SSE, polling) mutate a
 * store; React components subscribe through useStore and re-render.
 *
 * Selector contract: the selector result is compared with Object.is by React;
 * a component re-renders only when its selected value changes. Selectors must
 * therefore return stable references (a primitive, a stored sub-object, or a
 * memoized value) — returning a fresh object/array on every call would render
 * on every store change and can loop.
 */
import { useSyncExternalStore } from 'react';

export interface Store<T> {
  /** Current state snapshot. */
  get(): T;
  /** Replace state and notify subscribers (no-op if Object.is-equal). */
  set(next: T): void;
  /** Functional update: set(fn(get())). */
  update(fn: (prev: T) => T): void;
  /** Subscribe to changes; returns an unsubscribe function. */
  subscribe(listener: () => void): () => void;
}

export function createStore<T>(initial: T): Store<T> {
  let state = initial;
  const listeners = new Set<() => void>();

  const get = () => state;
  const set = (next: T) => {
    if (Object.is(next, state)) return;
    state = next;
    // Snapshot the listener set so unsubscribes during notify are safe.
    for (const listener of [...listeners]) listener();
  };
  const update = (fn: (prev: T) => T) => set(fn(state));
  const subscribe = (listener: () => void) => {
    listeners.add(listener);
    return () => {
      listeners.delete(listener);
    };
  };

  return { get, set, update, subscribe };
}

/** Subscribe a component to a store (optionally through a selector).
 * Re-renders only when the selected value changes (Object.is). */
export function useStore<T>(store: Store<T>): T;
export function useStore<T, S>(store: Store<T>, selector: (state: T) => S): S;
export function useStore<T, S>(store: Store<T>, selector?: (state: T) => S): T | S {
  return useSyncExternalStore(
    store.subscribe,
    () => (selector ? selector(store.get()) : store.get()),
    () => (selector ? selector(store.get()) : store.get()),
  );
}
