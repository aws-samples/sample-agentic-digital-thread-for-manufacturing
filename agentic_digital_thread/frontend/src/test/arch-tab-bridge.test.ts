// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* arch-tab-bridge — a synchronous switchArchTab
 * request made BEFORE ArchitectureView lazily mounts must be stashed and
 * drained exactly once at mount; once the window global is registered the
 * request goes straight through. */
// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  consumePendingArchTab,
  consumePendingArchTabFocus,
  requestArchTab,
  requestArchTabFocus,
} from '../views/architecture/arch-tab-bridge';

type W = { switchArchTab?: (t: string) => void };

afterEach(() => {
  delete (window as unknown as W).switchArchTab;
  consumePendingArchTab(); // drain any leftover between tests
  consumePendingArchTabFocus();
  vi.restoreAllMocks();
});

describe('arch-tab-bridge', () => {
  it('stashes the tab when window.switchArchTab is not registered (pre-mount)', () => {
    requestArchTab('investigation');
    expect(consumePendingArchTab()).toBe('investigation');
    // Exactly once — a second drain returns null.
    expect(consumePendingArchTab()).toBeNull();
  });

  it('calls window.switchArchTab directly when registered (mounted)', () => {
    const spy = vi.fn();
    (window as unknown as W).switchArchTab = spy;
    requestArchTab('proposed');
    expect(spy).toHaveBeenCalledWith('proposed');
    expect(consumePendingArchTab()).toBeNull(); // nothing stashed
  });

  it('keeps only the LATEST pre-mount request', () => {
    requestArchTab('map');
    requestArchTab('investigation');
    expect(consumePendingArchTab()).toBe('investigation');
  });

  it('focus request pre-mount: stashed, drained exactly once', () => {
    requestArchTabFocus('investigation');
    expect(consumePendingArchTabFocus()).toBe('investigation');
    expect(consumePendingArchTabFocus()).toBeNull();
  });

  it('focus request when mounted: focuses that tab button one frame later, stashes nothing', () => {
    const frames: FrameRequestCallback[] = [];
    vi.spyOn(window, 'requestAnimationFrame').mockImplementation((cb) => {
      frames.push(cb);
      return frames.length;
    });
    (window as unknown as W).switchArchTab = vi.fn();
    const tabs = document.createElement('div');
    for (const id of ['map', 'investigation']) {
      const btn = document.createElement('button');
      btn.className = 'arch-tab';
      btn.dataset.archTab = id;
      tabs.append(btn);
    }
    document.body.append(tabs);
    try {
      requestArchTabFocus('investigation');
      expect(consumePendingArchTabFocus()).toBeNull();
      // Deferred: a closing dialog's focus restore runs before this frame.
      expect(document.activeElement).toBe(document.body);
      frames.forEach((cb) => cb(0));
      expect((document.activeElement as HTMLElement).dataset.archTab).toBe('investigation');
    } finally {
      tabs.remove();
    }
  });
});
