// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tests for the external-store helper (jsx-migration W1.1):
 * createStore get/set/update/subscribe semantics and useStore selector
 * stability (a component re-renders only when its selected slice changes).
 * Plain .ts file → components built with createElement, no JSX. */
// @vitest-environment jsdom
import { act, createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { createStore, useStore } from '../lib/store';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

describe('createStore', () => {
  it('get returns the initial state', () => {
    const store = createStore({ n: 1 });
    expect(store.get()).toEqual({ n: 1 });
  });

  it('set replaces state and notifies subscribers', () => {
    const store = createStore(0);
    const listener = vi.fn();
    store.subscribe(listener);
    store.set(42);
    expect(store.get()).toBe(42);
    expect(listener).toHaveBeenCalledTimes(1);
  });

  it('set with an Object.is-equal value does not notify', () => {
    const state = { n: 1 };
    const store = createStore(state);
    const listener = vi.fn();
    store.subscribe(listener);
    store.set(state); // same reference
    expect(listener).not.toHaveBeenCalled();
  });

  it('update applies a functional transform and notifies', () => {
    const store = createStore({ count: 1, label: 'a' });
    const listener = vi.fn();
    store.subscribe(listener);
    store.update((prev) => ({ ...prev, count: prev.count + 1 }));
    expect(store.get()).toEqual({ count: 2, label: 'a' });
    expect(listener).toHaveBeenCalledTimes(1);
  });

  it('unsubscribe stops notifications', () => {
    const store = createStore(0);
    const listener = vi.fn();
    const unsubscribe = store.subscribe(listener);
    store.set(1);
    unsubscribe();
    store.set(2);
    expect(listener).toHaveBeenCalledTimes(1);
  });

  it('notifies every subscriber, and unsubscribing during notify is safe', () => {
    const store = createStore(0);
    const calls: string[] = [];
    const unsubA = store.subscribe(() => {
      calls.push('a');
      unsubA(); // self-unsubscribe mid-notify must not break iteration
    });
    store.subscribe(() => calls.push('b'));
    store.set(1);
    store.set(2);
    expect(calls).toEqual(['a', 'b', 'b']);
  });
});

describe('useStore', () => {
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
  });

  it('renders the current state and re-renders on set', () => {
    const store = createStore({ label: 'first' });
    function View() {
      const state = useStore(store);
      return createElement('span', { id: 'out' }, state.label);
    }
    act(() => root.render(createElement(View)));
    expect(container.querySelector('#out')!.textContent).toBe('first');

    act(() => store.set({ label: 'second' }));
    expect(container.querySelector('#out')!.textContent).toBe('second');
  });

  it('selector: re-renders only when the selected slice changes (stability)', () => {
    const store = createStore({ count: 0, noise: 0 });
    let renders = 0;
    function CountView() {
      const count = useStore(store, (s) => s.count);
      renders += 1;
      return createElement('span', { id: 'count' }, String(count));
    }
    act(() => root.render(createElement(CountView)));
    expect(renders).toBe(1);
    expect(container.querySelector('#count')!.textContent).toBe('0');

    // Unselected slice changes → selected value Object.is-equal → no re-render.
    act(() => store.update((s) => ({ ...s, noise: s.noise + 1 })));
    act(() => store.update((s) => ({ ...s, noise: s.noise + 1 })));
    expect(renders).toBe(1);

    // Selected slice changes → re-render with the new value.
    act(() => store.update((s) => ({ ...s, count: s.count + 1 })));
    expect(renders).toBe(2);
    expect(container.querySelector('#count')!.textContent).toBe('1');
  });

  it('two components share one store', () => {
    const store = createStore({ a: 'x', b: 'y' });
    function AView() {
      const a = useStore(store, (s) => s.a);
      return createElement('span', { id: 'a' }, a);
    }
    function BView() {
      const b = useStore(store, (s) => s.b);
      return createElement('span', { id: 'b' }, b);
    }
    act(() =>
      root.render(createElement('div', null, createElement(AView), createElement(BView))),
    );
    act(() => store.update((s) => ({ ...s, b: 'z' })));
    expect(container.querySelector('#a')!.textContent).toBe('x');
    expect(container.querySelector('#b')!.textContent).toBe('z');
  });

  it('unmounted components are unsubscribed (no leaked notifications)', () => {
    const store = createStore(0);
    function View() {
      const n = useStore(store);
      return createElement('span', null, String(n));
    }
    act(() => root.render(createElement(View)));
    act(() => root.unmount());
    // After unmount the store must have no listeners left that throw/render.
    expect(() => act(() => store.set(5))).not.toThrow();
    // Remount for the shared afterEach unmount.
    root = createRoot(container);
  });
});
