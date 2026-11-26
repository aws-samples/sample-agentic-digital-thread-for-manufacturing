// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ChatThread } from '../chat/ChatThread';
import { createThreadStore, type ThreadStore } from '../chat/thread-store';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let host: HTMLDivElement;
let root: Root;
let store: ThreadStore;
let scrollHeight: number;
let nextFrameId: number;
let frames: Map<number, FrameRequestCallback>;

beforeEach(() => {
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
  nextFrameId = 1;
  frames = new Map();
  vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
    const id = nextFrameId++;
    frames.set(id, callback);
    return id;
  });
  vi.stubGlobal('cancelAnimationFrame', (id: number) => {
    frames.delete(id);
  });
  store = createThreadStore();
  scrollHeight = 1_000;
  Object.defineProperty(host, 'scrollHeight', {
    configurable: true,
    get: () => scrollHeight,
  });
  Object.defineProperty(host, 'clientHeight', {
    configurable: true,
    value: 200,
  });
});

afterEach(() => {
  act(() => root.unmount());
  host.remove();
  vi.unstubAllGlobals();
});

function renderThread() {
  const containerRef = { current: host as HTMLElement };
  act(() => root.render(<ChatThread store={store} containerRef={containerRef} />));
}

function scrollTo(top: number) {
  act(() => {
    host.scrollTop = top;
    host.dispatchEvent(new Event('scroll'));
  });
}

function flushAnimationFrame() {
  act(() => {
    const pending = [...frames.values()];
    frames.clear();
    pending.forEach((callback) => callback(16));
  });
}

describe('<ChatThread/> auto-follow', () => {
  it('pauses token following when the reader scrolls up and offers a jump control', () => {
    renderThread();
    expect(host.scrollTop).toBe(1_000);

    scrollTo(650); // 150px from the viewport bottom: outside the 80px threshold.
    const jump = host.querySelector<HTMLButtonElement>('.chat-jump-to-latest');
    expect(jump?.textContent).toBe('Jump to latest');
    expect(jump?.type).toBe('button');
    expect(jump?.style.position).toBe('sticky');
    expect(jump?.style.transition).toContain('var(--motion-base)');

    scrollHeight = 1_100;
    act(() => store.startTurn('question'));
    act(() => store.applyEvent('token', { text: 'streamed answer' }));
    flushAnimationFrame();
    expect(host.scrollTop).toBe(650);
    expect(host.querySelector('.chat-jump-to-latest')).not.toBeNull();
  });

  it('jumps to the bottom, hides the control, and resumes following new tokens', () => {
    renderThread();
    scrollTo(650);

    scrollHeight = 1_100;
    act(() => store.startTurn('question'));
    const jump = host.querySelector<HTMLButtonElement>('.chat-jump-to-latest')!;
    act(() => jump.click());
    expect(host.scrollTop).toBe(1_100);
    expect(host.querySelector('.chat-jump-to-latest')).toBeNull();

    scrollHeight = 1_250;
    act(() => store.applyEvent('token', { text: 'next token' }));
    flushAnimationFrame();
    expect(host.scrollTop).toBe(1_250);
  });

  it('resumes automatic following when the reader manually returns near bottom', () => {
    renderThread();
    act(() => store.startTurn('question'));
    scrollTo(650);
    expect(host.querySelector('.chat-jump-to-latest')).not.toBeNull();

    scrollTo(760); // 40px from the viewport bottom: inside the threshold.
    expect(host.querySelector('.chat-jump-to-latest')).toBeNull();

    scrollHeight = 1_150;
    act(() => store.applyEvent('token', { text: 'follow this token' }));
    flushAnimationFrame();
    expect(host.scrollTop).toBe(1_150);
  });
});
