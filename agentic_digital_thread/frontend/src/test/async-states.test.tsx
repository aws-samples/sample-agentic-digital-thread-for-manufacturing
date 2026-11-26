// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  EmptyState,
  ErrorState,
  LoadingState,
  NoDataState,
  Skeleton,
} from '../shared/ui/AsyncStates';
import {
  assertNever,
  asyncStateData,
  isErrorState,
  isLoadingState,
  isReadyState,
  type AsyncState,
} from '../shared/ui/async-state';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.restoreAllMocks();
});

function render(element: React.ReactElement) {
  act(() => root.render(element));
}

function variantName<T>(state: AsyncState<T>): string {
  switch (state.status) {
    case 'idle':
      return 'idle';
    case 'loading':
      return 'loading';
    case 'ready':
      return state.refreshing ? 'refreshing' : 'ready';
    case 'empty':
      return state.reason;
    case 'no-data':
      return 'no-data';
    case 'error':
      return state.retryable ? 'retryable-error' : 'error';
  }
  return assertNever(state);
}

const ALL_STATES = [
  { status: 'idle' },
  { status: 'loading', previous: { id: 1 } },
  { status: 'ready', data: { id: 2 }, refreshing: true },
  { status: 'empty', reason: 'no-items', message: 'Nothing here' },
  { status: 'empty', reason: 'not-configured', message: 'Configure a system' },
  { status: 'no-data', message: 'No samples yet' },
  { status: 'error', message: 'Unavailable', retryable: true },
] satisfies readonly AsyncState<{ id: number }>[];

describe('AsyncState vocabulary', () => {
  it('exhaustively represents every variant and narrows with helpers', () => {
    expect(ALL_STATES.map(variantName)).toEqual([
      'idle',
      'loading',
      'refreshing',
      'no-items',
      'not-configured',
      'no-data',
      'retryable-error',
    ]);

    expect(isLoadingState(ALL_STATES[1])).toBe(true);
    expect(asyncStateData(ALL_STATES[1])).toEqual({ id: 1 });
    expect(isReadyState(ALL_STATES[2])).toBe(true);
    expect(asyncStateData(ALL_STATES[2])).toEqual({ id: 2 });
    expect(isErrorState(ALL_STATES[6])).toBe(true);
  });
});

describe('shared async-state components', () => {
  it('uses polite status semantics for loading and refreshing, never alert', () => {
    render(<LoadingState label="Refreshing metrics" compact className="panel-refresh" />);

    const status = container.querySelector('[role="status"]');
    expect(status?.textContent).toContain('Refreshing metrics');
    expect(status?.classList.contains('async-state--compact')).toBe(true);
    expect(status?.classList.contains('panel-refresh')).toBe(true);
    expect(container.querySelector('[role="alert"]')).toBeNull();
  });

  it('announces errors assertively only when urgent', () => {
    const retry = vi.fn();
    render(<ErrorState message="Metrics unavailable" onRetry={retry} />);
    expect(container.querySelector('[role="status"]')?.textContent).toContain('Metrics unavailable');
    expect(container.querySelector('[role="alert"]')).toBeNull();

    render(<ErrorState message="Connection lost" onRetry={retry} urgent />);
    expect(container.querySelector('[role="alert"]')?.textContent).toContain('Connection lost');
    expect(container.querySelector('[role="status"]')).toBeNull();
  });

  it('activates Retry by click, Enter, and Space', () => {
    const retry = vi.fn();
    render(<ErrorState message="Could not load" onRetry={retry} />);
    const button = container.querySelector('button')!;

    act(() => button.click());
    act(() => {
      button.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
    });
    act(() => {
      button.dispatchEvent(new KeyboardEvent('keydown', { key: ' ', bubbles: true, cancelable: true }));
    });

    expect(button.getAttribute('type')).toBe('button');
    expect(retry).toHaveBeenCalledTimes(3);
  });

  it('renders empty state with an optional keyboard-native CTA', () => {
    const configure = vi.fn();
    render(<EmptyState message="No systems configured" actionLabel="Configure" onAction={configure} />);

    expect(container.querySelector('[role="status"]')?.textContent).toContain('No systems configured');
    const button = container.querySelector('button')!;
    act(() => button.click());
    expect(configure).toHaveBeenCalledOnce();
  });

  it('keeps honest no-data separate and test-addressable', () => {
    render(<NoDataState message="Mapped successfully, but no samples were returned" />);

    const noData = container.querySelector('[data-testid="async-no-data"]');
    expect(noData).not.toBeNull();
    expect(noData?.getAttribute('role')).toBe('status');
    expect(noData?.classList.contains('async-state--no-data')).toBe(true);
    expect(noData?.textContent).toContain('no samples');
    expect(container.querySelector('[role="alert"]')).toBeNull();
  });

  it('renders decorative skeleton blocks without a text announcement', () => {
    render(<Skeleton lines={3} compact className="card-skeleton" />);

    const skeleton = container.querySelector('[data-testid="async-skeleton"]');
    expect(skeleton?.getAttribute('aria-hidden')).toBe('true');
    expect(skeleton?.classList.contains('card-skeleton')).toBe(true);
    expect(skeleton?.querySelectorAll('.async-skeleton__line')).toHaveLength(3);
    expect(skeleton?.textContent).toBe('');
    expect(container.querySelector('[role="status"], [role="alert"]')).toBeNull();
  });
});
