// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const conceptsQuery = vi.hoisted(() => ({
  value: {
    isError: false,
    data: {} as unknown,
    refetch: vi.fn(async () => ({ data: {} })),
  },
}));

vi.mock('../views/onboard/onboard-queries', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../views/onboard/onboard-queries')>();
  return { ...actual, useConceptsQuery: () => conceptsQuery.value };
});

import { useStore } from '../lib/store';
import {
  appendMsg,
  appendStream,
  flowStore,
  resetToWelcome,
  selectFlowEntries,
  selectFlowScreen,
  setStreamMd,
  startWizard,
  type FlowEntry,
} from '../views/onboard/flow-store';
import { ObFlow } from '../views/onboard/components/ObFlow';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

type Frame = { id: number; callback: FrameRequestCallback };

let frames: Frame[];
let nextFrameId: number;
let container: HTMLDivElement;
let root: Root;

function flushFrame(): void {
  const pending = frames;
  frames = [];
  for (const frame of pending) frame.callback(performance.now());
}

beforeEach(() => {
  conceptsQuery.value.isError = false;
  conceptsQuery.value.data = {};
  conceptsQuery.value.refetch.mockReset();
  conceptsQuery.value.refetch.mockResolvedValue({ data: {} });
  frames = [];
  nextFrameId = 1;
  vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
    const id = nextFrameId++;
    frames.push({ id, callback });
    return id;
  });
  vi.stubGlobal('cancelAnimationFrame', (id: number) => {
    frames = frames.filter((frame) => frame.id !== id);
  });
  resetToWelcome();
  startWizard();
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
});

describe('onboarding external-store render frequency', () => {
  it('surfaces an uncached concepts failure with a retry action', () => {
    conceptsQuery.value.isError = true;
    conceptsQuery.value.data = undefined;

    act(() => root.render(<ObFlow />));

    expect(container.textContent).toContain('Could not load onboarding concepts.');
    const retry = Array.from(container.querySelectorAll('button')).find(
      (button) => button.textContent === 'Retry',
    );
    expect(retry).toBeDefined();
    act(() => retry?.click());
    expect(conceptsQuery.value.refetch).toHaveBeenCalledOnce();
  });

  it('coalesces a token burst to one publish/render per animation frame', () => {
    const streamId = appendStream();
    let renders = 0;
    let selected: FlowEntry[] = [];

    function EntriesProbe() {
      selected = useStore(flowStore, selectFlowEntries);
      renders++;
      return <span>{selected.length}</span>;
    }

    act(() => root.render(<EntriesProbe />));
    expect(renders).toBe(1);

    act(() => {
      setStreamMd(streamId, 'a');
      setStreamMd(streamId, 'ab');
      setStreamMd(streamId, 'abc');
    });

    expect(frames).toHaveLength(1);
    expect(renders).toBe(1);
    expect(selected[0]).toMatchObject({ kind: 'stream', md: '', typing: true });

    act(() => flushFrame());
    expect(renders).toBe(2);
    expect(selected[0]).toMatchObject({ kind: 'stream', md: 'abc', typing: false });

    act(() => {
      setStreamMd(streamId, 'abcd');
      setStreamMd(streamId, 'abcde');
    });
    expect(frames).toHaveLength(1);
    expect(renders).toBe(2);
    act(() => flushFrame());
    expect(renders).toBe(3);
    expect(selected[0]).toMatchObject({ md: 'abcde' });
  });

  it('publishes structural appends synchronously and preserves unchanged entry identity', () => {
    const firstId = appendMsg('stable');
    const streamId = appendStream();
    const before = flowStore.get().entries;
    let publishes = 0;
    const unsubscribe = flowStore.subscribe(() => publishes++);

    const appendedId = appendMsg('synchronous');
    expect(publishes).toBe(1);
    expect(flowStore.get().entries.at(-1)).toMatchObject({ id: appendedId, md: 'synchronous' });

    setStreamMd(streamId, 'next');
    expect(publishes).toBe(1);
    act(() => flushFrame());
    expect(publishes).toBe(2);

    const after = flowStore.get().entries;
    expect(after.find((entry) => entry.id === firstId)).toBe(before[0]);
    expect(after.find((entry) => entry.id === streamId)).not.toBe(before[1]);
    unsubscribe();
  });

  it('does not rerender a root subscribed to an unrelated slice', () => {
    let rootRenders = 0;

    function ScreenProbe() {
      const screen = useStore(flowStore, selectFlowScreen);
      rootRenders++;
      return <span>{screen}</span>;
    }

    act(() => root.render(<ScreenProbe />));
    expect(rootRenders).toBe(1);

    act(() => {
      appendMsg('unrelated entry');
      flowStore.update((state) => ({ ...state, modal: { kind: 'resetAll' } }));
    });

    expect(rootRenders).toBe(1);
    expect(container.textContent).toBe('flow');
  });
});
