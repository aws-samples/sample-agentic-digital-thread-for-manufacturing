// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/** Shared vocabulary for request-backed and other asynchronous UI state. */
export type AsyncState<T> =
  | { status: 'idle' }
  | { status: 'loading'; previous?: T }
  | { status: 'ready'; data: T; refreshing?: boolean }
  | { status: 'empty'; reason: 'no-items' | 'not-configured'; message: string }
  | { status: 'no-data'; message: string }
  | { status: 'error'; message: string; retryable: boolean };

export type ReadyState<T> = Extract<AsyncState<T>, { status: 'ready' }>;
export type LoadingState<T> = Extract<AsyncState<T>, { status: 'loading' }>;
export type ErrorState<T> = Extract<AsyncState<T>, { status: 'error' }>;

export function isReadyState<T>(state: AsyncState<T>): state is ReadyState<T> {
  return state.status === 'ready';
}

export function isLoadingState<T>(state: AsyncState<T>): state is LoadingState<T> {
  return state.status === 'loading';
}

export function isErrorState<T>(state: AsyncState<T>): state is ErrorState<T> {
  return state.status === 'error';
}

/** Returns retained data while loading and current data once ready. */
export function asyncStateData<T>(state: AsyncState<T>): T | undefined {
  if (state.status === 'ready') return state.data;
  if (state.status === 'loading') return state.previous;
  return undefined;
}

/** Exhaustiveness helper for consumers that render every async-state variant. */
export function assertNever(value: never): never {
  throw new Error(`Unexpected async state: ${String(value)}`);
}
